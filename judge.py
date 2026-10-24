"""Quality judge for the workshop: a local VLM (Qwen3.8-27B-FP8 behind vLLM's OpenAI API, see serve_judge.sh) grades
every generated variant against the room photo, the carpenter's sketch and the order (pieces, cm dims, materials).
Never raises into the caller: a stopped judge yields status "unavailable" and the studio works without verdicts."""

import base64
import io
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime

import httpx
from fastapi import APIRouter
from PIL import Image, ImageOps

JUDGE_URL = os.environ.get("FLUX_JUDGE_URL", "http://127.0.0.1:8002")
JUDGE_MODEL = os.environ.get("FLUX_JUDGE_MODEL", "Qwen/Qwen3.8-27B-FP8")
WEIGHTS = {"room_preserved": .25, "matches_request": .25, "proportions": .20, "realism": .15, "artifacts": .15}
# calibrated on real verdicts: the model's own pass flag fails nearly every image (often for invented defects), so it only
# vetoes below PASS_OVERRULE; clearly wrong results (missing piece, wrong wood) score a 2-4 and fail via PASS_MIN
PASS_OVERALL, PASS_ROOM, PASS_MIN, PASS_OVERRULE = 7.0, 6, 5, 7.5
MIN_FREE_GB = 15
MAX_SIDE = 1024
STATUS_TTL = 20
_transport = None          # tests inject an httpx.MockTransport
_sleep = time.sleep        # tests skip the memory-guard wait
_cache: dict = {"t": 0.0, "v": None}
_cache_lock = threading.Lock()

router = APIRouter()

SCHEMA = {
    "type": "object",
    "properties": {
        # the comment comes first: written before the numbers, it works as a short chain of thought
        "comment_de": {"type": "string"},
        "fix_hint_en": {"type": "string"},
        "faded_edges": {"type": "boolean"},
        "room_preserved": {"type": ["integer", "null"], "minimum": 0, "maximum": 10},
        **{k: {"type": "integer", "minimum": 0, "maximum": 10}
           for k in ("matches_request", "proportions", "realism", "artifacts")},
        "pass": {"type": "boolean"},
    },
    "required": ["comment_de", "fix_hint_en", "faded_edges", *WEIGHTS, "pass"],
    "additionalProperties": False,
}

SYSTEM = """You are an experienced master carpenter (Schreinermeister) and an experienced photo editor. You inspect AI-generated \
visualisations that a carpenter shows to customers: custom-made furniture inserted into a photo of the customer's real \
room. Grade honestly and consistently: a flawed image embarrasses the carpenter, but rejecting a good image wastes \
an expensive regeneration. Judge only what you can clearly see; do not invent defects.

Rules for judging:
- ROOM SHELL: walls, niches, floor, windows, doors, ceiling, radiators, sockets, lighting and camera perspective must be \
unchanged versus the original photo, unless "room changes" asks for something. Only the carpentry may change \
(plus loose clutter when tidying is requested, and the objects marked for removal). Parts of the room that a piece \
stands in front of are hidden behind it, which is correct (e.g. a radiator inside a requested radiator cover).
- SIZE: check each piece's width x height x depth in cm against the room. References if nothing better is visible: \
ceiling about 250 cm, interior door about 200 cm high and 80-90 cm wide, worktop about 90 cm high, socket about 30 cm \
above the floor. A 250 cm high piece must almost reach a 250-260 cm ceiling; a 75 cm sideboard must stay below hip height. \
Each piece should fill its marked box (grid 0-1000 over the image, [top, left, bottom, right]). The room shell wins \
over the numbers: a built-in piece that exactly fills its box or a niche up to the lintel is correctly sized even if the \
cm figures suggest slightly more; mention such a mismatch in the comment, but do not fail an otherwise good image for it.
- PLAN: if a measured PLAN image (plain wooden blocks drawn into the photo) is given, it is the exact size, position, \
orientation and division (doors, drawers, shelves) computed from the measured room. A piece that matches the plan's \
outline and position is correctly sized and placed, even if it looks large or small to you; judge "proportions" mainly \
by how closely the result follows the plan. Never ask for the flat colours or block look of the plan.
- ORDER: furniture type, material/wood colour, handles, details text; removed objects must be gone; if a sketch is given, \
the result must follow it (number and arrangement of doors, drawers, shelves, compartments; overall layout).
- REALISM: plausible light and shadows, contact with floor and walls, no floating or intersecting furniture, \
correct perspective.
- ARTIFACTS: warped or bent straight lines, melted or duplicated handles, duplicated objects, smeared textures, \
text or letters, drawn outline or box lines, visible seams at region borders.
- HARD FAIL: furniture that is transparent, see-through or fades/dissolves into the wall or floor at any edge (e.g. a \
cabinet top that blurs into the wall instead of ending in a crisp edge). Set "faded_edges" = true, artifacts 0-3 and \
pass = false, however good the rest is. Otherwise "faded_edges" = false.

Scores are integers 0-10 (10 = perfect, 8 = good with minor flaws, 6 = acceptable, 4 = clearly flawed, \
0-2 = unusable). "artifacts": 10 = no artifacts at all. A garbled, noisy, blank or obviously broken result gets \
0-2 in every applicable category and fails. If the requested furniture is missing, matches_request is 0-2.
"room_preserved" must be null when there is no original room photo.
"pass" = true when the carpenter could show the image to the customer as it is. Fail only for a defect a customer \
would notice at once: furniture missing, clearly wrong type, material or colour, room shell changed, size far off \
(more than about 20 %), floating or intersecting furniture, or obvious artifacts. Minor imperfections do not fail.
"comment_de": at most 2 short sentences in German for the carpenter: the main strength and the main problem.
"fix_hint_en": ONE imperative English sentence telling the image generator what to correct (e.g. "Make the wardrobe \
reach the ceiling and keep the window unchanged."), or "" if nothing needs fixing. It must never ask to alter walls, \
niches, windows, doors or the ceiling unless room changes were requested.
Reply with the JSON object only."""

SKETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "comment_de": {"type": "string"},
        **{k: {"type": ["integer", "null"]} for k in ("w", "h", "d", "doors", "drawers")},
    },
    "required": ["comment_de", "w", "h", "d", "doors", "drawers"],
    "additionalProperties": False,
}

SKETCH_SYSTEM = """You read a carpenter's furniture sketch or CAD drawing. Extract the overall outer dimensions of the \
piece in centimetres: w = width, h = height, d = depth (convert mm to cm; for a single value like "2400" without unit \
on a furniture drawing assume mm). Count the doors and drawers. Use null for every value that is not written on or \
countable in the drawing - never guess or estimate from proportions. "comment_de": one short German sentence \
describing the piece (e.g. "Kleiderschrank mit 3 Türen und offenem Fach links."). Reply with the JSON object only."""

TASKS = {
    "room": "Furniture was added to the customer's room photo. Compare the GENERATED result with the ORIGINAL photo.",
    "free": "A free product visualisation was generated (no customer room). Judge the furniture itself; "
            "room_preserved must be null.",
    "render": "A studio product rendering was generated from the carpenter's sketch. Judge how faithfully and "
              "realistically it builds the sketched piece; room_preserved must be null. The free-standing piece on a neutral studio "
              "background is intended (it is not installed in a room); never ask to place it in a room or niche.",
    "edit": "An existing visualisation was edited. Everything except the requested edit must stay as in the image "
            "BEFORE the edit; room_preserved = how well the rest of the image was kept.",
}


def rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    return img.convert("RGB")


def data_url(img: Image.Image) -> str:
    img = rgb(img)
    if max(img.size) > MAX_SIDE:
        s = MAX_SIDE / max(img.size)
        img = img.resize((max(16, round(img.width * s)), max(16, round(img.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def client(timeout: float) -> httpx.Client:
    return httpx.Client(base_url=JUDGE_URL, transport=_transport, timeout=httpx.Timeout(timeout, connect=2))


def unit_state() -> str:
    try:
        s = subprocess.run(["systemctl", "--user", "is-active", "flux-judge"], capture_output=True, text=True,
                           timeout=2).stdout.strip()
    except Exception:
        return "unknown"
    return {"active": "active", "reloading": "active", "activating": "activating", "inactive": "inactive",
            "failed": "inactive", "deactivating": "inactive"}.get(s, "unknown")


def status(force: bool = False) -> dict:
    with _cache_lock:
        if not force and _cache["v"] and time.time() - _cache["t"] < STATUS_TTL:
            return dict(_cache["v"])
    ok = False
    try:
        with client(2) as c:
            ok = c.get("/health").status_code == 200 and bool(c.get("/v1/models").json().get("data"))
    except Exception:
        pass
    v = {"available": ok, "state": unit_state(), "model": JUDGE_MODEL, "url": JUDGE_URL, "checked": now()}
    with _cache_lock:
        _cache.update(t=time.time(), v=v)
    return dict(v)


def mark_down() -> None:
    with _cache_lock:
        _cache.update(t=0.0, v=None)


def mem_available_gb() -> float:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except OSError:
        pass
    return float("inf")


def wait_memory(limit: float = 120, step: float = 5) -> bool:
    waited = 0.0
    while mem_available_gb() < MIN_FREE_GB:
        if waited >= limit:
            return False
        _sleep(step)
        waited += step
    return True


# ---------------------------------------------------------------- parsing & scoring

def first_object(text: str) -> str | None:
    start = text.find("{")
    while start >= 0:
        depth, quote, esc = 0, None, False
        for i in range(start, len(text)):
            ch = text[i]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def loads_lenient(s: str):
    for attempt in range(3):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
        s = re.sub(r",\s*([}\]])", r"\1", s)
        if attempt == 0:
            s = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", re.sub(r"\bNone\b", "null", s)))
        else:
            s = re.sub(r"(?<![A-Za-z])'|'(?![A-Za-z])", '"', s)   # quotes, not apostrophes inside words
    return None


def raw_fields(text: str) -> dict:
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    obj = first_object(text)
    data = loads_lenient(obj) if obj else None
    if isinstance(data, dict):
        return data
    data = {}   # hopeless JSON (e.g. cut off): pick the fields one by one
    for m in re.finditer(r"[\"']?(\w+)[\"']?\s*:\s*(-?\d+(?:\.\d+)?|null|None|true|false|True|False)", text):
        v = m.group(2).lower()
        data.setdefault(m.group(1), None if v in ("null", "none") else v == "true" if v in ("true", "false") else float(v))
    for m in re.finditer(r"[\"'](\w+)[\"']\s*:\s*\"((?:[^\"\\]|\\.)*)\"", text):
        data.setdefault(m.group(1), m.group(2))
    return data


def clamp_int(v, lo: int = 0, hi: int = 10) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return max(lo, min(hi, round(float(v))))
    except (TypeError, ValueError):
        return None


def as_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no"):
        return v.strip().lower() in ("true", "yes")
    return None


def parse(text: str, need_room: bool = False) -> dict | None:
    """None (→ re-ask, then status "error") when a score is missing; room_preserved only counts with a room photo:
    a null there would otherwise fail a good image in score() and trigger a pointless auto-retry."""
    d = raw_fields(text)
    out = {k: clamp_int(d.get(k)) for k in WEIGHTS}
    if any(out[k] is None for k in WEIGHTS if k != "room_preserved" or need_room):
        return None
    out["pass"] = as_bool(d.get("pass"))
    out["faded_edges"] = as_bool(d.get("faded_edges")) is True
    out["comment_de"] = str(d.get("comment_de") or d.get("comment") or "").strip()[:400]
    out["fix_hint_en"] = str(d.get("fix_hint_en") or d.get("fix_hint") or "").strip()[:300]
    return out


def score(scores: dict, model_pass: bool | None, has_room: bool, faded: bool = False) -> tuple[float, bool]:
    used = {k: v for k, v in scores.items() if k in WEIGHTS and v is not None
            and (k != "room_preserved" or has_room)}
    total = sum(WEIGHTS[k] for k in used)
    overall = round(sum(WEIGHTS[k] * v for k, v in used.items()) / total, 1) if total else 0.0
    ok = (overall >= PASS_OVERALL and all(v >= PASS_MIN for v in used.values())
          and (not has_room or (used.get("room_preserved") or 0) >= PASS_ROOM)
          and (model_pass is not False or overall >= PASS_OVERRULE) and not faded)
    return overall, ok


def needs_retry(results: list[dict]) -> bool:
    return bool(results) and all((r or {}).get("status") == "ok" and (r or {}).get("pass") is False for r in results)


def retry_hint(results: list[dict]) -> str:
    ok = sorted((r for r in results if (r or {}).get("status") == "ok"), key=lambda r: -(r.get("overall") or 0))
    best = [r for r in ok if r.get("overall") == ok[0].get("overall")] if ok else []
    hints: list[str] = []
    for group in (best, ok):
        for r in group:
            h = (r.get("fix_hint") or "").strip()
            if h and h not in hints:
                hints.append(h)
        if hints:
            break
    text = " ".join(hints)
    return text if len(text) <= 300 else text[:300].rsplit(" ", 1)[0]


def retry_suffix(hint: str) -> str:
    return f"Important corrections: {hint}" if hint and hint.strip() else ""


# ---------------------------------------------------------------- requests

def request_text(req: dict) -> str:
    kind = req.get("kind", "room")
    lines = [f"Task: {TASKS.get(kind, TASKS['room'])}", "", "Ordered furniture:"]
    for i, p in enumerate(req.get("pieces") or [], 1):
        dims = " x ".join(f"{p[k]} cm {n}" for k, n in (("w", "wide"), ("h", "high"), ("d", "deep")) if p.get(k))
        lines.append(f"{i}. {p.get('label') or 'Möbel'}: {p.get('desc') or ''}".rstrip())
        for name, val in (("Size", dims), ("Material", p.get("material")), ("Handles", p.get("handles")),
                          ("Marked box [top,left,bottom,right] 0-1000", p.get("box")),
                          ("In the room", p.get("relation"))):
            if val:
                lines.append(f"   {name}: {val}")
    if not req.get("pieces"):
        lines.append("(none)")
    if req.get("removes"):
        lines.append(f"Objects to remove (boxes, must be gone, wall/floor shown behind): {req['removes']}")
    if kind in ("room", "edit"):
        lines.append("Tidy: yes - loose clutter may be cleared (not a room change)." if req.get("tidy")
                     else "Tidy: no - all loose objects must stay.")
        lines.append(f"Room changes requested: {req['room_changes']}" if req.get("room_changes")
                     else "Room changes requested: none - the room shell must stay exactly as photographed.")
        if req.get("protect") and kind == "room":
            lines.append("Protected room: outside the marked boxes the original pixels were restored, so look closely "
                         "at the box borders for seams and at what changed inside the boxes.")
    if req.get("edit_text"):
        lines.append(f"Requested edit: {req['edit_text']}")
    return "\n".join(lines)


def image_parts(images: dict, kind: str) -> list[tuple[str, Image.Image]]:
    room, result, sketch, plan = images.get("room"), images.get("result"), images.get("sketch"), images.get("plan")
    seq = {"render": [("the carpenter's SKETCH of the furniture", sketch), ("the GENERATED result", result)],
           "edit": [("the image BEFORE the edit", room), ("the GENERATED result (after the edit)", result),
                    ("the carpenter's SKETCH of the furniture", sketch)],
           "free": [("the GENERATED result", result), ("the carpenter's SKETCH of the furniture", sketch)]
           }.get(kind, [("the ORIGINAL room photo", room), ("the GENERATED result", result),
                        ("the measured PLAN: the room photo with the ordered furniture as a geometric block mock-up", plan),
                        ("the carpenter's SKETCH of the furniture", sketch)])
    return [(label, img) for label, img in seq if img is not None][:4]


def content_of(parts: list[tuple[str, Image.Image]], text: str) -> list[dict]:
    content = []
    for i, (label, img) in enumerate(parts, 1):
        content.append({"type": "text", "text": f"Image {i}: {label}."})
        content.append({"type": "image_url", "image_url": {"url": data_url(img)}})
    return content + [{"type": "text", "text": text}]


def ask(system: str, content: list, schema: dict, name: str, timeout: float, max_tokens: int = 700,
        temperature: float = 0) -> str:
    """One chat completion; returns the raw text. Transport errors propagate (→ unavailable)."""
    body = {"model": JUDGE_MODEL, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}}
    with client(timeout) as c:
        r = c.post("/v1/chat/completions", json=body)
        if r.status_code == 400:     # structured output rejected: plain request, parse() repairs the text
            body.pop("response_format")
            r = c.post("/v1/chat/completions", json=body)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning_content") or ""


def verdict(status_: str, t0: float, comment: str, **kw) -> dict:
    return {"status": status_, "model": JUDGE_MODEL, "scores": None, "overall": None, "pass": None,
            "comment": comment, "fix_hint": "", **kw, "seconds": round(time.time() - t0, 1), "graded": now()}


def call(system: str, content: list, schema: dict, name: str, timeout: float, parser, t0: float, **kw):
    """Shared availability check, memory guard and one re-ask; returns (parsed | None, failure verdict | None)."""
    if not status()["available"]:
        return None, verdict("unavailable", t0, "Prüfer nicht verfügbar")
    if not wait_memory():
        return None, verdict("unavailable", t0, "Speicher knapp – Prüfung übersprungen")
    try:
        for attempt in range(2):
            parsed = parser(ask(system, content, schema, name, timeout, temperature=0.3 * attempt, **kw))
            if parsed is not None:
                return parsed, None
    except (httpx.TransportError, httpx.HTTPStatusError) as e:   # timeouts, refused, 5xx from a dying server
        mark_down()
        print(f"judge: unavailable ({type(e).__name__}: {e})", flush=True)
        return None, verdict("unavailable", t0, "Prüfer nicht verfügbar")
    return None, verdict("error", t0, "Bewertung fehlgeschlagen")


def grade(images: dict, request: dict, timeout: float = 180) -> dict:
    t0 = time.time()
    try:
        kind = request.get("kind", "room")
        parts = image_parts(images, kind)
        if images.get("result") is None:
            return verdict("error", t0, "Bewertung fehlgeschlagen")
        has_room = kind in ("room", "edit") and images.get("room") is not None
        text = request_text(request)
        if not has_room:
            text += "\nThere is no original room photo: set room_preserved to null."
        d, fail = call(SYSTEM, content_of(parts, text), SCHEMA, "verdict", timeout, lambda t: parse(t, has_room), t0)
        if fail:
            return fail
        scores = {k: d[k] for k in WEIGHTS}
        if not has_room:
            scores["room_preserved"] = None
        comment, hint = d["comment_de"], d["fix_hint_en"]
        if d["faded_edges"]:   # hard fail: a see-through piece must never reach the customer
            scores["artifacts"] = min(scores["artifacts"], 3)
            comment = f"Möbelkante verblasst oder durchscheinend. {comment}".strip()[:400]
            hint = hint or "Draw every piece solid with crisp edges, ending exactly inside its marked box."
        overall, ok = score(scores, d["pass"], has_room, d["faded_edges"])
        return verdict("ok", t0, comment or ("Bestanden." if ok else "Nicht bestanden."),
                       scores=scores, overall=overall, fix_hint=hint, faded_edges=d["faded_edges"], **{"pass": ok})
    except Exception as e:
        print(f"judge: grading failed ({type(e).__name__}: {e})", flush=True)
        return verdict("error", t0, "Bewertung fehlgeschlagen")


def parse_sketch(text: str) -> dict | None:
    d = raw_fields(text)
    if not any(k in d for k in ("w", "h", "d", "doors", "drawers")):
        return None
    out = {k: clamp_int(d.get(k), 1, 2000) for k in ("w", "h", "d")}
    out.update({k: clamp_int(d.get(k), 0, 50) for k in ("doors", "drawers")})
    out["comment"] = str(d.get("comment_de") or d.get("comment") or "").strip()[:300]
    return out


def read_sketch(img: Image.Image, timeout: float = 120) -> dict:
    t0 = time.time()
    nulls = {"w": None, "h": None, "d": None, "doors": None, "drawers": None}
    try:
        content = content_of([("the furniture sketch", img)], "Read the dimensions and count doors and drawers.")
        d, fail = call(SKETCH_SYSTEM, content, SKETCH_SCHEMA, "sketch", timeout, parse_sketch, t0, max_tokens=300)
        if fail:
            return {"status": fail["status"], **nulls, "comment": fail["comment"]}
        return {"status": "ok", **d}
    except Exception as e:
        print(f"judge: sketch reading failed ({type(e).__name__}: {e})", flush=True)
        return {"status": "error", **nulls, "comment": "Bewertung fehlgeschlagen"}


@router.get("/api/judge/status")
def api_status():
    return status()


PLAN_SYSTEM = """You check a carpenter's furniture PLAN before any image is generated. You get the ORIGINAL room photo and the
same photo with the planned pieces drawn in as plain shaded wooden blocks (a geometric mock-up; grey blurred areas are
things that will be removed). Judge only the PLACEMENT, not the look of the blocks. The blocks hide what is behind them:
for every piece, look at the SAME area in image 1 to see what it would cover or stand in. Report ONLY clear mistakes:
- it hangs or stands in front of a WINDOW or glass surface, or blocks a DOOR (the door leaf or the door opening itself),
- it stands in front of or inside a RADIATOR / heater,
- a standing piece floats in the air, a wall piece hangs in front of nothing solid, or it pokes through a wall or ceiling.
These are NORMAL and never a problem: chairs at a table, a bench next to a table, a new piece standing exactly where an old
piece of the same kind stands (it replaces it), leftover space or walkway width, the exact size. If you are not sure, it is
ok. Write problems and the fix in short plain German (Sie-Form), e.g. „Hängt vor dem Fenster.“ / „An die freie Wand links
daneben setzen.“ Return JSON only."""

PLAN_SCHEMA = {"type": "object", "properties": {"pieces": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "ok": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}},
    "fix": {"type": "string"}}, "required": ["id", "ok", "problems", "fix"]}}}, "required": ["pieces"]}


def _crop(img: Image.Image, box, grow: float = 0.6) -> Image.Image:
    W, H = img.size
    t, l, b, r = box
    ph, pw = (b - t) * grow, (r - l) * grow
    t, b = max(0, t - ph), min(1000, b + ph * 0.6)
    l, r = max(0, l - pw), min(1000, r + pw)
    return img.crop((int(l * W / 1000), int(t * H / 1000), int(r * W / 1000), int(b * H / 1000)))


def check_piece(photo: Image.Image, plan: Image.Image, piece: dict, timeout: float = 90) -> dict:
    """One piece: the original photo and the mock-up, both cropped around it (the zoom shows radiators, sockets, frames)."""
    t0 = time.time()
    b = piece.get("box") or [0, 0, 1000, 1000]
    text = (f'Planned piece id "{piece["id"]}": {piece["label"]} ({piece.get("desc", "")}). Both images show the same detail of the '
            "room around it. Return one entry for this id.")

    def parser(t):
        d = loads_lenient(first_object(t) or "")
        if not isinstance(d, dict) or not isinstance(d.get("pieces"), list) or not d["pieces"]:
            return None
        e = d["pieces"][0] if isinstance(d["pieces"][0], dict) else {}
        probs = [str(x)[:200] for x in (e.get("problems") or []) if str(x).strip()][:4]
        return {"id": piece["id"], "ok": bool(e.get("ok", True)) and not probs, "problems": probs, "fix": str(e.get("fix") or "")[:200]}
    parts = [("the ORIGINAL room photo (detail)", _crop(photo, b)), ("the same detail with the PLANNED piece as a block", _crop(plan, b))]
    res, fail = call(PLAN_SYSTEM, content_of(parts, text), PLAN_SCHEMA, "plan", timeout, parser, t0, max_tokens=300)
    return fail or res


def check_plan(photo: Image.Image, plan: Image.Image, pieces: list[dict], timeout: float = 90) -> dict:
    """Placement check of the mock-up before generating, one zoomed request per piece (vLLM runs them together).
    `pieces`: {id, label, desc, box (0-1000 grid)}. → {"status": "ok", "pieces": [{id, ok, problems, fix}], "seconds"}."""
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    photo = rgb(photo)                       # loaded once: PIL images are not safe to load from several threads
    plan = rgb(plan).resize(photo.size)
    with ThreadPoolExecutor(max_workers=4) as ex:
        res = list(ex.map(lambda p: check_piece(photo, plan, p, timeout), pieces))
    bad = next((r for r in res if r.get("status") in ("unavailable", "error")), None)
    if bad:
        return bad
    return {"status": "ok", "pieces": res, "seconds": round(time.time() - t0, 1), "model": JUDGE_MODEL}
