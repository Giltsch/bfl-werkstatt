"""The Werkstatt's server side: catalog, prompt building for room / free / sketch-render / edit jobs, and the server-side
record of variants (tablets suspend tabs and two devices may share a project, so the browser never writes variants).

Coordinates: boxes are [top, left, bottom, right] on the 0-1000 grid of the room photo; 3d placements are projected with
geometry.py (optional; without it the box sent by the client is used)."""

import copy
import hashlib
import json
import math
import random
import re
import string
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from PIL import Image, ImageDraw, ImageOps

import projects

try:
    import geometry
except ImportError:
    geometry = None
try:
    import judge
except ImportError:
    judge = None

HERE = Path(__file__).parent
OUT = HERE / "outputs"  # tests monkeypatch this
router = APIRouter()
SUBMIT = None  # set by configure(): submit(params, images) -> public job

MAX_PIECES = 4
MAX_REFS = 5  # room + guide + sketches + sample
SMALL_REF_PIXELS = 400_000  # non-room refs: the room alone must stay full size for keep_outside
COLORS = ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9"]  # = W3D.COLORS
ORD = ["the first image", "the second image", "the third image", "the fourth image", "the fifth image",
       "the sixth image", "the seventh image", "the eighth image", "the ninth image", "the tenth image"]


def _entries(rows):
    return {r[0]: {"de": r[1], "en": r[2], **({"dims": r[3]} if len(r) > 3 else {})} for r in rows}


CATALOG = {
    "pieces": _entries([
        ("schrank", "Einbauschrank", "fitted floor-to-ceiling built-in wardrobe with flush doors", [200, 240, 60]),
        ("kueche", "Küche", "fitted kitchen with base cabinets, wall cabinets and a worktop", [240, 220, 60]),
        ("regal", "Regalwand", "wall-to-wall built-in shelving unit", [240, 220, 35]),
        ("sideboard", "Sideboard", "low sideboard on a recessed plinth", [180, 75, 45]),
        ("tisch", "Esstisch", "solid wood dining table", [180, 76, 90]),
        ("bett", "Bett", "solid wood bed frame with headboard", [180, 95, 210]),
        ("treppe", "Treppe", "wooden staircase with matching handrail", [100, 280, 300]),
        ("garderobe", "Garderobe", "hallway wardrobe unit with a bench, coat hooks and shoe storage", [150, 200, 40]),
        ("bad", "Badmöbel", "vanity unit under the washbasin with drawers", [100, 55, 48]),
        ("sitzbank", "Sitzbank", "built-in window seat bench with storage drawers and a cushion", [160, 45, 50]),
        ("wand", "Wandverkleidung", "wooden wall panelling", [300, 250, 4]),
        ("tuer", "Tür", "solid wood interior door with frame", [90, 210, 10]),
    ]),
    "materials": _entries([
        ("eiche", "Eiche natur geölt", "natural oak with a clear oiled matte finish and visible straight grain"),
        ("eicheraeuchert", "Eiche geräuchert", "smoked oak, dark warm brown, oiled matte"),
        ("eicheweiss", "Eiche weiß geölt", "white-oiled oak, pale Scandinavian look"),
        ("nuss", "Nussbaum", "American walnut, rich dark brown with lively grain"),
        ("esche", "Esche", "light ash with pronounced straight grain"),
        ("kirsch", "Kirschbaum", "cherry wood, warm reddish brown"),
        ("fichte", "Fichte massiv", "solid spruce, light with small knots"),
        ("multiplex", "Multiplex Birke", "birch plywood with visible layered edges"),
        ("altholz", "Altholz", "reclaimed weathered old timber with saw marks"),
        ("weiss", "Lack weiß matt", "smooth matte white lacquer"),
        ("anthrazit", "Lack anthrazit", "smooth matte anthracite grey lacquer"),
        ("custom", "Eigene Angabe…", ""),
    ]),
    "handles": _entries([
        ("push", "grifflos (Push)", "handleless push-to-open fronts"),
        ("leiste", "Griffleiste", "integrated recessed grip rails"),
        ("messing", "Messing", "slim brushed brass bar handles"),
        ("schwarz", "Schwarz matt", "slim matte black bar handles"),
        ("leder", "Lederlaschen", "leather pull tabs"),
        ("knopf", "Holzknöpfe", "turned wooden knobs"),
    ]),
}

# The building shell is the customer's room, not the carpenter's work: it stays as photographed unless the carpenter
# asks for a room change ("room_changes").
SHELL = ("the walls and their exact shape, niches, alcoves, corners and wall color, the ceiling, the floor, windows, "
         "doors, radiators, pipes, sockets, light fittings")
TIDY = "Also tidy up: remove loose clutter such as papers, cables, cups and bags from the floor and surfaces."
UPSCALE_PROMPT = ("Reproduce the first image exactly: identical composition, characters, objects, colors and lighting, "
                  "rendered at higher resolution with crisp fine detail and clean textures.")
WALL_EN = {"hinten": "back", "links": "left", "rechts": "right"}


def keep_text(changes: str) -> str:
    if changes:
        return (f"Apart from that, keep {SHELL}, the lighting, perspective and camera exactly as in the photo. "
                "Photorealistic interior photograph.")
    return (f"Change only the furniture. The building itself is fixed: keep {SHELL}, the lighting, perspective and camera "
            "exactly as in the photo; do not add, remove, move or reshape any wall, niche, window or door. The new furniture "
            "fits into the existing room geometry. Photorealistic interior photograph.")


def configure(submit) -> None:
    global SUBMIT
    SUBMIT = submit


# ---------------- small helpers ----------------

def rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    return img.convert("RGB")


def _snap(v) -> int:
    return max(256, min(4096, round(int(v) / 16) * 16))


def dims_for(aspect: float, side: int) -> tuple[int, int]:
    h = (side * side / aspect) ** 0.5
    return _snap(h * aspect), _snap(h)


def _int(v):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 2000 else None


def _box(b):
    """A valid grid box (ints, clamped, non-degenerate) or None."""
    try:
        t, l, bo, r = (max(0, min(1000, int(round(float(x))))) for x in b)
    except (TypeError, ValueError):
        return None
    return [t, l, bo, r] if bo > t and r > l else None


def _pad(b):
    return [b[0], b[1], min(1000, b[2] + 15), b[3]]


def _uid(n: int, taken=()) -> str:
    while True:
        s = "".join(random.choices(string.ascii_lowercase + string.digits, k=n))
        if s not in taken:
            return s


def _uniq(items):
    return list(dict.fromkeys(x for x in items if x))


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _mat_en(p):
    return (p.get("material_custom") or "").strip() if p.get("material") == "custom" else \
        CATALOG["materials"].get(p.get("material"), {}).get("en", "")


def _short(p) -> str:
    """The piece type's first phrase ("fitted kitchen"), as the material-swap prompt names it."""
    t = CATALOG["pieces"].get(p.get("type"))
    return t["en"].split(" with ")[0] if t else "furniture"


def _pieces(doc):
    return [p for p in (doc.get("pieces") or []) if isinstance(p, dict)]


def _geo(doc):
    g = doc.get("geometry")
    return g if geometry is not None and isinstance(g, dict) and g.get("ok") and g.get("floor") else None


def _is3d(p):
    pl = p.get("placement") or {}
    return pl.get("mode") == "3d" and isinstance(pl.get("pose"), dict)


# ---------------- pieces ----------------

def piece_dims(piece: dict) -> tuple[int, int, int]:
    default = CATALOG["pieces"].get(piece.get("type"), {}).get("dims", [100, 100, 60])
    return tuple(_int(piece.get(k)) or default[i] for i, k in enumerate("whd"))


def _dims_used(piece):
    """The dims the prompt, judge and sheet carry: a 3d piece's wireframe was drawn with piece_dims, so all three."""
    return piece_dims(piece) if _is3d(piece) else tuple(_int(piece.get(k)) for k in "whd")


def labels(piece: dict) -> dict:
    mat = (piece.get("material_custom") or "").strip() if piece.get("material") == "custom" else \
        CATALOG["materials"].get(piece.get("material"), {}).get("de", "")
    return {"label": CATALOG["pieces"].get(piece.get("type"), {}).get("de", "Möbel"), "material_label": mat,
            "handles_label": CATALOG["handles"].get(piece.get("handles"), {}).get("de", "")}


def snapshot(doc: dict, piece_ids: list[str], boxes: dict) -> dict:
    by_id = {p.get("id"): p for p in _pieces(doc)}
    rows = []
    for pid in piece_ids:
        if pid in by_id:
            w, h, d = _dims_used(by_id[pid])
            rows.append({"id": pid, **labels(by_id[pid]), "w": w, "h": h, "d": d})
    return {"pieces": rows, "boxes": {k: list(b) for k, b in (boxes or {}).items() if k in piece_ids and b}}


def _rel_text(rel: dict | None) -> str:
    if not rel:
        return ""
    parts = []
    if rel.get("ceiling_ratio") and rel.get("ceiling_height"):
        parts.append(f"reaches {round(100 * rel['ceiling_ratio'])}% of the {round(rel['ceiling_height'])} cm ceiling height")
    if rel.get("wall_ratio") and rel.get("wall_label"):
        wall = WALL_EN.get(rel["wall_label"])
        # wall_length is the visible wall section behind the piece (e.g. a niche), not the whole room wall
        parts.append(f"covers about {round(100 * rel['wall_ratio'])}% of the width of the {wall + ' ' if wall else ''}wall section "
                     f"directly behind it (about {round(rel['wall_length'])} cm wide)" if rel.get("wall_length") else
                     f"covers about {round(100 * rel['wall_ratio'])}% of the width of the {wall + ' ' if wall else ''}wall")
    return "it " + " and ".join(parts) if parts else ""


def piece_desc(piece: dict, rel: dict | None = None) -> str:
    t = CATALOG["pieces"].get(piece.get("type"))
    w, h, d = _dims_used(piece)
    kind = t["en"] if t else ""
    ratio = (rel or {}).get("ceiling_ratio")
    if h and (ratio < 0.95 if ratio else h < 235):  # an explicit lower height beats the catalog's "floor-to-ceiling"
        kind = kind.replace("floor-to-ceiling ", "")
    s = f"a custom-made {kind}, built by a skilled carpenter" if t else "a custom-made piece of carpentry"
    if _mat_en(piece):
        s += f", made of {_mat_en(piece)}"
    if piece.get("handles") in CATALOG["handles"]:
        s += f", with {CATALOG['handles'][piece['handles']]['en']}"
    if (piece.get("text") or "").strip():
        s += f". Details: {piece['text'].strip()}"
    dm = [f"{w} cm wide" if w else "", f"{h} cm high" if h else "", f"{d} cm deep" if d else ""]
    if any(dm):
        s += ". Size exactly " + ", ".join(x for x in dm if x)
    if _rel_text(rel):
        s += "; " + _rel_text(rel)
    return s


def _relations(doc, piece, box):
    """geometry.relations for a visible 3d piece on a measured room, else None."""
    geo = _geo(doc)
    if not geo or not box or not _is3d(piece):
        return None
    try:
        return geometry.relations(geo, piece_dims(piece), piece["placement"]["pose"])
    except Exception:
        traceback.print_exc()
        return None


def resolve_boxes(doc: dict) -> tuple[dict, list[str]]:
    boxes, warns, geo = {}, [], _geo(doc)
    for k, p in enumerate(_pieces(doc), 1):
        pl = p.get("placement") or {}
        box = None
        if _is3d(p):
            if geo:
                try:
                    r = geometry.project_box(geo, piece_dims(p), pl["pose"])
                    box = _box(r["box"]) if r.get("visible") else None
                except Exception:
                    traceback.print_exc()
                    box = _box(pl.get("box") or ())
            else:
                box = _box(pl.get("box") or ())
            if box is None:
                warns.append(f"Möbel {k}: Das Möbel ragt aus dem Bild.")
        elif pl.get("mode") == "2d":
            box = _box(pl.get("box") or ())
        boxes[p.get("id")] = box
    return boxes, warns


def _validate(doc, need_desc=True):
    pieces = _pieces(doc)
    if len(pieces) > MAX_PIECES:
        raise HTTPException(400, "Höchstens 4 Möbel pro Projekt.")
    if not pieces:
        raise HTTPException(400, "Bitte Möbel auswählen oder beschreiben.")
    if need_desc:
        for k, p in enumerate(pieces, 1):
            if p.get("type") not in CATALOG["pieces"] and not (p.get("text") or "").strip():
                raise HTTPException(400, "Bitte Möbel auswählen oder beschreiben." if len(pieces) == 1
                                    else f"Möbel {k}: bitte auswählen oder beschreiben.")
    for p in pieces:
        if p.get("material") == "custom" and not (p.get("material_custom") or "").strip():
            raise HTTPException(400, "Bitte die eigene Oberfläche beschreiben.")
    return pieces


def _active(doc, pieces):
    return next((p for p in pieces if p.get("id") == doc.get("active_piece")), pieces[0])


def _design_ref(p):
    if p.get("use_render") and p.get("render"):
        return p["render"], "render"
    return (p["sketch"], "sketch") if p.get("sketch") else (None, None)


def _base(doc, width, height, count=None):
    return {"upsample": True, "mode": "distilled", "width": width, "height": height, "seed": -1,
            "count": count or max(1, min(4, int(doc.get("count") or 2))), "source": f"workshop:{doc.get('id')}"}


def _res(doc):
    return doc.get("res") if doc.get("res") in (1024, 1536) else 1536


def _judge_cfg(doc):
    j = doc.get("judge") if isinstance(doc.get("judge"), dict) else {}
    return j.get("enabled", True) is not False, j.get("auto_retry", True) is not False


def estimate_seconds(width, height, count, roles, upsample: bool = True) -> int:
    full = sum(1 for r in roles if r in ("room", "source"))
    small = len(roles) - full
    # the prompt upsampler (VLM, once per job) measured 130-150 s for a room job with boxes on the Spark
    return round((130 if upsample else 20) + count * (60 * width * height / (1776 * 1328) + 15 * max(0, full - 1) + 6 * small))


def _finish(params, refs, roles, warns):
    est = estimate_seconds(params["width"], params["height"], params["count"], roles, params.get("upsample", True))
    if est > 300:  # a normal 2-variant room job takes about 4 min
        warns.append(f"Dauer ca. {round(est / 60)} Min.")
    return {"params": params, "refs": refs, "roles": roles, "warnings": warns, "estimate_s": est}


def _collect_refs(doc, pieces, lead):
    """[lead refs…, design refs (active piece first, then piece order), samples (deduplicated)], capped at MAX_REFS.
    Returns (refs, roles, design {piece id: (index, kind)}, samples {piece id | None: index}, warnings)."""
    act = _active(doc, pieces)
    order = [act] + [p for p in pieces if p is not act]
    cand = list(lead)
    for p in order:
        path, kind = _design_ref(p)
        if path:
            cand.append((path, f"{kind}:{p.get('id')}"))
    for p in pieces:
        if p.get("sample"):
            cand.append((p["sample"], f"sample:{p.get('id')}"))
    if doc.get("sample") and any(not p.get("sample") for p in pieces):
        cand.append((doc["sample"], "sample"))
    refs, roles, slot, dropped = [], [], {}, []  # slot: role -> ref index
    for path, role in cand:
        if path in refs:  # the same image twice: one slot
            slot[role] = refs.index(path)
        elif len(refs) >= MAX_REFS:
            dropped.append(role)
        else:
            slot[role] = len(refs)
            refs.append(path)
            roles.append(role)
    design = {r.split(":", 1)[1]: (i, r.split(":")[0]) for r, i in slot.items() if r.startswith(("sketch:", "render:"))}
    samples = {(r.split(":", 1)[1] if ":" in r else None): i for r, i in slot.items() if r.startswith("sample")}
    warns = []
    n = sum(1 for r in dropped if not r.startswith("sample"))
    if n:
        warns.append(f"Zu viele Vorlagen – {n} Skizze(n) nicht mitgegeben (höchstens 5 Bilder).")
    if any(r.startswith("sample") for r in dropped):
        warns.append("Zu viele Vorlagen – Holzmuster nicht mitgegeben (höchstens 5 Bilder).")
    return refs, roles, design, samples, warns


def _sketch_ref(doc, pieces, design):
    """The one design ref the judge sees: the active piece's, else the first."""
    act = _active(doc, pieces).get("id")
    return design[act][0] if act in design else min((i for i, _ in design.values()), default=None)


def _design_sentences(pieces, design, samples, tag, name):
    """Sketch / render and material-sample sentences; tag(i) names reference i, name(k, p) names piece k."""
    out, single = [], len(pieces) == 1
    for k, p in enumerate(pieces, 1):
        if p.get("id") in design:
            i, kind = design[p["id"]]
            what = "the product rendering" if kind == "render" else "the carpenter's sketch"
            subj = "Its design" if name(k, p) is None else f"The design of {name(k, p)}"
            out.append(f"{subj} follows {what} in {tag(i)}.")
    for k, p in enumerate(pieces, 1):
        if p.get("id") in samples:
            subj = "Its wood color and grain match" if single else f"The wood color and grain of {name(k, p) or 'it'} match"
            out.append(f"{subj} the material sample in {tag(samples[p['id']])}.")
    if None in samples:
        out.append(f"{'Its' if single else 'The'} wood color and grain match the material sample in {tag(samples[None])}.")
    return out


# ---------------- guide image ----------------

def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def guide_image(room: Image.Image, doc: dict, boxes: dict) -> Image.Image:
    img = rgb(room).copy()
    W, H = img.size
    draw, width, geo = ImageDraw.Draw(img), max(2, round(0.004 * max(W, H))), _geo(doc)
    for k, p in enumerate(_pieces(doc)):
        box, col = boxes.get(p.get("id")), _hex(COLORS[k % len(COLORS)])
        if not box:
            continue
        if _is3d(p) and geo:
            gw, gh = geo.get("size") or [W, H]
            c = geometry.project_box(geo, piece_dims(p), p["placement"]["pose"])["corners"]
            for a, b in ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)):
                if c[a] and c[b]:
                    draw.line([(c[a][0] * W / gw, c[a][1] * H / gh), (c[b][0] * W / gw, c[b][1] * H / gh)], fill=col, width=width)
        else:
            draw.rectangle([box[1] * W / 1000, box[0] * H / 1000, box[3] * W / 1000, box[2] * H / 1000], outline=col, width=width)
    return img


def _guide_ref(doc, boxes):
    pieces = _pieces(doc)
    key = json.dumps([doc.get("room"), boxes, [(p.get("placement") or {}).get("pose") for p in pieces],
                      [piece_dims(p) for p in pieces]], sort_keys=True)
    rel = f"guides/{doc.get('id')}-{hashlib.sha1(key.encode()).hexdigest()[:10]}.png"
    if not (OUT / rel).exists():
        with Image.open(OUT / doc["room"]) as room:
            g = guide_image(room, doc, boxes)
        (OUT / "guides").mkdir(parents=True, exist_ok=True)
        g.save(OUT / rel)
    return rel


# ---------------- plans ----------------

def judge_request(doc: dict, kind: str, piece_ids: list[str], boxes: dict | None = None, edit_text: str = "") -> dict:
    boxes = boxes or {}
    rows = []
    for p in _pieces(doc):
        if p.get("id") not in piece_ids:
            continue
        box = boxes.get(p["id"])
        rel = _relations(doc, p, box)
        w, h, d = _dims_used(p)
        rows.append({"id": p["id"], "label": labels(p)["label"], "desc": piece_desc(p, rel), "w": w, "h": h, "d": d,
                     "material": _mat_en(p), "handles": CATALOG["handles"].get(p.get("handles"), {}).get("en", ""),
                     "box": box, "relation": _rel_text(rel)})
    return {"kind": kind, "pieces": rows,
            "removes": [b for b in map(_box, doc.get("removes") or []) if b] if kind == "room" else [],
            "room_changes": (doc.get("room_changes") or "").strip(), "tidy": bool(doc.get("tidy", True)),
            "protect": bool(doc.get("protect", True)), "edit_text": edit_text}


def _variant_meta(doc, kind, pieces, boxes, keep):
    lab = [labels(p) for p in pieces]
    ids = [p.get("id") for p in pieces]
    return {"kind": kind, "piece_ids": ids, "piece": " + ".join(l["label"] for l in lab),
            "material": " + ".join(_uniq(l["material_label"] for l in lab)),
            "handles": " + ".join(_uniq(l["handles_label"] for l in lab)), "note": "", "keep": keep, "from": None,
            "sketch": None, "upscaled": False, "retry_of": None, "room": doc.get("room") if kind == "room" else None,
            **snapshot(doc, ids, boxes)}


def build_room_job(doc: dict) -> dict:
    pieces = _validate(doc)
    if not doc.get("room"):
        return _build_free(doc, pieces)
    boxes, warns = resolve_boxes(doc)
    rels = {p["id"]: _relations(doc, p, boxes.get(p.get("id"))) for p in pieces}
    for k, p in enumerate(pieces, 1):
        r = rels[p["id"]] or {}
        if (r.get("ceiling_ratio") or 0) > 1:
            warns.append(f"Möbel {k}: Höher als die Decke ({round(r['ceiling_height'])} cm)!")
        if (r.get("wall_ratio") or 0) > 1:
            warns.append(f"Möbel {k}: Breiter als die Wand ({round(r['wall_length'])} cm)!")
    desc = {p["id"]: piece_desc(p, rels[p["id"]]) for p in pieces}
    placed = [(k, p) for k, p in enumerate(pieces, 1) if boxes.get(p.get("id"))]
    unplaced = [p for p in pieces if not boxes.get(p.get("id"))]
    removes = [b for b in map(_box, doc.get("removes") or []) if b]
    changes = (doc.get("room_changes") or "").strip()
    room_edit = f" Also change the room itself as requested: {changes}." if changes else ""
    lead = [(doc["room"], "room")]
    if doc.get("guide") and placed:
        try:
            lead.append((_guide_ref(doc, boxes), "guide"))
        except Exception:
            traceback.print_exc()
            warns.append("Umrisse konnten nicht gezeichnet werden.")
    refs, roles, design, samples, w2 = _collect_refs(doc, pieces, lead)
    warns += w2
    rw, rh = doc.get("room_size") or [4, 3]
    width, height = dims_for(rw / rh, _res(doc))
    params = _base(doc, width, height)
    tag = lambda i: f"<ref_image_{i}>"
    if placed or removes:
        rows = [{"id": f"moebel_{k}", "from": "target", "src_bbox": None, "tgt_bbox": boxes[p["id"]], "desc": desc[p["id"]]}
                for k, p in placed]
        rows += [{"id": f"weg_{i}", "from": "ref_image_0", "src_bbox": r, "tgt_bbox": None,
                  "desc": "an existing object that must disappear"} for i, r in enumerate(removes, 1)]
        out = []
        if placed:
            fit = "each precisely fitted" if len(placed) > 1 else "precisely fitted"
            adds = ", ".join(f"{desc[p['id']]} in <moebel_{k}>" for k, p in placed)
            out.append(f"In <ref_image_0>, add {adds}, {fit} to the walls, floor and ceiling around it.")
        for i, p in enumerate(unplaced):
            where = (p.get("where") or "").strip()
            spot = f"here: {where}" if where else "in the most suitable place"
            out.append(f"In <ref_image_0>, build {desc[p['id']]} {spot}." if not placed and i == 0
                       else f"Also build {desc[p['id']]} {spot}.")
        num = {p.get("id"): k for k, p in placed}
        # unplaced pieces have no box tag: name them by type (deviation from the spec, which only covers placed pieces)
        name = lambda k, p: f"<moebel_{num[p['id']]}>" if p.get("id") in num else (None if len(pieces) == 1 else f"the {_short(p)}")
        out += _design_sentences(pieces, design, samples, tag, name)
        if "guide" in roles:
            out.append(f"{tag(roles.index('guide'))} shows the same photo with the planned furniture outlines drawn in: "
                       "each piece fills exactly its outline. Do not draw any lines.")
        if removes:
            out.append(f"Remove {', '.join(f'<weg_{i}>' for i in range(1, len(removes) + 1))} and show the wall and floor behind.")
        if doc.get("tidy", True):
            out.append(TIDY)
        params.update(task="layout", boxes=rows, prompt=f"{' '.join(out)}{room_edit} {keep_text(changes)}")
    else:
        parts = [desc[p["id"]] + (f". Location: {p['where'].strip()}" if (p.get("where") or "").strip()
                                  else " in the most suitable place") for p in pieces]
        out = [f"In the first image, build {', and '.join(parts)}."]
        name = lambda k, p: None if len(pieces) == 1 else f"the {_short(p)}"
        out += _design_sentences(pieces, design, samples, lambda i: ORD[i], name)
        if doc.get("tidy", True):
            out.append(TIDY)
        params.update(task="edit", prompt=f"{' '.join(out)}{room_edit} {keep_text(changes)}")
    keep = None
    if doc.get("protect", True):
        if not changes and not unplaced:
            keep = [_pad(boxes[p["id"]]) for _, p in placed] + removes
        if unplaced:
            warns.append("Raum schützen wirkt erst, wenn alle Möbel platziert sind.")
    params["keep"] = keep
    enabled, retry = _judge_cfg(doc)
    ids = [p.get("id") for p in pieces]
    if enabled:
        sk = _sketch_ref(doc, pieces, design)
        params["judge"] = {"request": judge_request(doc, "room", ids, boxes), "room_ref": 0, "sketch_ref": sk}
    params["retry"] = 1 if enabled and retry else 0
    params["meta"] = {"variant": _variant_meta(doc, "room", pieces, {i: boxes[i] for i in ids if boxes.get(i)}, keep)}
    return _finish(params, refs, roles, warns)


def _build_free(doc, pieces):
    refs, roles, design, samples, warns = _collect_refs(doc, pieces, [])
    desc = " and ".join(piece_desc(p) for p in pieces)
    setting = (doc.get("setting") or "").strip() or "a bright, calm room with natural daylight"
    width, height = dims_for(4 / 3, _res(doc))
    params = _base(doc, width, height)
    if design:
        idx = sorted(design.values())
        kinds = {k for _, k in idx}
        what = "the product rendering" if kinds == {"render"} else "the carpenter's sketch"
        if len(idx) > 1:
            what += "es" if what.endswith("sketch") else "s"
        verb = "shown" if kinds == {"render"} else "drawn"
        prompt = (f"Photorealistic photograph of the finished furniture, built exactly as {verb} in {what} in "
                  f"{' and '.join(ORD[i] for i, _ in idx)}: {desc}. {'They stand' if len(pieces) > 1 else 'It stands'} in {setting}.")
    else:
        prompt = f"Professional interior photograph of {desc}, in {setting}."
    name = lambda k, p: None if len(pieces) == 1 else f"the {_short(p)}"
    smp = _design_sentences(pieces, {}, samples, lambda i: ORD[i], name)
    prompt += "".join(f" {s}" for s in smp)
    prompt += " Realistic proportions and craftsmanship details, eye-level camera, soft natural light."
    params.update(task="edit" if refs else "create", prompt=prompt, keep=None)
    enabled, retry = _judge_cfg(doc)
    ids = [p.get("id") for p in pieces]
    if enabled:
        sk = _sketch_ref(doc, pieces, design)
        params["judge"] = {"request": judge_request(doc, "free", ids), "room_ref": None, "sketch_ref": sk}
    params["retry"] = 1 if enabled and retry else 0
    params["meta"] = {"variant": _variant_meta(doc, "free", pieces, {}, None)}
    return _finish(params, refs, roles, warns)


def build_render_job(doc: dict, piece_id: str) -> dict:
    pieces = _validate(doc, need_desc=False)
    p = next((x for x in pieces if x.get("id") == piece_id), None)
    if p is None:
        raise HTTPException(404, "Möbel nicht gefunden.")
    if not p.get("sketch"):
        raise HTTPException(400, "Bitte zuerst eine Skizze hochladen.")
    try:
        with Image.open(OUT / p["sketch"]) as im:
            sw, sh = im.size  # uploads are stored EXIF-transposed
    except Exception:
        raise HTTPException(400, "Bitte zuerst eine Skizze hochladen.")
    refs, roles = [p["sketch"]], [f"sketch:{piece_id}"]
    sample = p.get("sample") or doc.get("sample")
    if sample:
        refs.append(sample)
        roles.append("sample")
    width, height = dims_for(min(1.6, max(0.75, sw / sh)), _res(doc))
    params = _base(doc, width, height)
    prompt = (f"Photorealistic studio product photograph of the finished piece of furniture drawn in the carpenter's sketch "
              f"in the first image: {piece_desc(p)}. Built exactly as drawn: same proportions, the same number and arrangement "
              "of doors, drawers, shelves and compartments; sketch lines, dimension lines, handwriting and annotations are not "
              "part of the object. All doors and drawers are closed, so the front looks exactly like the sketch; open shelves "
              "and compartments stay open only where the sketch draws them. It stands on a seamless light grey studio background with a soft shadow on the floor, soft "
              "diffused studio lighting, three-quarter view from slightly above eye level, the whole piece in frame.")
    if sample:
        prompt += " Its wood color and grain match the material sample in the second image."
    prompt += " Realistic proportions and craftsmanship details."
    params.update(task="edit", prompt=prompt, keep=None)
    enabled, retry = _judge_cfg(doc)
    if enabled:
        params["judge"] = {"request": judge_request(doc, "render", [piece_id]), "room_ref": None, "sketch_ref": 0}
    params["retry"] = 1 if enabled and retry else 0
    lab = labels(p)
    params["meta"] = {"variant": {"kind": "render", "piece_ids": [piece_id], "sketch": p["sketch"], "piece": lab["label"],
                                  "material": lab["material_label"], "handles": lab["handles_label"], "note": "", "keep": None,
                                  "from": None, "upscaled": False, "retry_of": None, **snapshot(doc, [piece_id], {})}}
    return _finish(params, refs, roles, [])


def _variant(doc, vid):
    v = next((x for x in doc.get("variants") or [] if x.get("id") == vid), None)
    if v is None:
        raise HTTPException(404, "Variante nicht gefunden.")
    return v


def build_edit_job(doc: dict, variant_id: str, op: str, material: str | None = None, text: str = "",
                   keep_only: bool = True, piece_id: str | None = None) -> dict:
    v = _variant(doc, variant_id)
    if op not in ("material", "edit", "upscale"):
        raise HTTPException(400, "Unbekannte Aktion.")
    w, h = v.get("size") or [1536, 1152]
    by_id = {p.get("id"): p for p in _pieces(doc)}
    ids = v.get("piece_ids") or list(by_id)
    if piece_id is not None and piece_id not in by_id:
        raise HTTPException(404, "Möbel nicht gefunden.")
    snap = {"pieces": copy.deepcopy(v["pieces"]), "boxes": copy.deepcopy(v.get("boxes") or {})} \
        if isinstance(v.get("pieces"), list) else snapshot(doc, ids, v.get("boxes") or {})
    keep = v.get("keep") or None
    meta = {"kind": op, "piece_ids": ids, "piece": v.get("piece", ""), "material": v.get("material", ""),
            "handles": v.get("handles", ""), "note": v.get("note", ""), "keep": keep, "from": v["id"], "sketch": None,
            "upscaled": bool(v.get("upscaled")), "retry_of": None}
    params = {"upsample": True, "mode": "distilled", "width": w, "height": h, "seed": -1, "count": 1,
              "source": f"workshop:{doc.get('id')}", "task": "edit", "keep": keep}
    edit_text = ""
    if op == "material":
        m = CATALOG["materials"].get(material)
        if not m or material == "custom":
            raise HTTPException(400, "Unbekanntes Material.")
        targets = [piece_id] if piece_id else ids
        kind = " and ".join(_uniq(_short(by_id[i]) for i in targets if i in by_id)) or "furniture"
        params["prompt"] = (f"In the first image, change only the material of the {kind} to {m['en']}. Keep its shape and "
                            "design, handles, the room, lighting and camera exactly unchanged.")
        edit_text = f"change only the material of the {kind} to {m['en']}"
        if piece_id:
            box = _box(snap["boxes"].get(piece_id) or ())
            if box:
                params["keep"] = [_pad(box)] + [b for b in map(_box, doc.get("removes") or []) if b]
        for row in snap["pieces"]:
            if row.get("id") in targets:
                row["material_label"] = m["de"]
        meta["material"] = " + ".join(_uniq(r.get("material_label") for r in snap["pieces"])) or m["de"]
    elif op == "edit":
        text = (text or "").strip()
        if not text:
            raise HTTPException(400, "Bitte die Änderung beschreiben.")
        params["prompt"] = (f"In the first image, {text}. Keep everything else exactly unchanged, especially the walls, niches, "
                            "windows, doors and floor.")
        params["keep"] = keep if keep_only else None
        meta["note"] = "; ".join(x for x in (v.get("note"), text) if x)
        edit_text = text
    else:
        if math.sqrt(w * h) >= 2000:
            raise HTTPException(400, "Das Bild ist schon groß genug.")
        tw, th = dims_for(w / h, min(2304, math.floor(math.sqrt(w * h) * 2 + 0.5)))
        params.update(prompt=UPSCALE_PROMPT, upsample=False, seed=7, width=tw, height=th)
        meta.update(upscaled=True, fav=bool(v.get("fav")))
    enabled, _ = _judge_cfg(doc)
    if enabled and op != "upscale":
        params["judge"] = {"request": judge_request(doc, "edit", ids, snap["boxes"], edit_text), "room_ref": 0, "sketch_ref": None}
    params["retry"] = 0
    params["meta"] = {"variant": {**meta, **snap}}
    return _finish(params, [v["image"]], ["source"], [])


def load_refs(refs: list[str], roles: list[str]) -> list[Image.Image]:
    root, out = OUT.resolve(), []
    for rel, role in zip(refs, roles):
        p = (OUT / rel).resolve()
        if root not in p.parents or not p.is_file():
            raise HTTPException(400, f"Bild fehlt: {rel}")
        with Image.open(p) as im:
            im.load()
            img = rgb(im)
        if role not in ("room", "source") and img.width * img.height > SMALL_REF_PIXELS:
            s = (SMALL_REF_PIXELS / (img.width * img.height)) ** 0.5
            img = img.resize((max(16, int(img.width * s)), max(16, int(img.height * s))), Image.LANCZOS)
        out.append(img)
    return out


# ---------------- server-side variant record ----------------

PROTECTED = ("id", "image", "size", "seed", "job", "judge")


def record(job: dict, judge_only: bool = False) -> None:
    try:
        p = job.get("params") or {}
        m = re.fullmatch(r"workshop:([a-z0-9]{6,32})", str(p.get("source") or ""))
        meta = (p.get("meta") or {}).get("variant") if isinstance(p.get("meta"), dict) else None
        if not m or not isinstance(meta, dict):
            return

        def fn(doc):
            variants = doc.setdefault("variants", [])
            by_image = {v.get("image"): v for v in variants}
            for r in job.get("results") or []:
                v = by_image.get(f"{r['id']}.png")
                if v is not None:
                    if "judge" in r:
                        v["judge"] = r["judge"]
                elif not judge_only and job.get("state") in ("done", "error"):  # error: images saved before the failure
                    v = {"fav": False, **{k: copy.deepcopy(x) for k, x in meta.items() if k not in PROTECTED},
                         "id": _uid(8, {x.get("id") for x in variants}), "image": f"{r['id']}.png",
                         "size": [r.get("width"), r.get("height")], "seed": r.get("seed"), "job": job.get("id"),
                         "judge": r.get("judge")}
                    variants.append(v)
                    by_image[v["image"]] = v
            if job.get("state") in ("done", "error", "cancelled") and job.get("judge_state") in (None, "off", "done"):
                pending = [x for x in doc.get("pending_jobs") or [] if x != job.get("id")]
                if job.get("retry_job") and job["retry_job"] not in pending:
                    pending.append(job["retry_job"])
                doc["pending_jobs"] = pending
            variants.sort(key=lambda v: v.get("image") or "", reverse=True)

        projects.mutate("workshop", m.group(1), fn)
    except HTTPException:
        return  # project deleted meanwhile
    except Exception:
        traceback.print_exc()


# ---------------- endpoints ----------------

def _unavailable(comment="Prüfer nicht verfügbar"):
    return {"status": "unavailable", "model": None, "scores": None, "overall": None, "pass": None, "comment": comment,
            "fix_hint": "", "seconds": 0.0, "graded": _now()}


def _submit(pid, plan, dry_run):
    if dry_run:
        return {"job": None, "plan": plan}
    if SUBMIT is None:
        raise HTTPException(503, "Server noch nicht bereit.")
    job = SUBMIT(plan["params"], load_refs(plan["refs"], plan["roles"]))

    def add(doc):
        pending = doc.setdefault("pending_jobs", [])
        if job["id"] not in pending:
            pending.append(job["id"])

    projects.mutate("workshop", pid, add)
    return {"job": job, "plan": plan}


@router.get("/api/workshop/catalog")
def catalog():
    return {**CATALOG, "max_pieces": MAX_PIECES, "max_refs": MAX_REFS}


# plain `def` endpoints: FastAPI runs them in its threadpool (disk I/O, image loading, the projects lock, the judge call)
@router.post("/api/workshop/{pid}/generate")
def generate(pid: str, req: dict):
    doc = projects.load("workshop", pid)
    kind = req.get("kind") or "room"
    if kind == "render":
        plan = build_render_job(doc, req.get("piece_id") or doc.get("active_piece"))
    elif kind in ("room", "free"):
        plan = build_room_job(doc if kind == "room" else {**doc, "room": None})
    else:
        raise HTTPException(400, "Unbekannte Art.")
    return _submit(pid, plan, bool(req.get("dry_run")))


@router.post("/api/workshop/{pid}/edit")
def edit(pid: str, req: dict):
    doc = projects.load("workshop", pid)
    plan = build_edit_job(doc, str(req.get("variant_id") or ""), req.get("op"), material=req.get("material"),
                          text=str(req.get("text") or ""), keep_only=req.get("keep_only", True) is not False,
                          piece_id=req.get("piece_id"))
    return _submit(pid, plan, bool(req.get("dry_run")))


def _open(rel):
    if not rel:
        return None
    p = (OUT / rel).resolve()
    if OUT.resolve() not in p.parents or not p.is_file():
        return None
    with Image.open(p) as im:
        im.load()
        return rgb(im)


@router.post("/api/workshop/{pid}/judge/{vid}")
def regrade(pid: str, vid: str):
    doc = projects.load("workshop", pid)
    v = _variant(doc, vid)
    if judge is None:
        return _unavailable()
    kind = v.get("kind") or ("room" if doc.get("room") else "free")
    ids = v.get("piece_ids") or [p.get("id") for p in _pieces(doc)]
    by_id = {p.get("id"): p for p in _pieces(doc)}
    sketch = None
    if kind == "render":
        req, room, sketch = judge_request(doc, "render", ids), None, _open(v.get("sketch"))
    elif kind in ("room", "free"):
        # the photo the variant was made from (an older variant may predate a new room photo)
        req, room = judge_request(doc, kind, ids, v.get("boxes") or {}), _open(v.get("room") or doc.get("room")) if kind == "room" else None
        cand = [doc.get("active_piece")] + ids
        sketch = next((img for i in cand if i in by_id and (img := _open(_design_ref(by_id[i])[0]))), None)
    else:
        parent = next((x for x in doc.get("variants") or [] if x.get("id") == v.get("from")), None)
        text = v.get("note") or ("reproduce the image at higher resolution without changes" if kind == "upscale" else "")
        req = judge_request(doc, "edit", ids, v.get("boxes") or {}, text)
        room = _open(parent["image"]) if parent else _open(doc.get("room"))
    result = _open(v.get("image"))
    if result is None:
        raise HTTPException(404, "Bild der Variante fehlt.")
    verdict = judge.grade({"room": room, "result": result, "sketch": sketch}, req)

    def put(d):
        for x in d.get("variants") or []:
            # a judge outage never overwrites a real verdict
            if x.get("id") == vid and (verdict.get("status") == "ok" or (x.get("judge") or {}).get("status") != "ok"):
                x["judge"] = verdict

    projects.mutate("workshop", pid, put)
    return verdict


@router.post("/api/workshop/{pid}/sketch_dims")
def sketch_dims(pid: str, req: dict):
    doc = projects.load("workshop", pid)
    p = next((x for x in _pieces(doc) if x.get("id") == req.get("piece_id")), None)
    if p is None:
        raise HTTPException(404, "Möbel nicht gefunden.")
    img = _open(p.get("sketch"))
    if img is None:
        raise HTTPException(400, "Bitte zuerst eine Skizze hochladen.")
    nulls = {k: None for k in ("w", "h", "d", "doors", "drawers")}
    if judge is None or not hasattr(judge, "read_sketch"):
        return {"status": "unavailable", **nulls, "comment": "Prüfer nicht verfügbar"}
    try:
        return judge.read_sketch(img)
    except Exception:
        traceback.print_exc()
        return {"status": "error", **nulls, "comment": "Bewertung fehlgeschlagen"}
