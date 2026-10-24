"""Channels with fake transports only: httpx.MockTransport for Telegram, fake imaplib/smtplib classes for mail.
An autouse fixture makes every real socket connection fail, so nothing here can reach the internet."""

import json
import os
import socket
import stat
import sys
import threading
import time
from email.message import EmailMessage
from email import message_from_bytes, policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import channels
import projects
from channels import base, mail, telegram

REAL_PASSWORD_SET = channels.password_set

TOKEN = "123456789:AAH" + "x" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    store, out = tmp_path / "projects", tmp_path / "outputs"
    (store / "workshop").mkdir(parents=True)
    (out / "uploads").mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", store)
    monkeypatch.setattr(projects, "OUT", out)
    monkeypatch.setattr(base, "SWEEP_S", 0.05)
    monkeypatch.setattr(base, "BACKOFF_MIN", 0.01)

    def no_net(*a, **k):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(socket, "getaddrinfo", no_net)
    monkeypatch.setattr(channels, "password_set", lambda: True)    # switching on needs a team password (own test)
    yield tmp_path
    channels.stop_all(1)


# ---------------- fakes ----------------

class FakeAgent:
    def __init__(self):
        self.calls, self.evs, self.subs, self.n = [], {}, [], 0
        self.fail = None

    def start(self, photo, who, channel, text=None, audio=None):
        if self.fail:
            raise self.fail
        self.n += 1
        pid = f"pid{self.n:09d}"
        self.calls.append(("start", pid, who, channel, text, audio, photo))
        self.evs[pid] = [{"n": 1, "role": "user", "kind": "photo" if photo else "text", "text": text or ""}]
        return pid

    def handle(self, pid, who, channel, text=None, audio=None, photo=None):
        if self.fail:
            raise self.fail
        self.calls.append(("handle", pid, who, channel, text, audio, photo))
        return [self.emit(pid, "status", "Verstanden.", notify=False)]

    def events(self, pid, after=0):
        return [e for e in self.evs.get(pid, []) if e["n"] > after]

    def subscribe(self, cb):
        self.subs.append(cb)
        return lambda: self.subs.remove(cb)

    def emit(self, pid, kind, text="", image=None, data=None, notify=True):
        lst = self.evs.setdefault(pid, [])
        ev = {"n": len(lst) + 1, "at": "2026-10-18T10:00:00", "role": "agent", "kind": kind, "text": text,
              "image": image, "data": data or {}}
        lst.append(ev)
        if notify:
            for cb in list(self.subs):
                cb(pid, ev)
        return ev


class FakeTelegram:
    """Bot API on httpx.MockTransport: queued updates, recorded sends, downloadable files."""

    def __init__(self):
        self.updates, self.sent, self.files, self.offsets = [], [], {}, []
        self.fail_next, self.block, self.status = [], None, None
        self.lock = threading.Lock()

    def push(self, message):
        with self.lock:
            self.updates.append({"update_id": 100 + len(self.updates) + len(self.offsets) * 0, "message": message})

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.telegram.org"
        path = req.url.path
        if path.startswith(f"/file/bot{TOKEN}/"):
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[1]])
        method = path.rsplit("/", 1)[1]
        if self.fail_next:
            f = self.fail_next.pop(0)
            if isinstance(f, Exception):
                raise f
            return httpx.Response(f[0], json={"ok": False, "error_code": f[0], "description": "x", **f[1]})
        if method == "getUpdates":
            body = json.loads(req.content)
            self.offsets.append(body["offset"])
            if self.block:
                self.block.wait(5)
            with self.lock:
                ups = [u for u in self.updates if u["update_id"] >= body["offset"]]
            if not ups:
                time.sleep(0.02)
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "getFile":
            fid = json.loads(req.content)["file_id"]
            return httpx.Response(200, json={"ok": True, "result": {"file_id": fid, "file_path": f"photos/{fid}",
                                                                     "file_size": len(self.files[fid])}})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "werkstatt_bot"}})
        ctype = req.headers.get("content-type", "")
        if ctype.startswith("multipart/"):
            body = req.read()
            rec = {"method": method, "multipart": body}
        else:
            rec = {"method": method, **json.loads(req.content)}
        with self.lock:
            self.sent.append(rec)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})

    def texts(self):
        return [s.get("text") for s in self.sent if s["method"] == "sendMessage"]


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(telegram, "make_client",
                        lambda timeout: httpx.Client(transport=httpx.MockTransport(fake.handler), timeout=timeout))
    return fake


def tg_cfg(**kw):
    return telegram.TelegramChannel.normalize({"enabled": True, "token": TOKEN,
                                               "allowed_chats": [{"id": 42, "name": "Peter"}], **kw})


def msg(chat=42, **kw):
    return {"message_id": 1, "chat": {"id": chat, "type": "private", "first_name": "Max", "username": "max"}, **kw}


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------- config + router ----------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(channels.router)
    return TestClient(app)


def test_defaults_are_off_and_secrets_hidden(client):
    r = client.get("/api/channels").json()
    assert r["channels"]["telegram"]["enabled"] is False and r["channels"]["mail"]["enabled"] is False
    assert r["channels"]["telegram"]["token"] == {"set": False}
    assert r["channels"]["mail"]["password"] == {"set": False}
    assert "Internet" in r["warning"]
    assert not channels.config_path().exists()          # reading never writes


def test_put_secret_write_only_and_mode_600(client):
    r = client.put("/api/channels", json={"telegram": {"token": TOKEN, "allowed_chats": [42, "43", "x", True]}})
    assert r.status_code == 200
    assert TOKEN not in r.text and r.json()["channels"]["telegram"]["token"] == {"set": True}
    assert [c["id"] for c in r.json()["channels"]["telegram"]["allowed_chats"]] == [42, 43]
    assert stat.S_IMODE(os.stat(channels.config_path()).st_mode) == 0o600
    assert json.loads(channels.config_path().read_text())["telegram"]["token"] == TOKEN
    client.put("/api/channels", json={"telegram": {"token": "", "who": "peter"}})        # "" keeps
    assert channels.load()["telegram"]["token"] == TOKEN
    client.put("/api/channels", json={"telegram": {"token": {"set": True}}})              # echoed view keeps
    assert channels.load()["telegram"]["token"] == TOKEN
    client.put("/api/channels", json={"telegram": {"token": None}})                       # null clears
    assert channels.load()["telegram"]["token"] == ""
    assert TOKEN not in client.get("/api/channels").text


def test_put_rejects_enabling_invalid_and_unknown(client):
    r = client.put("/api/channels", json={"telegram": {"enabled": True}})
    assert r.status_code == 400 and "Bot-Token fehlt" in r.json()["detail"]
    assert client.put("/api/channels", json={"sms": {}}).status_code == 400
    assert client.put("/api/channels", json={"mail": "x"}).status_code == 400
    r = client.put("/api/channels", json={"mail": {"enabled": True, "imap_host": "imap.example.de",
                                                   "smtp_host": "smtp.example.de", "user": "w@example.de",
                                                   "password": "pw", "allowed_senders": ["w@example.de"]}})
    assert r.status_code == 400 and "Mail-Schleife" in r.json()["detail"]
    assert channels.load()["mail"]["enabled"] is False


def test_check_is_offline_when_disabled(client, monkeypatch):
    monkeypatch.setattr(telegram, "make_client", lambda t: (_ for _ in ()).throw(AssertionError("online")))
    client.put("/api/channels", json={"telegram": {"token": "kaputt"}})
    r = client.post("/api/channels/telegram/check").json()
    assert r["online"] is None and r["ok"] is False and any("Format" in i for i in r["issues"])
    client.put("/api/channels", json={"telegram": {"token": TOKEN, "allowed_chats": [42]}})
    r = client.post("/api/channels/telegram/check").json()
    assert r["ok"] is True and r["online"] is None and r["note"]
    assert client.post("/api/channels/fax/check").status_code == 404


def test_check_online_only_when_enabled(client, tg):
    client.put("/api/channels", json={"telegram": {"token": TOKEN, "allowed_chats": [42]}})
    r = client.post("/api/channels/telegram/check", json={"enabled": True}).json()
    assert r["ok"] is True and r["online"] is None and r["enabled"] is False     # a body cannot switch the test online
    assert channels.load()["telegram"]["enabled"] is False        # check never saves
    assert tg.texts() == [] and not getattr(tg, "offsets", None)
    client.put("/api/channels", json={"telegram": {"enabled": True}})
    channels.stop_all(1)
    r = client.post("/api/channels/telegram/check").json()
    assert r["ok"] is True and r["online"]["bot"] == "werkstatt_bot"
    r = client.post("/api/channels/telegram/check", json={"allowed_chats": [42, 43]}).json()
    assert r["online"] is None and "erst speichern" in r["note"]   # unsaved values: offline only
    tg.fail_next = [(401, {})]
    r = client.post("/api/channels/telegram/check", json={}).json()
    assert r["ok"] is False and "Token" in r["online"]["detail"]


def test_enabling_needs_a_team_password(client, monkeypatch, tg, env):
    monkeypatch.setattr(channels, "password_set", REAL_PASSWORD_SET)     # the real check: settings.json in the tmp store
    assert Path(projects.STORE).is_relative_to(env)
    client.put("/api/channels", json={"telegram": {"token": TOKEN, "allowed_chats": [42]}})
    r = client.put("/api/channels", json={"telegram": {"enabled": True}})
    assert r.status_code == 409 and "Team-Passwort" in r.json()["detail"]
    assert channels.load()["telegram"]["enabled"] is False
    cfg = channels.load()
    cfg["telegram"]["enabled"] = True                    # switched on by hand in the file: still not started
    channels.save(cfg)
    assert channels.start_all(FakeAgent()) == [] and channels.status() == {}
    channels.stop_all(1)
    import team
    projects.update_settings(lambda st: st["access"].update(password=team.hash_password("geheim123")))
    assert channels.password_set() and channels.start_all(FakeAgent()) == ["telegram"]
    channels.stop_all(1)


def test_start_all_starts_nothing_by_default():
    before = threading.active_count()
    assert channels.start_all(FakeAgent()) == []
    assert channels.status() == {} and threading.active_count() == before


# ---------------- Telegram ----------------

def test_unknown_chat_gets_one_refusal_and_is_never_served(tg):
    agent = FakeAgent()
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    tg.push(msg(chat=999, text="Hallo"))
    tg.push(msg(chat=999, photo=[{"file_id": "f1", "width": 10, "height": 10}]))
    ch.poll_once()
    assert agent.calls == []
    assert tg.texts() == [telegram.REFUSAL]
    assert ch.state["refused"]["999"]["count"] == 2 and "@max" in ch.state["refused"]["999"]["name"]
    assert ch.state["offset"] == 102
    ch.client.close()


def test_photo_and_voice_go_to_the_agent_and_mapping_persists(tg):
    agent = FakeAgent()
    tg.files.update(big=PNG, small=b"small", v1=b"OggS-voice")
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    tg.push(msg(caption="Einbauschrank links", photo=[{"file_id": "small", "width": 90, "height": 60},
                                                       {"file_id": "big", "width": 1280, "height": 960}]))
    ch.poll_once()
    kind, pid, who, chan, text, audio, photo = agent.calls[0]
    assert (kind, who, chan, text, photo) == ("start", "telegram", "telegram", "Einbauschrank links", PNG)
    tg.push(msg(voice={"file_id": "v1", "duration": 3}))
    ch.poll_once()
    assert agent.calls[1][:2] == ("handle", pid) and agent.calls[1][5] == b"OggS-voice"
    saved = json.loads((base.store_dir() / "telegram.json").read_text())
    assert saved["chats"]["42"]["pid"] == pid and saved["orders"][pid]["chat"] == 42 and saved["offset"] == 102
    assert stat.S_IMODE(os.stat(base.store_dir() / "telegram.json").st_mode) == 0o600
    ch.client.close()
    again = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    tg.push(msg(text="achtzig tief"))
    again.poll_once()
    assert agent.calls[2][:2] == ("handle", pid) and agent.calls[2][4] == "achtzig tief"
    assert tg.offsets[-1] == 102        # resumed from the persisted offset
    again.client.close()


def test_commands_new_status_help(tg):
    agent = FakeAgent()
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    for t in ("/start", "/status"):
        tg.push(msg(text=t))
    ch.poll_once()
    assert tg.texts() == [telegram.WELCOME, telegram.NO_ORDER] and agent.calls == []
    tg.push(msg(text="Regal ins Wohnzimmer"))
    ch.poll_once()
    pid = agent.calls[0][1]
    agent.emit(pid, "question", "Wie breit soll das Regal sein?", notify=False)
    tg.push(msg(text="/status@werkstatt_bot"))
    ch.poll_once()
    assert tg.texts()[-1] == f"Auftrag {pid}:\nWie breit soll das Regal sein?"
    tg.push(msg(text="/neu"))
    tg.push(msg(text="Küche"))
    ch.poll_once()
    assert tg.texts()[-1] == telegram.NEW_ORDER
    assert agent.calls[-1][0] == "start" and agent.calls[-1][1] != pid
    ch.client.close()


def test_agent_error_is_answered_politely(tg):
    from fastapi import HTTPException
    agent = FakeAgent()
    agent.fail = HTTPException(409, "Der Auftrag ist gerade gesperrt.")
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    tg.push(msg(text="Schrank"))
    ch.poll_once()
    assert tg.texts() == ["Der Auftrag ist gerade gesperrt."]
    agent.fail = RuntimeError("Traceback with internals")
    tg.push(msg(text="Schrank"))
    ch.poll_once()
    assert tg.texts()[-1] == base.GENERIC_ERROR
    ch.client.close()


def test_events_out_photos_documents_dedupe(tg, env):
    agent = FakeAgent()
    out = env / "outputs"
    (out / "uploads" / "prev.png").write_bytes(PNG)
    (out / "angebot.pdf").write_bytes(b"%PDF-1.4 x")
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    ch.track("pidX", chat=42)
    agent.subs.append(ch.deliver)
    agent.emit("pidX", "preview", "So? (ja / ändern …)", image="uploads/prev.png")
    agent.emit("pidX", "result", "Fertig: 2 Bilder.", data={"images": ["/outputs/uploads/prev.png"],
               "files": [{"name": "Angebot", "path": "angebot.pdf"}], "offer_text": "Pos. 1 Einbauschrank",
               "links": ["http://192.168.1.5:7860/k/abc"]})
    agent.evs["pidX"].append({"n": 3, "role": "user", "kind": "text", "text": "ja"})
    ch.sweep()                           # nothing new: events 1–2 already delivered, 3 is the user's own
    batch = []
    while not ch.outbox.empty():
        batch.append(ch.outbox.get())
    assert [e["n"] for _, e in batch] == [1, 2]
    ch.send("pidX", {"chat": 42}, [e for _, e in batch])
    methods = [s["method"] for s in tg.sent]
    assert methods == ["sendPhoto", "sendPhoto", "sendDocument"]
    assert b"So? (ja" in tg.sent[0]["multipart"] and PNG in tg.sent[0]["multipart"]
    assert b"Pos. 1 Einbauschrank" in tg.sent[1]["multipart"] and b"192.168.1.5" in tg.sent[1]["multipart"]
    assert b"%PDF-1.4" in tg.sent[2]["multipart"]
    tg.sent.clear()
    ch.send("pidX", {"chat": 777}, [batch[0][1]])        # chat no longer allowed: silence
    assert tg.sent == []
    ch.client.close()


def test_adapter_file_stays_inside_outputs(env):
    (env / "secret.txt").write_text("nope")
    (env / "outputs" / "ok.png").write_bytes(PNG)
    a = base.AgentAdapter(FakeAgent())
    assert a.file("ok.png") == ("ok.png", PNG) and a.file("/outputs/ok.png") == ("ok.png", PNG)
    assert a.file("../secret.txt") is None and a.file(str(env / "secret.txt")) is None
    assert a.file("https://x/y.png") is None and a.file(None) is None and a.file("missing.png") is None


def test_backoff_recovers_and_token_never_leaks(tg, monkeypatch):
    agent = FakeAgent()
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(agent))
    tg.fail_next = [httpx.ConnectError(f"boom https://api.telegram.org/bot{TOKEN}/getUpdates"), (500, {}),
                    (429, {"parameters": {"retry_after": 0.01}})]
    tg.push(msg(text="Schrank"))
    ch.start()
    assert wait_for(lambda: agent.calls)
    assert ch.info["last_error"] and TOKEN not in ch.info["last_error"]
    assert TOKEN not in json.dumps(ch.status())
    ch.stop(2)
    assert not ch.threads


def test_bad_token_is_fatal_and_stops(tg):
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(FakeAgent()))
    tg.fail_next = [(401, {})]
    ch.start()
    assert wait_for(lambda: ch.info["fatal"])
    assert "Token" in ch.info["fatal"] and not ch.running
    ch.stop(1)


def test_stop_is_prompt_during_a_long_poll(tg):
    tg.block = threading.Event()
    ch = telegram.TelegramChannel(tg_cfg(), base.AgentAdapter(FakeAgent()))
    ch.start()
    assert wait_for(lambda: tg.offsets)
    t0 = time.time()
    ch.stop(0.5)
    assert time.time() - t0 < 1.5 and not ch.running
    tg.push(msg(text="spät"))
    tg.block.set()                       # the abandoned poll returns late: it must not process or advance the offset
    time.sleep(0.2)
    assert ch.state["offset"] == 0


def test_start_all_end_to_end_with_subscription(tg, client):
    client.put("/api/channels", json={"telegram": {"token": TOKEN, "allowed_chats": [42]}})
    agent = FakeAgent()
    assert channels.start_all(agent) == []                       # saved but not enabled
    r = client.put("/api/channels", json={"telegram": {"enabled": True}})
    assert r.json()["channels"]["telegram"]["status"]["running"] is True    # enabling after start_all starts it
    tg.push(msg(text="Sideboard unter das Fenster"))
    assert wait_for(lambda: agent.calls)
    pid = agent.calls[0][1]
    agent.emit(pid, "question", "Wie breit soll das Sideboard sein?")
    assert wait_for(lambda: "Wie breit soll das Sideboard sein?" in tg.texts())
    time.sleep(0.2)
    assert tg.texts().count("Wie breit soll das Sideboard sein?") == 1      # subscription + sweep: sent once
    client.put("/api/channels", json={"telegram": {"enabled": False}})
    assert channels.status() == {}
    channels.stop_all(1)
    assert agent.subs == []


# ---------------- mail ----------------

def mail_cfg(**kw):
    return mail.MailChannel.normalize({"enabled": True, "imap_host": "imap.example.de", "smtp_host": "smtp.example.de",
                                       "user": "werkstatt@example.de", "password": "geheim",
                                       "allowed_senders": ["kunde@example.de", "@schreinerei.de"],
                                       "processed_folder": "Erledigt", **kw})


def make_mail(frm="Max Kunde <kunde@example.de>", subject="Neuer Schrank", text="Links ein Schrank, 220 breit.",
              photo=PNG, audio=None, headers=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "werkstatt@example.de", subject
    m["Message-ID"] = f"<m{abs(hash((frm, subject, text))) % 10**8}@example.de>"
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(text)
    if photo:
        m.add_attachment(photo, maintype="image", subtype="png", filename="raum.png")
    if audio:
        m.add_attachment(audio, maintype="application", subtype="octet-stream", filename="memo.m4a")
    return m


class FakeIMAP:
    instances: list = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        assert ssl_context is not None
        self.host, self.port, self.logged_in, self.flags, self.moved = host, port, False, {}, []
        self.capabilities = ("IMAP4REV1", "MOVE")
        FakeIMAP.instances.append(self)

    def login(self, user, pw):
        if pw != "geheim":
            raise mail.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = True
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        assert self.logged_in
        return ("OK", [b"2"]) if folder == '"INBOX"' else ("NO", [b"no"])

    def response(self, code):
        return code, [b"777"]

    def uid(self, cmd, *args):
        cmd = cmd.upper()
        if cmd == "SEARCH":
            return "OK", [" ".join(u for u in MAILBOX if "\\Seen" not in MAILBOX[u][1]).encode()]
        if cmd == "FETCH":
            assert args[1] == "(BODY.PEEK[])"
            return "OK", [(b"1 (UID x BODY[] {n}", MAILBOX[args[0]][0]), b")"]
        if cmd == "STORE":
            MAILBOX[args[0]][1].add(args[2].strip("()"))
            return "OK", []
        if cmd == "MOVE":
            self.moved.append((args[0], args[1]))
            return "OK", []
        raise AssertionError(cmd)

    def logout(self):
        return "BYE", []


MAILBOX: dict = {}


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, context=None, timeout=None):
        assert context is not None
        self.host = host

    def login(self, user, pw):
        if pw != "geheim":
            raise mail.smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, m):
        FakeSMTP.sent.append(message_from_bytes(m.as_bytes(), policy=policy.default))

    def quit(self):
        pass


@pytest.fixture
def mbox(monkeypatch):
    MAILBOX.clear()
    FakeIMAP.instances.clear()
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("plain SMTP")))
    return MAILBOX


def test_mail_allowlisted_photo_text_audio_to_agent_and_marked(mbox):
    mbox["1"] = (make_mail(audio=b"m4a-voice").as_bytes(), set())
    mbox["2"] = (make_mail(frm="Spam <spam@evil.example>").as_bytes(), set())
    mbox["3"] = (make_mail(frm="chef@schreinerei.de", headers={"Auto-Submitted": "auto-replied"}).as_bytes(), set())
    agent = FakeAgent()
    ch = mail.MailChannel(mail_cfg(), base.AgentAdapter(agent))
    ch.poll_once()
    assert len(agent.calls) == 1
    kind, pid, who, chan, text, audio, photo = agent.calls[0]
    assert (kind, chan, text, audio, photo) == ("start", "mail", "Links ein Schrank, 220 breit.", b"m4a-voice", PNG)
    assert "\\Seen" in mbox["1"][1] and FakeIMAP.instances[0].moved == [("1", '"Erledigt"')]
    assert mbox["2"][1] == set() and mbox["3"][1] == set()          # unknown + auto mails stay unread
    assert "spam@evil.example" in ch.state["refused"] and ch.state["orders"][pid]["to"] == "kunde@example.de"
    ch.poll_once()                                                   # nothing processed twice, refusals not re-logged
    assert len(agent.calls) == 1 and ch.state["refused"]["spam@evil.example"]["count"] == 1


def test_mail_reply_continues_the_order(mbox):
    agent = FakeAgent()
    ch = mail.MailChannel(mail_cfg(), base.AgentAdapter(agent))
    mbox["1"] = (make_mail().as_bytes(), set())
    ch.poll_once()
    pid = agent.calls[0][1]
    quoted = "Bitte achtzig tief.\n\nAm 18.10.2026 schrieb Werkstatt:\n> Wie tief?\n"
    mbox["2"] = (make_mail(subject=f"Re: Neuer Schrank [W-{pid}]", text=quoted, photo=None).as_bytes(), set())
    mbox["3"] = (make_mail(frm="chef@schreinerei.de", subject=f"Re: x [W-{pid}]", text="fremder Auftrag",
                           photo=None).as_bytes(), set())
    mbox["4"] = (make_mail(subject="neu", text="Küche bitte", photo=None).as_bytes(), set())
    ch.poll_once()
    assert agent.calls[1][:2] == ("handle", pid) and agent.calls[1][4] == "Bitte achtzig tief."
    assert agent.calls[2][0] == "start"          # other sender cannot hijack an order via the subject tag
    assert agent.calls[3][0] == "start" and agent.calls[3][4] == "Küche bitte"


def test_mail_events_go_out_as_one_reply_with_attachments(mbox, env):
    (env / "outputs" / "v1.png").write_bytes(PNG)
    (env / "outputs" / "Visualisierung.pdf").write_bytes(b"%PDF-1.4 y")
    agent = FakeAgent()
    ch = mail.MailChannel(mail_cfg(), base.AgentAdapter(agent))
    mbox["1"] = (make_mail().as_bytes(), set())
    ch.poll_once()
    pid = agent.calls[0][1]
    evs = [agent.emit(pid, "status", "Ich sehe mir den Raum an …", notify=False),
           agent.emit(pid, "result", "Fertig.", data={"images": ["v1.png"], "pdf": "Visualisierung.pdf"}, notify=False)]
    ch.send(pid, ch.state["orders"][pid], evs)
    assert len(FakeSMTP.sent) == 1
    m = FakeSMTP.sent[0]
    assert m["To"] == "kunde@example.de" and f"[W-{pid}]" in m["Subject"] and m["Subject"].startswith("Re: Neuer")
    assert m["Auto-Submitted"] == "auto-replied" and m["In-Reply-To"].startswith("<m")
    body = m.get_body(("plain",)).get_content()
    assert "Ich sehe mir den Raum an" in body and "Fertig." in body
    names = sorted(p.get_filename() for p in m.iter_attachments())
    assert names == ["Visualisierung.pdf", "v1.png"]
    assert ch.state["threads"][m["Message-ID"]] == pid
    # a reply to our mail without the tag still finds the order through In-Reply-To
    r = make_mail(subject="Re: passt", text="ja", photo=None)
    r["In-Reply-To"] = m["Message-ID"]
    mbox["2"] = (r.as_bytes(), set())
    ch.poll_once()
    assert agent.calls[-1][:2] == ("handle", pid)


def test_mail_bad_password_is_fatal(mbox):
    ch = mail.MailChannel(mail_cfg(password="falsch"), base.AgentAdapter(FakeAgent()))
    with pytest.raises(base.Fatal):
        ch.poll_once()


def test_mail_online_check(mbox, client):
    cfg = {k: v for k, v in mail_cfg().items() if k != "enabled"}
    client.put("/api/channels", json={"mail": cfg})
    r = client.post("/api/channels/mail/check").json()
    assert r["online"] is None and r["ok"] and FakeIMAP.instances == []        # disabled: offline only
    r = client.post("/api/channels/mail/check", json={"enabled": True}).json()
    assert r["online"] is None and FakeIMAP.instances == []                    # a body never goes online
    client.put("/api/channels", json={"mail": {"enabled": True}})
    channels.stop_all(1)
    FakeIMAP.instances.clear()
    r = client.post("/api/channels/mail/check").json()
    assert r["ok"] and "erfolgreich" in r["online"]["detail"]
    client.put("/api/channels", json={"mail": {"password": "falsch"}})
    channels.stop_all(1)
    r = client.post("/api/channels/mail/check").json()
    assert not r["ok"] and "IMAP-Anmeldung" in r["online"]["detail"] and "falsch" not in json.dumps(r)
    assert "geheim" not in client.get("/api/channels").text and "falsch" not in client.get("/api/channels").text


def test_stored_mail_password_never_goes_to_another_host(mbox, client):
    """The reviewer's probe: retargeting the saved channel (check body or PUT) must not log in elsewhere with it."""
    logins = []
    orig = FakeIMAP.login
    mbox_login = lambda self, user, pw: logins.append((self.host, self.port, pw)) or orig(self, user, pw)
    FakeIMAP.login = mbox_login
    try:
        cfg = {k: v for k, v in mail_cfg().items() if k != "enabled"}
        client.put("/api/channels", json={"mail": cfg})
        r = client.post("/api/channels/mail/check", json={"enabled": True, "imap_host": "127.0.0.1", "imap_port": 8002})
        assert r.json()["online"] is None and logins == []
        client.put("/api/channels", json={"mail": {"enabled": True}})
        channels.stop_all(1)
        r = client.post("/api/channels/mail/check", json={"imap_host": "127.0.0.1", "imap_port": 8002}).json()
        assert r["online"] is None and any("Passwort" in i for i in r["issues"]) and logins == []
        for change in ({"imap_host": "imap.boese.example"}, {"smtp_host": "smtp.boese.example"}, {"imap_port": 8002},
                       {"user": "jemand@boese.example"}):
            client.put("/api/channels", json={"mail": {**cfg, "enabled": False, "password": "geheim"}})
            r = client.put("/api/channels", json={"mail": {**change, "enabled": True}})
            assert r.status_code == 400 and "Passwort" in r.json()["detail"]                # refused, nothing saved
            client.put("/api/channels", json={"mail": {**change, "enabled": False}})
            assert channels.load()["mail"]["password"] == ""                                 # typed again or nothing
        client.put("/api/channels", json={"mail": {"imap_host": "imap.neu.example", "password": "geheim"}})
        assert channels.load()["mail"]["password"] == "geheim"                          # new target + new secret: ok
        client.put("/api/channels", json={"mail": {"poll_s": 120}})
        assert channels.load()["mail"]["password"] == "geheim"                          # other fields keep it
    finally:
        FakeIMAP.login = orig
    assert all(h == "imap.example.de" for h, _, _ in logins)


def test_mail_threaded_send_coalesces(mbox, env, monkeypatch):
    monkeypatch.setattr(mail.MailChannel, "coalesce_s", 0.2)
    agent = FakeAgent()
    ch = mail.MailChannel(mail_cfg(poll_s=3600), base.AgentAdapter(agent))
    mbox["1"] = (make_mail().as_bytes(), set())
    ch.start()
    assert wait_for(lambda: agent.calls)
    pid = agent.calls[0][1]
    for t in ("Ich sehe mir den Raum an …", "Wie tief soll der Schrank sein?"):
        ch.deliver(pid, agent.emit(pid, "question", t, notify=False))
    assert wait_for(lambda: FakeSMTP.sent)
    time.sleep(0.3)
    ch.stop(1)
    assert len(FakeSMTP.sent) == 1 and "Wie tief" in FakeSMTP.sent[0].get_body(("plain",)).get_content()


# ---------------- helpers ----------------

def test_body_text_cuts_quotes_signature_and_html():
    m = EmailMessage()
    m.set_content("Hallo,\nein Regal bitte.\n-- \nMax Mustermann\nTel 123")
    assert mail.body_text(m) == "Hallo,\nein Regal bitte."
    h = EmailMessage()
    h.set_content("<p>Schrank <b>wei&szlig;</b></p><div>raumhoch</div><style>p{}</style>", subtype="html")
    assert mail.body_text(h).split() == ["Schrank", "weiß", "raumhoch"]
    q = EmailMessage()
    q.set_content("ja\n\nOn Sat, 18 Oct 2026 Werkstatt wrote:\n> So?")
    assert mail.body_text(q) == "ja"


def test_split_text_and_render_missing_files():
    parts = base.split_text("a" * 50 + "\n\n" + "b" * 50, 60)
    assert parts == ["a" * 50, "b" * 50]
    assert all(len(p) <= 10 for p in base.split_text("x" * 35, 10))
    out = base.render({"role": "agent", "kind": "result", "text": "Fertig", "data": {"images": ["nope.png"]}},
                      base.AgentAdapter(FakeAgent()))
    assert out["photos"] == [] and "1 Datei(en)" in out["text"]


def test_normalize_is_strict():
    c = mail.MailChannel.normalize({"imap_host": "imap.x.de; rm -rf", "imap_port": "99999", "smtp_security": "tls",
                                    "allowed_senders": ["KUNDE@X.DE", "kein", "@x.de", "@x"], "poll_s": 1})
    assert c["imap_host"] == "" and c["imap_port"] == 993 and c["smtp_security"] == "ssl"
    assert c["allowed_senders"] == ["kunde@x.de", "@x.de"] and c["poll_s"] == 30
    t = telegram.TelegramChannel.normalize({"poll_timeout": 999, "allowed_chats": [{"id": "-100123", "name": "Gruppe"}]})
    assert t["poll_timeout"] == 50 and t["allowed_chats"][0]["id"] == -100123 and t["enabled"] is False
