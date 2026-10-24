"""The customer link (public.py): token lookup, customer-safe JSON, whitelisted files, respond flow, rate limits,
security headers and the standalone public app. Same tmp data root as test_delivery."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import delivery
import public
from test_delivery import PID, create, env  # noqa: F401  (env is a fixture)

FORBIDDEN = ("phone", "email", "street", "internal_notes", "prompt", "caption", "judge", "seed",
             "0981 777", "kunde@example.org", "Hauptstr", "Rabatt", "Interne Notiz", "Prüftext", "anna", "Anna", "activity")


def edit_version(env, n, **kw):
    env.projects.mutate("workshop", PID, lambda d: next(v for v in d["versions"] if v["n"] == n).update(kw))


def respond(env, token, client=None, **body):
    body = {"variant_id": "v2nuss", "action": "freigeben", "name": "Herr Beispiel", "comment": "", "confirm": True, **body}
    return (client or env.client).post(f"/k/{token}/respond", json=body)


def test_unknown_bad_expired_withdrawn(env):
    token = create(env)["version"]["token"]
    assert env.client.get("/k/kurz/data").status_code == 404
    assert env.client.get("/k/" + "x" * 32 + "/data").status_code == 404
    assert env.client.get(f"/k/{token}/data").status_code == 200
    edit_version(env, 1, expires=(date.today() - timedelta(days=1)).isoformat())
    r = env.client.get(f"/k/{token}/data")
    assert r.status_code == 410 and r.json() == {"detail": "Dieser Link ist nicht mehr gültig.", "company": "Schreinerei Muster",
                                                 "contact": "Werkstr. 3 · 91522 Ansbach · 0981 123456", "tel": "0981 123456"}
    assert env.client.get("/k/" + "y" * 32 + "/data").json()["company"] == "Schreinerei Muster"  # unknown: today's Firmendaten
    assert respond(env, token).status_code == 410
    edit_version(env, 1, expires=date.today().isoformat())  # valid through its last day
    assert env.client.get(f"/k/{token}/data").status_code == 200
    env.client.post(f"/api/workshop/{PID}/versions/1/withdraw")
    for path in ("data", "pdf", "zip", "img/01_Vorher.jpg"):
        assert env.client.get(f"/k/{token}/{path}").status_code == 410, path


def test_payload_is_customer_safe(env):
    token = create(env)["version"]["token"]
    r = env.client.get(f"/k/{token}/data")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"respond", "status", "expires", "company", "title", "customer", "ref", "version", "date", "note",
                      "variants", "response", "approved_variant", "pdf", "zip", "texts"}
    raw = r.text
    for bad in FORBIDDEN:
        assert bad not in raw, bad
    assert d["respond"] is True and d["status"] == "angesehen" and d["version"] == 1 and d["ref"] == "2026-007"
    assert d["company"] == {"name": "Schreinerei Muster", "contact": "Werkstr. 3 · 91522 Ansbach · 0981 123456",
                            "color": "#2f6b4f", "logo": "img/logo.png", "tel": "0981 123456", "mail": "", "web": ""}
    assert d["note"].startswith("Guten Tag Familie Beispiel") and d["customer"] == "Familie Beispiel"
    v = d["variants"][0]
    assert v == {"n": 1, "id": "v1room", "label": "Entwurf 1", "before": "img/01_Vorher.jpg",
                 "after": "img/02_Entwurf-1_Eiche-natur-geoelt_KI-Visualisierung.jpg", "before_label": "Vorher",
                 "after_label": "Nachher", "rows": [{"piece": "Sideboard", "material": "Eiche natur geölt",
                                                     "handles": "grifflos (Push)", "dims": "180 × 75 × 45 cm"}],
                 "focus": [0.3, 0.7]}
    assert d["pdf"] == "pdf" and d["zip"] == "zip" and d["texts"]["ai"] == "KI-Visualisierung – unverbindlich"
    for rel in [v["before"], v["after"], d["company"]["logo"], d["pdf"], d["zip"]]:
        assert env.client.get(f"/k/{token}/{rel}").status_code == 200, rel


def test_company_filled_from_settings_when_snapshot_empty(env):
    env.settings(company={})
    token = create(env)["version"]["token"]
    env.settings(company={"name": "Schreinerei Neu", "phone": "0981 1", "email": "info@neu.example"})
    c = env.client.get(f"/k/{token}/data").json()["company"]
    assert c["name"] == "Schreinerei Neu" and c["tel"] == "0981 1" and c["mail"] == "info@neu.example"


def test_first_view_marks_viewed_once(env):
    token = create(env)["version"]["token"]
    rev = env.doc()["rev"]
    for _ in range(3):
        env.client.get(f"/k/{token}/data")
    doc = env.doc()
    ver = doc["versions"][0]
    assert ver["status"] == "angesehen" and ver["viewed_at"] and doc["rev"] == rev + 1
    assert [a["text"] for a in doc["activity"] if a["what"] == "viewed"] == ["Kunde hat Version 1 angesehen"]
    assert doc["status"] == "kunde" and ver["sent_via"] == "opened"   # opened before any send button: it is out
    assert not any(a["what"] == "version_sent" for a in doc["activity"])


def test_team_view_is_not_the_customers(env, monkeypatch):
    token = create(env)["version"]["token"]
    monkeypatch.setattr(public.team, "is_team", lambda request: True)
    assert env.client.get(f"/k/{token}/data").json()["status"] == "offen"
    assert env.doc()["versions"][0]["status"] == "offen" and not env.doc()["versions"][0].get("viewed_at")
    monkeypatch.setattr(public.team, "is_team", lambda request: False)
    assert env.client.get(f"/k/{token}/data").json()["status"] == "angesehen"


def test_files_whitelisted(env):
    ver = create(env)["version"]
    token = ver["token"]
    r = env.client.get(f"/k/{token}/img/01_Vorher.jpg")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=3600" and r.headers["content-type"] == "image/jpeg"
    for bad in ("img/../x", "img/..%2F..%2Fsettings.json", f"img/{ver['files']['pdf']}", "img/LIESMICH.txt",
                f"img/{ver['files']['zip']}"):
        assert env.client.get(f"/k/{token}/{bad}").status_code == 404, bad
    assert env.client.get(f"/k/{token}/img/..").headers["content-type"].startswith("text/html")  # normalized to the page
    r = env.client.get(f"/k/{token}/pdf")
    assert r.status_code == 200 and ver["files"]["pdf"] in r.headers["content-disposition"] and r.headers["cache-control"] == "private, no-store"


def test_public_app_only_k_routes(env):
    def flat(routes):  # FastAPI ≥ 0.140 keeps included routers as _IncludedRouter wrappers
        for r in routes:
            sub = getattr(r, "original_router", None)
            yield from flat(sub.routes) if sub is not None else [r]

    paths = [getattr(r, "path", "") for r in flat(public.public_app.routes)]
    assert paths and all(p.startswith("/k/") for p in paths), paths
    c = TestClient(public.public_app)
    token = create(env)["version"]["token"]
    assert c.get(f"/k/{token}/data").status_code == 200
    for path in ("/api/projects/workshop", f"/api/workshop/{PID}/versions/1/pdf", "/outputs/uploads/room.png", "/static/kunde.html",
                 "/docs", "/redoc", "/openapi.json", "/"):
        r = c.get(path)
        assert r.status_code == 404, path
        assert r.headers["x-robots-tag"] == "noindex, nofollow" and r.headers["referrer-policy"] == "no-referrer"


def test_headers_everywhere(env):
    token = create(env)["version"]["token"]
    for method, path, code in (("get", f"/k/{token}", 200), ("get", f"/k/{token}/data", 200), ("get", "/k/" + "y" * 30 + "/data", 404),
                               ("post", f"/k/{token}/respond", 400)):
        r = getattr(env.client, method)(path, **({"json": {"action": "freigeben"}} if method == "post" else {}))
        assert r.status_code == code, (path, r.text)
        assert r.headers["x-robots-tag"] == "noindex, nofollow" and r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["content-security-policy"] == public.CSP
        assert r.headers["cache-control"].startswith("private")
    page = env.client.get(f"/k/{token}")
    assert page.headers["content-type"].startswith("text/html") and "<" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"] and "frame-ancestors 'none'" in public.CSP


def test_respond_freigeben(env):
    token = create(env)["version"]["token"]
    r = respond(env, token)
    assert r.status_code == 200 and r.json() == {"ok": True, "status": "freigegeben"}
    doc = env.doc()
    a, ver = doc["approved"], doc["versions"][0]
    assert {k: a[k] for k in ("version", "variant_id", "by", "via", "name")} == \
        {"version": 1, "variant_id": "v2nuss", "by": "kunde", "via": "link", "name": "Herr Beispiel"}
    assert ver["status"] == "freigegeben" and ver["response"]["action"] == "freigeben" and doc["status"] == "freigegeben"
    assert any(x["what"] == "approved" and x["text"] == "Entwurf 2 freigegeben – per Link (Herr Beispiel)" and x["by"] == "kunde"
               for x in doc["activity"])
    again = respond(env, token)
    assert again.status_code == 409 and again.json()["detail"] == "Dieser Entwurf wurde bereits freigegeben."
    d = env.client.get(f"/k/{token}/data").json()
    assert d["respond"] is False and d["approved_variant"] == "v2nuss" and d["response"]["name"] == "Herr Beispiel"
    assert set(d["response"]) == {"variant_id", "action", "name", "comment", "at"}


def test_respond_change_request(env):
    token = create(env)["version"]["token"]
    assert env.doc()["status"] != "kunde"
    r = respond(env, token, action="aenderung", variant_id=None, comment="Bitte Griffe in Messing")
    assert r.status_code == 200 and r.json()["status"] == "aenderung"
    doc = env.doc()
    assert doc["status"] == "entwurf" and doc["versions"][0]["status"] == "aenderung" and doc["approved"] is None
    texts = [x["text"] for x in doc["activity"]]
    assert "Änderungswunsch zu Version 1: „Bitte Griffe in Messing“" in texts and "Status: Beim Kunden → Entwurf" in texts
    assert respond(env, token, action="aenderung", comment="nochmal").status_code == 409
    token2 = create(env)["version"]["token"]  # allowed again with a new version
    assert respond(env, token2, action="aenderung", comment="noch etwas").status_code == 200


def test_respond_validation(env):
    token = create(env)["version"]["token"]
    assert respond(env, token, name="  ").json()["detail"] == "Bitte Ihren Namen angeben."
    assert respond(env, token, name="x" * 81).status_code == 400
    assert respond(env, token, confirm=False).status_code == 400
    assert respond(env, token, comment="x" * 1001).status_code == 400
    assert respond(env, token, action="bestellen").status_code == 400
    assert respond(env, token, variant_id="v3rend").json()["detail"] == "Bitte einen Entwurf auswählen."  # not in this version
    assert env.doc()["versions"][0]["response"] is None
    assert respond(env, "z" * 32).status_code == 404


def test_single_variant_needs_no_choice(env):
    token = create(env, ids=("v3rend",))["version"]["token"]
    assert respond(env, token, variant_id=None).status_code == 200
    assert env.doc()["approved"]["variant_id"] == "v3rend"


def test_xss_stored_verbatim(env):
    token = create(env)["version"]["token"]
    evil = "<img src=x onerror=alert(1)>"
    assert respond(env, token, name=f"<b>Herr X</b>", comment=evil).status_code == 200
    ver = env.doc()["versions"][0]
    assert ver["response"]["name"] == "<b>Herr X</b>" and ver["response"]["comment"] == evil
    assert env.client.get(f"/k/{token}/data").json()["response"]["comment"] == evil


def test_rate_limit_per_token(env):
    token = create(env)["version"]["token"]
    codes = [respond(env, token, name="").status_code for _ in range(public.LIMIT_TOKEN)]
    assert set(codes) == {400}
    r = respond(env, token)
    assert r.status_code == 429 and r.json()["detail"] == "Zu viele Anfragen – bitte später erneut versuchen."
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert env.doc()["versions"][0]["response"] is None


def test_ip_limit_ignores_forwarded_headers_without_trusted_proxy(env, monkeypatch):
    monkeypatch.setattr(public, "LIMIT_IP", 3)
    token = create(env)["version"]["token"]
    c = TestClient(env.app, client=("127.0.0.1", 40000))
    codes = [c.post(f"/k/{token}/respond", json={"action": "freigeben"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
             for i in range(4)]
    assert codes == [400, 400, 400, 429]


def test_ip_limit_uses_forwarded_ip_behind_trusted_proxy(env, monkeypatch):
    monkeypatch.setattr(public, "LIMIT_IP", 3)
    env.settings(trusted_proxy=True)
    token = create(env)["version"]["token"]
    c = TestClient(env.app, client=("127.0.0.1", 40000))
    post = lambda ip: c.post(f"/k/{token}/respond", json={"action": "freigeben"}, headers={"CF-Connecting-IP": ip}).status_code
    assert [post("198.51.100.1") for _ in range(3)] == [400] * 3
    assert post("198.51.100.1") == 429
    assert [post("203.0.113.7") for _ in range(3)] == [400] * 3  # its own bucket
    # a peer that is not the local tunnel cannot pick its bucket
    far = TestClient(env.app, client=("192.0.2.50", 40000))
    far_post = lambda ip: far.post(f"/k/{token}/respond", json={"action": "freigeben"}, headers={"CF-Connecting-IP": ip}).status_code
    assert [far_post(f"10.1.1.{i}") for i in range(4)] == [400, 400, 400, 429]


def test_page_served_for_any_wellformed_token(env, monkeypatch):
    monkeypatch.setattr(public, "PAGE", Path("/nonexistent/kunde.html"))
    r = env.client.get("/k/" + "a" * 32)
    assert r.status_code == 200 and "JavaScript" in r.text
    assert env.client.get("/k/a$b").status_code == 404


# ---------------- PIN-protected links ----------------

def test_pin_link_locked_until_pin(env):
    public._HITS.clear()
    res = create(env, pin=True)
    ver, token = res["version"], res["version"]["token"]
    pin = ver["pin"]
    assert len(pin) == 6 and pin.isdigit()
    r = env.client.get(f"/k/{token}/data")
    assert r.status_code == 401 and r.json()["detail"]["pin"] is True and r.json()["detail"]["company"] == "Schreinerei Muster"
    assert pin not in r.text
    for path in ("pdf", "zip", "img/01_Vorher.jpg"):
        assert env.client.get(f"/k/{token}/{path}").status_code == 401, path
    assert respond(env, token).status_code == 401
    assert env.doc()["versions"][0]["status"] == "offen"   # a locked look is no view
    assert env.client.post(f"/k/{token}/pin", json={"pin": "000000" if pin != "000000" else "111111"}).status_code == 403
    r = env.client.post(f"/k/{token}/pin", json={"pin": f"{pin[:3]} {pin[3:]}"})   # spaces as written on the note
    assert r.status_code == 200
    ck = r.headers["set-cookie"]
    assert "HttpOnly" in ck and f"Path=/k/{token}" in ck and "SameSite=strict" in ck.replace("Strict", "strict")
    d = env.client.get(f"/k/{token}/data")
    assert d.status_code == 200 and "pin" not in d.json() and pin not in d.text
    assert env.client.get(f"/k/{token}/img/01_Vorher.jpg").status_code == 200
    assert respond(env, token).status_code == 200


def test_pin_cookie_is_per_link_and_forgery_fails(env):
    public._HITS.clear()
    a = create(env, pin=True)["version"]
    b = create(env, ids=("v1room",), pin=True)["version"]
    assert env.client.post(f"/k/{a['token']}/pin", json={"pin": a["pin"]}).status_code == 200
    assert env.client.get(f"/k/{a['token']}/data").status_code == 200
    assert env.client.get(f"/k/{b['token']}/data").status_code == 401        # cookie path and signature bind to one link
    env.client.cookies.set(public._pin_cookie(b["token"]), "9999999999.deadbeef", path=f"/k/{b['token']}")
    assert env.client.get(f"/k/{b['token']}/data").status_code == 401
    edit_version(env, 1, pin="123456")                                       # a new PIN locks unlocked devices out
    assert env.client.get(f"/k/{a['token']}/data").status_code == 401


def test_pin_tries_are_limited(env):
    public._HITS.clear()
    ver = create(env, pin=True)["version"]
    wrong = "000000" if ver["pin"] != "000000" else "111111"
    codes = [env.client.post(f"/k/{ver['token']}/pin", json={"pin": wrong}).status_code for _ in range(public.PIN_TOKEN)]
    assert set(codes) == {403}
    r = env.client.post(f"/k/{ver['token']}/pin", json={"pin": ver["pin"]})   # even the right PIN waits now
    assert r.status_code == 429 and "15 Minuten" in r.json()["detail"]


def test_link_without_pin_unchanged_and_team_preview(env, monkeypatch):
    public._HITS.clear()
    plain = create(env)["version"]
    assert "pin" not in plain and env.client.get(f"/k/{plain['token']}/data").status_code == 200
    assert env.client.post(f"/k/{plain['token']}/pin", json={"pin": "x"}).json() == {"ok": True}
    locked = create(env, ids=("v1room",), pin=True)["version"]
    monkeypatch.setattr(public.team, "is_team", lambda request: True)          # the shop previewing its own link
    assert env.client.get(f"/k/{locked['token']}/data").status_code == 200
