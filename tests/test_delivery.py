"""Versions, approvals, draft sheets, DSGVO export and purge (delivery.py) on a private tmp data root.

Runs against the real v3 projects.py when it provides the spec §4.1 contract (its paths are redirected into tmp),
else against FakeProjects below — the same contract in ~60 lines (set FAKE_PROJECTS=1 to force it)."""
import copy
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import delivery
import printing
import public

PID, OTHER = "aaaa1111bbbb", "cccc2222dddd"
STATUS = ["anfrage", "termin", "entwurf", "kunde", "freigegeben", "auftrag", "fertig", "abgesagt"]
LABELS = dict(zip(STATUS, ["Anfrage", "Vor-Ort-Termin", "Entwurf", "Beim Kunden", "Freigegeben", "Auftrag",
                           "Abgeschlossen", "Abgesagt"]))


class FakeProjects:
    """projects.py's contract for delivery/public: load, mutate (no save when unchanged, rev+1), locked, log,
    advance, settings, company_line, Gone, STATUS_LABELS."""
    STATUS, STATUS_LABELS = STATUS, LABELS

    class Gone(HTTPException):
        def __init__(self):
            super().__init__(404, "Auftrag nicht gefunden.")

    def __init__(self, store: Path):
        self.store, self.locks, self.meta = store, {}, threading.Lock()

    def _lock(self, pid):
        with self.meta:
            return self.locks.setdefault(pid, threading.RLock())

    @contextmanager
    def locked(self, kind, pid):
        with self._lock(pid):
            yield

    def load(self, kind, pid):
        p = self.store / kind / f"{pid}.json"
        if not p.exists():
            raise self.Gone()
        return json.loads(p.read_text())

    def mutate(self, kind, pid, fn, who=None, auto=True):
        with self._lock(pid):
            doc = self.load(kind, pid)
            before = copy.deepcopy(doc)
            fn(doc)
            if doc == before:
                return doc
            doc["rev"] = int(doc.get("rev") or 0) + 1
            doc["updated_by"] = who
            delivery.write_json(self.store / kind / f"{pid}.json", doc)
            return doc

    def log(self, doc, what, text, who):
        doc.setdefault("activity", []).append({"at": datetime.now().isoformat(timespec="seconds"), "by": who, "what": what, "text": text})

    def advance(self, doc, target, who, text=None):
        order = STATUS[:7]
        cur = doc.get("status") or "anfrage"
        if cur in order and target in order and order.index(target) > order.index(cur):
            doc["status"] = target
            self.log(doc, "status", text or f"Status: {LABELS[cur]} → {LABELS[target]}", who)
            return True
        return False

    def settings(self):
        try:
            return json.loads((self.store / "settings.json").read_text())
        except FileNotFoundError:
            return {"company": {}, "team": [], "public_base_url": "", "trusted_proxy": False}

    def company_line(self, company):
        return printing.company_line(company)


def make_projects(data: Path, monkeypatch):
    import projects as real
    if os.environ.get("FAKE_PROJECTS") != "1" and all(hasattr(real, n) for n in ("locked", "Gone", "settings", "company_line", "advance")):
        here = Path(real.__file__).resolve().parent
        for k, v in list(vars(real).items()):
            if isinstance(v, Path):
                try:
                    monkeypatch.setattr(real, k, data / v.resolve().relative_to(here))
                except ValueError:
                    pass
        return real
    return FakeProjects(data / "projects")


def img(path: Path, size=(1600, 1200), color=(180, 170, 160), label=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    im.paste((90, 60, 40), (size[0] // 4, size[1] // 3, size[0] // 2, size[1] * 3 // 4))
    im.save(path)


class Env:
    def __init__(self, data: Path, projects):
        self.data, self.out, self.store, self.jobs, self.projects = data, data / "outputs", data / "projects", data / "jobs", projects
        self.app = FastAPI()
        self.app.include_router(delivery.router)
        self.app.include_router(public.router)
        self.client = TestClient(self.app)

    def write(self, doc, kind="workshop"):
        delivery.write_json(self.store / kind / f"{doc['id']}.json", doc)

    def doc(self, pid=PID):
        return self.projects.load("workshop", pid)

    def settings(self, **kw):
        s = {"version": 1, "company": {"name": "Schreinerei Muster", "street": "Werkstr. 3", "zip": "91522", "city": "Ansbach",
                                       "phone": "0981 123456", "color": "#2f6b4f", "logo": "uploads/logo.png"},
             "team": [{"id": "anna", "name": "Anna", "color": "#e879f9"}], "public_base_url": "", "trusted_proxy": False}
        s.update(kw)
        delivery.write_json(self.store / "settings.json", s)


def make_doc(pid=PID, room="uploads/room.png", **kw):
    piece = {"id": "p1aaaa", "type": "sideboard", "label": "Sideboard", "material": "eiche", "material_label": "Eiche natur geölt",
             "handles": "push", "handles_label": "grifflos (Push)", "w": 180, "h": 75, "d": 45, "sketch": "uploads/sketch.png",
             "placement": {"mode": "none", "pose": None, "box": None}}
    snap = {k: piece[k] for k in ("id", "label", "material_label", "handles_label", "w", "h", "d")}
    v = lambda vid, image, kind, **x: {"id": vid, "image": image, "kind": kind, "fav": False, "note": f"Interne Notiz {vid}",
                                       "piece_ids": ["p1aaaa"], "pieces": [dict(snap, **x.pop("snap", {}))], "seed": 4242,
                                       "judge": {"status": "ok", "comment": "Prüftext"}, **x}
    doc = {"id": pid, "version": 3, "rev": 1, "frev": {}, "frev_floor": 0, "created": "2026-10-18T09:00:00",
           "title": "TEST Küche", "customer": "Familie Beispiel", "status": "entwurf", "ref": "2026-007", "ext_ref": "A-99",
           "contact": {"phone": "0981 777", "email": "kunde@example.org", "street": "Hauptstr. 1", "zip": "91522", "city": "Ansbach"},
           "appointment": None, "internal_notes": "Rabatt intern", "archived_at": None, "room": room, "sample": None,
           "pieces": [piece], "quality": "fein", "count": 2, "judge": {"enabled": True, "auto_retry": False},
           "variants": [v("v1room", "20261018-100000-1.png", "room", room=room, boxes={"p1aaaa": [200, 300, 800, 700]}),
                        v("v2nuss", "20261018-100100-2.png", "material", snap={"material_label": "Nussbaum"}, **{"from": "v1room"}),
                        v("v3rend", "20261018-100200-3.png", "render", sketch="uploads/sketch.png")],
           "trash": [v("v9trsh", "20261018-090000-9.png", "room", room=room)], "deleted_variants": ["v9trsh"],
           "versions": [], "approved": None, "activity": [], "pending_jobs": []}
    doc.update(kw)
    return doc


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for d in ("outputs/uploads", "outputs/refs", "outputs/geometry", "outputs/guides", "projects/workshop", "projects/book", "jobs"):
        (data / d).mkdir(parents=True)
    projects = make_projects(data, monkeypatch)
    for mod in (delivery, public):
        monkeypatch.setattr(mod, "projects", projects)
    monkeypatch.setattr(delivery, "DATA", data)
    monkeypatch.setattr(delivery, "OUT", data / "outputs")
    monkeypatch.setattr(delivery, "STORE", data / "projects")
    monkeypatch.setattr(delivery, "JOBS", data / "jobs")
    monkeypatch.setattr(delivery, "jobs_hook", None)
    monkeypatch.setattr(delivery, "RESERVED", {})
    public._HITS.clear()
    out = data / "outputs"
    img(out / "uploads/room.png")
    img(out / "uploads/sketch.png", (800, 600), (250, 250, 250))
    img(out / "uploads/logo.png", (300, 100), (47, 107, 79))
    for i, name in enumerate(["20261018-100000-1", "20261018-100100-2", "20261018-100200-3", "20261018-090000-9"]):
        img(out / f"{name}.png", (2400, 1800) if i == 0 else (1328, 1776), (150 + 20 * i, 140, 120))
        (out / f"{name}.json").write_text(json.dumps({"id": name, "refs": [f"refs/ref{i}.png"], "prompt": "x", "caption": "y"}))
        img(out / f"refs/ref{i}.png", (64, 64))
    for s in (".json", "-depth.npy", "-depth.jpg"):
        (out / f"geometry/room{s}").write_bytes(b"x")
    (out / f"guides/{PID}-0123456789.png").write_bytes(b"x")
    e = Env(data, projects)
    e.settings()
    e.write(make_doc())
    return e


def text_of_pdf(path: Path) -> str:
    return subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout


def create(env, ids=("v1room", "v2nuss"), note="Guten Tag Familie Beispiel,\nanbei unsere Entwürfe.", **kw):
    r = env.client.post(f"/api/workshop/{PID}/versions", json={"variant_ids": list(ids), "customer_note": note, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- versions ----------------

def test_create_version_freezes_files(env):
    res = create(env)
    ver = res["version"]
    vdir = env.out / f"exports/workshop-{PID}/v1"
    assert ver["n"] == 1 and ver["dir"] == f"exports/workshop-{PID}/v1" and ver["status"] == "offen"
    assert ver["files"]["images"] == ["01_Vorher.jpg", "02_Entwurf-1_Eiche-natur-geoelt_KI-Visualisierung.jpg",
                                      "03_Entwurf-2_Nussbaum_KI-Visualisierung.jpg"]  # identical befores once
    assert ver["files"]["pdf"] == "Visualisierung_2026-007_Familie-Beispiel_V1.pdf" and ver["files"]["logo"] == "logo.png"
    assert sorted(p.name for p in vdir.iterdir()) == sorted(ver["files"]["images"] + [ver["files"]["pdf"], ver["files"]["zip"], "logo.png", "LIESMICH.txt"])
    for name in ver["files"]["images"]:
        with Image.open(vdir / name) as im:
            assert im.format == "JPEG" and max(im.size) <= 2048
            desc = im.getexif().get(0x010E)
            assert desc == ("KI-Visualisierung - unverbindlich" if "KI-Visualisierung" in name else None)
    snap = ver["snapshot"]
    assert snap["title"] == "TEST Küche" and snap["ref"] == "2026-007" and snap["company"]["name"] == "Schreinerei Muster"
    assert snap["company"]["contact"] == "Werkstr. 3 · 91522 Ansbach · 0981 123456"
    assert snap["variants"][1] == {"n": 2, "id": "v2nuss", "before": "01_Vorher.jpg", "after": ver["files"]["images"][2],
                                   "before_label": "Vorher", "after_label": "Nachher",
                                   "rows": [{"piece": "Sideboard", "material": "Nussbaum", "handles": "grifflos (Push)", "dims": "180 × 75 × 45 cm"}],
                                   "focus": [0.3, 0.7]}  # the room ancestor's boxes
    lan = f"http://testserver/k/{ver['token']}"
    assert res["link"] == lan and res["share_link"] == lan and ver["link"] == lan and res["lan_only"] and res["public_link"] is None
    assert res["qr_svg"].startswith("<svg")
    shares = json.loads((env.store / "shares.json").read_text())
    assert shares[ver["token"]] == {"pid": PID, "n": 1, "expires": ver["expires"]}
    doc = env.doc()
    # only prepared: the status moves to „Beim Kunden“ once a send button is used or the customer opens the link
    assert doc["status"] != "kunde" and doc["versions"][0]["token"] == ver["token"] and res["doc_rev"] == doc["rev"]
    assert doc["versions"][0]["sent_at"] is None
    assert any(a["what"] == "version_ready" and a["text"] == "Version 1 für den Kunden bereitgestellt (2 Entwürfe)" for a in doc["activity"])
    assert not any(a["what"] == "version_sent" for a in doc["activity"])
    if "locked" in doc["variants"][0]:  # derived by the real v3 normalize
        assert {v["id"]: v["locked"] for v in doc["variants"]} == {"v1room": True, "v2nuss": True, "v3rend": False}
    r = env.client.get(res["pdf"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and "attachment" in r.headers["content-disposition"]
    assert ver["files"]["pdf"] in r.headers["content-disposition"]
    assert env.client.get(res["zip"]).headers["content-type"] == "application/zip"
    assert env.client.get(f"/api/workshop/{PID}/versions/1/qr.svg").status_code == 200  # the LAN link without a public address
    assert env.client.get(f"/api/workshop/{PID}/versions/9/pdf").status_code == 404


def test_sent_only_when_really_sent(env):
    create(env)
    assert env.client.post(f"/api/workshop/{PID}/versions/1/sent", json={"via": "fax"}).status_code == 400
    r = env.client.post(f"/api/workshop/{PID}/versions/1/sent", json={"via": "whatsapp"})
    assert r.status_code == 200 and r.json()["status"] == "kunde" and r.json()["version"]["sent_via"] == "whatsapp"
    first = r.json()["version"]["sent_at"]
    r = env.client.post(f"/api/workshop/{PID}/versions/1/sent", json={"via": "email"})   # the first hand-over counts
    assert r.json()["version"]["sent_at"] == first and r.json()["version"]["sent_via"] == "whatsapp"
    doc = env.doc()
    assert [a["text"] for a in doc["activity"] if a["what"] == "version_sent"] == ["Version 1 an den Kunden gesendet (per WhatsApp)"]
    legacy = {"n": 7, "status": "offen"}                               # versions from before sent_at: already out
    assert delivery.is_sent(legacy) and not delivery.mark_sent({"status": "entwurf"}, legacy, "email", "anna")


def test_render_before_is_sketch_and_free_has_none(env):
    doc = make_doc()
    img(env.out / "20261018-100300-4.png", (1328, 1776), (90, 140, 120))
    doc["variants"].append({"id": "v4free", "image": "20261018-100300-4.png", "kind": "free", "piece_ids": ["p1aaaa"]})
    env.write(doc)
    ver = create(env, ids=("v3rend", "v4free"))["version"]
    assert ver["files"]["images"] == ["01_Skizze.jpg", "02_Entwurf-1_Eiche-natur-geoelt_KI-Visualisierung.jpg",
                                      "03_Entwurf-2_Eiche-natur-geoelt_KI-Visualisierung.jpg"]
    a, b = ver["snapshot"]["variants"]
    assert (a["before"], a["before_label"], a["after_label"]) == ("01_Skizze.jpg", "Skizze", "Entwurf")
    assert (b["before"], b["before_label"], b["after_label"]) == (None, None, "Entwurf")


def test_lan_link_in_files_without_public_address(env):
    """No public address: the customer still gets a working link in the shop Wi-Fi (PDF text + QR, LIESMICH)."""
    res = create(env)
    ver, token = res["version"], res["version"]["token"]
    link = f"http://testserver/k/{token}"
    vdir = env.out / ver["dir"]
    texts = [text_of_pdf(vdir / ver["files"]["pdf"])]
    with zipfile.ZipFile(vdir / ver["files"]["zip"]) as z:
        names = z.namelist()
        assert sorted(names) == sorted(ver["files"]["images"] + [ver["files"]["pdf"], "LIESMICH.txt"])
        assert not any(n.endswith((".html", ".htm")) for n in names)
        blobs = {n: z.read(n) for n in names}
    texts.append(blobs["LIESMICH.txt"].decode("utf-8"))
    for t in texts:
        assert link in t.replace("\n", "")
    readme = texts[1]
    assert "anbei unsere Entwürfe" in readme and "Schreinerei Muster" in readme and "KI-Visualisierung" in readme
    assert "Rabatt" not in readme + texts[0] and "Interne Notiz" not in readme + texts[0] and "0981 777" not in readme
    assert "Gewählter Entwurf (keine Auftragserteilung)" in texts[0] and "Online ansehen und freigeben" in texts[0]
    assert "Schreinerei Muster · Werkstr. 3 · 91522 Ansbach · 0981 123456" in texts[0]  # sender line in the footer


def test_lan_base_replaces_localhost(env, monkeypatch):
    monkeypatch.setattr(delivery, "lan_ip", lambda: "192.168.178.111")
    res = env.client.post(f"/api/workshop/{PID}/versions", json={"variant_ids": ["v1room"]}, headers={"host": "localhost:7860"})
    assert res.status_code == 200, res.text
    j = res.json()
    assert j["link"].startswith("http://192.168.178.111:7860/k/") and j["share_link"] == j["link"]
    monkeypatch.setattr(delivery, "lan_ip", lambda: None)  # no network: keep what the browser used
    j = env.client.post(f"/api/workshop/{PID}/versions", json={"variant_ids": ["v1room"]}, headers={"host": "127.0.0.1:7860"}).json()
    assert j["link"].startswith("http://127.0.0.1:7860/k/")


def test_public_link_in_pdf_and_qr(env):
    env.settings(public_base_url="https://entwurf.schreinerei-muster.de/")
    res = create(env)
    token = res["version"]["token"]
    link = f"https://entwurf.schreinerei-muster.de/k/{token}"
    assert res["public_link"] == link and not res["lan_only"] and res["link"].startswith("http://testserver/k/")
    assert res["qr_svg"].startswith("<svg") and "href" not in res["qr_svg"] and "viewBox" in res["qr_svg"]
    vdir = env.out / res["version"]["dir"]
    pdf = text_of_pdf(vdir / res["version"]["files"]["pdf"])
    assert "Online ansehen und freigeben" in pdf and link in pdf.replace("\n", "")
    assert "testserver" not in pdf
    assert link in (vdir / "LIESMICH.txt").read_text()
    r = env.client.get(f"/api/workshop/{PID}/versions/1/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


def test_version_validation(env):
    post = lambda ids: env.client.post(f"/api/workshop/{PID}/versions", json={"variant_ids": ids})
    assert post([]).json()["detail"] == "Bitte mindestens einen Entwurf wählen."
    assert post(["v1room", "v2nuss", "v3rend", "a1", "a2"]).json()["detail"] == "Höchstens 4 Entwürfe pro Version."
    r = post(["v1room", "nope99"])
    assert r.status_code == 404 and r.json()["detail"] == "Vorschlag nicht gefunden."
    assert post(["v9trsh"]).status_code == 404  # trashed variants cannot be sent
    assert env.client.post("/api/workshop/zzzz9999yyyy/versions", json={"variant_ids": ["v1room"]}).status_code == 404
    assert not (env.out / "exports" / f"workshop-{PID}").exists() or not list((env.out / "exports" / f"workshop-{PID}").iterdir())


def test_parallel_versions(env):
    results, errors = [], []

    def run():
        try:
            results.append(delivery.create_version(PID, ["v1room"], "", 90, "anna", "http://h"))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and sorted(r["version"]["n"] for r in results) == [1, 2]
    d = env.out / "exports" / f"workshop-{PID}"
    assert sorted(p.name for p in d.iterdir()) == ["v1", "v2"]
    assert sorted(v["n"] for v in env.doc()["versions"]) == [1, 2]
    assert len({r["version"]["token"] for r in results}) == 2


def test_build_failure_leaves_nothing(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("PDF kaputt")

    real_build = delivery.printing.build_sheet
    monkeypatch.setattr(delivery.printing, "build_sheet", boom)
    with pytest.raises(RuntimeError):
        delivery.create_version(PID, ["v1room"], "", 90, "anna", "http://h")
    d = env.out / "exports" / f"workshop-{PID}"
    assert list(d.iterdir()) == [] and env.doc()["versions"] == [] and env.doc()["status"] == "entwurf"
    assert not (env.store / "shares.json").exists() or json.loads((env.store / "shares.json").read_text()) == {}
    monkeypatch.setattr(delivery.printing, "build_sheet", real_build)
    assert delivery.create_version(PID, ["v1room"], "", 90, "anna", "http://h")["version"]["n"] == 1  # number reused


def test_commit_failure_removes_folder_and_token(env, monkeypatch):
    real = env.projects.mutate

    def failing(kind, pid, fn, who=None, auto=True):
        raise OSError("Platte voll")

    monkeypatch.setattr(env.projects, "mutate", failing)
    with pytest.raises(OSError):
        delivery.create_version(PID, ["v1room"], "", 90, "anna", "http://h")
    monkeypatch.setattr(env.projects, "mutate", real)
    assert list((env.out / "exports" / f"workshop-{PID}").iterdir()) == []
    assert json.loads((env.store / "shares.json").read_text()) == {} and delivery.RESERVED[PID] == set()


def test_project_lock_not_held_while_building(env, monkeypatch):
    started, release = threading.Event(), threading.Event()
    real = printing.build_sheet

    def slow(*a, **k):
        started.set()
        assert release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(delivery.printing, "build_sheet", slow)
    t = threading.Thread(target=delivery.create_version, args=(PID, ["v1room"], "", 90, "anna", "http://h"))
    t.start()
    assert started.wait(10)
    done = threading.Event()

    def edit():
        env.projects.mutate("workshop", PID, lambda d: d.update(customer="Familie Neu"), who="anna", auto=False)
        done.set()

    threading.Thread(target=edit).start()
    assert done.wait(3), "projects.mutate blocked while a version was being built"
    release.set()
    t.join(10)
    doc = env.doc()
    assert doc["customer"] == "Familie Neu" and doc["versions"][0]["n"] == 1


def test_variant_deleted_while_building_is_refused(env, monkeypatch):
    real = printing.build_sheet

    def delete_then_build(*a, **k):
        env.projects.mutate("workshop", PID, lambda d: d.update(variants=[v for v in d["variants"] if v["id"] != "v2nuss"]))
        return real(*a, **k)

    monkeypatch.setattr(delivery.printing, "build_sheet", delete_then_build)
    r = env.client.post(f"/api/workshop/{PID}/versions", json={"variant_ids": ["v1room", "v2nuss"]})
    assert r.status_code == 409 and env.doc()["versions"] == []
    assert list((env.out / "exports" / f"workshop-{PID}").iterdir()) == []


def test_draft_sheet_fresh_folders_and_v1_untouched(env):
    create(env)
    v1 = env.out / f"exports/workshop-{PID}/v1"
    before = {p.name: p.read_bytes() for p in v1.iterdir()}
    urls = []
    for _ in range(5):
        r = env.client.post(f"/api/workshop/{PID}/sheet", json={"variant_ids": ["v3rend"]})
        assert r.status_code == 200, r.text
        urls.append(r.json()["pdf"])
    assert len(set(urls)) == 5 and all(u.startswith(f"/outputs/exports/workshop-{PID}/entwurf-") for u in urls)
    assert urls[-1].endswith("/Visualisierung_2026-007_Familie-Beispiel_Entwurf.pdf")
    d = env.out / f"exports/workshop-{PID}"
    drafts = sorted(p.name for p in d.glob("entwurf-*"))
    assert len(drafts) == 3 and urls[-1].split("/")[-2] == drafts[-1]
    assert {p.name: p.read_bytes() for p in v1.iterdir()} == before
    pdf = env.out / urls[-1].removeprefix("/outputs/")
    t = text_of_pdf(pdf)
    assert "Skizze" in t and "Gewählter Entwurf" not in t and "Interne Notiz" not in t and "Schreinerei Muster" in t
    r = env.client.post(f"/api/workshop/{PID}/sheet")  # default choice (newest) without a body
    assert r.status_code == 200 and r.json()["skipped"] == []


def test_withdraw(env):
    res = create(env)
    token = res["version"]["token"]
    assert env.client.get(f"/k/{token}/data").status_code == 200
    r = env.client.post(f"/api/workshop/{PID}/versions/1/withdraw")
    assert r.status_code == 200 and r.json()["version"]["status"] == "zurueckgezogen"
    assert json.loads((env.store / "shares.json").read_text())[token]["revoked"] is True
    assert env.client.get(f"/k/{token}/data").status_code == 410
    assert env.client.get(f"/k/{token}/pdf").status_code == 410
    doc = env.doc()
    assert [a["text"] for a in doc["activity"] if a["what"] == "withdrawn"] == ["Version 1 zurückgezogen"]
    if "locked" in doc["variants"][0]:
        assert not any(v["locked"] for v in doc["variants"])
    assert env.client.post(f"/api/workshop/{PID}/versions/1/withdraw").status_code == 200  # idempotent, logged once
    assert len([a for a in env.doc()["activity"] if a["what"] == "withdrawn"]) == 1
    assert create(env)["version"]["n"] == 2  # a new version replaces it


def test_approval_capture_and_undo(env):
    create(env)
    r = env.client.post(f"/api/workshop/{PID}/approval", json={"version": 1, "variant_id": "v2nuss", "via": "telefon",
                                                               "name": "Herr Beispiel", "comment": "passt"})
    assert r.status_code == 200, r.text
    a = r.json()["approved"]
    assert {k: a[k] for k in ("version", "variant_id", "via", "name", "comment")} == \
        {"version": 1, "variant_id": "v2nuss", "via": "telefon", "name": "Herr Beispiel", "comment": "passt"}
    doc = env.doc()
    assert doc["status"] == "freigegeben" and doc["versions"][0]["status"] == "freigegeben" and r.json()["doc_rev"] == doc["rev"]
    assert doc["versions"][0]["response"]["via"] == "telefon"
    assert any(x["text"] == "Entwurf 2 freigegeben – telefonisch (erfasst von anon)" for x in doc["activity"])
    bad = env.client.post(f"/api/workshop/{PID}/approval", json={"version": 1, "variant_id": "v3rend", "via": "papier"})
    assert bad.status_code == 404
    assert env.client.post(f"/api/workshop/{PID}/approval", json={"version": 1, "variant_id": "v2nuss", "via": "fax"}).status_code == 400
    r = env.client.delete(f"/api/workshop/{PID}/approval")
    assert r.status_code == 200 and r.json()["approved"] is None
    doc = env.doc()
    assert doc["status"] == "freigegeben" and doc["versions"][0]["status"] == "offen" and doc["versions"][0]["response"] is None
    assert doc["activity"][-1]["text"] == "Freigabe zurückgenommen"
    env.client.post(f"/api/workshop/{PID}/versions/1/withdraw")
    r = env.client.post(f"/api/workshop/{PID}/approval", json={"version": 1, "variant_id": "v2nuss", "via": "vor_ort"})
    assert r.status_code == 409


def test_approved_variant_stamped_in_next_version(env):
    create(env)
    env.client.post(f"/api/workshop/{PID}/approval", json={"version": 1, "variant_id": "v1room", "via": "papier"})
    ver = create(env, ids=("v3rend", "v1room"))["version"]
    t = text_of_pdf(env.out / ver["dir"] / ver["files"]["pdf"])
    assert "Freigegebener Entwurf" in t and "Gewählter Entwurf" not in t


# ---------------- DSGVO export ----------------

def test_export_zip(env):
    create(env)
    (env.jobs / "j1.json").write_text(json.dumps({"id": "j1", "project": PID, "refs": ["refs/jobref.png"]}))
    img(env.out / "refs/jobref.png", (32, 32))
    r = env.client.get(f"/api/workshop/{PID}/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        doc = json.loads(z.read("projekt.json"))
        readme = z.read("LIESMICH.txt").decode()
    assert doc["id"] == PID and doc["internal_notes"] == "Rabatt intern"  # Art. 15: everything stored about the order
    for rel in ("outputs/uploads/room.png", "outputs/20261018-100000-1.png", "outputs/20261018-100000-1.json",
                "outputs/refs/ref0.png", "outputs/20261018-090000-9.png", "outputs/geometry/room-depth.jpg",
                f"outputs/guides/{PID}-0123456789.png", "outputs/refs/jobref.png", "outputs/uploads/sketch.png"):
        assert rel in names, rel
    assert any(n.startswith(f"outputs/exports/workshop-{PID}/v1/") for n in names)
    assert "Datenexport vom" in readme and "2026-007" in readme
    assert env.doc()["activity"][-1]["what"] == "export"
    assert not list((env.out / "exports").glob(".export-*"))  # temp file removed after streaming


# ---------------- purge ----------------

def all_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def purge(env, confirm="TEST Küche"):
    return env.client.post(f"/api/workshop/{PID}/purge", json={"confirm": confirm})


def test_purge_wrong_confirm(env):
    r = purge(env, "TEST Kueche")
    assert r.status_code == 400 and r.json()["detail"] == "Bestätigung stimmt nicht – bitte den Titel genau eintippen."
    assert (env.store / "workshop" / f"{PID}.json").exists()


def test_purge_empty_title_needs_loeschen(env):
    env.write(make_doc(title=""))
    assert purge(env, "").status_code == 400
    assert purge(env, "LÖSCHEN").status_code == 200


def test_purge_deletes_everything_of_the_order(env):
    res = create(env)
    before = all_files(env.data)
    r = purge(env)
    assert r.status_code == 200, r.text
    after = all_files(env.data)
    gone = before - after
    assert f"projects/workshop/{PID}.json" in gone
    for rel in ("outputs/uploads/room.png", "outputs/uploads/sketch.png", "outputs/20261018-100000-1.png",
                "outputs/20261018-100000-1.json", "outputs/refs/ref1.png", f"outputs/guides/{PID}-0123456789.png"):
        assert rel in gone, rel
    assert not (env.out / "exports" / f"workshop-{PID}").exists()
    assert r.json() == {"ok": True, "files": len(gone)}
    assert res["version"]["token"] not in json.loads((env.store / "shares.json").read_text())
    assert "outputs/uploads/logo.png" in after  # company data, not the customer's
    assert env.client.get(f"/k/{res['version']['token']}/data").status_code == 404


def test_purge_keeps_files_other_projects_use(env):
    other = make_doc(OTHER, title="Anderer Kunde")
    other["variants"] = [dict(other["variants"][0], id="o1room", image="20261018-100100-2.png")]
    other["trash"], other["pieces"][0]["sketch"] = [], None
    env.write(other)
    env.write({"id": "bookbook1234", "pages": [{"image": "20261018-100200-3.png"}]}, kind="book")
    assert purge(env).status_code == 200
    for rel in ("uploads/room.png", "geometry/room.json", "geometry/room-depth.npy", "geometry/room-depth.jpg",
                "20261018-100100-2.png", "20261018-100100-2.json", "refs/ref1.png", "20261018-100200-3.png"):
        assert (env.out / rel).exists(), rel
    assert not (env.out / "20261018-100000-1.png").exists() and not (env.out / "uploads/sketch.png").exists()


def test_purge_finds_replaced_photos_and_renders_out_of_the_trash(env):
    img(env.out / "uploads/old-room.png")
    img(env.out / "20261001-000000-5.png")
    (env.out / "20261001-000000-5.json").write_text(json.dumps({"id": "20261001-000000-5", "refs": ["refs/r5.png"],
                                                                "source": f"workshop:{PID}"}))
    img(env.out / "refs/r5.png", (32, 32))
    img(env.out / "20261001-000000-6.png")
    (env.out / "20261001-000000-6.json").write_text(json.dumps({"id": "x", "source": f"workshop:{OTHER}"}))
    env.projects.mutate("workshop", PID, lambda d: d.update(old_files=["uploads/old-room.png"]))
    assert purge(env).status_code == 200
    for rel in ("uploads/old-room.png", "20261001-000000-5.png", "20261001-000000-5.json", "refs/r5.png"):
        assert not (env.out / rel).exists(), rel
    assert (env.out / "20261001-000000-6.png").exists()


def test_purge_deletes_geometry_cache(env):
    assert purge(env).status_code == 200
    for s in (".json", "-depth.npy", "-depth.jpg"):
        assert not (env.out / f"geometry/room{s}").exists()


def test_purge_deletes_backup_v2_copy(env):
    bk = env.store / "backup-v2" / "workshop" / f"{PID}.json"
    bk.parent.mkdir(parents=True)
    bk.write_text("{}")
    other = env.store / "backup-v2" / "workshop" / f"{OTHER}.json"
    other.write_text("{}")
    assert purge(env).status_code == 200
    assert not bk.exists() and other.exists()


def test_purge_deletes_job_refs(env):
    (env.jobs / "j1.json").write_text(json.dumps({"id": "j1", "project": PID, "refs": ["refs/jobref.png"]}))
    (env.jobs / "j2.json").write_text(json.dumps({"id": "j2", "project": OTHER, "refs": ["refs/other.png"]}))
    img(env.out / "refs/jobref.png", (32, 32))
    img(env.out / "refs/other.png", (32, 32))
    assert purge(env).status_code == 200
    assert not (env.out / "refs/jobref.png").exists() and not (env.jobs / "j1.json").exists()
    assert (env.out / "refs/other.png").exists() and (env.jobs / "j2.json").exists()


def test_purge_deletes_trashed_variants(env):
    assert purge(env).status_code == 200
    assert not (env.out / "20261018-090000-9.png").exists() and not (env.out / "20261018-090000-9.json").exists()
    assert not (env.out / "refs/ref3.png").exists()


def test_purge_cancels_jobs_first(env, monkeypatch):
    calls = []

    def hook(pid):
        calls.append((pid, (env.store / "workshop" / f"{PID}.json").exists(), (env.out / "uploads/room.png").exists()))
        return False

    monkeypatch.setattr(delivery, "jobs_hook", hook)
    before = all_files(env.data)
    r = purge(env)
    assert r.status_code == 503 and "lässt sich gerade nicht stoppen" in r.json()["detail"]
    assert calls == [(PID, True, True)] and all_files(env.data) == before
    monkeypatch.setattr(delivery, "jobs_hook", lambda pid: calls.append(pid) or True)
    assert purge(env).status_code == 200 and calls[-1] == PID


def test_purge_log_has_no_personal_data(env):
    assert purge(env).status_code == 200
    line = (env.store / "deletions.log").read_text().strip()
    assert line.split(" ")[1:4] == ["workshop", PID, "auf-wunsch"] and line.endswith("-dateien")
    for bad in ("Küche", "TEST", "Beispiel", "Hauptstr", "0981"):
        assert bad not in line
    datetime.fromisoformat(line.split(" ")[0])


def test_referenced_files_never_leave_the_data_root(env):
    doc = make_doc(room="../../etc/passwd.png", sample="/etc/hosts")
    doc["variants"][0]["image"] = "../secret.png"
    files = delivery.referenced_files(doc, env.data)
    assert all(env.data in p.parents for p in files)
    assert not any("passwd" in str(p) or "hosts" in str(p) or "secret" in str(p) for p in files)
