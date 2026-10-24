"""HTTP endpoints for the room geometry (DepthPro analysis + calibration). DepthPro has its own lock, so a
room is measured within seconds even while a FLUX job holds engine.lock."""

import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from PIL import Image

import geometry

router = APIRouter()
OUT = Path(__file__).parent / "outputs"
GEO_DIR = OUT / "geometry"
MIN_FREE_GB = 23         # before loading DepthPro (~4 GB weights + 4 GB activations) + the 15 GB reserve
MIN_FREE_INFER_GB = 19   # before every inference once it is loaded (activations at 1536 px) + the 15 GB reserve
depth_lock = threading.Lock()


def depth_loaded() -> bool:
    return geometry.depth_loaded()


def unload() -> None:
    with depth_lock:
        geometry.unload_depth()


def _mem_available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 ** 2
    except OSError:
        pass
    return float("inf")


def _room(room) -> str:
    if not isinstance(room, str) or not re.fullmatch(r"uploads/[\w.-]+\.png", room) or not (OUT / room).is_file():
        raise HTTPException(400, "Ungültiges Raumfoto.")
    return room


def _fail(room: str, error: str) -> dict:
    with Image.open(OUT / room) as im:
        size = im.size
    return geometry._empty(room, size, error)


def _analyze(room: str, force: bool) -> dict:
    if not depth_lock.acquire(timeout=120):
        return _fail(room, "GPU ist beschäftigt – später erneut versuchen.")
    try:
        if _mem_available_gb() < (MIN_FREE_INFER_GB if geometry.depth_loaded() else MIN_FREE_GB):
            return _fail(room, "Zu wenig freier Speicher – später erneut versuchen.")
        with Image.open(OUT / room) as im:
            im.load()
            return geometry.analyze(im, room, force, geo_dir=GEO_DIR)
    finally:
        depth_lock.release()


@router.post("/api/geometry/analyze")
async def analyze(body: dict):
    room = _room(body.get("room"))
    force = bool(body.get("force"))
    if not force:
        cached = geometry.load_cached(room, GEO_DIR)
        if cached:
            return cached
    return await run_in_threadpool(_analyze, room, force)


def _num(v, name: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Ungültiger Wert: {name}.")


def _pt(p) -> list[float]:
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise HTTPException(400, "Ungültiger Messpunkt.")
    return [_num(p[0], "Messpunkt"), _num(p[1], "Messpunkt")]


@router.post("/api/geometry/calibrate")
def calibrate(body: dict):
    room = _room(body.get("room"))
    # under depth_lock: a forced re-analysis reads the old calibration first and saves last, so it would drop this one
    if not depth_lock.acquire(timeout=120):
        raise HTTPException(503, "Raum wird gerade vermessen – bitte gleich noch einmal.")
    try:
        return _calibrate(room, body)
    finally:
        depth_lock.release()


def _calibrate(room: str, body: dict) -> dict:
    geo = geometry.load_cached(room, GEO_DIR)
    if not geo or not geo.get("ok"):
        raise HTTPException(400, "Raum noch nicht vermessen.")
    measured, scaled = None, True
    try:
        replace = body.get("on_conflict") == "replace"
        if body.get("reset"):
            geo = geometry.rescale(geo, 1 / (geo.get("scale") or 1.0))
            geo["scale"], geo["refs"] = 1.0, []
        elif body.get("room_height_cm") is not None:
            h = _num(body["room_height_cm"], "Raumhöhe")
            if not 100 <= h <= 500:
                raise HTTPException(400, "Raumhöhe muss zwischen 100 und 500 cm liegen.")
            geo, measured, _ = geometry.set_room_height(geo, h, replace)
            scaled = measured is not None  # no ceiling detected: the height is stored, the scale stays as it was
        else:
            mode = body.get("mode") or "floor"
            if mode not in ("floor", "vertical", "depth"):
                raise HTTPException(400, "Unbekannte Messart.")
            length = _num(body.get("length_cm"), "Länge")
            if not 1 <= length <= 5000:
                raise HTTPException(400, "Ungültige Länge.")
            depth = geometry.depth_map(room, GEO_DIR) if mode == "depth" else None
            label = body.get("label") if isinstance(body.get("label"), str) else None
            geo, measured, _ = geometry.calibrate(geo, _pt(body.get("p1")), _pt(body.get("p2")), length, mode, depth,
                                                  (label or "")[:40] or None, replace)
    except geometry.RefConflict as e:   # nothing saved: the client asks which of the two is right
        return {"geometry": geo, "conflict": e.info, "measured_cm": None, "scale": geo.get("scale") or 1.0, "scaled": False}
    except ValueError as e:
        raise HTTPException(400, str(e))
    geometry.save_cached(geo, GEO_DIR)
    return {"geometry": geo, "measured_cm": measured, "scale": geo["scale"], "scaled": scaled}


@router.post("/api/geometry/segment")
def segment_at(body: dict):
    """Tap-to-select: {room, point: [u, v] (room image px), negative?: [[u, v], ...]} → {"poly", "box"} on the 0-1000 grid
    of the object under the tap (SAM 2.1), for „Etwas entfernen“."""
    import segment
    room = _room(body.get("room"))
    try:
        u, v = (float(x) for x in body.get("point") or ())
        neg = [[float(a), float(b)] for a, b in (body.get("negative") or [])][:8]
    except (TypeError, ValueError):
        raise HTTPException(400, "Ungültiger Punkt.")
    if not segment.loaded() and _mem_available_gb() < 18:
        raise HTTPException(503, "Zu wenig freier Speicher – bitte einen Rahmen ziehen.")
    with Image.open(OUT / room) as im:
        im.load()
        img = geometry.rgb(im)
    if not (0 <= u <= img.width and 0 <= v <= img.height):
        raise HTTPException(400, "Punkt liegt außerhalb des Fotos.")
    try:
        masks, first = segment.masks_at(img, [[u, v]], neg)
    except Exception as e:
        raise HTTPException(503, f"Auswahl gerade nicht möglich – bitte einen Rahmen ziehen. ({type(e).__name__})")
    big = lambda parts: sum((o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1]) for o in parts) > 600_000   # ~whole photo
    opts = [parts for parts in map(segment.outline, masks) if parts and not big(parts)]
    if not opts:
        raise HTTPException(422, "Dort wurde nichts erkannt – bitte genauer tippen oder einen Rahmen ziehen.")
    first = min(first, len(opts) - 1)
    return {"parts": opts[first], "options": opts, "index": first}
