"""Daily data backup: one rotating tar.gz of projects/ (not .secret) and every file a project references, written to
settings.backup.target (ideally a USB disk or NAS)."""

import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

import projects

router = APIRouter()
DATA = Path(os.environ.get("FLUX_DATA") or Path(__file__).parent)
EVERY_S = 24 * 3600
IMG = re.compile(r"[\w./-]+\.(png|jpe?g|webp)")
_RUN = threading.Lock()
LAST: dict | None = None  # result of the last run in this process (also when last.json could not be written)


class Busy(Exception):
    pass


class Failed(Exception):
    pass


def _strings(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)
    elif isinstance(x, str):
        yield x


def referenced(data: Path | None = None) -> list[Path]:
    """projects/ files (not .secret, no temp files) + the outputs every project references: image paths anywhere in the
    docs (room, sample, sketches, renders, variant and trashed images, book pages), their .json sidecars and the refs
    listed there, guides/<pid>-*, and the sent version folders exports/workshop-<pid>/v<n>/."""
    data = Path(data or DATA)
    store, out = data / "projects", data / "outputs"
    files = [p for p in sorted(store.rglob("*")) if p.is_file() and p.name != ".secret" and not p.name.endswith(".tmp")]
    found: set[Path] = set()

    root = out.resolve()

    def add(rel: str):
        if ".." in rel or rel.startswith("/") or not IMG.fullmatch(rel):
            return
        p = out / rel
        if not p.is_file() or p in found or root not in p.resolve().parents:
            return
        found.add(p)
        side = p.with_suffix(".json")
        if p.parent == out and side.is_file():
            found.add(side)
            try:
                for r in json.loads(side.read_text()).get("refs") or []:
                    if isinstance(r, str):
                        add(r)
            except (OSError, ValueError, AttributeError):
                pass

    try:  # the company logo
        for s in _strings(json.loads((store / "settings.json").read_text())):
            add(s)
    except (OSError, ValueError):
        pass
    for kind in projects.KINDS:
        for f in sorted((store / kind).glob("*.json")):
            try:
                doc = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            for s in _strings(doc):
                add(s)
            if kind == "workshop":
                found.update(p for p in (out / "guides").glob(f"{f.stem}-*") if p.is_file())
                exp = out / "exports" / f"workshop-{f.stem}"
                for v in exp.glob("v*") if exp.is_dir() else []:
                    if v.is_dir() and re.fullmatch(r"v\d+", v.name):
                        found.update(p for p in v.rglob("*") if p.is_file())
    return files + sorted(found)


def _target(s: dict) -> Path:
    return Path(s["backup"]["target"])


def _archives(target: Path) -> list[Path]:
    return sorted(target.glob("werkstatt-*.tar.gz")) if target.is_dir() else []


def _finish(target: Path, res: dict) -> dict:
    global LAST
    LAST = res
    try:
        projects.write_json(target / "last.json", res)
    except OSError:
        pass
    return res


def run(settings: dict | None = None) -> dict:
    """Writes <target>/werkstatt-<stamp>.tar.gz (via .part), rotates to `keep`, writes last.json. Raises Busy when a
    backup is already running and Failed when the target is not writable or too full."""
    if not _RUN.acquire(blocking=False):
        raise Busy()
    try:
        s = settings or projects.settings()
        target, keep = _target(s), s["backup"]["keep"]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"werkstatt-{stamp}.tar.gz"
        part = target / f"{name}.part"
        try:
            target.mkdir(parents=True, exist_ok=True)
            files = referenced()
            need = sum(p.stat().st_size for p in files if p.exists()) * 1.2
            if shutil.disk_usage(target).free < need:
                raise Failed(f"zu wenig Platz am Ziel ({need / 2**20:.0f} MB nötig)")
            with tarfile.open(part, "w:gz", compresslevel=1) as tar:  # images are PNG/JPEG: gzip gains little
                for p in files:
                    arc = str(p.relative_to(DATA))
                    try:
                        if p.suffix == ".json":  # small and rewritten while we run: archive one consistent read
                            data = p.read_bytes()
                            info = tarfile.TarInfo(arc)
                            info.size, info.mtime, info.mode = len(data), int(p.stat().st_mtime), 0o600
                            tar.addfile(info, io.BytesIO(data))
                        else:
                            tar.add(p, arc, recursive=False)
                    except FileNotFoundError:
                        continue  # purged meanwhile
            os.replace(part, target / name)
            for old in _archives(target)[:-keep]:
                old.unlink(missing_ok=True)
            res = {"at": datetime.now().isoformat(timespec="seconds"), "file": name,
                   "bytes": (target / name).stat().st_size, "ok": True, "error": None}
        except (OSError, ValueError, Failed, tarfile.TarError) as e:  # ValueError: a path outside DATA
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            _finish(target, {"at": datetime.now().isoformat(timespec="seconds"), "file": None, "bytes": 0,
                             "ok": False, "error": str(e) or type(e).__name__})
            raise Failed(str(e)) from e
        return _finish(target, res)
    finally:
        _RUN.release()


def _same_disk(target: Path) -> bool:
    p = target
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return os.stat(p).st_dev == os.stat(DATA).st_dev
    except OSError:
        return False


def status() -> dict:
    s = projects.settings()
    target = _target(s)
    last = None
    try:
        last = json.loads((target / "last.json").read_text())
    except (OSError, ValueError):
        pass
    if LAST and (not isinstance(last, dict) or str(LAST.get("at")) > str(last.get("at"))):
        last = LAST
    return {"last": last if isinstance(last, dict) else None, "target": str(target), "keep": s["backup"]["keep"],
            "count": len(_archives(target)), "running": _RUN.locked(), "same_disk": _same_disk(target)}


def maybe_run() -> dict | None:
    """Runs a backup when the newest archive is older than 24 h (the server calls it hourly while the GPU is idle)."""
    arch = _archives(_target(projects.settings()))
    if arch and time.time() - arch[-1].stat().st_mtime < EVERY_S:
        return None
    try:
        return run()
    except (Busy, Failed) as e:
        print("backup:", type(e).__name__, e, flush=True)
        return None


@router.get("/api/backup")
def get_backup():
    return status()


@router.post("/api/backup")
async def post_backup():
    try:
        await run_in_threadpool(run)
    except Busy:
        raise HTTPException(409, "Sicherung läuft bereits.")
    except Failed:
        raise HTTPException(507, "Sicherung fehlgeschlagen: Ziel nicht beschreibbar oder voll.")
    return status()
