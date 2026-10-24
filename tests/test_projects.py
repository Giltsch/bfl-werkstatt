import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

import projects

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
LEGACY = FIX / "legacy_v1_0db93df20e67.json"
REAL = {pid: FIX / f"real_v2_{pid}.json" for pid in ("0db93df20e67", "bb505aeddc04")}


@pytest.fixture
def legacy():
    return json.loads(LEGACY.read_text())  # frozen anonymized copy; every test works on this copy


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = tmp_path / "projects"
    for k in projects.KINDS:
        (s / k).mkdir(parents=True)
    (tmp_path / "outputs" / "uploads").mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", s)
    monkeypatch.setattr(projects, "OUT", tmp_path / "outputs")
    monkeypatch.setattr(projects, "UPLOADS", tmp_path / "outputs" / "uploads")
    monkeypatch.setattr(projects, "EXPORTS", tmp_path / "outputs" / "exports")
    monkeypatch.setattr(projects, "job_running", None)
    app = FastAPI()

    @app.middleware("http")
    async def who(request: Request, call_next):  # stands in for team.install: X-Who header → owner
        request.state.owner = request.headers.get("x-who") or "anna"
        return await call_next(request)
    app.include_router(projects.router)
    return TestClient(app), tmp_path


def put_doc(tmp, doc, pid=None):
    pid = pid or doc["id"]
    (tmp / "projects" / "workshop" / f"{pid}.json").write_text(json.dumps(doc, ensure_ascii=False))
    return pid


def new(client, **body):
    r = client.post("/api/projects/workshop", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def patch(client, pid, base, *ops, who="anna"):
    return client.patch(f"/api/projects/workshop/{pid}", json={"base_rev": base, "ops": list(ops)}, headers={"x-who": who})


def s(path, value):
    return {"op": "set", "path": path, "value": value}


def real_store(tmp):
    for pid, f in REAL.items():
        shutil.copy(f, tmp / "projects" / "workshop" / f"{pid}.json")


# ---------------- migration ----------------

def test_migrate_legacy(legacy):
    before = LEGACY.read_text()
    doc = projects.migrate_workshop(copy.deepcopy(legacy))
    assert LEGACY.read_text() == before
    assert doc["version"] == 3 and doc["rev"] == 1 and doc["frev"] == {} and doc["frev_floor"] == 0
    for k in projects.LEGACY_KEYS:
        assert k not in doc
    assert len(doc["pieces"]) == 1
    p = doc["pieces"][0]
    assert p["type"] == "kueche" and p["label"] == "Küche" and (p["w"], p["h"], p["d"]) == (180, 250, 60)
    assert p["material"] == "eiche" and p["material_label"] == "Eiche natur geölt" and p["handles_label"] == "grifflos (Push)"
    assert p["placement"] == {"mode": "2d", "pose": None, "box": [150, 245, 780, 545]}
    assert p["text"] == "Besonderheiten (anonymisiert)"
    assert doc["title"] == "Beispielraum" and doc["customer"] == "Familie Beispiel"
    assert doc["active_piece"] == p["id"] and doc["removes"] == [[800, 600, 920, 800]]
    assert doc["judge"] == {"enabled": True, "auto_retry": True} and doc["guide"] is False
    assert doc["geometry"] is None and doc["calibration"] is None
    assert doc["deleted_variants"] == [] and doc["pending_jobs"] == []
    assert doc["count"] == 2 and doc["res"] == 1536 and doc["tidy"] and doc["protect"]
    assert doc["status"] == "entwurf" and doc["quality"] == "fein" and doc["ref"] == "" and doc["test"] is False
    assert doc["contact"] == {"salutation": "", "phone": "", "email": "", "street": "", "zip": "", "city": ""}
    assert doc["appointment"] is None and doc["versions"] == [] and doc["approved"] is None and doc["trash"] == []
    images = [v["image"] for v in doc["variants"]]
    assert images == sorted(images, reverse=True) and len(images) == 7
    v = {v["id"]: v for v in doc["variants"]}
    fav = v["cw5ky2zx"]
    assert fav["kind"] == "material" and fav["fav"] and fav["locked"] is False
    assert fav["pieces"] == [{"id": p["id"], "label": "Einbauschrank", "material_label": "Nussbaum",
                              "handles_label": "grifflos (Push)", "w": 180, "h": 250, "d": 60}]
    assert fav["boxes"] == {}
    assert v["oty2dff1"]["kind"] == "room" and v["oty2dff1"]["boxes"] == {p["id"]: [150, 245, 780, 545]}
    assert v["oty2dff1"]["pieces"][0]["label"] == "Küche"
    assert all(x["piece_ids"] == [p["id"]] and x["judge"] is None for x in doc["variants"])
    assert doc["selected"] == "oty2dff1"
    assert doc["thumb"] == fav["image"]  # first favourite


def test_migrate_idempotent_and_stable(legacy):
    a = projects.migrate_workshop(copy.deepcopy(legacy))
    b = projects.migrate_workshop(copy.deepcopy(a))
    assert a == b  # raw version 3 → only normalized, rev unchanged
    c = projects.migrate_workshop(copy.deepcopy(legacy))  # an unsaved v1 doc loads with the same ids every time
    assert c["pieces"][0]["id"] == a["pieces"][0]["id"]


def test_migrate_real_v2_fixtures():
    for pid, f in REAL.items():
        raw = json.loads(f.read_text())
        doc = projects.migrate_workshop(copy.deepcopy(raw))
        assert doc["version"] == 3 and doc["status"] == "entwurf" and doc["quality"] == "fein"
        for k in ("title", "customer", "geometry", "room", "room_size", "created"):
            assert doc[k] == raw.get(k)
        assert doc["pieces"] == raw["pieces"]
        assert [{k: x for k, x in v.items() if k != "locked"} for v in doc["variants"]] == \
            sorted(raw["variants"], key=lambda v: v["image"], reverse=True)


def test_migrate_infers_status():
    base = {"version": 2, "id": "abcdef123456"}
    assert projects.migrate_workshop(dict(base))["status"] == "anfrage"
    assert projects.migrate_workshop({**base, "room": "uploads/a.png"})["status"] == "termin"
    assert projects.migrate_workshop({**base, "status": "kunde", "variants": [{"image": "a.png"}]})["status"] == "kunde"
    assert projects.migrate_workshop({**base, "res": 1024})["quality"] == "entwurf"
    assert projects.migrate_workshop({**base, "res": 1024, "quality": "fein"})["quality"] == "fein"


def test_v2_with_legacy_keys_not_remigrated(legacy):
    v2 = projects.migrate_workshop(copy.deepcopy(legacy))
    old_ui = {"piece_type": "schrank", "piece_text": "", "w": "", "h": "", "d": "", "box": None, "sketch": None,
              "material": "eiche", "handles": "push", "where": "", "dims": "x", **copy.deepcopy(v2)}
    doc = projects.migrate_workshop(old_ui)
    assert len(doc["pieces"]) == 1 and doc["pieces"][0]["id"] == v2["pieces"][0]["id"]
    for k in projects.LEGACY_KEYS:
        assert k not in doc


def test_migrate_all(store):
    client, tmp = store
    real_store(tmp)
    legacy = json.loads(LEGACY.read_text())
    put_doc(tmp, {**legacy, "id": "aaaa11112222", "title": "TEST alt", "created": "2025-01-01T00:00:00"})
    rep = projects.migrate_all()
    assert [r["id"] for r in rep] == ["aaaa11112222", "0db93df20e67", "bb505aeddc04"]  # oldest created first
    by = {r["id"]: r for r in rep}
    assert by["0db93df20e67"] == {"id": "0db93df20e67", "from": 2, "status": "entwurf", "ref": "2026-001", "again": False}
    assert by["bb505aeddc04"]["ref"] == "2026-002" and by["aaaa11112222"]["ref"] == "" and by["aaaa11112222"]["from"] == 1
    assert projects.settings()["next_ref"] == {"2026": 3}
    for pid, f in REAL.items():
        assert (tmp / "projects/backup-v2/workshop" / f"{pid}.json").read_bytes() == f.read_bytes()
        doc = projects.load("workshop", pid)
        raw = json.loads(f.read_text())
        assert doc["pieces"] == raw["pieces"] and doc["title"] == raw["title"] and doc["customer"] == raw["customer"]
        assert doc["geometry"] == raw.get("geometry") and doc["updated"] == raw["updated"]
        assert [a["what"] for a in doc["activity"]] == ["migrated"] and doc["activity"][0]["by"] == "system"
    snap = {p: p.read_bytes() for p in (tmp / "projects").rglob("*.json")}
    assert projects.migrate_all() == []
    assert {p: p.read_bytes() for p in (tmp / "projects").rglob("*.json")} == snap


def test_migrate_all_never_overwrites_backup(store):
    client, tmp = store
    real_store(tmp)
    bk = tmp / "projects/backup-v2/workshop/0db93df20e67.json"
    bk.parent.mkdir(parents=True)
    bk.write_text('{"older": true}')
    rep = {r["id"]: r for r in projects.migrate_all()}
    assert bk.read_text() == '{"older": true}' and rep["0db93df20e67"]["again"] is True


def load_v2(tmp):
    d = tmp / "v2code"
    d.mkdir()
    shutil.copy(FIX / "projects_v2.py.txt", d / "projects_v2.py")
    spec = importlib.util.spec_from_file_location("projects_v2_test", d / "projects_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # its module-level mkdirs land in the temp dir
    return mod


def test_rollback_round_trip(store):
    client, tmp = store
    real_store(tmp)
    projects.migrate_all()
    pid = "bb505aeddc04"
    f = tmp / "projects/workshop" / f"{pid}.json"
    doc = json.loads(f.read_text())
    doc.update(status="kunde", rev=17)
    projects.log(doc, "meta", "Kundendaten geändert", "anna")
    f.write_text(json.dumps(doc))
    nr = projects.settings()["next_ref"]
    v2 = load_v2(tmp)
    back = v2.normalize_workshop(copy.deepcopy(doc))  # what the v2 code saves after a rollback
    assert back["version"] == 2 and back["status"] == "kunde" and back["ref"] == "2026-002" and back["rev"] == 17
    f.write_text(json.dumps(back))
    rep = projects.migrate_all()
    assert [r["id"] for r in rep] == [pid] and rep[0]["again"] is True
    got = projects.load("workshop", pid)
    assert got["ref"] == "2026-002" and got["status"] == "kunde" and got["rev"] == 18 and got["frev_floor"] == 18
    assert got["activity"] == doc["activity"] and [a["what"] for a in got["activity"]].count("migrated") == 1
    assert projects.settings()["next_ref"] == nr
    r = patch(client, pid, 17, s("title", "x"))  # a client from before the rollback must re-fetch
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale"


def test_migrate_check_tool(store):
    client, tmp = store
    real_store(tmp)
    sys.path.insert(0, str(ROOT / "tools"))
    import migrate_check
    src = tmp / "projects"
    before = {p: p.read_bytes() for p in src.rglob("*") if p.is_file()}
    assert migrate_check.main(["--src", str(src)]) == 0
    assert {p: p.read_bytes() for p in src.rglob("*") if p.is_file()} == before  # never writes to --src


def test_test_flag():
    t = lambda title: projects.normalize_workshop({"version": 3, "id": "abcdef123456", "title": title})["test"]
    assert t("TEST e2e") and t("  TEST") and t("TEST-Küche")
    assert not t("Testa Küche") and not t("test") and not t("TESTER") and not t("Mein TEST")


def test_empty_doc_gets_default_piece():
    doc = projects.normalize_workshop({"version": 3, "title": "Neues Projekt", "id": "abcdef123456"})
    assert len(doc["pieces"]) == 1
    p = doc["pieces"][0]
    assert p["type"] is None and p["label"] == "" and p["material"] == "eiche"
    assert p["placement"]["mode"] == "none" and p["w"] is None
    assert doc["active_piece"] == p["id"] and doc["variants"] == [] and doc["selected"] is None
    assert doc["status"] == "anfrage" and doc["thumb"] is None


def test_normalize_clamps_garbage():
    doc = projects.normalize_workshop({
        "version": 3, "id": "abcdef123456", "count": 9, "res": 777, "tidy": "yes", "room": "../etc/passwd.png",
        "removes": [[-5, 10, 2000, 500], [500, 500, 400, 600], "x", [1, 2, 3], [0, 0, 10, 10]] + [[0, 0, 5, 5]] * 30,
        "geometry": {"big": "x" * 70000}, "pending_jobs": ["abc123", "NOTHEX!", "abc123"] + [f"{i:06x}" for i in range(30)],
        "deleted_variants": [f"v{i:04d}" for i in range(600)], "status": "fertigx", "quality": "ultra",
        "contact": {"phone": "1" * 99, "evil": 1}, "appointment": {"at": "morgen"}, "rev": -4, "frev": {"a": "x", "b": [3, "x", "t"]},
        "activity": ["x"] + [{"at": "t", "by": "a", "what": "w", "text": str(i)} for i in range(400)],
        "trash": [{"id": "BAD", "image": "a.png"}, {"id": "okok11", "image": "t.png", "locked": True}],
        "pieces": [{"id": "BAD", "type": "rakete", "w": "abc", "h": 99999, "d": "60", "text": "t" * 5000, "label": "L" * 200,
                    "material": "gold", "handles": "x", "sketch": "a/../b.png",
                    "placement": {"mode": "3d", "pose": None, "box": [1, 2, 3, 4]}},
                   {"id": "p2abcd", "placement": {"mode": "2d", "box": [10, 10, 5, 50]}},
                   {"id": "p2abcd", "placement": {"mode": "3d", "pose": {"x": "1", "z": 300, "yaw": 0, "wall": "w0"}, "box": None}},
                   {}, {}, {}],
        "variants": [{"id": "Q", "image": "a.png", "fav": "yes"}, {"id": "zzzzzz", "image": "../x.png"}, "junk",
                     {"id": "okokok", "image": "b.png", "pieces": "bad", "boxes": {"p": [0, 0, 10, 10]}}]})
    assert doc["count"] == 4 and doc["res"] == 1536 and doc["tidy"] is True and doc["room"] is None
    assert doc["removes"][0] == [0, 10, 1000, 500] and len(doc["removes"]) == 20
    assert all(b[2] > b[0] and b[3] > b[1] for b in doc["removes"])
    assert doc["geometry"] is None
    assert len(doc["pending_jobs"]) == 20 and "NOTHEX!" not in doc["pending_jobs"]
    assert len(doc["deleted_variants"]) == 500 and doc["deleted_variants"][-1] == "v0599"
    assert doc["status"] == "entwurf" and doc["quality"] == "fein" and doc["rev"] == 1
    assert doc["contact"]["phone"] == "1" * 40 and "evil" not in doc["contact"] and doc["appointment"] is None
    assert doc["frev"] == {"b": [3, "x", "t"]} and len(doc["activity"]) == 300 and doc["activity"][-1]["text"] == "399"
    assert [t["id"] for t in doc["trash"]] == ["okok11"] and "locked" not in doc["trash"][0]
    assert len(doc["pieces"]) == 4
    a, b, c = doc["pieces"][:3]
    assert len({p["id"] for p in doc["pieces"]}) == 4 and a["id"] != "BAD"
    assert a["type"] is None and a["w"] is None and a["h"] is None and a["d"] == 60
    assert len(a["text"]) == 2000 and len(a["label"]) == 80 and a["material"] == "eiche" and a["handles"] is None
    assert a["sketch"] is None and a["placement"]["mode"] == "none"
    assert b["placement"]["mode"] == "none"
    assert c["placement"] == {"mode": "3d", "pose": {"x": 1.0, "z": 300.0, "yaw": 0.0, "wall": "w0"}, "box": None}
    assert [v["image"] for v in doc["variants"]] == ["b.png", "a.png"]
    va = doc["variants"][1]
    assv = doc["variants"][0]
    assert va["fav"] is False and len(va["id"]) == 8 and va["judge"] is None
    assert "pieces" not in assv and assv["boxes"] == {"p": [0, 0, 10, 10]}
    assert projects.normalize_workshop(copy.deepcopy(doc)) == doc  # idempotent


def test_locked_and_thumb():
    doc = projects.normalize_workshop({"version": 3, "id": "abcdef123456", "room": "uploads/r.png",
                                       "variants": [{"id": "va1111", "image": "1.png"}, {"id": "vb2222", "image": "2.png", "fav": True},
                                                    {"id": "vc3333", "image": "3.png"}],
                                       "versions": [{"n": 1, "status": "zurueckgezogen", "variant_ids": ["vc3333"]},
                                                    {"n": 2, "status": "offen", "variant_ids": ["va1111"]}]})
    assert {v["id"]: v["locked"] for v in doc["variants"]} == {"va1111": True, "vb2222": False, "vc3333": False}
    assert doc["thumb"] == "2.png"
    doc["approved"] = {"variant_id": "vc3333", "version": 1}
    projects.normalize_workshop(doc)
    assert doc["thumb"] == "3.png" and next(v for v in doc["variants"] if v["id"] == "vc3333")["locked"]
    assert projects.normalize_workshop({"version": 3, "id": "abcdef123456", "room": "uploads/r.png"})["thumb"] == "uploads/r.png"


# ---------------- create, list, PUT, settings ----------------

def test_create_contract(store):
    client, tmp = store
    d = new(client, title="Einbauschrank Flur", customer="Familie Müller", contact={"phone": "0981 1234", "city": "Ansbach"},
            appointment=None, evil="x")
    assert d["status"] == "anfrage" and d["rev"] == 1 and d["version"] == 3 and d["created_by"] == "anna"
    assert d["ref"] == f"{datetime.now().year}-001" and d["quality"] == "entwurf" and d["count"] == 2
    assert d["judge"] == {"enabled": True, "auto_retry": False} and "evil" not in d
    assert len(d["pieces"]) == 1 and d["pieces"][0]["type"] is None and d["pieces"][0]["handles"] == "push"
    assert d["contact"]["phone"] == "0981 1234" and d["contact"]["city"] == "Ansbach"
    assert [a["what"] for a in d["activity"]] == ["created"] and d["activity"][0]["text"] == "Auftrag angelegt"
    assert new(client)["ref"] == f"{datetime.now().year}-002"
    t = new(client, title="TEST e2e")
    assert t["ref"] == "" and t["test"] is True
    assert new(client, title="Mit Termin", appointment={"at": "2030-01-02T09:00"})["status"] == "termin"
    raw = json.loads((tmp / "projects/workshop" / f"{d['id']}.json").read_text())
    assert raw["version"] == 3 and raw["rev"] == 1


def test_get_missing(store):
    client, tmp = store
    r = client.get("/api/projects/workshop/abcdef123456")
    assert r.status_code == 404 and r.json()["detail"] == "Auftrag nicht gefunden."
    with pytest.raises(projects.Gone):
        projects.mutate("workshop", "abcdef123456", lambda d: None)
    assert isinstance(projects.Gone(), HTTPException) and projects.title("abcdef123456") == ""


def test_put_workshop_refused(store):
    client, tmp = store
    d = new(client, title="x")
    r = client.put(f"/api/projects/workshop/{d['id']}", json=d)
    assert r.status_code == 409 and r.json()["detail"] == "Die Werkstatt wurde aktualisiert – bitte die Seite neu laden."


def test_list_payload(store):
    client, tmp = store
    d = new(client, title="Esszimmer", customer="Behrens", contact={"city": "Ansbach", "email": "a@b.de"},
            internal_notes="Hund beißt")
    new(client, title="TEST x")
    patch(client, d["id"], 1, s("ext_ref", "A-123"), who="ben")
    projects.update_settings(lambda st: st["team"].append({"id": "ben", "name": "Ben", "color": "#7cc4a0"}))
    items = client.get("/api/projects/workshop").json()
    assert len(items) == 2 and [x["updated"] for x in items] == sorted((x["updated"] for x in items), reverse=True)
    it = next(x for x in items if x["id"] == d["id"])
    assert set(it) == {"id", "title", "customer", "status", "ref", "ext_ref", "city", "appointment", "archived_at", "test",
                       "retention_due", "created_by", "thumb", "updated", "updated_by", "updated_by_name", "variants",
                       "versions", "approved", "attention", "new", "outdated", "pending", "rev", "q"}
    assert it["updated_by"] == "ben" and it["updated_by_name"] == "Ben" and it["city"] == "Ansbach" and it["rev"] == 2
    assert all(w in it["q"] for w in ("esszimmer", "behrens", "a-123", "ansbach", "a@b.de", "hund beißt", it["ref"]))
    assert next(x for x in items if x["id"] != d["id"])["test"] is True
    e = new(client)
    assert e["title"] == "" and next(x for x in client.get("/api/projects/workshop").json() if x["id"] == e["id"])["title"] == "Ohne Titel"
    patch(client, d["id"], 2, {"op": "archive"})
    assert d["id"] not in [x["id"] for x in client.get("/api/projects/workshop").json()]
    assert [x["id"] for x in client.get("/api/projects/workshop?archived=1").json()] == [d["id"]]
    assert len(client.get("/api/projects/workshop?archived=all").json()) == 3


def test_list_attention_and_retention(store):
    client, tmp = store
    now = datetime.now()
    mk = lambda **kw: put_doc(tmp, {"version": 3, "id": kw.pop("id"), "rev": 1, "updated": now.isoformat(timespec="seconds"), **kw})
    old = (now - timedelta(days=200)).isoformat(timespec="seconds")
    mk(id="aaaaaa000001", appointment={"at": now.strftime("%Y-%m-%dT23:59")})
    mk(id="aaaaaa000002", appointment={"at": "2020-01-01T09:00"}, status="termin")
    mk(id="aaaaaa000003", appointment={"at": "2020-01-01T09:00"}, status="entwurf")
    mk(id="aaaaaa000004", status="freigegeben", approved={"variant_id": "x", "via": "link"})
    mk(id="aaaaaa000005", status="kunde", versions=[{"n": 1, "status": "offen"}, {"n": 2, "status": "aenderung"}])
    mk(id="aaaaaa000006", status="abgesagt", activity=[{"at": old, "by": "a", "what": "status", "text": "x"}])
    mk(id="aaaaaa000007", status="abgesagt", activity=[{"at": now.isoformat(), "by": "a", "what": "status", "text": "x"}])
    mk(id="aaaaaa000008", status="fertig", activity=[{"at": old, "by": "a", "what": "status", "text": "x"}])
    mk(id="aaaaaa000009", archived_at=old, updated=old, activity=[])
    got = {x["id"][-1]: x for x in client.get("/api/projects/workshop?archived=all").json()}
    assert {k: v["attention"] for k, v in got.items()} == {"1": "termin_heute", "2": "ueberfaellig", "3": None,
                                                           "4": "freigegeben", "5": "aenderung", "6": None, "7": None,
                                                           "8": None, "9": None}
    assert {k for k, v in got.items() if v["retention_due"]} == {"6", "9"}  # fertig: 12 months
    projects.update_settings(lambda st: st["retention"].update(fertig_months=6))
    assert {x["id"][-1] for x in client.get("/api/projects/workshop?archived=all").json() if x["retention_due"]} == {"6", "8", "9"}


def test_book_put_and_create(store):
    client, tmp = store
    book = {"title": "B", "pages": [{"text": "x", "image": None}], "w": "weird", "box": [1]}
    r = client.post("/api/projects/book", json=book).json()
    got = client.get(f"/api/projects/book/{r['id']}").json()
    assert got["w"] == "weird" and got["box"] == [1] and "version" not in got and "pieces" not in got and got["rev"] == 1
    out = client.put(f"/api/projects/book/{r['id']}", json={**got, "extra": 1, "rev": 99}).json()
    assert out["rev"] == 2 and client.get(f"/api/projects/book/{r['id']}").json()["extra"] == 1
    assert client.put("/api/projects/book/abcdef123456", json={}).status_code == 404
    assert client.get("/api/projects/book").json()[0]["title"] == "B"


def test_book_put_concurrent(store):
    client, tmp = store
    pid = client.post("/api/projects/book", json={"title": "B"}).json()["id"]
    codes = []

    def put(i):
        codes.append(client.put(f"/api/projects/book/{pid}", json={"title": f"B{i}", "pages": [{"text": "x" * i}]}).status_code)
    ts = [threading.Thread(target=put, args=(i,)) for i in range(50)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes == [200] * 50
    raw = json.loads((tmp / "projects/book" / f"{pid}.json").read_text())
    assert raw["rev"] == 51 and not list((tmp / "projects/book").glob("*.tmp")) and not list((tmp / "projects/book").glob(".*"))


def test_delete_bumps_list_rev(store):
    client, tmp = store
    d = new(client)
    r0 = projects.list_rev()
    assert client.delete(f"/api/projects/workshop/{d['id']}").json() == {"ok": True}
    assert projects.list_rev() > r0 and client.get(f"/api/projects/workshop/{d['id']}").status_code == 404


def test_settings(store):
    client, tmp = store
    g = client.get("/api/settings").json()
    assert set(g) == {"company", "team", "access", "retention", "public_base_url", "trusted_proxy", "backup"}
    assert g["access"] == {"password_set": False} and g["company"]["color"] == "#3d5a4a"
    assert g["retention"] == {"abgesagt_months": 6, "fertig_months": 12} and g["backup"]["keep"] == 14
    assert g["backup"]["target"].endswith("bfl-backups") and g["trusted_proxy"] is False
    projects.update_settings(lambda st: st["access"].update(password="pbkdf2_sha256$200000$aa$bb"))
    projects.update_settings(lambda st: st["team"].append({"id": "anna", "name": "Anna", "color": "#e879f9", "pin": "1234"}))
    g = client.get("/api/settings").json()
    assert g["access"] == {"password_set": True} and "pbkdf2" not in json.dumps(g) and "pin" not in json.dumps(g)
    assert "pin" not in json.loads((tmp / "projects/settings.json").read_text())["team"][0]
    r = client.put("/api/settings", json={"company": {"name": "Schreinerei Muster", "color": "#2f6b4f", "evil": 1},
                                          "public_base_url": "https://werkstatt.example.de/", "trusted_proxy": True,
                                          "retention": {"fertig_months": 24}, "backup": {"target": "/mnt/usb", "keep": 7},
                                          "team": [], "access": {"password": None}})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["company"]["name"] == "Schreinerei Muster" and "evil" not in g["company"] and g["trusted_proxy"] is True
    assert g["public_base_url"] == "https://werkstatt.example.de" and g["retention"]["fertig_months"] == 24
    assert g["backup"] == {"target": "/mnt/usb", "keep": 7} and len(g["team"]) == 1 and g["access"]["password_set"]
    for bad in ({"company": {"color": "red"}}, {"company": {"logo": "../x.png"}}, {"public_base_url": "http://x"},
                {"backup": {"target": "relativ"}}, {"backup": {"keep": 0}}):
        assert client.put("/api/settings", json=bad).status_code == 400
    assert projects.company_line({"street": "Hauptstr. 1", "zip": "91522", "city": "Ansbach", "phone": "0981 1"}) == \
        "Hauptstr. 1 · 91522 Ansbach · 0981 1"
    assert projects.company_line({"contact": "Eigene Zeile", "street": "x"}) == "Eigene Zeile"


def test_flux_data_honoured(tmp_path):
    code = "import projects; print(projects.STORE); print(projects.OUT)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "FLUX_DATA": str(tmp_path)},
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == [str(tmp_path / "projects"), str(tmp_path / "outputs")]


def test_export_dir_keeps_three(store):
    client, tmp = store
    dirs = []
    for _ in range(5):
        dirs.append(projects.export_dir("book", "abcdef123456"))
        assert dirs[-1].is_dir() and not any(dirs[-1].iterdir())  # a fresh, empty folder that survives its own pruning
    left = sorted(p.name for p in (tmp / "outputs/exports/book-abcdef123456").iterdir())
    assert left == sorted(d.name for d in dirs[-3:])


def test_write_json_atomic_unique(tmp_path):
    p = tmp_path / "x.json"
    ts = [threading.Thread(target=projects.write_json, args=(p, {"i": i, "pad": "x" * 10000})) for i in range(30)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert json.loads(p.read_text())["pad"] and list(tmp_path.iterdir()) == [p]


# ---------------- mutate, revisions ----------------

def record_like(doc, image="29991231-000000-1.png", vid="newvar01"):  # what workshop.record does
    if not any(v["image"] == image for v in doc["variants"]):
        doc["variants"].append({"id": vid, "image": image, "kind": "room", "judge": {"status": "pending"}, "job": "a1b2c3d4e5"})


def test_mutate_diff_based(store):
    client, tmp = store
    d = new(client)
    pid = d["id"]
    same = projects.mutate("workshop", pid, lambda doc: doc.update(title=doc["title"]), who="anna")
    assert same["rev"] == 1 and projects.load("workshop", pid)["rev"] == 1  # nothing changed → no save
    r = projects.mutate("workshop", pid, record_like, who="anna")
    assert r["rev"] == 2 and r["frev"] == {"variants.newvar01": [2, "anna", r["frev"]["variants.newvar01"][2], "auto"]}
    assert projects.mutate("workshop", pid, record_like, who="anna")["rev"] == 2  # re-record: no bump
    r = projects.mutate("workshop", pid, lambda doc: projects.log(doc, "x", "y", "anna"), who="anna")
    assert r["rev"] == 3 and set(r["frev"]) == {"variants.newvar01"}  # activity bumps rev, never frev
    r = projects.mutate("workshop", pid, lambda doc: doc["variants"][0].update(judge={"status": "ok"}), who="system")
    assert set(r["frev"]) == {"variants.newvar01", "variants.newvar01.judge"} and r["updated_by"] == "system"


def test_changed_paths():
    a = projects.normalize_workshop({"version": 3, "id": "abcdef123456", "pieces": [{"id": "p1aaaa", "type": "tisch"}],
                                     "variants": [{"id": "v1aaaa", "image": "1.png"}]})
    b = copy.deepcopy(a)
    b["pieces"][0].update(w=200, label="Anders")
    b["pieces"].append({"id": "p2bbbb"})
    b["variants"][0]["fav"] = True
    b["variants"][0]["locked"] = True
    b["contact"]["city"] = "Ansbach"
    b["judge"]["enabled"] = False
    b.update(geometry={"x": 1}, rev=9, updated="x", thumb="y", activity=[1], status="kunde")
    assert projects.changed_paths(a, b) == ["contact.city", "geometry", "judge.enabled", "pieces.p1aaaa.w", "pieces.p2bbbb",
                                            "status", "variants.v1aaaa.fav"]
    c = copy.deepcopy(a)
    c["trash"] = c.pop("variants")
    c["variants"] = []
    assert projects.changed_paths(a, c) == ["trash", "variants.v1aaaa"]


def test_changed_since(store):
    client, tmp = store
    d = new(client)
    p = d["pieces"][0]["id"]
    patch(client, d["id"], 1, s(f"pieces.{p}.w", 240), who="ben")
    patch(client, d["id"], 2, s("internal_notes", "x"), who="anna")
    doc = projects.load("workshop", d["id"])
    got = projects.changed_since(doc, 1, ("pieces", "room", "count"))
    assert [(c["path"], c["by"], c["by_name"]) for c in got] == [(f"pieces.{p}.w", "ben", "ben")]
    assert projects.changed_since(doc, 2, ("pieces",)) == [] and projects.changed_since(doc, 0, "internal_notes")


def test_frev_pruned():
    doc = projects.normalize_workshop({"version": 3, "id": "abcdef123456", "frev": {f"g{i}": [i + 1, "x", "t"] for i in range(450)}})
    assert len(doc["frev"]) == 400 and doc["frev_floor"] == 50 and min(e[0] for e in doc["frev"].values()) == 51


def test_mutate_concurrent(store):
    client, tmp = store
    pid = new(client)["id"]

    def bump(doc):
        doc["counter"] = doc.get("counter", 0) + 1

    threads = [threading.Thread(target=lambda: [projects.mutate("workshop", pid, bump) for _ in range(5)]) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    d = projects.load("workshop", pid)
    assert d["counter"] == 50 and d["rev"] == 51


def test_load_returns_copies_and_sees_external_writes(store):
    client, tmp = store
    d = new(client, title="A")
    a = projects.load("workshop", d["id"])
    a["title"] = "changed in memory"
    assert projects.load("workshop", d["id"])["title"] == "A" and projects.title(d["id"]) == "A"
    raw = json.loads((tmp / "projects/workshop" / f"{d['id']}.json").read_text())
    put_doc(tmp, {**raw, "title": "Extern geändert"})
    assert projects.load("workshop", d["id"])["title"] == "Extern geändert"


# ---------------- PATCH ops ----------------

def test_two_clients_merge(store):
    client, tmp = store
    d = new(client)
    pid, p = d["id"], d["pieces"][0]["id"]
    r = patch(client, pid, 1, s("title", "Küche Müller"), s("customer", "Müller"), s(f"pieces.{p}.w", 240), who="anna")
    assert r.status_code == 200 and r.json()["rev"] == 2
    r = patch(client, pid, 1, s("setting", "heller Altbau"), who="ben")  # stale base, other field
    assert r.status_code == 200
    doc = r.json()["doc"]
    assert (doc["title"], doc["customer"], doc["pieces"][0]["w"], doc["setting"]) == ("Küche Müller", "Müller", 240, "heller Altbau")
    r = patch(client, pid, 1, s(f"pieces.{p}.w", 220), who="ben")  # same field → conflict, nothing applied
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "conflict" and det["rev"] == 3 and det["doc"]["pieces"][0]["w"] == 240
    assert det["conflicts"] == [{"path": f"pieces.{p}.w", "mine": 220, "theirs": 240, "by": "anna", "by_name": "anna",
                                 "at": det["conflicts"][0]["at"]}]
    r = patch(client, pid, 1, s(f"pieces.{p}.w", 240), s("ext_ref", "A-1"), who="ben")  # same value twice → no conflict
    assert r.status_code == 200 and r.json()["doc"]["ext_ref"] == "A-1"
    r = patch(client, pid, 1, s("ref", "2026-077"), s(f"pieces.{p}.w", 200), who="ben")  # all-or-nothing
    assert r.status_code == 409 and projects.load("workshop", pid)["ref"] != "2026-077"


def test_stale_fav_note_and_pieces_kept(store):
    client, tmp = store
    d = new(client)
    pid, p = d["id"], d["pieces"][0]["id"]
    projects.mutate("workshop", pid, lambda doc: record_like(doc), who="anna")
    assert patch(client, pid, 2, s("variants.newvar01.fav", True), who="anna").status_code == 200
    r = patch(client, pid, 2, s("title", "Neu"), who="ben")
    assert r.status_code == 200 and r.json()["doc"]["variants"][0]["fav"] is True
    r = patch(client, pid, 4, {"op": "add_piece", "value": {"id": "k3m9qa", "type": "bett"}}, who="anna")
    assert r.status_code == 200 and len(r.json()["doc"]["pieces"]) == 2
    r = patch(client, pid, 4, s(f"pieces.{p}.text", "3 Türen"), who="ben")  # stale: does not revert the added piece
    assert r.status_code == 200 and [x["id"] for x in r.json()["doc"]["pieces"]] == [p, "k3m9qa"]
    assert patch(client, pid, 6, {"op": "remove_piece", "id": "k3m9qa"}, who="anna").status_code == 200
    r = patch(client, pid, 6, s("pieces.k3m9qa.w", 100), who="ben")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "gone"
    assert r.json()["detail"]["conflicts"][0]["path"] == "pieces.k3m9qa.w" and r.json()["detail"]["conflicts"][0]["by"] == "anna"


def test_no_false_conflicts_from_server_writes(store):
    client, tmp = store
    d = new(client)
    pid = d["id"]
    projects.mutate("workshop", pid, lambda doc: record_like(doc, "29991231-000000-0.png", "oldvar01"), who="anna")
    projects.mutate("workshop", pid, lambda doc: record_like(doc, "29991231-000000-9.png", "othvar01"), who="anna")
    base = projects.load("workshop", pid)["rev"]
    projects.mutate("workshop", pid, record_like, who="anna")                                    # new variant
    projects.mutate("workshop", pid, lambda doc: next(v for v in doc["variants"] if v["id"] == "oldvar01")
                    .update(judge={"status": "ok", "overall": 8}), who="anna")                   # verdict on the old one
    rev = projects.load("workshop", pid)["rev"]
    projects.mutate("workshop", pid, record_like, who="anna")                                    # re-record
    assert projects.load("workshop", pid)["rev"] == rev  # bumps nothing
    r = patch(client, pid, base, s("variants.oldvar01.fav", True), who="ben")
    assert r.status_code == 200, r.text
    r = patch(client, pid, base, {"op": "delete_variant", "id": "othvar01"}, who="ben")
    assert r.status_code == 200
    r = patch(client, pid, base, s("status", "kunde"), who="ben")  # records did not move the status (already entwurf)
    assert r.status_code == 200 and r.json()["doc"]["status"] == "kunde"


def test_placement_last_writer_wins(store):
    client, tmp = store
    d = new(client)
    pid, p = d["id"], d["pieces"][0]["id"]
    pa = {"mode": "3d", "pose": {"x": -20, "z": 370, "yaw": 0, "wall": "w0"}, "box": [106, 246, 744, 662]}
    pb = {"mode": "2d", "pose": None, "box": [10, 10, 500, 500]}
    assert patch(client, pid, 1, s(f"pieces.{p}.placement", pa), who="anna").status_code == 200
    r = patch(client, pid, 1, s(f"pieces.{p}.placement", pb), who="ben")
    assert r.status_code == 200 and r.json()["doc"]["pieces"][0]["placement"] == pb
    r = patch(client, pid, 1, s(f"pieces.{p}.w", 90), who="ben")  # placement entries never block other piece fields
    assert r.status_code == 200


def test_remove_piece_replacement(store):
    client, tmp = store
    d = new(client)
    pid, p = d["id"], d["pieces"][0]["id"]
    r = patch(client, pid, 1, {"op": "remove_piece", "id": p})
    assert r.status_code == 400
    r = patch(client, pid, 1, {"op": "remove_piece", "id": p, "replacement": {"id": p}})
    assert r.status_code == 400
    r = patch(client, pid, 1, {"op": "remove_piece", "id": p, "replacement": {"id": "q7c2xw", "type": None, "material": "eiche",
                                                                              "handles": "push", "label": "Fake"}})
    assert r.status_code == 200
    doc = r.json()["doc"]
    assert [x["id"] for x in doc["pieces"]] == ["q7c2xw"] and doc["pieces"][0]["label"] == "" and doc["active_piece"] == "q7c2xw"
    assert doc["activity"][-1]["what"] == "piece_remove"
    r = patch(client, pid, 2, {"op": "remove_piece", "id": p, "replacement": {"id": "zzzzzz"}})  # re-sent: no-op
    assert r.status_code == 200 and r.json()["rev"] == 2


def test_add_piece_limits(store):
    client, tmp = store
    d = new(client)
    pid = d["id"]
    ops = [{"op": "add_piece", "value": {"id": f"pc{i}aaa", "type": "regal"}} for i in range(3)]
    r = patch(client, pid, 1, *ops)
    assert r.status_code == 200 and len(r.json()["doc"]["pieces"]) == 4
    assert r.json()["doc"]["pieces"][1]["label"] == "Regalwand"
    r = patch(client, pid, 2, {"op": "add_piece", "value": {"id": "toomany1"}})
    assert r.status_code == 400 and r.json()["detail"] == "Höchstens 4 Möbel pro Auftrag."
    assert patch(client, pid, 2, ops[0]).json()["rev"] == 2  # re-sent add: idempotent
    assert patch(client, pid, 2, {"op": "add_piece", "value": {"id": "BAD!"}}).status_code == 400


def test_ops_validation(store):
    client, tmp = store
    d = new(client)
    pid, p = d["id"], d["pieces"][0]["id"]
    projects.mutate("workshop", pid, record_like)
    for op in (s("nope", 1), s("variants.newvar01.judge", {"status": "ok"}), s("rev", 99), s("__proto__", {}),
               s("pieces.__proto__.w", 1), s("contact.evil", "x"), s("title", 5), s("count", "3"), s("quality", "ultra"),
               s("status", "weg"), s("tidy", "ja"), s("room", "../x.png"), s("geometry", {"x": "y" * 70000}),
               s(f"pieces.{p}.type", "rakete"), s(f"pieces.{p}.w", "abc"), s(f"pieces.{p}.label", "x"),
               s("appointment", {"at": "morgen"}), s("removes", [[0, 0, 1, 1]] * 21), s("frev", {}),
               {"op": "explode"}, {"op": "set"}, {"op": "delete_variant", "id": "../x"}, "junk"):
        r = patch(client, pid, 2, op)
        assert r.status_code == 400 and r.json()["detail"].startswith("Ungültige Änderung"), (op, r.text)
    assert client.patch(f"/api/projects/workshop/{pid}", json={"base_rev": "x", "ops": [s("title", "a")]}).status_code == 400
    assert client.patch(f"/api/projects/workshop/{pid}", json={"base_rev": 2, "ops": [s("title", "a")] * 201}).status_code == 400
    r = patch(client, pid, 2, s("title", "T" * 500), s("count", 9), s(f"pieces.{p}.w", "240,4"), s(f"pieces.{p}.type", "tisch"))
    doc = r.json()["doc"]
    assert len(doc["title"]) == 200 and doc["count"] == 4 and doc["pieces"][0]["w"] == 240 and doc["pieces"][0]["label"] == "Esstisch"
    assert patch(client, "abcdef123456", 0, s("title", "x")).status_code == 404


def test_delete_restore_locked_variant(store):
    client, tmp = store
    d = new(client)
    pid = d["id"]
    projects.mutate("workshop", pid, lambda doc: (record_like(doc), record_like(doc, "29991231-000000-2.png", "secvar01")))
    projects.mutate("workshop", pid, lambda doc: doc["versions"].append({"n": 1, "status": "offen", "variant_ids": ["secvar01"]}))
    r = patch(client, pid, 3, {"op": "delete_variant", "id": "secvar01"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "locked"
    assert "Version zurück" in r.json()["detail"]["message"] and r.json()["detail"]["rev"] == 3
    assert r.json()["detail"]["doc"]["id"] == pid and r.json()["detail"]["conflicts"] == [{"path": "variants.secvar01"}]
    r = patch(client, pid, 3, {"op": "delete_variant", "id": "newvar01"})
    doc = r.json()["doc"]
    assert [v["id"] for v in doc["variants"]] == ["secvar01"] and doc["trash"][0]["id"] == "newvar01"
    assert "newvar01" in doc["deleted_variants"] and doc["activity"][-1]["text"] == "Vorschlag gelöscht"
    assert patch(client, pid, 4, {"op": "delete_variant", "id": "newvar01"}).json()["rev"] == 4  # already deleted
    doc = patch(client, pid, 4, {"op": "restore_variant", "id": "newvar01"}).json()["doc"]
    assert {v["id"] for v in doc["variants"]} == {"secvar01", "newvar01"} and doc["trash"] == []
    assert "newvar01" not in doc["deleted_variants"]
    r = patch(client, pid, 5, {"op": "restore_variant", "id": "unknown1"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "gone"
    projects.mutate("workshop", pid, record_like)  # a late record of the restored image does not duplicate it
    assert len(projects.load("workshop", pid)["variants"]) == 2


def test_side_effects(store):
    client, tmp = store
    d = new(client)
    pid = d["id"]
    doc = patch(client, pid, 1, s("room", "uploads/20261016-5d0db410c1.png")).json()["doc"]
    assert doc["status"] == "termin" and [a["what"] for a in doc["activity"]] == ["created", "photo", "status"]
    assert doc["activity"][1]["text"] == "Raumfoto hinzugefügt" and doc["activity"][2]["text"] == "Status: Anfrage → Vor-Ort-Termin"
    doc = patch(client, pid, 2, s("room", "uploads/20261016-aaaaaaaaaa.png")).json()["doc"]
    assert doc["activity"][-1]["text"] == "Raumfoto ersetzt" and doc["status"] == "termin"
    doc = patch(client, pid, 3, s("status", "anfrage")).json()["doc"]  # manual backward move is allowed and logged
    assert doc["status"] == "anfrage" and doc["activity"][-1]["text"] == "Status: Vor-Ort-Termin → Anfrage"
    doc = patch(client, pid, 4, s("appointment", {"at": "2030-10-20T09:00", "what": "Aufmaß"})).json()["doc"]
    assert doc["status"] == "termin" and doc["activity"][-2]["text"] == "Termin: Aufmaß am 20.10.2030, 09:00 Uhr"
    doc = patch(client, pid, 5, s("appointment", None), s("customer", "Meier"), s("contact.phone", "1"), s("ext_ref", "A-9")).json()["doc"]
    whats = [a["what"] for a in doc["activity"]]
    assert whats[-3:] == ["meta", "ext_ref", "appointment"] and doc["activity"][-1]["text"] == "Termin entfernt"
    assert doc["status"] == "termin"
    doc = patch(client, pid, 6, s("customer", "Meier-Schulz")).json()["doc"]
    assert [a["what"] for a in doc["activity"]].count("meta") == 1  # coalesced within 5 minutes
    doc = patch(client, pid, 7, {"op": "archive"}).json()["doc"]
    assert doc["archived_at"] and doc["activity"][-1]["text"] == "Archiviert"
    doc = patch(client, pid, 8, {"op": "unarchive"}).json()["doc"]
    assert doc["archived_at"] is None and doc["activity"][-1]["text"] == "Aus dem Archiv geholt"
    before = projects.load("workshop", pid)
    doc = patch(client, pid, 9, s("setting", "Altbau"), s("count", 3)).json()["doc"]
    assert doc["activity"] == before["activity"]  # design edits are not logged
    rev = doc["rev"]
    assert patch(client, pid, rev, s("count", 3)).json()["rev"] == rev  # unchanged → no save


def test_log_coalescing_and_cap():
    doc = {}
    projects.log(doc, "meta", "a", "anna")
    projects.log(doc, "meta", "b", "anna")
    projects.log(doc, "meta", "c", "ben")
    projects.log(doc, "status", "s1", "ben")
    projects.log(doc, "status", "s2", "ben")
    assert [(a["what"], a["text"]) for a in doc["activity"]] == [("meta", "b"), ("meta", "c"), ("status", "s1"), ("status", "s2")]
    doc["activity"][0]["at"] = (datetime.now() - timedelta(minutes=6)).isoformat(timespec="seconds")
    doc["activity"] = doc["activity"][:1]
    projects.log(doc, "meta", "d", "anna")
    assert len(doc["activity"]) == 2
    for i in range(400):
        projects.log(doc, "viewed", str(i), "kunde")
    assert len(doc["activity"]) == 300 and doc["activity"][-1]["text"] == "399"


def test_advance():
    doc = {"status": "anfrage", "activity": []}
    assert projects.advance(doc, "entwurf", "anna") and doc["status"] == "entwurf"
    assert (doc["activity"][-1]["what"], doc["activity"][-1]["text"], doc["activity"][-1]["by"]) == \
        ("status", "Status: Anfrage → Entwurf", "anna")
    assert not projects.advance(doc, "termin", "anna") and doc["status"] == "entwurf"
    assert not projects.advance(doc, "abgesagt", "anna")
    doc["status"] = "abgesagt"
    assert not projects.advance(doc, "kunde", "anna") and doc["status"] == "abgesagt"


def test_patch_concurrent_different_paths(store):
    client, tmp = store
    d = new(client)
    pid, p = d["id"], d["pieces"][0]["id"]
    ops = [s("title", "a"), s("customer", "b"), s("setting", "c"), s("room_changes", "d"), s("ext_ref", "e"),
           s("internal_notes", "f"), s("contact.phone", "1"), s("contact.email", "x@y"), s("contact.street", "s"),
           s("contact.zip", "9"), s("contact.city", "c"), s(f"pieces.{p}.text", "t"), s(f"pieces.{p}.where", "w"),
           s(f"pieces.{p}.material_custom", "m"), s(f"pieces.{p}.w", 10), s(f"pieces.{p}.h", 20), s(f"pieces.{p}.d", 30),
           s("tidy", False), s("guide", True), s("count", 3)]
    codes = []
    ts = [threading.Thread(target=lambda op=op: codes.append(patch(client, pid, 1, op).status_code)) for op in ops]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes == [200] * 20
    doc = projects.load("workshop", pid)
    assert doc["rev"] == 21 and doc["title"] == "a" and doc["contact"]["city"] == "c" and doc["pieces"][0]["d"] == 30
    assert doc["count"] == 3 and doc["tidy"] is False


# ---------------- uploads (unchanged) ----------------

def _upload(client, img, fmt, name, **params):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return client.post("/api/upload", params=params, files={"file": (name, buf.getvalue(), "application/octet-stream")})


def test_upload_max_side(store):
    client, tmp = store
    r = _upload(client, Image.new("RGB", (2000, 1000), "red"), "PNG", "a.png", max_side=512).json()
    assert (r["width"], r["height"]) == (512, 256)
    assert Image.open(tmp / "outputs" / r["path"]).size == (512, 256)
    r = _upload(client, Image.new("RGB", (300, 200), "red"), "PNG", "b.png").json()
    assert (r["width"], r["height"]) == (300, 200)


def test_upload_too_big(store, monkeypatch):
    client, tmp = store
    monkeypatch.setattr(projects.imaging, "MAX_UPLOAD", 1000)
    r = _upload(client, Image.effect_noise((200, 200), 64).convert("RGB"), "PNG", "big.png")
    assert r.status_code == 413 and "zu groß" in r.json()["detail"]


def test_absolute_image_paths_rejected():
    assert projects._img("uploads/a.png") == "uploads/a.png"
    assert projects._img("/home/x/secret.png") is None and projects._img("uploads/../a.png") is None


def test_replaced_room_goes_to_old_files():
    before = {"room": "uploads/a.png", "pieces": [], "variants": [{"image": "v1.png", "room": "uploads/a.png"}],
              "trash": [{"image": "t1.png"}]}
    work = {"room": "uploads/b.png", "pieces": [], "variants": [{"image": "v1.png", "room": "uploads/a.png"}], "trash": []}
    assert projects.doc_images(before) - projects.doc_images(work) == {"t1.png"}  # a.png is still a variant's room


def test_upload_heic(store):
    pillow_heif = pytest.importorskip("pillow_heif")
    client, tmp = store
    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (640, 480), (20, 120, 40))).save(buf, format="HEIF")
    r = client.post("/api/upload", files={"file": ("x.heic", buf.getvalue(), "image/heic")})
    assert r.status_code == 200, r.text
    assert (r.json()["width"], r.json()["height"]) == (640, 480)


def test_approval_outdated_when_pieces_change(store):
    doc = projects.new_workshop({"title": "Küche", "customer": "Huber"}, "anna")
    doc["pieces"][0].update(type="schrank", w=180, h=240, d=60)
    projects.normalize_workshop(doc)
    doc["approved"] = {"version": 1, "variant_id": "x", "via": "link", "spec": projects.design_spec(doc)}
    assert projects.normalize_workshop(doc)["approved"]["outdated"] is False
    doc["pieces"][0]["w"] = 136
    assert projects.normalize_workshop(doc)["approved"]["outdated"] is True
    doc["approved"].pop("spec")  # approvals from before this check are never flagged
    assert projects.normalize_workshop(doc)["approved"]["outdated"] is False
