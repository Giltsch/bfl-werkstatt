import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import projects
import team


@pytest.fixture
def app(tmp_path, monkeypatch):
    s = tmp_path / "projects"
    for k in projects.KINDS:
        (s / k).mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", s)
    monkeypatch.setattr(team, "_ATTEMPTS", {})
    monkeypatch.setattr(team, "_FAILS", {})
    a = FastAPI()
    team.install(a)
    a.include_router(team.router)
    a.include_router(projects.router)

    @a.get("/api/whoami")
    def whoami(request: Request):
        return {"owner": request.state.owner, "owner_name": request.state.owner_name, "user": request.state.user}

    @a.post("/api/thing")
    def thing():
        return {"ok": True}

    @a.get("/werkstatt")
    def page():
        return {"page": True}

    @a.get("/k/{token}")
    def k(token: str):
        return {"k": token}

    @a.post("/k/{token}/respond")
    def k_post(token: str):
        return {"ok": True}

    @a.get("/static/kunde.html")
    def kunde():
        return {"kunde": True}
    return a


def client(app):
    return TestClient(app, base_url="http://werkstatt.local:7860")


def test_device_cookie_and_owner(app):
    c = client(app)
    r = c.get("/api/whoami")
    dev = r.cookies.get("ws_dev")
    assert dev and len(dev) == 16 and r.json()["owner"] == f"dev:{dev[:8]}" and r.json()["owner_name"] == f"Gerät {dev[:4]}"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    r2 = c.get("/api/whoami")
    assert "ws_dev" not in r2.cookies and r2.json()["owner"] == f"dev:{dev[:8]}"
    me = c.get("/api/me").json()
    assert me == {"user": None, "device": dev, "auth": {"required": False, "ok": True}, "team": []}


def test_team_crud_and_slugs(app):
    c = client(app)
    a = c.post("/api/team", json={"name": "Jürgen Müller"}).json()
    assert a == {"id": "juergen-mueller", "name": "Jürgen Müller", "color": "#59a7ff"}
    b = c.post("/api/team", json={"name": "Jürgen  Müller!", "color": "#e879f9"}).json()
    assert b["id"] == "juergen-mueller-2" and b["color"] == "#e879f9"
    assert c.post("/api/team", json={"name": "Ægir Øst"}).json()["id"] == "aegir-oest"
    assert c.post("/api/team", json={"name": "!!!"}).json()["id"] == "person"
    assert c.post("/api/team", json={"name": "Ein sehr sehr langer Name"}).json()["id"] == "ein-sehr-sehr-langer"
    assert c.post("/api/team", json={"name": " "}).status_code == 400
    assert c.post("/api/team", json={"name": "x", "color": "red"}).status_code == 400
    r = c.patch("/api/team/juergen-mueller", json={"name": "Jürgen", "color": "#f87171"})
    assert r.json() == {"id": "juergen-mueller", "name": "Jürgen", "color": "#f87171"}
    assert c.patch("/api/team/nobody", json={"name": "x"}).status_code == 404
    assert c.delete("/api/team/juergen-mueller-2").json() == {"ok": True}
    assert c.delete("/api/team/juergen-mueller-2").status_code == 404
    ids = [m["id"] for m in c.get("/api/me").json()["team"]]
    assert ids == ["juergen-mueller", "aegir-oest", "person", "ein-sehr-sehr-langer"]
    assert team.owner_name("juergen-mueller") == "Jürgen" and team.owner_name("dev:8f3a1c2e") == "Gerät 8f3a"
    assert team.owner_name("unbekannt") == "unbekannt"


def test_pick_person(app):
    c = client(app)
    c.post("/api/team", json={"name": "Anna", "color": "#e879f9"})
    assert c.post("/api/me", json={"id": "nobody"}).json()["detail"] == "Unbekannte Person."
    r = c.post("/api/me", json={"id": "anna"})
    assert r.json() == {"user": {"id": "anna", "name": "Anna", "color": "#e879f9"}}
    assert "ws_user" in r.cookies
    w = c.get("/api/whoami").json()
    assert w["owner"] == "anna" and w["owner_name"] == "Anna" and w["user"]["name"] == "Anna"
    assert c.get("/api/me").json()["user"]["id"] == "anna"
    # a person created via the API gets her name on edits
    pid = c.post("/api/projects/workshop", json={"title": "x"}).json()["id"]
    assert projects.load("workshop", pid)["created_by"] == "anna"
    c.delete("/api/me")
    assert c.get("/api/whoami").json()["owner"].startswith("dev:")


def test_cookie_tamper_and_removed_member(app):
    c = client(app)
    c.post("/api/team", json={"name": "Anna"})
    c.post("/api/team", json={"name": "Ben"})
    good = team.sign_user("anna")
    uid, exp, sig = good.split(".")
    for bad in (f"ben.{exp}.{sig}", f"anna.{int(exp) + 1}.{sig}", f"anna.{exp}.{'0' * 64}", "anna", "", "a.b.c",
                team.sign_user("anna", days=-1)):
        c.cookies.set("ws_user", bad)
        assert c.get("/api/whoami").json()["user"] is None, bad
    c.cookies.set("ws_user", good)
    assert c.get("/api/whoami").json()["owner"] == "anna"
    c.delete("/api/team/anna")
    assert c.get("/api/whoami").json()["user"] is None  # a removed person's cookie is ignored


def test_secret_file(app, tmp_path):
    team._KEYS.clear()
    team.secret()
    p = tmp_path / "projects" / ".secret"
    assert len(bytes.fromhex(p.read_text())) == 32 and stat.S_IMODE(os.stat(p).st_mode) == 0o600


def set_password(c, pw, current=None):
    return c.put("/api/access", json={"password": pw, "current": current})


def test_password_flow(app):
    c = client(app)
    r = set_password(c, "geheim123")
    assert r.status_code == 200 and r.json() == {"password_set": True} and "ws_auth" in r.cookies
    s = projects.settings()
    assert s["access"]["password"].startswith("pbkdf2_sha256$200000$") and s["access"]["epoch"] == 2
    assert c.get("/api/whoami").status_code == 200  # the caller got a fresh cookie
    other = client(app)
    r = other.get("/api/whoami")
    assert r.status_code == 401 and r.json()["detail"] == "Bitte anmelden."
    assert other.post("/api/thing").status_code == 401
    r = other.get("/werkstatt?x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/werkstatt"
    for path in ("/k/abc", "/static/kunde.html", "/login"):
        assert other.get(path).status_code == 200, path
    assert other.post("/k/abc/respond").status_code == 200
    me = other.get("/api/me").json()
    assert me["auth"] == {"required": True, "ok": False} and me["team"] == [] and me["user"] is None
    assert other.post("/api/login", json={"password": "falsch"}).json()["detail"] == "Passwort stimmt nicht."
    r = other.post("/api/login", json={"password": "geheim123"})
    assert r.status_code == 200 and "ws_auth" in r.cookies
    assert other.get("/api/whoami").status_code == 200 and other.get("/api/me").json()["auth"]["ok"]
    # changing it needs the current password and logs out every other device
    assert set_password(c, "neu12345", "falsch").status_code == 403
    assert set_password(c, "neu", "geheim123").status_code == 400
    assert set_password(c, "neu12345", "geheim123").status_code == 200
    assert other.get("/api/whoami").status_code == 401 and c.get("/api/whoami").status_code == 200
    assert set_password(c, None, "neu12345").json() == {"password_set": False}
    assert client(app).get("/api/whoami").status_code == 200
    assert "pbkdf2" not in json.dumps(c.get("/api/settings").json())


def test_login_rate_limit(app):
    c = client(app)
    set_password(c, "geheim123")
    other = client(app)
    codes = [other.post("/api/login", json={"password": "x"}).status_code for _ in range(6)]
    assert codes == [403] * 5 + [429]
    assert other.post("/api/login", json={"password": "geheim123"}).json()["detail"] == "Zu viele Versuche – bitte eine Minute warten."
    t = team.clock()
    team.clock = lambda: t + 61
    try:
        assert other.post("/api/login", json={"password": "geheim123"}).status_code == 200
    finally:
        team.clock = __import__("time").time


def test_login_rate_limit_per_ip_without_cookies(app):
    c = client(app)
    set_password(c, "geheim123")
    codes = [client(app).post("/api/login", json={"password": "x"}).status_code for _ in range(12)]
    assert codes == [403] * team.FREE_FAILS + [429] * 2  # then the growing lockout
    t = team.clock()
    try:
        team.clock = lambda: t + 61  # 1 min after the 10th failure: one more try, then 2 min
        assert client(app).post("/api/login", json={"password": "x"}).status_code == 403
        assert client(app).post("/api/login", json={"password": "geheim123"}).status_code == 429
        team.clock = lambda: t + 61 + 121
        assert client(app).post("/api/login", json={"password": "geheim123"}).status_code == 200
        team.clock = lambda: t + 61 + 122
        assert team._FAILS == {}
    finally:
        team.clock = __import__("time").time


def test_status_open_for_loopback_and_host_allowlist(app):
    c = client(app)

    @app.get("/api/status")
    def status():
        return {"ok": True}
    set_password(c, "geheim123")
    assert client(app).get("/api/status").status_code == 401  # TestClient's peer is "testclient", not loopback
    loop = TestClient(app, base_url="http://127.0.0.1:7860", client=("127.0.0.1", 50000))
    assert loop.get("/api/status").status_code == 200
    assert loop.get("/api/status", headers={"Origin": "http://evil.example"}).status_code == 401
    assert loop.get("/api/whoami").status_code == 401
    for host in ("192.168.1.20:7860", "[fe80::1]:7860", "spark", "spark.local:7860", "localhost:7860"):
        assert loop.get("/api/status", headers={"Host": host}).status_code == 200, host
    r = loop.get("/api/status", headers={"Host": "attacker.example:7860"})
    assert r.status_code == 400 and "Hostname" in r.json()["detail"]
    assert team.host_ok("kunden.schreinerei.de", {"public_base_url": "https://kunden.schreinerei.de"})
    assert not team.host_ok("evil.schreinerei.de", {"public_base_url": "https://kunden.schreinerei.de"})
    r = loop.post("/api/login", content=b"x", headers={"Content-Length": str(team.MAX_BODY + 1)})
    assert r.status_code == 413


def test_password_min_length(app):
    c = client(app)
    r = set_password(c, "1234567")
    assert r.status_code == 400 and r.json()["detail"] == "Das Passwort muss mindestens 8 Zeichen haben."


def test_same_origin_writes(app):
    c = client(app)
    assert c.post("/api/thing").status_code == 200  # no Origin/Referer (curl, tools)
    assert c.post("/api/thing", headers={"Origin": "http://werkstatt.local:7860"}).status_code == 200
    r = c.post("/api/thing", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["detail"] == "Anfrage von einer fremden Seite abgelehnt."
    assert c.post("/api/thing", headers={"Origin": "http://werkstatt.local:7861"}).status_code == 403
    assert c.post("/api/thing", headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/thing", headers={"Referer": "http://evil.example/x"}).status_code == 403
    assert c.post("/api/thing", headers={"Referer": "http://werkstatt.local:7860/werkstatt"}).status_code == 200
    assert c.post("/k/abc/respond", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/whoami", headers={"Origin": "http://evil.example"}).status_code == 200  # reads are not checked


def test_safe_next(app):
    for good in ("/werkstatt", "/werkstatt#/p/abc", "/book?x=1"):
        assert team.safe_next(good) == good
    for bad in ("//evil.com", "/\\evil.com", "https://evil.com", "javascript:x", "", "werkstatt", "/x\ny", None, "/\x7f"):
        assert team.safe_next(bad) == "/werkstatt", bad
    c = client(app)
    html = c.get("/login", params={"next": "//evil.com"}).text
    assert 'const NEXT = "/werkstatt";' in html and "evil" not in html
    html = c.get("/login", params={"next": "/book</script><script>alert(1)</script>"}).text
    assert "</script><script>alert" not in html and "\\u003c/script\\u003e" in html
    assert "Werkstatt – Anmeldung" in html and "Passwort" in html and "<script src" not in html


def test_is_team(app):
    c = client(app)
    @app.get("/api/isteam")
    def isteam(request: Request):
        return {"team": team.is_team(request)}
    assert c.get("/api/isteam").json() == {"team": False}
    uid = c.post("/api/team", json={"name": "Anna"}).json()["id"]
    c.post("/api/me", json={"id": uid})
    assert c.get("/api/isteam").json() == {"team": True}
    other = client(app)
    set_password(c, "geheim123")
    assert other.get("/api/isteam").status_code == 401
    other.post("/api/login", json={"password": "geheim123"})
    assert other.get("/api/isteam").json() == {"team": True}
