"""Saved projects for the Book and Workshop pages: JSON documents on disk, image uploads, story drafting through a
local Ollama model, and the print exports (see printing.py)."""

import asyncio
import hashlib
import io
import json
import math
import os
import random
import re
import string
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps

import printing

try:  # HEIC/HEIF uploads from iPhones and desktops
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

HERE = Path(__file__).parent
OUT = HERE / "outputs"
UPLOADS = OUT / "uploads"
EXPORTS = OUT / "exports"
STORE = HERE / "projects"
KINDS = ("book", "workshop")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
STORY_MODEL = os.environ.get("STORY_MODEL", "gemma4:31b-it-q4_K_M")

for d in (UPLOADS, EXPORTS, *(STORE / k for k in KINDS)):
    d.mkdir(parents=True, exist_ok=True)

router = APIRouter()


def path_of(kind: str, pid: str) -> Path:
    if kind not in KINDS or not re.fullmatch(r"[a-z0-9]{6,32}", pid):
        raise HTTPException(404)
    return STORE / kind / f"{pid}.json"


def load(kind: str, pid: str) -> dict:
    p = path_of(kind, pid)
    if not p.exists():
        raise HTTPException(404, "Project not found.")
    doc = json.loads(p.read_text())
    return migrate_workshop(doc) if kind == "workshop" else doc


def save(kind: str, doc: dict) -> dict:
    doc["updated"] = datetime.now().isoformat(timespec="seconds")
    p = path_of(kind, doc["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    tmp.replace(p)
    return doc


# ---------------- workshop documents (version 2) ----------------
# v2: several pieces per project, each with its own dims/material/sketch/placement; variants carry a snapshot of the
# pieces at generation time and are written by the server (workshop.record), so PUTs merge instead of overwrite.

WORKSHOP_VERSION = 2
LOCK = threading.Lock()  # serializes every workshop read-modify-write (PUT, record, judge, generate)
PIECE_LABELS = {"schrank": "Einbauschrank", "kueche": "Küche", "regal": "Regalwand", "sideboard": "Sideboard",
                "tisch": "Esstisch", "bett": "Bett", "treppe": "Treppe", "garderobe": "Garderobe", "bad": "Badmöbel",
                "sitzbank": "Sitzbank", "wand": "Wandverkleidung", "tuer": "Tür"}
MATERIAL_LABELS = {"eiche": "Eiche natur geölt", "eicheraeuchert": "Eiche geräuchert", "eicheweiss": "Eiche weiß geölt",
                   "nuss": "Nussbaum", "esche": "Esche", "kirsch": "Kirschbaum", "fichte": "Fichte massiv",
                   "multiplex": "Multiplex Birke", "altholz": "Altholz", "weiss": "Lack weiß matt",
                   "anthrazit": "Lack anthrazit"}
HANDLE_LABELS = {"push": "grifflos (Push)", "leiste": "Griffleiste", "messing": "Messing", "schwarz": "Schwarz matt",
                 "leder": "Lederlaschen", "knopf": "Holzknöpfe"}
DEFAULT_PIECE = {"type": "schrank", "text": "", "label": "Einbauschrank", "material": "eiche", "material_custom": "",
                 "material_label": "Eiche natur geölt", "handles": "push", "handles_label": "grifflos (Push)",
                 "w": None, "h": None, "d": None, "sketch": None, "render": None, "use_render": False, "sample": None,
                 "where": "", "placement": {"mode": "none", "pose": None, "box": None}}
LEGACY_KEYS = ("piece_type", "piece_text", "w", "h", "d", "material", "material_custom", "handles", "sketch", "box", "where", "dims")
MAX_PIECES = 4
ALNUM = string.ascii_lowercase + string.digits


def _rid(n: int, seed: str | None = None) -> str:
    """Random id, or a stable one derived from seed (so loading an unsaved v1 doc twice gives the same ids)."""
    if seed is not None:
        return hashlib.sha1(seed.encode()).hexdigest()[:n]
    return "".join(random.choices(ALNUM, k=n))


def _str(v, n: int) -> str:
    return str(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dim(v) -> int | None:
    f = _num(str(v).strip().replace(",", ".")) if isinstance(v, str) else (None if isinstance(v, bool) else _num(v))
    return int(round(f)) if f is not None and 1 <= round(f) <= 2000 else None


def _img(v) -> str | None:
    return v if isinstance(v, str) and ".." not in v and re.fullmatch(r"[\w./-]+\.png", v) else None


def _box(b, strict: bool = True) -> list | None:
    """Grid box [top, left, bottom, right] clamped to 0..1000; None if malformed (or degenerate when strict)."""
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    nums = [_num(x) for x in b]
    if None in nums:
        return None
    t, l, bo, r = (min(1000, max(0, int(round(x)))) for x in nums)
    return None if strict and (bo <= t or r <= l) else [t, l, bo, r]


def _pose(p) -> dict | None:
    if not isinstance(p, dict):
        return None
    x, z, yaw = _num(p.get("x")), _num(p.get("z")), _num(p.get("yaw", 0))
    if x is None or z is None or yaw is None:
        return None
    wall = p.get("wall")
    return {"x": x, "z": z, "yaw": yaw, "wall": wall[:16] if isinstance(wall, str) and wall else None}


def _bool(v, default: bool) -> bool:
    return v if isinstance(v, bool) else default


def _labels(piece: dict, label="", material_label="", handles_label="") -> dict:
    mat = piece.get("material")
    return {"label": label or PIECE_LABELS.get(piece.get("type"), ""),
            "material_label": material_label or (piece.get("material_custom") if mat == "custom" else MATERIAL_LABELS.get(mat, "")),
            "handles_label": handles_label or HANDLE_LABELS.get(piece.get("handles"), "")}


def _piece(p, pid: str) -> dict:
    p = p if isinstance(p, dict) else {}
    out = {"id": pid, "type": p["type"] if p.get("type") in PIECE_LABELS else None, "text": _str(p.get("text"), 2000)}
    out["material"] = p["material"] if p.get("material") in MATERIAL_LABELS or p.get("material") == "custom" else "eiche"
    out["material_custom"] = _str(p.get("material_custom"), 200)
    out["handles"] = p["handles"] if p.get("handles") in HANDLE_LABELS else None
    out.update(_labels(out, *(_str(p.get(k), 80) for k in ("label", "material_label", "handles_label"))))
    for k in ("w", "h", "d"):
        out[k] = _dim(p.get(k))
    out["sketch"], out["render"], out["sample"] = _img(p.get("sketch")), _img(p.get("render")), _img(p.get("sample"))
    out["use_render"] = _bool(p.get("use_render"), False) and bool(out["render"])
    out["where"] = _str(p.get("where"), 500)
    pl = p.get("placement") if isinstance(p.get("placement"), dict) else {}
    mode, pose, box = pl.get("mode"), _pose(pl.get("pose")), _box(pl.get("box"))
    if mode == "3d" and pose:
        out["placement"] = {"mode": "3d", "pose": pose, "box": box}       # box None = not visible (unplaced)
    elif mode == "2d" and box:
        out["placement"] = {"mode": "2d", "pose": None, "box": box}
    else:
        out["placement"] = {"mode": "none", "pose": None, "box": None}
    return out


def _snapshot_piece(p) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("id"), str):
        return None
    return {"id": p["id"][:16], **{k: _str(p.get(k), 80) for k in ("label", "material_label", "handles_label")},
            **{k: _dim(p.get(k)) for k in ("w", "h", "d")}}


def _variant(v, vid: str) -> dict:
    v = {**v, "id": vid, "fav": _bool(v.get("fav"), False), "note": _str(v.get("note"), 2000)}
    v.setdefault("judge", None)
    v["piece_ids"] = [x for x in v.get("piece_ids") or [] if isinstance(x, str)][:MAX_PIECES] if isinstance(v.get("piece_ids"), list) else []
    if "pieces" in v:
        pieces = [_snapshot_piece(p) for p in v["pieces"]] if isinstance(v["pieces"], list) else [None]
        if None in pieces:
            del v["pieces"]
        else:
            v["pieces"] = pieces[:MAX_PIECES]
    if "boxes" in v:
        ok = isinstance(v["boxes"], dict) and all(isinstance(k, str) for k in v["boxes"])
        if ok:
            v["boxes"] = {k: _box(b) for k, b in list(v["boxes"].items())[:MAX_PIECES]}
        else:
            del v["boxes"]
    return v


def normalize_workshop(doc: dict) -> dict:
    """Clamps and validates a v2 workshop doc in place, fills defaults and always strips the v1 keys."""
    for k in LEGACY_KEYS:
        doc.pop(k, None)
    doc["version"] = WORKSHOP_VERSION
    for k, n in (("title", 200), ("customer", 200), ("setting", 1000), ("room_changes", 1000)):
        doc[k] = _str(doc.get(k), n)
    doc["room"], doc["sample"], doc["thumb"] = _img(doc.get("room")), _img(doc.get("sample")), _img(doc.get("thumb"))
    rs = doc.get("room_size")
    doc["room_size"] = [int(rs[0]), int(rs[1])] if (isinstance(rs, list) and len(rs) == 2 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x < 100_000 for x in rs)) else None
    doc["tidy"], doc["protect"], doc["guide"] = (_bool(doc.get("tidy"), True), _bool(doc.get("protect"), True),
                                                 _bool(doc.get("guide"), False))
    count = _num(doc.get("count"))
    doc["count"] = min(4, max(1, int(count))) if count is not None else 2
    doc["res"] = doc["res"] if doc.get("res") in (1024, 1536) else 1536
    doc["removes"] = [b for b in map(_box, doc.get("removes") or [] if isinstance(doc.get("removes"), list) else []) if b][:20]
    j = doc.get("judge") if isinstance(doc.get("judge"), dict) else {}
    doc["judge"] = {"enabled": _bool(j.get("enabled"), True), "auto_retry": _bool(j.get("auto_retry"), True)}
    g = doc.get("geometry")
    doc["geometry"] = g if isinstance(g, dict) and len(json.dumps(g)) <= 65536 else None
    c = doc.get("calibration")
    doc["calibration"] = c if isinstance(c, dict) and len(json.dumps(c)) <= 4096 else None
    if not isinstance(doc.get("company"), dict):
        doc["company"] = {}

    pieces, used = [], set()
    for i, p in enumerate((doc.get("pieces") if isinstance(doc.get("pieces"), list) else [])[:MAX_PIECES]):
        pid = p.get("id") if isinstance(p, dict) else None
        if not (isinstance(pid, str) and re.fullmatch(r"[a-z0-9]{4,12}", pid)) or pid in used:
            pid = _rid(6, f"{doc.get('id')}-{i}-{pid}")
            while pid in used:
                pid = _rid(6)
        used.add(pid)
        pieces.append(_piece(p, pid))
    if not pieces:
        pieces = [_piece(DEFAULT_PIECE, _rid(6, f"{doc.get('id')}-default"))]
    doc["pieces"] = pieces
    if doc.get("active_piece") not in {p["id"] for p in pieces}:
        doc["active_piece"] = pieces[0]["id"]

    dv = [x for x in doc.get("deleted_variants") or [] if isinstance(x, str) and re.fullmatch(r"[a-z0-9]{4,16}", x)]
    doc["deleted_variants"] = list(dict.fromkeys(dv))[-500:]
    deleted = set(doc["deleted_variants"])
    pj = [x for x in doc.get("pending_jobs") or [] if isinstance(x, str) and re.fullmatch(r"[a-f0-9]{6,32}", x)]
    doc["pending_jobs"] = list(dict.fromkeys(pj))[-20:]

    variants, ids, images = [], set(), set()
    for v in doc.get("variants") if isinstance(doc.get("variants"), list) else []:
        if not isinstance(v, dict) or not _img(v.get("image")) or v["image"] in images:
            continue
        vid = v.get("id")
        if not (isinstance(vid, str) and re.fullmatch(r"[a-z0-9]{4,16}", vid)) or vid in ids:
            vid = _rid(8, v["image"])
            while vid in ids:
                vid = _rid(8)
        if vid in deleted:
            continue
        ids.add(vid)
        images.add(v["image"])
        v = _variant(v, vid)
        j = v.get("judge")
        if job_running and isinstance(j, dict) and j.get("status") == "pending" and not job_running(str(v.get("job") or "")):
            # graded by a job this server no longer runs (restart mid-grading): offer "Neu bewerten" instead of a stuck badge
            v["judge"] = {**j, "status": "error", "comment": "Bewertung unterbrochen – bitte neu bewerten"}
        variants.append(v)
    doc["variants"] = sorted(variants, key=lambda v: v["image"], reverse=True)
    if doc.get("selected") not in ids:
        doc["selected"] = variants[0]["id"] if variants else None
    return doc


def migrate_workshop(doc: dict) -> dict:
    """v1 (one piece as top-level keys) → v2; runs only when the doc says it is older than v2, then normalizes."""
    if doc.get("version", 1) >= WORKSHOP_VERSION:
        return normalize_workshop(doc)
    old = sorted((v for v in doc.get("variants") or [] if isinstance(v, dict)), key=lambda v: str(v.get("image")), reverse=True)
    newest = old[0] if old else {}
    box = _box(doc.get("box"))
    w, h, d = (_dim(doc.get(k)) for k in ("w", "h", "d"))
    piece = {"id": _rid(6, f"{doc.get('id')}-piece"), "type": doc.get("piece_type"), "text": doc.get("piece_text") or "",
             "w": w, "h": h, "d": d, "material": doc.get("material"), "material_custom": doc.get("material_custom") or "",
             "handles": doc.get("handles"), "sketch": doc.get("sketch"), "render": None, "use_render": False,
             "sample": None, "where": doc.get("where") or "",
             "label": newest.get("piece") or PIECE_LABELS.get(doc.get("piece_type"), ""),
             "material_label": newest.get("material") or "", "handles_label": newest.get("handles") or "",
             "placement": {"mode": "2d" if box else "none", "pose": None, "box": box}}
    by_id = {v.get("id"): v for v in old}
    for v in old:
        if v.get("upscaled"):
            kind = "upscale"
        elif v.get("from"):
            parent = by_id.get(v["from"])
            kind = "material" if parent and parent.get("material") != v.get("material") else "edit"
        else:
            kind = "room" if doc.get("room") else "free"
        v.setdefault("kind", kind)
        v.setdefault("piece_ids", [piece["id"]])
        v.setdefault("judge", None)
        if "pieces" not in v:
            v["pieces"] = [{"id": piece["id"], "label": v.get("piece") or "", "material_label": v.get("material") or "",
                            "handles_label": v.get("handles") or "", "w": w, "h": h, "d": d}]
            v["boxes"] = {piece["id"]: box} if kind == "room" and box else {}
    doc["variants"] = old
    doc.update(version=WORKSHOP_VERSION, pieces=[piece], active_piece=piece["id"], guide=False,
               judge={"enabled": True, "auto_retry": True}, geometry=None, calibration=None,
               deleted_variants=[], pending_jobs=[])
    return normalize_workshop(doc)


job_running: Callable[[str], bool] | None = None  # set by the server: is this job id still queued/running/being judged?


def merge_workshop(stored: dict, client: dict) -> dict:
    """PUT merge: variants belong to the server (fav/note come from the client), everything else: client wins."""
    deleted = list(dict.fromkeys(stored.get("deleted_variants", []) + client.get("deleted_variants", [])))
    gone = set(deleted)
    theirs = {v["id"]: v for v in client.get("variants", [])}
    variants = []
    for v in stored.get("variants", []):
        if v["id"] in gone:
            continue
        if v["id"] in theirs:
            v = {**v, "fav": theirs[v["id"]].get("fav", v.get("fav")), "note": theirs[v["id"]].get("note", v.get("note"))}
        variants.append(v)
    known, images = {v["id"] for v in stored.get("variants", [])}, {v["image"] for v in variants}
    for v in client.get("variants", []):  # an old UI that built the variant itself
        if v["id"] not in known and v["id"] not in gone and v["image"] not in images:
            variants.append({**v, "judge": None})
            images.add(v["image"])
    pending = client.get("pending_jobs", [])
    if job_running:  # the server knows its jobs: keep ids it added meanwhile, drop finished/unknown ones the client still lists
        pending = [j for j in dict.fromkeys(stored.get("pending_jobs", []) + pending) if job_running(j)]
    return normalize_workshop({**client, "variants": variants, "deleted_variants": deleted, "pending_jobs": pending})


def mutate(kind: str, pid: str, fn: Callable[[dict], None]) -> dict:
    """Read-modify-write of one project under LOCK (load gives the migrated doc)."""
    with LOCK:
        doc = load(kind, pid)
        fn(doc)
        doc["id"] = pid
        return save(kind, normalize_workshop(doc) if kind == "workshop" else doc)


# ---------------- projects ----------------

@router.get("/api/projects/{kind}")
def list_projects(kind: str):
    if kind not in KINDS:
        raise HTTPException(404)
    items = []
    for f in (STORE / kind).glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        items.append({"id": d["id"], "title": d.get("title") or "Untitled", "updated": d.get("updated"), "thumb": d.get("thumb")})
    return sorted(items, key=lambda d: d["updated"] or "", reverse=True)


@router.post("/api/projects/{kind}")
def create_project(kind: str, doc: dict):
    if kind not in KINDS:
        raise HTTPException(404)
    doc = {**doc, "id": uuid.uuid4().hex[:12], "created": datetime.now().isoformat(timespec="seconds")}
    if kind == "workshop":
        with LOCK:
            return save(kind, migrate_workshop(doc))
    return save(kind, doc)


@router.get("/api/projects/{kind}/{pid}")
def get_project(kind: str, pid: str):
    return load(kind, pid)


@router.put("/api/projects/{kind}/{pid}")
def put_project(kind: str, pid: str, doc: dict):
    if kind == "workshop":
        with LOCK:
            stored = load(kind, pid)
            doc["id"] = pid
            return save(kind, merge_workshop(stored, migrate_workshop(doc)))
    load(kind, pid)  # must exist
    doc["id"] = pid
    return save(kind, doc)


@router.delete("/api/projects/{kind}/{pid}")
def delete_project(kind: str, pid: str):
    path_of(kind, pid).unlink(missing_ok=True)
    return {"ok": True}


@router.post("/api/upload")
async def upload(file: UploadFile = File(...), max_side: int | None = Query(None)):
    """Stores an image under outputs/uploads and returns its path relative to /outputs; max_side (>= 256) downscales
    so the long side fits (12 MP phone photos would otherwise become huge PNGs)."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(await file.read())))
        img.load()
    except Exception:
        raise HTTPException(400, "Not an image.")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "transparency" in img.info or img.mode in ("LA", "PA") else "RGB")
    if max_side and max(img.size) > max(256, max_side):
        s = max(256, max_side) / max(img.size)
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    name = f"{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:10]}.png"
    img.save(UPLOADS / name)
    return {"path": f"uploads/{name}", "width": img.width, "height": img.height}


# ---------------- book: story draft ----------------
# Three passes, like a real book: an editor's plan (what happens, and why it matters), the writing, then a line edit.
# One-shot drafts read like summaries; planning beats first and editing afterwards is what makes the text worth reading.

PLAN_SYSTEM = """You are an award-winning author and story editor of illustrated books. Before anyone writes a word, you
plan the book. Reply with JSON only, in exactly this shape:
{"title": str, "subtitle": str, "logline": str, "heart": str,
 "hero": {"name": str, "want": str, "flaw": str, "quirk": str},
 "characters": [{"name": str, "role": str, "look": str}],
 "settings": [str], "refrain": str,
 "beats": [{"page": int, "event": str, "turn": str}]}
Craft rules:
- Take the premise but find its surprising, specific angle. Reject the first idea anyone would have.
- "heart": the feeling or small truth the reader takes away. It is shown by the story, never said in it.
- The hero wants something concrete from the first pages, and something real stands in the way.
- Escalate: at least two attempts that fail or make things worse, each different and more inventive than the last.
- A low point at about two thirds of the book.
- The hero solves it through their own choice, quirk or flaw turned into strength. Not luck, not magic, not a helper.
- The ending echoes the opening image with a change, and the last page has a small surprise, joke or warm twist.
- Exactly N_PAGES beats, one per page; every beat moves something ("turn": what is different at the end of the page).
  No filler pages, no two pages with the same beat.
- At most 3-4 recurring characters. "look" (in English) is a precise visual description an illustrator can repeat on
  every page: species or material, body shape, colors, clothing, one or two distinctive features. No real or famous
  people, no brands.
- "settings": 2-4 recurring, visually distinct places (in English).
- "refrain": a short line in LANGUAGE that can recur and change meaning at the end; "" if it does not suit the book.
- "title" and "subtitle" in LANGUAGE: short, intriguing, not generic.
- Never: "it was all a dream", stated morals ("and so they learned that ..."), sparkly magic that fixes everything."""

WRITE_SYSTEM = """You are an award-winning author writing the final text of an illustrated book from an approved plan.
Reply with JSON only, in exactly this shape:
{"blurb": str, "cover_scene": str, "pages": [{"text": str, "scene": str}]}
Text rules ("text" is printed on the page, written in LANGUAGE for AUDIENCE, TEXT_LENGTH per page):
- Write like a native author of LANGUAGE, never like a translation.
- It must be a joy to read aloud: rhythm, varied sentence length, strong specific verbs, concrete sensory nouns.
- Dialogue with distinct voices. Humor where it fits.
- Show feelings through action and dialogue; never "she felt sad".
- Words and picture work together: do not describe what the illustration already shows. Tell what a picture cannot:
  sounds, thoughts, what someone says, what time passed.
- Make readers turn the page: many pages end on an open question, a promise or an unresolved moment.
- Use the refrain (if any) at least three times; its last use lands differently.
- Avoid: "suddenly", "magical", "very", "realized", adjective stacks, clichés, explaining the moral.
- The last line must land.
- Exactly N_PAGES pages, following the beats in order.
"scene" (in English) describes one illustration for that page: setting, the key moment of the beat, poses and
expressions, camera distance and angle, light and mood. Vary the camera across pages (wide establishing shot, medium,
close-up, view from above or below). Name the characters exactly as in the plan when they appear. Never ask for text,
letters or signs in the picture.
"blurb" (LANGUAGE): back-cover text, 2-4 sentences that make someone want to read the book, without giving away the end.
"cover_scene" (English): the front-cover illustration showing the hero and the promise of the book, with empty calm
space in the upper third for the title."""

EDIT_SYSTEM = """You are a demanding line editor for illustrated books in LANGUAGE, audience AUDIENCE. You receive the
plan and the page texts. First find what is weak, then rewrite. Reply with JSON only, in exactly this shape:
{"critique": [str], "pages": [{"text": str}]}
- "critique": 3-6 short, concrete problems you found (in English).
- "pages": the improved text for every page, same number and order (N_PAGES), TEXT_LENGTH per page.
Check every page: Does it sound natural to a native speaker? Is it fun to read aloud? Does each page turn pull the reader
on? Are there flat, generic or explaining sentences, filler words, repeated words, clichés, stated morals? Are the jokes
sharp? Do the characters sound different? Does the refrain land? Does the last line land?
Keep what works, keep the story and the page beats, and keep the character names. Change what does not work."""


DRAFTS: dict[str, dict] = {}  # draft task id -> {"stage", "result", "error", "created"}


STORY_NEED_GB = 26  # gemma 31B q4 + 16k context ≈ 22 GB, plus a reserve (unified memory: running out freezes the machine)


async def ensure_story_memory(model: str) -> None:
    """Refuses to make Ollama load the story model when the studio, the judge and DepthPro leave too little memory."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            loaded = {m.get("name") for m in (await client.get(f"{OLLAMA}/api/ps")).json().get("models") or []}
    except Exception:
        return  # Ollama down: ollama_json reports that
    if model in loaded:
        return
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:") and (free := int(line.split()[1]) / 2**20) < STORY_NEED_GB:
            raise RuntimeError(f"Not enough free memory for the story model ({free:.0f} GB free, about {STORY_NEED_GB} GB "
                               "needed). Stop the judge (systemctl --user stop flux-judge) or unload studio models, then try again.")


async def ollama_json(system: str, user: str, model: str, think: bool = False, temperature: float = 0.8) -> dict:
    await ensure_story_memory(model)
    body = {"model": model, "stream": False, "format": "json", "keep_alive": "2m", "think": think,
            "options": {"temperature": temperature, "num_ctx": 16384},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    last = None
    for _ in range(2):  # a retry when the model wanders off the JSON shape
        try:
            async with httpx.AsyncClient(timeout=1200) as client:
                r = await client.post(f"{OLLAMA}/api/chat", json=body)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"The story model is not reachable via Ollama ({e}).")
        try:
            return json.loads(r.json()["message"]["content"])
        except Exception as e:
            last = e
    raise RuntimeError(f"The story model returned no usable JSON ({last}). Try again.")


def fill(template: str, req: dict, n: int) -> str:
    length = {"short": "1-2 short sentences", "medium": "2-4 sentences", "long": "a paragraph of 4-7 sentences"}.get(
        req.get("text_length"), "2-4 sentences")
    return (template.replace("LANGUAGE", req.get("language") or "English").replace("N_PAGES", str(n))
            .replace("TEXT_LENGTH", length).replace("AUDIENCE", req.get("audience") or "all ages"))


WEAK_WORDS = {"english": ["suddenly", "very", "magical", "realized", "felt sad", "felt happy"],
              "german": ["plötzlich", "sehr", "magisch", "wurde klar", "fühlte sich traurig", "fühlte sich glücklich"]}


def lint(plan: dict, texts: list[str], language: str) -> list[str]:
    """Checks a model reliably skips when asked in prose: refrain count, banned words, the bookend ending."""
    out = []
    refrain = str(plan.get("refrain") or "").strip().rstrip(".!?")
    if refrain and len(texts) >= 6:
        key = " ".join(refrain.lower().split()[:4])
        uses = [i + 1 for i, t in enumerate(texts) if key in t.lower()]
        if len(uses) < 3:
            out.append(f'The refrain "{refrain}" appears only on page(s) {uses or "none"}. Use it (or a playful variation) '
                       f"on at least three pages, and let its last use on the final pages mean something new.")
    words = WEAK_WORDS.get(language.split()[0].lower(), WEAK_WORDS["english"] if language.lower().startswith("en") else [])
    for w in words:
        pages = [i + 1 for i, t in enumerate(texts) if re.search(rf"\b{re.escape(w)}\b", t, re.I)]
        if pages:
            out.append(f'Weak word "{w}" on page(s) {pages}: rewrite those sentences without it.')
    if len(texts) >= 6:
        out.append("Check that the last page echoes the first page (same place, words or gesture, but changed) and that its last line lands.")
    return out


async def edit_pass(req: dict, plan: dict, texts: list[str], model: str, notes: str = "") -> dict:
    n = len(texts)
    user = "Plan:\n" + json.dumps(plan, ensure_ascii=False) + "\n\nPages:\n" + "\n".join(
        f"{i + 1}. {t}" for i, t in enumerate(texts))
    found = lint(plan, texts, req.get("language") or "English")
    if found:
        user += "\n\nAn automatic check found these problems; fix all of them:\n- " + "\n- ".join(found)
    if notes:
        user += f"\n\nThe author's notes for this revision (follow them): {notes}"
    out = await ollama_json(fill(EDIT_SYSTEM, req, n), user, model, think=True, temperature=0.6)
    pages = [str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in out.get("pages", [])]
    if len(pages) != n or not all(p.strip() for p in pages):
        return {"critique": [str(c) for c in out.get("critique", [])], "pages": texts}  # keep the unedited text
    return {"critique": [str(c) for c in out.get("critique", [])], "pages": pages}


async def run_draft(task: dict, req: dict):
    try:
        model = req.get("model") or STORY_MODEL
        notes = (req.get("notes") or "").strip()
        t0 = time.time()
        if req.get("mode") == "revise":
            texts = [str(t) for t in req.get("texts") or []]
            task["stage"] = "Editing the text with your notes…"
            plan = req.get("outline") or {"premise": req.get("premise", "")}
            ed = await edit_pass(req, plan, texts, model, notes)
            task["result"] = {"mode": "revise", "pages": [{"text": t} for t in ed["pages"]], "critique": ed["critique"],
                              "seconds": round(time.time() - t0)}
            return
        n = max(4, min(40, int(req.get("pages") or 12)))
        user = (f"Premise: {req['premise']}\nNumber of pages: {n}\nAudience: {req.get('audience') or 'all ages'}\n"
                f"Illustration style: {req.get('style') or 'any'}")
        if req.get("characters"):
            user += "\nUse these characters (keep their names and looks): " + json.dumps(req["characters"], ensure_ascii=False)
        if notes:
            user += f"\nThe author's wishes: {notes}"
        task["stage"] = "1/3 Planning the plot…"
        plan = await ollama_json(fill(PLAN_SYSTEM, req, n), user, model, think=True, temperature=0.9)
        plan["beats"] = (plan.get("beats") or [])[:n]
        task["stage"] = "2/3 Writing the pages…"
        draft = await ollama_json(fill(WRITE_SYSTEM, req, n), "Approved plan:\n" + json.dumps(plan, ensure_ascii=False),
                                  model, temperature=0.85)
        pages = [{"text": str(p.get("text", "")), "scene": str(p.get("scene", ""))}
                 for p in draft.get("pages", []) if isinstance(p, dict)][:n]
        task["stage"] = "3/3 Editing the text…"
        ed = await edit_pass(req, plan, [p["text"] for p in pages], model, notes)
        for p, t in zip(pages, ed["pages"]):
            p["text"] = t
        looks = {c.get("name"): c for c in plan.get("characters", []) if isinstance(c, dict)}
        task["result"] = {
            "mode": "draft", "title": str(plan.get("title", "")), "subtitle": str(plan.get("subtitle", "")),
            "blurb": str(draft.get("blurb", "")), "cover_scene": str(draft.get("cover_scene", "")),
            "characters": [{"name": str(k), "look": str(c.get("look", ""))} for k, c in looks.items() if k],
            "pages": pages, "critique": ed["critique"],
            "outline": {k: plan.get(k) for k in ("logline", "heart", "hero", "settings", "refrain", "beats")},
            "seconds": round(time.time() - t0),
        }
    except Exception as e:
        task["error"] = str(e)
    finally:
        task["stage"] = "done"


@router.post("/api/books/{pid}/draft")
async def draft_story(pid: str, req: dict):
    """Starts a draft ("mode": "draft", from the premise) or a revision of the current texts ("mode": "revise") in the
    background; poll GET /api/books/draft/{task}. Takes a few minutes."""
    load("book", pid)
    if req.get("mode") == "revise":
        if not any(str(t).strip() for t in req.get("texts") or []):
            raise HTTPException(400, "There is no page text to revise yet.")
    elif not (req.get("premise") or "").strip():
        raise HTTPException(400, "Describe the story idea first.")
    for k in [k for k, t in DRAFTS.items() if time.time() - t["created"] > 3600]:
        del DRAFTS[k]
    tid = uuid.uuid4().hex[:10]
    DRAFTS[tid] = {"id": tid, "stage": "Starting…", "result": None, "error": None, "created": time.time()}
    DRAFTS[tid]["_task"] = asyncio.create_task(run_draft(DRAFTS[tid], req))  # keep a reference so it is not collected
    return {"task": tid}


@router.get("/api/books/draft/{tid}")
def draft_status(tid: str):
    if tid not in DRAFTS:
        raise HTTPException(404, "Unknown draft task (the server may have restarted).")
    t = DRAFTS[tid]
    return {"stage": t["stage"], "done": t["stage"] == "done", "result": t["result"], "error": t["error"],
            "seconds": round(time.time() - t["created"])}


# ---------------- exports ----------------

def export_dir(kind: str, pid: str) -> Path:
    d = EXPORTS / f"{kind}-{pid}"
    d.mkdir(exist_ok=True)
    for f in d.iterdir():
        f.unlink()
    return d


@router.get("/api/books/options")
def book_options():
    return printing.options()


@router.post("/api/books/{pid}/export")
async def export_book(pid: str):
    book = load("book", pid)
    d = export_dir("book", pid)
    try:
        report = await run_in_threadpool(printing.build_book, book, OUT, d)
    except printing.ExportError as e:
        raise HTTPException(400, str(e))
    rel = lambda p: f"/outputs/{p.relative_to(OUT)}?t={int(time.time())}"
    report["files"] = {k: rel(v) for k, v in report["files"].items()}
    report["previews"] = [rel(p) for p in report["previews"]]
    return report


@router.post("/api/workshop/{pid}/sheet")
async def export_sheet(pid: str):
    doc = load("workshop", pid)
    d = export_dir("workshop", pid)
    skipped = []
    try:
        pdf = await run_in_threadpool(printing.build_sheet, doc, OUT, d, skipped)
    except printing.ExportError as e:
        raise HTTPException(400, str(e))
    return {"pdf": f"/outputs/{pdf.relative_to(OUT)}?t={int(time.time())}", "skipped": skipped}
