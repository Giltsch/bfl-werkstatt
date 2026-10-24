"""The assistant (Phase 3): one conversation per order. A carpenter sends a room photo and says or types what to build;
the assistant measures the room, understands the order (intent.py), finds the places and the objects to remove in the
photo (ground.py), places the pieces like a tap in step ③ would (place.py), shows a „So?“ preview, and on „ja“ checks
the plan, generates through the normal job queue, waits for the images and the judge, and answers with the best images,
scores, the offer text and the 3D/DXF/PDF files.

The order is a normal Werkstatt order: every change goes through projects.apply_ops (the PATCH path), generation through
workshop.generate, so the web app shows and edits everything the assistant did.

Conversation: projects/agent/<pid>.json (mode 600) = {"state", "events": [...], questions, jobs, …}. States:
need_photo → analyzing → understanding → asking → preview → generating → done, plus error. Events are append-only:
{"n", "at", "role": "user"|"agent", "kind": "text"|"audio"|"photo"|"question"|"preview"|"result"|"status"|"error",
 "text", "image" (path under outputs/), "data"}.

Threads: every order has at most one worker thread (inputs are queued and handled one after the other); a single
watcher thread follows running generations and hands their end back to the order's worker. handle() never blocks on a
model."""

import copy
import json
import math
import os
import re
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import projects
import workshop

router = APIRouter()
HERE = Path(__file__).parent
STATES = ("need_photo", "analyzing", "understanding", "asking", "preview", "generating", "done", "error")
BUSY_STATES = ("analyzing", "understanding")
MAX_EVENTS = 500
MAX_TEXT = 2000
MAX_AUDIO = 25 * 2**20
PHOTO_SIDE = 2560            # the web app uploads room photos with maxSide 2560 too
COUNT = 2                    # images per generation (the order's default)
ASK_CONF = 0.35              # below this a grounded spot is not used: the assistant asks instead
SURE_CONF = 0.5              # below this the spot is used but marked „unsicher“ in the preview
MOVE_CM = 30                 # „weiter links“ without a number
CLEAR = 15                   # grid units between a piece and the X of „neben X“ (VLM frames leave out door casings)
PREVIEW_SIDE = 1280
KEEP_PREVIEWS = 5
WATCH_S = 3.0
INTENT_RETRY_S = 20          # one automatic retry when the language model is down (vLLM restarts take ~1 min)
LAN_PORT = int(os.environ.get("FLUX_PORT") or 7860)
COLORS = workshop.COLORS     # = W3D.COLORS: piece k has the same colour as in step ③
FONT = HERE / "fonts" / "Nunito-Bold.ttf"
EDGES = ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7))

HELP = ("So arbeite ich: Foto vom Raum schicken und sagen oder schreiben, was hinein soll – z. B. „links neben dem "
        "Fenster ein Einbauschrank, zwei-zwanzig breit, raumhoch, Weißlack, drei Türen grifflos, der alte Schrank kommt "
        "weg“. Ich frage nur nach, was fehlt, zeige eine Vorschau und rechne auf „ja“.\n"
        "Jederzeit möglich: „20 cm breiter“, „achtzig tief“, „nimm Eiche“, „weiter links“, „Raumhöhe 2,50“, "
        "„nochmal“ (neue Bilder), „senden“ (Kundenversion vorbereiten), „stopp“ (Berechnung abbrechen), „status“.")
ASK_PHOTO = "Bitte schicke zuerst ein Foto des Raums – am besten aus der Tür, mit Boden und Decke im Bild."
ASK_WHAT = ("Was soll gebaut werden? Zum Beispiel: „links neben dem Fenster ein Einbauschrank, zwei Meter breit, "
            "raumhoch, Weißlack, grifflos“.")
SO_HINT = ("Antworte „ja“ zum Rechnen – oder sag, was anders soll (z. B. „20 cm breiter“, „nimm Eiche“, "
           "„weiter links“).")
FIX_HINT = ("Antworte „Vorschlag“, dann setze ich das so um – „trotzdem rechnen“ rechnet die Bilder wie geplant, oder sag, "
            "was anders soll.")
_SLOPE_FIT = re.compile(r"\b(?:an die (?:dach)?schräge anpassen|(?:dach)?schräge anpassen|an (?:die )?(?:dach)?schräge angepasst|"
                        r"der (?:dach)?schräge folgen|schräg (?:zu)?schneiden|oben schräg)\b")
_SLOPE_LOWER = re.compile(r"(?:dann |mach (?:ihn |es |den schrank )?)?(?:niedriger|so hoch wie (?:es )?(?:geht|passt)|passend niedriger)( machen)?( bitte)?")
_SLOPE_MOVE = re.compile(r"(?:einen? )?ander(?:er|en) platz( bitte| suchen| nehmen)?|wo passt (?:er|es|der schrank)( hin)?|stell (?:ihn|es) woanders hin")
_FIX_RX = re.compile(r"(den |deinen )?vorschlag( übernehmen| nehmen| umsetzen| bitte)?|so wie vorgeschlagen|wie vorgeschlagen")

_LOCKS: dict[str, threading.RLock] = {}
_META = threading.Lock()
_QUEUES: dict[str, deque] = {}
_RUNNING: set = set()
_SUBS: list = []
_WATCH: set = set()
_WATCHER = {"thread": None, "stop": threading.Event()}


# ---------------- storage ----------------

def _dir() -> Path:
    return Path(projects.STORE) / "agent"


def _path(pid: str) -> Path:
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9]{6,32}", pid):
        raise projects.Gone()
    return _dir() / f"{pid}.json"


def _lock(pid: str) -> threading.RLock:
    with _META:
        return _LOCKS.setdefault(pid, threading.RLock())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(pid: str) -> dict | None:
    try:
        return json.loads(_path(pid).read_text())
    except (OSError, ValueError):
        return None


def _save(conv: dict) -> None:
    p = _path(conv["pid"])
    p.parent.mkdir(parents=True, exist_ok=True)
    conv["updated"] = _now()
    conv["events"] = conv["events"][-MAX_EVENTS:]
    projects.write_json(p, conv)
    try:
        os.chmod(p, 0o600)   # the texts can name the customer
    except OSError:
        pass


def _new_conv(pid: str, who: str, channel: str, state: str, auto_title=None) -> dict:
    return {"pid": pid, "version": 1, "created": _now(), "updated": _now(), "who": who, "channel": channel,
            "state": state, "stable": state if state not in BUSY_STATES else "asking", "events": [], "questions": [], "todo": [], "jobs": [], "retried": False,
            "retry_seen": False, "preview": None, "preview_rev": None, "focus": None, "queued": [],
            "auto_title": auto_title, "notes": []}


def _update(pid: str, fn) -> dict:
    """Read-modify-write of the conversation under its lock."""
    with _lock(pid):
        conv = _load(pid)
        if conv is None:
            raise projects.Gone()
        fn(conv)
        _save(conv)
        return conv


def _emit(pid: str, role: str, kind: str, text: str = "", image: str | None = None, data=None,
          state: str | None = None) -> dict:
    """Appends one event (and optionally moves the state), then tells the subscribers (channels, webhooks)."""
    ev = {}

    def fn(conv):
        n = (conv["events"][-1]["n"] if conv["events"] else 0) + 1
        ev.update({"n": n, "at": _now(), "role": role, "kind": kind, "text": text})
        if image:
            ev["image"] = image
        if data is not None:
            ev["data"] = data
        conv["events"].append(ev)
        if state:
            _set(conv, state)

    _update(pid, fn)
    if role == "agent":
        for cb in list(_SUBS):
            try:
                cb(pid, dict(ev))
            except Exception:
                traceback.print_exc()   # a channel or webhook problem never breaks the conversation
    return dict(ev)


def _set(conv: dict, state: str) -> None:
    conv["state"] = state
    if state not in BUSY_STATES:
        conv["stable"] = state


def _state(pid: str, state: str) -> None:
    _update(pid, lambda c: _set(c, state))


def _say(pid: str, text: str, state: str | None = None, data=None) -> dict:
    return _emit(pid, "agent", "text", text, data=data, state=state)


def _status(pid: str, text: str, data=None) -> dict:
    return _emit(pid, "agent", "status", text, data=data)


def _error(pid: str, text: str, state: str | None = None) -> dict:
    return _emit(pid, "agent", "error", text, state=state)


def _ask(pid: str, text: str, data=None) -> dict:
    return _emit(pid, "agent", "question", text, data=data, state="asking")


# ---------------- public contract (channels, connect, router) ----------------

def subscribe(cb):
    """cb(pid, event) for every agent event; returns the unsubscribe callable."""
    _SUBS.append(cb)

    def unsubscribe():
        if cb in _SUBS:
            _SUBS.remove(cb)
    return unsubscribe


def events(pid: str, after: int = 0) -> list[dict]:
    """Events with n > after. A conversation whose order was purged is deleted here (it may name the customer)."""
    conv = _load(pid) if re.fullmatch(r"[a-z0-9]{6,32}", str(pid)) else None
    if conv is None:
        return []
    if not projects.path_of("workshop", pid).exists():
        _forget(pid)
        return []
    return [e for e in conv["events"] if e.get("n", 0) > (after or 0)]


def state(pid: str) -> str | None:
    conv = _load(pid) if re.fullmatch(r"[a-z0-9]{6,32}", str(pid)) else None
    return conv.get("state") if conv else None


def busy(pid: str) -> bool:
    with _META:
        return pid in _RUNNING or bool(_QUEUES.get(pid))


def wait_idle(pid: str, timeout: float = 60) -> bool:
    """Until the order's worker has nothing left (tests and tools). The watcher may still follow a generation."""
    end = time.time() + timeout
    while time.time() < end:
        if not busy(pid):
            return True
        time.sleep(0.05)
    return not busy(pid)


def start(photo: bytes | None = None, who: str = "anon", channel: str = "web", text: str | None = None,
          audio: bytes | None = None, title: str | None = None) -> str:
    """A new „… (Assistent)“ order from a photo and/or a description; returns its id at once (the room is measured and
    the text understood in the order's worker)."""
    text = _text(text)
    if photo is None and not text and not audio:
        raise HTTPException(400, "Bitte ein Foto des Raums und eine Beschreibung (Text oder Sprachnachricht) senden.")
    up = _store_photo(photo) if photo else None
    auto = None
    title = str(title or "").strip()[:200]
    if not title:
        # „TEST …“ as the first word of the description makes a test order (no Vorgangsnummer, purgeable), so the
        # owner can try Telegram, mail or the API without touching the numbering
        test = "TEST " if re.match(r"TEST\b", text or "") else ""
        title = auto = f"{test}Auftrag vom {datetime.now():%d.%m. %H:%M} (Assistent)"
    req = SimpleNamespace(state=SimpleNamespace(owner=who, owner_name=who))
    doc = projects.create_project("workshop", req, {"title": title})   # the web app's „Neuer Auftrag“ path
    pid = doc["id"]
    ops = [{"op": "set", "path": "judge.auto_retry", "value": True}]   # one corrected retry when the judge fails
    q = _quality()
    if q:
        ops.append({"op": "set", "path": "quality", "value": q})
    if up:
        ops += [{"op": "set", "path": "room", "value": up["path"]},
                {"op": "set", "path": "room_size", "value": [up["width"], up["height"]]}]
    projects.apply_ops(pid, doc["rev"], ops, who)
    with _lock(pid):
        _save(_new_conv(pid, who, channel, "analyzing" if up else "need_photo", auto))
    if up:
        _emit(pid, "user", "photo", "Raumfoto", image=up["path"])
    if text:
        _emit(pid, "user", "text", text)
    if audio:
        _emit(pid, "user", "audio", "Sprachnachricht")
    _enqueue(pid, {"kind": "input", "who": who, "channel": channel, "text": text, "audio": audio, "analyze": bool(up)})
    return pid


def handle(pid: str, who: str = "anon", channel: str = "web", text: str | None = None, audio: bytes | None = None,
           photo: bytes | None = None) -> list[dict]:
    """A message to an order's assistant. Returns the events appended now; the answer follows as agent events
    (subscribe / events). Works on any order: a web-app order gets a conversation on its first message."""
    doc = _order(pid)                     # Gone (404) for a purged or unknown order
    text = _text(text)
    if not text and not audio and photo is None:
        raise HTTPException(400, "Bitte einen Text, eine Sprachnachricht oder ein Foto senden.")
    up = _store_photo(photo) if photo is not None else None
    with _lock(pid):
        if _load(pid) is None:
            st = "understanding" if doc.get("room") else "need_photo"
            _save(_new_conv(pid, who, channel, st))
    out = []
    was_busy = busy(pid)
    if up:
        out.append(_emit(pid, "user", "photo", "Neues Raumfoto", image=up["path"]))
    if text:
        out.append(_emit(pid, "user", "text", text))
    if audio:
        out.append(_emit(pid, "user", "audio", "Sprachnachricht"))
    _enqueue(pid, {"kind": "input", "who": who, "channel": channel, "text": text, "audio": audio, "photo": up})
    if was_busy:
        out.append(_status(pid, "Notiert – ich antworte, sobald der vorige Schritt fertig ist."))
    return out


def file_bytes(ref):
    """Channels: every file the assistant names lives under outputs/, so the default reader handles it."""
    return None


def recover() -> dict:
    """At server start: follow generations again, unstick conversations a restart interrupted, delete conversations of
    purged orders."""
    out = {"watching": [], "interrupted": [], "removed": []}
    d = _dir()
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        pid = f.stem
        conv = _load(pid)
        if conv is None:
            continue
        try:
            if not projects.path_of("workshop", pid).exists():
                _forget(pid)
                out["removed"].append(pid)
                continue
        except HTTPException:
            continue
        if conv.get("state") == "generating" and conv.get("jobs"):
            _watch(pid)
            out["watching"].append(pid)
        elif conv.get("state") in BUSY_STATES:
            back = conv.get("stable") if conv.get("stable") not in BUSY_STATES else "error"
            _error(pid, "Der Werkstatt-Rechner wurde neu gestartet, während ich gearbeitet habe. Bitte die letzte "
                        "Nachricht noch einmal schicken.", state=back)
            out["interrupted"].append(pid)
            if back == "generating" and conv.get("jobs"):     # interrupted while a generation was running
                _watch(pid)
                out["watching"].append(pid)
    return out


def stop() -> None:
    _WATCHER["stop"].set()
    t = _WATCHER["thread"]
    if t is not None:
        t.join(timeout=WATCH_S + 2)
    _WATCHER["thread"] = None


def _forget(pid: str) -> None:
    with _lock(pid):
        _path(pid).unlink(missing_ok=True)
    _WATCH.discard(pid)


# ---------------- hooks (tests replace these) ----------------

def _intent():
    import intent
    return intent


def _profile():
    try:
        import profile as company_profile   # profile.py shadows the stdlib module on purpose
        return company_profile if hasattr(company_profile, "defaults_for") else None
    except Exception:
        return None


def _quality() -> str | None:
    prof = _profile()
    try:
        q = prof.effective()["quality"] if prof else None
    except Exception:
        traceback.print_exc()
        q = None
    return q if q in workshop.QUALITY else None


def _understand(text: str, doc: dict) -> dict:
    return _intent().understand(text, doc, _profile())


def _transcribe(audio: bytes) -> dict:
    import voice
    return voice.transcribe(audio, "de")


def _locate(photo, phrase: str, geo, doc) -> dict:
    import ground
    return workshop.EXCLUSIVE(lambda: ground.locate(photo, phrase, geo, doc))


def _find_object(photo, phrase: str, refine: bool = True) -> dict:
    import ground
    return workshop.EXCLUSIVE(lambda: ground.find_object(photo, phrase, refine_with_sam=refine))


def _measure(room: str) -> dict:
    import geometry
    import geometry_api
    return geometry.load_cached(room, geometry_api.GEO_DIR) or geometry_api._analyze(room, False)


def _calibrate(room: str, height: float) -> dict:
    import geometry_api
    r = geometry_api.calibrate({"room": room, "room_height_cm": height})
    if r.get("conflict"):   # the carpenter said it explicitly: the new height wins over an older reference
        r = geometry_api.calibrate({"room": room, "room_height_cm": height, "on_conflict": "replace"})
    return r


def _check_plan(pid: str) -> dict:
    return workshop.check_plan(pid)


def _generate(pid: str, req: dict, who: str) -> dict:
    """The web app's „Berechnen“ (validation, guide image, job queue, pending_jobs, variant record)."""
    return workshop.generate(pid, req, SimpleNamespace(state=SimpleNamespace(owner=who, owner_name=who)))


def _sched():
    import jobs
    return jobs.scheduler


def _job_view(job_id: str) -> dict | None:
    s = _sched()
    return s.get(job_id) if s is not None else None


def _cancel_job(job_id: str, who: str) -> None:
    s = _sched()
    if s is not None:
        s.cancel(job_id, by=who, by_name="Assistent")


def _gpu_busy() -> bool:
    """A FLUX job runs or the engine lock is held (e.g. the model preload after a restart): grounding will wait."""
    s = _sched()
    try:
        lock = getattr(getattr(s, "engine", None), "lock", None)
        held = bool(lock is not None and callable(getattr(lock, "locked", None)) and lock.locked())
        return bool(s and (s.status()["busy"] or held))
    except Exception:
        return False


def _judge_available() -> bool:
    try:
        import judge
        return bool(judge.status(force=True).get("available"))
    except Exception:
        return False


# ---------------- worker ----------------

def _enqueue(pid: str, item: dict) -> None:
    with _META:
        _QUEUES.setdefault(pid, deque()).append(item)
        if pid in _RUNNING:
            return
        _RUNNING.add(pid)
    threading.Thread(target=_run, args=(pid,), daemon=True, name=f"agent-{pid}").start()


def _run(pid: str) -> None:
    while True:
        with _META:
            q = _QUEUES.get(pid)
            if not q:
                _RUNNING.discard(pid)
                _QUEUES.pop(pid, None)
                return
            item = q.popleft()
        try:
            if item["kind"] == "input":
                _on_input(pid, item)
            elif item["kind"] == "finished":
                _on_finished(pid, item.get("who") or "system")
        except projects.Gone:
            _forget(pid)
        except Exception:
            traceback.print_exc()
            try:
                conv = _load(pid) or {}
                back = conv.get("stable") if conv.get("stable") not in BUSY_STATES else "error"
                _error(pid, "Da ist bei mir etwas schiefgelaufen. Bitte noch einmal versuchen – im Werkstatt-Programm "
                            "ist der Auftrag ganz normal weiter bearbeitbar.", state=back or "error")
            except Exception:
                traceback.print_exc()


def _on_input(pid: str, item: dict) -> None:
    who = item.get("who") or "anon"
    up = item.get("photo")
    if up and (_load(pid) or {}).get("state") == "generating":   # the running images would show the old photo
        _stop(pid, who, quiet=True)
        _status(pid, "Die laufende Berechnung habe ich abgebrochen, weil ein neues Foto kam.")
    if up:
        _new_photo(pid, up, who)
    if up or item.get("analyze"):
        _analyze(pid, who)
    text = item.get("text") or ""
    if item.get("audio"):
        _state(pid, "understanding")
        tr = _transcribe(item["audio"])
        heard = (tr.get("text") or "").strip() if tr.get("status") == "ok" else ""
        if heard:
            _say(pid, f"Verstanden: „{heard}“")
            text = f"{text} {heard}".strip()
        elif tr.get("status") == "ok":
            _error(pid, "In der Sprachnachricht war nichts zu verstehen. Bitte noch einmal sprechen oder schreiben.",
                   state=(_load(pid) or {}).get("stable"))
        else:
            _error(pid, (tr.get("message") or "Die Spracherkennung ist gerade nicht verfügbar.") +
                   " Bitte die Nachricht als Text schicken.", state=(_load(pid) or {}).get("stable"))
    doc = projects.load("workshop", pid)
    conv = _load(pid)
    if not doc.get("room"):
        if text:
            _update(pid, lambda c: c["queued"].append(text))
        _ask(pid, ASK_PHOTO, {"field": "photo"})
        _state(pid, "need_photo")
        return
    if conv.get("queued") and (up or item.get("analyze")):
        text = " ".join(conv["queued"] + ([text] if text else []))
        _update(pid, lambda c: c.__setitem__("queued", []))
    if text:
        _on_text(pid, text, who)
    elif up or item.get("analyze"):
        if _live(doc) or conv.get("todo"):
            _advance(pid, who)
        else:
            _ask(pid, ASK_WHAT, {"field": "type"})


def _new_photo(pid: str, up: dict, who: str) -> None:
    """Like step ①'s newRoomOps: the old measurement, calibration, removals and every placement are void; pieces with a
    location phrase are placed again on the new photo."""
    doc = projects.load("workshop", pid)
    none = {"mode": "none", "pose": None, "box": None}
    ops = [{"op": "set", "path": "room", "value": up["path"]},
           {"op": "set", "path": "room_size", "value": [up["width"], up["height"]]},
           {"op": "set", "path": "geometry", "value": None}, {"op": "set", "path": "calibration", "value": None},
           {"op": "set", "path": "removes", "value": []}]
    ops += [{"op": "set", "path": f"pieces.{p['id']}.placement", "value": none} for p in doc["pieces"]]
    _apply(pid, ops, who)
    todo = [p["id"] for p in _live(doc)]
    _update(pid, lambda c: c.update(todo=todo, preview=None, preview_rev=None))


def _analyze(pid: str, who: str) -> None:
    doc = projects.load("workshop", pid)
    if not doc.get("room") or (doc.get("geometry") or {}).get("room") == doc["room"]:
        return
    _state(pid, "analyzing")
    _status(pid, "Foto erhalten – ich vermesse den Raum …")
    g = _measure(doc["room"])
    if not isinstance(g, dict):
        return
    ops = [{"op": "set", "path": "geometry", "value": g}] + _reproject_ops({**doc, "geometry": g})
    _apply(pid, ops, who)
    if not g.get("ok"):
        _say(pid, f"Den Raum konnte ich nicht vermessen ({g.get('error') or 'unbekannter Grund'}). Ich plane die Möbel "
                  "dann nach der Beschreibung – genaue Positionen kann man im Werkstatt-Programm setzen.")


# ---------------- understanding ----------------

def _words(text: str) -> list[str]:
    return re.findall(r"[a-zäöüß0-9]+", (text or "").lower())


YES_STRONG = {"ja", "jo", "jap", "jep", "ok", "okay", "passt", "genau", "los", "rechnen", "jawohl", "einverstanden",
              "perfekt", "super", "prima", "top", "gut", "mach", "machen", "richtig", "stimmt", "klar", "gerne", "gern",
              "standard", "übernehmen", "vorschlag"}
YES_SOFT = {"so", "bitte", "mal", "das", "ist", "sieht", "aus", "alles", "danke", "schön", "sehr", "fein", "dann",
            "rechne", "es", "ganz", "gut", "jetzt", "der", "den", "die", "nimm", "nehmen", "wie", "vorgeschlagen",
            "normal", "auch", "reicht", "trotzdem", "dennoch"}


def _is_yes(text: str) -> bool:
    w = _words(text)
    return bool(w) and len(w) <= 6 and any(x in YES_STRONG for x in w) and all(x in YES_STRONG | YES_SOFT for x in w)


def _command(text: str) -> str | None:
    t = " ".join(_words(text))
    n = len(t.split())
    if not t:
        return None
    if n <= 3 and re.fullmatch(r"(hilfe|help|was kannst du|wie geht das)", t):
        return "help"
    if n <= 4 and re.search(r"\b(stopp|stop|abbrechen|abbruch|halt)\b", t):
        return "stop"
    if n <= 3 and re.fullmatch(r"(status|wie weit|stand|wie lange noch|und)", t):
        return "status"
    if n <= 5 and re.search(r"\b(nochmal|nochmals|noch mal|noch einmal|neu rechnen|neue bilder|neue vorschläge|wiederholen)\b", t):
        return "again"
    if n <= 5 and re.search(r"\b(senden|schicken|verschicken|kundenversion|an den kunden)\b", t):
        return "send"
    if n <= 2 and re.fullmatch(r"(nein|ne|nee|nö|falsch)", t):
        return "no"
    if n <= 4 and re.search(r"\b(in fein|feine qualität|hohe qualität|fein rechnen|qualität fein|für den kunden)\b", t):
        return "fein"
    if n <= 4 and re.search(r"\b(als entwurf|entwurf|schneller entwurf|nur entwurf)\b", t):
        return "entwurf"
    if _is_yes(text):
        return "yes"
    return None


_MOVE = re.compile(r"^(?:(?:bitte\s+)?(?:schieb|schiebe|rück|rücke|setz|setze|stell|stelle|verschieb|verschiebe)\s+(?:bitte\s+)?)?"
                   r"(?:(?:den|das|die)\s+)?(?:(?P<what>[a-zäöüß]+)\s+)?(?:(?:noch\s+)?(?P<cm>\d+)\s*cm\s+)?"
                   r"(?P<bit>etwas\s+|ein\s+bisschen\s+|ein\s+stück\s+|ein\s+wenig\s+)?(?:noch\s+)?(?:weiter\s+|mehr\s+)?"
                   r"(?:nach\s+)?(?P<dir>links|rechts)(?:\s+(?:schieben|rücken|setzen|verschieben|bitte))?$", re.I)
_HEIGHT = re.compile(r"\b(?:raumhöhe|deckenhöhe|die decke ist|decke)\s*(?:ist|=|:|von|beträgt)?\s*(?:ca\.?\s*)?(\d{3})\s*cm\b",
                     re.I)


def _on_text(pid: str, text: str, who: str) -> None:
    conv = _load(pid)
    st = conv.get("state")
    cmd = _command(text)
    if cmd == "help":
        _say(pid, HELP)
        return
    if cmd == "status":
        _say(pid, _status_text(pid))
        return
    if cmd == "stop":
        _stop(pid, who)
        return
    if st == "preview" and conv.get("conflict") and _FIX_RX.fullmatch(" ".join(_words(text))):
        if conv["conflict"].get("cmd") == "slope_fit":
            _slope_fit(pid, who, conv["conflict"]["piece"])
        else:
            _understand_text(pid, conv["conflict"]["send"], who)    # the plan check's suggestion, as if it had been said
        return
    said = " ".join(_words(text))
    if st in ("preview", "done", "asking") and _SLOPE_FIT.search(said):
        _slope_fit(pid, who)
        return
    if st in ("preview", "done") and _SLOPE_LOWER.fullmatch(said) and _slope_target(pid, projects.load("workshop", pid)):
        _slope_lower(pid, who)
        return
    if st in ("preview", "done") and _SLOPE_MOVE.fullmatch(said):
        _slope_move(pid, who)
        return
    if st == "generating" and cmd in ("yes", "again", "send", "no"):
        _say(pid, "Die Bilder werden gerade gerechnet – ich melde mich, sobald sie fertig und geprüft sind. "
                  "(„stopp“ bricht ab.)")
        return
    if conv.get("questions") and cmd in (None, "yes", "no") and _answer(pid, text, cmd, who):
        return
    if cmd == "yes":
        if st == "preview":
            _go(pid, who)
        elif st == "done":
            _say(pid, "Prima. „senden“ bereitet eine Kundenversion vor, „nochmal“ rechnet neue Bilder – oder sag, was "
                      "anders soll.")
        elif _live(projects.load("workshop", pid)):
            _advance(pid, who)
        else:
            _ask(pid, ASK_WHAT, {"field": "type"})
        return
    if cmd == "no":
        _say(pid, "Was soll anders werden? Zum Beispiel „20 cm breiter“, „nimm Eiche“, „weiter links“ oder „der Stuhl "
                  "soll auch weg“.")
        return
    if cmd == "again":
        if st in ("done", "preview", "error", "asking") and _live(projects.load("workshop", pid)):
            _go(pid, who, again=True)
        else:
            _ask(pid, ASK_WHAT, {"field": "type"})
        return
    if cmd == "send":
        _send(pid, who)
        return
    if cmd in ("fein", "entwurf"):
        _apply(pid, [{"op": "set", "path": "quality", "value": cmd}], who)
        _say(pid, "Qualität „Fein (für den Kunden)“ – dauert etwa doppelt so lange." if cmd == "fein" else
             "Qualität „Entwurf (schneller)“.")
        if st == "preview":
            _update(pid, lambda c: c.__setitem__("preview_rev", projects.load("workshop", pid)["rev"]))
        return
    norm = _intent().normalize_de(text)
    m = _HEIGHT.search(norm)
    if m and len(_words(norm)) <= 6:
        _set_height(pid, int(m.group(1)), who)
        return
    m = _MOVE.match(norm.strip(" .!"))
    if m and st in ("preview", "done", "asking", "generating"):
        if _move(pid, m, who):
            return
    _understand_text(pid, text, who)


def _status_text(pid: str) -> str:
    conv, doc = _load(pid), projects.load("workshop", pid)
    st = conv.get("state")
    if st == "generating":
        parts = []
        for jid in conv.get("jobs") or []:
            v = _job_view(jid)
            if v and v.get("stage_text"):
                parts.append(v["stage_text"] + (f" ({v['eta_text']})" if v.get("eta_text") else ""))
        return "Berechnung läuft: " + ("; ".join(parts) if parts else "bitte etwas Geduld.")
    lines = [f"Stand: {STATE_DE.get(st, st)}."]
    lines += [f"{k}. {_piece_line(p)}" for k, p in enumerate(_live(doc), 1)]
    return "\n".join(lines)


STATE_DE = {"need_photo": "warte auf ein Raumfoto", "analyzing": "Raum wird vermessen", "understanding": "ich lese mich ein",
            "asking": "warte auf eine Antwort", "preview": "Vorschau wartet auf „ja“", "generating": "Bilder werden gerechnet",
            "done": "fertig", "error": "Fehler – bitte erneut versuchen"}


def _focus_doc(conv: dict, doc: dict) -> dict:
    """The order as intent.understand sees it: a bare correction („breiter“) means the piece we talked about last."""
    live = {p["id"] for p in _live(doc)}
    return {**doc, "active_piece": conv["focus"]} if conv.get("focus") in live else doc


def _understand_text(pid: str, text: str, who: str) -> None:
    conv = _load(pid)
    was = conv.get("state")
    _state(pid, "understanding")
    doc = projects.load("workshop", pid)
    res = _understand(text, _focus_doc(conv, doc))
    if res.get("status") == "unavailable" and _wait_for_model():
        doc = projects.load("workshop", pid)
        res = _understand(text, _focus_doc(_load(pid), doc))
    back = was if was not in BUSY_STATES else (_load(pid).get("stable") or "error")
    if res.get("status") == "unavailable":
        _error(pid, "Die KI zum Verstehen ist gerade nicht erreichbar. Bitte in ein paar Minuten noch einmal schicken – "
                    "oder im Werkstatt-Programm weiterarbeiten.", state=back)
        return
    if res.get("status") != "ok":
        _error(pid, "Das habe ich nicht verstanden. Bitte anders formulieren, z. B. „ein Sideboard in Eiche, "
                    "1,80 breit, unter dem Fenster“.", state=back)
        return
    ops = list(res.get("ops") or [])
    removes = res.get("removes") or []
    questions = res.get("questions") or []
    if not ops and not removes:
        q = next((x for x in questions if x.get("question_de")), None)
        if conv.get("questions"):        # an answer to the open question that was not understood: ask it again
            _ask(pid, "Das habe ich nicht verstanden. " + _question_text(conv["questions"][0]),
                 {"field": conv["questions"][0].get("field"), "piece": conv["questions"][0].get("piece"),
                  "default": conv["questions"][0].get("default")})
            return
        if was == "generating":
            _say(pid, "Das habe ich nicht als Änderung verstanden – die Berechnung läuft weiter.", state="generating")
        else:
            _ask(pid, (q or {}).get("question_de") or ASK_WHAT, {"field": "type"})
        return
    if was == "generating":   # the plan changes: the running images would show the old one
        _stop(pid, who, quiet=True)
        _status(pid, "Die laufende Berechnung habe ich abgebrochen, weil sich der Plan ändert.")
    before = doc
    if ops:
        doc = _apply(pid, ops, who)
        _learn(before, doc, who, ops, res)
        # a shelf or table has no fronts: a default „grifflos (Push)“ would end up in the prompt as „push-to-open fronts“
        spoken = _intent().spoken(_intent().normalize_de(text), "handles") if text else None
        bare = [{"op": "set", "path": f"pieces.{p['id']}.handles", "value": None} for p in doc["pieces"]
                if p.get("type") and p["type"] not in _FRONTS and p.get("handles") and not spoken
                and any(r.get("id") == p["id"] for r in res.get("pieces") or [])]
        if bare:
            doc = _apply(pid, bare, who)
        sync = _reproject_ops(doc)        # new dims: boxes follow, wall pieces re-snap (their depth may differ)
        if sync:
            doc = _apply(pid, sync, who)
    _retitle(pid, doc, who)
    pieces = res.get("pieces") or []
    old_where = {p["id"]: (p.get("where") or "").strip() for p in before["pieces"]}
    old_ids = {p["id"] for p in _live(before)}
    todo = []
    for p in pieces:
        pid_ = p.get("id")
        if not pid_:
            continue
        cur = next((x for x in doc["pieces"] if x["id"] == pid_), None)
        if cur is None:
            continue
        placed = (cur.get("placement") or {}).get("mode") in ("2d", "3d")
        if pid_ not in old_ids or (cur.get("where") or "").strip() != old_where.get(pid_, "") or not placed:
            todo.append(pid_)
    focus = next((p.get("id") for p in reversed(pieces) if p.get("id")), conv.get("focus"))
    qs = []
    for q in questions:
        idx = q.get("piece")
        piece_id = pieces[idx]["id"] if isinstance(idx, int) and 0 <= idx < len(pieces) else None
        qs.append({"piece": piece_id, "field": q.get("field"), "question_de": q.get("question_de"),
                   "default": q.get("default")})
    summary = res.get("summary_de") or "Änderung übernommen."
    notes = list(res.get("warnings") or [])

    def fn(c):
        c["todo"] = list(dict.fromkeys((c.get("todo") or []) + todo))
        c["questions"] = qs
        c["focus"] = focus
    _update(pid, fn)
    _say(pid, "Verstanden: " + summary + ("".join(f"\nHinweis: {w}" for w in notes)))
    if removes:
        _find_removes(pid, removes, who)
    _advance(pid, who)


def _wait_for_model() -> bool:
    """One pause for a restarting language model; True when it answers again."""
    for _ in range(max(1, INTENT_RETRY_S // 5)):
        time.sleep(min(5, INTENT_RETRY_S))
        if _judge_available():
            return True
    return False


def _learn(before: dict, after: dict, who: str, ops: list, res: dict) -> None:
    """The company profile learns from corrections of existing pieces (as the PATCH hook does), never from first fills."""
    refs = {p.get("ref") for p in res.get("pieces") or [] if p.get("ref")}
    mine = [o for o in ops if o.get("op") == "set" and str(o.get("path", "")).split(".")[1:2] and
            str(o["path"]).split(".")[1] in refs]
    if not mine:
        return
    prof = _profile()
    if prof is None or not hasattr(prof, "learn_doc"):
        return
    try:
        prof.learn_doc(before, after, who, mine)
    except Exception:
        traceback.print_exc()


def _retitle(pid: str, doc: dict, who: str) -> None:
    """An automatic title („Auftrag vom …“) becomes „Einbauschrank + Sideboard (Assistent)“ once the pieces are known."""
    conv = _load(pid)
    auto = conv.get("auto_title")
    if not auto or doc.get("title") != auto:
        return
    labels = [workshop.labels(p)["label"] for p in _live(doc) if p.get("type")]
    if not labels:
        return
    title = (("TEST " if projects.is_test(auto) else "") + " + ".join(dict.fromkeys(labels)) + " (Assistent)")[:200]
    _apply(pid, [{"op": "set", "path": "title", "value": title}], who)
    _update(pid, lambda c: c.__setitem__("auto_title", title))


def _answer(pid: str, text: str, cmd: str | None, who: str) -> bool:
    """The answer to the open question. False when the text is something else (then it is understood as usual)."""
    conv = _load(pid)
    q = conv["questions"][0]
    doc = projects.load("workshop", pid)
    piece = next((p for p in doc["pieces"] if p["id"] == q.get("piece")), None)
    field = q.get("field")
    if piece is None or field not in ("w", "h", "d", "where"):
        _update(pid, lambda c: c["questions"].pop(0) if c["questions"] else None)
        return False
    value = None
    if field == "where":
        if cmd == "yes" or re.fullmatch(r"(egal|du|frei|such dir was aus|wo es passt|irgendwo)", " ".join(_words(text))):
            value = ""          # the generation picks the spot
        elif cmd is None and len(_words(text)) <= 14 and set(_words(text)) & _WHERE_WORDS:
            value = text.strip()
    elif cmd == "yes" and q.get("default"):
        value = int(q["default"])
    else:
        value = _value(text)
    if value is None:
        return False
    _update(pid, lambda c: c["questions"].pop(0))
    if field == "where":
        if value:
            doc = _apply(pid, [{"op": "set", "path": f"pieces.{piece['id']}.where", "value": value}], who)
            _update(pid, lambda c: c.__setitem__("todo", list(dict.fromkeys((c.get("todo") or []) + [piece["id"]]))))
        else:
            _update(pid, lambda c: c.__setitem__("skip", list(dict.fromkeys((c.get("skip") or []) + [piece["id"]]))))
            _say(pid, f"Gut – den Platz für {_name(piece, 'acc')} wählt die Bildberechnung selbst.")
    else:
        doc = _apply(pid, [{"op": "set", "path": f"pieces.{piece['id']}.{field}", "value": value}], who)
        _say(pid, f"{_FIELD_DE[field]}: {value} cm.")
    _advance(pid, who)
    return True


_FIELD_DE = {"w": "Breite", "h": "Höhe", "d": "Tiefe"}
_WHERE_WORDS = {"links", "rechts", "neben", "unter", "über", "an", "am", "in", "im", "vor", "hinter", "wand", "ecke",
                "fenster", "tür", "mitte", "wo", "statt", "anstelle", "stelle", "zwischen", "bei", "gegenüber", "hinten",
                "vorne", "dort", "da", "nische", "rückwand", "seite", "heizung", "sofa", "boden", "decke"}
_FILLER = {"breit", "tief", "hoch", "lang", "ca", "etwa", "circa", "ungefähr", "so", "cm", "die", "der", "das", "breite",
           "tiefe", "höhe", "länge", "bitte", "sagen", "wir", "mal", "ist", "sind", "soll", "werden", "es", "er", "sie",
           "insgesamt", "genau", "ganz", "auf", "von"}


def _value(text: str) -> int | None:
    """A bare size answer („zwei zwanzig“, „1,80“, „220“) in cm, else None."""
    n = _intent().normalize_de(text)
    nums = re.findall(r"\d+(?:[.,]\d+)?", n)
    rest = [w for w in _words(re.sub(r"\d+(?:[.,]\d+)?", " ", n)) if w not in _FILLER]
    if len(nums) != 1 or rest:
        return None
    v = float(nums[0].replace(",", "."))
    if v < 10 and ("," in nums[0] or "." in nums[0]):
        v *= 100            # „2,2“ → metres
    v = int(round(v))
    return v if 10 <= v <= 2000 else None


# ---------------- grounding and placement ----------------

def _live(doc: dict) -> list[dict]:
    return [p for p in doc.get("pieces") or [] if p.get("type") or (p.get("text") or "").strip()]


def _photo(doc: dict):
    return workshop._open(doc.get("room"))


_GENDER = {"Einbauschrank": "m", "Esstisch": "m", "Hängeschrank": "m", "Küche": "f", "Regalwand": "f", "Garderobe": "f",
           "Sitzbank": "f", "Wandverkleidung": "f", "Treppe": "f", "Tür": "f", "Badmöbel": "p"}


def _name(p: dict, case: str = "nom") -> str:
    """„der Einbauschrank“ / „den Einbauschrank“ / „dem Einbauschrank“ (case "nom" / "acc" / "dat")."""
    lab = workshop.labels(p)["label"]
    g = _GENDER.get(lab, "n")
    art = {"nom": {"m": "der", "f": "die", "n": "das", "p": "die"}, "acc": {"m": "den", "f": "die", "n": "das", "p": "die"},
           "dat": {"m": "dem", "f": "der", "n": "dem", "p": "den"}}[case][g]
    return f"{art} {lab}"


def _find_removes(pid: str, removes: list, who: str) -> None:
    doc = projects.load("workshop", pid)
    photo = _photo(doc)
    if photo is None:
        return
    found, missing, down = [], [], False
    _status(pid, "Ich suche im Foto, was entfernt werden soll …" + (
        " Der Rechner ist gerade beschäftigt – das kann etwas dauern." if _gpu_busy() else ""))
    for r in removes:
        what = str(r.get("what") or "").strip()
        if not what:
            continue
        res = _find_object(photo, _removal_phrase(what))
        if res.get("status") == "ok" and res.get("parts"):
            found.append((what, res["parts"]))
        elif res.get("status") == "unavailable":
            down = True
            missing.append(what)
        else:
            missing.append(what)
    if found:
        cur = list(doc.get("removes") or [])
        for _, parts in found:
            cur += [p for p in parts if isinstance(p, dict)]
        _apply(pid, [{"op": "set", "path": "removes", "value": cur[:20]}], who)
        _update(pid, lambda c: c.__setitem__("removed", list(dict.fromkeys((c.get("removed") or []) + [w for w, _ in found]))))
    if missing:
        why = "die Bild-KI ist gerade nicht erreichbar" if down else "ich habe es im Foto nicht sicher gefunden"
        _say(pid, f"„{', '.join(missing)}“ habe ich nicht markiert ({why}). Im Werkstatt-Programm unter Raum → "
                  "„Etwas entfernen“ kann man es antippen.")


_CARRIER = re.compile(r"(schrank|kommode|regal|tisch|sideboard|board|anrichte|bank|vitrine|truhe|möbel|ablage|konsole)",
                      re.I)


def _removal_phrase(what: str) -> str:
    """A cabinet that goes takes what stands on it along (TV, lamp, decoration): otherwise SAM cuts out only the cabinet,
    the protected photo keeps the TV's top edge, and a ghost strip stays above the new piece (seen live, 2026-10-18)."""
    if _CARRIER.search(what) and not re.search(r"\b(mit|samt|inklusive|und)\b", what, re.I):
        return f"{what} mit allem, was darauf steht"
    return what


_HERE_RX = re.compile(r"\b(an (seine|ihre|dessen|deren|die|der) stelle|stattdessen|dort|da wo|an der gleichen stelle|"
                      r"an derselben stelle|anstelle|statt)\b", re.I)


def _where_phrase(where: str, conv: dict) -> str:
    """„an seine Stelle“ after „der alte Fernsehschrank kommt weg“ → „wo der alte Fernsehschrank steht“, which
    ground.locate snaps onto the marked removal."""
    removed = conv.get("removed") or []
    if removed and _HERE_RX.search(where):
        return f"wo {removed[-1]} steht"
    return where


def _place(pid: str, piece_id: str, who: str) -> dict | None:
    """Places one piece from its location phrase. None = done (placed, or nothing to do); else a question dict."""
    import place
    doc = projects.load("workshop", pid)
    piece = next((p for p in doc["pieces"] if p["id"] == piece_id), None)
    if piece is None or not _live({"pieces": [piece]}):
        return None
    conv = _load(pid)
    where = (piece.get("where") or "").strip()
    if not place.scene_for(doc):
        return None                       # no measurement: the prompt carries the location phrase
    if not where:
        if piece_id in (conv.get("skip") or []):
            return None
        return {"piece": piece_id, "field": "where", "default": None,
                "question_de": f"Wo soll {_name(piece)} hin? (z. B. „links an der Wand“, „unter dem Fenster“ – oder "
                               "„egal“, dann wählt die Bildberechnung den Platz)"}
    photo = _photo(doc)
    phrase = _where_phrase(where, conv)
    res = _locate(photo, phrase, doc.get("geometry"), doc)
    st = res.get("status")
    if st == "unavailable":
        _note(pid, "Die Bild-KI zum Platzieren ist gerade nicht erreichbar – die Möbel werden nach Beschreibung "
                   "platziert.")
        return None
    pt = res.get("point")
    if st != "ok" or not pt or (res.get("confidence") or 0) < ASK_CONF:
        return {"piece": piece_id, "field": "where", "default": None,
                "question_de": f"„{where}“ habe ich im Foto nicht sicher gefunden. Wo genau soll {_name(piece)} hin? "
                               "(z. B. „an der Rückwand rechts“ – oder „egal“)"}
    grid = [pt[1], pt[0]]                  # ground: [x, y]; place: [top, left]
    scene = place.scene_for(doc)
    u, v = place.from_grid(grid, scene.size)
    pl = _anchor(scene, doc, piece, u, v, res.get("wall"))
    for wall in (() if pl else (True, None, False) if res.get("wall") else (None, False)):
        pl = place.placement_for(doc, piece, grid, against_wall=wall)
        b = (pl or {}).get("box")
        if b and (b[2] - b[0]) * (b[3] - b[1]) > 850_000:
            pl = None                     # fills the whole photo: the piece would stand right in front of the camera
        if pl:
            break
    if not pl:
        return {"piece": piece_id, "field": "where", "default": None,
                "question_de": f"An der Stelle „{where}“ passt {_name(piece)} nicht ins Bild. Bitte eine andere Stelle nennen (oder „egal“)."}
    ref = _beside(where)
    if ref and pl.get("mode") == "3d" and pl.get("pose", {}).get("wall"):
        pl = _clear_of(pid, scene, doc, piece, pl, photo, *ref)
    if pl.get("elev") is not None and piece.get("elev"):
        # a tap hangs the piece at the height the ray meets the wall; a spoken or default height (Unterkante) wins,
        # the phrase only says where along the wall
        pl = {**place.reprojected(place.scene_for(doc), piece, {"mode": "3d", "pose": pl["pose"], "box": None}),
              "elev": piece["elev"]}
        if not pl.get("box"):
            pl = None
    if not pl:
        return {"piece": piece_id, "field": "where", "default": None,
                "question_de": f"An der Stelle „{where}“ passt {_name(piece)} nicht ins Bild. Bitte eine andere Stelle nennen (oder „egal“)."}
    _apply(pid, place.tap_ops(doc, piece, pl) + list(pl.get("moves") or []), who)

    def fn(c):
        c.setdefault("spots", {})[piece_id] = {"why": res.get("why") or where, "confidence": res.get("confidence")}
    _update(pid, fn)
    return None


def _anchor(scene, doc: dict, piece: dict, u: float, v: float, wall_id: str | None) -> dict | None:
    """Placement from a grounded point by its spot on the wall or floor, not by the tap rule (the middle of the box on
    the point): the VLM points at the place („rechts neben dem Fernsehschrank“), usually where wall and floor meet, and
    has no idea how tall the piece is. Against a wall: the piece stands (or hangs at its elev) with its back on that
    wall, centred at the point along the wall, shifted to stay within the wall and beside pieces already on it."""
    import place
    W, H, D = scene.dims(piece)
    wall_hung = bool(piece.get("elev")) or bool(place._wall_hung(piece, workshop.CATALOG))
    elev = piece.get("elev") or (place._wall_hung(piece, workshop.CATALOG) if wall_hung else 0)
    shown = {**piece, "elev": elev} if elev else piece
    fp, hit = scene.floor_point(u, v), scene.wall_hit(u, v)
    w, p, moves = None, None, []
    if wall_id:
        w = scene.wall_by_id(wall_id)
        if w is not None:
            p = (hit["x"], hit["z"]) if hit and hit["wall"]["id"] == wall_id else (fp["x"], fp["z"]) if fp else None
    if p is None and fp:
        nw = scene.nearest_wall(fp["x"], fp["z"])
        if nw and abs(nw["dist"]) - D / 2 < 60:
            w, p = nw["wall"], (fp["x"], fp["z"])
        elif not wall_hung:
            pose = {"x": fp["x"], "z": fp["z"], "yaw": 0, "wall": None}
            pl = place.reprojected(scene, shown, {"mode": "3d", "pose": pose, "box": None})
            return {**pl, **({"elev": elev} if elev else {})} if pl.get("box") else None
    if p is None and hit:
        w, p = hit["wall"], (hit["x"], hit["z"])
    if w is None or p is None:
        return None
    n = w["normal"]
    dist = n[0] * p[0] + n[1] * p[1] + w["offset"]
    on = [p[0] - dist * n[0], p[1] - dist * n[1]]             # the point on the wall line
    if w.get("from") and w.get("to"):
        a, b = w["from"], w["to"]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L > 1:
            t = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
            s = (on[0] - a[0]) * t[0] + (on[1] - a[1]) * t[1]
            s = min(max(s, W / 2 + 2), L - W / 2 - 2) if L >= W + 4 else L / 2
            for o in _live(doc):                               # do not stand inside a piece already on this wall
                pose = (o.get("placement") or {}).get("pose") or {}
                if o["id"] == piece["id"] or pose.get("wall") != w["id"] or (o.get("placement") or {}).get("mode") != "3d":
                    continue
                if (o.get("elev") or 0) >= elev + H or elev >= (o.get("elev") or 0) + scene.dims(o)[1]:
                    continue                                   # one hangs above the other
                so = (pose["x"] - a[0]) * t[0] + (pose["z"] - a[1]) * t[1]
                wo = scene.dims(o)[0]
                half = (wo + W) / 2 + 1
                if abs(s - so) >= half:
                    continue
                right, left = so + half, so - half
                first, second = (right, left) if s >= so else (left, right)
                if W / 2 <= first <= L - W / 2:
                    s = first
                elif W / 2 <= second <= L - W / 2:
                    s = second
                elif W + wo + 2 <= L:                          # neither side alone: the neighbour makes room too
                    if s < so:
                        so2 = min(L - wo / 2, max(so, s + half))
                        s = max(W / 2, min(s, so2 - half))
                        so2 = max(so2, s + half)
                    else:
                        so2 = max(wo / 2, min(so, s - half))
                        s = min(L - W / 2, max(s, so2 + half))
                        so2 = min(so2, s - half)
                    q = {**pose, "x": pose["x"] + (so2 - so) * t[0], "z": pose["z"] + (so2 - so) * t[1]}
                    nxt = place.reprojected(scene, o, {"mode": "3d", "pose": q, "box": None})
                    if nxt.get("box"):
                        moves.append({"op": "set", "path": f"pieces.{o['id']}.placement", "value": nxt})
            on = [a[0] + s * t[0], a[1] + s * t[1]]
    pose = {"x": on[0] + n[0] * D / 2, "z": on[1] + n[1] * D / 2, "yaw": scene.wall_yaw(w), "wall": w["id"]}
    pl = place.reprojected(scene, shown, {"mode": "3d", "pose": pose, "box": None})
    if not pl.get("box"):
        return None
    return {**pl, **({"elev": int(elev)} if elev and elev != (piece.get("elev") or 0) else {}), "moves": moves}


def _wall_axis(w: dict):
    """(start, unit tangent, length) of a wall with a measured extent, else None."""
    if not w or not w.get("from") or not w.get("to"):
        return None
    a, b = w["from"], w["to"]
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    return (a, ((b[0] - a[0]) / L, (b[1] - a[1]) / L), L) if L > 1 else None


def _on_wall(scene, doc: dict, wall_id: str, skip: str):
    """Pieces standing (or hanging) on one wall: [(piece, s along the wall, width, bottom, top)]."""
    w = scene.wall_by_id(wall_id)
    ax = _wall_axis(w)
    out = []
    for o in _live(doc):
        pose = (o.get("placement") or {}).get("pose") or {}
        if o["id"] == skip or pose.get("wall") != wall_id or (o.get("placement") or {}).get("mode") != "3d" or not ax:
            continue
        a, t, _ = ax
        W, H, _ = scene.dims(o)
        out.append((o, (pose["x"] - a[0]) * t[0] + (pose["z"] - a[1]) * t[1], W, o.get("elev") or 0, (o.get("elev") or 0) + H))
    return out


_BESIDE = re.compile(r"\b(?:(links|rechts)\s+)?neben\s+((?:dem|der|den|die|das|einem|einer|einen)\s+[^,.;]+)", re.I)


def _beside(where: str):
    """„links neben der Tür“ → ("links", "der Tür"); None without a „neben“ reference."""
    m = _BESIDE.search(where or "")
    if not m:
        return None
    ref = re.split(r"\s+(?:und|an|auf|in|unter|über|vor|hinter|mit|bis)\s+", m.group(2).strip(), maxsplit=1)[0]
    return ((m.group(1) or "").lower() or None, ref)


def _clear_of(pid: str, scene, doc: dict, piece: dict, pl: dict, photo, side: str | None, ref: str) -> dict:
    """„neben X“: the piece must not cover X in the photo. Slides it along its wall (5 cm steps, ≤ 3 m) to the named
    side of X's frame (or the side it is already on), staying within the wall. X is found by the VLM (frame only)."""
    import place
    obj = _find_object(photo, ref, refine=False)
    box = obj.get("box") if obj.get("status") == "ok" else None
    if not box:
        return pl
    shown = {**piece, **({"elev": pl["elev"]} if pl.get("elev") else {})}
    pose = pl["pose"]
    w = scene.wall_by_id(pose.get("wall"))
    ax = _wall_axis(w)
    if not ax:
        return pl
    a, t, L = ax
    W = scene.dims(shown)[0]

    def frame(q):
        r = scene.project_box(shown, q)
        return r["box"] if r["visible"] else None

    cur = frame(pose)
    if not cur or cur[3] <= box[1] - CLEAR or cur[1] >= box[3] + CLEAR:
        return pl                                       # already clear of it
    if side is None:
        side = "links" if (cur[1] + cur[3]) < (box[1] + box[3]) else "rechts"
    c = scene.to_image([pose["x"], 0, pose["z"]])
    c2 = scene.to_image([pose["x"] + t[0] * 10, 0, pose["z"] + t[1] * 10])
    if not c or not c2:
        return pl
    sign = (-1 if c2[0] > c[0] else 1) if side == "links" else (1 if c2[0] > c[0] else -1)
    s0 = (pose["x"] - a[0]) * t[0] + (pose["z"] - a[1]) * t[1]
    for k in range(1, 61):
        s = s0 + sign * 5 * k
        if not (W / 2 <= s <= L - W / 2):
            break
        q = {**pose, "x": pose["x"] + sign * 5 * k * t[0], "z": pose["z"] + sign * 5 * k * t[1]}
        f = frame(q)
        if not f:
            break
        if (side == "links" and f[3] <= box[1] - CLEAR) or (side == "rechts" and f[1] >= box[3] + CLEAR):
            nxt = place.reprojected(scene, shown, {"mode": "3d", "pose": q, "box": None})
            return {**pl, **nxt} if nxt.get("box") else pl
    _note(pid, f"{workshop.labels(piece)['label']} passt nicht ganz {side} neben {ref} – bitte in der Vorschau prüfen.")
    return pl


def _note(pid: str, text: str) -> None:
    """A hint shown once with the next preview."""
    _update(pid, lambda c: c["notes"].append(text) if text not in c["notes"] else None)


def _reproject_ops(doc: dict) -> list:
    """editor.js syncBoxOps: every 3d box in line with its pose and the current dims (after dims or geometry changes)."""
    import place
    scene = place.scene_for(doc)
    ops = []
    for p in doc.get("pieces") or []:
        pl = p.get("placement") or {}
        if pl.get("mode") != "3d":
            continue
        nxt = place.reprojected(scene, p, pl) if scene else ({**pl, "mode": "2d", "pose": None} if pl.get("box") else
                                                            {"mode": "none", "pose": None, "box": None})
        if json.dumps(nxt, sort_keys=True) != json.dumps(pl, sort_keys=True):
            ops.append({"op": "set", "path": f"pieces.{p['id']}.placement", "value": nxt})
    return ops


def _move(pid: str, m, who: str) -> bool:
    """„weiter links“, „20 cm nach rechts“: along the wall the piece stands at (or its own width axis), in the photo's
    left/right. False when there is no placed piece to move."""
    import place
    doc = projects.load("workshop", pid)
    scene = place.scene_for(doc)
    placed = [p for p in _live(doc) if (p.get("placement") or {}).get("mode") == "3d"]
    if not scene or not placed:
        return False
    conv = _load(pid)
    what = (m.group("what") or "").lower()
    what = "" if what in ("weiter", "mehr", "noch", "etwas", "nach", "bitte", "ein", "ganz") else what
    piece = next((p for p in placed if what and what in workshop.labels(p)["label"].lower()), None) or \
        next((p for p in placed if p["id"] == conv.get("focus")), None) or placed[0]
    cm = int(m.group("cm")) if m.group("cm") else (MOVE_CM // 2 if m.group("bit") else MOVE_CM)
    pose = dict(piece["placement"]["pose"])
    wall = scene.wall_by_id(pose.get("wall")) if pose.get("wall") else None
    if wall:
        t = (-wall["normal"][1], wall["normal"][0])
    else:
        th = math.radians(pose.get("yaw") or 0)
        t = (math.cos(th), math.sin(th))
    h = scene.dims(piece)[1]
    y = scene.elev(piece) + h / 2
    best = None
    for s in (1, -1):
        cand = {**pose, "x": pose["x"] + s * cm * t[0], "z": pose["z"] + s * cm * t[1]}
        a, b = scene.to_image([pose["x"], y, pose["z"]]), scene.to_image([cand["x"], y, cand["z"]])
        if a is None or b is None:
            continue
        du = b[0] - a[0]
        if (m.group("dir").lower() == "links" and du < 0) or (m.group("dir").lower() == "rechts" and du > 0):
            best = cand
    if best is None:
        _say(pid, "In diese Richtung kann ich es nicht verschieben – bitte im Werkstatt-Programm ziehen.")
        return True
    nxt = place.reprojected(scene, piece, {"mode": "3d", "pose": best, "box": None})
    if not nxt.get("box"):
        _say(pid, "Dann wäre es nicht mehr im Bild – ich lasse es, wo es ist.")
        return True
    pushes, pushed = [], []
    ax = _wall_axis(wall)
    if ax:                                     # a neighbour on the same wall moves along instead of standing inside it
        a, tt, L = ax
        W, H, _ = scene.dims(piece)
        bot = piece.get("elev") or 0
        s_new = (best["x"] - a[0]) * tt[0] + (best["z"] - a[1]) * tt[1]
        s_old = (pose["x"] - a[0]) * tt[0] + (pose["z"] - a[1]) * tt[1]
        sign = 1 if s_new > s_old else -1
        if not (W / 2 - 1 <= s_new <= L - W / 2 + 1):
            _say(pid, "Weiter geht es nicht – dort ist die Wand zu Ende.")
            return True
        for o, so, wo, ob, ot in _on_wall(scene, doc, wall["id"], piece["id"]):
            if ot <= bot or ob >= bot + H or (so - s_old) * sign <= 0:
                continue                       # above/below it, or on the other side
            need = (W + wo) / 2 + 1 - abs(so - s_new)
            if need <= 0:
                continue
            so2 = so + sign * need
            if not (wo / 2 - 1 <= so2 <= L - wo / 2 + 1):
                _say(pid, f"Dafür ist neben {_name(o, 'dat')} kein Platz mehr – die Wand ist zu Ende. Vielleicht schmaler?")
                return True
            po = o["placement"]["pose"]
            q = {**po, "x": po["x"] + sign * need * tt[0], "z": po["z"] + sign * need * tt[1]}
            nq = place.reprojected(scene, o, {"mode": "3d", "pose": q, "box": None})
            if not nq.get("box"):
                _say(pid, "Dann wäre das Nachbarmöbel nicht mehr im Bild – ich lasse es, wo es ist.")
                return True
            pushes.append({"op": "set", "path": f"pieces.{o['id']}.placement", "value": nq})
            pushed.append(f"{workshop.labels(o)['label']} rückt mit")
    was = _load(pid).get("state")
    if was == "generating":
        _stop(pid, who, quiet=True)
        _status(pid, "Die laufende Berechnung habe ich abgebrochen, weil sich der Plan ändert.")
    _apply(pid, [{"op": "set", "path": f"pieces.{piece['id']}.placement", "value": nxt}] + pushes, who)
    _update(pid, lambda c: c.__setitem__("focus", piece["id"]))
    _say(pid, f"{workshop.labels(piece)['label']} {cm} cm nach {m.group('dir').lower()} verschoben"
              + (f" ({', '.join(pushed)})." if pushed else "."))
    _preview(pid, who)
    return True


def _set_height(pid: str, h: int, who: str) -> None:
    """„Raumhöhe 2,55“: calibrates the measurement (geometry_api.calibrate, like step ①) and scales the placements;
    raumhohe pieces follow the new ceiling."""
    import place
    if not 100 <= h <= 500:
        _say(pid, "Die Raumhöhe muss zwischen 1,00 und 5,00 m liegen.")
        return
    doc = projects.load("workshop", pid)
    g = doc.get("geometry") or {}
    if not doc.get("room") or not g.get("ok"):
        _say(pid, "Der Raum ist noch nicht vermessen – die Raumhöhe nehme ich, sobald das geklappt hat.")
        return
    try:
        r = _calibrate(doc["room"], h)
    except HTTPException as e:
        _error(pid, str(e.detail))
        return
    geo = r["geometry"]
    k = (geo.get("scale") or 1) / (g.get("scale") or 1)
    pieces = copy.deepcopy(doc["pieces"])
    if abs(k - 1) > 1e-9:
        place.scale_poses(pieces, k)
    old_c = round((g.get("ceiling") or {}).get("height") or 0)
    new_c = round((geo.get("ceiling") or {}).get("height") or h)
    cal = {"p1": None, "p2": None, "length_cm": None, "mode": "room_height", "measured_cm": r.get("measured_cm"),
           "scale": geo.get("scale"), "room_height_cm": h}
    ops = [{"op": "set", "path": "geometry", "value": geo}, {"op": "set", "path": "calibration", "value": cal}]
    scene = place.init(geo, workshop.CATALOG)
    for p, old in zip(pieces, doc["pieces"]):
        if old_c and p.get("h") == old_c and not p.get("elev"):     # raumhoch
            p["h"] = new_c
            ops.append({"op": "set", "path": f"pieces.{p['id']}.h", "value": new_c})
        pl = p.get("placement") or {}
        if pl.get("mode") == "3d" and scene:
            nxt = place.reprojected(scene, p, pl)
            if json.dumps(nxt, sort_keys=True) != json.dumps(old.get("placement"), sort_keys=True):
                ops.append({"op": "set", "path": f"pieces.{p['id']}.placement", "value": nxt})
    _apply(pid, ops, who)
    _say(pid, f"Raumhöhe {h / 100:.2f} m übernommen – die Maße sind jetzt nachgemessen.".replace(".", ",", 1))
    if _live(projects.load("workshop", pid)) and _load(pid).get("state") in ("preview", "done", "asking"):
        _preview(pid, who)


def _question_text(q: dict) -> str:
    d = q.get("default")
    if d and q.get("field") in ("w", "h", "d"):
        return f"{q['question_de']} (Vorschlag: {d} cm – „ja“ übernimmt ihn)"
    return q["question_de"]


def _advance(pid: str, who: str) -> None:
    """Next step after any change: open questions first, then placing what still needs a spot, then the preview."""
    conv = _load(pid)
    if conv.get("questions"):
        q = conv["questions"][0]
        _ask(pid, _question_text(q), {"field": q.get("field"), "piece": q.get("piece"), "default": q.get("default")})
        return
    doc = projects.load("workshop", pid)
    if not _live(doc):
        _ask(pid, ASK_WHAT, {"field": "type"})
        return
    todo = [x for x in conv.get("todo") or [] if any(p["id"] == x for p in doc["pieces"])]
    if todo:
        _state(pid, "understanding")
        if _gpu_busy():
            _status(pid, "Der Rechner ist gerade mit Bildern beschäftigt – das Platzieren kann etwas dauern.")
        else:
            _status(pid, "Ich suche die Stellen im Foto …")
    for piece_id in todo:
        q = _place(pid, piece_id, who)
        _update(pid, lambda c: c.__setitem__("todo", [x for x in c.get("todo") or [] if x != piece_id]))
        if q:
            _update(pid, lambda c: c.__setitem__("questions", [q]))
            _ask(pid, q["question_de"], {"field": q["field"], "piece": q["piece"], "default": None})
            return
    _preview(pid, who)


# ---------------- preview ----------------

def _font(size: int):
    try:
        return ImageFont.truetype(str(FONT), size)
    except OSError:
        return ImageFont.load_default()


def _hex(c: str) -> tuple:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _dims(p: dict) -> tuple:
    return workshop.piece_dims(p)


def _piece_line(p: dict, spot: dict | None = None) -> str:
    lab = workshop.labels(p)
    w, h, d = (p.get("w"), p.get("h"), p.get("d"))
    if (p.get("placement") or {}).get("mode") == "3d":
        w, h, d = _dims(p)
    dims = " × ".join(str(x) if x else "?" for x in (w, h, d)) + " cm"
    handles = lab["handles_label"] if p.get("type") in _FRONTS or not p.get("type") else ""
    bits = [x for x in (lab["material_label"], handles, (p.get("text") or "").strip()) if x]
    s = f"{lab['label']}: {dims}" + (", " + ", ".join(bits) if bits else "")
    elev = p.get("elev") or 0
    if elev:
        s += f", Unterkante {elev} cm über dem Boden"
    if p.get("slope_fit"):
        s += ", oben an die Dachschräge angepasst"
    where = (p.get("where") or "").strip()
    mode = (p.get("placement") or {}).get("mode")
    if mode in ("2d", "3d"):
        s += f" – {where}" if where else " – platziert"
        if spot and (spot.get("confidence") or 1) < SURE_CONF:
            s += " (Stelle unsicher – bitte prüfen)"
    else:
        s += f" – {where} (Platz wählt die Bildberechnung)" if where else " – Platz wählt die Bildberechnung"
    return s


_FRONTS = {"schrank", "kueche", "sideboard", "haengeschrank", "garderobe", "bad", "sitzbank"}   # = intent.HANDLE_TYPES


def _dash(draw, a, b, fill, width, on=14, off=9):
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    if L < 1:
        return
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    s = 0.0
    while s < L:
        e = min(L, s + on)
        draw.line([(a[0] + ux * s, a[1] + uy * s), (a[0] + ux * e, a[1] + uy * e)], fill=fill, width=width)
        s = e + off


def _pill(draw, xy, text, bg, fg, font, size):
    """A label like W3D's: a filled box centred above xy, kept inside the picture."""
    W, H = size
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    w, h = r - l + 14, b - t + 10
    x = min(max(2, xy[0] - w / 2), max(2, W - w - 2))
    y = min(max(2, xy[1] - h), max(2, H - h - 2))
    draw.rounded_rectangle([x, y, x + w, y + h], radius=5, fill=bg)
    draw.text((x + 7 - l, y + 5 - t), text, font=font, fill=fg)
    return y


def _text_h(draw, text, font) -> int:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return b - t


def render_preview(doc: dict) -> Image.Image:
    """The „So?“ picture: the photo with the planned pieces drawn in like step ③ draws them (W3D.drawOverlay: coloured
    wireframe, light front face, „B × H × T“ label) on top of the 3d mock-up the generation will get as its guide, and
    the regions to remove dashed in red."""
    import place
    photo = _photo(doc)
    if photo is None:
        raise HTTPException(400, "Der Auftrag hat noch kein Raumfoto.")
    pieces = _live(doc)
    boxes, _ = workshop.resolve_boxes(doc)
    base = photo
    placed3d = [p for p in pieces if boxes.get(p["id"]) and workshop._is3d(p)]
    if workshop._geo(doc) and (placed3d or doc.get("removes")):
        try:
            base = workshop.guide_image(photo, doc, boxes, placed3d)
        except Exception:
            traceback.print_exc()
    img = base.convert("RGB")
    if max(img.size) > PREVIEW_SIDE:
        s = PREVIEW_SIDE / max(img.size)
        img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    W, H = img.size
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    lw = max(2, round(0.0028 * max(W, H)))
    small, big = _font(max(13, round(W / 75))), _font(max(15, round(W / 62)))
    removals, removals_boxes, labels = [], [], []
    for r in doc.get("removes") or []:
        if isinstance(r, dict) and r.get("poly"):
            pts = [(x * W / 1000, y * H / 1000) for x, y in r["poly"]]
        else:
            t, l, b, ri = (r if isinstance(r, list) else r.get("box"))
            pts = [(l * W / 1000, t * H / 1000), (ri * W / 1000, t * H / 1000), (ri * W / 1000, b * H / 1000),
                   (l * W / 1000, b * H / 1000)]
        draw.polygon(pts, fill=(255, 90, 70, 40))
        for a, b in zip(pts, pts[1:] + pts[:1]):
            _dash(draw, a, b, (255, 110, 90, 235), lw)
        cx = sum(p[0] for p in pts) / len(pts)
        bottom = max(p[1] for p in pts)
        rb = [min(p[1] for p in pts), min(p[0] for p in pts), bottom, max(p[0] for p in pts)]
        removals.append((cx, bottom, rb))
        removals_boxes.append(rb)
    scene = place.scene_for(doc)
    gw, gh = (scene.size if scene else (W, H))
    sx, sy = W / gw, H / gh
    for k, p in enumerate(doc.get("pieces") or []):
        if p not in pieces:
            continue
        col = _hex(COLORS[k % len(COLORS)])
        num = pieces.index(p) + 1
        box = boxes.get(p["id"])
        lab = workshop.labels(p)["label"]
        w, h, d = _dims(p)
        if scene and workshop._is3d(p) and box:
            pb = scene.project_box(p)
            C = [(c[0] * sx, c[1] * sy) if c else None for c in pb["corners"]]
            face = [C[j] for j in (0, 1, 5, 4)]
            if all(face):
                draw.polygon(face, fill=col + (40,))
            for a, b in EDGES:
                if C[a] and C[b]:
                    draw.line([C[a], C[b]], fill=col + (255,), width=lw + 1)
            top = [C[j] for j in (4, 5, 6, 7) if C[j]]
            u = sum(c[0] for c in top) / len(top) if top else (box[1] + box[3]) / 2 * W / 1000
            v = min(c[1] for c in top) if top else box[0] * H / 1000
        elif box:
            x0, y0, x1, y1 = box[1] * W / 1000, box[0] * H / 1000, box[3] * W / 1000, box[2] * H / 1000
            draw.rectangle([x0, y0, x1, y1], outline=col + (255,), width=lw + 1)
            u, v = (x0 + x1) / 2, y0
        else:
            continue
        labels.append((u, v, col, f"{num}  {lab}", f"B {w} × H {h} × T {d}"))
    done = []
    for cx, bottom, rb in removals:   # labels last, so no outline crosses them; overlapping regions get one label
        if any(rb[0] < o[2] and o[0] < rb[2] and rb[1] < o[3] and o[1] < rb[3] for o in done):
            continue
        done.append(rb)
        low = max([bottom] + [r[2] for r in removals_boxes if rb[0] < r[2] and r[0] < rb[2] and rb[1] < r[3] and r[1] < rb[3]])
        _pill(draw, (cx, low + 30), "wird entfernt", (255, 110, 90, 230), (255, 255, 255, 255), small, (W, H))
    for u, v, col, name, dims in labels:
        hn, hd = _text_h(draw, name, big) + 10, _text_h(draw, dims, small) + 10
        if v - 6 - hd - 3 - hn < 2:            # no room above the piece: inside, below its top edge
            v = v + 8 + hn + 3 + hd + 6
        y = _pill(draw, (u, v - 6), dims, col + (240,), (11, 13, 16, 255), small, (W, H))
        _pill(draw, (u, y - 3), name, col + (255,), (11, 13, 16, 255), big, (W, H))
    unplaced = [(pieces.index(p) + 1, p) for p in pieces if not boxes.get(p["id"])]
    if unplaced:
        text = "  ·  ".join(f"{n} {workshop.labels(p)['label']}: Platz wählt die Bildberechnung" for n, p in unplaced)
        _pill(draw, (W / 2, H - 6), text, (0, 0, 0, 170), (255, 255, 255, 255), small, (W, H))
    return Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")


def _assistent_dir(pid: str) -> Path:
    import delivery
    return delivery.exports_dir(pid) / "assistent"     # purged and exported (DSGVO) with the order's export folder


def _rel(p: Path) -> str:
    return p.resolve().relative_to(Path(workshop.OUT).resolve()).as_posix()


def _preview(pid: str, who: str) -> None:
    doc = projects.load("workshop", pid)
    pieces = _live(doc)
    if not pieces:
        _ask(pid, ASK_WHAT, {"field": "type"})
        return
    image = None
    if doc.get("room"):
        conv = _load(pid)
        n = (conv["events"][-1]["n"] if conv["events"] else 0) + 1
        folder = _assistent_dir(pid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"vorschau-{n:04d}.jpg"
        render_preview(doc).save(path, quality=88)
        for old in sorted(folder.glob("vorschau-*.jpg"))[:-KEEP_PREVIEWS]:
            old.unlink(missing_ok=True)
        image = _rel(path)
    check = _plan_check(pid, doc)
    conv = _load(pid)
    spots = conv.get("spots") or {}
    lines = ["So geplant – passt das?"]
    lines += [f"{k}. {_piece_line(p, spots.get(p['id']))}" for k, p in enumerate(pieces, 1)]
    if conv.get("removed") or doc.get("removes"):
        what = ", ".join(conv.get("removed") or []) or f"{len(doc['removes'])} markierte Stelle(n)"
        lines.append(f"Wird entfernt: {what}")
    notes = list(conv.get("notes") or [])
    notes += _plan_warnings(doc)
    # A piece in front of a window, door or radiator (what the plan check reports) is a question, not an OK: the
    # problem and its fix come first, and the fix is one answer away instead of three minutes of wrong images.
    problems, conflict = [], None
    # a piece reaching into a sloped ceiling is measured, not guessed: that problem and its fix come before the plan
    # check's (which often reports the same thing in vaguer words)
    sloped = set()
    for r in _slope_rows(doc):
        p, k = r["piece"], r["n"]
        sloped.add(p["id"])
        alt = [f"„niedriger“ ({r['fit_h']} cm)"] if r["fit_h"] else []
        try:
            if _slope_spot(doc, p):
                alt.append("„anderer Platz“ (dort passt er in voller Höhe)")
        except Exception:
            traceback.print_exc()
        problems.append(f"{workshop.labels(p)['label']} {k} ragt {round(r['over'])} cm in die Dachschräge (dort nur "
                        f"{round(r['lowest'])} cm frei). Vorschlag: an die Schräge anpassen (oben schräg)"
                        + (" – oder " + " oder ".join(alt) if alt else "") + ".")
        if conflict is None:
            conflict = {"piece": p["id"], "n": k, "fix": "an die Dachschräge anpassen", "cmd": "slope_fit"}
    for row in check.get("pieces") or []:
        if not row.get("ok"):
            k, p = next(((i, p) for i, p in enumerate(pieces, 1) if p["id"] == row.get("id")), ("?", None))
            if p and p["id"] in sloped and re.search(r"schräg|dach|decke|first|balken", " ".join(row.get("problems") or []), re.I):
                continue
            fix = (row.get("fix") or "").strip()
            problems.append(f"KI-Plancheck zu {k}: {' '.join(row.get('problems') or [])}" + (f" Vorschlag: {fix}" if fix else ""))
            if fix and p and conflict is None:
                conflict = {"piece": p["id"], "n": k, "fix": fix,
                            "send": f"{workshop.labels(p)['label']} {k}: {fix}" if len(pieces) > 1 else fix}
    if problems:
        lines[0] = "So passt es noch nicht:"
        lines[1:1] = [f"⚠ {x}" for x in problems] + ["Geplant ist:"]
    lines += [f"Hinweis: {x}" for x in dict.fromkeys(notes)]
    lines.append(FIX_HINT if conflict else SO_HINT)
    data = {"rev": doc["rev"], "pieces": [_piece_data(p, k) for k, p in enumerate(pieces, 1)],
            "removes": conv.get("removed") or [], "plan_check": check.get("status"),
            "problems": problems, "conflict": conflict, "url": f"/werkstatt#/p/{pid}"}
    _emit(pid, "agent", "preview", "\n".join(lines), image=image, data=data, state="preview")
    _update(pid, lambda c: c.update(preview=image, preview_rev=doc["rev"], notes=[], conflict=conflict))


def _piece_data(p: dict, k: int) -> dict:
    lab = workshop.labels(p)
    return {"n": k, "id": p["id"], "type": p.get("type"), "label": lab["label"], "dims": list(_dims(p)),
            "material": lab["material_label"], "handles": lab["handles_label"], "text": p.get("text") or "",
            "where": p.get("where") or "", "elev": p.get("elev") or 0,
            "placed": (p.get("placement") or {}).get("mode") in ("2d", "3d"), "wall": ((p.get("placement") or {}).get("pose") or {}).get("wall")}


def _plan_warnings(doc: dict) -> list[str]:
    """What step ④ would warn about: higher than the ceiling, wider than the wall, guessed sizes, overlaps."""
    out = []
    boxes, _ = workshop.resolve_boxes(doc)
    pieces = _live(doc)
    for k, p in enumerate(pieces, 1):
        rel = workshop._relations(doc, p, boxes.get(p["id"])) or {}
        if (rel.get("ceiling_ratio") or 0) > 1.01 and workshop.slope_over(rel) <= workshop.SLOPE_TOL \
                and not p.get("slope_fit"):
            out.append(f"{k} ist höher als die Decke ({round(rel['ceiling_height'])} cm).")
        if (rel.get("wall_ratio") or 0) > 1.01:
            out.append(f"{k} ist breiter als die Wand dahinter (etwa {round(rel['wall_length'])} cm).")
    import place
    scene = place.scene_for(doc)
    clash = set()
    for i, a in enumerate(pieces, 1):
        pose = (a.get("placement") or {}).get("pose") or {}
        if not scene or not pose.get("wall") or (a.get("placement") or {}).get("mode") != "3d":
            continue
        ax = _wall_axis(scene.wall_by_id(pose["wall"]))
        if not ax:
            continue
        W, H, _ = scene.dims(a)
        s = (pose["x"] - ax[0][0]) * ax[1][0] + (pose["z"] - ax[0][1]) * ax[1][1]
        bot = a.get("elev") or 0
        for o, so, wo, ob, ot in _on_wall(scene, doc, pose["wall"], a["id"]):
            j = pieces.index(o) + 1
            over = (W + wo) / 2 - abs(s - so)
            if j > i and over > 2 and ot > bot and ob < bot + H:
                clash.add((i, j))
                la, lo = workshop.labels(a)["label"], workshop.labels(o)["label"]
                out.append(f"{i} und {j} stehen an der Wand {round(over)} cm ineinander – z. B. „{la} "
                           f"{max(10, math.ceil(over / 10) * 10)} cm schmaler“ oder „{lo} weiter rechts“.")
    for i, a in enumerate(pieces, 1):
        for j, b in enumerate(pieces[i:], i + 1):
            if (i, j) in clash:
                continue
            ba, bb = boxes.get(a["id"]), boxes.get(b["id"])
            if ba and bb:
                inter = max(0, min(ba[2], bb[2]) - max(ba[0], bb[0])) * max(0, min(ba[3], bb[3]) - max(ba[1], bb[1]))
                small = min((ba[2] - ba[0]) * (ba[3] - ba[1]), (bb[2] - bb[0]) * (bb[3] - bb[1])) or 1
                if inter / small > 0.35:
                    out.append(f"{i} und {j} überschneiden sich im Bild – „weiter links/rechts“ schiebt eins zur Seite.")
    g = workshop._geo(doc)
    if g and not doc.get("calibration") and any(workshop._is3d(p) for p in pieces):
        out.append("Maße geschätzt, der Raum ist nicht nachgemessen – z. B. „Raumhöhe 2,60“ schreiben, dann stimmt es genau.")
    return out


def _slope_rows(doc: dict) -> list[dict]:
    """Pieces that reach into a sloped ceiling: [{piece, n, over, lowest, fit_h}] (fit_h = the straight height that
    would fit, rounded down to 5 cm with 2 cm air; None when that would be under 60 cm)."""
    out = []
    boxes, _ = workshop.resolve_boxes(doc)
    for k, p in enumerate(_live(doc), 1):
        if p.get("slope_fit"):
            continue
        rel = workshop._relations(doc, p, boxes.get(p["id"])) or {}
        over = workshop.slope_over(rel)
        if over <= workshop.SLOPE_TOL:
            continue
        sl = rel["slope"]
        fit = int((sl["lowest"] - 2 - (p.get("elev") or 0)) // 5 * 5)
        out.append({"piece": p, "n": k, "over": over, "lowest": sl["lowest"], "fit_h": fit if fit >= 60 else None})
    return out


def _slope_target(pid: str, doc: dict):
    """The piece a slope answer means: the one the preview asked about, else the first reaching into a slope."""
    rows = _slope_rows(doc)
    conflict = (_load(pid) or {}).get("conflict") or {}
    return next((r for r in rows if r["piece"]["id"] == conflict.get("piece")), rows[0] if rows else None)


def _slope_fit(pid: str, who: str, piece_id: str | None = None) -> None:
    doc = projects.load("workshop", pid)
    row = next((r for r in _slope_rows(doc) if r["piece"]["id"] == piece_id), None) if piece_id else _slope_target(pid, doc)
    if row is None:
        _say(pid, "Es ragt nichts in eine Dachschräge – da muss nichts angepasst werden.")
        return
    p = row["piece"]
    _apply(pid, [{"op": "set", "path": f"pieces.{p['id']}.slope_fit", "value": True}], who)
    _say(pid, f"{workshop.labels(p)['label']} wird an die Dachschräge angepasst: oben schräg, an der niedrigsten Stelle "
              f"etwa {round(row['lowest'])} cm hoch. Das genaue Schrägenmaß nimmst du vor Ort auf.")
    _preview(pid, who)


def _slope_lower(pid: str, who: str) -> None:
    doc = projects.load("workshop", pid)
    row = _slope_target(pid, doc)
    if row is None:
        _say(pid, "Es ragt nichts in eine Dachschräge.")
        return
    p = row["piece"]
    if not row["fit_h"]:
        _say(pid, f"Dort sind nur etwa {round(row['lowest'])} cm frei – niedriger lohnt sich nicht. Besser „an die "
                  "Schräge anpassen“ oder „anderer Platz“.")
        return
    _apply(pid, [{"op": "set", "path": f"pieces.{p['id']}.h", "value": row["fit_h"]}], who)
    _say(pid, f"{workshop.labels(p)['label']} ist jetzt {row['fit_h']} cm hoch – so passt er unter die Schräge.")
    _preview(pid, who)


def _slope_spot(doc: dict, piece: dict):
    """A place on a wall where the piece fits under the sloped ceilings, nearest to where it is now: (placement, wall
    label) or None. Free of the other pieces on that wall and fully in the photo."""
    import place
    scene = place.scene_for(doc)
    geo = workshop._geo(doc)
    if not scene or not geo:
        return None
    W, H, D = scene.dims(piece)
    elev = piece.get("elev") or 0
    pose0 = (piece.get("placement") or {}).get("pose") or {}
    x0, z0 = pose0.get("x", 0.0), pose0.get("z", 0.0)
    import mockup
    depth = geometry_depth(doc)
    skip = None
    if depth is not None and doc.get("removes"):
        dh, dw = depth.shape
        m = Image.new("L", (dw, dh), 0)
        dr = ImageDraw.Draw(m)
        for r in workshop._removes(doc):
            b = workshop._rbox(r)
            dr.rectangle([b[1] * dw / 1000, b[0] * dh / 1000, b[3] * dw / 1000, b[2] * dh / 1000], fill=255)
        skip = np.asarray(m) > 0                       # what will be removed anyway is no obstacle
    best = None
    for w in geo.get("walls") or []:
        ax = _wall_axis(w)
        if not ax:
            continue
        a, t, L = ax
        if L < W + 4:
            continue
        n = w["normal"]
        others = _on_wall(scene, doc, w["id"], piece["id"])
        for k in range(int((L - W) // 10) + 1):
            s_ = W / 2 + 2 + 10 * k
            if s_ > L - W / 2 - 2:
                break
            if any(abs(s_ - so) < (W + wo) / 2 + 1 and ot > elev and ob < elev + H for _, so, wo, ob, ot in others):
                continue
            on = (a[0] + s_ * t[0], a[1] + s_ * t[1])
            pose = {"x": on[0] + n[0] * D / 2, "z": on[1] + n[1] * D / 2, "yaw": scene.wall_yaw(w), "wall": w["id"]}
            sc = workshop.geometry.slope_clearance(geo, (W, H, D), {**pose, "y": elev})
            if sc and sc["over"] > workshop.SLOPE_TOL:
                continue
            ch = (geo.get("ceiling") or {}).get("height")
            if ch and elev + H > ch + 1:
                continue
            d = math.hypot(pose["x"] - x0, pose["z"] - z0)
            if best is not None and d >= best[0]:
                continue
            pl = place.reprojected(scene, piece, {"mode": "3d", "pose": pose, "box": None})
            b = pl.get("box")
            if not b or (b[2] - b[0]) * (b[3] - b[1]) > 850_000 or b[1] < 5 or b[3] > 995:
                continue
            if depth is not None:                      # not into a bed, not hidden behind one, not before a window
                ob = mockup.obstacles(geo, piece, (W, H, D), {**pl["pose"], "y": elev}, depth, skip)
                if ob["inside"] > 0.08 or ob["front_low"] > 0.2 or ob["behind"] > 0.15:
                    continue
            best = (d, pl, w.get("label"))
    return (best[1], best[2]) if best else None


def geometry_depth(doc: dict):
    """The room's stored depth map (cm), or None."""
    try:
        return workshop.geometry.depth_map(doc["room"]) if doc.get("room") else None
    except Exception:
        traceback.print_exc()
        return None


def _slope_move(pid: str, who: str) -> None:
    doc = projects.load("workshop", pid)
    row = _slope_target(pid, doc)
    if row is None:
        _say(pid, "Es ragt nichts in eine Dachschräge.")
        return
    p = row["piece"]
    spot = _slope_spot(doc, p)
    if spot is None:
        _say(pid, f"Im Foto finde ich keinen Platz, an dem {_name(p)} in voller Höhe unter die Schräge passt. "
                  + (f"„niedriger“ macht ihn {row['fit_h']} cm hoch, " if row["fit_h"] else "")
                  + "„an die Schräge anpassen“ schneidet ihn oben schräg zu.")
        return
    pl, label = spot
    _apply(pid, [{"op": "set", "path": f"pieces.{p['id']}.placement", "value": pl}], who)
    where = {"hinten": "an die Rückwand", "links": "an die linke Wand", "rechts": "an die rechte Wand"}.get(label, "an eine andere Wand")
    _say(pid, f"{workshop.labels(p)['label']} steht jetzt {where}, dort passt er in voller Höhe unter die Decke.")
    _preview(pid, who)


def _plan_check(pid: str, doc: dict) -> dict:
    """The KI-Plancheck of step ④ (window, door, radiator, floating). Skipped quietly when the judge is down."""
    if not any(workshop._is3d(p) for p in _live(doc)):
        return {"status": "none", "pieces": []}
    try:
        res = _check_plan(pid)
    except Exception:
        traceback.print_exc()
        return {"status": "error", "pieces": []}
    if res.get("status") in ("unavailable", "error"):
        _note(pid, "KI-Plancheck gerade nicht möglich.")
    return res if isinstance(res, dict) else {"status": "error", "pieces": []}


# ---------------- generation ----------------

def _go(pid: str, who: str, again: bool = False) -> None:
    conv = _load(pid)
    doc = projects.load("workshop", pid)
    if conv.get("preview_rev") is None:
        _preview(pid, who)
        return
    if projects.changed_since(doc, conv["preview_rev"], workshop.DESIGN):
        _say(pid, "Der Auftrag wurde inzwischen geändert (z. B. im Werkstatt-Programm) – hier die aktuelle Vorschau.")
        _preview(pid, who)
        return
    req = {"kind": "room" if doc.get("room") else "free", "base_rev": conv["preview_rev"], "count": COUNT}
    try:
        out = _generate(pid, req, who)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        if e.status_code == 409 and detail.get("code") == "running" and (detail.get("job") or {}).get("id"):
            jid = detail["job"]["id"]
            _update(pid, lambda c: c.update(jobs=[jid], retried=False, retry_seen=False))
            _status(pid, "Für diesen Auftrag läuft schon eine Berechnung – ich warte auf deren Bilder.")
            _state(pid, "generating")
            _watch(pid)
            return
        if e.status_code == 409 and detail.get("code") == "changed":
            _say(pid, "Der Auftrag wurde gerade geändert – hier die aktuelle Vorschau.")
            _preview(pid, who)
            return
        _error(pid, f"Rechnen geht so nicht: {detail.get('message') or 'unbekannter Fehler'}", state="preview")
        return
    job = out.get("job") or {}
    plan = out.get("plan") or {}
    if not job.get("id"):
        _error(pid, "Die Berechnung ließ sich nicht starten. Bitte gleich noch einmal „ja“ schreiben.", state="preview")
        return
    _update(pid, lambda c: c.update(jobs=[job["id"]], retried=False, retry_seen=False, started=time.time()))
    eta = plan.get("estimate_text") or job.get("eta_text") or ""
    warn = "".join(f"\nHinweis: {w}" for w in plan.get("warnings") or [] if "nicht nachgemessen" not in w)
    msg = f"Ich rechne {plan.get('label') or 'die Bilder'}" + (f" – {eta}" if eta else "")
    _status(pid, msg + ("" if msg.endswith(".") else ".") + warn,
            data={"job": job["id"]})
    _state(pid, "generating")
    _watch(pid)


def _stop(pid: str, who: str, quiet: bool = False) -> None:
    conv = _load(pid)
    if not conv.get("jobs") or conv.get("state") not in ("generating", "understanding"):
        if not quiet:
            _say(pid, "Gerade läuft keine Berechnung.")
        return
    for jid in conv.get("jobs") or []:
        try:
            _cancel_job(jid, who)
        except Exception:
            traceback.print_exc()
    _WATCH.discard(pid)
    _update(pid, lambda c: c.update(jobs=[]))
    if not quiet:
        _say(pid, "Berechnung abgebrochen. „ja“ oder „nochmal“ startet sie neu.", state="preview")
    else:
        _state(pid, "preview")


def _watch(pid: str) -> None:
    _WATCH.add(pid)
    with _META:
        t = _WATCHER["thread"]
        if t is None or not t.is_alive():
            _WATCHER["stop"].clear()
            t = threading.Thread(target=_watch_loop, daemon=True, name="agent-watch")
            _WATCHER["thread"] = t
            t.start()


def _watch_loop() -> None:
    while not _WATCHER["stop"].wait(WATCH_S):
        for pid in list(_WATCH):
            try:
                if _poll(pid):
                    _WATCH.discard(pid)
                    _enqueue(pid, {"kind": "finished", "who": (_load(pid) or {}).get("who")})
            except projects.Gone:
                _forget(pid)
            except Exception:
                traceback.print_exc()


FINAL = ("done", "error", "cancelled")


def _poll(pid: str) -> bool:
    """True when every job of the generation (incl. the scheduler's corrected retry) is over and graded. A message
    handled during the generation (understanding / transcribing / a new photo) is not the end of it: the jobs decide."""
    conv = _load(pid)
    if conv is None or conv.get("state") not in ("generating",) + BUSY_STATES or not conv.get("jobs"):
        return True
    doc = projects.load("workshop", pid)
    ids = list(conv.get("jobs") or [])
    active = False
    new_retry = None
    for jid in list(ids):
        v = _job_view(jid)
        if v and v.get("retry_job") and v["retry_job"] not in ids:
            ids.append(v["retry_job"])
            new_retry = v["retry_job"]
    for jid in ids:
        v = _job_view(jid)
        if jid in (doc.get("pending_jobs") or []):
            active = True
        elif v and (v.get("state") not in FINAL or v.get("judge_state") in ("pending", "running")):
            active = True
    if new_retry:
        _update(pid, lambda c: c.update(jobs=ids, retry_seen=True))
        _status(pid, "Kein Bild hat die Qualitätsprüfung bestanden – ich rechne einen Korrekturversuch.")
    return not active


def _rank(v: dict) -> tuple:
    j = v.get("judge") if isinstance(v.get("judge"), dict) else {}
    return (1 if j.get("pass") is True else 0, float(j.get("overall") or 0))


def _score_text(v: dict) -> str:
    j = v.get("judge") if isinstance(v.get("judge"), dict) else {}
    if j.get("status") == "ok" and j.get("overall") is not None:
        s = f"{float(j['overall']):.1f}".replace(".", ",") + " von 10"
        return s + (", geprüft ✓" if j.get("pass") else ", nicht bestanden")
    if j.get("status") == "unavailable":
        return "ungeprüft (Prüfer nicht erreichbar)"
    return "ohne Bewertung"


def _on_finished(pid: str, who: str) -> None:
    import judge
    conv = _load(pid)
    if conv.get("state") != "generating":
        return
    doc = projects.load("workshop", pid)
    ids = set(conv.get("jobs") or [])
    mine = [v for v in doc.get("variants") or [] if v.get("job") in ids]
    if not mine and not _poll(pid):      # queued too early (e.g. a recovered watch): keep following the jobs
        _watch(pid)
        return
    if not mine:
        why = ""
        for jid in ids:
            v = _job_view(jid) or {}
            if v.get("state") == "error":
                why = f" ({v.get('error') or 'Fehler'})"
            elif v.get("state") == "cancelled":
                why = " (abgebrochen)"
        _error(pid, f"Die Berechnung hat keine Bilder geliefert{why}. „ja“ startet sie noch einmal.", state="preview")
        return
    verdicts = [v.get("judge") or {} for v in mine]
    if not conv.get("retry_seen") and not conv.get("retried") and judge.needs_retry(verdicts):
        # the scheduler retries only when the order's auto_retry is on; otherwise the assistant does it once
        _update(pid, lambda c: c.update(retried=True))
        _status(pid, "Kein Bild hat die Qualitätsprüfung bestanden – ich rechne noch einmal.")
        _go_retry(pid, who)
        return
    _result(pid, who, doc, mine)


def _go_retry(pid: str, who: str) -> None:
    conv = _load(pid)
    doc = projects.load("workshop", pid)
    req = {"kind": "room" if doc.get("room") else "free", "base_rev": conv.get("preview_rev") or doc["rev"], "count": 1}
    try:
        out = _generate(pid, req, who)
    except HTTPException:
        traceback.print_exc()
        _result(pid, who, doc, [v for v in doc.get("variants") or [] if v.get("job") in set(conv.get("jobs") or [])])
        return
    jid = (out.get("job") or {}).get("id")
    if jid:
        _update(pid, lambda c: c.update(jobs=list(c.get("jobs") or []) + [jid], retry_seen=True))
        _watch(pid)


def _result(pid: str, who: str, doc: dict, variants: list[dict]) -> None:
    ranked = sorted(variants, key=_rank, reverse=True)
    best = ranked[:3]
    ok = [v for v in ranked if (v.get("judge") or {}).get("pass") is True]
    graded = any((v.get("judge") or {}).get("status") == "ok" for v in ranked)
    lines = []
    if ok:
        lines.append(f"Fertig: {len(ranked)} {'Bild' if len(ranked) == 1 else 'Bilder'}, {len(ok)} davon von der "
                     "KI-Prüfung freigegeben.")
    elif graded:
        lines.append(f"Fertig: {len(ranked)} {'Bild' if len(ranked) == 1 else 'Bilder'} – keins hat die KI-Prüfung "
                     "bestanden. Hier die besten; bitte selbst ansehen. „nochmal“ rechnet neue.")
    else:
        lines.append(f"Fertig: {len(ranked)} {'Bild' if len(ranked) == 1 else 'Bilder'} (die KI-Prüfung war nicht "
                     "erreichbar – bitte selbst ansehen).")
    for k, v in enumerate(best, 1):
        comment = ((v.get("judge") or {}).get("comment") or "").strip()
        lines.append(f"Bild {k}: {_score_text(v)}" + (f" – {comment}" if comment else ""))
    files, models, pdf, offer_text = [], [], None, ""
    try:
        import offer
        offer_text = offer.offer_text(doc, best[0]["id"])
    except Exception:
        traceback.print_exc()
    try:
        import delivery
        pdf = delivery.draft_sheet(pid, [v["id"] for v in best])["pdf"]
    except Exception:
        traceback.print_exc()
    try:
        import connect
        folder = _assistent_dir(pid)
        folder.mkdir(parents=True, exist_ok=True)
        typed = [p for p in _live(doc) if p.get("type")]
        if typed:
            data, _ = connect.render_file(pid, doc, "zeichnung.dxf")
            (folder / "zeichnung.dxf").write_bytes(data)
            files.append(_rel(folder / "zeichnung.dxf"))
        for p in typed:
            data, _ = connect.render_file(pid, doc, f"modell_{p['id']}.glb")
            (folder / f"modell_{p['id']}.glb").write_bytes(data)
            models.append({"piece": p["id"], "label": workshop.labels(p)["label"], "glb": _rel(folder / f"modell_{p['id']}.glb"),
                           "stl": f"/api/workshop/{pid}/model/{p['id']}.stl"})
    except Exception:
        traceback.print_exc()
    lines.append("„senden“ bereitet eine Kundenversion vor, „nochmal“ rechnet neue Bilder – oder sag, was anders soll.")
    data = {"variants": [{"id": v["id"], "image": v["image"], "score": (v.get("judge") or {}).get("overall"),
                          "pass": (v.get("judge") or {}).get("pass"), "comment": (v.get("judge") or {}).get("comment"),
                          "status": (v.get("judge") or {}).get("status")} for v in best],
            "images": [v["image"] for v in best], "offer_text": offer_text, "pdf": pdf, "files": files,
            "models": models, "url": f"/werkstatt#/p/{pid}/ergebnis"}
    _emit(pid, "agent", "result", "\n".join(lines), image=best[0]["image"], data=data, state="done")
    _update(pid, lambda c: c.update(jobs=[], result=[v["id"] for v in best]))
    prof = _profile()
    try:
        decision = prof.auto_send_decision([v.get("judge") or {} for v in best]) if prof else {"send": False}
    except Exception:
        decision = {"send": False}
    if decision.get("send"):
        _send(pid, who, [best[i]["id"] for i in decision.get("picks") or []])


def _send(pid: str, who: str, ids: list | None = None) -> None:
    """„senden“: prepares a customer version (PDF, ZIP, link with PIN) like step ⑤ – it is not sent anywhere; the
    carpenter passes the link on himself."""
    import delivery
    conv = _load(pid)
    doc = projects.load("workshop", pid)
    live = {v["id"] for v in doc.get("variants") or []}
    ids = [x for x in (ids or conv.get("result") or []) if x in live][:delivery.MAX_VARIANTS]
    if not ids:
        _say(pid, "Es gibt noch keine fertigen Bilder zum Senden.")
        return
    base = f"http://{delivery.lan_ip() or '127.0.0.1'}:{LAN_PORT}"
    try:
        res = delivery.create_version(pid, ids, "", 90, who, base, pin=True)
    except HTTPException as e:
        _error(pid, f"Kundenversion ging nicht: {e.detail}")
        return
    ver = res.get("version") or {}
    lines = [f"Kundenversion {ver.get('n')} ist vorbereitet – noch nicht verschickt.",
             f"Link (nur im Werkstatt-WLAN erreichbar): {res.get('link')}"]
    if ver.get("pin"):
        lines.append(f"PIN für den Kunden: {ver['pin']} (bitte getrennt mitteilen)")
    lines.append("Im Werkstatt-Programm unter „Senden“ lässt sie sich verschicken oder zurückziehen.")
    pdf_path = (Path(str(ver.get("dir") or "")) / str((ver.get("files") or {}).get("pdf") or "")).as_posix() \
        if ver.get("dir") and (ver.get("files") or {}).get("pdf") else None
    _say(pid, "\n".join(lines), data={"version": ver.get("n"), "pdf": pdf_path, "link": res.get("link")})


# ---------------- order writes ----------------

def _apply(pid: str, ops: list, who: str) -> dict:
    """projects.apply_ops on the latest revision (a concurrent web edit is re-read once)."""
    if not ops:
        return projects.load("workshop", pid)
    for attempt in (0, 1):
        doc = projects.load("workshop", pid)
        try:
            return projects.apply_ops(pid, doc["rev"], ops, who)
        except HTTPException as e:
            if e.status_code != 409 or attempt:
                raise
    raise RuntimeError("unreachable")


def _text(v) -> str:
    s = str(v or "").strip()
    if len(s) > MAX_TEXT:
        raise HTTPException(400, f"Der Text ist zu lang (höchstens {MAX_TEXT} Zeichen).")
    return s


def _store_photo(data: bytes) -> dict:
    import imaging
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise HTTPException(400, "Das Foto ist leer.")
    if len(data) > imaging.MAX_UPLOAD:
        raise HTTPException(413, f"Das Foto ist zu groß (höchstens {imaging.MAX_UPLOAD // 2**20} MB).")
    try:
        return projects._store_upload(bytes(data), PHOTO_SIDE)
    except HTTPException:
        raise HTTPException(400, "Das Foto konnte nicht gelesen werden.")


# ---------------- HTTP ----------------

def _who(request: Request) -> str:
    return getattr(request.state, "owner", None) or "anon"


async def _read(f: UploadFile | None, limit: int, what: str) -> bytes | None:
    if f is None or not getattr(f, "filename", None) and not getattr(f, "size", None):
        return None
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{what} ist zu groß (höchstens {limit // 2**20} MB).")
    return data or None


def _view(pid: str, after: int = 0, new=None) -> dict:
    conv = _load(pid) or {}
    evs = events(pid, after)
    out = {"pid": pid, "state": conv.get("state"), "busy": busy(pid) or conv.get("state") in BUSY_STATES,
           "events": evs, "next": max([e["n"] for e in conv.get("events") or []] + [0]),
           "preview": conv.get("preview")}
    if new is not None:
        out["new"] = new
    return out


@router.post("/api/agent/start")
async def api_start(request: Request, photo: UploadFile | None = File(None), audio: UploadFile | None = File(None),
                    text: str | None = Form(None), title: str | None = Form(None)):
    import imaging
    photo_b = await _read(photo, imaging.MAX_UPLOAD, "Das Foto")
    audio_b = await _read(audio, MAX_AUDIO, "Die Sprachnachricht")
    who = _who(request)
    pid = await run_in_threadpool(lambda: start(photo_b, who, "web", text=text, audio=audio_b, title=title))
    return _view(pid)


@router.post("/api/agent/{pid}/message")
async def api_message(pid: str, request: Request, photo: UploadFile | None = File(None),
                      audio: UploadFile | None = File(None), text: str | None = Form(None)):
    import imaging
    photo_b = await _read(photo, imaging.MAX_UPLOAD, "Das Foto")
    audio_b = await _read(audio, MAX_AUDIO, "Die Sprachnachricht")
    who = _who(request)
    new = await run_in_threadpool(lambda: handle(pid, who, "web", text=text, audio=audio_b, photo=photo_b))
    return _view(pid, new[0]["n"] - 1 if new else 0, new)


def _order(pid: str) -> dict:
    """The order, or 404 – and then its conversation goes too (a purge does not know about projects/agent/)."""
    try:
        return projects.load("workshop", pid)
    except HTTPException:
        if re.fullmatch(r"[a-z0-9]{6,32}", str(pid)):
            _forget(pid)
        raise


@router.get("/api/agent/{pid}/events")
def api_events(pid: str, after: int = 0):
    _order(pid)
    return _view(pid, max(0, after))


@router.get("/api/agent/{pid}/preview.png")
def api_preview(pid: str):
    """The latest „So?“ picture (a JPEG, whatever the name says: the SPEC fixed the route)."""
    _order(pid)
    conv = _load(pid) or {}
    rel = conv.get("preview")
    p = (Path(workshop.OUT) / str(rel)).resolve() if rel else None
    if not p or Path(workshop.OUT).resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "Noch keine Vorschau.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/api/agent")
def api_list(limit: int = 50):
    """Conversations, newest first (the Assistent panel)."""
    out = []
    d = _dir()
    for f in sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if d.is_dir() else []:
        conv = _load(f.stem)
        if not conv:
            continue
        try:
            doc = projects.load("workshop", f.stem)
        except HTTPException:
            continue
        last = next((e for e in reversed(conv["events"]) if e.get("role") == "agent"), None)
        out.append({"pid": f.stem, "title": doc.get("title"), "state": conv.get("state"), "updated": conv.get("updated"),
                    "channel": conv.get("channel"), "last": (last or {}).get("text", "")[:200], "thumb": doc.get("thumb")})
        if len(out) >= max(1, min(200, limit)):
            break
    return {"conversations": out}
