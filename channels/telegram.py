"""Telegram front door: Bot API long polling (outbound HTTPS only, no webhook, no listening port).

Only chats on the allowlist are served. An unknown chat gets one polite refusal and is logged (the owner sees its id in
the settings and can add it); everything it sends is ignored. Each chat has a current order: photo, voice note or text
goes to agent.start (no current order) or agent.handle; the agent's events come back as messages, photos, documents."""

import re

import httpx

from .base import (AgentAdapter, Channel, Fatal, Retry, render, scrub, split_text, user_error)

API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"\d{5,16}:[A-Za-z0-9_-]{30,64}")
MAX_DOWNLOAD = 20 * 2**20     # the Bot API serves files up to 20 MB
MAX_PHOTO = 10 * 2**20        # sendPhoto limit; bigger images go out as documents
TEXT_LIMIT, CAPTION_LIMIT = 4000, 1000
POLL_TIMEOUT = 25

WELCOME = ("Guten Tag! Ich bin der Werkstatt-Assistent.\n\n"
           "Schicken Sie ein Foto des Raums und beschreiben Sie per Sprachnachricht oder Text, was hinein soll – "
           "z. B. „links neben dem Fenster ein Einbauschrank, zwei-zwanzig breit, raumhoch, Weißlack“.\n\n"
           "/neu – neuen Auftrag beginnen\n/status – Stand des aktuellen Auftrags\n/hilfe – diese Hilfe")
REFUSAL = ("Entschuldigung, dieser Chat ist für den Werkstatt-Assistenten nicht freigeschaltet. "
           "Bitte wenden Sie sich an die Schreinerei.")
NEW_ORDER = "Alles klar, ein neuer Auftrag. Schicken Sie ein Foto des Raums und sagen Sie, was hinein soll."
NO_ORDER = "Es läuft gerade kein Auftrag. Schicken Sie ein Foto des Raums, um zu beginnen."
UNSUPPORTED = "Das kann ich leider nicht verarbeiten. Bitte schicken Sie ein Foto, eine Sprachnachricht oder Text."


def make_client(timeout: float) -> httpx.Client:
    """Tests replace this with a client on httpx.MockTransport."""
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False)


def _chat_entry(v) -> dict | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, str)):
        v = {"id": v}
    if not isinstance(v, dict):
        return None
    try:
        cid = int(str(v.get("id")).strip())
    except (TypeError, ValueError):
        return None
    return {"id": cid, "name": str(v.get("name") or "").strip()[:80], "who": str(v.get("who") or "").strip()[:80]}


class TelegramChannel(Channel):
    id = "telegram"
    label = "Telegram"
    SECRETS = ("token",)

    def __init__(self, cfg: dict, agent: AgentAdapter):
        super().__init__(cfg, agent)
        self.client = make_client(cfg["poll_timeout"] + 15)
        self.state.setdefault("offset", 0)
        self.state.setdefault("chats", {})

    # ----- config -----

    @classmethod
    def defaults(cls) -> dict:
        return {"enabled": False, "token": "", "allowed_chats": [], "poll_timeout": POLL_TIMEOUT, "who": ""}

    @classmethod
    def normalize(cls, raw) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        chats, seen = [], set()
        for v in (raw.get("allowed_chats") if isinstance(raw.get("allowed_chats"), list) else [])[:100]:
            e = _chat_entry(v)
            if e and e["id"] not in seen:
                seen.add(e["id"])
                chats.append(e)
        try:
            pt = min(50, max(5, int(raw.get("poll_timeout") or POLL_TIMEOUT)))
        except (TypeError, ValueError):
            pt = POLL_TIMEOUT
        return {"enabled": bool(raw.get("enabled")), "token": str(raw.get("token") or "").strip()[:100],
                "allowed_chats": chats, "poll_timeout": pt, "who": str(raw.get("who") or "").strip()[:80]}

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        issues = []
        if not cfg["token"]:
            issues.append("Bot-Token fehlt (von @BotFather).")
        elif not TOKEN_RE.fullmatch(cfg["token"]):
            issues.append("Bot-Token hat nicht das erwartete Format (Zahl:Zeichenfolge).")
        if not cfg["allowed_chats"]:
            issues.append("Keine freigegebenen Chats: ohne Chat-ID wird niemand bedient.")
        return issues

    @classmethod
    def online_check(cls, cfg: dict) -> dict:
        """getMe with the token: is it valid, and which bot is it? (Only called for an enabled channel.)"""
        try:
            with make_client(20) as c:
                r = c.post(f"{API}/bot{cfg['token']}/getMe")
                body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "detail": f"Telegram nicht erreichbar: {scrub(type(e).__name__, cfg['token'])}"}
        if not body.get("ok"):
            return {"ok": False, "detail": "Telegram lehnt den Token ab." if r.status_code in (401, 404)
                    else f"Telegram-Fehler {r.status_code}."}
        me = body.get("result") or {}
        return {"ok": True, "detail": f"Verbunden als @{me.get('username') or '?'}.", "bot": me.get("username")}

    # ----- Bot API -----

    def call(self, method: str, data: dict | None = None, files: dict | None = None, timeout: float | None = None):
        token = self.cfg["token"]
        try:
            r = self.client.post(f"{API}/bot{token}/{method}", data=data if files else None,
                                 json=None if files else data, files=files,
                                 timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.HTTPError as e:
            raise Retry(f"{method}: {type(e).__name__}: {scrub(e, token)}")
        try:
            body = r.json()
        except ValueError:
            raise Retry(f"{method}: HTTP {r.status_code}, keine JSON-Antwort")
        if body.get("ok"):
            return body.get("result")
        code, desc = body.get("error_code") or r.status_code, scrub(body.get("description") or "", token)
        if code in (401, 404):
            raise Fatal("Telegram lehnt den Bot-Token ab (401/404). Bitte Token prüfen.")
        if code == 429:
            raise Retry(f"{method}: zu viele Anfragen", after=float((body.get("parameters") or {}).get("retry_after") or 5))
        if code == 409:  # a webhook is set or another poller uses this token
            raise Retry(f"{method}: Konflikt (409) – läuft der Bot noch woanders? {desc}", after=30)
        if code in (400, 403) and method != "getUpdates":  # bad request / bot blocked: repeating cannot help
            raise _Dropped(f"{method}: Fehler {code} {desc}")
        raise Retry(f"{method}: Fehler {code} {desc}")

    def download(self, file_id: str) -> bytes:
        f = self.call("getFile", {"file_id": file_id}) or {}
        if int(f.get("file_size") or 0) > MAX_DOWNLOAD or not f.get("file_path"):
            raise _TooBig()
        try:
            r = self.client.get(f"{API}/file/bot{self.cfg['token']}/{f['file_path']}", timeout=60)
        except httpx.HTTPError as e:
            raise Retry(f"download: {type(e).__name__}: {scrub(e, self.cfg['token'])}")
        if r.status_code != 200:
            raise Retry(f"download: HTTP {r.status_code}")
        if len(r.content) > MAX_DOWNLOAD:
            raise _TooBig()
        return r.content

    def say(self, chat_id: int, text: str) -> None:
        for part in split_text(text, TEXT_LIMIT):
            self.call("sendMessage", {"chat_id": chat_id, "text": part})

    # ----- incoming -----

    def idle(self) -> float:
        return 0.0

    def poll_once(self) -> None:
        updates = self.call("getUpdates", {"offset": self.state["offset"], "timeout": self.cfg["poll_timeout"],
                                           "allowed_updates": ["message"]}) or []
        for u in updates:
            if self.stop_ev.is_set():  # an abandoned long poll returning late: leave the offset for the next start
                return
            try:
                self.on_update(u)
            except (Retry, Fatal):
                raise  # offset not advanced: the update comes again after the backoff
            except Exception as e:  # one broken update must not block the queue forever
                self.error(f"update {u.get('update_id')}: {type(e).__name__}: {e}")
            with self.lock:
                self.state["offset"] = max(self.state["offset"], int(u.get("update_id") or 0) + 1)
            self.save_state()

    def allowed(self, chat_id) -> dict | None:
        return next((c for c in self.cfg["allowed_chats"] if c["id"] == chat_id), None)

    def on_update(self, u: dict) -> None:
        msg = u.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("chat"), dict):
            return
        chat = msg["chat"]
        chat_id = chat.get("id")
        entry = self.allowed(chat_id)
        if entry is None:
            name = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip()
            if chat.get("username"):
                name = f"{name} @{chat['username']}".strip()
            if self.refuse_log(str(chat_id), name=name[:80] or str(chat.get("title") or "")[:80],
                               type=chat.get("type"), text=str(msg.get("text") or "")[:80]):
                try:
                    self.call("sendMessage", {"chat_id": chat_id, "text": REFUSAL})
                except (Retry, _Dropped) as e:
                    self.error(f"refusal: {e}")
            return
        self.info["received"] += 1
        who = entry["who"] or self.cfg["who"] or "telegram"
        text = str(msg.get("text") or msg.get("caption") or "").strip()[:4000]
        if text.startswith("/"):
            return self.command(chat_id, text)
        photo = audio = None
        try:
            if msg.get("photo"):
                best = max(msg["photo"], key=lambda p: (p.get("width", 0) * p.get("height", 0), p.get("file_size", 0)))
                photo = self.download(best["file_id"])
            elif isinstance(msg.get("document"), dict) and str(msg["document"].get("mime_type", "")).startswith("image/"):
                photo = self.download(msg["document"]["file_id"])  # sent as file: full resolution, best for measuring
            for k in ("voice", "audio"):
                if isinstance(msg.get(k), dict):
                    audio = self.download(msg[k]["file_id"])
                    break
        except _TooBig:
            return self.say(chat_id, "Die Datei ist zu groß (höchstens 20 MB).")
        except _Dropped as e:
            self.error(f"download: {e}")
            return self.say(chat_id, "Die Datei konnte nicht geladen werden. Bitte noch einmal schicken.")
        if photo is None and audio is None and not text:
            return self.say(chat_id, UNSUPPORTED)
        self.to_agent(chat_id, who, text or None, audio, photo)

    def to_agent(self, chat_id: int, who: str, text, audio, photo) -> None:
        key = str(chat_id)
        with self.lock:
            pid = (self.state["chats"].get(key) or {}).get("pid")
        try:
            if pid:
                for ev in self.agent.handle(pid, who, "telegram", text=text, audio=audio, photo=photo):
                    self.deliver(pid, ev)
            else:
                pid = self.agent.start(photo, who, "telegram", text=text, audio=audio)
                with self.lock:
                    self.state["chats"][key] = {"pid": pid}
                self.track(pid, chat=chat_id)
                self.sweep([pid])  # events appended before the mapping existed
        except Exception as e:
            self.error(f"agent: {type(e).__name__}: {e}")
            self.say(chat_id, user_error(e))

    def command(self, chat_id: int, text: str) -> None:
        cmd = text.split()[0][1:].split("@")[0].lower()
        key = str(chat_id)
        if cmd == "neu":
            with self.lock:
                self.state["chats"][key] = {"pid": None}
            self.save_state()
            return self.say(chat_id, NEW_ORDER)
        if cmd == "status":
            with self.lock:
                pid = (self.state["chats"].get(key) or {}).get("pid")
            if not pid:
                return self.say(chat_id, NO_ORDER)
            try:
                evs = [e for e in self.agent.events(pid) if e.get("role") == "agent" and e.get("text")]
            except Exception as e:
                return self.say(chat_id, user_error(e))
            last = evs[-1]["text"] if evs else "Der Auftrag wird vorbereitet."
            return self.say(chat_id, f"Auftrag {pid}:\n{last}")
        self.say(chat_id, WELCOME)  # /start, /hilfe, /help and anything unknown

    # ----- outgoing -----

    def send(self, pid: str, target: dict, events: list) -> None:
        chat_id = target.get("chat")
        if chat_id is None or self.allowed(chat_id) is None:  # removed from the allowlist since: stay silent
            return
        while events:  # sent events leave the list, so a retry after a network error does not repeat them
            out = render(events[0], self.agent)
            try:
                self.send_rendered(chat_id, out)
            except _Dropped as e:
                self.error(f"chat {chat_id}: {e}")
            events.pop(0)

    def send_rendered(self, chat_id: int, out: dict) -> None:
        text = out["text"]
        photos = out["photos"]
        if len(photos) == 1 and len(text) <= CAPTION_LIMIT and photos[0][1] and len(photos[0][1]) <= MAX_PHOTO:
            self._photo(chat_id, photos[0], text)
        else:
            if text:
                self.say(chat_id, text)
            for p in photos:
                self._photo(chat_id, p, "")
        for name, data in out["documents"]:
            self.call("sendDocument", {"chat_id": chat_id}, files={"document": (name, data)}, timeout=120)

    def _photo(self, chat_id: int, photo: tuple, caption: str) -> None:
        name, data = photo
        data_ = {"chat_id": chat_id, **({"caption": caption} if caption else {})}
        if len(data) > MAX_PHOTO:
            self.call("sendDocument", data_, files={"document": (name, data)}, timeout=120)
        else:
            self.call("sendPhoto", data_, files={"photo": (name, data)}, timeout=120)

    def stop(self, timeout: float = 5) -> None:
        super().stop(timeout)
        if not self.threads:  # never close the client under a poll that is still running
            self.client.close()


class _TooBig(Exception):
    pass


class _Dropped(Exception):
    """Telegram rejected this one request (400 bad request, 403 bot blocked): retrying would only repeat the error."""
