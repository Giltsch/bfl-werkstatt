"""connect.py (API keys, /api/v1, webhooks), offer.py (Positionstext) and dxf.py (R12 drawing) on a private tmp store.
The agent is a fake module in sys.modules; webhooks go to httpx.MockTransport — nothing leaves the process."""
import io
import json
import socket
import stat
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import connect
import dxf
import offer
import projects
import team


def png_bytes(size=(64, 48), color=(180, 160, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeAgent:
    """agent.py contract (SPEC): start / handle / events. Creates real TEST orders in the tmp store."""

    def __init__(self, out: Path):
        self.out, self.calls, self.convs = out, [], {}

    def module(self):
        m = types.ModuleType("agent")
        m.start, m.handle, m.events = self.start, self.handle, self.events
        return m

    def start(self, photo, who, channel, text=None, audio=None):
        self.calls.append(("start", photo, who, channel, text, audio))
        doc = projects.new_workshop({"title": "TEST API-Auftrag"}, who)
        pc = doc["pieces"][0]
        pc.update(type="schrank", w=220, h=250, d=60, material="weiss", handles="push", text="3 Türen grifflos",
                  where="links neben dem Fenster")
        (self.out / "preview_1.png").write_bytes(png_bytes(color=(10, 200, 10)))
        (self.out / "res1.png").write_bytes(png_bytes(color=(1, 2, 3)))
        (self.out / "res2.png").write_bytes(png_bytes(color=(4, 5, 6)))
        doc["variants"] = [
            {"id": "va11", "image": "res1.png", "fav": False, "judge": {"status": "ok", "overall": 6.1, "pass": False},
             "piece_ids": [pc["id"]], "pieces": [{"id": pc["id"], "label": "Einbauschrank", "material_label": "Lack weiß matt",
                                                   "handles_label": "grifflos (Push)", "w": 200, "h": 250, "d": 60}]},
            {"id": "vb22", "image": "res2.png", "fav": False, "judge": {"status": "ok", "overall": 7.4, "pass": True},
             "piece_ids": [pc["id"]]},
            {"id": "vc33", "image": "missing.png", "judge": None}]
        projects.save("workshop", doc, who)
        self.convs[doc["id"]] = {"state": "asking", "events": [
            {"n": 1, "at": "t", "role": "user", "kind": "text", "text": text},
            {"n": 2, "at": "t", "role": "agent", "kind": "preview", "text": "So?", "image": "preview_1.png"},
            {"n": 3, "at": "t", "role": "agent", "kind": "question", "text": "Wie tief soll der Schrank sein?",
             "data": {"field": "d", "default": 60}}]}
        return doc["id"]

    def handle(self, pid, who, channel, text=None, audio=None, photo=None):
        self.calls.append(("handle", pid, who, channel, text, audio, photo))
        c = self.convs[pid]
        n = len(c["events"])
        new = [{"n": n + 1, "at": "t", "role": "user", "kind": "text", "text": text},
               {"n": n + 2, "at": "t", "role": "agent", "kind": "status", "text": "Verstanden."}]
        c["events"] += new
        c["state"] = "preview"
        return new

    def events(self, pid, after=0):
        c = self.convs.get(pid, {"state": None, "events": []})
        return {"state": c["state"], "events": [e for e in c["events"] if e["n"] > after]}


REAL_PASSWORD_SET = connect.password_set
PUBLIC_IP = "93.184.215.14"
OWN_IPS = {"127.0.0.1", "::1", "192.168.178.111", "2003:d8:174a:6600::7"}     # this machine (incl. a public IPv6)
DNS = {"n8n.lan": "192.168.178.20", "selbst.example": "2003:d8:174a:6600::7", "example.invalid": PUBLIC_IP, "a.invalid": PUBLIC_IP, "intern.example": "10.0.0.5",
       "metadata.example": "169.254.169.254", "v6.example": "::1"}


def fake_resolve(dns, host, port):
    """getaddrinfo stand-in: no real DNS in tests."""
    if host not in dns:
        raise socket.gaierror(-2, "Name or service not known")
    ip = dns[host]
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]


@pytest.fixture
def env(tmp_path, monkeypatch):
    s, out = tmp_path / "projects", tmp_path / "outputs"
    for k in projects.KINDS:
        (s / k).mkdir(parents=True)
    out.mkdir()
    monkeypatch.setattr(projects, "STORE", s)
    monkeypatch.setattr(projects, "OUT", out)
    for name in ("_RATE", "_FAILS", "_TOUCHED"):
        monkeypatch.setattr(connect, name, {})
    monkeypatch.setattr(connect, "_RECENT", [])
    fake = FakeAgent(out)
    monkeypatch.setitem(sys.modules, "agent", fake.module())
    sent = []
    monkeypatch.setattr(connect, "TRANSPORT", httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(599)))
    dns = dict(DNS)
    monkeypatch.setattr(connect, "RESOLVE", lambda host, port, **k: fake_resolve(dns, host, port))
    monkeypatch.setattr(connect, "password_set", lambda: True)      # keys/webhooks need a team password (own test)
    monkeypatch.setattr(connect, "_OWN", {"at": float("inf"), "ips": frozenset(OWN_IPS)})
    yield types.SimpleNamespace(store=s, out=out, agent=fake, sent=sent, dns=dns)
    connect.stop()


def make_app():
    a = FastAPI()
    team.install(a)
    a.include_router(connect.router)
    a.include_router(connect.v1)
    return a


@pytest.fixture
def client(env):
    return TestClient(make_app(), base_url="http://werkstatt.local:7860")


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def new_key(name="Test"):
    view, plain = connect.create_key(name)
    return view["id"], plain


def create_order(client, key, text="links ein Einbauschrank, zwei-zwanzig breit"):
    return client.post("/api/v1/orders", headers=bearer(key), files={"photo": ("raum.png", png_bytes(), "image/png")},
                       data={"text": text})


# ---------------- keys ----------------

def test_key_is_shown_once_and_stored_hashed(env, client):
    r = client.post("/api/keys", json={"name": "n8n Büro"})
    assert r.status_code == 200
    body = r.json()
    plain = body["key"]
    assert plain.startswith("wk_") and len(plain) >= 40 and body["scopes"] == ["orders"] and "hash" not in body
    raw = (env.store / "apikeys.json").read_text()
    assert plain not in raw and connect._hash(plain) in raw
    assert stat.S_IMODE((env.store / "apikeys.json").stat().st_mode) == 0o600
    listed = client.get("/api/keys").json()["keys"]
    assert listed == [{k: body[k] for k in ("id", "name", "scopes", "created", "created_by", "last_used", "hint")}]
    assert "key" not in listed[0] and "hash" not in listed[0] and listed[0]["hint"].startswith(plain[:7])
    assert connect.check_key(plain)["id"] == body["id"]
    assert connect.check_key(plain + "x") is None and connect.check_key("") is None and connect.check_key(None) is None
    assert client.delete(f"/api/keys/{body['id']}").json() == {"ok": True}
    assert connect.check_key(plain) is None and client.delete(f"/api/keys/{body['id']}").status_code == 404


def test_key_validation(env, client):
    assert client.post("/api/keys", json={"name": " "}).status_code == 400
    assert client.post("/api/keys", json={"name": "x" * 61}).status_code == 400
    assert client.post("/api/keys", json={"name": "a", "scopes": ["admin"]}).status_code == 400
    a, b = connect.create_key("a")[1], connect.create_key("b")[1]
    assert a != b and connect.check_key(a)["name"] == "a" and connect.check_key(b)["name"] == "b"


def test_v1_requires_a_valid_key(env, client):
    r = client.get("/api/v1/orders/abcdef123456")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/v1/orders/abcdef123456", headers=bearer("wk_nope")).status_code == 401
    assert client.get("/api/v1/orders/abcdef123456", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.post("/api/v1/orders", files={"photo": ("a.png", png_bytes(), "image/png")}).status_code == 401
    kid, plain = new_key()
    data = json.loads((env.store / "apikeys.json").read_text())
    data["keys"][0]["scopes"] = []
    (env.store / "apikeys.json").write_text(json.dumps(data))
    assert client.get("/api/v1/orders/abcdef123456", headers=bearer(plain)).status_code == 403
    assert env.agent.calls == []


def test_team_password_lets_v1_through_with_the_integration_hook(env, client):
    """The integration (team.OPEN has "/api/v1/"): with a team password set, /api/v1 works by key alone and every other
    route still wants the login cookie."""
    assert "/api/v1/" in team.OPEN
    projects.update_settings(lambda s: s["access"].update(password=team.hash_password("geheim123")))
    kid, plain = new_key()
    assert client.get("/api/v1/orders/abcdef123456", headers=bearer(plain)).status_code == 404
    assert client.get("/api/v1/orders/abcdef123456").status_code == 401
    assert client.get("/api/keys", headers=bearer(plain)).status_code == 401                       # keys are no login


def test_agent_missing_gives_503(env, client, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", None)   # import agent → ImportError
    kid, plain = new_key()
    r = create_order(client, plain)
    assert r.status_code == 503 and "Assistent" in r.json()["detail"]


# ---------------- orders ----------------

def test_create_order_and_view(env, client):
    kid, plain = new_key("Zapier")
    r = create_order(client, plain)
    assert r.status_code == 201, r.text
    v = r.json()
    pid = v["order"]
    call = env.agent.calls[0]
    assert call[0] == "start" and call[1] == png_bytes() and call[2] == "API: Zapier" and call[3] == "api"
    assert call[4] == "links ein Einbauschrank, zwei-zwanzig breit" and call[5] is None
    assert json.loads((env.store / "v1_orders.json").read_text())["orders"][pid]["key"] == kid
    assert v["state"] == "asking" and v["title"] == "TEST API-Auftrag" and v["next"] == 3
    assert v["questions"] == [{"n": 3, "text": "Wie tief soll der Schrank sein?", "data": {"field": "d", "default": 60}}]
    assert v["events"][1]["image"] == f"/api/v1/orders/{pid}/files/ereignis_2.png"
    assert [x["variant"] for x in v["results"]] == ["vb22", "va11"]                 # passed first, missing image left out
    assert v["results"][0]["image"] == f"/api/v1/orders/{pid}/files/bild_vb22.png" and v["results"][0]["pass"] is True
    names = {f["name"] for f in v["files"]}
    piece = projects.load("workshop", pid)["pieces"][0]["id"]
    assert {"angebot.txt", "zeichnung.dxf", f"zeichnung_{piece}.dxf", f"modell_{piece}.glb", f"modell_{piece}.stl",
            f"zuschnitt_{piece}.csv", "bild_vb22.png", "angebot_va11.txt", "ereignis_2.png"} <= names
    after = client.get(f"/api/v1/orders/{pid}?after=2", headers=bearer(plain)).json()
    assert [e["n"] for e in after["events"]] == [3]


def test_create_order_validation(env, client, monkeypatch):
    kid, plain = new_key()
    assert client.post("/api/v1/orders", headers=bearer(plain), data={"text": "  "}).status_code == 400
    r = client.post("/api/v1/orders", headers=bearer(plain), files={"photo": ("a.png", b"not an image", "image/png")},
                    data={"text": "Schrank"})
    assert r.status_code == 400 and "Bild" in r.json()["detail"]
    assert client.post("/api/v1/orders", headers=bearer(plain), data={"text": "x" * 4001}).status_code == 400
    monkeypatch.setattr(connect, "MAX_PHOTO", 100)
    assert create_order(client, plain).status_code == 413
    assert client.post("/api/v1/orders", headers=bearer(plain), json={"text": "a"}).status_code == 400
    assert env.agent.calls == []
    r = client.post("/api/v1/orders", headers=bearer(plain), data={"text": "Nur Text, Foto folgt"})
    assert r.status_code == 201 and env.agent.calls[0][1] is None


def test_a_key_never_sees_other_orders(env, client):
    ka, a = new_key("A")
    kb, b = new_key("B")
    pid = create_order(client, a).json()["order"]
    other = projects.new_workshop({"title": "TEST nicht per API"}, "anna")
    projects.save("workshop", other, "anna")
    piece = projects.load("workshop", pid)["pieces"][0]["id"]
    for key, target in ((b, pid), (a, other["id"]), (a, "zzzzzz999999"), (a, "../../etc")):
        for path in (f"/api/v1/orders/{target}", f"/api/v1/orders/{target}/files",
                     f"/api/v1/orders/{target}/files/bild_vb22.png", f"/api/v1/orders/{target}/files/zeichnung_{piece}.dxf"):
            r = client.get(path, headers=bearer(key))
            assert r.status_code == 404, (path, r.status_code)
        r = client.post(f"/api/v1/orders/{target}/messages", headers=bearer(key), data={"text": "ja"})
        assert r.status_code == 404
    assert [c[0] for c in env.agent.calls] == ["start"]
    connect.delete_key(ka)   # a deleted key's orders are unreachable, not inherited
    assert client.get(f"/api/v1/orders/{pid}", headers=bearer(a)).status_code == 401


def test_message(env, client):
    kid, plain = new_key("M")
    pid = create_order(client, plain).json()["order"]
    r = client.post(f"/api/v1/orders/{pid}/messages", headers=bearer(plain), data={"text": "sechzig tief"})
    assert r.status_code == 200
    v = r.json()
    assert env.agent.calls[-1] == ("handle", pid, "API: M", "api", "sechzig tief", None, None)
    assert [e["kind"] for e in v["new"]] == ["text", "status"] and v["state"] == "preview" and v["questions"] == []
    assert client.post(f"/api/v1/orders/{pid}/messages", headers=bearer(plain), data={}).status_code == 400
    r = client.post(f"/api/v1/orders/{pid}/messages", headers=bearer(plain),
                    files={"photo": ("b.png", png_bytes(color=(9, 9, 9)), "image/png")})
    assert r.status_code == 200 and env.agent.calls[-1][6] == png_bytes(color=(9, 9, 9))


def test_files(env, client):
    kid, plain = new_key()
    pid = create_order(client, plain).json()["order"]
    piece = projects.load("workshop", pid)["pieces"][0]["id"]

    def get(name):
        return client.get(f"/api/v1/orders/{pid}/files/{name}", headers=bearer(plain))

    r = get("bild_vb22.png")
    assert r.status_code == 200 and r.content == (env.out / "res2.png").read_bytes()
    assert 'filename="TEST_API-Auftrag_bild_vb22.png"' in r.headers["content-disposition"]
    t = get("angebot_va11.txt").text
    assert "Pos. 1" in t and "200 × 250 × 60 cm" in t and "Entwurf va11" in t        # the variant's snapshot dims
    assert "220 × 250 × 60 cm" in get("angebot.txt").text
    d = get(f"zeichnung_{piece}.dxf")
    assert d.status_code == 200 and d.content.startswith(b"0\r\nSECTION") and b"2200" in d.content
    assert get("zeichnung.dxf").status_code == 200
    assert get(f"modell_{piece}.glb").content[:4] == b"glTF"
    assert len(get(f"modell_{piece}.stl").content) > 84
    assert get(f"zuschnitt_{piece}.csv").content.decode("utf-8-sig").startswith("Teil;Anzahl")
    assert get("ereignis_2.png").content == (env.out / "preview_1.png").read_bytes()
    for bad in ("bild_vc33.png", "bild_nope.png", "angebot_nope.txt", "ereignis_3.png", "ereignis_9.png",
                "zeichnung_nope.dxf", "modell_nope.glb", "..%2F..%2Fsecret.png", "bild_vb22.jpg", "x.txt"):
        assert get(bad).status_code == 404, bad
    assert {f["name"] for f in client.get(f"/api/v1/orders/{pid}/files", headers=bearer(plain)).json()["files"]} >= {
        "angebot.txt", "bild_vb22.png"}


def test_team_export_route(env, client):
    other = projects.new_workshop({"title": "TEST Web"}, "anna")
    other["pieces"][0].update(type="sideboard", w=180, h=75, d=45)
    projects.save("workshop", other, "anna")
    r = client.get(f"/api/workshop/{other['id']}/export/angebot.txt")
    assert r.status_code == 200 and "Sideboard" in r.text and "TEST_Web_angebot.txt" in r.headers["content-disposition"]
    assert client.get(f"/api/workshop/{other['id']}/export/zeichnung.dxf").content.startswith(b"0\r\nSECTION")
    assert client.get(f"/api/workshop/{other['id']}/export/nope.exe").status_code == 404
    assert client.get("/api/workshop/zzzzzz999999/export/angebot.txt").status_code == 404


def test_event_image_outside_outputs_is_not_served(env, client):
    kid, plain = new_key()
    pid = create_order(client, plain).json()["order"]
    env.agent.convs[pid]["events"][1]["image"] = "../projects/apikeys.json"
    v = client.get(f"/api/v1/orders/{pid}", headers=bearer(plain)).json()
    assert "image" not in v["events"][1]
    assert client.get(f"/api/v1/orders/{pid}/files/ereignis_2.png", headers=bearer(plain)).status_code == 404


def test_rate_limits(env, client, monkeypatch):
    monkeypatch.setattr(connect, "LIMITS", {**connect.LIMITS, "read": (3, 60.0), "create": (1, 3600.0)})
    kid, plain = new_key()
    other = new_key("andere")[1]
    codes = [client.get("/api/v1/orders/abcdef123456", headers=bearer(plain)).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]          # one count per request (router + route dependency share the result)
    r = client.get("/api/v1/orders/abcdef123456", headers=bearer(plain))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert client.get("/api/v1/orders/abcdef123456", headers=bearer(other)).status_code == 404   # per key
    assert create_order(client, other).status_code == 201
    assert create_order(client, other).status_code == 429 and len(env.agent.calls) == 1


def test_wrong_keys_lock_out_the_ip(env, client, monkeypatch):
    monkeypatch.setattr(connect, "AUTH_FAILS", (3, 60.0))
    kid, plain = new_key()
    for _ in range(3):
        assert client.get("/api/v1/orders/abcdef123456", headers=bearer("wk_guess")).status_code == 401
    assert client.get("/api/v1/orders/abcdef123456", headers=bearer(plain)).status_code == 429


def test_last_used_is_throttled(env, client, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(connect, "clock", lambda: t[0])
    kid, plain = new_key()
    assert connect.list_keys()[0]["last_used"] is None
    client.get("/api/v1/orders/abcdef123456", headers=bearer(plain))
    first = connect.list_keys()[0]["last_used"]
    assert first
    mtime = (env.store / "apikeys.json").stat().st_mtime_ns
    client.get("/api/v1/orders/abcdef123456", headers=bearer(plain))
    assert (env.store / "apikeys.json").stat().st_mtime_ns == mtime     # within TOUCH_S: no write


# ---------------- webhooks ----------------

def capture(env, monkeypatch, responses=None):
    """MockTransport that records requests and answers from the list (status codes or exceptions), then 200."""
    got, todo = [], list(responses or [])

    def handler(req):
        got.append(req)
        nxt = todo.pop(0) if todo else 200
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(nxt)
    monkeypatch.setattr(connect, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(connect, "RETRIES", (0, 0, 0, 0))
    return got


def test_webhooks_are_off_by_default(env, client, monkeypatch):
    got = capture(env, monkeypatch)
    view, secret = connect.add_hook("https://example.invalid/hook")
    assert view["enabled"] is False and connect.webhooks_view()["enabled"] is False
    assert connect.emit("order.result", "abcdef123456", {}) == 0
    connect.edit_hook(view["id"], {"enabled": True})
    assert connect.emit("order.result", "abcdef123456", {}) == 0      # master switch still off
    connect.set_webhooks_enabled(True)
    connect.edit_hook(view["id"], {"enabled": False})
    assert connect.emit("order.result", "abcdef123456", {}) == 0
    assert connect.flush(2) and got == [] and env.sent == []
    assert client.post(f"/api/webhooks/{view['id']}/test").status_code == 409


def test_webhook_delivery_is_signed(env, monkeypatch):
    got = capture(env, monkeypatch)
    view, secret = connect.add_hook("https://example.invalid/hook", ["order.result"], enabled=True)
    connect.set_webhooks_enabled(True)
    assert connect.emit("order.question", "abcdef123456", {"text": "?"}) == 0     # not subscribed
    assert connect.emit("order.result", "abcdef123456", {"text": "fertig"}) == 1
    assert connect.flush(5)
    assert len(got) == 1
    req = got[0]
    body = req.content
    assert json.loads(body)["event"] == "order.result" and json.loads(body)["data"] == {"text": "fertig"}
    h = req.headers
    assert h["x-werkstatt-event"] == "order.result" and h["x-werkstatt-signature"].startswith("sha256=")
    assert connect.verify(secret, h["x-werkstatt-timestamp"], body, h["x-werkstatt-signature"])
    assert not connect.verify(secret, h["x-werkstatt-timestamp"], body + b" ", h["x-werkstatt-signature"])
    assert not connect.verify("anderes", h["x-werkstatt-timestamp"], body, h["x-werkstatt-signature"])
    last = connect.webhooks_view()["hooks"][0]["last"]
    assert last["ok"] is True and last["status"] == 200 and last["attempts"] == 1
    assert stat.S_IMODE((env.store / "webhooks.json").stat().st_mode) == 0o600


def test_webhook_retries_and_gives_up(env, monkeypatch):
    got = capture(env, monkeypatch, [500, httpx.ConnectError("down"), 503])
    view, _ = connect.add_hook("https://example.invalid/a", enabled=True)
    connect.set_webhooks_enabled(True)
    connect.emit("order.preview", "abcdef123456", {})
    assert connect.flush(5)
    assert len(got) == 4 and len({r.headers["x-werkstatt-delivery"] for r in got}) == 1
    assert connect.webhooks_view()["hooks"][0]["last"] == {**connect.webhooks_view()["hooks"][0]["last"], "ok": True,
                                                           "attempts": 4}
    got2 = capture(env, monkeypatch, [500] * 10)
    connect.emit("order.preview", "abcdef123456", {})
    assert connect.flush(5)
    assert len(got2) == 5 and connect.webhooks_view()["hooks"][0]["last"]["ok"] is False   # 1 + len(RETRIES)
    got3 = capture(env, monkeypatch, [400])
    connect.emit("order.preview", "abcdef123456", {})
    assert connect.flush(5)
    assert len(got3) == 1 and connect.webhooks_view()["hooks"][0]["last"]["error"] == "HTTP 400"   # no retry on 4xx


def test_switching_off_stops_pending_retries(env, monkeypatch):
    got = capture(env, monkeypatch, [500] * 10)
    monkeypatch.setattr(connect, "RETRIES", (0.3, 0.3, 0.3, 0.3))
    connect.add_hook("https://example.invalid/a", enabled=True)
    connect.set_webhooks_enabled(True)
    connect.emit("order.result", "abcdef123456", {})
    for _ in range(100):
        if got:
            break
        threading.Event().wait(0.01)
    connect.set_webhooks_enabled(False)
    assert connect.flush(5) and len(got) == 1


def test_hook_bound_to_a_key_sees_only_its_orders(env, client, monkeypatch):
    got = capture(env, monkeypatch)
    ka, a = new_key("A")
    kb, b = new_key("B")
    pa = create_order(client, a).json()["order"]
    pb = create_order(client, b).json()["order"]
    connect.add_hook("https://example.invalid/a", key=ka, enabled=True)
    connect.set_webhooks_enabled(True)
    assert connect.emit("order.result", pb, {}) == 0
    assert connect.emit("order.result", pa, {}) == 1
    assert connect.flush(5) and len(got) == 1 and json.loads(got[0].content)["order"] == pa


def test_on_agent_event(env, client, monkeypatch):
    got = capture(env, monkeypatch)
    kid, plain = new_key()
    pid = create_order(client, plain).json()["order"]
    connect.add_hook("https://example.invalid/a", enabled=True)
    connect.set_webhooks_enabled(True)
    connect.on_agent_event(pid, {"n": 9, "kind": "status", "text": "läuft"})                      # not a webhook event
    connect.on_agent_event(pid, {"n": 10, "kind": "result", "role": "agent", "text": "Fertig", "image": "res2.png"})
    connect.on_agent_event({"pid": pid, "n": 11, "kind": "question", "text": "Breite?"})          # cb(event) form
    connect.on_agent_event("bad", None)
    assert connect.flush(5) and len(got) == 2
    res = json.loads(got[0].content)
    assert res["event"] == "order.result" and res["data"]["results"][0]["variant"] == "vb22"
    assert res["data"]["image"] == f"/api/v1/orders/{pid}/files/ereignis_10.png"
    assert json.loads(got[1].content)["event"] == "order.question"


def test_webhook_team_routes(env, client, monkeypatch):
    got = capture(env, monkeypatch)
    assert client.post("/api/webhooks", json={"url": "ftp://x"}).status_code == 400
    assert client.post("/api/webhooks", json={"url": "https://a.invalid/x", "events": ["order.nope"]}).status_code == 400
    assert client.post("/api/webhooks", json={"url": "https://a.invalid/x", "key": "nokey"}).status_code == 400
    r = client.post("/api/webhooks", json={"url": "https://a.invalid/x"}).json()
    assert r["secret"] and r["enabled"] is False and r["events"] == sorted(connect.EVENTS)
    v = client.get("/api/webhooks").json()
    assert v["enabled"] is False and "secret" not in v["hooks"][0] and r["secret"] not in json.dumps(v)
    assert client.patch(f"/api/webhooks/{r['id']}", json={"enabled": "yes"}).status_code == 400
    assert client.patch(f"/api/webhooks/{r['id']}", json={"enabled": True}).json()["enabled"] is True
    assert client.put("/api/webhooks", json={"enabled": 1}).status_code == 400
    assert client.put("/api/webhooks", json={"enabled": True}).json()["enabled"] is True
    assert client.post(f"/api/webhooks/{r['id']}/test").json() == {"queued": True}
    assert connect.flush(5) and json.loads(got[0].content)["event"] == "ping"
    assert client.delete(f"/api/webhooks/{r['id']}").json() == {"ok": True}
    assert client.delete(f"/api/webhooks/{r['id']}").status_code == 404
    assert client.post("/api/webhooks/nope/test").status_code == 404


def test_webhooks_refuse_internal_addresses(env, client, monkeypatch):
    """No probe into this machine (vLLM :8002, customer app :7861), the LAN or cloud metadata (the reviewer's SSRF)."""
    got = capture(env, monkeypatch)
    for url in ("http://127.0.0.1:8002/v1/models", "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/x",
                "http://192.168.178.1/", "http://100.64.0.1/", "http://[::1]:7861/", "http://[::ffff:127.0.0.1]/",
                "http://0.0.0.0:8003/", "http://localhost:11434/", "http://drucker.local/", "https://intern.example/h",
                "https://metadata.example/", "https://v6.example/", "http://224.0.0.1/"):
        r = client.post("/api/webhooks", json={"url": url, "enabled": True})
        assert r.status_code == 400 and "Internet" in r.json()["detail"], url
    view, _ = connect.add_hook("https://example.invalid/hook", enabled=True)
    assert client.patch(f"/api/webhooks/{view['id']}", json={"url": "http://127.0.0.1:7861/"}).status_code == 400
    assert connect.webhooks_view()["hooks"][0]["url"] == "https://example.invalid/hook"
    connect.set_webhooks_enabled(True)
    env.dns["example.invalid"] = "127.0.0.1"                  # DNS changes after the check (rebinding)
    connect.emit("order.result", "abcdef123456", {})
    assert connect.flush(5) and got == []                     # never contacted
    last = connect.webhooks_view()["hooks"][0]["last"]
    assert last["ok"] is False and last["status"] is None and "internen Netz" in last["error"]
    env.dns["example.invalid"] = PUBLIC_IP
    connect.emit("order.result", "abcdef123456", {})
    assert connect.flush(5) and len(got) == 1
    req = got[0]                                              # connected to the checked IP, named by Host and SNI
    assert req.url.host == PUBLIC_IP and req.headers["host"] == "example.invalid"
    assert req.extensions.get("sni_hostname") == "example.invalid"


def test_webhook_never_targets_this_machine_and_lan_needs_opt_in(env, client, monkeypatch):
    got = capture(env, monkeypatch)
    for url in ("http://[2003:d8:174a:6600::7]:8080/", "https://selbst.example/x"):     # own public IPv6
        assert client.post("/api/webhooks", json={"url": url}).status_code == 400, url
    assert client.post("/api/webhooks", json={"url": "http://n8n.lan:5678/webhook/w"}).status_code == 400
    assert client.post("/api/webhooks", json={"url": "http://192.168.178.20:5678/w"}).status_code == 400
    r = client.post("/api/webhooks", json={"url": "http://n8n.lan:5678/webhook/w", "lan": True, "enabled": True})
    assert r.status_code == 200 and r.json()["lan"] is True
    for url in ("http://192.168.178.111:8080/", "http://127.0.0.1:8002/", "http://169.254.169.254/", "http://localhost/",
                "https://selbst.example/x"):
        r2 = client.post("/api/webhooks", json={"url": url, "lan": True})
        assert r2.status_code == 400 and "diesen Rechner" in r2.json()["detail"], url
    assert client.patch(f"/api/webhooks/{r.json()['id']}", json={"lan": False}).status_code == 400   # n8n.lan needs it
    connect.set_webhooks_enabled(True)
    connect.emit("order.result", "abcdef123456", {})
    assert connect.flush(5) and len(got) == 1 and got[0].url.host == "192.168.178.20"
    assert got[0].headers["host"] == "n8n.lan:5678"


def test_own_addresses_are_read_from_the_interfaces(monkeypatch):
    monkeypatch.setattr(connect, "_OWN", {"at": 0.0, "ips": frozenset()})
    own = connect._own_ips()
    assert "127.0.0.1" in own and not connect._allowed_ip("127.0.0.1", lan=True)
    assert all(not connect._allowed_ip(ip, lan=True) for ip in own)


def test_unresolvable_hook_is_retried_not_sent(env, monkeypatch):
    got = capture(env, monkeypatch)
    view, _ = connect.add_hook("https://noch-nicht.example/h", enabled=True)       # accepted, checked at delivery
    connect.set_webhooks_enabled(True)
    connect.emit("order.preview", "abcdef123456", {})
    assert connect.flush(5) and got == []
    assert connect.webhooks_view()["hooks"][0]["last"]["error"] == "Adresse nicht auflösbar"


def test_keys_and_webhooks_need_a_team_password(env, client, monkeypatch):
    """The server listens on 0.0.0.0: without a team password every LAN device could mint keys or send data out."""
    got = capture(env, monkeypatch)
    monkeypatch.setattr(connect, "password_set", REAL_PASSWORD_SET)
    assert client.get("/api/keys").json()["password_set"] is False
    r = client.post("/api/keys", json={"name": "Gast"})
    assert r.status_code == 409 and "Team-Passwort" in r.json()["detail"] and connect.list_keys() == []
    assert client.put("/api/webhooks", json={"enabled": True}).status_code == 409
    assert client.post("/api/webhooks", json={"url": "https://a.invalid/x", "enabled": True}).status_code == 409
    hid = client.post("/api/webhooks", json={"url": "https://a.invalid/x"}).json()["id"]      # off: allowed
    assert client.patch(f"/api/webhooks/{hid}", json={"enabled": True}).status_code == 409
    assert client.put("/api/webhooks", json={"enabled": False}).status_code == 200
    connect.edit_hook(hid, {"enabled": True})                 # switched on by hand: still nothing goes out
    connect.set_webhooks_enabled(True)
    assert connect.emit("order.result", "abcdef123456", {}) == 0 and connect.flush(2) and got == []
    assert client.get("/api/webhooks").json()["password_set"] is False


# ---------------- offer text ----------------

def _doc(**piece):
    return {"title": "TEST Küche Meier", "ref": "2026-014", "removes": [],
            "pieces": [{"id": "p1", "type": "schrank", "w": 220, "h": 250, "d": 60, "material": "weiss", "handles": "push",
                        "text": "3 Türen grifflos", "where": "links neben dem Fenster", **piece}],
            "variants": [{"id": "v1", "piece_ids": ["p1"], "pieces": [{"id": "p1", "label": "Einbauschrank",
                          "material_label": "Eiche natur geölt", "handles_label": "Griffleiste", "w": 180, "h": 240, "d": 60}]}]}


def test_offer_text_positions():
    t = offer.offer_text(_doc())
    assert t.startswith("Angebotspositionen – TEST Küche Meier (Nr. 2026-014)\n")
    assert "Pos. 1   1 Stück Einbauschrank nach Maß" in t
    for s in ("Abmessungen (B × H × T): 220 × 250 × 60 cm", "Material / Oberfläche: Lack weiß matt", "Griffe: grifflos (Push)",
              "Korpus aus 19-mm-Platten", "Rückwand 8 mm", "3 Fronten", "Fachböden", "Sockel zurückgesetzt",
              "Ausführung: 3 Türen grifflos", "Pos. 2 Lieferung und fachgerechte Montage"):
        assert s in " ".join(t.split()), s   # wrapped lines read as one
    assert "Demontage" not in t and "Montage:" not in t
    assert "Lage" not in t and "neben dem Fenster" not in t      # the spoken location phrase stays internal


def test_offer_text_variant_wallhung_removes_and_gaps():
    t = offer.offer_text(_doc(), "v1")
    assert "Entwurf v1" in t and "180 × 240 × 60 cm" in t and "Eiche natur geölt" in t and "Griffleiste" in t
    d = _doc(type="haengeschrank", elev=140, h=None, text="")
    d["removes"] = [[0, 0, 100, 100]]
    t = offer.offer_text(d)
    assert "220 × – × 60 cm (fehlende Maße nach Aufmaß)" in t and "Aufbau" not in t
    assert "Pos. 2   Demontage und Entsorgung" in t and "Pos. 3   Lieferung" in t
    d = _doc(type="haengeschrank", elev=140, w=120, h=60, d=35)
    assert "Montage: wandhängend, Unterkante 140 cm über Fertigfußboden" in offer.offer_text(d)
    tisch = offer.offer_text(_doc(type="tisch", w=200, h=76, d=90, text=""))
    assert "Esstisch" in tisch and "Griffe" not in tisch and "Aufbau" not in tisch
    with pytest.raises(KeyError):
        offer.offer_text(_doc(), "nope")
    assert "Noch keine Möbel geplant." in offer.offer_text({"title": "x", "pieces": [{"id": "a", "type": None}]})


def test_offer_text_wraps_long_notes():
    t = offer.offer_text(_doc(text="Sehr lange Beschreibung " * 20))
    rows = [r for r in t.splitlines() if r.strip()]
    assert max(len(r) for r in rows) <= offer.WIDTH + 1
    assert all(r.startswith(offer.INDENT) for r in rows if "Beschreibung" in r)


# ---------------- DXF ----------------

def sections(pairs):
    names, i = [], 0
    while i < len(pairs):
        if pairs[i] == (0, "SECTION"):
            names.append(pairs[i + 1][1])
        i += 1
    return names


def entities(pairs):
    start = pairs.index((2, "ENTITIES"))
    out, cur = [], None
    for c, v in pairs[start + 1:]:
        if c == 0:
            if cur:
                out.append(cur)
            if v in ("ENDSEC", "EOF"):
                break
            cur = {"type": v, "codes": []}
        else:
            cur["codes"].append((c, v))
            cur.setdefault(c, v)
    return out


def test_dxf_is_valid_r12():
    data = dxf.piece_dxf({"type": "schrank", "text": "3 Türen", "label": "Einbauschrank"}, (220, 250, 60), "TEST Müller")
    pairs = dxf.parse(data)
    assert pairs[0] == (0, "SECTION") and pairs[-1] == (0, "EOF")
    assert sections(pairs) == ["HEADER", "TABLES", "BLOCKS", "ENTITIES"]
    assert (1, "AC1009") in pairs and pairs.count((0, "SECTION")) == pairs.count((0, "ENDSEC"))
    layers = {v for c, v in pairs[pairs.index((2, "LAYER")):pairs.index((2, "STYLE"))] if c == 2} - {"LAYER"}
    ents = entities(pairs)
    assert {e["type"] for e in ents} == {"LINE", "TEXT"}                       # R12 primitives only, no LWPOLYLINE
    assert all(e[8] in layers for e in ents)
    for e in ents:
        if e["type"] == "LINE":
            assert [c for c, _ in e["codes"]] == [8, 10, 20, 30, 11, 21, 31]
    texts = [dxf.unescape(e[1]) for e in ents if e["type"] == "TEXT"]
    for s in ("2200", "2500", "600", "Einbauschrank – Vorderansicht", "Draufsicht", "TEST Müller"):
        assert s in texts, s
    assert any(e[8] == "VERDECKT" for e in ents)          # shelves behind the doors are dashed
    assert sum(1 for e in ents if e[8] == "FRONTEN") >= 3 * 4 - 2
    xs = [float(e[k]) for e in ents for k in (10, 11) if k in e]
    ys = [float(e[k]) for e in ents for k in (20, 21) if k in e]
    ext = dict(pairs[pairs.index((9, "$EXTMIN")) + 1:pairs.index((9, "$EXTMIN")) + 3])
    assert float(ext[10]) <= min(xs) and float(ext[20]) <= min(ys)
    assert data == dxf.piece_dxf({"type": "schrank", "text": "3 Türen", "label": "Einbauschrank"}, (220, 250, 60), "TEST Müller")


def test_dxf_geometry_is_to_scale():
    ents = entities(dxf.parse(dxf.piece_dxf({"type": "sideboard"}, (180, 75, 45))))
    front = [e for e in ents if e["type"] == "LINE" and e[8] != "BEMASSUNG" and float(e[20]) >= 0 and float(e[21]) >= 0]
    xs = [float(e[k]) for e in front for k in (10, 11)]
    ys = [float(e[k]) for e in front for k in (20, 21)]
    assert abs(max(xs) - min(xs) - 1820) < 0.01 and abs(max(ys) - 750) < 0.01 and min(ys) == 0   # top overhangs 1 cm
    top = [e for e in ents if e["type"] == "LINE" and e[8] != "BEMASSUNG" and float(e[20]) < -100 and float(e[21]) < -100]
    ty = [float(e[k]) for e in top for k in (20, 21)]
    assert abs(max(ty) - min(ty) - 460) < 0.01                                 # 45 cm + the top's 1 cm overhang at the front


def test_dxf_wall_hung_and_several_pieces():
    data = dxf.drawing([({"type": "haengeschrank", "elev": 140}, (120, 60, 35), "Hängeschrank"),
                        ({"type": "tisch"}, (200, 76, 90), "Esstisch"), ({"type": None}, (1, 1, 1), "skip")],
                       "TEST", ["Maße in mm"])
    ents = entities(dxf.parse(data))
    texts = [dxf.unescape(e[1]) for e in ents if e["type"] == "TEXT"]
    assert "1400" in texts and "OKFF" in texts and "Esstisch – Vorderansicht" in texts and "Maße in mm" in texts
    assert data.isascii() and b"H\\U+00E4ngeschrank \\U+2013 Vorderansicht" in data   # escapes, not a code page
    assert dxf.escape("Maße × 2") == "Ma\\U+00DFe \\U+00D7 2" and dxf.unescape(dxf.escape("Öl – ß")) == "Öl – ß"
