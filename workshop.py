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

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from PIL import Image, ImageDraw, ImageFilter, ImageOps

import projects
from jobs import eta_text as jobs_eta_text

try:
    import geometry
    import mockup
except ImportError:
    geometry = mockup = None
try:
    import judge
except ImportError:
    judge = None

HERE = Path(__file__).parent
OUT = HERE / "outputs"  # tests monkeypatch this
router = APIRouter()
BUSY = None      # set by configure(): () -> bool, the GPU has work (no caption prefetch then)
SUBMIT = None    # set by configure(): scheduler.submit(params, images, owner=…, …) -> JobView
ESTIMATE = None  # estimate(params, ref_sizes) -> seconds (the scheduler's calibrated model)
JUDGE_EST = None  # judge_est(n_images) -> seconds of KI grading after the images (0 when the judge is down)
WAIT = None      # wait(owner) -> seconds until a new job of that owner would start
EXCLUSIVE = lambda fn: fn()  # exclusive(fn): runs a judge call from an HTTP thread never alongside a FLUX image

MAX_PIECES = 4
MAX_REFS = 5  # room + guide + sketches + sample
SMALL_REF_PIXELS = 400_000  # non-room refs: the room alone must stay full size for keep_outside
COLORS = ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9"]  # = W3D.COLORS
# steps of the `entwurf` preset, room jobs only; every other job runs 16 steps. tools/ab_steps.py, 2026-10-17: 3 rooms ×
# seeds 11/22/33, judge means 8 vs 16 steps: overall 6.92 vs 6.86, pass rate 44 % vs 22 %, artifacts 6.56 vs 6.78
# (rule: ≥ −0.3 / ≥ −10 pp / ≥ −0.5 → ok), 32.8 s vs 62.2 s per image
STEPS_DRAFT = 8
QUALITY = {"entwurf": {"res": 1536, "steps": STEPS_DRAFT, "label": "Entwurf (schneller)"},
           "fein": {"res": 1536, "steps": 16, "label": "Fein (für den Kunden)"}}
FULL_STEPS = 16
COMMON_MATERIALS = ["eiche", "eicheweiss", "nuss", "weiss"]
SKIP_PUP_FOR: set = set()  # job kinds that send the template caption ({"material"} if its A/B passes)
# a generate with base_rev is refused when someone else changed one of these since (projects.changed_since)
DESIGN = ("pieces", "room", "room_size", "removes", "geometry", "calibration", "protect", "tidy", "guide", "mockup",
          "room_changes", "setting", "sample", "count", "quality", "judge")
ORD = ["the first image", "the second image", "the third image", "the fourth image", "the fifth image",
       "the sixth image", "the seventh image", "the eighth image", "the ninth image", "the tenth image"]


def _entries(rows):
    return {r[0]: {"de": r[1], "en": r[2], **({"dims": r[3]} if len(r) > 3 else {}), **({"elev": r[4]} if len(r) > 4 else {})}
            for r in rows}


CATALOG = {
    "pieces": _entries([
        ("schrank", "Einbauschrank", "fitted floor-to-ceiling built-in wardrobe with flush doors", [200, 240, 60]),
        ("kueche", "Küche", "fitted kitchen with base cabinets, wall cabinets and a worktop", [240, 220, 60]),
        ("regal", "Regalwand", "wall-to-wall built-in shelving unit", [240, 220, 35]),
        ("sideboard", "Sideboard", "low sideboard on a recessed plinth", [180, 75, 45]),
        ("wandregal", "Wandregal", "wall-mounted open shelf with floating boards", [120, 40, 25], 150),
        ("haengeschrank", "Hängeschrank", "wall-mounted cabinet with flush doors", [120, 60, 35], 140),
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
SLOPE_TOL = 3   # cm a piece may touch a sloped ceiling before it counts as reaching into it
TIDY = "Also tidy up: remove loose clutter such as papers, cables, cups and bags from the floor and surfaces."
KEEP_OTHERS = ("Existing furniture that is not being replaced, such as benches, sofas, chairs, plants and lamps, stays where it is "
               "unless it stands inside a marked area.")
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


def configure(submit, estimate=None, wait=None, exclusive=None, busy=None, judge_est=None) -> None:
    global SUBMIT, ESTIMATE, WAIT, EXCLUSIVE, BUSY, JUDGE_EST
    SUBMIT, ESTIMATE, WAIT, BUSY, JUDGE_EST = submit, estimate, wait, busy, judge_est
    EXCLUSIVE = exclusive or (lambda fn: fn())


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


def _removes(doc) -> list:
    """„Entfernen“ areas as keep regions: frames stay [t, l, b, r], tapped outlines become {"poly"}."""
    out = []
    for r in doc.get("removes") or []:
        if isinstance(r, dict) and isinstance(r.get("poly"), list) and _box(r.get("box") or ()):
            out.append({"poly": r["poly"]})
        elif _box(r):
            out.append(_box(r))
    return out


def _rkeep(removes) -> list:
    """Remove areas as keep regions, marked so keep.py gives them a wider margin (the old piece's shadow and side) and
    takes everything changed next to them from the edit."""
    return [{**r, "remove": True} if isinstance(r, dict) else {"box": r, "remove": True} for r in removes]


def _rbox(r):
    """The frame around a remove region."""
    if isinstance(r, dict):
        xs, ys = [p[0] for p in r["poly"]], [p[1] for p in r["poly"]]
        return [int(min(ys)), int(min(xs)), int(math.ceil(max(ys))), int(math.ceil(max(xs)))]
    return r


def _pad(b):
    return [b[0], b[1], min(1000, b[2] + 15), b[3]]


def _outline(geo, dims, pose):
    """A projected 3d box as a {"poly"} region on the 0-1000 grid, or None when it is not (fully) usable."""
    r = geometry.project_box(geo, dims, pose)
    if not r.get("visible") or len(r.get("hull") or []) < 3:
        return None
    W, H = geo["size"]
    return {"poly": [[round(1000 * u / W, 1), round(1000 * v / H, 1)] for u, v in r["hull"]][:32]}


def keep_regions_for(doc, piece, box):
    """Where FLUX may change the photo for one placed piece: its exact 3d silhouette plus, for a standing piece, the floor
    around its footprint (contact shadow). A 2d frame (or a failed projection) keeps the padded rectangle."""
    geo = _geo(doc)
    if not geo or not _is3d(piece):
        return [_pad(box)]
    try:
        pose, (w, h, d) = piece_pose(piece), piece_dims(piece)
        body = _outline(geo, (w, h, d), pose)
        if body is None:
            return [_pad(box)]
        if piece.get("slope_fit") and geo.get("slopes"):
            # fitted to a Dachschräge: only what stays under the slope may change – a roof window or the slope above
            # stays the photo's (a piece drawn a little taller than measured is still taken, see keep._continues)
            hull = geometry.slope_hull(geo, (w, h, d), pose)
            if hull and len(hull) >= 3:
                W_, H_ = geo["size"]
                body = {"poly": [[round(1000 * u / W_, 1), round(1000 * v / H_, 1)] for u, v in hull][:32]}
        out = [body]
        if not pose.get("y"):
            shadow = _outline(geo, (w * 1.2 + 20, 2, d * 1.2 + 20), pose)
            if shadow:
                out.append(shadow)
        return out
    except Exception:
        traceback.print_exc()
        return [_pad(box)]


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
    if geometry is None or not isinstance(g, dict) or not g.get("ok") or not g.get("floor"):
        return None
    return geometry.with_slopes(g)   # measurements from before slope detection get their Dachschrägen too


def _is3d(p):
    pl = p.get("placement") or {}
    return pl.get("mode") == "3d" and isinstance(pl.get("pose"), dict)


# ---------------- pieces ----------------

def piece_pose(piece: dict) -> dict:
    """The stored 3d pose plus the piece's elevation (cm above the floor) as y, the form geometry.py projects."""
    return {**piece["placement"]["pose"], "y": max(0, _int(piece.get("elev")) or 0)}


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


def slope_over(rel: dict | None) -> float:
    """cm by which a piece reaches into a sloped ceiling (0 = fits, or no slope there)."""
    return float(((rel or {}).get("slope") or {}).get("over") or 0)


def _rel_text(rel: dict | None) -> str:
    if not rel:
        return ""
    parts = []
    sl = rel.get("slope") or {}
    if rel.get("slope_fit") and slope_over(rel) > 0:
        parts.append(f"stands under the sloped ceiling: its top is cut at an angle along the slope, so it is only "
                     f"about {round(sl['lowest'])} cm high at its lowest side and fills the space up to the slope without a gap")
    elif rel.get("ceiling_ratio") and rel.get("ceiling_height"):
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
    elev = _int(piece.get("elev")) or 0
    if elev > 0:
        s += f". It is mounted on the wall and does not touch the floor: its underside hangs {elev} cm above the floor"
    if piece.get("slope_fit") and slope_over(rel) > 0:
        s += (". Built to fit under the roof slope (Dachschräge): the top and the doors follow the slope at an angle, "
              "like a carpenter's fitted knee-wall cabinet")
    if _rel_text(rel):
        s += "; " + _rel_text(rel)
    return s


def _relations(doc, piece, box):
    """geometry.relations for a visible 3d piece on a measured room, else None."""
    geo = _geo(doc)
    if not geo or not box or not _is3d(piece):
        return None
    try:
        return {**geometry.relations(geo, piece_dims(piece), piece_pose(piece)), "slope_fit": bool(piece.get("slope_fit"))}
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
                    r = geometry.project_box(geo, piece_dims(p), piece_pose(p))
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


def _quality(doc):
    q = doc.get("quality")
    return q if q in QUALITY else ("entwurf" if doc.get("res") == 1024 else "fein")  # = the v3 migration rule


def _base(doc, width, height, count=None, steps=FULL_STEPS):
    return {"upsample": True, "mode": "distilled", "width": width, "height": height, "seed": -1, "steps": steps,
            "count": count or max(1, min(4, int(doc.get("count") or 2))), "source": f"workshop:{doc.get('id')}"}


def _res(doc):
    return QUALITY[_quality(doc)]["res"]


def _judge_cfg(doc):
    j = doc.get("judge") if isinstance(doc.get("judge"), dict) else {}
    return j.get("enabled", True) is not False, j.get("auto_retry", True) is not False


CAPTION_S = {"i2i_bbox": 61, "i2i": 13, "t2i_bbox": 50, "t2i": 20}  # vLLM pup, measured (upsampler.py start values)


def estimate_seconds(params: dict, ref_sizes: list) -> int:
    """Fallback when the scheduler's calibrated model is not configured: caption + per image (2 + steps × step_s + 6)."""
    mode = ("i2i" if ref_sizes else "t2i") + ("_bbox" if params.get("boxes") else "")
    caption = CAPTION_S[mode] if params.get("upsample", True) else 0
    pixels = params["width"] * params["height"] + sum(min(w * h, 1024 * 1024) for w, h in ref_sizes)
    step_s = max(0.3, 0.27e-3 * pixels / 256 - 0.3)
    return round(caption + params["count"] * (2 + params.get("steps", FULL_STEPS) * step_s + 6))


def _ref_sizes(refs, roles):
    """The sizes load_refs would hand to the job (non-room refs shrunk), read from the file headers only."""
    out = []
    for rel, role in zip(refs, roles):
        try:
            with Image.open(OUT / rel) as im:
                w, h = im.size
        except Exception:
            continue
        if role not in ("room", "source") and w * h > SMALL_REF_PIXELS:
            s = (SMALL_REF_PIXELS / (w * h)) ** 0.5
            w, h = max(16, int(w * s)), max(16, int(h * s))
        out.append((w, h))
    return out


def _finish(params, refs, roles, warns, label=""):
    sizes = _ref_sizes(refs, roles)
    est = ESTIMATE(params, sizes) if ESTIMATE else estimate_seconds(params, sizes)
    return _timing({"params": params, "refs": refs, "roles": roles, "warnings": warns, "estimate_s": round(est), "label": label})


def _timing(plan):
    """judge_s and the German line „Bilder in ca. 3½ Min., Bewertung ca. 30 Sek. danach“ (same text as a running job's)."""
    p = plan["params"]
    js = round(JUDGE_EST(p.get("count") or 1)) if JUDGE_EST and isinstance(p.get("judge"), dict) else 0
    plan.update(judge_s=js, estimate_text=jobs_eta_text(plan["estimate_s"], js))
    return plan


def _count_label(n):
    return "1 Vorschlag" if n == 1 else f"{n} Vorschläge"


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


def _erase(img: Image.Image, removes: list) -> Image.Image:
    """The photo with the remove regions inpainted (OpenCV Telea at ≤ 640 px, upscaled into the masked area only)."""
    import cv2
    W, H = img.size
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    for reg in removes:
        if isinstance(reg, dict):
            d.polygon([(x * W / 1000, y * H / 1000) for x, y in reg["poly"]], fill=255)
        else:
            r = _rbox(reg)
            d.rectangle([r[1] * W / 1000, r[0] * H / 1000, r[3] * W / 1000, r[2] * H / 1000], fill=255)
    m = m.filter(ImageFilter.MaxFilter(2 * max(2, round(0.008 * max(W, H))) + 1))   # its edge and contact shadow too
    k = min(1.0, 640 / max(W, H))
    sw, sh = max(1, round(W * k)), max(1, round(H * k))
    small = cv2.cvtColor(np.asarray(img.resize((sw, sh), Image.LANCZOS)), cv2.COLOR_RGB2BGR)
    sm = (np.asarray(m.resize((sw, sh), Image.NEAREST)) > 0).astype(np.uint8) * 255
    filled = cv2.inpaint(small, sm, max(3, round(0.02 * max(sw, sh))), cv2.INPAINT_TELEA)
    big = Image.fromarray(cv2.cvtColor(filled, cv2.COLOR_BGR2RGB)).resize((W, H), Image.BICUBIC)
    big = big.filter(ImageFilter.GaussianBlur(max(1, round(0.004 * max(W, H)))))
    out = img.copy()
    out.paste(big, (0, 0), m.filter(ImageFilter.GaussianBlur(2)))
    return out


def guide_image(room: Image.Image, doc: dict, boxes: dict, pieces=None) -> Image.Image:
    """The photo with the planned furniture drawn in: 3d pieces as a shaded, divided mock-up (mockup.py), 2d frames as
    coloured rectangles."""
    pieces = _pieces(doc) if pieces is None else pieces
    img = rgb(room).copy()
    W, H = img.size
    if max(W, H) > 1280:  # a reference is fitted to ~1 MP anyway
        img = img.resize((round(W * 1280 / max(W, H)), round(H * 1280 / max(W, H))), Image.LANCZOS)
        W, H = img.size
    geo = _geo(doc)
    removes = _removes(doc)
    if removes:  # what must disappear: filled in from the wall and floor around it, so the reference shows the empty
        img = _erase(img, removes)  # spot (a grey blur there came back as grey cloth or smudges in the image)
    items = [(p, piece_dims(p), piece_pose(p), mockup.WOOD.get(p.get("material"), (200, 170, 130)))
             for p in pieces if boxes.get(p.get("id")) and _is3d(p) and geo]
    if items:
        depth = None
        try:
            depth = geometry.depth_map(doc["room"]) if doc.get("room") else None
            if depth is not None:  # removed things never hide the new furniture
                depth = depth.copy()
                dh, dw = depth.shape
                for r in map(_rbox, removes):
                    depth[r[0] * dh // 1000:r[2] * dh // 1000 + 1, r[1] * dw // 1000:r[3] * dw // 1000 + 1] = np.inf
        except Exception:
            traceback.print_exc()
            depth = None
        img = mockup.render(img, geo, items, depth)
    draw, width = ImageDraw.Draw(img), max(2, round(0.004 * max(W, H)))
    for k, p in enumerate(pieces):
        box = boxes.get(p.get("id"))
        if box and not (_is3d(p) and geo):
            draw.rectangle([box[1] * W / 1000, box[0] * H / 1000, box[3] * W / 1000, box[2] * H / 1000],
                           outline=_hex(COLORS[k % len(COLORS)]), width=width)
    return img


def _guide_ref(doc, boxes, pieces=None, lead_rel=None):
    pieces = _pieces(doc) if pieces is None else pieces
    lead_rel = lead_rel or doc.get("room")
    key = json.dumps([lead_rel, boxes, doc.get("removes"), [(p.get("placement") or {}).get("pose") for p in pieces], [piece_dims(p) for p in pieces],
                      [(p.get("type"), p.get("text"), p.get("elev"), p.get("material"), bool(p.get("slope_fit"))) for p in pieces],
                      (_geo(doc) or {}).get("scale"), (_geo(doc) or {}).get("slopes"), "mockup-2"], sort_keys=True)
    rel = f"guides/{doc.get('id')}-{hashlib.sha1(key.encode()).hexdigest()[:10]}.png"
    src = (OUT / str(lead_rel or "")).resolve()
    if OUT.resolve() not in src.parents:
        raise HTTPException(400, "Ungültiges Raumfoto.")
    if not (OUT / rel).exists():
        with Image.open(src) as room:
            g = guide_image(room, doc, boxes, pieces)
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
            "removes": [_rbox(r) for r in _removes(doc)] if kind == "room" else [],
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


def _same_piece(p, row, old_box, box) -> bool:
    """The piece looks as it did when `row` (a variant's snapshot) was made: same type, size and wood, same spot."""
    w, h, d = _dims_used(p)
    lab = labels(p)
    if (row.get("label"), row.get("material_label"), row.get("w"), row.get("h"), row.get("d")) != \
            (lab["label"], lab["material_label"], w, h, d):
        return False
    return not (old_box and box) or max(abs(a - b) for a, b in zip(old_box, box)) <= 8


def base_candidate(doc: dict, pieces: list, boxes: dict, want=None):
    """A variant to add the new pieces to, keeping the furniture it already shows: (variant, kept, new) or None.
    `want` False = never, a variant id = only that one, None = the starred one, else the best graded, else the newest.
    Only when every piece it shows is unchanged and every new piece is placed (the new box is the only edit area)."""
    if want is False or not doc.get("room"):
        return None
    cands = [v for v in doc.get("variants") or [] if v.get("kind") in ("room", "edit", "material")
             and isinstance(v.get("pieces"), list) and v.get("image") and (v.get("room") or v.get("from"))]
    if want:
        cands = [v for v in cands if v.get("id") == want]
    else:
        ok = lambda v: (v.get("judge") or {}).get("pass") is True
        cands = [v for v in cands if v.get("fav")] + [v for v in cands if not v.get("fav") and ok(v)]
    for v in cands:
        snap = {r.get("id"): r for r in v["pieces"]}
        old = [p for p in pieces if p.get("id") in snap]
        new = [p for p in pieces if p.get("id") not in snap]
        if not old or not new or not all(boxes.get(p.get("id")) for p in new):
            continue
        if all(_same_piece(p, snap[p["id"]], (v.get("boxes") or {}).get(p["id"]), boxes.get(p["id"])) for p in old):
            return v, old, new
    return None


def build_room_job(doc: dict) -> dict:
    pieces = _validate(doc)
    if not doc.get("room"):
        return _build_free(doc, pieces)
    boxes, warns = resolve_boxes(doc)
    all_pieces, base = pieces, base_candidate(doc, pieces, boxes, doc.get("base"))
    if base:
        base_v, kept, pieces = base
        # „Entfernen“ frames belong to the original photo: on a chosen proposal they would erase the furniture that stays
        doc = {**doc, "removes": []}
    rels = {p["id"]: _relations(doc, p, boxes.get(p.get("id"))) for p in pieces}
    for k, p in enumerate(pieces, 1):
        r = rels[p["id"]] or {}
        over = slope_over(r)
        if over > SLOPE_TOL and not p.get("slope_fit"):
            warns.append(f"Möbel {k}: ragt {round(over)} cm in die Dachschräge (dort nur {round(r['slope']['lowest'])} cm frei) – "
                         "„An Dachschräge anpassen“ in Schritt ② oder niedriger planen.")
        elif (r.get("ceiling_ratio") or 0) > 1 and not (p.get("slope_fit") and over > 0):
            warns.append(f"Möbel {k}: Höher als die Decke ({round(r['ceiling_height'])} cm)!")
        if (r.get("wall_ratio") or 0) > 1:
            warns.append(f"Möbel {k}: Breiter als die Wand ({round(r['wall_length'])} cm)!")
    desc = {p["id"]: piece_desc(p, rels[p["id"]]) for p in pieces}
    placed = [(k, p) for k, p in enumerate(pieces, 1) if boxes.get(p.get("id"))]
    unplaced = [p for p in pieces if not boxes.get(p.get("id"))]
    removes = _removes(doc)
    changes = (doc.get("room_changes") or "").strip()
    room_edit = f" Also change the room itself as requested: {changes}." if changes else ""
    lead = [(base_v["image"] if base else doc["room"], "room")]
    mock3d = bool(_geo(doc)) and doc.get("mockup", True) is not False and any(_is3d(p) for _, p in placed)
    if (doc.get("guide") or mock3d) and placed:
        try:
            lead.append((_guide_ref(doc, boxes, [p for _, p in placed], lead[0][0]), "guide"))
        except Exception:
            traceback.print_exc()
            warns.append("Umrisse konnten nicht gezeichnet werden.")
    refs, roles, design, samples, w2 = _collect_refs(doc, pieces, lead)
    warns += w2
    rw, rh = doc.get("room_size") or [4, 3]
    width, height = dims_for(rw / rh, _res(doc))
    params = _base(doc, width, height, steps=QUALITY[_quality(doc)]["steps"])
    tag = lambda i: f"<ref_image_{i}>"
    if placed or removes:
        rows = [{"id": f"moebel_{k}", "from": "target", "src_bbox": None, "tgt_bbox": boxes[p["id"]], "desc": desc[p["id"]]}
                for k, p in placed]
        rows += [{"id": f"weg_{i}", "from": "ref_image_0", "src_bbox": _rbox(r), "tgt_bbox": None,
                  "desc": "an existing object that must disappear"} for i, r in enumerate(removes, 1)]
        out = []
        if placed:
            fit = "each precisely fitted" if len(placed) > 1 else "precisely fitted"
            adds = ", ".join(f"{desc[p['id']]} in <moebel_{k}>" for k, p in placed)
            out.append(f"In <ref_image_0>, add {adds}, {fit} to the walls, floor and ceiling around it.")
            if base:
                stay = " and ".join(_uniq(_short(p) for p in kept))
                out.append(f"The {stay} already in <ref_image_0> stays exactly as it is.")
        for i, p in enumerate(unplaced):
            where = (p.get("where") or "").strip()
            spot = f"here: {where}" if where else "in the most suitable place"
            out.append(f"In <ref_image_0>, build {desc[p['id']]} {spot}." if not placed and i == 0
                       else f"Also build {desc[p['id']]} {spot}.")
        num = {p.get("id"): k for k, p in placed}
        # unplaced pieces have no box tag: name them by type (deviation from the spec, which only covers placed pieces)
        name = lambda k, p: f"<moebel_{num[p['id']]}>" if p.get("id") in num else (None if len(pieces) == 1 else f"the {_short(p)}")
        out += _design_sentences(pieces, design, samples, tag, name)
        if "guide" in roles and not mock3d:
            out.append(f"{tag(roles.index('guide'))} shows the same photo with the planned furniture outlines drawn in: "
                       "each piece fills exactly its outline. Do not draw any lines.")
        elif "guide" in roles:
            out.append(f"{tag(roles.index('guide'))} is the same photo with a plain geometric mock-up of the planned furniture "
                       "drawn in: build each piece with exactly that size, position and perspective and with the same division "
                       "into doors, drawers, shelves and plinth, but with real materials, joints and details. Never copy the "
                       "flat colours or the dark lines of the mock-up.")
        if removes:
            out.append(f"Remove {', '.join(f'<weg_{i}>' for i in range(1, len(removes) + 1))} and show the wall and floor behind.")
        out.append(KEEP_OTHERS)
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
    geo = _geo(doc)
    if geo and not doc.get("calibration") and any(_is3d(p) for _, p in placed):
        warns.append("Größen sind nur geschätzt: Der Raum wurde noch nicht nachgemessen (Schritt 1: Deckenhöhe oder eine bekannte Länge).")
    if base:
        hit = lambda a, b: a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
        for _, p in placed:
            over = [labels(k)["label"] for k in kept if boxes.get(k.get("id")) and hit(boxes[p["id"]], boxes[k["id"]])]
            if over:
                warns.append(f"{labels(p)['label']} überdeckt im Foto {', '.join(over)} – dort kann sich das Bild ändern.")
    keep = None
    if doc.get("protect", True) or base:  # building on a variant: everything but the new boxes stays as it is
        if not changes and not unplaced:
            keep = [r for _, p in placed for r in keep_regions_for(doc, p, boxes[p["id"]])] + _rkeep(removes)
        if unplaced:
            warns.append("Raum schützen wirkt erst, wenn alle Möbel platziert sind.")
    params["keep"] = keep
    enabled, retry = _judge_cfg(doc)
    ids = [p.get("id") for p in pieces]
    meta_keep = ((base_v.get("keep") or []) + (keep or [])) if base and base_v.get("keep") is not None and keep else keep
    if enabled:
        sk = _sketch_ref(doc, pieces, design)
        params["judge"] = {"request": judge_request(doc, "room", ids, boxes), "room_ref": 0, "sketch_ref": sk,
                           "plan_ref": roles.index("guide") if mock3d and "guide" in roles else None}
    params["retry"] = 1 if enabled and retry else 0
    all_ids = [p.get("id") for p in all_pieces]
    params["meta"] = {"variant": {**_variant_meta(doc, "room", all_pieces, {i: boxes[i] for i in all_ids if boxes.get(i)}, meta_keep),
                                  "active_piece": _active(doc, pieces).get("id"),
                                  **({"base": base_v["id"], "base_image": base_v["image"]} if base else {}),
                                  **({"plan": refs[roles.index("guide")]} if mock3d and "guide" in roles else {})}}
    plan = _finish(params, refs, roles, warns, _count_label(params["count"]))
    if base:
        plan["base"] = {"variant": base_v["id"], "image": base_v["image"], "kept": [labels(p)["label"] for p in kept],
                        "new": [labels(p)["label"] for p in pieces]}
    return plan


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
    params["meta"] = {"variant": {**_variant_meta(doc, "free", pieces, {}, None), "active_piece": _active(doc, pieces).get("id")}}
    return _finish(params, refs, roles, warns, _count_label(params["count"]))


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
                                  "from": None, "upscaled": False, "retry_of": None, "active_piece": piece_id,
                                  **snapshot(doc, [piece_id], {})}}
    return _finish(params, refs, roles, [], f"Skizze: {lab['label']}")


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
            "upscaled": bool(v.get("upscaled")), "retry_of": None, "active_piece": v.get("active_piece")}
    params = {"upsample": op not in SKIP_PUP_FOR, "mode": "distilled", "width": w, "height": h, "seed": -1, "count": 1,
              "steps": FULL_STEPS, "source": f"workshop:{doc.get('id')}", "task": "edit", "keep": keep}
    edit_text, label = "", "Änderung"
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
                params["keep"] = [_pad(box)] + _rkeep(_removes(doc))
        for row in snap["pieces"]:
            if row.get("id") in targets:
                row["material_label"] = m["de"]
        meta["material"] = " + ".join(_uniq(r.get("material_label") for r in snap["pieces"])) or m["de"]
        label = f"Holz: {m['de']}"
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
        label = "Für Druck vergrößern"
    enabled, _ = _judge_cfg(doc)
    if enabled and op != "upscale":
        params["judge"] = {"request": judge_request(doc, "edit", ids, snap["boxes"], edit_text), "room_ref": 0, "sketch_ref": None}
    params["retry"] = 0
    params["meta"] = {"variant": {**meta, **snap}}
    if op == "material":
        params["meta"]["log"] = f"Holz getauscht: {CATALOG['materials'][material]['de']}"
    elif op == "edit":
        params["meta"]["log"] = f"Änderung berechnet: {edit_text}"
    return _finish(params, [v["image"]], ["source"], [], label)


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

PROTECTED = ("id", "image", "size", "seed", "job", "judge", "by", "caption_backend", "steps")
FINISHED_JUDGE = (None, "off", "done", "unavailable")


def _generated_text(job, meta, n):
    kind = meta.get("kind")
    if kind == "render":
        return "Skizze gerendert"
    if kind == "upscale":
        return "Für Druck vergrößert"
    if kind in ("material", "edit"):
        return ((job.get("params") or {}).get("meta") or {}).get("log") or ("Holz getauscht" if kind == "material" else "Änderung berechnet")
    return f"{_count_label(n)} berechnet"


def record(job: dict, judge_only: bool = False) -> None:
    """Writes the job's results into its project: new images as variants (called after every image), verdicts, and
    once the job is over its activity entry, the status move and the pending_jobs cleanup. Idempotent: the diff-based
    mutate saves nothing when nothing changed. A purged project raises projects.Gone (the scheduler drops the files)."""
    p = job.get("params") or {}
    m = re.fullmatch(r"workshop:([a-z0-9]{6,32})", str(p.get("source") or ""))
    meta = (p.get("meta") or {}).get("variant") if isinstance(p.get("meta"), dict) else None
    if not m or not isinstance(meta, dict):
        return
    who = job.get("owner") or "system"

    def fn(doc):
        variants = doc.setdefault("variants", [])
        by_image = {v.get("image"): v for v in variants}
        deleted = set(doc.get("deleted_variants") or [])
        trashed = {t.get("image") for t in doc.get("trash") or [] if isinstance(t, dict)}
        for r in job.get("results") or []:
            v = by_image.get(f"{r['id']}.png")
            if v is not None:
                if "judge" in r:
                    v["judge"] = r["judge"]
            elif not judge_only and f"{r['id']}.png" not in trashed:  # deleted while the job still ran: stays deleted
                v = {"fav": False, **{k: copy.deepcopy(x) for k, x in meta.items() if k not in PROTECTED},
                     "id": _uid(8, {x.get("id") for x in variants} | deleted), "image": f"{r['id']}.png",
                     "size": [r.get("width"), r.get("height")], "seed": r.get("seed"), "job": job.get("id"),
                     "judge": r.get("judge"), "by": job.get("owner"), "caption_backend": job.get("caption_backend"),
                     "steps": p.get("steps")}
                variants.append(v)
                by_image[v["image"]] = v
        if job.get("state") in ("done", "error", "cancelled") and job.get("judge_state") in FINISHED_JUDGE:
            pending = doc.get("pending_jobs") or []
            if job.get("id") in pending:  # the job ends now: log it once
                made = sum(1 for v in variants if v.get("job") == job.get("id"))
                if job["state"] == "cancelled":
                    projects.log(doc, "cancelled", f"Berechnung abgebrochen ({job.get('cancelled_by') or job.get('owner_name') or 'System'})", who)
                elif job["state"] == "error":
                    projects.log(doc, "failed", f"Berechnung fehlgeschlagen: {job.get('error') or 'unbekannter Fehler'}", who)
                else:
                    projects.log(doc, "generated", _generated_text(job, meta, len(job.get("results") or []) or made), who)
                if job.get("retry_job"):
                    projects.log(doc, "retry", "Kein Vorschlag hat die Prüfung bestanden – neuer Versuch mit Korrektur", who)
                if made:
                    projects.advance(doc, "entwurf", who)
            pending = [x for x in pending if x != job.get("id")]
            if job.get("retry_job") and job["retry_job"] not in pending:
                pending.append(job["retry_job"])
            doc["pending_jobs"] = pending
        variants.sort(key=lambda v: v.get("image") or "", reverse=True)

    try:
        projects.mutate("workshop", m.group(1), fn, who, auto=True)
    except HTTPException as e:
        if e.status_code == 404:
            raise  # purged: the scheduler deletes the job's files
        traceback.print_exc()
    except Exception:
        traceback.print_exc()


# ---------------- endpoints ----------------

def _unavailable(comment="Prüfer nicht verfügbar"):
    return {"status": "unavailable", "model": None, "scores": None, "overall": None, "pass": None, "comment": comment,
            "fix_hint": "", "seconds": 0.0, "graded": _now()}


def _owner(request):
    st = getattr(request, "state", None)
    return getattr(st, "owner", None) or "anon", getattr(st, "owner_name", None) or ""


def _ups():
    import upsampler  # lazy: pulls in diffusers' helpers
    return upsampler


def _caption_plan(plan, images=None, prefetch=False):
    """What the caption will most likely use, so step ④ can warn before starting (vllm/template/cache/reuse).
    prefetch: the GPU is idle and step ④ is open — start the caption now, the job will find it cached or in flight."""
    p = plan["params"]
    try:
        up = _ups()
        st = up.status()
    except Exception:
        return {"backend": "template", "state": "down"}
    if p.get("caption"):
        return {"backend": "reuse", "state": st["state"]}
    if not p.get("upsample", True):
        return {"backend": "template", "state": st["state"]}
    try:
        images = images if images is not None else load_refs(plan["refs"], plan["roles"])
        item = {"request": p.get("prompt") or "", "images": images, "boxes": p.get("boxes") or None,
                "width": p["width"], "height": p["height"]}
        if up.cache_has(item):
            return {"backend": "cache", "state": st["state"]}
        if prefetch and st["state"] == "up" and st["backend"] == "vllm" and BUSY is not None and not BUSY():
            getattr(up, "prefetch", lambda item: None)(item)
    except Exception:
        traceback.print_exc()
    return {"backend": st["backend"], "state": st["state"]}


def _lite(view):
    return {k: v for k, v in (view or {}).items() if k not in ("results", "caption", "source")}


def _submit(pid, plan, req, request, dedupe):
    owner, owner_name = _owner(request)
    plan["wait_s"] = round(WAIT(owner)) if WAIT else 0
    images = None
    if not req.get("dry_run"):
        if SUBMIT is None:
            raise HTTPException(503, "Server noch nicht bereit.")
        images = load_refs(plan["refs"], plan["roles"])
    plan["caption"] = _caption_plan(plan, images, prefetch=bool(req.get("dry_run")) and dedupe == "design")
    if plan["caption"].get("backend") == "cache" and ESTIMATE:  # no caption time for a known caption
        plan["estimate_s"] = round(ESTIMATE({**plan["params"], "upsample": False}, _ref_sizes(plan["refs"], plan["roles"])))
        _timing(plan)
    if req.get("dry_run"):
        return {"job": None, "plan": plan}

    def add(job_id):  # before the job can run: a job that ends at once still finds itself in pending_jobs
        def fn(doc):
            pending = doc.setdefault("pending_jobs", [])
            running = projects.job_running
            if job_id not in pending and (running is None or running(job_id)):
                pending.append(job_id)
        projects.mutate("workshop", pid, fn, owner, auto=True)

    try:
        job = SUBMIT(plan["params"], images, owner=owner, owner_name=owner_name, project=pid, label=plan.get("label", ""),
                     client_key=str(req["client_key"])[:80] if req.get("client_key") else None, dedupe=dedupe,
                     force=req.get("force") is True, on_accept=add)
    except HTTPException:
        raise
    except Exception as e:
        view = getattr(e, "view", None)
        if type(e).__name__ != "Duplicate" or not isinstance(view, dict):  # jobs.Duplicate (not imported: no cycle)
            raise
        pos = view.get("position") or 0
        where = "läuft gerade" if pos == 0 else f"Platz {pos + 1}"
        name = view.get("owner_name") or view.get("owner") or "jemand"
        raise HTTPException(409, {"code": "running", "message": f"Für diesen Auftrag läuft schon eine Berechnung ({name}, {where}).",
                                  "job": _lite(view)})
    return {"job": job, "plan": plan}


def _check_rev(doc, req):
    if req.get("base_rev") is None or req.get("dry_run"):
        return
    try:
        base = int(req["base_rev"])
    except (TypeError, ValueError):
        raise HTTPException(400, "Ungültige Änderung: base_rev.")
    changes = projects.changed_since(doc, base, DESIGN)
    if changes:
        names = " und ".join(_uniq(c.get("by_name") or c.get("by") or "jemand" for c in changes)) or "jemand"
        raise HTTPException(409, {"code": "changed", "rev": doc.get("rev"), "doc": doc,
                                  "message": f"Der Auftrag wurde gerade von {names} geändert. Bitte kurz prüfen und dann erneut starten.",
                                  "changes": [{"path": c.get("path"), "by_name": c.get("by_name")} for c in changes]})


@router.get("/api/workshop/catalog")
def catalog():
    return {**CATALOG, "max_pieces": MAX_PIECES, "max_refs": MAX_REFS,
            "quality": {k: {"label": q["label"], "steps": q["steps"]} for k, q in QUALITY.items()},
            "common_materials": COMMON_MATERIALS}


# plain `def` endpoints: FastAPI runs them in its threadpool (disk I/O, image loading, the projects lock, the judge call)
@router.post("/api/workshop/{pid}/generate")
def generate(pid: str, req: dict, request: Request):
    doc = projects.load("workshop", pid)
    _check_rev(doc, req)
    kind = req.get("kind") or "room"
    ids = [p.get("id") for p in _pieces(doc)]
    doc = {**doc, "active_piece": req.get("active_piece") if req.get("active_piece") in ids else (ids[0] if ids else None)}
    if req.get("count") is not None:
        try:
            doc["count"] = max(1, min(4, int(req["count"])))
        except (TypeError, ValueError):
            raise HTTPException(400, "Ungültige Anzahl.")
    if req.get("quality") in QUALITY:
        doc["quality"] = req["quality"]
    if req.get("base") is False or isinstance(req.get("base"), str):
        doc["base"] = req["base"]
    if kind == "render":
        piece_id = req.get("piece_id") or req.get("active_piece")
        plan, dedupe = build_render_job(doc, piece_id), f"render:{piece_id}"
    elif kind in ("room", "free"):
        plan, dedupe = build_room_job(doc if kind == "room" else {**doc, "room": None}), "design"
    else:
        raise HTTPException(400, "Unbekannte Art.")
    return _submit(pid, plan, req, request, dedupe)


@router.post("/api/workshop/{pid}/edit")
def edit(pid: str, req: dict, request: Request):
    doc = projects.load("workshop", pid)
    vid, op = str(req.get("variant_id") or ""), req.get("op")
    plan = build_edit_job(doc, vid, op, material=req.get("material"), text=str(req.get("text") or ""),
                          keep_only=req.get("keep_only", True) is not False, piece_id=req.get("piece_id"))
    return _submit(pid, plan, req, request, f"{op}:{vid}")


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
def regrade(pid: str, vid: str, request: Request):
    doc = projects.load("workshop", pid)
    v = _variant(doc, vid)
    if judge is None:
        return _unavailable()
    kind = v.get("kind") or ("room" if doc.get("room") else "free")
    ids = v.get("piece_ids") or [p.get("id") for p in _pieces(doc)]
    by_id = {p.get("id"): p for p in _pieces(doc)}
    sketch = plan = None
    if kind == "render":
        req, room, sketch = judge_request(doc, "render", ids), None, _open(v.get("sketch"))
    elif kind in ("room", "free"):
        # the photo the variant was made from (an older variant may predate a new room photo)
        req, room = judge_request(doc, kind, ids, v.get("boxes") or {}), _open(v.get("base_image") or v.get("room") or doc.get("room")) if kind == "room" else None
        cand = [v.get("active_piece")] + ids  # the piece active when the job started (v3 never updates doc.active_piece)
        sketch = next((img for i in cand if i in by_id and (img := _open(_design_ref(by_id[i])[0]))), None)
        plan = _open(v.get("plan"))
    else:
        parent = next((x for x in doc.get("variants") or [] if x.get("id") == v.get("from")), None)
        text = v.get("note") or ("reproduce the image at higher resolution without changes" if kind == "upscale" else "")
        req = judge_request(doc, "edit", ids, v.get("boxes") or {}, text)
        room = _open(parent["image"]) if parent else _open(doc.get("room"))
    result = _open(v.get("image"))
    if result is None:
        raise HTTPException(404, "Bild der Variante fehlt.")
    verdict = EXCLUSIVE(lambda: judge.grade({"room": room, "result": result, "sketch": sketch,
                                             "plan": plan if kind == "room" else None}, req))

    def put(d):
        for x in d.get("variants") or []:
            # a judge outage never overwrites a real verdict
            if x.get("id") == vid and (verdict.get("status") == "ok" or (x.get("judge") or {}).get("status") != "ok"):
                x["judge"] = verdict

    projects.mutate("workshop", pid, put, _owner(request)[0], auto=True)
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
        return EXCLUSIVE(lambda: judge.read_sketch(img))
    except Exception:
        traceback.print_exc()
        return {"status": "error", **nulls, "comment": "Bewertung fehlgeschlagen"}


_PLAN_CHECKS: dict[str, dict] = {}   # guide image → verdict (the same plan is never checked twice)


@router.post("/api/workshop/{pid}/check_plan")
def check_plan(pid: str, req: dict | None = None):
    """KI-Plancheck before generating: the judge VLM looks at the photo and the 3d mock-up of every placed piece and names
    clear placement mistakes (in front of a window or door, at a radiator, floating). → {status, pieces: [{id, label, ok,
    problems, fix}]}; status "none" when nothing is placed in 3d."""
    doc = projects.load("workshop", pid)
    if judge is None:
        return _unavailable()
    geo, pieces = _geo(doc), _pieces(doc)
    boxes, _ = resolve_boxes(doc)
    placed = [p for p in pieces if boxes.get(p.get("id")) and _is3d(p)]
    if not geo or not placed or not doc.get("room"):
        return {"status": "none", "pieces": []}
    rel = _guide_ref(doc, boxes, placed, doc["room"])
    if rel in _PLAN_CHECKS:
        return _PLAN_CHECKS[rel]
    photo, plan = _open(doc["room"]), _open(rel)
    rows = []
    for p in placed:
        w, h, d = piece_dims(p)
        elev = _int(p.get("elev")) or 0
        rows.append({"id": p["id"], "label": labels(p)["label"], "box": boxes[p["id"]],
                     "desc": f"{w}×{h}×{d} cm" + (f", hängt {elev} cm über dem Boden" if elev else "")})
    res = EXCLUSIVE(lambda: judge.check_plan(photo, plan, rows))
    if res.get("status") == "ok":
        names = {r["id"]: r["label"] for r in rows}
        res = {**res, "pieces": [{**x, "label": names.get(x["id"], "Möbel")} for x in res["pieces"] if x["id"] in names]}
        if len(_PLAN_CHECKS) > 200:
            _PLAN_CHECKS.clear()
        _PLAN_CHECKS[rel] = res
    return res


@router.get("/api/workshop/{pid}/model/{piece_id}.{ext}")
def model_file(pid: str, piece_id: str, ext: str, scale: float = 1.0):
    """3D files of one piece from its dimensions: GLB (viewer/AR, metres), STL (mm, 1:scale for a print miniature or CAD),
    CSV (Zuschnittliste)."""
    import export3d
    from fastapi.responses import Response
    doc = projects.load("workshop", pid)
    p = next((x for x in _pieces(doc) if x.get("id") == piece_id), None)
    if p is None:
        raise HTTPException(404, "Möbel nicht gefunden.")
    if not p.get("type"):
        raise HTTPException(400, "Bitte zuerst die Möbelart wählen.")
    dims, name = piece_dims(p), re.sub(r"[^\w-]+", "_", f"{doc.get('title') or 'Auftrag'}_{labels(p)['label']}")
    if ext == "glb":
        return Response(export3d.glb({**p, "label": labels(p)["label"]}, dims), media_type="model/gltf-binary",
                        headers={"Content-Disposition": f'inline; filename="{name}.glb"'})
    if ext == "stl":
        s = max(1.0, min(100.0, float(scale)))
        tag = "" if s == 1 else f"_1-{int(s) if s == int(s) else s}"
        return Response(export3d.stl(p, dims, s), media_type="model/stl",
                        headers={"Content-Disposition": f'attachment; filename="{name}{tag}.stl"'})
    if ext == "csv":
        return Response(("﻿" + export3d.cut_list(p, dims)).encode(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}_Zuschnitt.csv"'})
    raise HTTPException(400, "Unbekanntes Format.")
