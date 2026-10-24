"""Front doors to the agent (Telegram, e-mail): config in projects/channels.json (mode 600), the settings API and the
lifecycle for the server lifespan.

Everything is OFF by default. A channel only connects to the internet when the owner has entered credentials AND
switched it on; start_all() skips everything else, and /check validates offline unless the channel is enabled.
Secrets are write-only: GET shows {"set": true/false}, PUT keeps a secret unless a new value (or null to clear) is sent;
a changed server address or user drops the stored secret (it is never sent anywhere it was not entered for).
Switching a channel on needs a team password: the settings routes are otherwise open to every device on the LAN."""

import threading
import traceback
from pathlib import Path

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

import projects

from .base import AgentAdapter, Channel, read_json, write_private
from .mail import MailChannel
from .telegram import TelegramChannel

router = APIRouter()
KINDS: dict[str, type[Channel]] = {"telegram": TelegramChannel, "mail": MailChannel}
WARNING = ("Ein eingeschalteter Kanal verbindet die Werkstatt mit dem Internet (ausgehend: Telegram-Server bzw. Ihr "
           "Mail-Server). Nur freigegebene Chats/Absender werden bedient.")
_LOCK = threading.RLock()
_RUN: dict = {"agent": None, "channels": {}, "unsubscribe": None}
NEED_PASSWORD = ("Bitte zuerst ein Team-Passwort setzen (Einstellungen → Zugang). Ohne Passwort könnte jedes Gerät im "
                 "WLAN die Werkstatt mit dem Internet verbinden.")


def password_set() -> bool:
    return bool(projects.settings()["access"]["password"])


def config_path() -> Path:
    return Path(projects.STORE) / "channels.json"


def load() -> dict:
    """Full normalized config incl. secrets (server-side only)."""
    raw = read_json(config_path())
    return {cid: cls.normalize({**cls.defaults(), **(raw.get(cid) or {})}) for cid, cls in KINDS.items()}


def save(cfg: dict) -> None:
    write_private(config_path(), cfg)


def merge(cid: str, current: dict, patch) -> dict:
    """Apply a partial update; secrets: missing / "" / {"set": …} keep the stored value, null clears it. When a
    target field (server, port, user) changes, a stored secret is kept only if a new one comes with the patch."""
    if not isinstance(patch, dict):
        raise HTTPException(400, f"Ungültige Einstellungen für {KINDS[cid].label}.")
    cls = KINDS[cid]
    new = dict(current)
    for k, v in patch.items():
        if k not in new:
            continue
        if k in cls.SECRETS:
            if v is None:
                new[k] = ""
            elif isinstance(v, str) and v.strip():
                new[k] = v.strip()
            continue
        new[k] = v
    new = cls.normalize(new)
    if any(new.get(k) != current.get(k) for k in cls.TARGETS):
        for k in cls.SECRETS:
            if not (isinstance(patch.get(k), str) and patch[k].strip()):
                new[k] = ""
    return new


def public(cfg: dict) -> dict:
    out = {}
    for cid, cls in KINDS.items():
        c = {k: ({"set": bool(v)} if k in cls.SECRETS else v) for k, v in cfg[cid].items()}
        ch = _RUN["channels"].get(cid)
        c["label"] = cls.label
        c["issues"] = cls.validate(cfg[cid])
        c["status"] = ch.status() if ch else {"running": False, "refused": _refused(cid)}
        out[cid] = c
    return out


def _refused(cid: str) -> list:
    """Unknown senders logged by an earlier run (so the owner can allowlist a chat id while the channel is off)."""
    r = read_json(Path(projects.STORE) / "channels" / f"{cid}.json").get("refused") or {}
    return [{"key": k, **{f: v.get(f) for f in ("name", "at", "count")}} for k, v in
            sorted(r.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)][:20]


# ---------------- lifecycle ----------------

def _dispatch(*args) -> None:
    """agent.subscribe callback: (pid, event) or (event with "pid"). Hands the event to the channel following the order;
    must be quick and never raise into the agent."""
    try:
        pid, ev = (args[0], args[1]) if len(args) >= 2 else ((args[0] or {}).get("pid"), args[0])
        for ch in list(_RUN["channels"].values()):
            if ch.running:
                ch.deliver(pid, ev)
    except Exception:
        traceback.print_exc()


def _start(cid: str, cfg: dict) -> bool:
    cls = KINDS[cid]
    if not cfg["enabled"] or cls.validate(cfg) or _RUN["agent"] is None:
        return False
    if not password_set():
        print(f"channels: {cid} not started – no team password set", flush=True)
        return False
    ch = cls(cfg, _RUN["agent"])
    ch.start()
    _RUN["channels"][cid] = ch
    print(f"channels: {cid} started", flush=True)
    return True


def _stop(cid: str, timeout: float = 5) -> None:
    ch = _RUN["channels"].pop(cid, None)
    if ch:
        ch.stop(timeout)
        print(f"channels: {cid} stopped", flush=True)


def start_all(agent, out_dir=None) -> list[str]:
    """Server lifespan: start every enabled, valid channel. Returns the ids started (usually none)."""
    with _LOCK:
        _RUN["agent"] = agent if isinstance(agent, AgentAdapter) else AgentAdapter(agent, out_dir)
        if _RUN["unsubscribe"] is None:
            _RUN["unsubscribe"] = _RUN["agent"].subscribe(_dispatch) or (lambda: None)
        cfg = load()
        return [cid for cid in KINDS if cid not in _RUN["channels"] and _start(cid, cfg[cid])]


def stop_all(timeout: float = 5) -> None:
    with _LOCK:
        for cid in list(_RUN["channels"]):
            _stop(cid, timeout)
        unsub, _RUN["unsubscribe"], _RUN["agent"] = _RUN["unsubscribe"], None, None
        if callable(unsub):
            try:
                unsub()
            except Exception:
                traceback.print_exc()


def status() -> dict:
    return {cid: ch.status() for cid, ch in _RUN["channels"].items()}


def _restart(cid: str, cfg: dict) -> None:
    """After a config change: stop the old instance, start a new one if enabled (only once start_all has run)."""
    with _LOCK:
        _stop(cid, 2)
        _start(cid, cfg)


# ---------------- API ----------------

@router.get("/api/channels")
def get_channels():
    return {"channels": public(load()), "warning": WARNING, "password_set": password_set()}


@router.put("/api/channels")
async def put_channels(body: dict):
    def work():
        with _LOCK:
            cfg = load()
            changed = []
            for cid, patch in body.items():
                if cid not in KINDS:
                    raise HTTPException(400, f"Unbekannter Kanal: {str(cid)[:40]}")
                new = merge(cid, cfg[cid], patch)
                issues = KINDS[cid].validate(new)
                if new["enabled"] and issues:
                    raise HTTPException(400, f"{KINDS[cid].label} kann nicht eingeschaltet werden: " + " ".join(issues))
                if new["enabled"] and new != cfg[cid] and not password_set():
                    raise HTTPException(409, f"{KINDS[cid].label} kann nicht eingeschaltet werden. " + NEED_PASSWORD)
                if new != cfg[cid]:
                    cfg[cid] = new
                    changed.append(cid)
            save(cfg)
            for cid in changed:
                _restart(cid, cfg[cid])
            return {"channels": public(cfg), "warning": WARNING, "password_set": password_set()}
    return await run_in_threadpool(work)


@router.post("/api/channels/{cid}/check")
async def check_channel(cid: str, body: dict | None = None):
    """Validates `body` (unsaved form values over the saved config) offline. Only the SAVED config of a channel that is
    switched on is ever tested online: a body can neither switch the test online nor point the stored secret at another
    server."""
    if cid not in KINDS:
        raise HTTPException(404, "Unbekannter Kanal.")
    cls = KINDS[cid]
    saved = load()[cid]
    cfg = merge(cid, saved, {k: v for k, v in body.items() if k != "enabled"}) if body else saved
    issues = cls.validate(cfg)
    online = None
    if saved["enabled"] and cfg == saved and not issues:
        online = await run_in_threadpool(cls.online_check, saved)
    note = None
    if not saved["enabled"]:
        note = "Nur offline geprüft: der Kanal ist ausgeschaltet."
    elif cfg != saved:
        note = "Nur offline geprüft: erst speichern, dann wird die Verbindung getestet."
    return {"ok": not issues and (online is None or online["ok"]), "issues": issues, "online": online,
            "enabled": saved["enabled"], "note": note}
