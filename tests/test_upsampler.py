"""upsampler.py without GPU or server: requests identical to diffusers' upsample_prompt (captured from engine.upsample
with a fake model/processor), streaming, cancel, fallbacks, batching, cache, estimates, retry captions."""

import base64
import io
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
import torch
from PIL import Image

import upsampler as up
from diffusers.modular_pipelines.flux3 import prompt_upsampler as U

ROWS = [{"id": "moebel_1", "from": "target", "src_bbox": None, "tgt_bbox": [150, 245, 780, 545], "desc": "a kitchen"},
        {"id": "weg_1", "from": "ref_image_0", "src_bbox": [800, 600, 920, 800], "tgt_bbox": None, "desc": "must go"}]


def room(color=(120, 90, 60), size=(1344, 1008)):
    return Image.new("RGB", size, color)


def item(images=(), boxes=None, request="add a kitchen", width=1776, height=1328):
    return {"request": request, "images": list(images), "boxes": boxes, "width": width, "height": height}


class Stream(httpx.SyncByteStream):
    """A streamed SSE body; records whether the client closed it."""

    def __init__(self, tokens, delay=0.0, usage=True, fail_after=None):
        self.tokens, self.delay, self.usage, self.fail_after, self.closed, self.sent = tokens, delay, usage, fail_after, False, 0

    def __iter__(self):
        for i, t in enumerate(self.tokens):
            if self.fail_after is not None and i >= self.fail_after:
                raise httpx.RemoteProtocolError("server disconnected")
            if self.delay:
                time.sleep(self.delay)
            self.sent += 1
            yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': t}}]})}\n\n".encode()
        if self.usage:
            yield f"data: {json.dumps({'choices': [], 'usage': {'completion_tokens': len(self.tokens)}})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    def close(self):
        self.closed = True


class Server:
    """httpx.MockTransport stand-in for vLLM; one fresh client per _client() call (per thread)."""

    def __init__(self, reply=lambda body: ["A caption."], status=200, delay=0.0, chunk_delay=0.0, fail_after=None):
        self.reply, self.status, self.delay, self.chunk_delay, self.fail_after = reply, status, delay, chunk_delay, fail_after
        self.bodies, self.streams, self.clients, self.threads = [], [], 0, set()
        self.lock = threading.Lock()

    def handler(self, request):
        body = json.loads(request.content)
        with self.lock:
            self.bodies.append(body)
            self.threads.add(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, text="boom")
        s = Stream(self.reply(body), self.chunk_delay, fail_after=self.fail_after)
        self.streams.append(s)
        return httpx.Response(200, stream=s, headers={"content-type": "text/event-stream"})

    def client(self):
        with self.lock:
            self.clients += 1
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=up.TIMEOUT)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(up, "_status", {"t": 0.0, "v": None})
    monkeypatch.setattr(up, "_down_until", 0.0)
    monkeypatch.setattr(up, "_cache", type(up._cache)())
    monkeypatch.setattr(up, "_seconds", json.loads(json.dumps(up._seconds)))
    monkeypatch.setattr(up, "_tokens", dict(up._tokens))
    monkeypatch.setattr(up, "_hist", {})
    monkeypatch.setattr(up, "_prefetch", {})
    monkeypatch.setattr(up, "_unit_state", lambda: "down")
    monkeypatch.setattr(up, "LOCAL", False)
    monkeypatch.setattr(up, "ENGINE", None)


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(up, "_healthy", lambda: True)
    monkeypatch.setattr(up, "_client", s.client)
    return s


# ---------------- request = diffusers' request ----------------

def diffusers_messages(request, images, boxes, width, height):
    """The messages engine.upsample → diffusers' upsample_prompt builds (fake model/processor capture them)."""
    import engine
    seen = {}

    class Proc:
        def apply_chat_template(self, messages, **kw):
            seen["messages"], seen["kw"] = messages, kw

            class Batch(dict):
                def to(self, device):
                    return self
            return Batch(input_ids=torch.zeros((1, 3), dtype=torch.long))

        def decode(self, ids, skip_special_tokens):
            return "x"

    class Model:
        device = "cpu"

        def generate(self, **kw):
            seen["gen"] = kw
            return torch.zeros((1, 4), dtype=torch.long)

    class Self:
        pup, pup_processor = Model(), Proc()

        def load_pup(self):
            pass

    unit = [up.to_unit(b) for b in boxes] if boxes else None
    engine.Engine.upsample(Self(), request, images, unit, width, height)
    return seen


def decode(url):
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")


@pytest.mark.parametrize("case", ["t2i", "t2i_wide", "i2i", "i2i_bbox", "i2i_bbox_multi"])
def test_request_equals_diffusers(server, case):
    images = {"t2i": [], "t2i_wide": [], "i2i": [room()], "i2i_bbox": [room()],
              "i2i_bbox_multi": [room(), room((250, 250, 250), (300, 400)), room((90, 60, 30), (200, 200))]}[case]
    boxes = ROWS if "bbox" in case else None
    w, h = (1024, 1024) if case == "t2i" else (1776, 1328)
    res = up.caption(item(images, boxes, width=w, height=h))
    assert res["backend"] == "vllm" and not res["cached"]
    body = server.bodies[0]
    ref = diffusers_messages("add a kitchen", images, boxes, w, h)
    mode = up.mode_of(images, boxes)
    assert mode == {"t2i": "t2i", "t2i_wide": "t2i", "i2i": "i2i"}.get(case, "i2i_bbox")
    sys_msg, user = body["messages"]
    assert sys_msg == {"role": "system", "content": ref["messages"][0]["content"][0]["text"]}
    assert sys_msg["content"] == U.SYSTEM_PROMPTS[mode]
    want = ref["messages"][1]["content"]
    assert user["role"] == "user" and len(user["content"]) == len(want)
    for got, exp in zip(user["content"], want):
        if exp["type"] == "image":
            assert got["type"] == "image_url"
            img = decode(got["image_url"]["url"])
            assert img.size == exp["image"].size and img.tobytes() == exp["image"].convert("RGB").tobytes()
        else:
            assert got == {"type": "text", "text": exp["text"]}
    if case == "t2i_wide":
        assert user["content"][0]["text"].startswith("TARGET CANVAS: 4:3\n") or user["content"][0]["text"].startswith("TARGET CANVAS:")
    if "bbox" in case:
        assert "LOCKED BOUNDING-BOX ANNOTATIONS" in user["content"][-1]["text"]
    assert ref["kw"]["enable_thinking"] is False and ref["gen"]["do_sample"] is False
    assert body["temperature"] == 0 and body["max_tokens"] == ref["gen"]["max_new_tokens"] == U.MAX_NEW_TOKENS
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["skip_special_tokens"] is True
    assert body["model"] == "pup"


def test_reconcile_and_empty(server, monkeypatch):
    planned = [{"id": "obj_7", "bbox": [151, 246, 779, 546], "desc": "a custom oak kitchen"}]
    server.reply = lambda body: ["A room with a new kitchen <obj_7>. ", json.dumps(planned)]
    res = up.caption(item([room()], ROWS))
    locked = U.boxes_to_canvas([up.to_unit(b) for b in ROWS])
    assert res["text"] == U.reconcile_boxes("A room with a new kitchen <obj_7>. " + json.dumps(planned), locked)
    text, rows = U.split_caption(res["text"])
    assert "<moebel_1>" in text and [r["tgt_bbox"] for r in rows if r["id"] == "moebel_1"] == [[150, 245, 780, 545]]
    assert res["tokens"] == 2
    server.reply = lambda body: ["   "]
    res = up.caption(item([room()], ROWS, request="other"))
    assert res["backend"] == "template" and res["text"] == "other " + json.dumps(ROWS, ensure_ascii=False)
    assert up.status()["state"] == "up"  # an empty answer is not an outage


# ---------------- fallbacks ----------------

def test_down_and_starting_return_template_at_once(monkeypatch):
    monkeypatch.setattr(up, "_healthy", lambda: False)
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("the upsampler never sleeps"))
    for state in ("down", "starting"):
        monkeypatch.setattr(up, "_status", {"t": 0.0, "v": None})
        monkeypatch.setattr(up, "_unit_state", lambda s=state: s)
        assert up.status() == {"backend": "template", "state": state, "model": None}
        t0 = time.time()
        res = up.caption(item([room()], ROWS, request="  build it "))
        assert time.time() - t0 < 0.5
        assert res == {"text": "build it " + json.dumps(ROWS, ensure_ascii=False), "backend": "template", "seconds": 0.0,
                       "tokens": None, "cached": False}
        assert up.caption(item())["text"] == "add a kitchen"


def test_5xx_marks_down_for_30s(server, monkeypatch):
    server.status = 503
    health = []
    monkeypatch.setattr(up, "_healthy", lambda: health.append(1) or True)
    res = up.caption(item([room()]))
    assert res["backend"] == "template" and len(server.bodies) == 1
    n = len(health)
    st = up.status(fresh=True)
    assert st["state"] == "down" and len(health) == n  # marked down: no health probe for 30 s
    res = up.caption(item([room()], request="again"))
    assert res["backend"] == "template" and len(server.bodies) == 1
    monkeypatch.setattr(up, "_down_until", time.time() - 1)
    server.status = 200
    assert up.status(fresh=True)["state"] == "up" and up.caption(item([room()], request="later"))["backend"] == "vllm"


def test_server_killed_mid_stream(server):
    server.fail_after, server.chunk_delay = 3, 0.01
    server.reply = lambda body: ["tok "] * 50
    t0 = time.time()
    res = up.caption(item([room()]))
    assert res["backend"] == "template" and time.time() - t0 < 3
    assert up.status()["state"] == "down"


class FakeEngine:
    def __init__(self):
        self.lock, self.pup, self.calls = threading.Lock(), None, []

    def upsample(self, request, images, boxes, width, height):
        self.calls.append((request, len(images), boxes, width, height))
        return "local caption"


def test_local_only_with_50_gb(monkeypatch):
    eng = FakeEngine()
    up.configure(eng)
    monkeypatch.setattr(up, "_healthy", lambda: False)
    monkeypatch.setattr(up, "LOCAL", True)
    monkeypatch.setattr(up, "mem_available_gb", lambda: 40.0)
    assert up.caption(item([room()], ROWS))["backend"] == "template" and eng.calls == []
    monkeypatch.setattr(up, "mem_available_gb", lambda: 60.0)
    res = up.caption(item([room()], ROWS, request="x"))
    assert res["backend"] == "local" and res["text"] == "local caption"
    assert eng.calls[0][2][0] == up.to_unit(ROWS[0]) and eng.calls[0][2][0]["tgt_bbox"] == [0.15, 0.245, 0.78, 0.545]
    assert up.status(fresh=True)["backend"] == "template"  # status says local only once the model is loaded
    eng.pup = object()
    monkeypatch.setattr(up, "mem_available_gb", lambda: 20.0)  # loaded: no new load spike
    assert up.status(fresh=True)["backend"] == "local" and up.caption(item(request="y"))["backend"] == "local"
    monkeypatch.setattr(up, "LOCAL", False)
    assert up.caption(item(request="z"))["backend"] == "template"


# ---------------- batching, cancel, progress, cache ----------------

def test_caption_many_concurrent_own_clients(server):
    server.delay = 0.5
    t0 = time.time()
    res = up.caption_many([item(request=f"r{i}") for i in range(3)])
    assert time.time() - t0 < 1.2 and [r["backend"] for r in res] == ["vllm"] * 3
    assert server.clients == 3 and len(server.threads) == 3  # one client per request thread, never shared
    res = up.caption_many([item(request=f"s{i}") for i in range(4)])  # BATCH = 3 at a time
    assert len(res) == 4 and all(r["backend"] == "vllm" for r in res)


def test_cancel_aborts_stream(server):
    server.reply, server.chunk_delay = (lambda body: ["tok "] * 400), 0.05
    ev = threading.Timer(0.3, lambda: cancel.set())
    cancel = threading.Event()
    ev.start()
    t0 = time.time()
    res = up.caption(item([room()]), cancel=cancel)
    assert time.time() - t0 < 1.0 and res == {"error": "Abgebrochen", "cancelled": True}
    assert server.streams[0].closed and server.streams[0].sent < 40
    assert not up.cache_has(item([room()]))
    cancel = threading.Event()
    cancel.set()
    assert up.caption_many([item(request="q")], [cancel])[0]["cancelled"] is True  # cancelled before it started


def test_progress_and_cache(server, monkeypatch):
    server.reply, server.chunk_delay = (lambda body: ["t "] * 30), 0.04
    monkeypatch.setattr(up, "PROGRESS_S", 0.1)
    seen = []
    it, expected = item([room()], ROWS), up.expected_tokens("i2i_bbox")
    res = up.caption_many([it], on_progress=lambda i, n, e: seen.append((i, n, e)))[0]
    assert res["backend"] == "vllm" and res["tokens"] == 30
    assert len(seen) >= 3 and [n for _, n, _ in seen] == sorted({n for _, n, _ in seen}) and seen[0][0] == 0
    assert all(e == expected for _, _, e in seen) and up.expected_tokens("i2i_bbox") < expected  # EMA learnt
    assert up.cache_has(it) and up.cache_has(item([room()], ROWS))  # same pixels, other objects
    assert not up.cache_has(item([room((1, 2, 3))], ROWS)) and not up.cache_has(item([room()], ROWS, request="other"))
    hit = up.caption(item([room()], ROWS))
    assert hit == {"text": res["text"], "backend": "cache", "seconds": 0.0, "tokens": None, "cached": True}
    assert len(server.bodies) == 1
    monkeypatch.setattr(up, "_healthy", lambda: False)
    monkeypatch.setattr(up, "_status", {"t": 0.0, "v": None})
    assert up.caption(item([room()], ROWS))["backend"] == "cache"  # a hit needs no service
    up.caption(item(request="template only"))
    assert not up.cache_has(item(request="template only"))  # template results are not cached
    for i in range(up.CACHE_SIZE + 2):
        up._cache_put(f"k{i}", "x")
    assert len(up._cache) == up.CACHE_SIZE and not up.cache_has(it)  # LRU


def test_item_errors_never_raise(server):
    bad = item([room()], [{"id": "x", "desc": "no box"}])
    res = up.caption_many([bad, item(request="fine")])
    assert res[0]["cancelled"] is False and "error" in res[0] and res[1]["backend"] == "vllm"


# ---------------- modes, estimates, stats, status, retry ----------------

def test_mode_keys_everywhere():
    assert [up.mode_of(i, b) for i, b in (([], None), ([1], None), ([], [1]), ([1], [1]))] == ["t2i", "i2i", "t2i_bbox", "i2i_bbox"]
    for m in ("t2i", "i2i", "t2i_bbox", "i2i_bbox"):
        assert m in U.SYSTEM_PROMPTS and m in up.MODE_LABELS and m in up._seconds["vllm"] and m in up._tokens
    assert up.estimate_s("i2i_bbox", "vllm") == 61 and up.estimate_s("i2i", "vllm") == 13
    assert up.estimate_s("i2i_bbox", "local") == pytest.approx(61 * 2.8)
    assert up.estimate_s("i2i_bbox", "template") == 0 and up.estimate_s("i2i", "cache") == 0
    assert up.expected_tokens("i2i_bbox") == 1340


def test_median_and_stats_roundtrip(server):
    server.reply = lambda body: ["w "] * 500
    up.caption(item([room()]))
    assert up.estimate_s("i2i", "vllm") < 13 and up.expected_tokens("i2i") == round((270 + 500) / 2)  # median of seed + 1
    for _ in range(2):
        up.caption(item([room((9, 9, 9))], request=f"r{_}"))
    assert up.expected_tokens("i2i") == 500  # [270, 500, 500, 500]: a single old/odd value no longer drags it
    st = up.stats()
    up.load_stats({"seconds": {"vllm": {"i2i": 99.0, "bogus": 1, "t2i": -5}, "nope": {}}, "tokens": {"i2i": "x", "t2i": 321}})
    assert up.estimate_s("i2i", "vllm") == 99 and up.estimate_s("t2i", "vllm") == st["seconds"]["vllm"]["t2i"]
    assert up.expected_tokens("t2i") == 321 and up.expected_tokens("i2i") == int(st["tokens"]["i2i"])
    up.load_stats(None)
    up.load_stats(st)
    assert up.stats() == st


def test_unit_state(monkeypatch):
    import subprocess
    monkeypatch.undo()  # the real _unit_state
    now = time.monotonic()

    def fake(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")

    for out, want in ((f"ActiveState=activating\nActiveEnterTimestampMonotonic=0\n", "starting"),
                      (f"ActiveState=active\nActiveEnterTimestampMonotonic={int((now - 60) * 1e6)}\n", "starting"),
                      (f"ActiveState=active\nActiveEnterTimestampMonotonic={int((now - 900) * 1e6)}\n", "down"),
                      ("ActiveState=inactive\nActiveEnterTimestampMonotonic=0\n", "down"),
                      ("ActiveState=failed\n", "down")):
        monkeypatch.setattr(up.subprocess, "run", fake(out))
        assert up._unit_state() == want


def test_retry_caption():
    locked = U.boxes_to_canvas([up.to_unit(b) for b in ROWS])
    cap = U.reconcile_boxes("A kitchen in <moebel_1>, <weg_1> removed. " + json.dumps(locked), locked)
    out = up.retry_caption(cap, "make the worktop thinner")
    text, rows = U.split_caption(out)
    assert rows == U.split_caption(cap)[1] and text.endswith("Important corrections: make the worktop thinner")
    assert text.startswith("A kitchen in <moebel_1>")
    assert up.retry_caption("A plain caption.", "brighter") == "A plain caption. Important corrections: brighter"
    assert up.retry_caption("A plain caption.", "  ") == "A plain caption."
    tmpl = "build it " + json.dumps(ROWS, ensure_ascii=False)  # template captions carry editor rows: still last
    assert U.split_caption(up.retry_caption(tmpl, "x"))[1] == ROWS


def test_prefetch_is_reused_by_the_job(server, monkeypatch):
    server.reply, server.chunk_delay = (lambda body: ["t "] * 40), 0.02
    it = item([room()], ROWS)
    assert up.prefetch(it) is True and up.prefetch(item([room()], ROWS)) is False  # one request per design
    res = up.caption_many([item([room()], ROWS)])[0]  # waits for the running prefetch instead of asking again
    assert res["backend"] == "vllm" and len(server.bodies) == 1 and up.cache_has(it)
    assert up.prefetch(it) is False  # cached: nothing to do


def test_prefetch_cancelled_for_the_gpu(server):
    server.reply, server.chunk_delay = (lambda body: ["t "] * 200), 0.02
    it = item([room()], ROWS)
    up.prefetch(it)
    time.sleep(0.2)
    up.cancel_prefetch()
    for _ in range(100):
        if not up._prefetch:
            break
        time.sleep(0.05)
    assert not up._prefetch and not up.cache_has(it)
