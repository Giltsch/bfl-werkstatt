"""Grounding for the Werkstatt assistant: turns spoken location phrases ("links neben dem Fenster") and object phrases
("die alte Kommode") into points and outlines on the room photo (0-1000 grid, points [x, y], boxes [top, left, bottom,
right]). Qwen points natively on that grid, which measured best on evalset/ground.json (see docs/system/ground.md); a
labelled grid overlay is kept as an alternative method. Points are checked against the room geometry (floor / wall /
ceiling, nearest wall) and objects are cut out with SAM 2.1. Never raises into the caller: a stopped VLM yields status
"unavailable"."""

import io
import math
import re
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from PIL import Image, ImageDraw, ImageFont

import judge

router = APIRouter()
OUT = Path(__file__).parent / "outputs"   # tests monkeypatch this

METHODS = ("native", "native_short", "grid", "grid2")
TIMEOUT = 60
MAX_PHRASE = 200
WALL_NEAR_CM = 60          # a floor point this close to a wall belongs to it (the piece will be snapped against it)
SAM_MIN_IOU = 0.4          # below this the SAM cut-out disagrees with the VLM's frame: keep the frame instead
SAM_MIN_FREE_GB = 18       # before loading SAM (~0.4 GB) next to everything else, as geometry_api does
GRID_COLS, GRID_ROWS = 12, 8
CONF = {"native": 0.85, "native_short": 0.8, "grid": 0.7, "grid2": 0.75}

_COORD = {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 1000}}
PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        # the reference comes first: written before the coordinates it works as a short chain of thought
        "reference_de": {"type": "string"},
        "found": {"type": "boolean"},
        "surface": {"type": "string", "enum": ["floor", "wall", "unclear"]},
        "point_2d": {**_COORD, "minItems": 2, "maxItems": 2},
    },
    "required": ["reference_de", "found", "surface", "point_2d"],
    "additionalProperties": False,
}
OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "label_de": {"type": "string"},
        "found": {"type": "boolean"},
        "bbox_2d": {**_COORD, "minItems": 4, "maxItems": 4},
        "point_2d": {**_COORD, "minItems": 2, "maxItems": 2},
    },
    "required": ["label_de", "found", "bbox_2d", "point_2d"],
    "additionalProperties": False,
}
# without the reference text: fewer output tokens (decode dominates the latency on the Spark)
SHORT_SCHEMA = {**PLACE_SCHEMA, "properties": {k: v for k, v in PLACE_SCHEMA["properties"].items() if k != "reference_de"},
                "required": ["found", "surface", "point_2d"]}
GRID_SCHEMA = {
    "type": "object",
    "properties": {"reference_de": {"type": "string"}, "found": {"type": "boolean"}, "cell": {"type": "string"}},
    "required": ["reference_de", "found", "cell"],
    "additionalProperties": False,
}

PLACE_SYSTEM = """You help a carpenter (Schreiner) plan furniture in a photo of a customer's room. The carpenter says in \
German WHERE a new piece of furniture should go. Find that spot in the photo: the place where the MIDDLE of the new piece \
would appear. Use the objects, walls, windows and corners named in the phrase as references ("links/rechts" = left/right \
in the photo; "hinten" = the far wall; "neben" = directly beside it, not on it; "unter dem Fenster" = the wall below the \
window; "wo X steht" = on X, which will be replaced; "Mitte des Raums" = the middle of the visible floor). The spot must \
be on the floor or on a wall of the room, never on the ceiling, outside a window or on the camera's side of the room.
"reference_de": the reference you used, in a few German words (e.g. "rechts neben dem Fernsehschrank, Rückwand").
"found": false only if the named reference is not in the photo at all.
"surface": "wall" if the spot is on a wall (the piece stands or hangs against it), "floor" if it is free floor.
"point_2d": [x, y] of the spot, coordinates normalised to 0-1000 over the image width and height.
Reply with the JSON object only."""

OBJECT_SYSTEM = """You find objects in a photo of a customer's room for a carpenter (Schreiner). The carpenter names an \
object in German (e.g. "die alte Kommode", "der Heizkörper unter dem Fenster", "die Sitzgruppe"). Find exactly that \
object; for a group (e.g. table with chairs) frame the whole group.
"label_de": what you found, in a few German words.
"found": false if no such object is visible.
"bbox_2d": [x1, y1, x2, y2] tight frame around the visible object, "point_2d": [x, y] a point clearly ON the object \
(not on a gap or the wall behind it); all coordinates normalised to 0-1000 over the image width and height.
Reply with the JSON object only."""

GRID_PLACE_TEXT = """The photo has a labelled grid: columns A-{lc} from left to right, rows 1-{rows} from top to bottom; \
every cell shows its label (e.g. "C4") in its top-left corner. {task} Answer with the ONE cell that contains that spot \
("cell"), plus "reference_de" and "found" as described."""

GRID_PLACE_SYSTEM = PLACE_SYSTEM.replace(
    '"surface": "wall" if the spot is on a wall (the piece stands or hangs against it), "floor" if it is free floor.\n', "")


# ---------------------------------------------------------------- small helpers

def _image(photo) -> Image.Image:
    """PIL image from an image, a path or raw bytes (always RGB, EXIF orientation applied)."""
    if isinstance(photo, Image.Image):
        return judge.rgb(photo)
    if isinstance(photo, (bytes, bytearray)):
        photo = io.BytesIO(photo)
    with Image.open(photo) as im:
        im.load()
        return judge.rgb(im)


def _clamp(v, lo=0.0, hi=1000.0) -> float:
    return max(lo, min(hi, float(v)))


def _point(p) -> list[float] | None:
    try:
        x, y = (float(c) for c in p)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return [round(_clamp(x), 1), round(_clamp(y), 1)]


def _box_xyxy(b) -> list[int] | None:
    """VLM frame [x1, y1, x2, y2] → codebase box [top, left, bottom, right] (sorted, clamped, never empty)."""
    try:
        x1, y1, x2, y2 = (_clamp(c) for c in b)
    except (TypeError, ValueError):
        return None
    l, r, t, bt = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    if r - l < 1 or bt - t < 1:
        return None
    return [int(t), int(l), int(math.ceil(bt)), int(math.ceil(r))]


def iou(a, b) -> float:
    """IoU of two [top, left, bottom, right] boxes."""
    if not a or not b:
        return 0.0
    t, l, bt, r = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, bt - t) * max(0.0, r - l)
    area = lambda x: max(0.0, x[2] - x[0]) * max(0.0, x[3] - x[1])
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def union_box(parts: list[dict]) -> list[int] | None:
    boxes = [p["box"] for p in parts or [] if p.get("box")]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def inside(point, box, margin: float = 0) -> bool:
    x, y = point
    return box[1] - margin <= x <= box[3] + margin and box[0] - margin <= y <= box[2] + margin


def _fields(text: str) -> dict | None:
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S)
    obj = judge.first_object(re.sub(r"```(?:json)?", "", text))
    d = judge.loads_lenient(obj) if obj else None
    return d if isinstance(d, dict) else None


def _result(status: str, t0: float, why: str = "", **kw) -> dict:
    return {"status": status, "point": None, "why": why, **kw, "seconds": round(time.time() - t0, 2)}


def _ask(system: str, img: Image.Image, text: str, schema: dict, name: str, timeout: float) -> dict | None:
    """One VLM call with one re-ask on unparseable output. Transport errors propagate (→ unavailable)."""
    content = [{"type": "image_url", "image_url": {"url": judge.data_url(img)}}, {"type": "text", "text": text}]
    for attempt in range(2):
        d = _fields(judge.ask(system, content, schema, name, timeout, max_tokens=200, temperature=0.3 * attempt))
        if d is not None:
            return d
    return None


def _guard(t0: float) -> dict | None:
    if not judge.status()["available"]:
        return _result("unavailable", t0, "KI-Modell nicht verfügbar")
    if not judge.wait_memory(limit=30):
        return _result("unavailable", t0, "Speicher knapp – bitte gleich noch einmal")
    return None


# ---------------------------------------------------------------- grid overlay

def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow < 10.1
        return ImageFont.load_default()


def cell_label(col: int, row: int) -> str:
    return f"{chr(65 + col)}{row + 1}"


def parse_cell(text: str, cols: int, rows: int) -> tuple[int, int] | None:
    m = re.search(r"\b([A-Za-z])\s*-?\s*(\d{1,2})\b", str(text or ""))
    if not m:
        return None
    col, row = ord(m.group(1).upper()) - 65, int(m.group(2)) - 1
    return (col, row) if 0 <= col < cols and 0 <= row < rows else None


def grid_overlay(img: Image.Image, cols: int = GRID_COLS, rows: int = GRID_ROWS, max_side: int = 1024) -> Image.Image:
    """The photo (downscaled) with a thin labelled grid; labels sit in the cells' top-left corners."""
    k = min(1.0, max_side / max(img.size))
    im = img.resize((max(16, round(img.width * k)), max(16, round(img.height * k))), Image.LANCZOS) if k < 1 else img.copy()
    W, H = im.size
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(max(11, min(W // cols, H // rows) // 5))
    for c in range(1, cols):
        d.line([(c * W / cols, 0), (c * W / cols, H)], fill=(255, 230, 0, 200), width=2)
    for r in range(1, rows):
        d.line([(0, r * H / rows), (W, r * H / rows)], fill=(255, 230, 0, 200), width=2)
    for c in range(cols):
        for r in range(rows):
            x, y, s = c * W / cols + 3, r * H / rows + 3, cell_label(c, r)
            bb = d.textbbox((x, y), s, font=font)
            d.rectangle((bb[0] - 2, bb[1] - 1, bb[2] + 2, bb[3] + 1), fill=(0, 0, 0, 170))
            d.text((x, y), s, fill=(255, 255, 255, 255), font=font)
    return Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")


def _grid_pick(img: Image.Image, task: str, region, cols: int, rows: int, timeout: float) -> tuple[list[float], dict] | None:
    """Asks for one cell of a grid laid over `region` ([top, left, bottom, right] of the full image, 0-1000) and returns
    the cell centre on the full image's grid."""
    t, l, b, r = region
    W, H = img.size
    crop = img.crop((round(l * W / 1000), round(t * H / 1000), round(r * W / 1000), round(b * H / 1000)))
    text = GRID_PLACE_TEXT.format(lc=chr(64 + cols), rows=rows, task=task)
    d = _ask(GRID_PLACE_SYSTEM, grid_overlay(crop, cols, rows), text, GRID_SCHEMA, "cell", timeout)
    if d is None:
        return None
    cell = parse_cell(d.get("cell"), cols, rows)
    if cell is None:
        return None
    c, rw = cell
    x = l + (c + 0.5) * (r - l) / cols
    y = t + (rw + 0.5) * (b - t) / rows
    return [round(x, 1), round(y, 1)], d


# ---------------------------------------------------------------- geometry check

def _vec(geo):
    import numpy as np
    import geometry
    fr = geometry._frame(geo)
    return np, fr


def surface_at(geo: dict | None, point) -> dict:
    """What the photo shows at a grid point according to the room geometry (planes only, furniture ignored):
    {"surface": "floor"|"wall"|"ceiling"|None, "wall": id|None, "x", "y", "z" (floor coords, cm), "depth" (cm)}.
    The nearest of floor plane, wall planes (within their extent, between floor and ceiling) and ceiling wins."""
    out = {"surface": None, "wall": None, "x": None, "y": None, "z": None, "depth": None}
    if not geo or not geo.get("ok") or not geo.get("floor") or not geo.get("size"):
        return out
    np, fr = _vec(geo)
    W, H = geo["size"]
    K = geo["intrinsics"]
    u, v = point[0] * W / 1000, point[1] * H / 1000
    ray = np.array([(u - K["cx"]) / K["fx"], (v - K["cy"]) / K["fy"], 1.0])
    cam = np.array([-fr["origin"] @ fr["right"], -fr["origin"] @ fr["up"], -fr["origin"] @ fr["forward"]])
    dirv = np.array([ray @ fr["right"], ray @ fr["up"], ray @ fr["forward"]])   # same t as the camera ray: t = depth Z
    ceil_h = (geo.get("ceiling") or {}).get("height")
    hits = []
    if dirv[1] < 0:
        t = -cam[1] / dirv[1]
        if t > 0:
            hits.append((t, "floor", None))
    if ceil_h and dirv[1] > 0:
        t = (ceil_h - cam[1]) / dirv[1]
        if t > 0:
            hits.append((t, "ceiling", None))
    for w in geo.get("walls") or []:
        n0, n1 = w["normal"]
        den = n0 * dirv[0] + n1 * dirv[2]
        if abs(den) < 1e-9:
            continue
        t = -(n0 * cam[0] + n1 * cam[2] + w["offset"]) / den
        if t <= 0:
            continue
        P = cam + t * dirv
        if P[1] < 0 or (ceil_h and P[1] > ceil_h) or not _covers(w, P[0], P[2]):
            continue
        hits.append((t, "wall", w["id"]))
    if not hits:
        return out
    t, kind, wid = min(hits, key=lambda h: h[0])
    P = cam + t * dirv
    out.update(surface=kind, wall=wid, x=round(float(P[0]), 1), y=round(float(P[1]), 1), z=round(float(P[2]), 1),
               depth=round(float(t), 1))
    return out


def _covers(w: dict, x: float, z: float, margin: float = 50.0) -> bool:
    """Mirrors W3D `covers`: the foot of (x, z) lies on the wall segment ± margin (walls without extent cover all)."""
    if not w.get("from") or not w.get("to"):
        return True
    ax, az = w["to"][0] - w["from"][0], w["to"][1] - w["from"][1]
    L = math.hypot(ax, az)
    if L < 1e-6:
        return True
    s = ((x - w["from"][0]) * ax + (z - w["from"][1]) * az) / L
    return -margin <= s <= L + margin


def nearest_wall(geo: dict, x: float, z: float) -> tuple[str, float] | None:
    """(wall id, distance cm) of the closest covering wall to a floor point, like W3D nearestWall."""
    best = None
    for w in geo.get("walls") or []:
        if not _covers(w, x, z):
            continue
        dist = abs(w["normal"][0] * x + w["normal"][1] * z + w["offset"])
        if best is None or dist < best[1]:
            best = (w["id"], round(dist, 1))
    return best


def check_place(geo: dict | None, point, claimed: str | None = None) -> dict:
    """Validates a place point against the geometry. → {"point", "surface", "wall", "penalty", "notes"}: points on the
    ceiling slide down onto the wall below (a piece never goes there), points that hit nothing lose confidence."""
    res = {"point": list(point), "surface": None, "wall": None, "penalty": 0.0, "notes": []}
    s = surface_at(geo, point)
    if s["surface"] is None and not (geo and geo.get("ok")):
        return res                                    # no usable geometry: nothing to check
    if s["surface"] == "ceiling":
        p = list(point)
        while p[1] < 1000 and surface_at(geo, p)["surface"] == "ceiling":
            p[1] = min(1000.0, p[1] + 10)
        s2 = surface_at(geo, p)
        if s2["surface"] in ("wall", "floor"):
            res["point"], s = [p[0], round(p[1], 1)], s2
            res["notes"].append("von der Decke auf die Wand darunter verschoben")
        res["penalty"] += 0.2
    if s["surface"] is None:
        res["penalty"] += 0.25
        res["notes"].append("Punkt liegt auf keiner erkannten Wand- oder Bodenfläche")
    res["surface"] = s["surface"] if s["surface"] in ("floor", "wall") else None
    if s["surface"] == "wall":
        res["wall"] = s["wall"]
    elif s["surface"] == "floor":
        nw = nearest_wall(geo, s["x"], s["z"])
        if nw and nw[1] <= WALL_NEAR_CM:
            res["wall"] = nw[0]
    if claimed in ("floor", "wall") and res["surface"] and claimed != res["surface"]:
        res["penalty"] += 0.1                     # mild: the middle of a standing piece often sits on the wall above floor
    return res


_REPLACE_RX = re.compile(r"\b(wo|anstelle|statt|an (der )?stelle)\b", re.I)


def snap_to_remove(doc: dict | None, phrase: str, point) -> list[float] | None:
    """"wo die alte Kommode steht": when the order already marks that spot for removal, the middle of the marked region
    is the better point than the VLM's guess on the old piece."""
    if not doc or not _REPLACE_RX.search(phrase or ""):
        return None
    for r in doc.get("removes") or []:
        box = r.get("box") if isinstance(r, dict) else r
        if isinstance(box, (list, tuple)) and len(box) == 4 and inside(point, box, margin=20):
            return [round((box[1] + box[3]) / 2, 1), round((box[0] + box[2]) / 2, 1)]
    return None


# ---------------------------------------------------------------- locate

def _wall_label(geo, wid) -> str:
    for w in (geo or {}).get("walls") or []:
        if w["id"] == wid:
            return w.get("label") or ""
    return ""


def locate(photo, phrase: str, geo: dict | None = None, doc: dict | None = None, method: str = "native",
           timeout: float = TIMEOUT) -> dict:
    """A location phrase → {"status": "ok"|"not_found"|"unavailable"|"error", "point": [x, y] (0-1000), "wall": id|None,
    "surface": "floor"|"wall"|None, "confidence": 0..1, "why": German text, "method", "seconds"}."""
    t0 = time.time()
    phrase = str(phrase or "").strip()[:MAX_PHRASE]
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    if not phrase:
        return _result("error", t0, "Keine Ortsangabe", wall=None, surface=None, confidence=0.0, method=method)
    fail = _guard(t0)
    if fail:
        return {**fail, "wall": None, "surface": None, "confidence": 0.0, "method": method}
    try:
        img = _image(photo)
        task = f'The carpenter says: "{phrase}".'
        claimed = None
        if method in ("native", "native_short"):
            short = method == "native_short"
            system = PLACE_SYSTEM.replace(
                '"reference_de": the reference you used, in a few German words (e.g. "rechts neben dem Fernsehschrank, Rückwand").\n',
                "") if short else PLACE_SYSTEM
            d = _ask(system, img, f"{task} Where is that spot?", SHORT_SCHEMA if short else PLACE_SCHEMA, "place", timeout)
            point = _point(d.get("point_2d")) if d else None
            claimed = d.get("surface") if d else None
        else:
            pick = _grid_pick(img, task + " Find the spot where the middle of the new piece would appear.", [0, 0, 1000, 1000],
                              GRID_COLS, GRID_ROWS, timeout)
            point, d = pick if pick else (None, None)
            if pick and method == "grid2":   # second pass: a finer grid on the 3 × 3 cells around the first answer
                cw, ch = 1000 / GRID_COLS, 1000 / GRID_ROWS
                region = [max(0, point[1] - 1.5 * ch), max(0, point[0] - 1.5 * cw),
                          min(1000, point[1] + 1.5 * ch), min(1000, point[0] + 1.5 * cw)]
                fine = _grid_pick(img, task + " Find the spot where the middle of the new piece would appear (this is a "
                                  "zoomed part of the photo).", region, 6, 6, timeout)
                if fine:
                    point = fine[0]
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        judge.mark_down()
        print(f"ground: unavailable ({type(e).__name__}: {e})", flush=True)
        return _result("unavailable", t0, "KI-Modell nicht verfügbar", wall=None, surface=None, confidence=0.0, method=method)
    except Exception as e:   # unreadable photo or a bug: report, never raise into the conversation
        print(f"ground: locate failed: {e!r}", flush=True)
        return _result("error", t0, "Ort konnte nicht bestimmt werden", wall=None, surface=None, confidence=0.0, method=method)
    if d is None or point is None:
        return _result("error", t0, "Ort konnte nicht bestimmt werden", wall=None, surface=None, confidence=0.0, method=method)
    ref = str(d.get("reference_de") or "").strip()[:120]
    if d.get("found") is False:
        return _result("not_found", t0, f"Im Foto nicht gefunden: {ref or phrase}", wall=None, surface=None,
                       confidence=0.2, method=method, reference=ref)
    conf = CONF[method]
    snapped = snap_to_remove(doc, phrase, point)
    if snapped:
        point = snapped
    chk = check_place(geo, point, claimed)
    conf = max(0.05, conf - chk["penalty"])
    why = ref or phrase
    if chk["wall"]:
        lbl = _wall_label(geo, chk["wall"])
        why += f" (Wand {lbl})" if lbl else ""
    if chk["notes"]:
        why += " – " + "; ".join(chk["notes"])
    return {"status": "ok", "point": chk["point"], "wall": chk["wall"], "surface": chk["surface"],
            "confidence": round(conf, 2), "why": why, "reference": ref, "method": method,
            "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- find_object

def _sam_candidates(img: Image.Image, box, point, prompt: str) -> list:
    """SAM masks for the VLM's answer: "box" prompts with the frame (+ the point), "point" with the point alone
    (segment.masks_at, the tap-to-select path)."""
    import segment
    W, H = img.size
    px = [point[0] * W / 1000, point[1] * H / 1000]
    if prompt == "point":
        return segment.masks_at(img, [px])[0]
    import torch
    t, l, b, r = box
    xyxy = [l * W / 1000, t * H / 1000, r * W / 1000, b * H / 1000]
    with segment._lock:
        model, proc = segment._load()
        inp = proc(images=img, input_boxes=[[xyxy]], input_points=[[[px]]], input_labels=[[[1]]],
                   return_tensors="pt").to("cuda")
        with torch.no_grad():
            out = model(**inp, multimask_output=True)
        masks = proc.post_process_masks(out.pred_masks.cpu(), inp["original_sizes"])[0][0].numpy()
    return [m.astype(bool) for m in masks if m.sum() >= 50]


def refine(img: Image.Image, box, point, prompt: str = "box") -> tuple[list[dict], float] | None:
    """SAM cut-out that matches the VLM frame best → (parts, IoU with the frame), or None when SAM is not available."""
    import segment
    if not segment.loaded() and judge.mem_available_gb() < SAM_MIN_FREE_GB:
        return None
    try:
        masks = _sam_candidates(img, box, point, prompt)
    except Exception as e:
        print(f"ground: SAM failed: {e!r}", flush=True)
        return None
    best = (None, -1.0)
    for m in masks:
        parts = segment.outline(m)
        if not parts:
            continue
        # compare the mask's own frame (the outline is grown a little for the eraser)
        import numpy as np
        ys, xs = np.nonzero(m)
        h, w = m.shape
        mb = [1000 * ys.min() / h, 1000 * xs.min() / w, 1000 * (ys.max() + 1) / h, 1000 * (xs.max() + 1) / w]
        score = iou(mb, box)
        if score > best[1]:
            best = (parts, score)
    return (best[0], round(best[1], 3)) if best[0] else None


def find_object(photo, phrase: str, refine_with_sam: bool = True, sam_prompt: str = "box",
                timeout: float = TIMEOUT) -> dict:
    """An object phrase → {"status", "point": [x, y], "box": [t, l, b, r] (VLM frame), "parts": [{"poly", "box"}],
    "refined": bool (SAM outline vs. plain frame), "iou", "label", "why", "seconds"}."""
    t0 = time.time()
    phrase = str(phrase or "").strip()[:MAX_PHRASE]
    empty = {"box": None, "parts": [], "refined": False, "iou": None, "label": ""}
    if not phrase:
        return _result("error", t0, "Kein Gegenstand genannt", **empty)
    fail = _guard(t0)
    if fail:
        return {**fail, **empty}
    try:
        img = _image(photo)
        d = _ask(OBJECT_SYSTEM, img, f'Find: "{phrase}".', OBJECT_SCHEMA, "object", timeout)
        t_vlm = time.time() - t0
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        judge.mark_down()
        print(f"ground: unavailable ({type(e).__name__}: {e})", flush=True)
        return _result("unavailable", t0, "KI-Modell nicht verfügbar", **empty)
    except Exception as e:
        print(f"ground: find_object failed: {e!r}", flush=True)
        return _result("error", t0, "Gegenstand konnte nicht gesucht werden", **empty)
    if d is None:
        return _result("error", t0, "Gegenstand konnte nicht gesucht werden", **empty)
    label = str(d.get("label_de") or "").strip()[:120]
    box, point = _box_xyxy(d.get("bbox_2d")), _point(d.get("point_2d"))
    if d.get("found") is False or box is None:
        return _result("not_found", t0, f"Im Foto nicht gefunden: {phrase}", **{**empty, "label": label})
    if point is None or not inside(point, box):
        point = [round((box[1] + box[3]) / 2, 1), round((box[0] + box[2]) / 2, 1)]
    t, l, b, r = box
    parts = [{"poly": [[l, t], [r, t], [r, b], [l, b]], "box": box}]
    refined, score, t1 = False, None, time.time()
    if refine_with_sam:
        got = refine(img, box, point, sam_prompt)
        if got:
            score = got[1]
            if got[1] >= SAM_MIN_IOU:
                parts, refined = got[0], True
    return {"status": "ok", "point": point, "box": box, "parts": parts, "refined": refined, "iou": score,
            "label": label, "why": label or phrase, "seconds": round(time.time() - t0, 2),
            "timing": {"vlm": round(t_vlm, 2), "sam": round(time.time() - t1, 2)}}


# ---------------------------------------------------------------- HTTP

def _room_image(rel) -> Image.Image | None:
    if not isinstance(rel, str) or not rel:
        return None
    p = (OUT / rel).resolve()
    if OUT.resolve() not in p.parents or not p.is_file():
        return None
    return _image(p)


@router.post("/api/workshop/{pid}/locate")
def api_locate(pid: str, body: dict):
    """{phrase, kind: "place"|"object"} → locate / find_object on the order's room photo (+ its geometry)."""
    import projects
    phrase = body.get("phrase") if isinstance(body, dict) else None
    if not isinstance(phrase, str) or not phrase.strip():
        raise HTTPException(400, "Bitte beschreiben, was gesucht wird.")
    kind = body.get("kind") or "place"
    if kind not in ("place", "object"):
        raise HTTPException(400, "Unbekannte Art – „place“ oder „object“.")
    doc = projects.load("workshop", pid)
    photo = _room_image(doc.get("room"))
    if photo is None:
        raise HTTPException(400, "Der Auftrag hat noch kein Raumfoto.")
    geo = doc.get("geometry") if isinstance(doc.get("geometry"), dict) and doc["geometry"].get("ok") else None
    try:
        import workshop
        exclusive = workshop.EXCLUSIVE        # never alongside a FLUX image (set by the server at start-up)
    except Exception:
        exclusive = lambda fn: fn()
    if kind == "place":
        return exclusive(lambda: locate(photo, phrase, geo, doc))
    return exclusive(lambda: find_object(photo, phrase))
