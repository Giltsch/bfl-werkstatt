"""The only caption entry point: the prompt upsampler (klein-pup) served by vLLM (flux-pup, 127.0.0.1:8003), with the
in-process transformers pup (opt-in) and the template caption as fallbacks. It never waits for a starting service —
the scheduler decides that (jobs.py, caption_wait).

The request is exactly what diffusers' `upsample_prompt` builds (system prompt by mode, prepared references, layout
table, scaffold, greedy decoding), streamed so that progress is real and a cancel drops the connection, which makes
vLLM abort the request."""

import base64
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import traceback
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from statistics import median

import httpx
from diffusers.modular_pipelines.flux3 import prompt_upsampler as U

from imaging import canvas_ratio, mem_available_gb

URL = os.environ.get("FLUX_PUP_URL", "http://127.0.0.1:8003")
MODEL = "pup"
LOCAL = os.environ.get("FLUX_PUP_LOCAL") == "1"
LOCAL_MIN_FREE_GB = 50
START_WAIT_S = 420  # the scheduler's limit for parking a job while the service starts (cold start: FLUX ~60 s, then
                    # vLLM ~220 s; the GPU runs other people's images meanwhile)
BATCH = 3           # = --max-num-seqs of serve_pup.sh
CACHE_SIZE = 64
UNIT = "flux-pup"
MAX_IMAGES = 10     # = --limit-mm-per-prompt of serve_pup.sh; more → template
STATUS_TTL, DOWN_S, START_GRACE_S, PROGRESS_S = 5, 30, 600, 0.5
TIMEOUT = httpx.Timeout(connect=2, read=30, write=30, pool=5)  # read: per streamed chunk
HIST = 9  # estimates are the median of the last HIST measured captions per mode (one long caption must not skew them)
MODE_LABELS = {"i2i_bbox": "layout", "i2i": "edit", "t2i_bbox": "create_layout", "t2i": "create"}  # logs/bench only
LOCAL_FACTOR = 2.8

ENGINE = None
# EMA start values: i2i_bbox/i2i measured by tools/bench_upsampler.py on the Spark (FP8, 2026-10-17: layout 61 s /
# 1340 tokens, edit 13 s / 272 tokens, 21 tok/s); t2i* from the research estimates
_seconds = {"vllm": {"i2i_bbox": 61.0, "i2i": 13.0, "t2i_bbox": 50.0, "t2i": 20.0}, "local": {}}
_tokens = {"i2i_bbox": 1340.0, "i2i": 270.0, "t2i_bbox": 950.0, "t2i": 400.0}
_hist: dict = {}                  # (backend|"tokens", mode) -> deque of measured values; _seconds/_tokens hold their medians
_cache: "OrderedDict[str, str]" = OrderedDict()
_prefetch: dict = {}              # key -> {"cancel": Event, "done": Event, "tokens": int, "claimed": bool, "result": dict|None}
_lock = threading.Lock()          # cache, stats, status
_local_lock = threading.Lock()
_status = {"t": 0.0, "v": None}
_down_until = 0.0


def configure(engine=None) -> None:
    """The server's Engine, used only by the opt-in local fallback (FLUX_PUP_LOCAL=1)."""
    global ENGINE
    ENGINE = engine


def to_unit(row: dict) -> dict:
    """An editor row (0-1000) in the 0-1 form diffusers' upsampler takes."""
    u = lambda b: [v / 1000 for v in b] if b else None
    out = {"id": row["id"], "desc": row.get("desc", "")}
    if "bbox" in row:
        out["bbox"] = u(row["bbox"])
    else:
        out.update({"from": row.get("from") or "target", "src_bbox": u(row.get("src_bbox")), "tgt_bbox": u(row.get("tgt_bbox"))})
    return out


def mode_of(images, boxes) -> str:
    """diffusers' own mode key (prompt_upsampler.upsample_prompt): system prompt, estimates and cache use it."""
    return ("i2i" if images else "t2i") + ("_bbox" if boxes else "")


# ---------------- status ----------------

def _unit_state() -> str:
    """starting while systemd starts the unit or vLLM is still loading (active but unhealthy < 10 min), else down."""
    try:
        out = subprocess.run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return "down"
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    state = props.get("ActiveState")
    if state in ("activating", "reloading"):
        return "starting"
    if state == "active":
        since = int(props.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
        return "starting" if since and time.monotonic() - since < START_GRACE_S else "down"
    return "down"


def _healthy() -> bool:
    try:
        return httpx.get(f"{URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def _local_ready() -> bool:
    return LOCAL and ENGINE is not None and getattr(ENGINE, "pup", None) is not None


def status(fresh=False) -> dict:
    """{"backend": "vllm"|"local"|"template", "state": "up"|"starting"|"down", "model": …}, cached 5 s."""
    with _lock:
        if not fresh and _status["v"] and time.time() - _status["t"] < STATUS_TTL:
            return dict(_status["v"])
    state = "up" if time.time() >= _down_until and _healthy() else _unit_state()
    if state == "up":
        v = {"backend": "vllm", "state": "up", "model": "klein-pup-fp8"}
    elif _local_ready():
        v = {"backend": "local", "state": state, "model": "klein-pup"}
    else:
        v = {"backend": "template", "state": state, "model": None}
    with _lock:
        _status.update(t=time.time(), v=v)
    return dict(v)


def stop() -> bool:
    """POST /api/unload/pup: stops flux-pup (~16 GB back); captions use the template until the studio's next start."""
    try:
        r = subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True, timeout=60)
    except Exception:
        return False
    _mark_down()
    return r.returncode == 0


def _mark_down():
    global _down_until
    with _lock:
        _down_until = time.time() + DOWN_S
        _status["v"] = None


# ---------------- requests ----------------

def _png_url(img) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_request(item: dict) -> tuple[dict, list | None, str]:
    """(body, locked rows, mode) — the chat request diffusers' upsample_prompt would run, as an OpenAI request."""
    images, boxes = item.get("images") or [], item.get("boxes") or None
    request, mode = item.get("request") or "", mode_of(images, boxes)
    w, h = int(item.get("width") or 1024), int(item.get("height") or 1024)
    if not images and w != h:
        request = f"TARGET CANVAS: {canvas_ratio(w, h)}\n{request}"
    locked = U.boxes_to_canvas([to_unit(b) for b in boxes]) if boxes else None
    if locked:
        request = U.format_layout_request(request, locked)
    if images:
        refs = U.prepare_references(images)
        content = [{"type": "image_url", "image_url": {"url": _png_url(r)}} for r in refs]
        content.append({"type": "text", "text": U.I2I_SCAFFOLD[bool(locked)].format(instruction=request)})
    else:
        content = [{"type": "text", "text": request}]
    body = {"model": MODEL, "messages": [{"role": "system", "content": U.SYSTEM_PROMPTS[mode]}, {"role": "user", "content": content}],
            "temperature": 0, "max_tokens": U.MAX_NEW_TOKENS, "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}, "skip_special_tokens": True}
    return body, locked, mode


def template_text(item: dict) -> str:
    """Today's upsample=False caption: the prompt itself, the editor rows appended as JSON."""
    text = (item.get("request") or "").strip()
    return f"{text} {json.dumps(item['boxes'], ensure_ascii=False)}" if item.get("boxes") else text


def _key(item: dict) -> str:
    # the prepared references are a pure function of the input pixels, so hashing those identifies them as well
    h = hashlib.sha1()
    for part in (mode_of(item.get("images"), item.get("boxes")), item.get("request") or "",
                 json.dumps(item.get("boxes") or None, sort_keys=True), str(item.get("width")), str(item.get("height"))):
        h.update(part.encode() + b"\0")
    for im in item.get("images") or []:
        h.update(hashlib.sha1(f"{im.mode}{im.size}".encode() + im.tobytes()).digest())
    return h.hexdigest()


def cached(item: dict) -> str | None:
    """The cached caption of this item, if any (the scheduler skips the caption unit and its wait then)."""
    try:
        return _cache_get(_key(item))
    except Exception:
        return None


def cache_has(item: dict) -> bool:
    try:
        k = _key(item)
    except Exception:
        return False
    with _lock:
        return k in _cache


def _cache_get(k):
    with _lock:
        if k in _cache:
            _cache.move_to_end(k)
            return _cache[k]
    return None


def _cache_put(k, text):
    with _lock:
        _cache[k] = text
        _cache.move_to_end(k)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


class _Down(Exception):
    pass


class _Cancelled(Exception):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _stream(body, cancel, progress):
    """One streamed chat completion in the calling thread with its own client; returns (text, tokens)."""
    parts, n, usage = [], 0, None
    with _client() as client:
        try:
            with client.stream("POST", f"{URL}/v1/chat/completions", json=body) as r:
                if r.status_code >= 500:
                    raise _Down(f"HTTP {r.status_code}")
                if r.status_code != 200:
                    r.read()
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                for line in r.iter_lines():
                    if cancel is not None and cancel.is_set():
                        r.close()  # same thread: the connection drops and vLLM aborts the request
                        raise _Cancelled()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or usage
                    for ch in chunk.get("choices") or []:
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            parts.append(delta)
                            n += 1
                            progress(n)
        except (httpx.TransportError, httpx.StreamError) as e:
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            raise _Down(f"{type(e).__name__}: {e}")
    return "".join(parts).strip(), (usage or {}).get("completion_tokens") or n


def _push(tab: dict, key, mode, value, nd):
    """Append a measurement (the stored estimate seeds an empty history) and keep the median as the estimate."""
    q = _hist.setdefault((key, mode), deque([tab[mode]] if mode in tab else [], maxlen=HIST))
    q.append(value)
    tab[mode] = round(median(q), nd)


def _learn(mode, backend, seconds, tokens=None):
    with _lock:
        _push(_seconds.setdefault(backend, {}), backend, mode, max(seconds, 0.01), 2)
        if tokens:
            _push(_tokens, "tokens", mode, tokens, 1)


def _vllm(item, cancel, progress, batch_n):
    body, locked, mode = build_request(item)
    t0 = time.time()
    text, tokens = _stream(body, cancel, progress)
    if locked and text:
        text = U.reconcile_boxes(text, locked)
    if not text:
        raise RuntimeError("leere Beschreibung")
    dt = time.time() - t0
    _learn(mode, "vllm", dt / (1 + 0.15 * (batch_n - 1)), tokens)  # concurrent items take longer each
    return {"text": text, "backend": "vllm", "seconds": round(dt, 1), "tokens": tokens, "cached": False}


def _local_ok() -> bool:
    return LOCAL and ENGINE is not None and (getattr(ENGINE, "pup", None) is not None or mem_available_gb() >= LOCAL_MIN_FREE_GB)


def _fallback(item, cancel):
    """Local pup (opt-in, enough memory) → template; neither ever waits for the service."""
    if cancel is not None and cancel.is_set():
        return {"error": "Abgebrochen", "cancelled": True}
    if _local_ok():
        try:
            t0 = time.time()
            boxes = [to_unit(b) for b in item["boxes"]] if item.get("boxes") else None
            with _local_lock, ENGINE.lock:
                text = ENGINE.upsample(item.get("request") or "", item.get("images") or [], boxes,
                                       int(item.get("width") or 1024), int(item.get("height") or 1024))
            dt = time.time() - t0
            _learn(mode_of(item.get("images"), item.get("boxes")), "local", dt)
            return {"text": text, "backend": "local", "seconds": round(dt, 1), "tokens": None, "cached": False}
        except Exception:
            traceback.print_exc()
    return {"text": template_text(item), "backend": "template", "seconds": 0.0, "tokens": None, "cached": False}


def caption(item, cancel=None, on_progress=None) -> dict:
    cb = (lambda i, t, e: on_progress(t, e)) if on_progress else None
    return caption_many([item], [cancel], cb)[0]


def caption_many(items, cancels=None, on_progress=None) -> list[dict]:
    """Captions for several items: vLLM items run concurrently (≤ BATCH), the rest sequentially. Each result is
    {"text", "backend", "seconds", "tokens", "cached"} or {"error", "cancelled"}; never raises for item problems."""
    items = list(items)
    cancels = list(cancels or []) + [None] * (len(items) - len(cancels or []))
    out: list = [None] * len(items)
    keys: list = [None] * len(items)
    cb_lock = threading.Lock()
    for i, it in enumerate(items):
        try:
            if it.get("boxes"):
                U.boxes_to_canvas([to_unit(b) for b in it["boxes"]])  # malformed rows are the item's error on every path
            keys[i] = _key(it)
            hit = _cache_get(keys[i])
        except Exception as e:
            out[i] = {"error": f"{type(e).__name__}: {e}", "cancelled": False}
            continue
        if hit is not None:
            out[i] = {"text": hit, "backend": "cache", "seconds": 0.0, "tokens": None, "cached": True}
    todo = [i for i in range(len(items)) if out[i] is None]

    def progress_fn(i):
        last = [0.0]
        expected = expected_tokens(mode_of(items[i].get("images"), items[i].get("boxes")))

        def f(n):
            if on_progress and time.time() - last[0] >= PROGRESS_S:
                last[0] = time.time()
                with cb_lock:
                    try:
                        on_progress(i, n, expected)
                    except Exception:
                        traceback.print_exc()
        return f

    for i in todo:  # a prefetch of the same design is already running: wait for it instead of asking twice
        with _lock:
            pf = _prefetch.get(keys[i])
            if pf:
                pf["claimed"] = True
        if not pf:
            continue
        f, cancel = progress_fn(i), cancels[i]
        while not pf["done"].wait(0.25) and not (cancel is not None and cancel.is_set()):
            f(pf["tokens"])
        if cancel is not None and cancel.is_set():
            out[i] = {"error": "Abgebrochen", "cancelled": True}
        elif pf["result"]:
            out[i] = dict(pf["result"])
    todo = [i for i in todo if out[i] is None]
    up = bool(todo) and status()["state"] == "up"
    remote = [i for i in todo if up and len(items[i].get("images") or []) <= MAX_IMAGES]

    def run_remote(i):
        try:
            return _vllm(items[i], cancels[i], progress_fn(i), len(remote))
        except _Cancelled:
            return {"error": "Abgebrochen", "cancelled": True}
        except _Down as e:
            print(f"upsampler: vLLM unavailable ({e}) – fallback", flush=True)
            _mark_down()
        except Exception as e:
            print(f"upsampler: vLLM request failed ({type(e).__name__}: {e}) – fallback", flush=True)
        try:
            return _fallback(items[i], cancels[i])
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "cancelled": False}

    if remote:
        with ThreadPoolExecutor(max_workers=min(BATCH, len(remote))) as pool:
            for i, res in zip(remote, pool.map(run_remote, remote)):
                out[i] = res
    for i in todo:
        if out[i] is None:
            try:
                out[i] = _fallback(items[i], cancels[i])
            except Exception as e:
                out[i] = {"error": f"{type(e).__name__}: {e}", "cancelled": False}
    for i in todo:
        if out[i].get("backend") in ("vllm", "local"):
            _cache_put(keys[i], out[i]["text"])
    return out


def prefetch(item: dict) -> bool:
    """Starts the caption of a design somebody is about to compute (step ④ open, GPU idle), so the job finds it cached
    or in flight and skips most of the caption time. One prefetch at a time: a newer design cancels an unclaimed older
    one; the scheduler cancels unclaimed ones when FLUX or the judge needs the GPU (both slow each other down)."""
    try:
        k = _key(item)
    except Exception:
        return False
    with _lock:
        if k in _cache or k in _prefetch:
            return False
        for old in _prefetch.values():
            if not old["claimed"]:
                old["cancel"].set()
        pf = _prefetch[k] = {"cancel": threading.Event(), "done": threading.Event(), "tokens": 0, "claimed": False, "result": None}

    def run():
        try:
            if status()["state"] == "up" and len(item.get("images") or []) <= MAX_IMAGES and not pf["cancel"].is_set():
                res = _vllm(item, pf["cancel"], lambda n: pf.__setitem__("tokens", n), 1)
                _cache_put(k, res["text"])
                pf["result"] = res
        except _Cancelled:
            pass
        except _Down as e:
            print(f"upsampler: prefetch – vLLM unavailable ({e})", flush=True)
            _mark_down()
        except Exception as e:
            print(f"upsampler: prefetch failed ({type(e).__name__}: {e})", flush=True)
        finally:
            with _lock:
                if _prefetch.get(k) is pf:
                    del _prefetch[k]
            pf["done"].set()

    threading.Thread(target=run, daemon=True, name="pup-prefetch").start()
    return True


def cancel_prefetch() -> None:
    with _lock:
        for pf in _prefetch.values():
            if not pf["claimed"]:
                pf["cancel"].set()


def retry_caption(caption: str, hint: str) -> str:
    """The previous caption with the judge's corrections inserted before its layout table (which must stay last)."""
    text, rows = U.split_caption(caption or "")
    try:
        import judge
        suffix = judge.retry_suffix(hint)
    except ImportError:
        suffix = f"Important corrections: {hint}" if hint and hint.strip() else ""
    if not suffix:
        return caption
    return f"{text} {suffix} {json.dumps(rows, ensure_ascii=False)}" if rows else f"{text} {suffix}"


# ---------------- estimates ----------------

def estimate_s(mode, backend=None) -> float:
    backend = backend or status()["backend"]
    with _lock:
        if backend == "vllm":
            return float(_seconds["vllm"].get(mode, 30.0))
        if backend == "local":
            return float(_seconds["local"].get(mode) or _seconds["vllm"].get(mode, 30.0) * LOCAL_FACTOR)
    return 0.0


def expected_tokens(mode) -> int:
    with _lock:
        return int(_tokens.get(mode, 600))


def stats() -> dict:
    with _lock:
        return {"seconds": json.loads(json.dumps(_seconds)), "tokens": dict(_tokens),
                "hist": {f"{k}|{m}": [round(x, 2) for x in q] for (k, m), q in _hist.items()}}


def load_stats(d: dict) -> None:
    """Restore EMA tables saved by stats(); anything malformed is ignored."""
    if not isinstance(d, dict):
        return
    ok = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1e5
    with _lock:
        for backend, tab in (d.get("seconds") or {}).items():
            if backend in _seconds and isinstance(tab, dict):
                _seconds[backend].update({m: float(v) for m, v in tab.items() if m in MODE_LABELS and ok(v)})
        if isinstance(d.get("tokens"), dict):
            _tokens.update({m: float(v) for m, v in d["tokens"].items() if m in MODE_LABELS and ok(v)})
        for name, vals in (d.get("hist") or {}).items() if isinstance(d.get("hist"), dict) else ():
            key, _, mode = str(name).partition("|")
            tab = _tokens if key == "tokens" else _seconds.get(key)
            vals = [float(x) for x in vals if ok(x)][-HIST:] if isinstance(vals, list) else []
            if tab is None or mode not in MODE_LABELS or not vals:
                continue
            _hist[(key, mode)] = deque(vals, maxlen=HIST)
            tab[mode] = round(median(vals), 1 if key == "tokens" else 2)
