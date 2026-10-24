import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import live
import projects
import team


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = tmp_path / "projects"
    for k in projects.KINDS:
        (s / k).mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", s)
    monkeypatch.setattr(projects, "job_running", None)
    monkeypatch.setattr(live, "PRESENCE", {})
    monkeypatch.setattr(live, "started", "2026-10-18T09:58:40")
    t = [1000.0]
    monkeypatch.setattr(live, "clock", lambda: t[0])
    app = FastAPI()
    team.install(app)
    for r in (team.router, live.router, projects.router):
        app.include_router(r)
    projects.update_settings(lambda st: st["team"].extend([{"id": "anna", "name": "Anna", "color": "#e879f9"},
                                                            {"id": "ben", "name": "Ben", "color": "#7cc4a0"}]))
    return app, t


def person(app, uid=None):
    c = TestClient(app)
    if uid:
        assert c.post("/api/me", json={"id": uid}).status_code == 200
    else:
        c.get("/api/me")
    return c


def test_doc_only_when_newer(env):
    app, t = env
    a = person(app, "anna")
    pid = a.post("/api/projects/workshop", json={"title": "x"}).json()["id"]
    r = a.get("/api/sync", params={"project": pid, "rev": 0, "view": "raum", "device": "Handy"}).json()
    assert r["project"]["rev"] == 1 and r["project"]["doc"]["id"] == pid and r["project"]["deleted"] is False
    assert r["server"] == {"started": "2026-10-18T09:58:40", "version": "3.0"} and r["me"]["id"] == "anna"
    assert set(r) == {"t", "server", "me", "list_rev", "news", "project", "presence", "queue"}
    r = a.get("/api/sync", params={"project": pid, "rev": 1}).json()
    assert r["project"] == {"rev": 1, "doc": None, "deleted": False}
    projects.mutate("workshop", pid, lambda d: d.update(title="neu"), who="ben")
    r = a.get("/api/sync", params={"project": pid, "rev": 1}).json()
    assert r["project"]["rev"] == 2 and r["project"]["doc"]["title"] == "neu"
    a.delete(f"/api/projects/workshop/{pid}")
    r = a.get("/api/sync", params={"project": pid, "rev": 2}).json()
    assert r["project"] == {"rev": 0, "doc": None, "deleted": True} and r["presence"] == []
    r = a.get("/api/sync", params={"project": "../../etc", "rev": 2}).json()
    assert r["project"]["deleted"] is True


def test_list_rev_increases(env):
    app, t = env
    a = person(app, "anna")
    r0 = a.get("/api/sync").json()
    assert r0["project"] is None and r0["presence"] == []
    pid = a.post("/api/projects/workshop", json={}).json()["id"]
    r1 = a.get("/api/sync").json()["list_rev"]
    assert r1 > r0["list_rev"]
    projects.mutate("workshop", pid, lambda d: d.update(title="x"))
    assert a.get("/api/sync").json()["list_rev"] > r1
    b = a.post("/api/projects/book", json={"title": "B"}).json()["id"]
    r2 = a.get("/api/sync").json()["list_rev"]
    a.put(f"/api/projects/book/{b}", json={"title": "C"})
    assert a.get("/api/sync").json()["list_rev"] > r2


def test_presence(env):
    app, t = env
    a, b, anon = person(app, "anna"), person(app, "ben"), person(app)
    pid = a.post("/api/projects/workshop", json={}).json()["id"]
    other = a.post("/api/projects/workshop", json={}).json()["id"]
    sync = lambda c, **p: c.get("/api/sync", params={"rev": 99, **p}).json()["presence"]
    assert sync(a, project=pid, view="moebel", device="Handy") == []  # own device excluded
    got = sync(b, project=pid, view="platzieren", device="Tablet")
    assert got == [{"user": "anna", "name": "Anna", "color": "#e879f9", "device": "Handy", "view": "moebel",
                    "since": got[0]["since"]}]
    assert sync(anon, project=other, view="raum", device="PC") == []  # per project only
    t[0] += 5
    got = sync(a, project=pid, view="moebel", device="Handy")
    assert [(p["user"], p["view"], p["device"]) for p in got] == [("ben", "platzieren", "Tablet")]
    since = got[0]["since"]
    t[0] += 5
    sync(b, project=pid, view="platzieren", device="Tablet")
    t[0] += 1
    assert sync(a, project=pid, view="moebel")[0]["since"] == since  # same view: since kept
    dev = anon.cookies.get("ws_dev")
    got = sync(anon, project=pid, view="ergebnis", device="Raumschiff")
    assert {p["user"] for p in got} == {"anna", "ben"}
    anon_entry = [p for p in sync(a, project=pid, view="moebel") if p["user"].startswith("dev:")][0]
    assert anon_entry == {**anon_entry, "user": f"dev:{dev[:8]}", "name": f"Gerät {dev[:4]}", "device": "PC", "view": "ergebnis"}
    t[0] += 13  # Ben and the anonymous device stop polling
    assert sync(a, project=pid, view="moebel") == []
    sync(b, project=pid, view="ergebnis")
    sync(b)  # back on the dashboard: Ben's presence on the project ends at once
    assert sync(a, project=pid, view="moebel") == []


def test_queue_passthrough(env, monkeypatch):
    app, t = env
    a = person(app, "anna")
    pid = a.post("/api/projects/workshop", json={}).json()["id"]
    assert a.get("/api/sync").json()["queue"] == {}
    seen = []
    monkeypatch.setattr(live, "queue_view", lambda owner, project: seen.append((owner, project)) or {"busy": True, "wait_s": 95})
    assert a.get("/api/sync", params={"project": pid}).json()["queue"] == {"busy": True, "wait_s": 95}
    assert seen == [("anna", pid)]
    a.get("/api/sync")
    assert seen[-1] == ("anna", None)
    monkeypatch.setattr(live, "queue_view", lambda owner, project: 1 / 0)
    assert a.get("/api/sync").json()["queue"] == {}  # a broken queue never breaks editing


def test_without_middleware(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "STORE", tmp_path)
    (tmp_path / "workshop").mkdir()
    monkeypatch.setattr(live, "PRESENCE", {})
    app = FastAPI()
    app.include_router(live.router)
    r = TestClient(app).get("/api/sync", params={"project": "abcdef123456"}).json()
    assert r["me"] is None and r["project"]["deleted"] is True and r["queue"] == {}
