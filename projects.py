"""Saved projects for the Book and Workshop pages: JSON documents on disk (workshop model v3 with status pipeline,
revisions and granular PATCH ops), settings, image uploads, story drafting through a local Ollama model, and the
book print exports (see printing.py)."""

import asyncio
import copy
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import string
import tempfile
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps

import imaging
import printing

try:  # HEIC/HEIF uploads from iPhones and desktops
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

HERE = Path(__file__).parent
DATA = Path(os.environ.get("FLUX_DATA") or HERE)
OUT = DATA / "outputs"
UPLOADS = OUT / "uploads"
EXPORTS = OUT / "exports"
STORE = DATA / "projects"
KINDS = ("book", "workshop")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
STORY_MODEL = os.environ.get("STORY_MODEL", "gemma4:31b-it-q4_K_M")

for d in (UPLOADS, EXPORTS, *(STORE / k for k in KINDS)):
    d.mkdir(parents=True, exist_ok=True)

router = APIRouter()


class Gone(HTTPException):
    def __init__(self, detail: str = "Auftrag nicht gefunden."):
        super().__init__(404, detail)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_json(path, obj) -> None:
    """Atomic: unique temp file in the same dir, fsync, os.replace (concurrent writers never share a temp file)."""
    path = Path(path)
    f = tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False,
                                    encoding="utf-8")
    try:
        with f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(f.name, path)
    except BaseException:
        Path(f.name).unlink(missing_ok=True)
        raise


# ---------------- locks, cache, files ----------------

LOCK = threading.RLock()  # kept for compatibility; every read-modify-write uses the per-project lock below
_META = threading.Lock()
_LOCKS: dict[tuple, threading.RLock] = {}
_CACHE: dict[str, tuple] = {}  # path -> ((ino, mtime_ns, size), raw parsed JSON); never handed out uncopied
_LIST_REV = [int(time.time())]


@contextmanager
def locked(kind: str, pid: str):
    with _META:
        lk = _LOCKS.setdefault((kind, pid), threading.RLock())
    with lk:
        yield


def list_rev() -> int:
    return _LIST_REV[0]


def _bump_list_rev() -> None:
    with _META:
        _LIST_REV[0] += 1


def path_of(kind: str, pid: str) -> Path:
    if kind not in KINDS or not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9]{6,32}", pid):
        raise Gone() if kind == "workshop" else HTTPException(404, "Project not found.")
    return STORE / kind / f"{pid}.json"


def _read(p: Path):
    """Raw parsed JSON (shared cache object, do not modify) or None if the file is missing."""
    try:
        with open(p, "rb") as f:
            st = os.fstat(f.fileno())
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            c = _CACHE.get(str(p))
            if c and c[0] == key:
                return c[1]
            raw = json.loads(f.read())
    except FileNotFoundError:
        return None
    _CACHE[str(p)] = (key, raw)
    return raw


def load(kind: str, pid: str) -> dict:
    """Migrated + normalized deep copy (raw JSON cached by inode/mtime/size)."""
    raw = _read(path_of(kind, pid))
    if raw is None:
        raise Gone() if kind == "workshop" else HTTPException(404, "Project not found.")
    doc = copy.deepcopy(raw)
    return migrate_workshop(doc) if kind == "workshop" else doc


def _write(kind: str, doc: dict, who=None, touch: bool = True) -> dict:
    if touch:
        doc["updated"] = now()
        if who:
            doc["updated_by"] = who
    p = path_of(kind, doc["id"])
    write_json(p, doc)
    try:
        st = p.stat()
        _CACHE[str(p)] = ((st.st_ino, st.st_mtime_ns, st.st_size), copy.deepcopy(doc))
    except OSError:
        _CACHE.pop(str(p), None)
    _bump_list_rev()
    return doc


def save(kind: str, doc: dict, who=None) -> dict:
    """Whole-document write (book PUT, creation): rev + 1, updated/updated_by, atomic file."""
    with locked(kind, doc["id"]):
        doc["rev"] = max(0, _int(doc.get("rev"), 0)) + 1
        return _write(kind, doc, who)


def title(pid: str) -> str:
    try:
        raw = _read(path_of("workshop", pid))
    except HTTPException:
        return ""
    return raw.get("title") if isinstance(raw, dict) and isinstance(raw.get("title"), str) else ""


# ---------------- workshop documents (version 3) ----------------
# v2: several pieces per project, each with its own dims/material/sketch/placement; variants carry a snapshot of the
# pieces at generation time and are written by the server (workshop.record).
# v3: status pipeline, Vorgangs-Nr., customer data, activity log, versions/approval, and revisions: `rev` per doc and
# `frev` per changed path, so concurrent edits of different fields merge and only real same-field edits conflict.

WORKSHOP_VERSION = 3
STATUS = ["anfrage", "termin", "entwurf", "kunde", "freigegeben", "auftrag", "fertig", "abgesagt"]
STATUS_LABELS = {"anfrage": "Anfrage", "termin": "Vor-Ort-Termin", "entwurf": "Entwurf", "kunde": "Beim Kunden",
                 "freigegeben": "Freigegeben", "auftrag": "Auftrag", "fertig": "Abgeschlossen", "abgesagt": "Abgesagt"}
ORDER = {s: i for i, s in enumerate(STATUS[:7])}  # abgesagt: manual only, never moved automatically
QUALITY = ("entwurf", "fein")
CONTACT = {"salutation": 20, "phone": 40, "email": 120, "street": 120, "zip": 12, "city": 80}  # salutation: Herr/Frau/Familie
TEXTS = {"title": 200, "customer": 200, "setting": 1000, "room_changes": 1000, "ref": 20, "ext_ref": 60,
         "internal_notes": 4000}
PIECE_LABELS = {"schrank": "Einbauschrank", "kueche": "Küche", "regal": "Regalwand", "sideboard": "Sideboard",
                "wandregal": "Wandregal", "haengeschrank": "Hängeschrank",
                "tisch": "Esstisch", "bett": "Bett", "treppe": "Treppe", "garderobe": "Garderobe", "bad": "Badmöbel",
                "sitzbank": "Sitzbank", "wand": "Wandverkleidung", "tuer": "Tür"}
MATERIAL_LABELS = {"eiche": "Eiche natur geölt", "eicheraeuchert": "Eiche geräuchert", "eicheweiss": "Eiche weiß geölt",
                   "nuss": "Nussbaum", "esche": "Esche", "kirsch": "Kirschbaum", "fichte": "Fichte massiv",
                   "multiplex": "Multiplex Birke", "altholz": "Altholz", "weiss": "Lack weiß matt",
                   "anthrazit": "Lack anthrazit"}
HANDLE_LABELS = {"push": "grifflos (Push)", "leiste": "Griffleiste", "messing": "Messing", "schwarz": "Schwarz matt",
                 "leder": "Lederlaschen", "knopf": "Holzknöpfe"}
DEFAULT_PIECE = {"type": None, "text": "", "label": "", "material": "eiche", "material_custom": "",
                 "material_label": "Eiche natur geölt", "handles": "push", "handles_label": "grifflos (Push)",
                 "w": None, "h": None, "d": None, "sketch": None, "render": None, "use_render": False, "sample": None,
                 "where": "", "placement": {"mode": "none", "pose": None, "box": None}}
PIECE_FIELDS = ("type", "text", "material", "material_custom", "handles", "w", "h", "d", "sketch", "render",
                "use_render", "sample", "where", "elev", "placement", "slope_fit")
LEGACY_KEYS = ("piece_type", "piece_text", "w", "h", "d", "material", "material_custom", "handles", "sketch", "box", "where", "dims")
MAX_PIECES = 4
MAX_OPS = 200
ACTIVITY_MAX, TRASH_MAX, FREV_MAX, OLD_FILES_MAX = 300, 20, 400, 500
NO_COALESCE = {"status", "version_sent", "version_ready", "approved", "change_request", "viewed"}
ALNUM = string.ascii_lowercase + string.digits
PIECE_ID, VARIANT_ID = r"[a-z0-9]{4,12}", r"[a-z0-9]{4,16}"
APPT_AT = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"


def _rid(n: int, seed: str | None = None) -> str:
    """Random id, or a stable one derived from seed (so loading an unsaved v1 doc twice gives the same ids)."""
    if seed is not None:
        return hashlib.sha1(seed.encode()).hexdigest()[:n]
    return "".join(random.choices(ALNUM, k=n))


def _str(v, n: int) -> str:
    return str(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _int(v, default: int) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dim(v) -> int | None:
    f = _num(str(v).strip().replace(",", ".")) if isinstance(v, str) else (None if isinstance(v, bool) else _num(v))
    return int(round(f)) if f is not None and 1 <= round(f) <= 2000 else None


def _elev(v) -> int | None:
    """Height of a wall-hung piece's underside above the floor in cm (0 = stands on the floor)."""
    f = _num(str(v).strip().replace(",", ".")) if isinstance(v, str) else (None if isinstance(v, bool) else _num(v))
    return int(round(f)) if f is not None and 0 <= round(f) <= 400 else None


def _img(v) -> str | None:
    return v if isinstance(v, str) and ".." not in v and not v.startswith("/") and re.fullmatch(r"[\w./-]+\.png", v) else None


def _box(b, strict: bool = True) -> list | None:
    """Grid box [top, left, bottom, right] clamped to 0..1000; None if malformed (or degenerate when strict)."""
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    nums = [_num(x) for x in b]
    if None in nums:
        return None
    t, l, bo, r = (min(1000, max(0, int(round(x)))) for x in nums)
    return None if strict and (bo <= t or r <= l) else [t, l, bo, r]


def _remove(r):
    """An „Entfernen“ area: a [top, left, bottom, right] frame, or a tapped outline {"poly": [[x, y], ...], "box": frame}."""
    if isinstance(r, dict):
        poly, box = r.get("poly"), _box(r.get("box"))
        if not box or not isinstance(poly, list) or not 3 <= len(poly) <= 32:
            return None
        try:
            pts = [[round(min(1000.0, max(0.0, float(x))), 1), round(min(1000.0, max(0.0, float(y))), 1)] for x, y in poly]
        except (TypeError, ValueError):
            return None
        return {"poly": pts, "box": box}
    return _box(r)


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
    if _elev(p.get("elev")):  # only wall-hung pieces carry it (keeps older documents byte-identical)
        out["elev"] = _elev(p.get("elev"))
    if p.get("slope_fit") is True:  # top cut to follow a sloped ceiling (Dachschräge); absent = straight top
        out["slope_fit"] = True
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


def _appointment(a) -> dict | None:
    if not isinstance(a, dict) or not isinstance(a.get("at"), str) or not re.fullmatch(APPT_AT, a["at"]):
        return None
    return {"at": a["at"], "what": _str(a.get("what"), 60) or "Aufmaß"}


def _infer_status(doc: dict) -> str:
    if any(isinstance(v, dict) and v.get("image") for v in doc.get("variants") or [] if isinstance(doc.get("variants"), list)):
        return "entwurf"
    return "termin" if _img(doc.get("room")) else "anfrage"


def is_test(t) -> bool:
    return bool(re.match(r"TEST\b", t.strip())) if isinstance(t, str) else False


def _frev(f) -> dict:
    out = {}
    for p, e in (f.items() if isinstance(f, dict) else []):
        if isinstance(p, str) and isinstance(e, list) and len(e) in (3, 4) and _int(e[0], 0) >= 1:
            out[p] = [_int(e[0], 0), e[1] if isinstance(e[1], str) else None, _str(e[2], 40)] + (["auto"] if e[3:] == ["auto"] else [])
    return out


def _prune(frev: dict, floor: int) -> tuple[dict, int]:
    if len(frev) <= FREV_MAX:
        return frev, floor
    items = sorted(frev.items(), key=lambda kv: kv[1][0], reverse=True)
    floor = max(floor, max(e[0] for _, e in items[FREV_MAX:]))
    return dict(items[:FREV_MAX]), floor


def normalize_workshop(doc: dict) -> dict:
    """Clamps and validates a v3 workshop doc in place (never raises), fills defaults, strips the v1 keys and derives
    test/thumb/locked. Unknown keys are kept."""
    for k in LEGACY_KEYS:
        doc.pop(k, None)
    doc["version"] = WORKSHOP_VERSION
    doc["rev"] = max(1, _int(doc.get("rev"), 1))
    doc["frev"], doc["frev_floor"] = _prune(_frev(doc.get("frev")), max(0, _int(doc.get("frev_floor"), 0)))
    for k in ("created_by", "updated_by"):
        doc[k] = _str(doc.get(k), 60) or None
    for k, n in TEXTS.items():
        doc[k] = _str(doc.get(k), n)
    doc["test"] = is_test(doc["title"])
    doc["room"], doc["sample"] = _img(doc.get("room")), _img(doc.get("sample"))
    rs = doc.get("room_size")
    doc["room_size"] = [int(rs[0]), int(rs[1])] if (isinstance(rs, list) and len(rs) == 2 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x < 100_000 for x in rs)) else None
    doc["tidy"], doc["protect"], doc["guide"] = (_bool(doc.get("tidy"), True), _bool(doc.get("protect"), True),
                                                 _bool(doc.get("guide"), False))
    if "mockup" in doc:  # 3d mock-up as a reference; absent = on (keeps older documents byte-identical)
        doc["mockup"] = _bool(doc.get("mockup"), True)
    count = _num(doc.get("count"))
    doc["count"] = min(4, max(1, int(count))) if count is not None else 2
    if doc.get("quality") not in QUALITY:
        doc["quality"] = "entwurf" if doc.get("res") == 1024 else "fein"
    doc["res"] = 1536  # derived, kept for old readers (both presets render at 1536 px)
    doc["removes"] = [b for b in map(_remove, doc.get("removes") or [] if isinstance(doc.get("removes"), list) else []) if b][:20]
    j = doc.get("judge") if isinstance(doc.get("judge"), dict) else {}
    doc["judge"] = {"enabled": _bool(j.get("enabled"), True), "auto_retry": _bool(j.get("auto_retry"), True)}
    g = doc.get("geometry")
    doc["geometry"] = g if isinstance(g, dict) and len(json.dumps(g)) <= 65536 else None
    c = doc.get("calibration")
    doc["calibration"] = c if isinstance(c, dict) and len(json.dumps(c)) <= 4096 else None
    if not isinstance(doc.get("company"), dict):
        doc["company"] = {}
    if doc.get("status") not in STATUS:
        doc["status"] = _infer_status(doc)
    c = doc.get("contact") if isinstance(doc.get("contact"), dict) else {}
    doc["contact"] = {k: _str(c.get(k), n) for k, n in CONTACT.items()}
    doc["appointment"] = _appointment(doc.get("appointment"))
    doc["archived_at"] = _str(doc.get("archived_at"), 40) or None
    doc["versions"] = [v for v in doc.get("versions") or [] if isinstance(v, dict) and _int(v.get("n"), 0) >= 1] \
        if isinstance(doc.get("versions"), list) else []
    doc["approved"] = doc["approved"] if isinstance(doc.get("approved"), dict) else None
    act = [a for a in doc.get("activity") or [] if isinstance(a, dict)] if isinstance(doc.get("activity"), list) else []
    doc["activity"] = [{"at": _str(a.get("at"), 40), "by": _str(a.get("by"), 60) or None, "what": _str(a.get("what"), 40),
                        "text": _str(a.get("text"), 2000)} for a in act][-ACTIVITY_MAX:]

    pieces, used = [], set()
    for i, p in enumerate((doc.get("pieces") if isinstance(doc.get("pieces"), list) else [])[:MAX_PIECES]):
        pid = p.get("id") if isinstance(p, dict) else None
        if not (isinstance(pid, str) and re.fullmatch(PIECE_ID, pid)) or pid in used:
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

    if "old_files" in doc:  # replaced photos and renders that fell out of the trash: still the customer's, for DSGVO
        of = doc["old_files"] if isinstance(doc["old_files"], list) else []
        doc["old_files"] = list(dict.fromkeys(x for x in of if _img(x)))[-OLD_FILES_MAX:]
    dv = [x for x in doc.get("deleted_variants") or [] if isinstance(x, str) and re.fullmatch(VARIANT_ID, x)]
    doc["deleted_variants"] = list(dict.fromkeys(dv))[-500:]
    deleted = set(doc["deleted_variants"])
    pj = [x for x in doc.get("pending_jobs") or [] if isinstance(x, str) and re.fullmatch(r"[a-f0-9]{6,32}", x)]
    doc["pending_jobs"] = list(dict.fromkeys(pj))[-20:]
    trash = []
    for v in doc.get("trash") if isinstance(doc.get("trash"), list) else []:
        if isinstance(v, dict) and _img(v.get("image")) and isinstance(v.get("id"), str) and re.fullmatch(VARIANT_ID, v["id"]):
            v = _variant(v, v["id"])
            v.pop("locked", None)
            trash.append(v)
    doc["trash"] = trash[-TRASH_MAX:]

    frozen = {x for ver in doc["versions"] if ver.get("status") != "zurueckgezogen"
              for x in (ver.get("variant_ids") if isinstance(ver.get("variant_ids"), list) else [])}
    appr = doc["approved"].get("variant_id") if doc["approved"] else None
    variants, ids, images = [], set(), set()
    for v in doc.get("variants") if isinstance(doc.get("variants"), list) else []:
        if not isinstance(v, dict) or not _img(v.get("image")) or v["image"] in images:
            continue
        vid = v.get("id")
        if not (isinstance(vid, str) and re.fullmatch(VARIANT_ID, vid)) or vid in ids:
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
        v["locked"] = vid in frozen or vid == appr
        variants.append(v)
    doc["variants"] = sorted(variants, key=lambda v: v["image"], reverse=True)
    if doc.get("selected") not in ids:
        doc["selected"] = variants[0]["id"] if variants else None
    by_id = {v["id"]: v for v in doc["variants"]}
    pick = by_id.get(appr) or next((v for v in doc["variants"] if v["fav"]), None) or (doc["variants"] or [None])[0]
    doc["thumb"] = pick["image"] if pick else doc["room"]
    if doc["approved"] is not None:  # derived: the pieces no longer match what the customer approved
        doc["approved"]["outdated"] = isinstance(doc["approved"].get("spec"), list) and doc["approved"]["spec"] != design_spec(doc)
    return doc


def design_spec(doc: dict) -> list:
    """What a customer approval refers to: type, text, dims, wood and handles of every designed piece."""
    return [[p.get(k) for k in ("id", "type", "text", "w", "h", "d", "material", "material_custom", "handles")]
            for p in doc.get("pieces") or [] if isinstance(p, dict) and (p.get("type") or str(p.get("text") or "").strip())]


def new_approvals() -> int:
    """Approvals by the customer link that nobody in the shop has looked at yet (badge on the dashboard)."""
    rev = list_rev()
    if _NEWS[0] != rev:
        n = 0
        for f in (STORE / "workshop").glob("*.json"):
            try:
                d = load("workshop", f.stem)
            except Exception:
                continue
            a = d.get("approved") or {}
            n += d["status"] == "freigegeben" and a.get("via") == "link" and not a.get("seen_at")
        _NEWS[:] = [rev, n]
    return _NEWS[1]


_NEWS = [None, 0]


def _v1_to_v2(doc: dict) -> dict:
    """v1 (one piece as top-level keys) → v2 structure (unchanged algorithm of the v2 code)."""
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
    doc.update(version=2, pieces=[piece], active_piece=piece["id"], guide=False,
               judge={"enabled": True, "auto_retry": True}, geometry=None, calibration=None,
               deleted_variants=[], pending_jobs=[])
    return doc


def migrate_workshop(doc: dict) -> dict:
    """Pure v1 → v2 → v3, then normalize. Rollback-safe: the v2 → v3 step never overwrites a v3 value (a doc saved by
    the v2 code after a rollback says version 2 but keeps every v3 key); rev is never reset."""
    ver = _int(doc.get("version"), 1)
    if ver >= WORKSHOP_VERSION:
        return normalize_workshop(doc)
    if ver < 2:
        _v1_to_v2(doc)
    had_v3 = "rev" in doc
    doc.setdefault("status", _infer_status(doc))
    doc.setdefault("quality", "entwurf" if doc.get("res") == 1024 else "fein")
    for k, v in (("ref", ""), ("ext_ref", ""), ("frev", {}), ("frev_floor", 0), ("activity", []), ("versions", []),
                 ("approved", None), ("trash", []), ("contact", {}), ("appointment", None), ("internal_notes", ""),
                 ("archived_at", None), ("created_by", None), ("updated_by", None)):
        doc.setdefault(k, copy.deepcopy(v))
    doc["rev"] = max(_int(doc.get("rev"), 0), 0) + 1
    if had_v3:  # edits made by the v2 code are not in frev: every client's old base_rev must re-fetch
        doc["frev_floor"] = doc["rev"]
    return normalize_workshop(doc)


job_running: Callable[[str], bool] | None = None  # set by the server: is this job id still queued/running/being judged?


# ---------------- activity, status ----------------

def log(doc: dict, what: str, text: str, who) -> None:
    """The only writer of doc.activity: an entry with the same what+by from the last 5 minutes is replaced (moved to the
    end with the new time and text), except for milestones."""
    act = doc.setdefault("activity", [])
    t = now()
    if what not in NO_COALESCE:
        for i in range(len(act) - 1, -1, -1):
            try:
                if (datetime.fromisoformat(t) - datetime.fromisoformat(act[i]["at"])).total_seconds() > 300:
                    break
            except (KeyError, TypeError, ValueError):
                break
            if act[i].get("what") == what and act[i].get("by") == who:
                del act[i]  # replaced by the entry appended below (newest last)
                break
    act.append({"at": t, "by": who, "what": what, "text": text})
    del act[:-ACTIVITY_MAX]


def advance(doc: dict, target: str, who, text=None) -> bool:
    """Automatic status move: forward only, never out of abgesagt; logs."""
    cur = doc.get("status")
    if target not in ORDER or cur == "abgesagt" or ORDER.get(cur, 0) >= ORDER[target]:
        return False
    doc["status"] = target
    log(doc, "status", text or f"Status: {STATUS_LABELS.get(cur, cur)} → {STATUS_LABELS[target]}", who)
    return True


def name_of(who, team=None) -> str:
    if not who:
        return ""
    if who.startswith("dev:"):
        return f"Gerät {who[4:8]}"
    if who in ("kunde", "system"):
        return {"kunde": "Kunde", "system": "System"}[who]
    for m in (settings()["team"] if team is None else team):
        if m.get("id") == who:
            return m.get("name") or who
    return who


# ---------------- revisions ----------------

_M = object()
IGNORED = {"rev", "frev", "frev_floor", "updated", "updated_by", "thumb", "test", "activity", "old_files",
           "selected", "active_piece"}  # derived/bookkeeping; the last two are deprecated per-device view state
ITEM_IGNORED = {"pieces": {"label", "material_label", "handles_label"}, "variants": {"locked"}}


def _diff_items(k: str, a: list, b: list) -> list[str]:
    A = {x.get("id"): x for x in a if isinstance(x, dict)}
    B = {x.get("id"): x for x in b if isinstance(x, dict)}
    out = []
    for i in sorted(set(A) | set(B), key=str):
        if i not in A or i not in B:
            out.append(f"{k}.{i}")
            continue
        x, y = A[i], B[i]
        out += [f"{k}.{i}.{f}" for f in sorted(set(x) | set(y)) if f not in ITEM_IGNORED[k] and x.get(f, _M) != y.get(f, _M)]
    return out


def changed_paths(old: dict, new: dict) -> list[str]:
    """Paths whose value really changed, at the granularity of frev and of conflict checks (spec §5.1)."""
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k, _M), new.get(k, _M)
        if k in IGNORED or a == b:
            continue
        if k in ITEM_IGNORED and isinstance(a, list) and isinstance(b, list):
            out += _diff_items(k, a, b)
        elif k in ("contact", "judge") and isinstance(a, dict) and isinstance(b, dict):
            out += [f"{k}.{f}" for f in sorted(set(a) | set(b)) if a.get(f, _M) != b.get(f, _M)]
        else:
            out.append(k)
    return out


def _related(p: str, q: str) -> bool:
    return p == q or q.startswith(p + ".") or p.startswith(q + ".")


def _is_placement(p: str) -> bool:
    s = p.split(".")
    return len(s) == 3 and s[0] == "pieces" and s[2] == "placement"


def changed_since(doc: dict, base_rev: int, prefixes) -> list[dict]:
    """frev entries newer than base_rev under the prefixes (segment-wise), oldest first."""
    prefixes = (prefixes,) if isinstance(prefixes, str) else tuple(prefixes)
    if base_rev < _int(doc.get("frev_floor"), 0):  # pruned history: assume a change
        return [{"path": "", "by": None, "by_name": "", "at": None}]
    team = settings()["team"]
    hits = sorted(((e[0], p, e) for p, e in (doc.get("frev") or {}).items()
                   if e[0] > base_rev and any(p == x or p.startswith(x + ".") for x in prefixes)), key=lambda t: t[:2])
    return [{"path": p, "by": e[1], "by_name": name_of(e[1], team), "at": e[2]} for _, p, e in hits]


def _commit(kind: str, before: dict, after: dict, who, auto: bool) -> dict:
    rev = max(_int(before.get("rev"), 0), 0) + 1
    after["rev"] = rev
    if kind == "workshop":
        who_ = who or "system"
        frev = dict(before.get("frev") or {})
        t = now()
        for p in changed_paths(before, after):
            frev[p] = [rev, who_, t] + (["auto"] if auto else [])
        after["frev"], after["frev_floor"] = _prune(frev, _int(before.get("frev_floor"), 0))
    return _write(kind, after, who)


def mutate(kind: str, pid: str, fn: Callable[[dict], None], who=None, auto: bool = True) -> dict:
    """Read-modify-write under the per-project lock. fn edits the doc in place; nothing changed (deep-equal after
    normalizing) → no save, no rev bump; else rev + 1 and frev for every changed path (marked "auto" for server-side
    writers). Raises Gone for a missing project."""
    with locked(kind, pid):
        doc = load(kind, pid)
        work = copy.deepcopy(doc)
        fn(work)
        work["id"] = pid
        if kind == "workshop":
            normalize_workshop(work)
        if work == doc:
            return work
        return _commit(kind, doc, work, who, auto)


# ---------------- PATCH ops ----------------

class _Bad(Exception):
    pass


class _GoneOp(Exception):
    pass


class _LockedOp(Exception):
    pass


def _bad(path) -> HTTPException:
    return HTTPException(400, f"Ungültige Änderung: {path}.")


def _find(items: list, iid: str) -> dict | None:
    return next((x for x in items if x.get("id") == iid), None)


def _need_str(v, n):
    if not isinstance(v, str):
        raise _Bad()
    return v[:n]


def _need_bool(v):
    if not isinstance(v, bool):
        raise _Bad()
    return v


def _need_img(v):
    if v is not None and not _img(v):
        raise _Bad()
    return v


def _piece_value(f: str, v):
    """Validates one piece field; the piece is re-normalized by _piece afterwards."""
    if f == "type":
        if v is not None and v not in PIECE_LABELS:
            raise _Bad()
    elif f == "material":
        if v not in MATERIAL_LABELS and v != "custom":
            raise _Bad()
    elif f == "handles":
        if v is not None and v not in HANDLE_LABELS:
            raise _Bad()
    elif f in ("w", "h", "d"):
        if v is not None and (_dim(v) is None or isinstance(v, bool)):
            raise _Bad()
    elif f == "elev":
        if v is not None and (_elev(v) is None or isinstance(v, bool)):
            raise _Bad()
    elif f in ("sketch", "render", "sample"):
        _need_img(v)
    elif f in ("use_render", "slope_fit"):
        _need_bool(v)
    elif f in ("text", "material_custom", "where"):
        _need_str(v, 2000)
    elif f == "placement":
        if v is not None and not isinstance(v, dict):
            raise _Bad()
    return v


def _client_piece(v: dict) -> dict:
    return _piece({k: x for k, x in v.items() if k not in ("label", "material_label", "handles_label")}, v["id"])


def _set(doc: dict, path: str, v) -> None:
    s = path.split(".")
    k = s[0]
    if len(s) == 1:
        if k in TEXTS:
            doc[k] = _need_str(v, TEXTS[k])
        elif k in ("room", "sample"):
            doc[k] = _need_img(v)
        elif k == "room_size":
            if v is not None and not (isinstance(v, list) and len(v) == 2 and all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x < 100_000 for x in v)):
                raise _Bad()
            doc[k] = v
        elif k in ("tidy", "protect", "guide", "mockup"):
            doc[k] = _need_bool(v)
        elif k == "count":
            if not isinstance(v, int) or isinstance(v, bool):
                raise _Bad()
            doc[k] = min(4, max(1, v))
        elif k == "quality":
            if v not in QUALITY:
                raise _Bad()
            doc[k] = v
        elif k == "removes":
            if not isinstance(v, list) or len(v) > 20 or any(_remove(b) is None for b in v):
                raise _Bad()
            doc[k] = [_remove(b) for b in v]
        elif k in ("geometry", "calibration"):
            if v is not None and not (isinstance(v, dict) and len(json.dumps(v)) <= (65536 if k == "geometry" else 4096)):
                raise _Bad()
            doc[k] = v
        elif k == "appointment":
            if v is not None and (_appointment(v) is None or not isinstance(v.get("what", ""), str)):
                raise _Bad()
            doc[k] = _appointment(v)
        elif k == "status":
            if v not in STATUS:
                raise _Bad()
            doc[k] = v
        else:
            raise _Bad()
    elif k == "contact" and len(s) == 2 and s[1] in CONTACT:
        doc["contact"][s[1]] = _need_str(v, CONTACT[s[1]])
    elif k == "judge" and len(s) == 2 and s[1] in ("enabled", "auto_retry"):
        doc["judge"][s[1]] = _need_bool(v)
    elif k == "pieces" and len(s) in (2, 3) and re.fullmatch(PIECE_ID, s[1]):
        if len(s) == 3 and s[2] not in PIECE_FIELDS:
            raise _Bad()
        if len(s) == 2 and not isinstance(v, dict):
            raise _Bad()
        p = _find(doc["pieces"], s[1])
        if p is None:
            raise _GoneOp()
        if len(s) == 2:
            for f in PIECE_FIELDS:
                if f in v:
                    _piece_value(f, v[f])
            new = dict(v)
        else:
            new = {**p, s[2]: _piece_value(s[2], v)}
        for f in ("label", "material_label", "handles_label"):  # recomputed server-side from the codes
            new.pop(f, None)
        doc["pieces"][doc["pieces"].index(p)] = _piece(new, s[1])
    elif k == "variants" and len(s) == 3 and s[2] == "fav" and re.fullmatch(VARIANT_ID, s[1]):
        v = _need_bool(v)
        x = _find(doc["variants"], s[1])
        if x is None:
            raise _GoneOp()
        x["fav"] = v
    else:
        raise _Bad()


def _op_path(op) -> str:
    if not isinstance(op, dict):
        raise _bad("op")
    kind = op.get("op")
    if kind == "set":
        p = op.get("path")
        if not isinstance(p, str) or not p or len(p) > 80:
            raise _bad("path")
        return p
    if kind == "add_piece":
        v = op.get("value")
        if not isinstance(v, dict) or not isinstance(v.get("id"), str) or not re.fullmatch(PIECE_ID, v["id"]):
            raise _bad("add_piece")
        return f"pieces.{v['id']}"
    if kind in ("remove_piece", "delete_variant", "restore_variant"):
        iid = op.get("id")
        if not isinstance(iid, str) or not re.fullmatch(PIECE_ID if kind == "remove_piece" else VARIANT_ID, iid):
            raise _bad(kind)
        return f"{'pieces' if kind == 'remove_piece' else 'variants'}.{iid}"
    if kind in ("archive", "unarchive"):
        return "archived_at"
    raise _bad(kind if isinstance(kind, str) and len(kind) < 40 else "op")


def _apply(doc: dict, op: dict, path: str) -> None:
    """One op on a normalized doc (in place). Raises _Bad, _GoneOp, _LockedOp or a 400 HTTPException."""
    kind = op["op"]
    if kind == "set":
        _set(doc, path, op.get("value"))
    elif kind == "add_piece":
        v = op["value"]
        if _find(doc["pieces"], v["id"]):
            return  # a re-sent op (lost ack): the piece is already there
        if len(doc["pieces"]) >= MAX_PIECES:
            raise HTTPException(400, "Höchstens 4 Möbel pro Auftrag.")
        for f in PIECE_FIELDS:
            if f in v:
                _piece_value(f, v[f])
        doc["pieces"].append(_client_piece(v))
    elif kind == "remove_piece":
        p = _find(doc["pieces"], op["id"])
        if p is None:
            return  # already removed (re-sent op)
        i = doc["pieces"].index(p)
        if len(doc["pieces"]) == 1:
            r = op.get("replacement")
            if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not re.fullmatch(PIECE_ID, r["id"]) \
                    or r["id"] == op["id"]:
                raise _Bad()
            for f in PIECE_FIELDS:
                if f in r:
                    _piece_value(f, r[f])
            doc["pieces"][i] = _client_piece(r)
        else:
            del doc["pieces"][i]
    elif kind == "delete_variant":
        v = _find(doc["variants"], op["id"])
        if v is None:
            if op["id"] in doc["deleted_variants"]:
                return  # already deleted
            raise _GoneOp()
        if v.get("locked"):
            raise _LockedOp()
        doc["variants"].remove(v)
        v = {k: x for k, x in v.items() if k != "locked"}
        doc["trash"] = (doc["trash"] + [v])[-TRASH_MAX:]
        doc["deleted_variants"].append(op["id"])
    elif kind == "restore_variant":
        v = _find(doc["trash"], op["id"])
        if v is None:
            if _find(doc["variants"], op["id"]):
                return
            raise _GoneOp()
        doc["trash"].remove(v)
        doc["deleted_variants"] = [x for x in doc["deleted_variants"] if x != op["id"]]
        doc["variants"].append(v)
    elif kind == "archive":
        doc["archived_at"] = doc.get("archived_at") or now()
    elif kind == "unarchive":
        doc["archived_at"] = None


def _get(doc: dict, path: str):
    s = path.split(".")
    if s[0] in ("pieces", "variants") and len(s) >= 2:
        x = _find(doc.get(s[0]) or [], s[1])
        return x if x is None or len(s) == 2 else x.get(s[2])
    v = doc.get(s[0])
    return v.get(s[1]) if len(s) == 2 and isinstance(v, dict) else v


def _fmt_at(at: str) -> str:
    try:
        d = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return str(at)
    return d.strftime("%d.%m.%Y, %H:%M Uhr")


def _piece_name(p) -> str:
    return (p or {}).get("label") or "Möbel"


def doc_images(doc: dict) -> set:
    """Every image path the doc points to (photos, sketches, renders, samples, variant and trashed variant images)."""
    out = {doc.get(k) for k in ("room", "sample", "sketch")}
    for p in doc.get("pieces") or []:
        if isinstance(p, dict):
            out.update(p.get(k) for k in ("sketch", "render", "sample"))
    for v in (doc.get("variants") or []) + (doc.get("trash") or []):
        if isinstance(v, dict):
            out.update(v.get(k) for k in ("image", "room", "sketch"))
    return {x for x in out if _img(x)}


def _side_effects(before: dict, work: dict, ops: list, who) -> None:
    """Activity entries and automatic status moves for a user's PATCH (design edits are not logged)."""
    dropped = doc_images(before) - doc_images(work)
    if dropped:  # a DSGVO purge still finds them (delivery.referenced_files)
        work["old_files"] = list(dict.fromkeys((work.get("old_files") or []) + sorted(dropped)))[-OLD_FILES_MAX:]
    paths = changed_paths(before, work)
    manual_status = "status" in paths
    if "customer" in paths or any(p.startswith("contact.") for p in paths):
        log(work, "meta", "Kundendaten geändert", who)
    if "ext_ref" in paths and work["ext_ref"]:
        log(work, "ext_ref", f"Angebots-Nr.: {work['ext_ref']}", who)
    if "appointment" in paths:
        a = work["appointment"]
        log(work, "appointment", f"Termin: {a['what']} am {_fmt_at(a['at'])}" if a else "Termin entfernt", who)
    if "room" in paths and work["room"]:
        log(work, "photo", "Raumfoto ersetzt" if before.get("room") else "Raumfoto hinzugefügt", who)
    old_p, new_p = {p["id"]: p for p in before["pieces"]}, {p["id"]: p for p in work["pieces"]}
    for i in new_p.keys() - old_p.keys():
        if any(o.get("op") == "add_piece" for o in ops):
            log(work, "piece_add", f"Möbel hinzugefügt: {_piece_name(new_p[i])}", who)
    for i in old_p.keys() - new_p.keys():
        log(work, "piece_remove", f"Möbel entfernt: {_piece_name(old_p[i])}", who)
    if "archived_at" in paths:
        log(work, "archived" if work["archived_at"] else "unarchived",
            "Archiviert" if work["archived_at"] else "Aus dem Archiv geholt", who)
    if any(p.startswith("variants.") and len(p.split(".")) == 2 and p.split(".")[1] in work["deleted_variants"]
           for p in paths):
        log(work, "deleted_variant", "Vorschlag gelöscht", who)
    if manual_status:
        log(work, "status", f"Status: {STATUS_LABELS[before['status']]} → {STATUS_LABELS[work['status']]}", who)
    elif ("room" in paths and work["room"] and not before.get("room")) or ("appointment" in paths and work["appointment"]):
        advance(work, "termin", who)


def apply_ops(pid: str, base_rev: int, ops: list, who) -> dict:
    """All-or-nothing PATCH (spec §5.1) under the per-project lock; returns the saved (or unchanged) doc."""
    if not isinstance(ops, list) or not ops or len(ops) > MAX_OPS:
        raise HTTPException(400, f"Ungültige Änderung: höchstens {MAX_OPS} Änderungen auf einmal.")
    if not isinstance(base_rev, int) or isinstance(base_rev, bool) or base_rev < 0:
        raise _bad("base_rev")
    paths = [_op_path(op) for op in ops]
    with locked("workshop", pid):
        doc = load("workshop", pid)
        if base_rev < doc["frev_floor"]:
            raise HTTPException(409, {"code": "stale", "message": "Der Auftrag wurde inzwischen geändert – er wird "
                                      "neu geladen.", "rev": doc["rev"], "doc": doc, "conflicts": []})
        team = settings()["team"]
        newer = [(q, e) for q, e in doc["frev"].items() if e[0] > base_rev]
        work = copy.deepcopy(doc)
        conflicts, gone = [], []
        for op, path in zip(ops, paths):
            cands = [] if _is_placement(path) else [
                (q, e) for q, e in newer if _related(path, q) and not _is_placement(q)
                and not (e[3:] == ["auto"] and path.startswith("variants."))]
            try:
                if cands:
                    trial = copy.deepcopy(work)
                    _apply(trial, op, path)
                    normalize_workshop(trial)
                    if any(_related(path, q) for q in changed_paths(work, trial)):
                        q, e = max(cands, key=lambda c: c[1][0])
                        mine = op.get("value", op.get("replacement")) if op["op"] != "remove_piece" else None
                        conflicts.append({"path": path, "mine": mine, "theirs": _get(work, path), "by": e[1],
                                          "by_name": name_of(e[1], team), "at": e[2]})
                        continue
                _apply(work, op, path)
                normalize_workshop(work)
            except _GoneOp:
                e = doc["frev"].get(".".join(path.split(".")[:2])) or [0, None, None]
                gone.append({"path": path, "by": e[1], "by_name": name_of(e[1], team), "at": e[2]})
            except _Bad:
                raise _bad(path)
            except _LockedOp:
                raise HTTPException(409, {"code": "locked", "message": "Dieser Vorschlag gehört zu einer gesendeten "
                                          "Version. Ziehen Sie zuerst die Version zurück.", "rev": doc["rev"],
                                          "doc": doc, "conflicts": [{"path": path}]})
        if gone or conflicts:
            name = (gone or conflicts)[0]["by_name"]
            msg = (f"Inzwischen von {name} gelöscht." if name else "Inzwischen gelöscht.") if gone else "Gleichzeitig geändert."
            raise HTTPException(409, {"code": "gone" if gone else "conflict", "message": msg, "rev": doc["rev"],
                                      "doc": doc, "conflicts": gone + conflicts})
        _side_effects(doc, work, ops, who)
        normalize_workshop(work)
        if work == doc:
            return work
        return _commit("workshop", doc, work, who, auto=False)


# ---------------- settings ----------------

SETTINGS_LOCK = threading.RLock()
COLORS = ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9", "#f87171", "#38bdf8"]
COMPANY = {"name": 120, "contact": 300, "street": 120, "zip": 12, "city": 80, "phone": 40, "email": 120, "web": 200}
HEX = r"#[0-9a-fA-F]{6}"
PW_HASH = r"pbkdf2_sha256\$\d+\$[0-9a-f]+\$[0-9a-f]+"


def default_backup_target() -> str:
    return str(Path.home() / "Dokumente" / "Projekte" / "bfl-backups")


def _settings_path(store=None) -> Path:
    return Path(store or STORE) / "settings.json"


def normalize_settings(s) -> dict:
    s = s if isinstance(s, dict) else {}
    c = s.get("company") if isinstance(s.get("company"), dict) else {}
    company = {k: _str(c.get(k), n) for k, n in COMPANY.items()}
    company["color"] = c["color"] if isinstance(c.get("color"), str) and re.fullmatch(HEX, c["color"]) else "#3d5a4a"
    company["logo"] = c["logo"] if _img(c.get("logo")) and c["logo"].startswith("uploads/") else None
    team, ids = [], set()
    for m in s.get("team") if isinstance(s.get("team"), list) else []:
        if isinstance(m, dict) and isinstance(m.get("id"), str) and re.fullmatch(r"[a-z0-9-]{1,20}", m["id"]) \
                and m["id"] not in ids and _str(m.get("name"), 40).strip():
            ids.add(m["id"])
            color = m["color"] if isinstance(m.get("color"), str) and re.fullmatch(HEX, m["color"]) else COLORS[len(team) % 6]
            team.append({"id": m["id"], "name": _str(m["name"], 40).strip(), "color": color})  # an old "pin" is dropped
    a = s.get("access") if isinstance(s.get("access"), dict) else {}
    pw = a["password"] if isinstance(a.get("password"), str) and re.fullmatch(PW_HASH, a["password"]) else None
    r = s.get("retention") if isinstance(s.get("retention"), dict) else {}
    b = s.get("backup") if isinstance(s.get("backup"), dict) else {}
    url = s.get("public_base_url") if isinstance(s.get("public_base_url"), str) else ""
    nr = s.get("next_ref") if isinstance(s.get("next_ref"), dict) else {}
    return {"version": 1, "company": company, "team": team,
            "access": {"password": pw, "epoch": max(1, _int(a.get("epoch"), 1))},
            "retention": {"abgesagt_months": min(120, max(1, _int(r.get("abgesagt_months"), 6))),
                          "fertig_months": min(120, max(1, _int(r.get("fertig_months"), 12)))},
            "public_base_url": url[:200] if url.startswith("https://") else "",
            "trusted_proxy": _bool(s.get("trusted_proxy"), False),
            "backup": {"target": b["target"][:500] if isinstance(b.get("target"), str) and os.path.isabs(b["target"])
                       else default_backup_target(), "keep": min(60, max(1, _int(b.get("keep"), 14)))},
            "next_ref": {k: max(1, _int(v, 1)) for k, v in nr.items() if isinstance(k, str) and re.fullmatch(r"\d{4}", k)}}


def settings(store=None) -> dict:
    """Full settings incl. the password hash (server-side use only); a deep copy."""
    raw = _read(_settings_path(store))
    return normalize_settings(copy.deepcopy(raw))


def update_settings(fn: Callable[[dict], None], store=None) -> dict:
    with SETTINGS_LOCK:
        s = settings(store)
        fn(s)
        s = normalize_settings(s)
        p = _settings_path(store)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_json(p, s)
        _CACHE.pop(str(p), None)
        return s


def public_settings(s: dict | None = None) -> dict:
    s = s or settings()
    return {"company": s["company"], "team": s["team"], "access": {"password_set": bool(s["access"]["password"])},
            "retention": s["retention"], "public_base_url": s["public_base_url"], "trusted_proxy": s["trusted_proxy"],
            "backup": s["backup"]}


def company_line(company: dict) -> str:
    c = company or {}
    if str(c.get("contact") or "").strip():
        return str(c["contact"]).strip()
    place = " ".join(x for x in (str(c.get("zip") or "").strip(), str(c.get("city") or "").strip()) if x)
    parts = [str(c.get("street") or "").strip(), place] + [str(c.get(k) or "").strip() for k in ("phone", "email", "web")]
    return " · ".join(x for x in parts if x)


def next_ref(year: str, store=None) -> str:
    """Next Vorgangs-Nr. "YYYY-NNN" from settings.next_ref (never below a number already used in the store)."""
    store = Path(store or STORE)
    used = 0
    for f in (store / "workshop").glob("*.json"):
        raw = _read(f)
        m = re.fullmatch(rf"{year}-(\d+)", str(raw.get("ref") or "")) if isinstance(raw, dict) else None
        used = max(used, int(m.group(1))) if m else used
    out = []

    def take(s):
        n = max(s["next_ref"].get(year, 1), used + 1)
        out.append(f"{year}-{n:03d}")
        s["next_ref"][year] = n + 1
    update_settings(take, store)
    return out[0]


@router.get("/api/settings")
def get_settings():
    return public_settings()


@router.put("/api/settings")
def put_settings(body: dict):
    c = body.get("company")
    if c is not None and not isinstance(c, dict):
        raise HTTPException(400, "Ungültige Firmendaten.")
    c = c or {}
    if "color" in c and not (isinstance(c["color"], str) and re.fullmatch(HEX, c["color"])):
        raise HTTPException(400, "Ungültige Hausfarbe.")
    if c.get("logo") is not None and not (_img(c["logo"]) and c["logo"].startswith("uploads/")):
        raise HTTPException(400, "Ungültiges Logo.")
    url = body.get("public_base_url")
    if url is not None and not (isinstance(url, str) and (url == "" or url.startswith("https://"))):
        raise HTTPException(400, "Die öffentliche Adresse muss mit https:// beginnen.")
    b = body.get("backup") if isinstance(body.get("backup"), dict) else {}
    if "target" in b and not (isinstance(b["target"], str) and os.path.isabs(b["target"])):
        raise HTTPException(400, "Der Ziel-Ordner muss ein vollständiger Pfad sein (beginnt mit /).")
    if "keep" in b and not (isinstance(b["keep"], int) and 1 <= b["keep"] <= 60):
        raise HTTPException(400, "Anzahl Sicherungen: 1 bis 60.")

    def fn(s):
        s["company"].update({k: v for k, v in c.items() if k in COMPANY or k in ("color", "logo")})
        if url is not None:
            s["public_base_url"] = url.strip().rstrip("/") if url else ""
        if isinstance(body.get("retention"), dict):
            s["retention"].update({k: v for k, v in body["retention"].items() if k in s["retention"]})
        if isinstance(body.get("trusted_proxy"), bool):
            s["trusted_proxy"] = body["trusted_proxy"]
        s["backup"].update({k: v for k, v in b.items() if k in ("target", "keep")})
    return public_settings(update_settings(fn))


# ---------------- migration ----------------

def migrate_all(store=None) -> list[dict]:
    """Once at server start: every workshop doc with raw version < 3 (oldest first) gets a byte backup (never
    overwritten), the v3 migration, a Vorgangs-Nr. if it has none, and one "migrated" activity entry. Idempotent."""
    store = Path(store or STORE)
    todo = []
    for f in (store / "workshop").glob("*.json"):
        try:
            raw = json.loads(f.read_text())
        except (OSError, ValueError) as e:
            print(f"migrate_all: skipped {f.name}: {e}", flush=True)
            continue
        if isinstance(raw, dict) and _int(raw.get("version"), 1) < WORKSHOP_VERSION:
            todo.append((str(raw.get("created") or ""), f.stem, f))
    out = []
    for _, pid, f in sorted(todo):
        with locked("workshop", pid):
            raw = json.loads(f.read_text())
            if _int(raw.get("version"), 1) >= WORKSHOP_VERSION:
                continue
            bk = store / "backup-v2" / "workshop" / f.name
            again = bk.exists()
            if not again:
                bk.parent.mkdir(parents=True, exist_ok=True)
                tmp = bk.with_name(f".{bk.stem}.{uuid.uuid4().hex[:8]}.tmp")
                shutil.copyfile(f, tmp)
                os.replace(tmp, bk)
            doc = migrate_workshop(copy.deepcopy(raw))
            doc["id"] = pid
            if not doc["ref"] and not doc["test"]:
                doc["ref"] = next_ref(str(doc.get("created") or now())[:4], store)
            if not any(a.get("what") == "migrated" for a in doc["activity"]):
                log(doc, "migrated", "Auf Werkstatt 3 umgestellt", "system")
            normalize_workshop(doc)
            write_json(f, doc)  # updated/updated_by stay: a migration is not an edit
            _CACHE.pop(str(f), None)
            _bump_list_rev()
            out.append({"id": pid, "from": _int(raw.get("version"), 1), "status": doc["status"], "ref": doc["ref"],
                        "again": again})
    return out


# ---------------- projects ----------------

def _who(request: Request | None) -> str:
    return (getattr(request.state, "owner", None) if request is not None else None) or "anon"


def _months_since(at) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(str(at))).days / 30.44
    except (TypeError, ValueError):
        return 0.0


def list_item(d: dict, s: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    appt = d.get("appointment")
    latest = max(d["versions"], key=lambda v: _int(v.get("n"), 0)) if d["versions"] else None
    if latest and latest.get("status") == "aenderung":
        attention = "aenderung"
    elif d["status"] == "freigegeben" and (d.get("approved") or {}).get("via") == "link":
        attention = "freigegeben"
    elif appt and appt["at"][:10] == today:
        attention = "termin_heute"
    elif appt and appt["at"] < datetime.now().strftime("%Y-%m-%dT%H:%M") and ORDER.get(d["status"], 9) <= ORDER["termin"]:
        attention = "ueberfaellig"
    else:
        attention = None
    marks = [a["at"] for a in d["activity"] if a.get("what") in ("status", "archived")]
    since = _months_since(marks[-1] if marks else d.get("archived_at") or d.get("updated"))
    r = s["retention"]
    due = ((d["status"] == "abgesagt" or bool(d["archived_at"])) and since >= r["abgesagt_months"]) or \
        (d["status"] == "fertig" and since >= r["fertig_months"])
    c = d["contact"]
    q = " ".join([d["title"], d["customer"], d["ref"], d["ext_ref"], *c.values(), d["internal_notes"][:500]])
    return {"id": d["id"], "title": d["title"] or "Ohne Titel", "customer": d["customer"], "status": d["status"],
            "ref": d["ref"], "ext_ref": d["ext_ref"], "city": c["city"], "appointment": appt,
            "archived_at": d["archived_at"], "test": d["test"], "retention_due": due, "created_by": d.get("created_by"),
            "thumb": d["thumb"], "updated": d.get("updated"), "updated_by": d.get("updated_by"),
            "updated_by_name": name_of(d.get("updated_by"), s["team"]), "variants": len(d["variants"]),
            "versions": len(d["versions"]), "approved": bool(d.get("approved")), "attention": attention,
            "new": attention == "freigegeben" and not (d.get("approved") or {}).get("seen_at"),
            "outdated": bool((d.get("approved") or {}).get("outdated")),
            "pending": len(d["pending_jobs"]), "rev": d["rev"], "q": " ".join(q.lower().split())}


@router.get("/api/projects/{kind}")
def list_projects(kind: str, archived: str = "0"):
    if kind not in KINDS:
        raise HTTPException(404)
    s = settings() if kind == "workshop" else None
    items = []
    for f in (STORE / kind).glob("*.json"):
        try:
            raw = _read(f)
            if kind == "book":
                items.append({"id": raw["id"], "title": raw.get("title") or "Untitled", "updated": raw.get("updated"),
                              "thumb": raw.get("thumb")})
                continue
            d = migrate_workshop(copy.deepcopy(raw))
            d["id"] = f.stem
        except Exception:
            continue
        if archived == "all" or (archived == "1") == bool(d["archived_at"]):
            items.append(list_item(d, s))
    return sorted(items, key=lambda d: d["updated"] or "", reverse=True)


def new_workshop(body: dict, who) -> dict:
    t = now()
    doc = {"id": uuid.uuid4().hex[:12], "version": WORKSHOP_VERSION, "rev": 0, "created": t, "created_by": who,
           "title": body.get("title"), "customer": body.get("customer"), "contact": body.get("contact"),
           "appointment": body.get("appointment"), "internal_notes": body.get("internal_notes"),
           "ext_ref": body.get("ext_ref"), "status": "anfrage", "quality": "entwurf", "count": 2,
           "judge": {"enabled": True, "auto_retry": False}, "pieces": [{**DEFAULT_PIECE, "id": _rid(6)}],
           "frev": {}, "frev_floor": 0, "activity": [], "versions": [], "approved": None, "trash": [],
           "archived_at": None, "variants": [], "deleted_variants": [], "pending_jobs": []}
    normalize_workshop(doc)
    doc["ref"] = "" if doc["test"] else next_ref(t[:4])
    log(doc, "created", "Auftrag angelegt", who)
    if doc["appointment"]:
        advance(doc, "termin", who)
    return doc


@router.post("/api/projects/{kind}")
def create_project(kind: str, request: Request, body: dict | None = None):
    if kind not in KINDS:
        raise HTTPException(404)
    body = body if isinstance(body, dict) else {}
    who = _who(request)
    doc = new_workshop(body, who) if kind == "workshop" else {**body, "id": uuid.uuid4().hex[:12], "created": now()}
    with locked(kind, doc["id"]):
        doc["rev"] = 1
        return _write(kind, doc, who)


@router.get("/api/projects/{kind}/{pid}")
def get_project(kind: str, pid: str):
    return load(kind, pid)


@router.patch("/api/projects/workshop/{pid}")
def patch_project(pid: str, body: dict, request: Request):
    before = load("workshop", pid)
    doc = apply_ops(pid, body.get("base_rev"), body.get("ops"), _who(request))
    try:  # the company profile learns from manual corrections; that must never break a save
        import profile as company_profile  # lazy: profile → workshop → projects
        company_profile.learn_doc(before, doc, _who(request), body.get("ops"))
    except Exception:
        traceback.print_exc()
    return {"rev": doc["rev"], "doc": doc}


@router.put("/api/projects/{kind}/{pid}")
def put_project(kind: str, pid: str, doc: dict, request: Request):
    if kind == "workshop":
        raise HTTPException(409, "Die Werkstatt wurde aktualisiert – bitte die Seite neu laden.")
    with locked(kind, pid):
        stored = load(kind, pid)  # must exist
        doc["id"] = pid
        doc["rev"] = _int(stored.get("rev"), 0) + 1
        return _write(kind, doc, _who(request))


@router.delete("/api/projects/{kind}/{pid}")
def delete_project(kind: str, pid: str):
    """Deletes the JSON only (delivery's purge removes the files; it calls this to drop the cache and bump list_rev)."""
    p = path_of(kind, pid)
    with locked(kind, pid):
        p.unlink(missing_ok=True)
        _CACHE.pop(str(p), None)
    _bump_list_rev()
    return {"ok": True}


@router.post("/api/upload")
async def upload(file: UploadFile = File(...), max_side: int | None = Query(None)):
    """Stores an image under outputs/uploads and returns its path relative to /outputs; max_side (>= 256) downscales
    so the long side fits (12 MP phone photos would otherwise become huge PNGs). The PIL work runs in the threadpool:
    the event loop keeps serving everybody else's saves and syncs."""
    data = await file.read(imaging.MAX_UPLOAD + 1)
    if len(data) > imaging.MAX_UPLOAD:
        raise HTTPException(413, f"Das Bild ist zu groß (höchstens {imaging.MAX_UPLOAD // 2**20} MB).")
    return await run_in_threadpool(_store_upload, data, max_side)


def _store_upload(data: bytes, max_side: int | None) -> dict:
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
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


STORY_NEED_GB = 37  # gemma 31B q4 + 16k context ≈ 22 GB, plus the 15 GB reserve (unified memory: running out freezes the machine)


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

def export_dir(kind: str, pid: str, keep: int = 3) -> Path:
    """A fresh exports/<kind>-<pid>/<stamp>/ folder (parallel exports never share one); keeps the newest `keep`."""
    base = EXPORTS / f"{kind}-{pid}"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d, i = base / stamp, 1
    while True:
        try:
            d.mkdir()
            break
        except FileExistsError:
            i += 1
            d = base / f"{stamp}-{i}"
    old = sorted((x for x in base.iterdir() if x != d and x.is_dir() and re.fullmatch(r"\d{8}-\d{6}(-\d+)?", x.name)),
                 key=lambda x: x.stat().st_mtime_ns)
    for x in old[:len(old) - (keep - 1)]:
        shutil.rmtree(x, ignore_errors=True)
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
