"""GPU scheduler (jobs.py) with FakeEngine/FakeUpsampler, tmp dirs, no GPU, no server."""

import ast
import io
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs  # noqa: E402
import judge  # noqa: E402

ROWS = [{"id": "moebel_1", "bbox": [100, 120, 600, 500], "desc": "Schrank"}]


def until(cond, timeout=10.0, msg="timeout"):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError(msg)


def P(prompt="Schrank", count=1, steps=4, upsample=False, **kw):
    return {"prompt": prompt, "count": count, "steps": steps, "upsample": upsample, "width": 256, "height": 256,
            "seed": 7, **kw}


def room():
    return Image.new("RGB", (256, 256), (120, 90, 60))


def finished(s, jid):
    j = s.jobs[jid]
    return j["state"] in jobs.FINAL and j["judge_state"] not in ("pending", "running")


def images_order(s):
    return [c["prompt"].split()[0] for c in s.engine.calls]


@pytest.fixture
def mk(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "status", lambda force=False: {"available": True, "state": "active", "model": "fake"})
    made = []

    def make(step_s=0.01, delay=0.2, state="up", hooks=None, start=True, out=None, jobs_dir=None, record=None):
        eng, ups = jobs.FakeEngine(step_s), jobs.FakeUpsampler(delay, state)
        rec = []

        def default_record(job, judge_only=False):
            rec.append({"id": job["id"], "state": job["state"], "n": len(job["results"]), "judge_only": judge_only,
                        "judge_state": job["judge_state"]})
            if record:
                record(job, judge_only)

        hk = {"record": default_record, "project_title": lambda pid: f"Titel {pid}", **(hooks or {})}
        s = jobs.Scheduler(eng, ups, lambda img, ref, keep: img, out_dir=out or tmp_path / "out",
                           jobs_dir=jobs_dir or tmp_path / "jobs", hooks=hk)
        s.engine, s.rec = eng, rec
        made.append(s)
        if start:
            s.start()
        return s

    yield make
    for s in made:
        s.drain(timeout=3)


def wake(s):
    with s._lock:
        s._cv.notify_all()


# ---------------- round robin, classes ----------------

def test_round_robin_both_queued(mk):
    s = mk(start=False)
    a = s.submit(P("A", count=4), [], owner="anna", owner_name="Anna")
    b = s.submit(P("B", count=1), [], owner="ben", owner_name="Ben")
    s.start()
    until(lambda: finished(s, a["id"]) and finished(s, b["id"]))
    order = images_order(s)
    assert order.index("B") in (1, 2) and order.count("A") == 4


def test_round_robin_late_arrival(mk):
    s = mk(step_s=0.03)
    a = s.submit(P("A", count=4, steps=6), [], owner="anna")
    until(lambda: s.engine.calls or s._current)
    b = s.submit(P("B", count=1, steps=6), [], owner="ben")
    until(lambda: finished(s, a["id"]) and finished(s, b["id"]))
    assert images_order(s).index("B") in (1, 2)


def test_owner_fifo_and_bg_waits_for_fg(mk):
    s = mk(start=False)
    bg = s.submit(P("BG", count=6), [], owner="carl")          # count > 4 → Hintergrund
    j1 = s.submit(P("J1", count=2), [], owner="anna")
    j2 = s.submit(P("J2", count=1), [], owner="anna")
    assert s.jobs[bg["id"]]["class"] == "bg" and s.jobs[j1["id"]]["class"] == "fg"
    s.start()
    until(lambda: all(finished(s, x["id"]) for x in (bg, j1, j2)))
    order = images_order(s)
    assert order[:3] == ["J1", "J1", "J2"] and order[3:] == ["BG"] * 6


# ---------------- captions ----------------

def test_caption_batching_and_backends(mk):
    s = mk(start=False, delay=0.3)
    a = s.submit(P("A", upsample=True, boxes=ROWS), [room()], owner="anna")
    b = s.submit(P("B", upsample=True), [], owner="ben")
    c = s.submit(P("C", upsample=False, boxes=ROWS), [room()], owner="carl")
    d = s.submit(P("D", caption="Fertige Beschreibung"), [], owner="dora")
    s.start()
    until(lambda: all(finished(s, x["id"]) for x in (a, b, c, d)))
    assert s.ups.batches == [2]
    assert [t[0] for t in s.trace].count("caption") == 1
    ja, jb, jc, jd = (s.jobs[x["id"]] for x in (a, b, c, d))
    assert ja["caption_backend"] == jb["caption_backend"] == "vllm"
    assert ja["caption"].startswith("[fake] A") and jobs.split_caption(ja["caption"])[1] == ROWS
    assert jc["caption_backend"] == "template" and jc["caption"] == f"C {json.dumps(ROWS)}"
    assert jd["caption_backend"] == "reuse" and jd["caption"] == "Fertige Beschreibung"
    assert ja["results"][0]["caption_backend"] == "vllm" and ja["results"][0]["job"] == ja["id"]


def test_caption_progress_streams(mk):
    s = mk(delay=1.0)
    a = s.submit(P("A", upsample=True), [], owner="anna")
    until(lambda: s.jobs[a["id"]]["phase"] == "caption" and (s.jobs[a["id"]]["step"] or 0) > 20)
    v = s.get(a["id"])
    assert v["steps"] == 100 and v["stage"] == "Upsampling prompt…"
    assert v["stage_text"] == "Beschreibung wird vorbereitet …" and "%" not in v["stage_text"]  # one overall bar instead
    assert 0 < v["progress"] < 1 and v["eta_done_s"] > 0
    assert v["eta_text"].startswith("Bilder ")                    # the job's whole time, never only the caption's
    s.jobs[a["id"]]["step"] = 99
    assert s.get(a["id"])["stage_text"] == "Beschreibung wird vorbereitet …"
    until(lambda: finished(s, a["id"]))
    assert s.get(a["id"])["progress"] == 1.0


def test_eta_text_and_german_durations():
    assert [jobs.dur_de(x) for x in (8, 32, 44, 75, 200, 210, 615)] == \
        ["ca. 10 Sek.", "ca. 30 Sek.", "ca. 40 Sek.", "ca. 1½ Min.", "ca. 3½ Min.", "ca. 3½ Min.", "ca. 10 Min."]
    assert jobs.eta_text(206, 30) == "Bilder in ca. 3½ Min., Bewertung ca. 30 Sek. danach"
    assert jobs.eta_text(10, 0) == "Bilder gleich fertig" and jobs.eta_text(None, 25, True) == "Bewertung in ca. 30 Sek."
    assert jobs.eta_text(None, 0, True) == ""


def test_estimate_uses_measured_durations(mk):
    s = mk(step_s=0.01, start=False)
    params = P("X", count=2, steps=8, width=1024, height=1024, upsample=False)
    model = s.estimate(params, [])
    key = jobs.dur_key("none", 8, 2, 1024, 1024)
    s.durations[key] = jobs.deque([204.0, 190.0, 900.0], maxlen=jobs.DUR_HIST)  # one outlier does not count
    assert model != 204.0 and s.estimate(params, []) == 204.0
    s._save_stats()
    assert list(mk(start=False).durations[key]) == [204.0, 190.0, 900.0]


def test_job_duration_is_learnt(mk):
    s = mk(step_s=0.01)
    a = s.submit(P("A", count=2, steps=4, width=512, height=512, upsample=False), [], owner="anna")
    until(lambda: finished(s, a["id"]))
    assert len(s.durations[jobs.dur_key("none", 4, 2, 512, 512)]) == 1


def test_caption_failure_errors_job(mk):
    s = mk(start=False)
    s.ups.caption_many = lambda items, cancels=None, on_progress=None: [{"error": "kaputt"} for _ in items]
    a = s.submit(P("A", upsample=True), [room()], owner="anna")
    s.start()
    until(lambda: finished(s, a["id"]))
    j = s.jobs[a["id"]]
    assert j["state"] == "error" and j["error"] == "Beschreibung fehlgeschlagen: kaputt"
    until(lambda: not (s.out / j["_refs"][0]).exists())  # ended without results → refs deleted


def test_caption_only_job(mk):
    s = mk()
    a = s.submit({"prompt": "Ein Tisch", "caption_only": True}, [], owner="anna")
    until(lambda: finished(s, a["id"]))
    v = s.get(a["id"])
    assert v["kind"] == "upsample" and v["state"] == "done" and v["caption"] == "[fake] Ein Tisch" and v["results"] == []
    assert s.engine.calls == []


# ---------------- judge ----------------

def test_judge_unit_never_overlaps_flux(mk, monkeypatch):
    calls = []

    def grade(images, request, timeout=180):
        t0 = time.time()
        time.sleep(0.15)
        calls.append((t0, time.time()))
        return {"status": "ok", "pass": True, "overall": 8.0, "fix_hint": ""}

    monkeypatch.setattr(judge, "grade", grade)
    s = mk(start=False, step_s=0.02)
    a = s.submit(P("A", count=2, judge={"request": {}, "room_ref": 0}), [room()], owner="anna")
    b = s.submit(P("B", count=1), [], owner="ben")
    s.start()
    until(lambda: finished(s, a["id"]) and finished(s, b["id"]))
    assert [(k, s.jobs[i[0]]["params"]["prompt"]) for k, i, *_ in s.trace] == [
        ("image", "A"), ("image", "B"), ("image", "A"), ("judge", "A")]
    assert len(calls) == 2
    for g0, g1 in calls:
        for e0, e1 in s.engine.intervals:
            assert g1 <= e0 or e1 <= g0, "FLUX ran while the judge graded"
    ja = s.jobs[a["id"]]
    assert ja["judge_state"] == "done" and all(r["judge"]["status"] == "ok" for r in ja["results"])
    assert json.loads((s.out / f"{ja['results'][0]['id']}.json").read_text())["judge"]["status"] == "ok"
    assert any(r["judge_only"] for r in s.rec) and s.rec[-1]["judge_state"] in ("done", "off")


def test_judge_unavailable_no_unit(mk, monkeypatch):
    monkeypatch.setattr(judge, "status", lambda force=False: {"available": False})
    monkeypatch.setattr(judge, "grade", lambda *a, **k: pytest.fail("graded although unavailable"))
    s = mk()
    a = s.submit(P("A", judge={"request": {}}), [], owner="anna")
    until(lambda: finished(s, a["id"]))
    j = s.jobs[a["id"]]
    assert j["judge_state"] == "unavailable" and j["results"][0]["judge"]["status"] == "unavailable"
    assert not s.running(a["id"])
    until(lambda: s.rec and s.rec[-1]["judge_state"] == "unavailable")   # the record is persisted after finished()
    assert "judge" not in [t[0] for t in s.trace]


def test_auto_retry_reuses_caption(mk, monkeypatch):
    monkeypatch.setattr(judge, "grade", lambda images, request, timeout=180: {
        "status": "ok", "pass": False, "overall": 3.0, "fix_hint": "keep the window free"})
    s = mk(start=False)
    meta = {"variant": {"kind": "room", "piece_ids": ["p1"]}}
    a = s.submit(P("Raum", count=2, upsample=True, boxes=ROWS, retry=1, meta=meta, source="workshop:p1aaaa",
                   judge={"request": {}, "room_ref": 0}), [room()], owner="anna", owner_name="Anna",
                 project="p1aaaa", dedupe="design")
    s.start()
    until(lambda: s.jobs[a["id"]]["retry_job"] and finished(s, s.jobs[a["id"]]["retry_job"]) and finished(s, a["id"]))
    old, new = s.jobs[a["id"]], s.jobs[s.jobs[a["id"]]["retry_job"]]
    text, rows = jobs.split_caption(new["caption"])
    assert rows == ROWS and "Important corrections: keep the window free" in text
    assert new["caption"].index("keep the window") < new["caption"].index("[{")
    assert new["caption_backend"] == "reuse" and s.ups.batches == [1]  # no second caption unit
    assert (new["owner"], new["project"], new["retry_of"], new["dedupe"]) == ("anna", "p1aaaa", old["id"], "design")
    assert new["label"] == "Neuer Versuch mit Korrektur" and new["count"] == 1
    assert new["params"]["meta"]["variant"]["retry_of"] == old["id"] and new["params"]["retry"] == 0
    assert new["retry_job"] is None  # only one retry


# ---------------- upsampler starting ----------------

def test_pup_starting_runs_other_images(mk):
    s = mk(state="starting", step_s=0.03, start=False)
    s.ups.START_WAIT_S = 30
    a = s.submit(P("A", upsample=True), [], owner="anna")
    b = s.submit(P("B", count=3, steps=6), [], owner="ben")
    s.start()
    until(lambda: s.jobs[a["id"]]["phase"] == "caption_wait")
    va = s.get(a["id"])
    assert va["state"] == "queued" and va["stage"] == "Queued"
    assert va["stage_text"] == "Beschreibungs-KI startet – andere Bilder laufen solange …"
    until(lambda: finished(s, b["id"]))
    assert s.jobs[a["id"]]["state"] == "queued" and s.engine.calls and s.ups.batches == []  # A waits, GPU was busy
    s.ups.state = "up"
    wake(s)
    until(lambda: finished(s, a["id"]), timeout=8)
    assert s.jobs[a["id"]]["caption_backend"] == "vllm"


def test_pup_starting_timeout_template(mk):
    s = mk(state="starting", start=False)
    s.ups.START_WAIT_S = 1
    a = s.submit(P("A", upsample=True, boxes=ROWS), [room()], owner="anna")
    t0 = time.time()
    s.start()
    until(lambda: finished(s, a["id"]), timeout=6)
    assert 0.9 <= time.time() - t0 < 4
    j = s.jobs[a["id"]]
    assert j["caption_backend"] == "template" and j["caption"] == f"A {json.dumps(ROWS)}"
    assert j["caption_wait_since"] is not None


def test_cancel_during_caption_wait(mk):
    s = mk(state="starting")
    s.ups.START_WAIT_S = 60
    a = s.submit(P("A", upsample=True), [room()], owner="anna", owner_name="Anna")
    until(lambda: s.jobs[a["id"]]["phase"] == "caption_wait")
    t0 = time.time()
    r = s.cancel(a["id"], "anna", "Anna")
    assert r == {"ok": True, "state": "cancelled"} and time.time() - t0 < 1
    assert s.get(a["id"])["stage_text"] == "Abgebrochen von Anna"
    assert s.rec[-1]["state"] == "cancelled"
    assert not any((s.out / "refs").iterdir())


def test_save_failure_errors_job_once(mk):
    s = mk(start=False)

    def boom(img, meta):
        raise OSError(28, "No space left on device")
    s._save_output = boom
    a = s.submit(P("A", count=2), [], owner="anna")
    s.start()
    until(lambda: finished(s, a["id"]))
    j = s.jobs[a["id"]]
    assert j["state"] == "error" and "No space left" in j["error"] and len(s.engine.calls) == 1
    assert s.status()["queue"] == 0


def test_cancel_after_last_step_skips_judge(mk, monkeypatch):
    monkeypatch.setattr(judge, "grade", lambda *a, **k: pytest.fail("graded although cancelled"))
    s = mk(start=False)
    s.keep_fn = lambda img, ref, keep: (time.sleep(0.6), img)[1]
    a = s.submit(P("A", keep=[[0, 0, 500, 500]], judge={"request": {}, "room_ref": 0}), [room()], owner="anna", owner_name="Anna")
    s.start()
    until(lambda: s.jobs[a["id"]]["phase"] == "keep")
    assert s.cancel(a["id"], "anna", "Anna")["state"] == "cancelling"
    until(lambda: finished(s, a["id"]), timeout=3)
    j = s.jobs[a["id"]]
    assert j["state"] == "done" and j["judge_state"] == "done" and j["cancelled_by"] is None
    assert j["results"][0]["judge"]["status"] == "error" and s.status()["queue"] == 0
    assert "abgebrochen" not in s.get(a["id"])["stage_text"]


def test_cached_caption_skips_unit_and_wait(mk):
    s = mk(state="starting", start=False)
    s.ups.cached = lambda item: f"cached: {item['request']}"
    a = s.submit(P("A", upsample=True), [room()], owner="anna")
    assert s.jobs[a["id"]]["caption_backend"] == "cache"
    t0 = time.time()
    s.start()
    until(lambda: finished(s, a["id"]), timeout=3)
    assert time.time() - t0 < 2 and s.ups.batches == [] and s.engine.calls[0]["prompt"] == "cached: A"


def test_on_accept_before_runnable(mk):
    s = mk()
    seen = []
    a = s.submit(P("A"), [], owner="anna", on_accept=lambda jid: seen.append((jid, s.jobs[jid]["_ready"], s.running(jid))))
    assert seen == [(a["id"], False, True)]

    def fail(jid):
        raise HTTPException(404, "gone")
    with pytest.raises(HTTPException):
        s.submit(P("B"), [], owner="anna", on_accept=fail)
    assert [j["params"]["prompt"] for j in s.jobs.values()] == ["A"]


def test_exclusive_waits_for_flux(mk):
    s = mk(step_s=0.05)
    a = s.submit(P("A", steps=10), [], owner="anna")
    until(lambda: (s.jobs[a["id"]]["step"] or 0) >= 1)
    t = s.exclusive(lambda: time.time())
    assert t >= s.engine.intervals[-1][1] - 0.01


def test_caption_wait_resets_when_up_and_on_recover(mk, tmp_path):
    s = mk(state="starting", start=False)
    s.ups.START_WAIT_S = 60
    a = s.submit(P("A", upsample=True), [], owner="anna")
    s.start()
    until(lambda: s.jobs[a["id"]]["caption_wait_since"] is not None)
    s.drain(timeout=3)
    s2 = mk(state="starting", start=False, jobs_dir=tmp_path / "jobs")
    s2.recover()
    assert s2.jobs[a["id"]]["caption_wait_since"] is None


# ---------------- dedupe, client key ----------------

def test_dedupe_and_force(mk):
    s = mk(start=False)
    a = s.submit(P("A"), [], owner="anna", owner_name="Anna", project="p1aaaa", dedupe="design")
    with pytest.raises(jobs.Duplicate) as e:
        s.submit(P("A"), [], owner="ben", project="p1aaaa", dedupe="design")
    assert e.value.view["id"] == a["id"] and e.value.view["owner_name"] == "Anna" and "caption" not in e.value.view
    other = s.submit(P("A"), [], owner="ben", project="p1aaaa", dedupe="render:p2")
    forced = s.submit(P("A"), [], owner="ben", project="p1aaaa", dedupe="design", force=True)
    assert len({a["id"], other["id"], forced["id"]}) == 3
    s.start()
    until(lambda: all(finished(s, x["id"]) for x in (a, other, forced)))
    s.submit(P("A"), [], owner="ben", project="p1aaaa", dedupe="design")  # finished → no conflict


def test_dedupe_atomic_two_devices(mk):
    s = mk(start=False)
    res, barrier = [], threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            res.append(s.submit(P("A"), [room()], owner=f"d{i}", project="p1aaaa", dedupe="design")["id"])
        except jobs.Duplicate as e:
            res.append(("dup", e.view["id"]))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ids = [r for r in res if isinstance(r, str)]
    assert len(ids) == 1 and all(r[1] == ids[0] for r in res if isinstance(r, tuple))


def test_client_key_returns_same_job(mk):
    s = mk(start=False)
    a = s.submit(P("A"), [], owner="anna", client_key="c-1")
    b = s.submit(P("A"), [], owner="anna", client_key="c-1")
    assert a["id"] == b["id"] and len(s.jobs) == 1


# ---------------- cancel ----------------

def test_cancel_queued_and_rights(mk):
    s = mk(start=False)
    a = s.submit(P("A"), [room()], owner="anna", owner_name="Anna")
    r = s.cancel(a["id"], "ben", "Ben")
    assert r == {"ok": False, "code": "not_owner", "owner_name": "Anna",
                 "message": "Dieser Auftrag gehört Anna. Trotzdem abbrechen?"}
    assert s.jobs[a["id"]]["state"] == "queued"
    assert s.cancel(a["id"], "ben", "Ben", force=True) == {"ok": True, "state": "cancelled"}
    j = s.jobs[a["id"]]
    assert j["cancelled_by"] == "Ben" and s.rec[-1]["state"] == "cancelled"
    assert not (s.dir / f"{a['id']}.json").exists() and not (s.out / j["_refs"][0]).exists()
    with pytest.raises(HTTPException) as e:
        s.cancel("nope", "anna")
    assert e.value.status_code == 404


def test_cancel_running_mid_image(mk):
    s = mk(step_s=0.03)
    a = s.submit(P("A", count=3, steps=20), [room()], owner="anna", owner_name="Anna")
    until(lambda: len(s.jobs[a["id"]]["results"]) == 1 and 3 <= (s.jobs[a["id"]]["step"] or 0) <= 10, timeout=10)
    t0 = time.time()
    assert s.cancel(a["id"], "anna", "Anna")["state"] == "cancelling"
    assert s.get(a["id"])["stage_text"] == "Berechnung wird abgebrochen …"
    until(lambda: s.jobs[a["id"]]["state"] == "cancelled", timeout=1)
    assert time.time() - t0 < 1
    j = s.jobs[a["id"]]
    assert len(j["results"]) == 1 and (s.out / f"{j['results'][0]['id']}.png").exists()
    assert (s.out / j["_refs"][0]).exists()  # kept: the saved image's meta points to it
    assert s.rec[-1]["state"] == "cancelled" and s.rec[-1]["n"] == 1
    assert s.get(a["id"])["stage_text"] == "Abgebrochen von Anna"


def test_cancel_project_and_gone(mk):
    s = mk(step_s=0.05)
    a = s.submit(P("A", steps=40, source="workshop:p1aaaa"), [room()], owner="anna", project="p1aaaa")
    b = s.submit(P("B", source="workshop:p1aaaa"), [room()], owner="anna", project="p1aaaa")
    until(lambda: s._current and a["id"] in s._current["jobs"])
    t0 = time.time()
    assert s.cancel_project("p1aaaa", timeout=5) is True
    assert time.time() - t0 < 1
    assert s.jobs[a["id"]]["state"] == s.jobs[b["id"]]["state"] == "cancelled"
    assert not any((s.out / "refs").iterdir()) and not list(s.dir.glob("*-*.json"))
    c = s.submit(P("C"), [], owner="anna", project="p1aaaa")  # purge given up: the order works again
    until(lambda: finished(s, c["id"]))
    assert s.jobs[c["id"]]["state"] == "done" and "p1aaaa" not in s._gone


def test_late_record_gone_deletes_files(mk):
    def record(job, judge_only=False):
        if job["results"]:
            raise HTTPException(404, "Auftrag nicht gefunden.")

    s = mk(record=record)
    a = s.submit(P("A", count=2, source="workshop:p1aaaa"), [room()], owner="anna", project="p1aaaa")
    until(lambda: finished(s, a["id"]))
    j = s.jobs[a["id"]]
    assert j["state"] == "cancelled"
    until(lambda: not any((s.out / "refs").iterdir()) and not (s.dir / f"{a['id']}.json").exists())
    assert j["results"] == [] and list(s.out.glob("*.png")) == [] and list(s.out.glob("*.json")) == []
    assert len(s.engine.calls) == 1


def test_error_without_results_deletes_refs(mk):
    s = mk(start=False)

    def boom(*a, **k):
        raise RuntimeError("CUDA weg")

    s.engine.generate = boom
    a = s.submit(P("A"), [room()], owner="anna")
    s.start()
    until(lambda: finished(s, a["id"]))
    j = s.jobs[a["id"]]
    assert j["state"] == "error" and j["error"] == "RuntimeError: CUDA weg"
    assert s.get(a["id"])["stage_text"] == "Fehler: RuntimeError: CUDA weg"
    until(lambda: not any((s.out / "refs").iterdir()))


# ---------------- persistence, recovery, drain ----------------

def test_drain_cancels_long_image_and_recovers(mk, tmp_path):
    s = mk(step_s=0.1)
    a = s.submit(P("A", count=2, steps=40, source="workshop:p1aaaa"), [room()], owner="anna", project="p1aaaa")
    until(lambda: (s.jobs[a["id"]]["step"] or 0) >= 1)
    t0 = time.time()
    s.drain(timeout=1)
    assert time.time() - t0 < 2.5 and s._current is None
    d = json.loads((s.dir / f"{a['id']}.json").read_text())
    assert d["done"] == [] and d["state"] in ("queued", "running") and d["refs"]
    logs = []
    s2 = mk(start=False, hooks={"log": lambda *x: logs.append(x)})
    assert s2.recover() == [a["id"]]
    assert logs == [("p1aaaa", "recovered", "Berechnung nach Neustart fortgesetzt", "system")]
    assert s2.get(a["id"])["stage_text"] == "Nach Neustart fortgesetzt"
    s2.start()
    until(lambda: finished(s2, a["id"]))
    assert len(s2.jobs[a["id"]]["results"]) == 2 and len(s2.engine.calls) == 2


def test_drain_finishes_current_image(mk):
    s = mk(step_s=0.02)
    a = s.submit(P("A", count=3, steps=10), [], owner="anna")
    until(lambda: len(s.jobs[a["id"]]["results"]) == 1 and 1 <= (s.jobs[a["id"]]["step"] or 0) <= 5)
    s.drain(timeout=5)
    j = s.jobs[a["id"]]
    assert len(j["results"]) == 2 and j["state"] == "running"
    assert len(json.loads((s.dir / f"{a['id']}.json").read_text())["done"]) == 2


@pytest.mark.parametrize("lost_write", [False, True])
def test_crash_recovery_no_duplicates(mk, tmp_path, lost_write):
    s = mk(step_s=0.03)
    a = s.submit(P("A", count=3, steps=10, source="workshop:p1aaaa"), [room()], owner="anna", project="p1aaaa")
    until(lambda: len(json.loads((s.dir / f"{a['id']}.json").read_text())["done"]) == 1)
    out2, jobs2 = tmp_path / "out2", tmp_path / "jobs2"  # the disk as a crash left it
    shutil.copytree(s.dir, jobs2, ignore=shutil.ignore_patterns(".*.tmp"))
    shutil.copytree(s.out, out2, ignore=shutil.ignore_patterns(".*.tmp"))
    first = json.loads((jobs2 / f"{a['id']}.json").read_text())["done"][0]
    for f in out2.glob("*.json"):
        if f.stem != first:
            f.unlink()
            (out2 / f"{f.stem}.png").unlink(missing_ok=True)
    if lost_write:  # crash between save_output and the job write
        f = jobs2 / f"{a['id']}.json"
        d = json.loads(f.read_text())
        f.write_text(json.dumps({**d, "done": []}))
    s2 = mk(start=False, out=out2, jobs_dir=jobs2)
    assert s2.recover() == [a["id"]]
    assert s2.rec[-1]["n"] == 1  # record reconciles once
    s2.start()
    until(lambda: finished(s2, a["id"]))
    j = s2.jobs[a["id"]]
    seeds = [r["seed"] for r in j["results"]]
    assert seeds == [7, 8, 9] and len(s2.engine.calls) == 2
    until(lambda: not (jobs2 / f"{a['id']}.json").exists())
    assert len(list(out2.glob("*.png"))) == 3


def test_recover_missing_ref(mk, tmp_path):
    s = mk(start=False)
    a = s.submit(P("A"), [room()], owner="anna")
    (s.out / s.jobs[a["id"]]["_refs"][0]).unlink()
    s2 = mk(start=False)
    assert s2.recover() == []
    j = s2.jobs[a["id"]]
    assert j["state"] == "error" and j["error"] == "Vorlage fehlt – bitte neu starten"
    assert not (s2.dir / f"{a['id']}.json").exists()


def test_recover_requeues_judging(mk, monkeypatch):
    monkeypatch.setattr(judge, "grade", lambda *a, **k: {"status": "ok", "pass": True, "overall": 8.0, "fix_hint": ""})
    s = mk(start=False)
    s.ups.caption_many = None
    a = s.submit(P("A", judge={"request": {}}), [], owner="anna")
    j = s.jobs[a["id"]]
    # simulate: image saved, process died before grading
    img = Image.new("RGB", (256, 256))
    meta = {"seed": 7, "job": a["id"], "judge": {"status": "pending"}, "width": 256, "height": 256}
    s._save_output(img, meta)
    j["results"].append(meta)
    j.update(state="done", judge_state="pending")
    s._persist(j)
    s2 = mk(start=False)
    assert s2.recover() == [a["id"]]
    s2.start()
    until(lambda: finished(s2, a["id"]))
    assert s2.jobs[a["id"]]["results"][0]["judge"]["status"] == "ok" and s2.engine.calls == []


# ---------------- plan, ETA, views ----------------

def test_plan_positions_eta_and_views(mk):
    s = mk(step_s=0.1, start=False)
    w0 = s.wait_s("carl")
    a = s.submit(P("GEHEIM-A", count=2, steps=20, source="workshop:p1aaaa"), [], owner="anna", owner_name="Anna",
                 project="p1aaaa", label="2 Vorschläge")
    time.sleep(1.05)
    w1 = s.wait_s("carl")
    assert w1 > w0
    b = s.submit(P("GEHEIM-B", steps=20), [], owner="ben", owner_name="Ben", project="p2bbbb", label="1 Vorschlag")
    s.start()
    until(lambda: (s.jobs[a["id"]]["step"] or 0) >= 2)
    va, vb = s.get(a["id"]), s.get(b["id"])
    assert va["position"] == 0 and va["eta_start_s"] == 0 and va["phase"] == "image"
    assert va["stage_text"].startswith("Bild 1 von 2 wird gerechnet … Schritt ")
    assert vb["position"] == 1 and vb["ahead"] == [{"owner_name": "Anna", "project_title": "Titel p1aaaa",
                                                    "label": "2 Vorschläge"}]
    assert vb["eta_start_s"] > 0 and va["eta_done_s"] > vb["eta_done_s"] > 0  # round robin: B before A2
    time.sleep(1.1)
    va2 = s.get(a["id"])
    assert va2["eta_done_s"] < va["eta_done_s"]
    q = s.view("ben", "p1aaaa")
    assert q["busy"] and q["running"]["id"] == a["id"] and [j["id"] for j in q["jobs"]] == [a["id"], b["id"]]
    assert [j["id"] for j in q["my_jobs"]] == [b["id"]] and [j["id"] for j in q["project_jobs"]] == [a["id"]]
    assert q["pup"] == {"backend": "vllm", "state": "up"} and q["judge"] == {"available": True}
    assert q["wait_s"] > 0 and q["eta_total_s"] >= q["wait_s"] - 1
    for j in q["jobs"] + q["my_jobs"] + [q["running"]]:
        assert not {"caption", "results", "source", "params"} & set(j) and "dedupe" in j
    assert "GEHEIM" not in json.dumps(q)
    until(lambda: finished(s, a["id"]) and finished(s, b["id"]), timeout=20)
    q = s.view("anna", "p1aaaa")
    pj = q["project_jobs"][0]
    assert pj["stage_text"] == "✓ Fertig – 2 Vorschläge" and set(pj["results"][0]) == {"id", "width", "height", "seed", "judge"}
    assert "GEHEIM" not in json.dumps(q) and q["jobs"] == [] and not q["busy"]


def test_waiting_text_and_estimate(mk):
    s = mk(start=False)
    a = s.submit(P("A", steps=16, width=1536, height=1536), [Image.new("RGB", (1024, 1024))], owner="anna")
    b = s.submit(P("B", steps=16, width=1536, height=1536), [Image.new("RGB", (1024, 1024))], owner="anna")
    vb = s.get(b["id"])
    assert vb["position"] == 1 and vb["stage_text"].startswith("In der Warteschlange – Platz 2, Start in ca. 1 Min.")
    assert s.get(a["id"])["stage_text"] == "Startet gleich …"
    est = s.estimate(P("X", count=2, steps=16, width=1536, height=1536, upsample=True, boxes=ROWS), [(1024, 1024)])
    assert est == pytest.approx(0.2 + 2 * (2 + 16 * 3.294 + 6), abs=0.2)  # 1536 + 1 MP ref → 3.29 s/step
    assert s.status() == {"busy": True, "queue": 2, "phase": "idle", "wait_s": s.wait_s()}
    assert s.running(a["id"]) and not s.running("nope")


def test_stats_persisted(mk):
    s = mk(step_s=0.02)
    a = s.submit(P("A", steps=10), [], owner="anna")
    until(lambda: finished(s, a["id"]))
    until(lambda: (s.dir / "stats.json").exists())
    k = json.loads((s.dir / "stats.json").read_text())["k"]
    assert k < 1.0  # fake steps are faster than the model predicts
    assert mk(start=False).k == pytest.approx(k, abs=1e-3)


# ---------------- old contract, router ----------------

def test_studio_contract_and_router(mk):
    s = mk(step_s=0.02)
    app = FastAPI()

    @app.middleware("http")
    async def who(request: Request, call_next):
        o = request.headers.get("x-owner")
        if o:
            request.state.owner, request.state.owner_name = o, o.title()
        return await call_next(request)

    app.include_router(jobs.router)
    jobs.scheduler = s
    try:
        c = TestClient(app)
        buf = io.BytesIO()
        room().save(buf, "PNG")
        r = c.post("/api/jobs", data={"params": json.dumps(P("Studio", count=2, steps=6))},
                   files=[("refs", ("ref0.png", buf.getvalue(), "image/png"))], headers={"x-owner": "anna"})
        assert r.status_code == 200, r.text
        v = r.json()
        for k in ("id", "state", "stage", "step", "steps", "position", "started", "results", "judge_state",
                  "judge_stage", "retry_job", "error", "caption", "stage_text", "eta_done_s", "owner_name"):
            assert k in v
        assert v["state"] == "queued" and v["owner"] == "anna" and v["owner_name"] == "Anna"
        stages = set()
        until(lambda: stages.add(c.get(f"/api/jobs/{v['id']}").json()["stage"]) or finished(s, v["id"]))
        assert {"Image 1/2", "Image 2/2"} <= stages | {s.jobs[v["id"]]["stage"]}
        g = c.get(f"/api/jobs/{v['id']}").json()
        assert g["state"] == "done" and g["judge_state"] == "off" and len(g["results"]) == 2
        assert g["results"][0]["refs"] == [s.jobs[v["id"]]["_refs"][0]] and g["caption"] == "Studio"
        assert c.get("/api/queue", headers={"x-owner": "anna"}).json()["busy"] is False
        # cancel rights over HTTP
        s2 = s.submit(P("Lang", steps=200), [], owner="anna", owner_name="Anna")
        r = c.delete(f"/api/jobs/{s2['id']}", headers={"x-owner": "ben"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_owner"
        assert r.json()["detail"]["message"] == "Dieser Auftrag gehört Anna. Trotzdem abbrechen?"
        assert c.delete(f"/api/jobs/{s2['id']}?force=1", headers={"x-owner": "ben"}).json()["ok"] is True
        assert c.delete("/api/jobs/nope").status_code == 404
        # single image: "Generating"; after pruning: 404
        one = s.submit(P("Eins", steps=4), [], owner="anon")
        until(lambda: finished(s, one["id"]))
        assert s.jobs[one["id"]]["stage"] == "Generating"
        s.jobs[one["id"]]["_final_at"] = time.time() - jobs.KEEP_S - 1
        assert c.get(f"/api/jobs/{one['id']}").status_code == 404
        cap = c.post("/api/jobs", data={"params": json.dumps({"prompt": "Nur Text", "caption_only": True})}).json()
        until(lambda: finished(s, cap["id"]))
        assert c.get(f"/api/jobs/{cap['id']}").json()["caption"] == "[fake] Nur Text"
        assert c.post("/api/jobs", data={"params": json.dumps({"prompt": ""})}).status_code == 400
    finally:
        jobs.scheduler = None


def test_retry_caption_fake_matches_rule():
    ups = jobs.FakeUpsampler()
    cap = f"A room with a cabinet. {json.dumps(ROWS)}"
    out = ups.retry_caption(cap, "move it left")
    assert jobs.split_caption(out) == ("A room with a cabinet. Important corrections: move it left", ROWS)
    assert ups.retry_caption("No table here.", "hint") == "No table here. Important corrections: hint"


def test_import_graph():
    tree = ast.parse((Path(__file__).resolve().parent.parent / "jobs.py").read_text())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"server", "engine", "workshop", "projects", "torch", "diffusers", "upsampler"}
    assert {"imaging", "judge"} <= names
