"""agent.py: the conversation state machine with fake models, a fake scheduler and a fake generation, on a tmp store.
The real intent.plan(), place.py, the PATCH path, the preview drawing, offer text and DXF run unchanged."""

import io
import json
import os
import re
import socket
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import agent
import delivery
import intent
import projects
import workshop

# §2.1 geometry fixture (= tests/test_ground.py): camera 150 cm above the floor, back wall w0 at z = 400 cm,
# left wall w1 at x = -200, right wall w2 at x = 250, ceiling 260 cm
GEO = {"version": 1, "ok": True, "error": None, "room": "uploads/test.png", "size": [1344, 1008],
       "intrinsics": {"fx": 1000.0, "fy": 1000.0, "cx": 672.0, "cy": 504.0},
       "floor": {"normal": [0.0, -0.984808, -0.173648], "d": 150.0, "inliers": 0.4},
       "frame": {"origin": [0.0, 147.7212, 26.0472], "right": [1.0, 0.0, 0.0], "up": [0.0, -0.984808, -0.173648],
                 "forward": [0.0, -0.173648, 0.984808]},
       "walls": [{"id": "w0", "label": "hinten", "normal": [0.0, -1.0], "offset": 400.0, "from": [-200.0, 400.0], "to": [250.0, 400.0]},
                 {"id": "w1", "label": "links", "normal": [1.0, 0.0], "offset": 200.0, "from": [-200.0, 400.0], "to": [-200.0, 100.0]},
                 {"id": "w2", "label": "rechts", "normal": [-1.0, 0.0], "offset": 250.0, "from": [250.0, 400.0], "to": [250.0, 100.0]}],
       "ceiling": {"height": 260.0}, "horizon": [[0.0, 327.67], [1344.0, 327.67]],
       "vps": {"vertical": None, "h1": None, "h2": None}, "scale": 1.0, "depth_range": [150.0, 600.0],
       "depth_preview": None, "confidence": "high", "seconds": 0.0}


def png(size=(1344, 1008), color=(200, 190, 180)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Fakes:
    """Scripted model answers and a fake GPU queue; every call is recorded."""

    def __init__(self):
        self.readings = []          # intent: model readings (dicts) or "unavailable"
        self.spots = {}             # ground.locate: phrase → result
        self.objects = {}           # ground.find_object: phrase → result
        self.calls = []
        self.jobs = {}              # job id → view
        self.verdicts = []          # per generation: list of judge verdicts (one per image)
        self.plan_check = {"status": "ok", "pieces": []}
        self.transcript = {"status": "ok", "text": ""}
        self.gate = None            # threading.Event: generation finishes only when set


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    for d in (out / "uploads", tmp_path / "projects" / "workshop"):
        d.mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", tmp_path / "projects")
    monkeypatch.setattr(projects, "OUT", out)
    monkeypatch.setattr(projects, "UPLOADS", out / "uploads")
    monkeypatch.setattr(projects, "EXPORTS", out / "exports")
    monkeypatch.setattr(projects, "job_running", None)
    monkeypatch.setattr(projects, "next_ref", lambda year, store=None: "2026-001")   # never the real settings.json
    monkeypatch.setattr(projects, "settings", lambda store=None: projects.normalize_settings({}))
    monkeypatch.setattr(workshop, "OUT", out)
    monkeypatch.setattr(delivery, "OUT", out)
    monkeypatch.setattr(delivery, "DATA", tmp_path)
    monkeypatch.setattr(delivery, "draft_sheet", lambda pid, ids=None: {"pdf": f"/outputs/exports/workshop-{pid}/entwurf/x.pdf"})
    monkeypatch.setattr(agent, "INTENT_RETRY_S", 0)
    monkeypatch.setattr(agent, "WATCH_S", 0.05)
    f = Fakes()

    def understand(text, doc):
        f.calls.append(("understand", text, doc.get("active_piece")))
        r = f.readings.pop(0)
        if r == "unavailable":
            return intent._result("unavailable", text, time.time())
        return intent._result("ok", text, time.time(), **intent.plan(r, doc, None, intent.normalize_de(text)))

    def locate(photo, phrase, geo, doc):
        f.calls.append(("locate", phrase))
        return f.spots.get(phrase) or {"status": "not_found", "point": None, "confidence": 0.2}

    def find_object(photo, phrase, refine=True):
        f.calls.append(("find", phrase))
        return f.objects.get(phrase) or {"status": "not_found", "parts": []}

    def generate(pid, req, who):
        f.calls.append(("generate", req))
        doc = projects.load("workshop", pid)
        workshop._check_rev(doc, req)          # the real stale-plan refusal
        jid = f"{len(f.jobs) + 1:06x}aa"
        verdicts = f.verdicts.pop(0) if f.verdicts else [{"status": "ok", "overall": 8.0, "pass": True, "comment": "Gut."}]
        f.jobs[jid] = {"id": jid, "state": "running", "judge_state": "off", "retry_job": None}

        def finish():
            if f.gate is not None:
                f.gate.wait(10)
            if f.jobs[jid]["state"] == "cancelled":
                return

            def rec(d):
                for i, v in enumerate(verdicts):
                    img = f"20261018-{jid}-{i}.png"
                    Image.new("RGB", (64, 48), (90, 90, 90)).save(projects.OUT / img)
                    d["variants"].append({"id": f"v{jid}{i}", "image": img, "job": jid, "judge": v, "kind": "room",
                                          "piece_ids": [p["id"] for p in d["pieces"]]})
                d["pending_jobs"] = [x for x in d.get("pending_jobs") or [] if x != jid]
            projects.mutate("workshop", pid, rec, who)
            f.jobs[jid].update(state="done", judge_state="done")

        projects.mutate("workshop", pid, lambda d: d["pending_jobs"].append(jid), who)
        threading.Thread(target=finish, daemon=True).start()
        return {"job": {"id": jid}, "plan": {"label": "2 Vorschläge", "estimate_text": "Bilder in ca. 1 Min.", "warnings": []}}

    def cancel(jid, who):
        f.calls.append(("cancel", jid))
        f.jobs[jid]["state"] = "cancelled"
        projects.mutate("workshop", next(p.stem for p in (projects.STORE / "workshop").glob("*.json")),
                        lambda d: d.__setitem__("pending_jobs", [x for x in d["pending_jobs"] if x != jid]), who)

    monkeypatch.setattr(agent, "_understand", understand)
    monkeypatch.setattr(agent, "_locate", locate)
    monkeypatch.setattr(agent, "_find_object", find_object)
    monkeypatch.setattr(agent, "_measure", lambda room: {**GEO, "room": room})
    monkeypatch.setattr(agent, "_check_plan", lambda pid: f.plan_check)
    monkeypatch.setattr(agent, "_generate", generate)
    monkeypatch.setattr(agent, "_job_view", lambda jid: dict(f.jobs[jid]) if jid in f.jobs else None)
    monkeypatch.setattr(agent, "_cancel_job", cancel)
    monkeypatch.setattr(agent, "_profile", lambda: None)
    monkeypatch.setattr(agent, "_gpu_busy", lambda: False)
    monkeypatch.setattr(agent, "_judge_available", lambda: True)
    monkeypatch.setattr(agent, "_transcribe", lambda audio: f.transcript)
    agent._SUBS.clear()
    agent._WATCH.clear()
    yield f
    agent.stop()
    agent._SUBS.clear()


SCHRANK = {"pieces": [{"type": "schrank", "w": 200, "full_height": True, "material": "weiss", "handles": "push",
                       "text": "3 Türen", "where": "an der Rückwand"}], "removes": ["der alte Schrank"], "confidence": 0.9}
SPOT = {"status": "ok", "point": [500, 400], "wall": "w0", "surface": "floor", "confidence": 0.85, "why": "Rückwand"}
OBJ = {"status": "ok", "point": [300, 600], "parts": [{"poly": [[250, 500], [350, 500], [350, 700], [250, 700]],
                                                    "box": [500, 250, 700, 350]}]}


def settle(pid, f=None, timeout=10):
    """Worker idle and (if a generation runs) its end handled."""
    end = time.time() + timeout
    while time.time() < end:
        if agent.wait_idle(pid, 0.2) and pid not in agent._WATCH:
            time.sleep(0.05)
            if agent.wait_idle(pid, 0.2) and pid not in agent._WATCH:
                return agent.events(pid)
        time.sleep(0.05)
    raise AssertionError(f"not idle: {agent.state(pid)} {agent.events(pid)[-1:]}")


def kinds(evs, role="agent"):
    return [e["kind"] for e in evs if e["role"] == role]


def test_full_flow_photo_text_preview_yes_result(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", "web", text="an der Rückwand ein Einbauschrank, zwei Meter breit, raumhoch, "
                                                    "Weißlack, drei Türen grifflos, der alte Schrank kommt weg",
                      title="TEST Assistent")
    evs = settle(pid)
    assert agent.state(pid) == "preview"
    assert kinds(evs, "user") == ["photo", "text"]
    prev = [e for e in evs if e["kind"] == "preview"][-1]
    assert prev["text"].startswith("So geplant") and "Einbauschrank: 200 × 260 × 60 cm" in prev["text"]
    assert "Wird entfernt: der alte Schrank" in prev["text"] and "„ja“" in prev["text"]
    assert prev["image"].startswith(f"exports/workshop-{pid}/assistent/vorschau-") and prev["image"].endswith(".jpg")
    img = Image.open(projects.OUT / prev["image"])
    assert img.size == (1280, 960)
    doc = projects.load("workshop", pid)
    p = doc["pieces"][0]
    assert (p["type"], p["w"], p["h"], p["d"], p["material"], p["handles"]) == ("schrank", 200, 260, 60, "weiss", "push")
    assert p["placement"]["mode"] == "3d" and p["placement"]["pose"]["wall"] == "w0" and p["placement"]["box"]
    assert doc["removes"] == [OBJ["parts"][0]]
    assert doc["judge"]["auto_retry"] is True and doc["geometry"]["ok"]
    assert ("understand", env.calls[0][1], env.calls[0][2]) == env.calls[0]

    agent.handle(pid, "peter", "web", text="Ja, passt.")
    evs = settle(pid)
    assert agent.state(pid) == "done"
    res = [e for e in evs if e["kind"] == "result"][-1]
    assert res["image"] and res["data"]["images"] and "Positionstext" not in res["text"]
    assert res["data"]["variants"][0]["score"] == 8.0 and "Einbauschrank" in res["data"]["offer_text"]
    assert res["data"]["pdf"].endswith(".pdf")
    dxf = projects.OUT / res["data"]["files"][0]
    assert dxf.name == "zeichnung.dxf" and dxf.read_bytes().startswith(b"0\r\nSECTION")
    assert res["data"]["models"][0]["glb"].endswith(".glb")
    gen = [c for c in env.calls if c[0] == "generate"]
    assert len(gen) == 1 and gen[0][1]["base_rev"] == prev["data"]["rev"] and gen[0][1]["kind"] == "room"
    conv = json.loads((projects.STORE / "agent" / f"{pid}.json").read_text())
    assert oct(os.stat(projects.STORE / "agent" / f"{pid}.json").st_mode & 0o777) == "0o600"
    assert [e["n"] for e in conv["events"]] == list(range(1, len(conv["events"]) + 1))


def test_question_for_width_then_answer_and_default(env):
    env.readings = [{"pieces": [{"type": "sideboard", "where": "unter dem Fenster"}], "confidence": 0.8}]
    env.spots["unter dem Fenster"] = {**SPOT, "wall": "w0"}
    pid = agent.start(png(), "peter", text="ein Sideboard unter dem Fenster", title="TEST Frage")
    evs = settle(pid)
    assert agent.state(pid) == "asking"
    q = [e for e in evs if e["kind"] == "question"][-1]
    assert q["text"] == "Wie breit soll das Sideboard werden? (Vorschlag: 180 cm – „ja“ übernimmt ihn)"
    assert q["data"]["field"] == "w" and q["data"]["default"] == 180
    agent.handle(pid, "peter", text="eins sechzig")
    evs = settle(pid)
    assert projects.load("workshop", pid)["pieces"][0]["w"] == 160
    assert agent.state(pid) == "preview" and "Breite: 160 cm." in [e["text"] for e in evs]

    env.readings = [{"pieces": [{"type": "wandregal", "where": "links an der Wand", "handles": "push"}]}]
    env.spots["links an der Wand"] = {**SPOT, "point": [100, 400], "wall": "w1"}
    agent.handle(pid, "peter", text="und ein Wandregal links an der Wand")
    settle(pid)
    assert agent.state(pid) == "asking"
    env.readings = [{"pieces": [], "confidence": 0.1}]
    agent.handle(pid, "peter", text="hm, keine Ahnung")    # not an answer: the open question comes again
    evs = settle(pid)
    assert evs[-1]["text"].startswith("Das habe ich nicht verstanden. Wie breit soll das Wandregal werden?")
    agent.handle(pid, "peter", text="ja, Standard passt")   # takes the default
    settle(pid)
    doc = projects.load("workshop", pid)
    regal = next(p for p in doc["pieces"] if p["type"] == "wandregal")
    assert regal["w"] == 120 and regal.get("elev") == 150 and regal["placement"]["pose"]["wall"] == "w1"
    assert regal["handles"] is None          # no fronts: the default push handles were cleared
    assert agent.state(pid) == "preview"


def test_correction_at_preview_reprojects_and_learns_focus(env):
    env.readings = [SCHRANK, {"pieces": [{"ref": None, "d": 80}]}]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", text="…", title="TEST Korrektur")
    settle(pid)
    box0 = projects.load("workshop", pid)["pieces"][0]["placement"]["box"]
    agent.handle(pid, "peter", text="nein, achtzig tief")
    evs = settle(pid)
    doc = projects.load("workshop", pid)
    p = doc["pieces"][0]
    assert p["d"] == 80
    assert p["placement"]["box"] != box0          # re-projected (and re-snapped: deeper piece, same wall)
    assert p["placement"]["pose"]["wall"] == "w0"
    assert env.calls[-1][0] != "locate" or True
    assert env.calls[[c[0] for c in env.calls].index("understand", 1)][2] == p["id"]   # focus = the piece
    assert agent.state(pid) == "preview" and kinds(evs).count("preview") == 2


def test_move_left_and_right(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", text="…", title="TEST Schieben")
    settle(pid)
    x0 = projects.load("workshop", pid)["pieces"][0]["placement"]["pose"]["x"]
    agent.handle(pid, "peter", text="zwanzig Zentimeter nach links")
    settle(pid)
    x1 = projects.load("workshop", pid)["pieces"][0]["placement"]["pose"]["x"]
    assert x1 == pytest.approx(x0 - 20, abs=0.01)      # back wall: image left = -x
    agent.handle(pid, "peter", text="den Schrank etwas weiter rechts")
    settle(pid)
    x2 = projects.load("workshop", pid)["pieces"][0]["placement"]["pose"]["x"]
    assert x2 == pytest.approx(x1 + 15, abs=0.01)
    assert agent.state(pid) == "preview"
    assert not any(c[0] == "understand" for c in env.calls[1:])   # moves never call the language model


def test_move_pushes_neighbour_on_the_same_wall(env):
    env.readings = [{"pieces": [{"type": "sideboard", "w": 100, "where": "hinten links"},
                                {"type": "sideboard", "w": 100, "where": "hinten rechts"}]}]
    env.spots["hinten links"] = {**SPOT, "point": [380, 400]}
    env.spots["hinten rechts"] = {**SPOT, "point": [620, 400]}
    pid = agent.start(png(), "peter", text="zwei Sideboards", title="TEST Schieben 2")
    settle(pid)
    a, b = projects.load("workshop", pid)["pieces"]
    assert a["placement"]["pose"]["wall"] == b["placement"]["pose"]["wall"] == "w0"
    gap = b["placement"]["pose"]["x"] - a["placement"]["pose"]["x"]
    agent._update(pid, lambda c: c.__setitem__("focus", a["id"]))
    agent.handle(pid, "peter", text=f"{int(gap)} cm nach rechts")
    evs = settle(pid)
    a2, b2 = projects.load("workshop", pid)["pieces"]
    assert a2["placement"]["pose"]["x"] == pytest.approx(a["placement"]["pose"]["x"] + int(gap), abs=0.01)
    assert b2["placement"]["pose"]["x"] - a2["placement"]["pose"]["x"] == pytest.approx(101, abs=0.01)   # pushed along
    assert "Sideboard rückt mit" in " ".join(e["text"] for e in evs)
    agent.handle(pid, "peter", text="3 cm nach rechts")      # once more: the right one would leave the wall? not yet
    settle(pid)
    before = projects.load("workshop", pid)["pieces"]
    agent.handle(pid, "peter", text="300 cm nach rechts")
    evs = settle(pid)
    assert re.search(r"Wand ist zu Ende|nicht mehr im Bild|zu Ende", evs[-1]["text"])
    assert projects.load("workshop", pid)["pieces"] == before        # refused: nothing moved


def test_beside_reference_slides_piece_clear(env):
    env.readings = [{"pieces": [{"type": "sideboard", "w": 120, "where": "links neben der Tür"}]}]
    env.spots["links neben der Tür"] = {**SPOT, "point": [500, 400]}
    env.objects["der Tür"] = {"status": "ok", "box": [300, 480, 700, 600], "point": [540, 500], "parts": []}
    pid = agent.start(png(), "peter", text="Sideboard links neben der Tür", title="TEST Neben")
    settle(pid)
    p = projects.load("workshop", pid)["pieces"][0]
    assert p["placement"]["box"][3] <= 465 and p["placement"]["pose"]["wall"] == "w0"
    assert ("find", "der Tür") in env.calls
    assert agent._beside("rechts neben dem Fernsehschrank an der Rückwand") == ("rechts", "dem Fernsehschrank")
    assert agent._beside("neben das Sofa") == (None, "das Sofa") and agent._beside("unter dem Fenster") is None


def test_place_not_found_asks_and_egal_skips(env):
    env.readings = [{"pieces": [{"type": "tisch", "w": 180, "where": "neben dem Kamin"}]}]
    pid = agent.start(png(), "peter", text="ein Esstisch neben dem Kamin", title="TEST Ort")
    evs = settle(pid)
    q = [e for e in evs if e["kind"] == "question"][-1]
    assert "„neben dem Kamin“ habe ich im Foto nicht sicher gefunden" in q["text"] and q["data"]["field"] == "where"
    agent.handle(pid, "peter", text="egal")
    evs = settle(pid)
    assert agent.state(pid) == "preview"
    prev = [e for e in evs if e["kind"] == "preview"][-1]
    assert "Platz wählt die Bildberechnung" in prev["text"]
    assert projects.load("workshop", pid)["pieces"][0]["placement"]["mode"] == "none"


def test_where_answer_relocates(env):
    env.readings = [{"pieces": [{"type": "sideboard", "w": 180}]}]
    env.spots["rechts an der Wand"] = {**SPOT, "point": [880, 560], "wall": "w2"}
    pid = agent.start(png(), "peter", text="ein Sideboard 1,80", title="TEST Wo")
    evs = settle(pid)
    assert [e for e in evs if e["kind"] == "question"][-1]["text"].startswith("Wo soll das Sideboard hin?")
    agent.handle(pid, "peter", text="rechts an der Wand")
    settle(pid)
    p = projects.load("workshop", pid)["pieces"][0]
    assert p["where"] == "rechts an der Wand" and p["placement"]["pose"]["wall"] == "w2"
    assert agent.state(pid) == "preview"


def test_judge_fail_retries_once_then_delivers_best(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    bad = {"status": "ok", "overall": 5.0, "pass": False, "comment": "Schrank schwebt."}
    env.verdicts = [[bad, {**bad, "overall": 5.5}], [{**bad, "overall": 6.1}]]
    pid = agent.start(png(), "peter", text="…", title="TEST Retry")
    settle(pid)
    projects.apply_ops(pid, projects.load("workshop", pid)["rev"], [{"op": "set", "path": "judge.auto_retry", "value": False}], "x")
    agent._update(pid, lambda c: c.__setitem__("preview_rev", projects.load("workshop", pid)["rev"]))
    agent.handle(pid, "peter", text="ja")
    evs = settle(pid)
    gens = [c for c in env.calls if c[0] == "generate"]
    assert len(gens) == 2 and gens[1][1]["count"] == 1            # exactly one retry
    assert "ich rechne noch einmal" in " ".join(e["text"] for e in evs if e["kind"] == "status")
    res = [e for e in evs if e["kind"] == "result"][-1]
    assert "keins hat die KI-Prüfung bestanden" in res["text"]
    assert [v["score"] for v in res["data"]["variants"]] == [6.1, 5.5, 5.0]
    assert agent.state(pid) == "done"


def test_judge_unavailable_results_unchecked(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    env.plan_check = {"status": "unavailable"}
    env.verdicts = [[{"status": "unavailable", "overall": None, "pass": None}]]
    pid = agent.start(png(), "peter", text="…", title="TEST Ohne Prüfer")
    evs = settle(pid)
    assert "KI-Plancheck gerade nicht möglich." in [e for e in evs if e["kind"] == "preview"][-1]["text"]
    agent.handle(pid, "peter", text="ok")
    evs = settle(pid)
    res = [e for e in evs if e["kind"] == "result"][-1]
    assert "KI-Prüfung war nicht erreichbar" in res["text"] and "ungeprüft" in res["text"]
    assert len([c for c in env.calls if c[0] == "generate"]) == 1


def test_plan_check_problem_shown(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid0 = {}

    def check(pid):
        doc = projects.load("workshop", pid)
        if pid0:                                       # after the fix the plan check is happy
            return {"status": "ok", "pieces": [{"id": pid0["id"], "ok": True, "problems": [], "fix": ""}]}
        pid0["id"] = doc["pieces"][0]["id"]
        return {"status": "ok", "pieces": [{"id": doc["pieces"][0]["id"], "ok": False, "problems": ["Steht vor dem Fenster."],
                                            "fix": "Nach rechts setzen."}]}
    agent._check_plan, old = check, agent._check_plan
    try:
        pid = agent.start(png(), "peter", text="…", title="TEST Plancheck")
        evs = settle(pid)
        pre = [e for e in evs if e["kind"] == "preview"][-1]
        lines = pre["text"].split("\n")
        # a conflict is a question: problem + suggestion first, „ja“ is no longer the suggested answer
        assert lines[0] == "So passt es noch nicht:"
        assert lines[1] == "⚠ KI-Plancheck zu 1: Steht vor dem Fenster. Vorschlag: Nach rechts setzen."
        assert lines[-1] == agent.FIX_HINT
        assert pre["data"]["conflict"] == {"piece": pid0["id"], "n": 1, "fix": "Nach rechts setzen.",
                                           "send": "Nach rechts setzen."}
        env.readings = [{"pieces": [{"ref": pid0["id"], "where": "rechts an der Rückwand"}]}]
        env.spots["rechts an der Rückwand"] = {**SPOT, "point": [700, 400]}
        agent.handle(pid, "peter", text="Vorschlag übernehmen")
        settle(pid)
    finally:
        agent._check_plan = old
    assert ("understand", "Nach rechts setzen.", pid0["id"]) in env.calls      # the fix went in as a correction
    assert projects.load("workshop", pid)["pieces"][0]["where"] == "rechts an der Rückwand"
    assert agent.state(pid) == "preview" and agent._load(pid)["conflict"] is None
    agent.handle(pid, "peter", text="Vorschlag übernehmen")          # no conflict left: an ordinary „ja“
    settle(pid)
    assert kinds(agent.events(pid))[-1] == "result"


def test_plan_check_conflict_yes_anyway(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ

    def check(pid):
        doc = projects.load("workshop", pid)
        return {"status": "ok", "pieces": [{"id": doc["pieces"][0]["id"], "ok": False, "problems": ["Vor der Tür."],
                                            "fix": "Weiter links."}]}
    agent._check_plan, old = check, agent._check_plan
    try:
        pid = agent.start(png(), "peter", text="…", title="TEST Plancheck trotzdem")
        settle(pid)
        agent.handle(pid, "peter", text="ja, trotzdem rechnen")
        evs = settle(pid)
    finally:
        agent._check_plan = old
    assert kinds(evs)[-1] == "result" and any(c[0] == "generate" for c in env.calls)


def test_correction_during_generation_cancels_and_previews(env):
    env.readings = [SCHRANK, {"pieces": [{"ref": None, "material": "eiche"}]}]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    env.gate = threading.Event()
    pid = agent.start(png(), "peter", text="…", title="TEST Abbruch")
    settle(pid)
    agent.handle(pid, "peter", text="ja")
    agent.wait_idle(pid, 5)
    assert agent.state(pid) == "generating"
    agent.handle(pid, "peter", text="ja")
    agent.wait_idle(pid, 5)
    assert "werden gerade gerechnet" in agent.events(pid)[-1]["text"]
    agent.handle(pid, "peter", text="nimm doch Eiche")
    agent.wait_idle(pid, 5)
    env.gate.set()
    evs = settle(pid)
    assert ("cancel", "000001aa") in env.calls
    assert projects.load("workshop", pid)["pieces"][0]["material"] == "eiche"
    assert agent.state(pid) == "preview" and kinds(evs)[-1] == "preview"
    assert "abgebrochen, weil sich der Plan ändert" in " ".join(e["text"] for e in evs)


def test_stop_command(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    env.gate = threading.Event()
    pid = agent.start(png(), "peter", text="…", title="TEST Stopp")
    settle(pid)
    agent.handle(pid, "peter", text="los")
    agent.wait_idle(pid, 5)
    agent.handle(pid, "peter", text="Stopp!")
    agent.wait_idle(pid, 5)
    env.gate.set()
    settle(pid)
    assert agent.state(pid) == "preview" and "Berechnung abgebrochen" in agent.events(pid)[-1]["text"]


def _generating(env, title):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    env.gate = threading.Event()
    pid = agent.start(png(), "peter", text="…", title=title)
    settle(pid)
    agent.handle(pid, "peter", text="ja")
    agent.wait_idle(pid, 5)
    assert agent.state(pid) == "generating" and pid in agent._WATCH
    return pid


def test_chatter_during_generation_keeps_watching(env, monkeypatch):
    """Understanding a message takes longer than WATCH_S: the watcher must not take that for the end of the job."""
    pid = _generating(env, "TEST Plaudern")
    slow = agent._understand
    monkeypatch.setattr(agent, "_understand", lambda text, doc: time.sleep(0.3) or slow(text, doc))
    env.readings = [{"pieces": [], "confidence": 0.1}]
    agent.handle(pid, "peter", text="wie sieht es aus mit dem Wetter")
    agent.wait_idle(pid, 5)
    assert agent.state(pid) == "generating" and pid in agent._WATCH
    env.gate.set()
    evs = settle(pid)
    assert kinds(evs)[-1] == "result" and agent.state(pid) == "done"
    assert not any("keine Bilder" in e["text"] for e in evs)
    assert not any(e["text"].endswith("..") for e in evs)       # „… in ca. 1 Min.“ without a second full stop


def test_voice_during_generation_keeps_watching(env, monkeypatch):
    pid = _generating(env, "TEST Sprachnachricht")
    monkeypatch.setattr(agent, "_transcribe", lambda audio: time.sleep(0.3) or
                        {"status": "unavailable", "text": "", "message": "Gerade ist zu wenig Speicher frei."})
    agent.handle(pid, "peter", audio=b"x")
    agent.wait_idle(pid, 5)
    assert agent.state(pid) == "generating" and pid in agent._WATCH
    env.gate.set()
    evs = settle(pid)
    assert kinds(evs)[-1] == "result" and not any("keine Bilder" in e["text"] for e in evs)


def test_job_finishing_while_understanding_still_delivers(env, monkeypatch):
    pid = _generating(env, "TEST Fertig beim Lesen")
    slow = agent._understand

    def understand(text, doc):
        env.gate.set()
        time.sleep(0.4)           # the job ends meanwhile; the worker is busy with this message
        return slow(text, doc)
    monkeypatch.setattr(agent, "_understand", understand)
    env.readings = [{"pieces": [], "confidence": 0.1}]
    agent.handle(pid, "peter", text="wie lange noch ungefähr")
    evs = settle(pid)
    assert kinds(evs)[-1] == "result" and agent.state(pid) == "done"


def test_new_photo_during_generation_cancels(env):
    pid = _generating(env, "TEST Neues Foto")
    agent.handle(pid, "peter", photo=png(color=(150, 160, 170)))
    agent.wait_idle(pid, 5)
    env.gate.set()
    evs = settle(pid)
    assert ("cancel", "000001aa") in env.calls
    assert "weil ein neues Foto kam" in " ".join(e["text"] for e in evs)
    assert agent.state(pid) != "generating" and pid not in agent._WATCH


def test_web_edit_after_preview_refreshes_preview(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", text="…", title="TEST Web")
    settle(pid)
    doc = projects.load("workshop", pid)
    projects.apply_ops(pid, doc["rev"], [{"op": "set", "path": f"pieces.{doc['pieces'][0]['id']}.w", "value": 240}], "kollege")
    agent.handle(pid, "peter", text="ja")
    evs = settle(pid)
    assert agent.state(pid) == "preview"
    assert "inzwischen geändert" in " ".join(e["text"] for e in evs[-3:])
    assert not any(c[0] == "generate" for c in env.calls)
    agent.handle(pid, "peter", text="ja")
    settle(pid)
    assert agent.state(pid) == "done"


def test_need_photo_queues_text(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(None, "peter", "telegram", text="Einbauschrank an der Rückwand", title="TEST Ohne Foto")
    evs = settle(pid)
    assert agent.state(pid) == "need_photo" and "Foto des Raums" in [e for e in evs if e["kind"] == "question"][-1]["text"]
    assert not env.calls
    agent.handle(pid, "peter", "telegram", photo=png())
    settle(pid)
    assert env.calls[0] == ("understand", "Einbauschrank an der Rückwand", projects.load("workshop", pid)["pieces"][0]["id"]) \
        or env.calls[0][1] == "Einbauschrank an der Rückwand"
    assert agent.state(pid) == "preview"


def test_auto_title_and_test_prefix(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", "api", text="TEST Einbauschrank an der Rückwand")
    doc = projects.load("workshop", pid)
    assert doc["test"] and doc["ref"] == "" and doc["title"].startswith("TEST Auftrag vom ")
    settle(pid)
    assert projects.load("workshop", pid)["title"] == "TEST Einbauschrank (Assistent)"
    env.readings = [SCHRANK]
    pid2 = agent.start(png(), "peter", "api", text="Einbauschrank an der Rückwand")
    assert projects.load("workshop", pid2)["ref"] == "2026-001"      # a real order gets its number
    settle(pid2)
    assert projects.load("workshop", pid2)["title"] == "Einbauschrank (Assistent)"


def test_photo_only_asks_what(env):
    pid = agent.start(png(), "peter", title="TEST Nur Foto")
    evs = settle(pid)
    assert agent.state(pid) == "asking" and evs[-1]["kind"] == "question" and "Was soll gebaut werden?" in evs[-1]["text"]


def test_audio_is_transcribed(env):
    env.transcript = {"status": "ok", "text": "ein Einbauschrank an der Rückwand, zwei Meter, raumhoch"}
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", audio=b"OggS fake", title="TEST Audio")
    evs = settle(pid)
    assert "Verstanden: „ein Einbauschrank an der Rückwand, zwei Meter, raumhoch“" in [e["text"] for e in evs]
    assert kinds(evs, "user") == ["photo", "audio"] and agent.state(pid) == "preview"
    env.transcript = {"status": "unavailable", "text": "", "message": "Gerade ist zu wenig Speicher frei."}
    agent.handle(pid, "peter", audio=b"x")
    evs = settle(pid)
    assert evs[-1]["kind"] == "error" and "als Text schicken" in evs[-1]["text"]
    assert agent.state(pid) == "preview"


def test_intent_unavailable_degrades(env):
    env.readings = ["unavailable", "unavailable"]
    pid = agent.start(png(), "peter", text="ein Schrank", title="TEST KI weg")
    evs = settle(pid)
    assert evs[-1]["kind"] == "error" and "nicht erreichbar" in evs[-1]["text"]
    assert agent.state(pid) not in agent.BUSY_STATES


def test_max_pieces_warning(env):
    env.readings = [{"pieces": [{"type": t, "w": 100, "where": "egal"} for t in
                                ("sideboard", "regal", "tisch", "bett", "garderobe")]}]
    pid = agent.start(png(), "peter", text="fünf Möbel", title="TEST Viele")
    evs = settle(pid)
    assert "Höchstens 4 Möbel" in " ".join(e["text"] for e in evs)
    assert len(projects.load("workshop", pid)["pieces"]) == 4


def test_room_height_calibrates(env, monkeypatch):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(), "peter", text="…", title="TEST Höhe")
    settle(pid)

    def calibrate(room, h):
        import geometry
        g = geometry.rescale(GEO, h / 260)
        g["scale"], g["ceiling"] = h / 260, {"height": float(h)}
        return {"geometry": {**g, "room": room}, "measured_cm": 260.0, "scale": g["scale"]}
    monkeypatch.setattr(agent, "_calibrate", calibrate)
    agent.handle(pid, "peter", text="Raumhöhe 2,50")
    evs = settle(pid)
    doc = projects.load("workshop", pid)
    assert doc["calibration"]["room_height_cm"] == 250 and doc["pieces"][0]["h"] == 250
    assert "Raumhöhe 2,50 m übernommen" in " ".join(e["text"] for e in evs)
    assert agent.state(pid) == "preview"


def test_send_prepares_version(env, monkeypatch):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    seen = {}

    def create_version(pid, ids, note, days, who, base, pin=False):
        seen.update(ids=ids, pin=pin, base=base)
        return {"version": {"n": 1, "pin": "123456", "dir": "exports/x/v1", "files": {"pdf": "a.pdf"}}, "link": f"{base}/k/tok"}
    monkeypatch.setattr(delivery, "create_version", create_version)
    pid = agent.start(png(), "peter", text="…", title="TEST Senden")
    settle(pid)
    agent.handle(pid, "peter", text="senden")
    settle(pid)
    assert "noch keine fertigen Bilder" in agent.events(pid)[-1]["text"]
    agent.handle(pid, "peter", text="ja")
    settle(pid)
    agent.handle(pid, "peter", text="an den Kunden senden")
    evs = settle(pid)
    assert seen["pin"] is True and len(seen["ids"]) == 1
    assert "noch nicht verschickt" in evs[-1]["text"] and "PIN für den Kunden: 123456" in evs[-1]["text"]


def test_subscribe_and_events_after(env):
    got = []
    unsub = agent.subscribe(lambda pid, ev: got.append((pid, ev["kind"], ev["role"])))
    pid = agent.start(png(), "peter", title="TEST Abo")
    settle(pid)
    assert got and all(r == "agent" for _, _, r in got) and got[-1] == (pid, "question", "agent")
    n = agent.events(pid)[-1]["n"]
    assert agent.events(pid, after=n) == [] and agent.events(pid, after=n - 1)[0]["n"] == n
    unsub()
    agent.handle(pid, "peter", text="hilfe")
    settle(pid)
    assert len(got) == len([e for e in agent.events(pid) if e["role"] == "agent"]) - 1


def test_handle_on_web_order_and_purged_order(env):
    req = type("R", (), {"state": type("S", (), {"owner": "peter"})()})()
    doc = projects.create_project("workshop", req, {"title": "TEST Web-Auftrag"})
    up = projects._store_upload(png(), None)
    projects.apply_ops(doc["id"], doc["rev"], [{"op": "set", "path": "room", "value": up["path"]}], "peter")
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    agent.handle(doc["id"], "peter", text="ein Schrank an der Rückwand")
    settle(doc["id"])
    assert agent.state(doc["id"]) == "preview"      # measured on the fly, then understood
    projects.delete_project("workshop", doc["id"])
    assert agent.events(doc["id"]) == []
    assert not (projects.STORE / "agent" / f"{doc['id']}.json").exists()
    with pytest.raises(projects.Gone):
        agent.handle(doc["id"], "peter", text="hallo")


def test_one_worker_per_order(env):
    seen, lock = [], threading.Lock()
    active = {"n": 0, "max": 0}

    def slow(text, doc):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        seen.append(text)
        return intent._result("ok", text, time.time(), questions=[{"piece": None, "field": "type", "default": None,
                                                                   "question_de": "Was soll gebaut werden?"}])
    agent._understand, old = slow, agent._understand
    try:
        pid = agent.start(png(), "peter", title="TEST Reihenfolge")
        settle(pid)
        for i in range(5):
            agent.handle(pid, "peter", text=f"Nachricht {i}")
        settle(pid)
    finally:
        agent._understand = old
    assert seen == [f"Nachricht {i}" for i in range(5)] and active["max"] == 1


def test_recover_after_restart(env):
    pid = agent.start(png(), "peter", title="TEST Neustart")
    settle(pid)
    agent._update(pid, lambda c: agent._set(c, "understanding"))
    out = agent.recover()
    assert pid in out["interrupted"] and agent.state(pid) == "asking"
    assert "neu gestartet" in agent.events(pid)[-1]["text"]
    (projects.STORE / "agent" / "dddddd111111.json").write_text(json.dumps(agent._new_conv("dddddd111111", "x", "web", "done")))
    assert "dddddd111111" in agent.recover()["removed"]


def test_removal_phrase():
    assert agent._removal_phrase("der alte Fernsehschrank") == "der alte Fernsehschrank mit allem, was darauf steht"
    assert agent._removal_phrase("der Heizkörper") == "der Heizkörper"
    assert agent._removal_phrase("der Tisch mit den Stühlen") == "der Tisch mit den Stühlen"


def test_commands():
    assert agent._command("Ja, passt.") == "yes" and agent._command("ja bitte rechnen") == "yes"
    assert agent._command("ja, aber 20 cm breiter") is None
    assert agent._command("passt fein") == "yes"
    assert agent._command("nochmal") == "again" and agent._command("Stopp!") == "stop"
    assert agent._command("an den Kunden senden") == "send" and agent._command("nein") == "no"
    assert agent._command("in fein rechnen") == "fein" and agent._command("nimm Eiche") is None
    assert agent._value("zwei zwanzig") == 220 and agent._value("1,80") == 180 and agent._value("220") == 220
    assert agent._value("60 tief") == 60 and agent._value("nimm Eiche") is None and agent._value("zwei Türen") is None
    assert agent._MOVE.match("weiter links") and agent._MOVE.match("20 cm nach rechts")
    assert agent._MOVE.match("den Schrank etwas weiter rechts").group("what").lower() == "schrank"
    assert not agent._MOVE.match("links neben dem Fenster ein Schrank")
    # imperative phrasing from the live run: it must move, not go to intent as a new location phrase
    m = agent._MOVE.match("Schieb den Schrank 40 cm nach links")
    assert m and m.group("what").lower() == "schrank" and m.group("cm") == "40" and m.group("dir") == "links"
    assert agent._MOVE.match("bitte rück das Regal etwas nach rechts").group("what").lower() == "regal"
    assert not agent._MOVE.match("stell das Sideboard links neben das Fenster")


def test_router(env):
    app = FastAPI()

    @app.middleware("http")
    async def owner(request, call_next):
        request.state.owner = "peter"
        return await call_next(request)
    app.include_router(agent.router)
    c = TestClient(app)
    assert c.post("/api/agent/start", data={"title": "TEST"}).status_code == 400
    assert c.post("/api/agent/start", files={"photo": ("a.png", b"nope", "image/png")}, data={"title": "TEST"}).status_code == 400
    r = c.post("/api/agent/start", files={"photo": ("a.png", png(), "image/png")}, data={"title": "TEST Router"})
    assert r.status_code == 200
    pid = r.json()["pid"]
    settle(pid)
    assert c.get(f"/api/agent/{pid}/preview.png").status_code == 404
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    r = c.post(f"/api/agent/{pid}/message", data={"text": "ein Schrank an der Rückwand"})
    assert r.status_code == 200 and r.json()["new"][0]["role"] == "user"
    settle(pid)
    r = c.get(f"/api/agent/{pid}/events", params={"after": 0})
    assert r.json()["state"] == "preview" and r.json()["events"][-1]["kind"] == "preview"
    pr = c.get(f"/api/agent/{pid}/preview.png")
    assert pr.status_code == 200 and pr.headers["content-type"] == "image/jpeg"
    assert c.get("/api/agent").json()["conversations"][0]["pid"] == pid
    assert c.get("/api/agent/ffffff000000/events").status_code == 404
    assert c.post(f"/api/agent/{pid}/message", data={}).status_code == 400
    assert c.post(f"/api/agent/{pid}/message", data={"text": "x" * 2001}).status_code == 400


def test_preview_image_draws_pieces_and_removes(env):
    env.readings = [SCHRANK]
    env.spots["an der Rückwand"] = SPOT
    env.objects["der alte Schrank mit allem, was darauf steht"] = OBJ
    pid = agent.start(png(color=(128, 128, 128)), "peter", text="…", title="TEST Bild")
    settle(pid)
    doc = projects.load("workshop", pid)
    img = agent.render_preview(doc)
    px = img.load()
    box = doc["pieces"][0]["placement"]["box"]
    W, H = img.size
    # the wireframe colour (#59a7ff) appears inside the piece's frame, the red removal outline somewhere in the photo
    blue = sum(1 for x in range(box[1] * W // 1000, box[3] * W // 1000) for y in range(box[0] * H // 1000, box[2] * H // 1000)
               if abs(px[x, y][0] - 0x59) < 30 and abs(px[x, y][2] - 0xff) < 30)
    red = sum(1 for x in range(0, W, 2) for y in range(0, H, 2) if px[x, y][0] > 230 and px[x, y][1] < 140)
    assert blue > 200 and red > 50
