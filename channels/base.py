"""Shared plumbing for the front doors (Telegram, e-mail): config and state files, the agent adapter, the worker threads
(poll loop with backoff, outbox sender, event sweep) and turning agent events into text, photos and documents.

A channel never talks to the internet unless the owner entered credentials and switched it on: nothing here starts a
thread on import, and start() is only called by channels.start_all() / the router for enabled, valid configs."""

import os
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import projects

SWEEP_S = 5               # how often mapped orders are checked for events a subscription might have missed
ORDER_TTL_DAYS = 14       # orders older than this (no traffic) are no longer followed
MAX_ORDERS = 200
MAX_REFUSED = 50
MAX_FILE = 45 * 2**20     # largest result file a channel reads into memory to send
BACKOFF_MIN, BACKOFF_MAX = 1.0, 300.0
SEND_TRIES = 3
OUT_KINDS = ("text", "question", "preview", "result", "status", "error")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
GENERIC_ERROR = "Das hat leider nicht geklappt. Bitte versuchen Sie es gleich noch einmal."
clock = time.time


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def store_dir() -> Path:
    """projects/channels/ for per-channel state (resolved at call time so tests can move projects.STORE)."""
    return Path(projects.STORE) / "channels"


def write_private(path, obj) -> None:
    """Atomic JSON write with mode 600 (the temp file already is 600; chmod covers files created by older code)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    projects.write_json(path, obj)
    os.chmod(path, 0o600)


def read_json(path) -> dict:
    raw = projects._read(Path(path))
    return dict(raw) if isinstance(raw, dict) else {}


def scrub(text, *secrets) -> str:
    """Error text without credentials (httpx errors can carry the request URL, which holds the bot token)."""
    s = str(text)
    for x in secrets:
        if x:
            s = s.replace(str(x), "***")
    return s[:500]


class Fatal(Exception):
    """A configuration problem retrying cannot fix (bad token, wrong password): the channel stops and reports it."""


class Retry(Exception):
    """A transient failure; `after` overrides the backoff (e.g. Telegram's retry_after)."""

    def __init__(self, msg: str, after: float | None = None):
        super().__init__(msg)
        self.after = after


# ---------------- agent adapter ----------------

class AgentAdapter:
    """The slice of the agent contract the channels use (SPEC agent.py): start / handle / events / subscribe, plus reading
    result files. Wraps the real `agent` module in the server and a fake object in tests."""

    def __init__(self, agent, out_dir=None):
        self.agent = agent
        self.out_dir = Path(out_dir) if out_dir else None

    def start(self, photo, who, channel, text=None, audio=None) -> str:
        return self.agent.start(photo, who, channel, text=text, audio=audio)

    def handle(self, pid, who, channel, text=None, audio=None, photo=None) -> list:
        return list(self.agent.handle(pid, who, channel, text=text, audio=audio, photo=photo) or [])

    def events(self, pid, after: int = 0) -> list:
        return list(self.agent.events(pid, after=after) or [])

    def subscribe(self, cb):
        """Returns an unsubscribe callable (or None). Channels also sweep events(), so an agent without subscribe works."""
        fn = getattr(self.agent, "subscribe", None)
        return fn(cb) if callable(fn) else None

    def file(self, ref) -> tuple[str, bytes] | None:
        """(filename, bytes) for an event image/file reference: a path relative to /outputs (with or without the
        "/outputs/" prefix) or an absolute path inside it. Anything outside outputs/, missing or too large → None."""
        custom = getattr(self.agent, "file_bytes", None)
        if callable(custom):
            got = custom(ref)
            if got:
                return got
        if not isinstance(ref, str) or not ref or "://" in ref:
            return None
        base = (self.out_dir or Path(projects.OUT)).resolve()
        rel = ref.split("?", 1)[0]
        rel = rel[len("/outputs/"):] if rel.startswith("/outputs/") else rel
        p = Path(rel) if os.path.isabs(rel) else base / rel
        try:
            p = p.resolve()
            p.relative_to(base)
            if not p.is_file() or p.stat().st_size > MAX_FILE:
                return None
            return p.name, p.read_bytes()
        except (ValueError, OSError):
            return None


# ---------------- rendering agent events ----------------

def _refs(v) -> list:
    """Image/file references from a value that may be a string, a dict with a path, or a list of those."""
    out = []
    for x in (v if isinstance(v, list) else [v] if v else []):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            r = x.get("path") or x.get("image") or x.get("file") or x.get("url")
            if isinstance(r, str):
                out.append(r)
    return out


def render(ev: dict, adapter: AgentAdapter, max_images: int = 4) -> dict:
    """{"text", "photos": [(name, bytes)], "documents": [(name, bytes)]} for one agent event. Result events carry
    their images and files in `data` (images/files/pdf/zip/offer_text/links); unknown keys are ignored."""
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    lines = [str(ev.get("text") or "").strip()]
    if ev.get("kind") == "result":
        offer = str(data.get("offer_text") or "").strip()
        if offer:
            lines.append(offer)
    links = [x for x in _refs(data.get("links")) if x.startswith(("http://", "https://"))]
    images = _refs(ev.get("image")) + _refs(data.get("images"))
    files = _refs(data.get("files")) + _refs(data.get("pdf")) + _refs(data.get("zip"))
    photos, docs, missing = [], [], 0
    for ref in dict.fromkeys(images):
        if len(photos) >= max_images:
            break
        got = adapter.file(ref)
        if got:
            photos.append(got)
        elif ref.startswith(("http://", "https://")):
            links.append(ref)
        else:
            missing += 1
    for ref in dict.fromkeys(files):
        got = adapter.file(ref)
        if got:
            (photos if got[0].lower().endswith(IMAGE_EXT) and len(photos) < max_images else docs).append(got)
        elif ref.startswith(("http://", "https://")):
            links.append(ref)
        else:
            missing += 1
    if links:
        lines.append("\n".join(dict.fromkeys(links)))
    if missing:
        lines.append(f"({missing} Datei(en) können hier nicht mitgeschickt werden – bitte im Werkstatt-Programm ansehen.)")
    return {"text": "\n\n".join(x for x in lines if x), "photos": photos, "documents": docs}


def split_text(text: str, limit: int) -> list[str]:
    """Chunks of at most `limit` characters, cut at paragraph or line breaks where possible."""
    text, out = text.strip(), []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        cut = cut if cut > limit // 3 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out + ([text] if text else [])


def user_error(e: Exception) -> str:
    """German detail of an HTTPException from the agent, else a generic line (internals never reach the chat)."""
    detail = getattr(e, "detail", None)
    return detail if isinstance(detail, str) and detail.strip() else GENERIC_ERROR


# ---------------- channel base ----------------

class Channel:
    """One front door. Subclasses set `id`, `label`, SECRETS and implement normalize / validate / online_check /
    poll_once / send. Threads: `poll` (poll_once in a loop with backoff), `send` (outbox) and `sweep` (catch-up)."""

    id = "base"
    label = "Kanal"
    SECRETS: tuple = ()
    TARGETS: tuple = ()     # config fields naming where a secret goes: changing one needs the secret typed again
    coalesce_s = 0.0      # mail waits a moment so several events become one message

    def __init__(self, cfg: dict, agent: AgentAdapter):
        self.cfg = cfg
        self.agent = agent
        self.lock = threading.RLock()
        self.stop_ev = threading.Event()
        self.outbox: queue.Queue = queue.Queue()
        self.threads: list[threading.Thread] = []
        self.state = self.load_state()
        self.info = {"running": False, "since": None, "last_poll": None, "last_error": None, "error_at": None,
                     "fatal": None, "received": 0, "sent": 0}

    # ----- config (classmethods: the router uses them without an instance) -----

    @classmethod
    def defaults(cls) -> dict:
        return {"enabled": False}

    @classmethod
    def normalize(cls, raw) -> dict:
        return {"enabled": bool((raw or {}).get("enabled"))}

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        """German problems that keep this config from working; checked offline, no network."""
        return []

    @classmethod
    def online_check(cls, cfg: dict) -> dict:
        return {"ok": False, "detail": "Keine Online-Prüfung verfügbar."}

    # ----- state -----

    def state_path(self) -> Path:
        return store_dir() / f"{self.id}.json"

    def load_state(self) -> dict:
        s = read_json(self.state_path())
        s.setdefault("orders", {})
        s.setdefault("refused", {})
        return s

    def save_state(self) -> None:
        with self.lock:
            orders = self.state["orders"]
            if len(orders) > MAX_ORDERS:
                for pid in sorted(orders, key=lambda p: orders[p].get("at") or "")[:len(orders) - MAX_ORDERS]:
                    del orders[pid]
            refused = self.state["refused"]
            if len(refused) > MAX_REFUSED:
                for k in sorted(refused, key=lambda k: refused[k].get("at") or "")[:len(refused) - MAX_REFUSED]:
                    del refused[k]
            write_private(self.state_path(), self.state)

    def track(self, pid: str, **info) -> None:
        """Follow an order's agent events from now on (info: where replies go)."""
        with self.lock:
            o = self.state["orders"].setdefault(pid, {"last_n": 0})
            o.update(info, at=now())
        self.save_state()

    def refuse_log(self, key: str, **info) -> bool:
        """Record an unknown sender; True the first time (so the caller answers at most once)."""
        with self.lock:
            first = key not in self.state["refused"]
            entry = self.state["refused"].setdefault(key, {"count": 0, "first": now()})
            entry.update(info, at=now(), count=entry["count"] + 1)
        self.save_state()
        print(f"channels.{self.id}: refused unknown sender {key}", flush=True)
        return first

    # ----- lifecycle -----

    def start(self) -> None:
        self.stop_ev.clear()
        self.info.update(running=True, since=now(), fatal=None)
        for name, fn in (("poll", self._poll_loop), ("send", self._send_loop), ("sweep", self._sweep_loop)):
            t = threading.Thread(target=fn, daemon=True, name=f"channel-{self.id}-{name}")
            self.threads.append(t)
            t.start()

    def stop(self, timeout: float = 5) -> None:
        """Signal every loop and wait briefly. A long poll still in flight is abandoned (daemon thread); it re-checks
        the stop flag before touching anything, and the Telegram offset only advances after processing."""
        self.stop_ev.set()
        self.outbox.put(None)
        end = clock() + timeout
        for t in self.threads:
            t.join(max(0.05, end - clock()))
        self.threads = [t for t in self.threads if t.is_alive()]
        self.info["running"] = False
        try:
            self.save_state()
        except OSError:
            traceback.print_exc()

    @property
    def running(self) -> bool:
        return self.info["running"] and not self.stop_ev.is_set()

    def status(self) -> dict:
        with self.lock:
            return {**self.info, "orders": len(self.state["orders"]), "refused": self.refused_view()}

    def refused_view(self) -> list:
        r = self.state.get("refused", {})
        return [{"key": k, **{f: v.get(f) for f in ("name", "at", "count")}} for k, v in
                sorted(r.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)][:20]

    def error(self, e, fatal: bool = False) -> None:
        msg = scrub(e, *[self.cfg.get(k) for k in self.SECRETS])
        self.info.update(last_error=msg, error_at=now())
        if fatal:
            self.info["fatal"] = msg
        print(f"channels.{self.id}: {'FATAL ' if fatal else ''}{msg}", flush=True)

    # ----- loops -----

    def poll_once(self) -> None:
        raise NotImplementedError

    def idle(self) -> float:
        """Seconds to wait between successful polls (Telegram long-polls, so 0)."""
        return 0.0

    def _poll_loop(self) -> None:
        delay = BACKOFF_MIN
        while not self.stop_ev.is_set():
            try:
                self.poll_once()
                self.info["last_poll"] = now()
                delay = BACKOFF_MIN
                wait = self.idle()
            except Fatal as e:
                self.stop_ev.set()
                self.info["running"] = False
                self.outbox.put(None)
                self.error(e, fatal=True)
                return
            except Retry as e:
                self.error(e)
                wait = e.after if e.after is not None else delay
                delay = min(BACKOFF_MAX, delay * 2)
            except Exception as e:  # network, parse, agent bugs: keep the door open, back off
                self.error(f"{type(e).__name__}: {e}")
                wait, delay = delay, min(BACKOFF_MAX, delay * 2)
            if wait and self.stop_ev.wait(wait):
                return

    def _send_loop(self) -> None:
        while True:
            item = self.outbox.get()
            if item is None or self.stop_ev.is_set():
                return
            batch = [item]
            if self.coalesce_s:
                end = clock() + self.coalesce_s
                while (left := end - clock()) > 0:
                    try:
                        nxt = self.outbox.get(timeout=left)
                    except queue.Empty:
                        break
                    if nxt is None:
                        self.outbox.put(None)
                        break
                    batch.append(nxt)
            by_pid: dict[str, list] = {}
            for pid, ev in batch:
                by_pid.setdefault(pid, []).append(ev)
            for pid, evs in by_pid.items():
                self._send_with_retry(pid, evs)

    def _send_with_retry(self, pid: str, evs: list) -> None:
        delay = BACKOFF_MIN
        for attempt in range(SEND_TRIES):
            try:
                with self.lock:
                    target = dict(self.state["orders"].get(pid) or {})
                if target:
                    k = len(evs)
                    self.send(pid, target, evs)
                    self.info["sent"] += k
                return
            except Fatal as e:
                self.error(e, fatal=True)
                return
            except Exception as e:
                self.error(f"send: {type(e).__name__}: {e}")
                after = getattr(e, "after", None)
                if attempt + 1 < SEND_TRIES and self.stop_ev.wait(after if after is not None else delay):
                    return
                delay *= 2

    def send(self, pid: str, target: dict, events: list) -> None:
        raise NotImplementedError

    def _sweep_loop(self) -> None:
        while not self.stop_ev.wait(SWEEP_S):
            try:
                self.sweep()
            except Exception as e:
                self.error(f"sweep: {type(e).__name__}: {e}")

    def sweep(self, pids=None) -> None:
        """Deliver agent events of followed orders that the subscription has not delivered (yet)."""
        cutoff = (datetime.now() - timedelta(days=ORDER_TTL_DAYS)).isoformat(timespec="seconds")
        with self.lock:
            orders = {p: dict(o) for p, o in self.state["orders"].items()
                      if (pids is None or p in pids) and (o.get("at") or "") >= cutoff}
        for pid, o in orders.items():
            for ev in self.agent.events(pid, after=int(o.get("last_n") or 0)):
                self.deliver(pid, ev)

    def deliver(self, pid: str, ev: dict) -> bool:
        """Queue one agent event for sending, at most once (dedupe by its sequence number `n`)."""
        if not isinstance(ev, dict) or ev.get("role") != "agent" or ev.get("kind") not in OUT_KINDS:
            return False
        try:
            n = int(ev.get("n"))
        except (TypeError, ValueError):
            return False
        with self.lock:
            o = self.state["orders"].get(pid)
            if o is None or n <= int(o.get("last_n") or 0):
                return False
            o["last_n"] = n
            o["at"] = now()
        self.outbox.put((pid, ev))
        try:
            self.save_state()
        except OSError:
            traceback.print_exc()
        return True
