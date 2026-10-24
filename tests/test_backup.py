import json
import os
import shutil
import sys
import tarfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backup
import projects

PID = "aaaa1111bbbb"


@pytest.fixture
def env(tmp_path, monkeypatch):
    data, target = tmp_path / "data", tmp_path / "usb" / "sicherung"
    out, store = data / "outputs", data / "projects"
    for d in ("uploads", "refs", "guides", "geometry", f"exports/workshop-{PID}/v1", f"exports/workshop-{PID}/entwurf-1"):
        (out / d).mkdir(parents=True)
    for k in projects.KINDS:
        (store / k).mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", store)
    monkeypatch.setattr(backup, "DATA", data)
    monkeypatch.setattr(backup, "LAST", None)
    projects.update_settings(lambda s: s["backup"].update(target=str(target), keep=3))
    files = {"uploads/room.png": b"room", "uploads/logo.png": b"logo", "uploads/fremd.png": b"x",
             "20261018-100000-1.png": b"img", "20261018-100000-1.json": json.dumps({"refs": ["refs/r1.png"]}).encode(),
             "refs/r1.png": b"ref", "20261018-999999-9.png": b"unreferenced", "20261018-999999-9.json": b"{}",
             "20261018-090000-9.png": b"trash", "book1.png": b"book", f"guides/{PID}-0123.png": b"guide",
             f"exports/workshop-{PID}/v1/Visualisierung_V1.pdf": b"pdf", f"exports/workshop-{PID}/entwurf-1/x.pdf": b"draft",
             "geometry/room.json": b"geo"}
    for k, v in files.items():
        (out / k).write_bytes(v)
    doc = {"id": PID, "version": 3, "rev": 1, "title": "TEST", "room": "uploads/room.png",
           "variants": [{"id": "v1room", "image": "20261018-100000-1.png"}],
           "trash": [{"id": "v9trsh", "image": "20261018-090000-9.png"}]}
    (store / "workshop" / f"{PID}.json").write_text(json.dumps(doc))
    (store / "book" / "bbbb2222cccc.json").write_text(json.dumps({"id": "bbbb2222cccc", "pages": [{"image": "book1.png"}]}))
    (store / "backup-v2" / "workshop").mkdir(parents=True)
    (store / "backup-v2" / "workshop" / f"{PID}.json").write_text("{}")
    (store / "shares.json").write_text("{}")
    (store / ".secret").write_text("ab" * 32)
    projects.update_settings(lambda s: s["company"].update(logo="uploads/logo.png"))
    app = FastAPI()
    app.include_router(backup.router)
    return TestClient(app), data, target


def members(path: Path) -> set:
    with tarfile.open(path) as t:
        return set(t.getnames())


def test_archive_contents(env):
    client, data, target = env
    r = client.post("/api/backup")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["count"] == 1 and st["last"]["ok"] and st["last"]["error"] is None and st["running"] is False
    assert st["target"] == str(target) and st["keep"] == 3 and isinstance(st["same_disk"], bool)
    arch = target / st["last"]["file"]
    assert arch.name.startswith("werkstatt-") and arch.name.endswith(".tar.gz") and st["last"]["bytes"] == arch.stat().st_size
    names = members(arch)
    for want in (f"projects/workshop/{PID}.json", "projects/book/bbbb2222cccc.json", "projects/settings.json",
                 "projects/shares.json", f"projects/backup-v2/workshop/{PID}.json", "outputs/uploads/room.png",
                 "outputs/uploads/logo.png", "outputs/20261018-100000-1.png", "outputs/20261018-100000-1.json",
                 "outputs/refs/r1.png", "outputs/20261018-090000-9.png", "outputs/book1.png", f"outputs/guides/{PID}-0123.png",
                 f"outputs/exports/workshop-{PID}/v1/Visualisierung_V1.pdf"):
        assert want in names, want
    for never in ("projects/.secret", "outputs/20261018-999999-9.png", "outputs/uploads/fremd.png",
                  f"outputs/exports/workshop-{PID}/entwurf-1/x.pdf", "outputs/geometry/room.json"):
        assert never not in names, never
    with tarfile.open(arch) as t:
        assert json.loads(t.extractfile(f"projects/workshop/{PID}.json").read())["id"] == PID
    assert json.loads((target / "last.json").read_text()) == st["last"]
    assert not list(target.glob("*.part"))
    assert client.get("/api/backup").json() == st


def test_absolute_paths_are_skipped(env, tmp_path):
    client, data, target = env
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    (data / "projects" / "book" / "evil00000000.json").write_text(json.dumps({"id": "evil00000000", "x": str(outside)}))
    assert outside not in backup.referenced(data)
    assert client.post("/api/backup").status_code == 200 and not list(target.glob("*.part"))


def test_rotation(env, monkeypatch):
    client, data, target = env
    target.mkdir(parents=True)
    for i in range(5):
        (target / f"werkstatt-2026010{i}-030000.tar.gz").write_bytes(b"old")
    (target / "fremd.tar.gz").write_bytes(b"keep me")
    backup.run()
    left = sorted(p.name for p in target.glob("werkstatt-*.tar.gz"))
    assert len(left) == 3 and left[:2] == ["werkstatt-20260103-030000.tar.gz", "werkstatt-20260104-030000.tar.gz"]
    assert (target / "fremd.tar.gz").exists()


def test_concurrent_run_409(env, monkeypatch):
    client, data, target = env
    started, release = threading.Event(), threading.Event()
    real = backup.referenced

    def slow(*a):
        started.set()
        release.wait(10)
        return real(*a)
    monkeypatch.setattr(backup, "referenced", slow)
    th = threading.Thread(target=backup.run)
    th.start()
    assert started.wait(5)
    assert client.get("/api/backup").json()["running"] is True
    r = client.post("/api/backup")
    assert r.status_code == 409 and r.json()["detail"] == "Sicherung läuft bereits."
    release.set()
    th.join()
    assert client.get("/api/backup").json()["count"] == 1


def test_full_target_507(env, monkeypatch):
    client, data, target = env
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10**9, 10**9 - 10, 10))
    r = client.post("/api/backup")
    assert r.status_code == 507 and r.json()["detail"] == "Sicherung fehlgeschlagen: Ziel nicht beschreibbar oder voll."
    st = client.get("/api/backup").json()
    assert st["last"]["ok"] is False and "Platz" in st["last"]["error"] and st["count"] == 0
    assert not list(target.glob("*.part"))


def test_unwritable_target_507(env, tmp_path):
    client, data, target = env
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    projects.update_settings(lambda s: s["backup"].update(target=str(blocker / "sub")))
    r = client.post("/api/backup")
    assert r.status_code == 507
    st = client.get("/api/backup").json()
    assert st["last"]["ok"] is False and st["count"] == 0  # kept in memory although last.json cannot be written


def test_maybe_run(env):
    client, data, target = env
    assert backup.maybe_run()["ok"]
    assert backup.maybe_run() is None  # newest archive younger than 24 h
    arch = next(target.glob("werkstatt-*.tar.gz"))
    old = time.time() - 25 * 3600
    os.utime(arch, (old, old))
    time.sleep(1.1)  # a new stamp
    assert backup.maybe_run()["ok"] and len(list(target.glob("werkstatt-*.tar.gz"))) == 2


def test_same_disk(env, tmp_path):
    client, data, target = env
    assert backup.status()["same_disk"] is True  # tmp_path: the same file system, target not created yet
