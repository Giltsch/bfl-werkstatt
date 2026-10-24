"""E-mail front door: IMAP (SSL) polling of one inbox folder, SMTP replies (SSL or STARTTLS). Outbound connections only.

Only mails from allowlisted senders are read (address or "@domain"); others stay untouched and unread in the mailbox and
are logged once. A mail's first photo attachment, its text (quotes and signature cut off) and an audio attachment (voice
memo) go to the agent; the order is identified by the "[W-<pid>]" tag in the subject or by In-Reply-To/References, else a
new order starts. Agent events are collected for a few seconds and sent as one reply with images and PDFs attached.
Processed mails are flagged \\Seen (and moved to `processed_folder` if set)."""

import email
import html
import imaplib
import re
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

from .base import AgentAdapter, Channel, Fatal, Retry, render, scrub, user_error

MAX_PER_POLL = 10
MAX_PART = 25 * 2**20
MAX_ATTACH_TOTAL = 20 * 2**20     # most providers reject mails above ~25 MB after base64
MAX_SEEN = 5000
TAG_RE = re.compile(r"\[W-([0-9a-z]{6,40})\]")
ADDR_RE = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")
AUDIO_EXT = (".m4a", ".ogg", ".oga", ".opus", ".mp3", ".wav", ".webm", ".aac", ".amr", ".3gp")
QUOTE_HEAD = re.compile(r"^(am .+ schrieb .+:|on .+ wrote:|-{2,}\s*(original|ursprüngliche).*|von:\s.+|from:\s.+)$", re.I)
NEW_WORDS = {"neu", "/neu", "neuer auftrag"}


def connect_imap(cfg: dict):
    """Tests replace imaplib.IMAP4_SSL; the context verifies the server certificate."""
    return imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=ssl.create_default_context(), timeout=30)


def connect_smtp(cfg: dict):
    ctx = ssl.create_default_context()
    if cfg["smtp_security"] == "starttls":
        s = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
        s.starttls(context=ctx)
        return s
    return smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx, timeout=30)


def _host(v) -> str:
    v = str(v or "").strip().lower()[:200]
    return v if re.fullmatch(r"[a-z0-9.-]+", v or "-") else ""


def _port(v, default: int) -> int:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return default
    return p if 0 < p < 65536 else default


def _sender_entry(v) -> str:
    v = str(v or "").strip().lower()[:200]
    if v.startswith("@") and re.fullmatch(r"@[a-z0-9.-]+\.[a-z]{2,}", v):
        return v
    return v if ADDR_RE.fullmatch(v) else ""


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def body_text(msg) -> str:
    """The new text of a mail: plain part (or de-tagged HTML) without quoted history and signature."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s == "--" or line.rstrip() == "-- " or QUOTE_HEAD.match(s):
            break
        if s.startswith(">"):
            continue
        lines.append(re.sub(r"[ \t]+", " ", s))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:4000]


def attachments(msg) -> tuple[list[bytes], list[bytes]]:
    """(photos, audios) as bytes, in mail order; inline images count (phones often embed the photo)."""
    photos, audios = [], []
    body = msg.get_body(preferencelist=("plain", "html"))
    for part in msg.walk():
        if part.is_multipart() or part is body:
            continue
        ctype, name = part.get_content_type(), str(part.get_filename() or "").lower()
        if ctype.startswith("image/"):
            target = photos
        elif ctype.startswith("audio/") or name.endswith(AUDIO_EXT):
            target = audios
        else:
            continue
        data = part.get_payload(decode=True) or b""
        if 0 < len(data) <= MAX_PART:
            target.append(data)
    return photos, audios


def is_auto(msg) -> bool:
    """Auto-replies, bounces and lists: never answered (mail loops)."""
    auto = str(msg.get("Auto-Submitted") or "no").strip().lower()
    prec = str(msg.get("Precedence") or "").strip().lower()
    return (auto != "no" or prec in ("bulk", "junk", "list", "auto_reply") or bool(msg.get("List-Id"))
            or bool(msg.get("X-Autoreply")) or str(msg.get("Return-Path") or "").strip() == "<>")


class MailChannel(Channel):
    id = "mail"
    label = "E-Mail"
    SECRETS = ("password",)
    TARGETS = ("imap_host", "imap_port", "smtp_host", "smtp_port", "user")
    coalesce_s = 8.0

    def __init__(self, cfg: dict, agent: AgentAdapter):
        super().__init__(cfg, agent)
        self.state.setdefault("uidvalidity", None)
        self.state.setdefault("seen", [])
        self.state.setdefault("threads", {})

    # ----- config -----

    @classmethod
    def defaults(cls) -> dict:
        return {"enabled": False, "imap_host": "", "imap_port": 993, "smtp_host": "", "smtp_port": 465,
                "smtp_security": "ssl", "user": "", "password": "", "from_addr": "", "folder": "INBOX",
                "processed_folder": "", "allowed_senders": [], "poll_s": 60, "who": ""}

    @classmethod
    def normalize(cls, raw) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        senders = []
        for v in (raw.get("allowed_senders") if isinstance(raw.get("allowed_senders"), list) else [])[:200]:
            e = _sender_entry(v)
            if e and e not in senders:
                senders.append(e)
        sec = raw.get("smtp_security") if raw.get("smtp_security") in ("ssl", "starttls") else "ssl"
        try:
            poll = min(3600, max(30, int(raw.get("poll_s") or 60)))
        except (TypeError, ValueError):
            poll = 60
        folder = re.sub(r"[\x00-\x1f\x7f]", "", str(raw.get("folder") or "INBOX")).strip()[:100] or "INBOX"
        done = re.sub(r"[\x00-\x1f\x7f]", "", str(raw.get("processed_folder") or "")).strip()[:100]
        return {"enabled": bool(raw.get("enabled")), "imap_host": _host(raw.get("imap_host")),
                "imap_port": _port(raw.get("imap_port"), 993), "smtp_host": _host(raw.get("smtp_host")),
                "smtp_port": _port(raw.get("smtp_port"), 465 if sec == "ssl" else 587), "smtp_security": sec,
                "user": str(raw.get("user") or "").strip()[:200], "password": str(raw.get("password") or "")[:500],
                "from_addr": str(raw.get("from_addr") or "").strip()[:200], "folder": folder,
                "processed_folder": done if done != folder else "", "allowed_senders": senders, "poll_s": poll,
                "who": str(raw.get("who") or "").strip()[:80]}

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        issues = []
        for k, label in (("imap_host", "IMAP-Server"), ("smtp_host", "SMTP-Server"), ("user", "Benutzername"),
                         ("password", "Passwort")):
            if not cfg[k]:
                issues.append(f"{label} fehlt.")
        sender = cls.from_addr(cfg)
        if not ADDR_RE.fullmatch(sender):
            issues.append("Absenderadresse fehlt oder ist ungültig (Benutzername ist keine E-Mail-Adresse).")
        if not cfg["allowed_senders"]:
            issues.append("Keine freigegebenen Absender: ohne Liste wird keine Mail gelesen.")
        if sender.lower() in cfg["allowed_senders"]:
            issues.append("Die eigene Adresse darf nicht freigegeben sein (Mail-Schleife).")
        if cfg["smtp_security"] == "ssl" and cfg["smtp_port"] == 587:
            issues.append("Port 587 nutzt normalerweise STARTTLS, nicht SSL.")
        return issues

    @classmethod
    def from_addr(cls, cfg: dict) -> str:
        return cfg["from_addr"] or cfg["user"]

    @classmethod
    def online_check(cls, cfg: dict) -> dict:
        """IMAP login + select and SMTP login (no mail is read or sent). Only called for an enabled channel."""
        secret = cfg["password"]
        try:
            m = connect_imap(cfg)
            try:
                m.login(cfg["user"], secret)
                typ, _ = m.select(_quote(cfg["folder"]), readonly=True)
                if typ != "OK":
                    return {"ok": False, "detail": f"Ordner „{cfg['folder']}“ nicht gefunden."}
            finally:
                _safe_logout(m)
        except imaplib.IMAP4.error as e:
            return {"ok": False, "detail": f"IMAP-Anmeldung fehlgeschlagen: {scrub(e, secret)}"}
        except (OSError, ssl.SSLError) as e:
            return {"ok": False, "detail": f"IMAP-Server nicht erreichbar: {scrub(type(e).__name__, secret)}"}
        try:
            s = connect_smtp(cfg)
            try:
                s.login(cfg["user"], secret)
            finally:
                _safe_quit(s)
        except smtplib.SMTPAuthenticationError:
            return {"ok": False, "detail": "SMTP-Anmeldung fehlgeschlagen."}
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            return {"ok": False, "detail": f"SMTP-Server nicht erreichbar: {scrub(type(e).__name__, secret)}"}
        return {"ok": True, "detail": "IMAP und SMTP: Anmeldung erfolgreich."}

    # ----- incoming -----

    def idle(self) -> float:
        return float(self.cfg["poll_s"])

    def allowed(self, addr: str) -> bool:
        addr = addr.lower()
        return any(addr == e or (e.startswith("@") and addr.endswith(e)) for e in self.cfg["allowed_senders"])

    def poll_once(self) -> None:
        try:
            m = connect_imap(self.cfg)
        except (OSError, ssl.SSLError) as e:
            raise Retry(f"IMAP: {type(e).__name__}: {e}")
        try:
            try:
                m.login(self.cfg["user"], self.cfg["password"])
            except imaplib.IMAP4.error as e:
                raise Fatal(f"IMAP-Anmeldung fehlgeschlagen: {e}")
            typ, _ = m.select(_quote(self.cfg["folder"]))
            if typ != "OK":
                raise Fatal(f"IMAP-Ordner „{self.cfg['folder']}“ nicht gefunden.")
            uv = (m.response("UIDVALIDITY")[1] or [None])[0]
            uv = uv.decode() if isinstance(uv, bytes) else uv
            with self.lock:
                if uv != self.state["uidvalidity"]:  # mailbox rebuilt: old UIDs mean nothing any more
                    self.state.update(uidvalidity=uv, seen=[])
                seen = set(self.state["seen"])
            typ, data = m.uid("SEARCH", None, "UNSEEN")
            uids = [u.decode() for u in (data[0] or b"").split()] if typ == "OK" and data else []
            for uid in [u for u in uids if u not in seen][:MAX_PER_POLL]:
                if self.stop_ev.is_set():
                    return
                self.fetch_one(m, uid)
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError) as e:
            raise Retry(f"IMAP: {type(e).__name__}: {e}")
        finally:
            _safe_logout(m)

    def fetch_one(self, m, uid: str) -> None:
        typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: unknown senders' mails stay unread
        raw = next((x[1] for x in (data or []) if isinstance(x, tuple) and len(x) > 1), None)
        if typ != "OK" or not raw:
            return
        msg = email.message_from_bytes(raw, policy=policy.default)
        handled = self.on_mail(msg)
        if handled:
            m.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            done = self.cfg["processed_folder"]
            if done:
                if "MOVE" in getattr(m, "capabilities", ()):
                    m.uid("MOVE", uid, _quote(done))
                elif m.uid("COPY", uid, _quote(done))[0] == "OK":
                    m.uid("STORE", uid, "+FLAGS", "(\\Deleted)")  # no expunge: it would also purge others' deletions
        with self.lock:
            self.state["seen"] = (self.state["seen"] + [uid])[-MAX_SEEN:]
        self.save_state()

    def on_mail(self, msg) -> bool:
        """True when the mail was handed to the agent (→ marked processed); unknown senders → False (stays unread)."""
        froms = [a for _, a in getaddresses([str(x) for x in msg.get_all("From", [])]) if a]
        addr = froms[0].lower() if len(froms) == 1 else ""
        own = self.from_addr(self.cfg).lower()
        if not addr or addr == own or is_auto(msg):
            return False
        if not self.allowed(addr):
            self.refuse_log(addr, name=parseaddr(str(msg.get("From") or ""))[0][:80],
                            text=str(msg.get("Subject") or "")[:80])
            return False
        self.info["received"] += 1
        subject = re.sub(r"[\r\n]+", " ", str(msg.get("Subject") or "")).strip()[:200]
        text = body_text(msg)
        photos, audios = attachments(msg)
        photo, audio = (photos[0] if photos else None), (audios[0] if audios else None)
        msgid = str(msg.get("Message-ID") or "").strip()[:300]
        refs = (str(msg.get("References") or "") + " " + str(msg.get("In-Reply-To") or "")).split()
        fresh = subject.lower().strip(" :") in NEW_WORDS or text.lower().split("\n")[0].strip(" .!") in NEW_WORDS
        pid = None if fresh else self.order_for(addr, subject, refs)
        if fresh and text.lower().split("\n")[0].strip(" .!") in NEW_WORDS:
            text = "\n".join(text.split("\n")[1:]).strip()
        if photo is None and audio is None and not text:
            return True
        who = self.cfg["who"] or "mail"
        try:
            if pid:
                with self.lock:
                    o = self.state["orders"][pid]
                    o.update(msgid=msgid or o.get("msgid"), refs=(o.get("refs", []) + ([msgid] if msgid else []))[-20:])
                self.save_state()
                for ev in self.agent.handle(pid, who, "mail", text=text or None, audio=audio, photo=photo):
                    self.deliver(pid, ev)
            else:
                pid = self.agent.start(photo, who, "mail", text=text or None, audio=audio)
                base = re.sub(r"^((re|aw|fwd?|wg)\s*:\s*)+", "", subject, flags=re.I).strip() or "Ihr Auftrag"
                self.track(pid, to=addr, subject=base, msgid=msgid, refs=[msgid] if msgid else [])
                if msgid:
                    with self.lock:
                        self.state["threads"][msgid] = pid
                self.sweep([pid])
            if len(photos) > 1:
                self.notice(pid, "Es wurde nur das erste Foto übernommen.")
        except Exception as e:
            self.error(f"agent: {type(e).__name__}: {e}")
            self.reply_error(addr, subject, msgid, user_error(e))
        return True

    def order_for(self, addr: str, subject: str, refs: list) -> str | None:
        """The followed order this mail belongs to (only if it was started by the same sender)."""
        with self.lock:
            orders, threads = self.state["orders"], self.state["threads"]
            cands = [m.group(1) for m in TAG_RE.finditer(subject)] + [threads.get(r) for r in reversed(refs)]
            return next((p for p in cands if p and p in orders and orders[p].get("to") == addr), None)

    def notice(self, pid: str, text: str) -> None:
        self.outbox.put((pid, {"role": "agent", "kind": "text", "text": text}))

    # ----- outgoing -----

    def compose(self, to: str, subject: str, in_reply: str, refs: list, text: str, files: list) -> EmailMessage:
        own = self.from_addr(self.cfg)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = own, to, subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=own.rsplit("@", 1)[-1] if "@" in own else None)
        msg["Auto-Submitted"] = "auto-replied"   # other robots must not answer us
        if in_reply:
            msg["In-Reply-To"] = in_reply
        if refs:
            msg["References"] = " ".join(refs[-10:])
        total, left_out = 0, 0
        attach = []
        for name, data in files:
            if total + len(data) > MAX_ATTACH_TOTAL:
                left_out += 1
                continue
            total += len(data)
            attach.append((name, data))
        if left_out:
            text += f"\n\n({left_out} Datei(en) waren zu groß für die E-Mail – bitte im Werkstatt-Programm abrufen.)"
        msg.set_content(text + "\n")
        for name, data in attach:
            maintype, subtype = _mime(name)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        return msg

    def smtp_send(self, msg: EmailMessage) -> None:
        try:
            s = connect_smtp(self.cfg)
            try:
                s.login(self.cfg["user"], self.cfg["password"])
                s.send_message(msg)
            finally:
                _safe_quit(s)
        except smtplib.SMTPAuthenticationError:
            raise Fatal("SMTP-Anmeldung fehlgeschlagen.")
        except smtplib.SMTPRecipientsRefused as e:
            self.error(f"SMTP: Empfänger abgelehnt: {list(e.recipients)}")
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            raise Retry(f"SMTP: {type(e).__name__}: {e}")

    def send(self, pid: str, target: dict, events: list) -> None:
        to = target.get("to")
        if not to or not self.allowed(to):  # removed from the allowlist since: stay silent
            return
        texts, files = [], []
        for ev in events:
            out = render(ev, self.agent, max_images=6)
            if out["text"]:
                texts.append(out["text"])
            files += out["photos"] + out["documents"]
        if not texts and not files:
            return
        texts.append("Antworten Sie einfach auf diese E-Mail (Text, Foto oder Sprachnachricht). "
                     "„neu“ als Betreff beginnt einen neuen Auftrag.")
        msg = self.compose(to, f"Re: {target.get('subject') or 'Ihr Auftrag'} [W-{pid}]", target.get("msgid") or "",
                           list(target.get("refs") or []), "\n\n".join(texts), files)
        self.smtp_send(msg)
        with self.lock:
            self.state["threads"][msg["Message-ID"]] = pid
            if len(self.state["threads"]) > 2000:
                for k in list(self.state["threads"])[:500]:
                    del self.state["threads"][k]
            o = self.state["orders"].get(pid)
            if o is not None:
                o["refs"] = (list(o.get("refs") or []) + [msg["Message-ID"]])[-20:]
        self.save_state()

    def reply_error(self, to: str, subject: str, msgid: str, text: str) -> None:
        try:
            self.smtp_send(self.compose(to, f"Re: {subject or 'Ihr Auftrag'}", msgid, [msgid] if msgid else [], text, []))
        except (Retry, Fatal) as e:
            self.error(f"error reply: {e}")


def _mime(name: str) -> tuple[str, str]:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    return {"png": ("image", "png"), "jpg": ("image", "jpeg"), "jpeg": ("image", "jpeg"), "webp": ("image", "webp"),
            "pdf": ("application", "pdf"), "zip": ("application", "zip"), "dxf": ("application", "dxf"),
            "glb": ("model", "gltf-binary"), "stl": ("model", "stl"), "csv": ("text", "csv"),
            "txt": ("text", "plain")}.get(ext, ("application", "octet-stream"))


def _safe_logout(m) -> None:
    try:
        m.logout()
    except Exception:
        pass


def _safe_quit(s) -> None:
    try:
        s.quit()
    except Exception:
        pass
