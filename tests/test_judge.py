import base64
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import judge

GOOD = {"comment_de": "Sauber eingepasst.", "fix_hint_en": "", "room_preserved": 9, "matches_request": 8,
        "proportions": 7, "realism": 8, "artifacts": 9, "pass": True}
REQ = {"kind": "room", "pieces": [{"id": "p1", "label": "Küche", "desc": "a custom-made kitchen", "w": 180, "h": 250,
                                   "d": 60, "material": "natural oak", "handles": "push", "box": [150, 245, 780, 545],
                                   "relation": "it reaches 96% of the 260 cm ceiling height"}],
       "removes": [[800, 600, 920, 800]], "room_changes": "", "tidy": True, "protect": True, "edit_text": ""}


class Fake:
    """Mock vLLM server: records chat bodies, answers with queued texts (or a fixed one)."""

    def __init__(self, answers=None, health=200, chat_status=200):
        self.answers, self.health, self.chat_status, self.bodies = list(answers or [json.dumps(GOOD)]), health, chat_status, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(self.health)
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": judge.JUDGE_MODEL}]})
        body = json.loads(req.content)
        self.bodies.append(body)
        status = self.chat_status(body) if callable(self.chat_status) else self.chat_status
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    judge.mark_down()
    monkeypatch.setattr(judge, "unit_state", lambda: "active")
    monkeypatch.setattr(judge, "mem_available_gb", lambda: 80.0)
    monkeypatch.setattr(judge, "_sleep", lambda s: None)
    yield
    judge.mark_down()


def use(monkeypatch, fake):
    monkeypatch.setattr(judge, "_transport", httpx.MockTransport(fake))
    return fake


def imgs(sketch=None, room=True):
    return {"room": Image.new("RGB", (400, 300), "white") if room else None,
            "result": Image.new("RGB", (400, 300), "gray"), "sketch": sketch}


def decode(url: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def test_healthy_grade(monkeypatch):
    fake = use(monkeypatch, Fake())
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "ok" and r["pass"] is True and r["model"] == judge.JUDGE_MODEL
    assert r["overall"] == round(.25 * 9 + .25 * 8 + .2 * 7 + .15 * 8 + .15 * 9, 1)
    assert r["comment"] == "Sauber eingepasst." and r["fix_hint"] == "" and r["graded"] and r["seconds"] >= 0
    body = fake.bodies[0]
    assert body["temperature"] == 0 and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["json_schema"]["schema"] == judge.SCHEMA
    text = body["messages"][1]["content"][-1]["text"]
    assert "180 cm wide x 250 cm high x 60 cm deep" in text and "[150, 245, 780, 545]" in text and "96%" in text


def test_low_room_score_fails(monkeypatch):
    use(monkeypatch, Fake([json.dumps({**GOOD, "room_preserved": 5, "matches_request": 10, "proportions": 10})]))
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "ok" and r["overall"] >= 6.5 and r["pass"] is False


def test_model_fail_overrides(monkeypatch):
    use(monkeypatch, Fake([json.dumps({**GOOD, "matches_request": 6, "proportions": 5, "pass": False, "fix_hint_en": "Make it taller."})]))
    r = judge.grade(imgs(), REQ)
    assert r["pass"] is False and r["fix_hint"] == "Make it taller."


def test_faded_edges_is_a_hard_fail(monkeypatch):
    use(monkeypatch, Fake([json.dumps({**GOOD, "faded_edges": True, "realism": 10, "artifacts": 10, "pass": True})]))
    r = judge.grade(imgs(), REQ)
    assert r["pass"] is False and r["faded_edges"] is True and r["scores"]["artifacts"] == 3
    assert r["comment"].startswith("Möbelkante verblasst") and r["fix_hint"]
    assert "faded_edges" in judge.SCHEMA["required"] and "transparent" in judge.SYSTEM
    assert judge.score({k: 9 for k in judge.WEIGHTS}, True, True, faded=True) == (9.0, False)


@pytest.mark.parametrize("text", [
    "```json\n" + json.dumps(GOOD) + "\n```",
    "<think>hmm, let me look {not json}</think>" + json.dumps(GOOD),
    json.dumps(GOOD)[:-1] + ",}",
    json.dumps(GOOD).replace('"', "'"),
    "Here is my verdict: " + json.dumps(GOOD) + " Hope this helps!",
    json.dumps(GOOD).replace("true", "True"),
    '{"comment_de": "abgeschnitten", "room_preserved": 9, "matches_request": 8, "proportions": 7, "realism": 8, "artifacts": 9, "pass": true, "fix_hint_en": "Make the',
])
def test_parse_repairs(text):
    d = judge.parse(text)
    assert d is not None and d["matches_request"] == 8 and d["artifacts"] == 9 and d["pass"] is True


def test_parse_clamps_and_hopeless():
    d = judge.parse(json.dumps({**GOOD, "realism": 14, "artifacts": -2, "room_preserved": None}))
    assert d["realism"] == 10 and d["artifacts"] == 0 and d["room_preserved"] is None
    assert judge.parse("I cannot judge this image.") is None
    assert judge.parse(json.dumps({k: v for k, v in GOOD.items() if k != "proportions"})) is None
    assert judge.parse(json.dumps({**GOOD, "comment_de": "Ein 'kleiner' Fehler {links}."}))["comment_de"] == "Ein 'kleiner' Fehler {links}."


def test_unparseable_twice_is_error(monkeypatch):
    fake = use(monkeypatch, Fake(["kaputt"]))
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "error" and r["comment"] == "Bewertung fehlgeschlagen" and r["scores"] is None
    assert len(fake.bodies) == 2


def test_400_retries_without_schema(monkeypatch):
    fake = use(monkeypatch, Fake(chat_status=lambda b: 400 if "response_format" in b else 200))
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "ok" and len(fake.bodies) == 2 and "response_format" not in fake.bodies[1]


def test_reasoning_content_fallback(monkeypatch):
    def h(req):
        if req.url.path != "/v1/chat/completions":
            return Fake()(req)
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": json.dumps(GOOD)}}]})
    use(monkeypatch, h)
    assert judge.grade(imgs(), REQ)["status"] == "ok"


def test_server_down_fast(monkeypatch):
    monkeypatch.setattr(judge, "JUDGE_URL", "http://127.0.0.1:9")   # nothing listens on the discard port
    t = time.time()
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "unavailable" and r["comment"] == "Prüfer nicht verfügbar" and time.time() - t < 3
    assert judge.status(force=True)["available"] is False


def test_health_not_ready(monkeypatch):
    fake = use(monkeypatch, Fake(health=503))
    assert judge.grade(imgs(), REQ)["status"] == "unavailable" and not fake.bodies


def test_crash_mid_request_is_unavailable(monkeypatch):
    fake = use(monkeypatch, Fake(chat_status=500))
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "unavailable" and fake.bodies


def test_memory_guard(monkeypatch):
    fake = use(monkeypatch, Fake())
    slept = []
    monkeypatch.setattr(judge, "mem_available_gb", lambda: 9.0)
    monkeypatch.setattr(judge, "_sleep", slept.append)
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "unavailable" and r["comment"] == "Speicher knapp – Prüfung übersprungen"
    assert sum(slept) == 120 and not fake.bodies


def test_memory_guard_recovers(monkeypatch):
    use(monkeypatch, Fake())
    vals = iter([9.0, 9.0, 30.0])
    monkeypatch.setattr(judge, "mem_available_gb", lambda: next(vals, 30.0))
    assert judge.grade(imgs(), REQ)["status"] == "ok"


def test_score_rules():
    s = {"room_preserved": 9, "matches_request": 8, "proportions": 7, "realism": 8, "artifacts": 9}
    assert judge.score(s, True, True) == (8.2, True)
    assert judge.score({**s, "artifacts": 4}, True, True)[1] is False          # one score below 5
    assert judge.score(s, None, True)[1] is True
    assert judge.score(s, False, True)[1] is True                               # 8.2 overrules the model's fail
    weak = {**s, "matches_request": 6, "proportions": 5}                        # 7.3: the model's fail counts
    assert judge.score(weak, True, True) == (7.3, True) and judge.score(weak, False, True) == (7.3, False)
    overall, ok = judge.score({**s, "room_preserved": 0}, True, False)          # ignored without room
    assert overall == round((.25 * 8 + .2 * 7 + .15 * 8 + .15 * 9) / .75, 1) and ok
    assert judge.score({**s, "room_preserved": None}, True, True)[1] is False   # room expected but not graded


def test_render_ignores_room(monkeypatch):
    fake = use(monkeypatch, Fake([json.dumps({**GOOD, "room_preserved": 0})]))
    sketch = Image.new("RGB", (300, 300), "white")
    r = judge.grade({"room": None, "result": Image.new("RGB", (300, 300)), "sketch": sketch}, {**REQ, "kind": "render"})
    assert r["status"] == "ok" and r["scores"]["room_preserved"] is None and r["pass"] is True
    content = fake.bodies[0]["messages"][1]["content"]
    labels = [c["text"] for c in content if c["type"] == "text"]
    assert labels[0] == "Image 1: the carpenter's SKETCH of the furniture." and labels[1] == "Image 2: the GENERATED result."
    assert sum(c["type"] == "image_url" for c in content) == 2


def test_image_order_and_labels(monkeypatch):
    fake = use(monkeypatch, Fake())
    room = Image.new("RGB", (2400, 1800), (255, 0, 0))
    res = Image.new("RGB", (1776, 1328), (0, 255, 0))
    sketch = Image.new("RGBA", (500, 400), (0, 0, 0, 0))          # transparent CAD screenshot
    judge.grade({"room": room, "result": res, "sketch": sketch}, REQ)
    content = fake.bodies[0]["messages"][1]["content"]
    assert [c["type"] for c in content] == ["text", "image_url"] * 3 + ["text"]
    assert [content[i]["text"] for i in (0, 2, 4)] == ["Image 1: the ORIGINAL room photo.", "Image 2: the GENERATED result.",
                                                     "Image 3: the carpenter's SKETCH of the furniture."]
    sent = [decode(content[i]["image_url"]["url"]) for i in (1, 3, 5)]
    assert all(c["image_url"]["url"].startswith("data:image/jpeg;base64,") for c in content if c["type"] == "image_url")
    assert sent[0].size == (1024, 768) and max(sent[1].size) == 1024
    r, g, b = sent[0].convert("RGB").getpixel((10, 10))
    assert r > 200 and g < 50
    assert min(sent[2].convert("RGB").getpixel((10, 10))) > 240         # alpha composited on white, not black


def test_edit_kind(monkeypatch):
    fake = use(monkeypatch, Fake())
    r = judge.grade(imgs(), {**REQ, "kind": "edit", "edit_text": "Make the doors sage green."})
    content = fake.bodies[0]["messages"][1]["content"]
    assert content[0]["text"] == "Image 1: the image BEFORE the edit." and "sage green" in content[-1]["text"]
    assert r["scores"]["room_preserved"] == 9


def test_no_result_is_error(monkeypatch):
    use(monkeypatch, Fake())
    assert judge.grade({"room": None, "result": None}, REQ)["status"] == "error"


def test_retry_truth_table():
    ok_f = {"status": "ok", "pass": False, "overall": 5.0, "fix_hint": "Make it taller."}
    ok_f2 = {"status": "ok", "pass": False, "overall": 5.0, "fix_hint": "Keep the window."}
    ok_f3 = {"status": "ok", "pass": False, "overall": 3.0, "fix_hint": "Start over."}
    ok_t = {"status": "ok", "pass": True, "overall": 8.0, "fix_hint": ""}
    down = {"status": "unavailable", "pass": None}
    assert judge.needs_retry([]) is False
    assert judge.needs_retry([ok_f, ok_f2]) is True
    assert judge.needs_retry([ok_f, ok_t]) is False
    assert judge.needs_retry([ok_f, down]) is False
    assert judge.needs_retry([down]) is False
    assert judge.retry_hint([ok_f3, ok_f, ok_f2, ok_f]) == "Make it taller. Keep the window."
    assert judge.retry_hint([ok_f3, {**ok_f, "fix_hint": ""}]) == "Start over."
    assert len(judge.retry_hint([{**ok_f, "fix_hint": "word " * 100}])) <= 300
    assert judge.retry_hint([down]) == ""
    assert judge.retry_suffix("Make it taller.") == "Important corrections: Make it taller."
    assert judge.retry_suffix("") == ""


def test_concurrent_grades(monkeypatch):
    use(monkeypatch, Fake())
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        out = list(ex.map(lambda _: judge.grade(imgs(), REQ), range(4)))
    assert all(r["status"] == "ok" for r in out)


def test_read_sketch(monkeypatch):
    fake = use(monkeypatch, Fake(['```json\n{"comment_de": "Schrank mit 3 Türen.", "w": 180, "h": 2500, "d": null, '
                                  '"doors": 3, "drawers": 0}\n```']))
    r = judge.read_sketch(Image.new("RGBA", (300, 200)))
    assert r == {"status": "ok", "w": 180, "h": 2000, "d": None, "doors": 3, "drawers": 0, "comment": "Schrank mit 3 Türen."}
    assert sum(c["type"] == "image_url" for c in fake.bodies[0]["messages"][1]["content"]) == 1
    monkeypatch.setattr(judge, "_transport", None)
    monkeypatch.setattr(judge, "JUDGE_URL", "http://127.0.0.1:9")
    judge.mark_down()
    r = judge.read_sketch(Image.new("RGB", (300, 200)))
    assert r["status"] == "unavailable" and r["w"] is None and r["doors"] is None


def test_status_endpoint_and_cache(monkeypatch):
    fake = use(monkeypatch, Fake())
    app = FastAPI()
    app.include_router(judge.router)
    r = TestClient(app).get("/api/judge/status").json()
    assert r["available"] is True and r["state"] == "active" and r["model"] == judge.JUDGE_MODEL and r["checked"]
    fake.health = 503
    assert judge.status()["available"] is True          # cached for 20 s
    assert judge.status(force=True)["available"] is False


def test_null_room_score_is_reasked(monkeypatch):
    fake = use(monkeypatch, Fake([json.dumps({**GOOD, "room_preserved": None}), json.dumps(GOOD)]))
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "ok" and r["pass"] is True and len(fake.bodies) == 2
    use(monkeypatch, Fake([json.dumps({**GOOD, "room_preserved": None})]))
    r = judge.grade(imgs(), REQ)
    assert r["status"] == "error" and not judge.needs_retry([r])  # no pointless auto-retry
    use(monkeypatch, Fake([json.dumps({**GOOD, "room_preserved": None})]))
    assert judge.grade(imgs(room=False), {**REQ, "kind": "free"})["status"] == "ok"


def test_check_plan_one_zoomed_request_per_piece(monkeypatch):
    bad = json.dumps({"pieces": [{"id": "x", "ok": False, "problems": ["Hängt vor dem Fenster."], "fix": "Daneben an die Wand."}]})
    fake = use(monkeypatch, Fake(answers=[bad]))
    photo, plan = Image.new("RGB", (800, 600), "white"), Image.new("RGB", (800, 600), "gray")
    pieces = [{"id": "a1", "label": "Wandregal", "desc": "100×40×25 cm", "box": [100, 600, 250, 900]},
              {"id": "b2", "label": "Sideboard", "desc": "120×75×40 cm", "box": [500, 0, 700, 200]}]
    r = judge.check_plan(photo, plan, pieces)
    assert r["status"] == "ok" and [p["id"] for p in r["pieces"]] == ["a1", "b2"]   # ids come from the request, not the model
    assert r["pieces"][0]["ok"] is False and r["pieces"][0]["problems"] == ["Hängt vor dem Fenster."]
    assert len(fake.bodies) == 2
    img = decode(fake.bodies[0]["messages"][1]["content"][1]["image_url"]["url"])
    assert img.size[0] < 800                                                        # a zoomed detail, not the whole photo
