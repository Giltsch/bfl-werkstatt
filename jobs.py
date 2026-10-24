"""The GPU scheduler: one worker thread runs caption, image and judge units for everyone, round-robin per person.

Grading runs as a unit between images (FLUX never shares the GPU with the judge), captions of everyone waiting are
fetched together, a starting upsampler never idles the GPU while other people's images could run, jobs survive a
restart (jobs/<id>.json) and every ETA comes from a simulation of the same pick rule. Imports imaging and judge only:
the engine, the upsampler and the project hooks are injected (server.py), so tests run with FakeEngine/FakeUpsampler.
"""

import copy
import io
import json
import math
import os
import random
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from statistics import median

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from PIL import Image, ImageDraw, PngImagePlugin
from starlette.concurrency import run_in_threadpool

import imaging
import judge

FINAL = ("done", "error", "cancelled")
KEEP_S = 24 * 3600      # finished jobs stay queryable this long
KEY_S = 600             # client_key dedupe window
RECENT_S = 600          # finished jobs shown in a project's queue view
PLAN_TTL = 1.0
LOAD_S = 60             # loading a FLUX pipe
JUDGE_S = 25            # judge unit start value (1-4 images)
ALPHA = 0.3
DUR_HIST = 9            # measured job durations kept per (caption mode, steps, count, size) for the estimates
GONE_TEXT = "Auftrag nicht gefunden."


class Cancelled(Exception):
    """Raised from on_step when the running job's cancel (or the drain) event is set."""


class Duplicate(Exception):
    def __init__(self, view: dict):
        super().__init__("duplicate job")
        self.view = view


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_json(path: Path, obj) -> None:
    f = tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False)
    try:
        json.dump(obj, f, ensure_ascii=False, default=str)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        os.replace(f.name, path)
    except BaseException:
        f.close()
        Path(f.name).unlink(missing_ok=True)
        raise


def split_caption(text: str) -> tuple[str, list | None]:
    """diffusers' split_caption: (caption, rows) where rows is the JSON list that closes the text, if any."""
    for i in range(len(text) - 1, -1, -1):
        if text[i] == "[":
            try:
                rows = json.loads(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(rows, list):
                return text[:i].rstrip(), rows
    return text.strip(), None


def dur_key(cmode: str, steps, count, w, h) -> str:
    return f"{cmode}|{steps}|{count}|{w}x{h}"


def fmt_dur(s: float) -> str:
    return f"{max(1, round(s))} s" if s < 60 else f"{max(1, round(s / 60))} Min."


def dur_de(s: float) -> str:
    """„ca. 40 Sek.“, „ca. 3½ Min.“ — half minutes up to 10 Min., whole minutes after."""
    if s < 45:
        return f"ca. {max(10, math.floor(s / 10 + 0.5) * 10)} Sek."
    if s >= 600:
        return f"ca. {round(s / 60)} Min."
    m = math.floor(s / 30 + 0.5) / 2
    return f"ca. {int(m)}{'½' if m % 1 else ''} Min."


def eta_text(images_s: float | None, judge_s: float | None, images_done: bool = False) -> str:
    """The one German time line every client shows, built from the server's plan (images, then the KI grading)."""
    if images_done:
        if not judge_s:
            return ""
        return "Bewertung gleich fertig" if judge_s <= 15 else f"Bewertung in {dur_de(judge_s)}"
    head = "Bilder gleich fertig" if (images_s or 0) <= 15 else f"Bilder in {dur_de(images_s)}"
    return f"{head}, Bewertung {dur_de(judge_s)} danach" if judge_s else head


def keep_regions(v):
    """Optional regions on the 0-1000 grid where the first reference may change: [top, left, bottom, right] boxes or
    {"poly": [[x, y], ...]} outlines (a piece's projected 3d silhouette, 3-32 points); {"box": [...]} is a frame in dict
    form. "remove": true marks an old piece that must disappear (wider margin, see keep.py)."""
    c = lambda x: max(0.0, min(1000.0, float(x)))
    out = []
    try:
        for r in v or []:
            flag = {"remove": True} if isinstance(r, dict) and r.get("remove") is True else {}
            if isinstance(r, dict) and isinstance(r.get("poly"), list) and 3 <= len(r["poly"]) <= 32:
                out.append({"poly": [[c(p[0]), c(p[1])] for p in r["poly"]], **flag})
            elif isinstance(r, dict) and isinstance(r.get("box"), (list, tuple)) and len(r["box"]) == 4:
                out.append({"box": [c(x) for x in r["box"]], **flag})
            elif isinstance(r, (list, tuple)) and len(r) == 4 and not isinstance(r[0], (list, tuple, dict)):
                out.append([c(x) for x in r])
    except (TypeError, ValueError, IndexError):
        raise HTTPException(400, "keep must be a list of [top, left, bottom, right] boxes or {poly} outlines.")
    return out or None


def validate(p: dict, images: list) -> dict:
    """Raw job params → validated params (today's enqueue rules; `caption` = precomputed caption, no upsampling)."""
    if len(images) > imaging.MAX_REFERENCES:
        raise HTTPException(400, f"At most {imaging.MAX_REFERENCES} reference images.")
    caption = p.get("caption") if isinstance(p.get("caption"), str) and p["caption"].strip() else None
    if not (p.get("prompt") or "").strip() and not p.get("boxes") and not caption:
        raise HTTPException(400, "Write a prompt first.")
    mode = p.get("mode") if p.get("mode") in imaging.MODE_DEFAULTS else "distilled"
    d = imaging.MODE_DEFAULTS[mode]
    try:
        seed = int(p.get("seed", -1))
        params = {
            "task": p.get("task", "create"),
            "prompt": p.get("prompt") or "",
            "upsample": bool(p.get("upsample", True)) and caption is None,
            "caption": caption,
            "mode": mode,
            "width": imaging.snap(p.get("width", 1024)),
            "height": imaging.snap(p.get("height", 1024)),
            "steps": max(1, min(100, int(p.get("steps") or d["steps"]))),
            "guidance": float(p["guidance"]) if mode == "base" and p.get("guidance") is not None else d["guidance"],
            "seed": seed if seed >= 0 else random.randint(0, 2**31 - 1),
            "count": max(1, min(8, int(p.get("count", 1)))),
            "boxes": p.get("boxes") or None,
            "source": str(p["source"])[:60] if p.get("source") else None,
            "keep": keep_regions(p.get("keep")),
            "judge": p["judge"] if isinstance(p.get("judge"), dict) else None,
            "retry": 1 if p.get("retry") else 0,
            "meta": p["meta"] if isinstance(p.get("meta"), dict) else None,
        }
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid job parameters.")
    return params


def base_step(w: int, h: int, ref_sizes) -> float:
    """Seconds per FLUX step before calibration (research: 1536 + room ref 3.3 s, 1024 + ref 1.9 s)."""
    tokens = (w * h + sum(min(a * b, imaging.MAX_REF_PIXELS) for a, b in ref_sizes)) / 256
    return max(0.3, 0.27e-3 * tokens - 0.3)


def choose(jobs: list[dict], last_owner, t: float, batch: int = 3):
    """pick_next on light job states — the one rule shared by the worker and the plan simulation.

    A state: id, owner, cls, created, cap (needs a caption), left (images left), done (images saved), judge
    (grading due once no images are left), blocked (caption_wait until this time). Returns None (nothing to do),
    ("wait", until) or (kind, [states]) with kind caption|image|judge."""
    run = [j for j in jobs if j["cap"] or j["left"] > 0 or j["judge"]]
    if not run:
        return None
    free = lambda j: not (j["cap"] and j["blocked"] > t)
    if any(j["cls"] == "fg" and free(j) for j in run):
        run = [j for j in run if j["cls"] == "fg"]
    run.sort(key=lambda j: j["created"])
    ready = [j for j in run if free(j)]
    if not ready:
        return "wait", min(j["blocked"] for j in run)
    owners = list(dict.fromkeys(j["owner"] for j in run))
    have = {j["owner"] for j in ready}
    i0 = owners.index(last_owner) + 1 if last_owner in owners else 0
    owner = next(o for o in (owners[(i0 + i) % len(owners)] for i in range(len(owners))) if o in have)
    mine = [j for j in ready if j["owner"] == owner]
    job = next((j for j in mine if j["done"] or (j["left"] == 0 and j["judge"] and not j["cap"])), mine[0])
    if job["left"] == 0 and job["judge"] and not job["cap"]:
        return "judge", [job]
    if job["cap"]:
        return "caption", ([job] + [j for j in ready if j is not job and j["cap"]])[:batch]
    return "image", [job]


class Scheduler:
    def __init__(self, engine, upsampler, keep_fn, *, out_dir: Path, jobs_dir: Path, hooks: dict | None = None):
        self.engine, self.ups, self.keep_fn = engine, upsampler, keep_fn
        self.out, self.dir = Path(out_dir), Path(jobs_dir)
        self.refs = self.out / "refs"
        for d in (self.out, self.refs, self.dir):
            d.mkdir(parents=True, exist_ok=True)
        self.hooks = hooks or {}
        self.jobs: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._current = None        # {"kind", "jobs": [ids], "owner", "started", "est", "events": {id: Event}}
        self._last_owner = None
        self._gone: set[str] = set()
        self._keys: dict[str, tuple[str, float]] = {}
        self._draining = False
        self._waiting = False
        self._thread = None
        self._plans: dict = {}
        self._pool = ThreadPoolExecutor(4, thread_name_prefix="judge")
        self.k = 1.0                # measured / predicted FLUX step time
        self.judge_s: dict[str, float] = {}
        self.durations: dict[str, deque] = {}  # dur_key -> seconds a job of that kind kept the GPU busy (caption + images)
        self.trace: list[tuple] = []  # (kind, [job ids], start, end) of the last units, for tests and the System page
        self._load_stats()

    # ---------------- submit ----------------

    def submit(self, params: dict, images: list, *, owner: str, owner_name: str = "", project: str | None = None,
               label: str = "", client_key: str | None = None, dedupe: str | None = None, force: bool = False,
               on_accept=None) -> dict:
        """on_accept(job_id) runs once the job is registered but before the worker may pick it (the project's
        pending_jobs entry: a job that ends at once is then still recorded); if it raises, the job is dropped."""
        p = validate(params, images)
        for im in images:
            im.load()
        kind = "upsample" if params.get("caption_only") else "generate"
        t = time.time()
        title = ""
        if project and self.hooks.get("project_title"):
            try:
                title = self.hooks["project_title"](project) or ""
            except Exception:
                pass
        if p["caption"] is not None:
            caption, backend = p["caption"], "reuse"
        elif not p["upsample"]:
            boxes = p["boxes"]
            caption = f"{p['prompt'].strip()} {json.dumps(boxes, ensure_ascii=False)}" if boxes else p["prompt"].strip()
            backend = "template"
        else:
            caption, backend = self._cached(p, images), "cache"  # a known caption: no caption unit, no caption wait
            backend = backend if caption is not None else None
        src = str(p.get("source") or "")
        fg = src.startswith("workshop:") or kind == "upsample" or (
            p["count"] <= 4 and max(p["width"], p["height"]) <= 2048 and p["mode"] == "distilled")
        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id, "kind": kind, "state": "queued", "phase": "waiting",
            "owner": owner, "owner_name": owner_name or owner, "project": project, "project_title": title,
            "label": label, "source": p["source"], "class": "fg" if fg else "bg",
            "count": 0 if kind == "upsample" else p["count"], "width": p["width"], "height": p["height"],
            "stage": "Queued", "step": None, "steps": None, "results": [], "caption": caption,
            "caption_backend": backend, "error": None, "cancelled_by": None,
            "judge_state": "off", "judge_stage": None, "retry_job": None, "retry_of": params.get("retry_of"),
            "created": t, "started": None, "finished": None, "dedupe": dedupe,
            "client_key": client_key, "caption_wait_since": None, "params": p,
            "_raw": params, "_images": list(images), "_refs": [], "_cancel": threading.Event(), "_ready": False,
            "_recovered": False, "_final_at": None,
            "_mode": self.ups.mode_of(images, p["boxes"]),
            "_base": base_step(p["width"], p["height"], [im.size for im in images]),
        }
        dup = None
        with self._lock:
            self._prune(t)
            if project:  # a purge that timed out and was not retried: the order is used again (record raises
                self._gone.discard(project)  # Gone, and the files go, if it is purged after all)
            old = self._keys.get(client_key) if client_key else None
            if old and t - old[1] < KEY_S and old[0] in self.jobs:
                existing = self.jobs[old[0]]
            else:
                existing = None
                if project and dedupe and not force:
                    dup = next((j for j in self._ordered() if j["project"] == project and j["dedupe"] == dedupe
                                and j["state"] in ("queued", "running") and not j["_cancel"].is_set()), None)
                if dup is None:
                    self.jobs[job_id] = job
                    if client_key:
                        self._keys[client_key] = (job_id, t)
        if existing is not None:
            return self._view(existing)
        if dup is not None:
            raise Duplicate(self._view(dup, "lite"))
        try:
            refs = [self._save_ref(im) for im in images]
            if on_accept is not None:
                on_accept(job_id)
        except Exception:
            with self._lock:
                self.jobs.pop(job_id, None)
            raise
        with self._lock:
            job["_refs"], job["_ready"] = refs, True
            cancelled = job["state"] == "cancelled"
            idle = not cancelled and not self._has_work(job)
            if not cancelled and not idle:
                self._persist(job)
            self._changed()
        if cancelled:
            self._settle(job)
        elif idle:  # caption-only job whose caption is already known
            self._finish(job, "done")
        return self._view(job)

    def _cached(self, p: dict, images: list):
        fn = getattr(self.ups, "cached", None)
        if fn is None:
            return None
        try:
            return fn({"request": p["prompt"], "images": images, "boxes": p["boxes"], "width": p["width"], "height": p["height"]})
        except Exception:
            return None

    def exclusive(self, fn):
        """fn() from an HTTP thread that needs the GPU (a regrade, reading a sketch): never alongside a FLUX image."""
        with self.engine.lock:
            try:
                getattr(self.engine, "empty_cache", lambda: None)()
            except Exception:
                pass
            return fn()

    def _save_ref(self, img: Image.Image) -> str:
        name = f"{uuid.uuid4().hex[:12]}.png"
        img.save(self.refs / name)
        return f"refs/{name}"

    def _save_output(self, img: Image.Image, meta: dict) -> str:
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{meta['seed']}"
        if (self.out / f"{name}.png").exists():
            name += f"-{uuid.uuid4().hex[:4]}"
        info = PngImagePlugin.PngInfo()
        meta["id"] = name
        info.add_text("flux3", json.dumps(meta, ensure_ascii=False))
        img.save(self.out / f"{name}.png", pnginfo=info)
        (self.out / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        return name

    # ---------------- job state helpers ----------------

    def _ordered(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: j["created"])

    @staticmethod
    def _left(j) -> int:
        return 0 if j["kind"] == "upsample" else max(0, j["count"] - len(j["results"]))

    def _needs_caption(self, j) -> bool:
        return j["caption"] is None and (j["kind"] == "upsample" or self._left(j) > 0)

    def _has_work(self, j) -> bool:
        return self._needs_caption(j) or self._left(j) > 0 or j["judge_state"] == "pending"

    @staticmethod
    def _active(j) -> bool:
        return j["state"] in ("queued", "running") or j["judge_state"] in ("pending", "running")

    def _runnable(self, j) -> bool:
        return (j["_ready"] and self._active(j) and not j["_cancel"].is_set() and j["project"] not in self._gone
                and self._has_work(j))

    def _executing(self, j) -> bool:
        return bool(self._current) and j["id"] in self._current["jobs"]

    def _changed(self) -> None:
        self._plans.clear()
        self._cv.notify_all()

    def _pup(self, fresh=False) -> dict:
        try:
            return self.ups.status(fresh=fresh) or {}
        except Exception:
            return {"backend": "template", "state": "down", "model": None}

    @staticmethod
    def _judge_available() -> bool:
        try:
            return bool(judge.status().get("available"))
        except Exception:
            return False

    def _start_wait(self) -> float:
        return float(getattr(self.ups, "START_WAIT_S", 240))

    # ---------------- estimates ----------------

    def _img_est(self, j) -> float:
        return 2 + j["params"]["steps"] * self.k * j["_base"] + 6

    def _cap_est(self, mode: str, backend) -> float:
        try:
            return float(self.ups.estimate_s(mode, backend))
        except Exception:
            return 0.0

    def _judge_est(self, n: int) -> float:
        return self.judge_s.get(str(max(1, min(n, 8))), JUDGE_S)

    def judge_estimate(self, n: int) -> float:
        """Seconds the KI grading of n images takes after the last image (0 when the judge is not running)."""
        return round(self._judge_est(n), 1) if self._judge_available() else 0.0

    def estimate(self, params: dict, ref_sizes: list[tuple[int, int]]) -> float:
        """Seconds a job with these raw params would take once started (until its last image is saved)."""
        try:
            mode = params.get("mode") if params.get("mode") in imaging.MODE_DEFAULTS else "distilled"
            w, h = imaging.snap(params.get("width", 1024)), imaging.snap(params.get("height", 1024))
            steps = max(1, min(100, int(params.get("steps") or imaging.MODE_DEFAULTS[mode]["steps"])))
            count = max(1, min(8, int(params.get("count", 1))))
        except (TypeError, ValueError):
            return 0.0
        cap, cmode = 0.0, "none"
        if params.get("upsample", True) and not params.get("caption"):
            backend = self._pup().get("backend")
            cap = self._cap_est(self.ups.mode_of(list(ref_sizes), params.get("boxes")), backend)
            if backend in ("vllm", "local"):
                cmode = self.ups.mode_of(list(ref_sizes), params.get("boxes"))
        if params.get("caption_only"):
            return round(cap, 1)
        load = 0 if mode in self.engine.pipes else LOAD_S
        hist = self.durations.get(dur_key(cmode, steps, count, w, h))
        if hist:  # what such jobs really took lately (rolling median) beats the model
            return round(median(hist) + load, 1)
        img = 2 + steps * self.k * base_step(w, h, ref_sizes) + 6
        return round(cap + count * img + load, 1)

    # ---------------- picking ----------------

    def _state(self, j, pup: dict, t: float, avail: bool) -> dict:
        cap = self._needs_caption(j)
        blocked = 0.0
        if cap and pup.get("state") == "starting":
            blocked = max(0.0, self._start_wait() - (t - (j["caption_wait_since"] or t)))
        planned = (avail and j["kind"] == "generate" and isinstance(j["params"].get("judge"), dict)
                   and j["judge_state"] == "off" and j["state"] in ("queued", "running"))
        return {"id": j["id"], "owner": j["owner"], "cls": j["class"], "created": j["created"], "cap": cap,
                "left": self._left(j), "done": len(j["results"]), "blocked": blocked,
                "judge": j["judge_state"] in ("pending", "running") or planned}

    def _pick(self, pup: dict):
        t = time.time()
        with self._lock:
            if self._draining:
                return None
            cands = [j for j in self._ordered() if self._runnable(j)]
            starting = pup.get("state") == "starting"
            for j in cands:
                if starting and self._needs_caption(j) and j["caption_wait_since"] is None:
                    j["caption_wait_since"] = t
                    self._persist(j)
                elif not starting and j["caption_wait_since"] is not None:  # the next outage waits afresh
                    j["caption_wait_since"] = None
                    self._persist(j)
            states = [self._state(j, pup, t, False) for j in cands]
            for j, s in zip(cands, states):
                if s["cap"] and s["blocked"] > 0:
                    j["phase"] = "caption_wait"
                elif j["phase"] == "caption_wait":
                    j["phase"] = "waiting"
            r = choose(states, self._last_owner, 0, int(getattr(self.ups, "BATCH", 3)))
            if r is None or r[0] == "wait":
                return r
            kind, picked = r
            jobs = [self.jobs[s["id"]] for s in picked]
            if kind == "image":
                j = jobs[0]
                est = self._img_est(j) + (0 if j["params"]["mode"] in self.engine.pipes else LOAD_S)
            elif kind == "caption":
                ests = [self._cap_est(j["_mode"], pup.get("backend")) for j in jobs]
                est = max(ests) * (1 + 0.15 * (len(jobs) - 1))
            else:
                est = self._judge_est(len(jobs[0]["results"]))
            self._last_owner = jobs[0]["owner"]
            self._current = {"kind": kind, "jobs": [j["id"] for j in jobs], "owner": jobs[0]["owner"],
                             "started": t, "est": est, "events": {j["id"]: threading.Event() for j in jobs}}
            for j in jobs:
                if j["_cancel"].is_set():
                    self._current["events"][j["id"]].set()
            self._plans.clear()
            return kind, jobs

    # ---------------- worker ----------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="gpu-worker", daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                if self._draining:
                    return
                fresh = self._waiting
            r = self._pick(self._pup(fresh=fresh))
            if r is None or r[0] == "wait":
                with self._lock:
                    if self._draining:
                        return
                    self._waiting = r is not None
                    self._cv.wait(5 if r is None else min(5.0, max(0.2, r[1])))
                continue
            self._waiting = False
            kind, jobs = r
            t0 = time.time()
            try:
                getattr(self, f"_run_{kind}")(jobs)
            except Exception:
                traceback.print_exc()
            finally:
                with self._lock:
                    t1 = time.time()
                    self.trace = (self.trace + [(kind, [j["id"] for j in jobs], t0, t1)])[-200:]
                    self._current = None
                    if kind in ("caption", "image"):
                        for j in jobs:
                            j["_busy"] = j.get("_busy", 0.0) + (t1 - t0)
                            self._learn_duration(j)
                    self._changed()
                self._save_stats()

    def _learn_duration(self, j):
        """Once all images of a fresh (not recovered) job are saved: its GPU time joins the history of its kind."""
        if j["kind"] != "generate" or j.get("_timed") or j.get("_recovered_once") or len(j["results"]) < j["count"]:
            return
        j["_timed"] = True
        cmode = j["_mode"] if j["caption_backend"] in ("vllm", "local") else "none"
        p = j["params"]
        key = dur_key(cmode, p["steps"], j["count"], p["width"], p["height"])
        self.durations.setdefault(key, deque(maxlen=DUR_HIST)).append(round(j["_busy"], 1))
        while len(self.durations) > 200:
            self.durations.pop(next(iter(self.durations)))

    def _event(self, j) -> threading.Event:
        return self._current["events"][j["id"]]

    def _run_caption(self, jobs: list[dict]):
        evs = [self._event(j) for j in jobs]
        items = [{"request": j["params"]["prompt"], "images": j["_images"], "boxes": j["params"]["boxes"],
                  "width": j["params"]["width"], "height": j["params"]["height"]} for j in jobs]
        with self._lock:
            for j in jobs:
                exp = self._expected(j)
                j.update(state="running", started=j["started"] or time.time(), phase="caption",
                         stage="Upsampling prompt…", step=0, steps=exp, _recovered=False, _tok0=None)

        def progress(i, tokens, expected):
            if 0 <= i < len(jobs):
                if tokens and not jobs[i].get("_tok0"):   # decode rate from the first token on: the prefill is not decoding
                    jobs[i]["_tok0"] = (time.time(), int(tokens))
                jobs[i].update(step=int(tokens), steps=max(int(expected or 0), int(tokens), 1))

        try:
            res = self.ups.caption_many(items, evs, progress)
        except Exception as e:
            traceback.print_exc()
            res = [{"error": f"{type(e).__name__}: {e}"}] * len(jobs)
        for j, r in zip(jobs, res):
            r = r or {}
            if j["_cancel"].is_set():
                self._finish(j, "cancelled")
            elif r.get("cancelled"):  # drain: the caption is fetched again after the restart
                with self._lock:
                    j.update(phase="waiting", stage="Queued", step=None, steps=None)
                    self._persist(j)
            elif r.get("error") or not r.get("text"):
                j["error"] = f"Beschreibung fehlgeschlagen: {r.get('error') or 'leere Antwort'}"
                self._finish(j, "error")
            else:
                with self._lock:
                    j.update(caption=r["text"], caption_backend=r.get("backend"), step=None, steps=None,
                             phase="caption_ready", stage="Queued")
                if j["kind"] == "upsample":
                    self._finish(j, "done")
                else:
                    with self._lock:
                        self._persist(j)

    def _expected(self, j) -> int:
        try:
            return int(self.ups.expected_tokens(j["_mode"]))
        except Exception:
            return 0

    def _run_image(self, jobs: list[dict]):
        job = jobs[0]
        p, ev = job["params"], self._event(job)
        n, N = len(job["results"]), p["count"]
        seed = p["seed"] + n
        label = f"Image {n + 1}/{N}" if N > 1 else "Generating"
        loaded = p["mode"] in self.engine.pipes
        getattr(self.ups, "cancel_prefetch", lambda: None)()  # a caption prefetch would halve FLUX's speed
        with self._lock:
            job.update(state="running", started=job["started"] or time.time(), phase="image" if loaded else "loading",
                       stage=label if loaded else f"{label}: loading model…", step=None, steps=None, _recovered=False)
        marks = {}

        def on_step(done, total):
            if ev.is_set():
                raise Cancelled()
            tt = time.time()
            marks.setdefault("first", (done, tt))
            marks["last"] = (done, tt)
            job.update(phase="image", stage=label, step=done, steps=total)

        t0 = time.time()
        try:
            with self.engine.lock:
                if ev.is_set():
                    raise Cancelled()
                img = self.engine.generate(job["caption"], job["_images"], p["mode"], p["width"], p["height"],
                                           p["steps"], p["guidance"], seed, on_step=on_step)
                if p.get("keep") and job["_images"]:
                    job.update(phase="keep", stage=f"{label}: restoring the unchanged areas…")
                    raw = img
                    img = self.keep_fn(img, job["_images"][0], p["keep"])
                    try:   # the unblended FLUX image: lets the blending be re-done or checked later
                        (self.out / "raw").mkdir(exist_ok=True)
                        raw.save(self.out / "raw" / f"{job['id']}-{seed}.png")
                    except Exception:
                        traceback.print_exc()
        except Cancelled:
            return self._stopped(job)
        except Exception as e:
            traceback.print_exc()
            job["error"] = f"{type(e).__name__}: {e}"
            return self._finish(job, "error")
        finally:
            try:
                getattr(self.engine, "empty_cache", lambda: None)()  # unified memory: cached blocks are lost to the judge
            except Exception:
                pass
        if "first" in marks and marks["last"][0] - marks["first"][0] >= 2:
            (d0, t_0), (d1, t_1) = marks["first"], marks["last"]
            ratio = (t_1 - t_0) / (d1 - d0) / job["_base"]
            self.k = max(0.02, min(50.0, (1 - ALPHA) * self.k + ALPHA * ratio))
        meta = {
            "id": None, "created": now_iso(), "task": p["task"], "request": p["prompt"], "caption": job["caption"],
            "upsampled": p["upsample"], "mode": p["mode"], "width": img.width, "height": img.height,
            "steps": p["steps"], "guidance": p["guidance"], "seed": seed, "refs": job["_refs"], "boxes": p["boxes"],
            "seconds": round(time.time() - t0, 1), "source": p.get("source"), "keep": p.get("keep"),
            "extra": p.get("meta"), "judge": {"status": "pending"} if p.get("judge") else None,
            "job": job["id"], "owner": job["owner"], "caption_backend": job["caption_backend"],
        }
        try:
            self._save_output(img, meta)
        except Exception as e:  # disk full, permissions: an error, never the same image again and again
            traceback.print_exc()
            job["error"] = f"Bild konnte nicht gespeichert werden: {type(e).__name__}: {e}"
            return self._finish(job, "error")
        with self._lock:
            job["results"].append(meta)
            gone = job["project"] in self._gone
            self._persist(job)
        if gone:
            return self._purge_job(job)
        if not self._record(job):
            return
        if len(job["results"]) >= N:  # a cancel after the last step: the images are complete, only grading is dropped
            self._finish(job, "done", skip_judge=job["_cancel"].is_set())
        elif ev.is_set():
            self._stopped(job)
        else:
            with self._lock:
                job.update(phase="waiting", stage="Queued", step=None, steps=None)

    def _run_judge(self, jobs: list[dict]):
        job = jobs[0]
        p, ev = job["params"], self._event(job)
        cfg, images, results = p.get("judge") or {}, job["_images"] or [], list(job["results"])
        ref = lambda k: images[cfg[k]] if isinstance(cfg.get(k), int) and 0 <= cfg[k] < len(images) else None
        getattr(self.ups, "cancel_prefetch", lambda: None)()
        with self._lock:
            job.update(phase="judge", judge_state="running", judge_stage=f"Prüfung 0/{len(results)}…")

        def one(r):
            with Image.open(self.out / f"{r['id']}.png") as res:
                r["judge"] = judge.grade({"room": ref("room_ref"), "result": res.convert("RGB"),
                                          "sketch": ref("sketch_ref"), "plan": ref("plan_ref")}, cfg.get("request") or {})
            (self.out / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))

        t0, n = time.time(), 0
        pending = {self._pool.submit(one, r) for r in results}
        while pending and not ev.is_set():
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    f.result()
                except Exception:
                    traceback.print_exc()
                n += 1
                job["judge_stage"] = f"Prüfung {n}/{len(results)}…"
                if not self._record(job, judge_only=True):
                    return
        cancelled = job["_cancel"].is_set()
        if pending and not cancelled:  # drain: graded again after the restart
            with self._lock:
                job.update(judge_state="pending", judge_stage=None, phase="judge_wait")
                self._persist(job)
            return
        for r in results:
            if (r.get("judge") or {}).get("status") == "pending":
                r["judge"] = {"status": "error", "comment": "Prüfung abgebrochen" if cancelled else "Bewertung fehlgeschlagen"}
        if not cancelled:
            key = str(max(1, min(len(results), 8)))
            self.judge_s[key] = round((1 - ALPHA) * self.judge_s.get(key, JUDGE_S) + ALPHA * (time.time() - t0), 1)
        verdicts = [r["judge"] for r in results]
        if not cancelled and p.get("retry") and job["state"] == "done" and job["caption"] and judge.needs_retry(verdicts):
            try:
                job["retry_job"] = self._retry(job, verdicts)["id"]
            except Exception:
                traceback.print_exc()
        with self._lock:
            job.update(judge_state="done", judge_stage=None, phase=job["state"])
        self._record(job, judge_only=True)
        self._settle(job)

    def _retry(self, job: dict, verdicts: list[dict]) -> dict:
        """One follow-up image whose caption is the old one with the judge's correction (no caption unit)."""
        raw = job["_raw"] or {}
        meta = copy.deepcopy(raw.get("meta") or {})
        if not isinstance(meta.get("variant"), dict):
            meta["variant"] = {}
        meta["variant"]["retry_of"] = job["id"]
        caption = self.ups.retry_caption(job["caption"], judge.retry_hint(verdicts))
        params = {**raw, "seed": -1, "count": 1, "retry": 0, "retry_of": job["id"], "meta": meta,
                  "caption": caption, "upsample": False}
        return self.submit(params, job["_images"] or [], owner=job["owner"], owner_name=job["owner_name"],
                           project=job["project"], label="Neuer Versuch mit Korrektur", dedupe=job["dedupe"], force=True)

    # ---------------- finishing ----------------

    def _stopped(self, job: dict):
        """A unit stopped by an event: the owner's cancel → cancelled; the drain → stays queued for the restart."""
        if job["_cancel"].is_set():
            return self._finish(job, "cancelled")
        with self._lock:
            job.update(phase="waiting", stage="Queued", step=None, steps=None)
            self._persist(job)

    def _finish(self, job: dict, state: str, skip_judge: bool = False):
        wants = (state != "cancelled" and job["kind"] == "generate" and bool(job["results"])
                 and isinstance(job["params"].get("judge"), dict) and job["project"] not in self._gone)
        avail = wants and not skip_judge and self._judge_available()
        unavailable = []
        with self._lock:
            if job["state"] in FINAL and job["judge_state"] not in ("pending", "running"):
                return
            if wants and avail:
                job.update(judge_state="pending", phase="judge_wait")
            else:
                if wants and skip_judge:
                    for r in job["results"]:
                        if (r.get("judge") or {}).get("status") == "pending":
                            r["judge"] = {"status": "error", "comment": "Prüfung abgebrochen"}
                            unavailable.append(r)
                    job.update(judge_state="done", cancelled_by=None)
                elif wants:  # judge down: verdicts say so at once, no unit
                    for r in job["results"]:
                        if (r.get("judge") or {}).get("status") == "pending":
                            r["judge"] = {"status": "unavailable", "model": None, "scores": None, "overall": None,
                                          "pass": None, "comment": "Prüfer nicht verfügbar", "fix_hint": "",
                                          "seconds": 0.0, "graded": now_iso()}
                            unavailable.append(r)
                    job["judge_state"] = "unavailable"
                job["phase"] = state
            if state == "cancelled" and not job["cancelled_by"]:
                job["cancelled_by"] = "System"
            job.update(state=state, finished=time.time(), step=None, steps=None)
            self._changed()
        for r in unavailable:
            try:
                (self.out / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
            except OSError:
                pass
        if not self._record(job):
            return
        self._settle(job)

    def _settle(self, job: dict):
        """Persist an unfinished job; forget the heavy parts and the file of a finished one."""
        with self._lock:
            if not (job["state"] in FINAL and job["judge_state"] not in ("pending", "running")):
                self._persist(job)
                return
            job.update(_images=None, _raw=None, _final_at=job["_final_at"] or time.time())
            self._changed()
        (self.dir / f"{job['id']}.json").unlink(missing_ok=True)
        if not job["results"]:
            self._unlink_refs(job)

    def _unlink_refs(self, job: dict):
        for r in job["_refs"]:
            if r.startswith("refs/") and "/" not in r[5:] and ".." not in r:
                (self.out / r).unlink(missing_ok=True)

    def _record(self, job: dict, judge_only: bool = False) -> bool:
        """hooks["record"]; False when the project is gone (the job's files are deleted then)."""
        if job["kind"] != "generate" or not self.hooks.get("record"):
            return True
        if job["project"] in self._gone:
            self._purge_job(job)
            return False
        try:
            self.hooks["record"](job, judge_only)
        except HTTPException as e:
            if e.status_code != 404:
                traceback.print_exc()
                return True
            with self._lock:
                if job["project"]:
                    self._gone.add(job["project"])
            self._purge_job(job)
            return False
        except Exception:
            traceback.print_exc()
        return True

    def _purge_job(self, job: dict):
        """A job of a purged order: stop it and delete every file it saved (outputs, refs, job file)."""
        with self._lock:
            job["_cancel"].set()
            if self._executing(job):
                self._current["events"][job["id"]].set()
            if job["state"] not in FINAL:
                job.update(state="cancelled", phase="cancelled", finished=time.time(),
                           cancelled_by=job["cancelled_by"] or "System")
            job.update(judge_state="done" if job["judge_state"] != "off" else "off", judge_stage=None,
                       _images=None, _raw=None, _final_at=job["_final_at"] or time.time())
            results, job["results"] = job["results"], []
            self._changed()
        for r in results:
            for ext in ("png", "json"):
                (self.out / f"{r['id']}.{ext}").unlink(missing_ok=True)
        self._unlink_refs(job)
        (self.dir / f"{job['id']}.json").unlink(missing_ok=True)

    # ---------------- cancel ----------------

    def cancel(self, job_id: str, by: str, by_name: str = "", force: bool = False) -> dict:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, GONE_TEXT)
            if by != job["owner"] and not force:
                return {"ok": False, "code": "not_owner", "owner_name": job["owner_name"],
                        "message": f"Dieser Auftrag gehört {job['owner_name']}. Trotzdem abbrechen?"}
            if not self._active(job) or (job["_cancel"].is_set() and not self._executing(job)):
                return {"ok": True, "state": job["state"]}
            job["_cancel"].set()
            if job["state"] not in FINAL:
                job["cancelled_by"] = by_name or ("System" if by == "system" else by)
            executing = self._executing(job)
            if executing:
                self._current["events"][job_id].set()
            self._changed()
        if executing:
            return {"ok": True, "state": "cancelling"}
        if job["state"] in FINAL:  # images complete, only grading was left
            self._skip_judge(job)
        else:
            self._finish(job, "cancelled")
        return {"ok": True, "state": job["state"]}

    def _skip_judge(self, job: dict):
        with self._lock:
            for r in job["results"]:
                if (r.get("judge") or {}).get("status") == "pending":
                    r["judge"] = {"status": "error", "comment": "Prüfung abgebrochen"}
            job.update(judge_state="done", judge_stage=None, phase=job["state"])
        self._record(job, judge_only=True)
        self._settle(job)

    def cancel_project(self, project, by="system", timeout=30) -> bool:
        """Purge: cancel all of the project's jobs and wait until none runs; later results are deleted."""
        with self._lock:
            self._gone.add(project)
            mine = [j for j in self._ordered() if j["project"] == project]
        for j in mine:
            if self._active(j):
                self.cancel(j["id"], by, "System" if by == "system" else by, force=True)
        deadline = time.time() + timeout
        with self._lock:
            while any(self._executing(j) for j in mine) and time.time() < deadline:
                self._cv.wait(0.1)
            return not any(self._executing(j) for j in mine)

    # ---------------- persistence ----------------

    def _persist(self, job: dict):
        if not job["_ready"] or (job["state"] in FINAL and job["judge_state"] not in ("pending", "running")):
            return
        d = {k: job[k] for k in ("id", "kind", "created", "owner", "owner_name", "project", "label", "client_key",
                                 "class", "dedupe", "caption_wait_since", "params", "state", "caption",
                                 "caption_backend", "judge_state", "retry_of", "retry_job", "error", "cancelled_by")}
        d.update(raw=job["_raw"], refs=job["_refs"], done=[r["id"] for r in job["results"]])
        try:
            write_json(self.dir / f"{job['id']}.json", d)
        except Exception:
            traceback.print_exc()

    def recover(self) -> list[str]:
        """Before start(): requeue every persisted job with its remaining images / grading."""
        found = []
        for f in self.dir.glob("*.json"):
            if f.name == "stats.json":
                continue
            try:
                d = json.loads(f.read_text())
                found.append((d.get("created") or 0, f, d))
            except Exception:
                print(f"jobs: unreadable {f.name}", flush=True)
        ids = []
        for _, f, d in sorted(found, key=lambda x: x[0]):
            try:
                if self._recover_one(d):
                    ids.append(d["id"])
            except Exception:
                traceback.print_exc()
        return ids

    def _recover_one(self, d: dict) -> bool:
        p = d["params"]
        results = []
        for rid in d.get("done") or []:
            try:
                results.append(json.loads((self.out / f"{rid}.json").read_text()))
            except Exception:
                pass
        if d.get("kind") != "upsample":  # images saved after the last write (crash between save and persist)
            have = {r.get("id") for r in results}
            for n in range(len(results), p["count"]):
                hit = None
                for g in sorted(self.out.glob(f"*-{p['seed'] + n}*.json")):
                    try:
                        m = json.loads(g.read_text())
                    except Exception:
                        continue
                    if m.get("job") == d["id"] and m.get("seed") == p["seed"] + n and m.get("id") not in have:
                        hit = m
                        break
                if hit is None:
                    break
                results.append(hit)
        images, missing = [], False
        for r in d.get("refs") or []:
            try:
                with Image.open(self.out / r) as im:
                    im.load()
                    images.append(im.copy())
            except Exception:
                missing = True
        job = {
            "id": d["id"], "kind": d.get("kind") or "generate", "state": d.get("state") or "queued", "phase": "waiting",
            "owner": d.get("owner") or "anon", "owner_name": d.get("owner_name") or d.get("owner") or "",
            "project": d.get("project"), "project_title": "", "label": d.get("label") or "",
            "source": p.get("source"), "class": d.get("class") or "fg",
            "count": 0 if d.get("kind") == "upsample" else p["count"], "width": p["width"], "height": p["height"],
            "stage": "Queued", "step": None, "steps": None, "results": results, "caption": d.get("caption"),
            "caption_backend": d.get("caption_backend"), "error": d.get("error"), "cancelled_by": d.get("cancelled_by"),
            "judge_state": d.get("judge_state") or "off", "judge_stage": None, "retry_job": d.get("retry_job"),
            "retry_of": d.get("retry_of"), "created": d.get("created") or time.time(), "started": None,
            "finished": None, "dedupe": d.get("dedupe"), "client_key": d.get("client_key"),
            "caption_wait_since": None, "params": p,  # a restart waits afresh for a starting upsampler
            "_raw": d.get("raw") or {}, "_images": images, "_refs": d.get("refs") or [], "_cancel": threading.Event(),
            "_ready": True, "_recovered": True, "_recovered_once": True, "_final_at": None,
            "_mode": self.ups.mode_of(images, p.get("boxes")),
            "_base": base_step(p["width"], p["height"], [im.size for im in images]),
        }
        if job["project"] and self.hooks.get("project_title"):
            try:
                job["project_title"] = self.hooks["project_title"](job["project"]) or ""
            except Exception:
                pass
        with self._lock:
            self.jobs[job["id"]] = job
        if job["judge_state"] == "running":
            job["judge_state"] = "pending"
        if missing and job["state"] not in FINAL:
            job["params"] = {**p, "judge": None}
            job["error"] = "Vorlage fehlt – bitte neu starten"
            self._finish(job, "error")
            return False
        if job["state"] in FINAL:
            if job["judge_state"] == "pending" and results and job["state"] != "cancelled" and not missing:
                job["phase"] = "judge_wait"
                self._persist(job)
                self._record(job)
                return True
            job["judge_state"] = "done" if job["judge_state"] != "off" else "off"
            self._record(job)
            self._settle(job)
            return False
        if not self._has_work(job):
            self._finish(job, "done")
            return False
        job["state"] = "running" if results else "queued"
        job["phase"] = "waiting"
        self._persist(job)
        self._record(job)
        if job["project"] and self.hooks.get("log"):
            try:
                self.hooks["log"](job["project"], "recovered", "Berechnung nach Neustart fortgesetzt", "system")
            except Exception:
                pass
        return True

    def drain(self, timeout: float = 200) -> None:
        """Shutdown: no new units; the current image may finish (≤ timeout), a caption or judge unit is abandoned;
        everything unfinished is persisted and recomputed after the restart."""
        with self._lock:
            self._draining = True
            cur = self._current
            if cur and cur["kind"] != "image":
                for ev in cur["events"].values():
                    ev.set()
            self._cv.notify_all()
        deadline = time.time() + timeout
        with self._lock:
            while self._current is not None and time.time() < deadline:
                self._cv.wait(0.1)
            if self._current is not None:  # the image is cancelled and recomputed after the restart
                for ev in self._current["events"].values():
                    ev.set()
                end = time.time() + 10
                while self._current is not None and time.time() < end:
                    self._cv.wait(0.1)
            for j in self._ordered():
                if j["_ready"] and self._active(j):
                    self._persist(j)
        self._save_stats()

    def _load_stats(self):
        try:
            d = json.loads((self.dir / "stats.json").read_text())
        except Exception:
            return
        self.k = float(d.get("k") or 1.0)
        self.judge_s = {str(k): float(v) for k, v in (d.get("judge_s") or {}).items()}
        for k, vals in (d.get("durations") or {}).items():
            vals = [float(x) for x in vals if isinstance(x, (int, float)) and 0 < x < 1e5] if isinstance(vals, list) else []
            if vals:
                self.durations[str(k)] = deque(vals[-DUR_HIST:], maxlen=DUR_HIST)
        try:
            self.ups.load_stats(d.get("upsampler") or {})
        except Exception:
            pass

    def _save_stats(self):
        try:
            ups = self.ups.stats()
        except Exception:
            ups = {}
        try:
            with self._lock:
                durs = {k: list(q) for k, q in self.durations.items()}
            write_json(self.dir / "stats.json", {"k": round(self.k, 4), "judge_s": self.judge_s, "durations": durs, "upsampler": ups})
        except Exception:
            traceback.print_exc()

    def _prune(self, t: float):
        for jid in [k for k, j in self.jobs.items() if j["_final_at"] and t - j["_final_at"] > KEEP_S]:
            del self.jobs[jid]
        for k in [k for k, (_, kt) in self._keys.items() if t - kt > KEY_S]:
            del self._keys[k]

    # ---------------- plan, ETA, views ----------------

    def _plan(self, extra_owner: str | None = None) -> dict:
        key = extra_owner if extra_owner is not None else "\0"
        hit = self._plans.get(key)
        if hit and time.time() - hit[0] < PLAN_TTL:
            return hit[1]
        pup, avail = self._pup(), self._judge_available()
        with self._lock:
            t = time.time()
            self._prune(t)
            cands = [j for j in self._ordered() if self._runnable(j)]
            states = {j["id"]: self._state(j, pup, t, avail) for j in cands}
            costs = {}
            for j in cands:
                costs[j["id"]] = {"img": self._img_est(j), "mode": j["params"]["mode"],
                                  "cap": self._cap_est(j["_mode"], "vllm" if states[j["id"]]["blocked"] else pup.get("backend")),
                                  "judge": self._judge_est(j["count"] or 1)}
            pipes = set(self.engine.pipes)
            units, tt, last = [], 0.0, self._last_owner
            cur = self._current
            if cur:
                rem = self._remaining(cur, t)
                units.append((cur["kind"], list(cur["jobs"]), 0.0, rem))
                for jid in cur["jobs"]:
                    if jid in states:
                        self._apply(cur["kind"], states[jid])
                if cur["kind"] == "image" and cur["jobs"][0] in self.jobs:
                    pipes.add(self.jobs[cur["jobs"][0]]["params"]["mode"])
                tt, last = rem, cur["owner"]
            if extra_owner is not None:
                states["\0v"] = {"id": "\0v", "owner": extra_owner, "cls": "fg", "created": t + 1e6, "cap": False,
                                 "left": 1, "done": 0, "blocked": 0.0, "judge": False}
                costs["\0v"] = {"img": 0.0, "mode": "distilled", "cap": 0.0, "judge": 0.0}
            batch = int(getattr(self.ups, "BATCH", 3))
            for _ in range(2000):
                r = choose(list(states.values()), last, tt, batch)
                if r is None:
                    break
                if r[0] == "wait":
                    tt = max(r[1], tt + 0.01)
                    continue
                kind, picked = r
                c = costs[picked[0]["id"]]
                if kind == "image":
                    dur = c["img"] + (0 if c["mode"] in pipes else LOAD_S)
                    pipes.add(c["mode"])
                elif kind == "caption":
                    dur = max(costs[s["id"]]["cap"] for s in picked) * (1 + 0.15 * (len(picked) - 1))
                else:
                    dur = c["judge"]
                units.append((kind, [s["id"] for s in picked], tt, tt + dur))
                for s in picked:
                    self._apply(kind, s)
                last, tt = picked[0]["owner"], tt + dur
                if extra_owner is not None and picked[0]["id"] == "\0v":
                    break
            info, order = {}, []
            for kind, ids, s0, s1 in units:
                for jid in ids:
                    if jid not in info:
                        before = [x for x in order if x != jid]
                        info[jid] = {"start": s0, "done": 0.0, "judged": 0.0, "ahead": before}
                        order.append(jid)
                    j = self.jobs.get(jid)
                    if kind == "image" or (kind == "caption" and j is not None and j["kind"] == "upsample"):
                        info[jid]["done"] = s1
                    elif kind == "judge":
                        info[jid]["judged"] = s1
            if cur:
                for jid in cur["jobs"]:
                    if jid in info:
                        info[jid].update(start=0.0, ahead=[])
            plan = {"t": t, "units": units, "info": info, "order": order,
                    "end": max((u[3] for u in units), default=0.0),
                    "wait": info["\0v"]["start"] if "\0v" in info else 0.0, "pup": pup, "judge": avail}
            self._plans[key] = (time.time(), plan)
            return plan

    @staticmethod
    def _apply(kind: str, s: dict):
        if kind == "caption":
            s["cap"] = False
        elif kind == "image":
            s["left"], s["done"] = max(0, s["left"] - 1), s["done"] + 1
        else:
            s["judge"] = False

    def _remaining(self, cur: dict, t: float) -> float:
        rem = cur["est"] - (t - cur["started"])
        j = self.jobs.get(cur["jobs"][0])
        if cur["kind"] == "caption" and j is not None and j["step"] and j["steps"] and t - cur["started"] > 5:
            # measured token rate; a caption longer than usual keeps a small tail instead of freezing at „1 s“
            t0, s0 = j.get("_tok0") or (cur["started"], 0)
            rate = (j["step"] - s0) / (t - t0) if t - t0 > 2 and j["step"] > s0 else j["step"] / (t - cur["started"])
            rem = max(j["steps"] - j["step"], 0.08 * j["step"]) / max(rate, 0.1)
        elif cur["kind"] == "image" and j is not None:
            if j["phase"] == "keep":
                rem = 3
            elif j["steps"] and j["step"] is not None:
                rem = (j["steps"] - j["step"]) * self.k * j["_base"] + 6
        return max(3.0, rem)

    def wait_s(self, owner: str | None = None) -> float:
        return round(self._plan(owner if owner is not None else "\0new")["wait"])

    def _view(self, job: dict, kind: str = "full", plan: dict | None = None) -> dict:
        plan = plan or self._plan()
        t = time.time()
        with self._lock:
            out = {k: v for k, v in job.items()
                   if not k.startswith("_") and k not in ("params", "client_key", "caption_wait_since")}
            out["results"] = list(job["results"])
            info = plan["info"].get(job["id"])
            executing = self._executing(job)
            el = t - plan["t"]
            if executing:
                pos, ahead, start = 0, [], 0.0
            elif info:
                pos, ahead, start = len(info["ahead"]), info["ahead"], max(0.0, info["start"] - el)
            else:
                pos, ahead, start = 0, [], 0.0
            done = max(0.0, info["done"] - el) if info and info["done"] else 0.0
            judged = max(0.0, info["judged"] - el) if info and info.get("judged") else 0.0
            names = []
            for jid in ahead[:3]:
                a = self.jobs.get(jid)
                if a is not None:
                    names.append({"owner_name": a["owner_name"], "project_title": a["project_title"], "label": a["label"]})
            elapsed = ((job["finished"] or t) - job["started"]) if job["started"] else 0
            images_done = len(job["results"]) >= (job["count"] or 0) or job["state"] in FINAL
            prog = 1.0 if images_done else min(0.97, elapsed / (elapsed + done)) if job["started"] and elapsed + done > 0 else 0.0
            out.update(done_images=len(job["results"]), position=pos, ahead=names, eta_start_s=round(start),
                       eta_done_s=round(done), eta_judge_s=round(judged), elapsed_s=round(elapsed), progress=round(prog, 3))
            grading = job["judge_state"] in ("pending", "running")
            out["eta_text"] = (eta_text(None, judged, True) if images_done and grading else
                               eta_text(done, max(0.0, judged - done) if judged else 0.0) if self._active(job) and not images_done else "")
            out["stage_text"] = self._stage_text(job, pos, start, executing)
        if kind == "full":
            return out
        if kind == "lite":
            for k in ("results", "caption", "source"):
                out.pop(k, None)
            return out
        out.pop("caption", None)  # sync: light results, never captions or prompts
        out["results"] = [{k: r.get(k) for k in ("id", "width", "height", "seed", "judge")} for r in out["results"]]
        return out

    def _stage_text(self, j: dict, pos: int, start: float, executing: bool) -> str:
        st, ph = j["state"], j["phase"]
        if st == "cancelled":
            return f"Abgebrochen von {j['cancelled_by'] or 'System'}"
        if st == "error":
            return f"Fehler: {j['error'] or 'unbekannt'}"
        if j["_cancel"].is_set() and self._active(j):
            return "Berechnung wird abgebrochen …"
        p = pos + 1  # Platz 1 = running now (same count as the 409 „läuft schon“ message)
        if j["judge_state"] == "running":
            return "Qualitätsprüfung …"
        if j["judge_state"] == "pending":
            return f"Fertig – Qualitätsprüfung folgt (Platz {p})"
        if st == "done":
            n = len(j["results"])
            if j["kind"] == "upsample":
                return "✓ Fertig"
            return "✓ Fertig – 1 Vorschlag" if n == 1 else f"✓ Fertig – {n} Vorschläge"
        if executing and ph == "caption":  # one overall bar and eta_text; never a „gleich fertig“ that means only this phase
            return "Beschreibung wird vorbereitet …"
        if executing and ph == "loading":
            return "Modell wird geladen …"
        if executing and ph == "image":
            n, N = len(j["results"]) + 1, j["count"]
            head = "Bild wird gerechnet …" if N == 1 else f"Bild {n} von {N} wird gerechnet …"
            return f"{head} Schritt {j['step']}/{j['steps']}" if j["steps"] else head
        if executing and ph == "keep":
            return "Raum wird wiederhergestellt …"
        if ph == "caption_wait":
            waited = time.time() - (j["caption_wait_since"] or time.time())
            if waited >= self._start_wait():
                return "Ohne Beschreibungs-KI weiter …"
            return "Beschreibungs-KI startet – andere Bilder laufen solange …"
        if ph == "caption_ready":
            return f"Beschreibung fertig – wartet auf den Rechner (Platz {p})"
        if j["_recovered"]:
            return "Nach Neustart fortgesetzt"
        if start < 15:
            return "Startet gleich …"
        return f"In der Warteschlange – Platz {p}, Start in ca. {max(1, round(start / 60))} Min."

    def get(self, job_id) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None or (job["_final_at"] and time.time() - job["_final_at"] > KEEP_S):
            return None
        return self._view(job)

    def view(self, owner: str | None, project: str | None) -> dict:
        plan = self._plan()
        wait_s = self.wait_s(owner)
        t = time.time()
        with self._lock:
            active = [j for j in self._ordered() if j["_ready"] and self._active(j)]
            rank = {jid: i for i, jid in enumerate(plan["order"])}
            active.sort(key=lambda j: (0 if self._executing(j) else 1, rank.get(j["id"], 1e9), j["created"]))
            cur = self.jobs.get(self._current["jobs"][0]) if self._current else None
            recent = [j for j in self._ordered() if project and j["project"] == project and j["_ready"]
                      and (self._active(j) or (j["_final_at"] and t - j["_final_at"] < RECENT_S))]
            busy = bool(self._current) or any(self._runnable(j) for j in active)
        pup = plan["pup"] or {}
        return {
            "busy": busy, "wait_s": wait_s, "eta_total_s": round(max(0.0, plan["end"] - (t - plan["t"]))),
            "pup": {"backend": pup.get("backend"), "state": pup.get("state")}, "judge": {"available": plan["judge"]},
            "running": self._view(cur, "lite", plan) if cur else None,
            "jobs": [self._view(j, "lite", plan) for j in active],
            "my_jobs": [self._view(j, "lite", plan) for j in active if owner is not None and j["owner"] == owner],
            "project_jobs": [self._view(j, "sync", plan) for j in recent],
        }

    def running(self, job_id) -> bool:
        j = self.jobs.get(job_id)
        return j is not None and (not j["_ready"] or self._active(j))

    def status(self) -> dict:
        with self._lock:
            cur = self.jobs.get(self._current["jobs"][0]) if self._current else None
            active = [j for j in self.jobs.values() if j["_ready"] and self._active(j)]
            busy = cur is not None or any(self._runnable(j) for j in active)
        return {"busy": busy, "queue": len(active), "phase": cur["phase"] if cur else "idle", "wait_s": self.wait_s()}


# ---------------- HTTP ----------------

router = APIRouter()
scheduler: Scheduler | None = None  # set by the server


def _sched() -> Scheduler:
    if scheduler is None:
        raise HTTPException(503, "Server noch nicht bereit.")
    return scheduler


def _who(request: Request) -> tuple[str, str]:
    owner = getattr(request.state, "owner", None) or "anon"
    return owner, getattr(request.state, "owner_name", None) or ""


@router.post("/api/jobs")
async def create_job(request: Request, params: str = Form(...), refs: list[UploadFile] = File(default=[])):
    s = _sched()
    if len(refs) > imaging.MAX_REFERENCES:
        raise HTTPException(400, f"At most {imaging.MAX_REFERENCES} reference images.")
    try:
        p = json.loads(params)
        assert isinstance(p, dict)
    except Exception:
        raise HTTPException(400, "params must be a JSON object.")
    images = []
    for f in refs:
        data = await f.read(imaging.MAX_UPLOAD + 1)
        if len(data) > imaging.MAX_UPLOAD:
            raise HTTPException(413, f"Reference image too large (max. {imaging.MAX_UPLOAD // 2**20} MB).")
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:
            raise HTTPException(400, "Unreadable reference image.")
        images.append(im)
    owner, name = _who(request)
    return await run_in_threadpool(s.submit, p, images, owner=owner, owner_name=name, label=str(p.get("label") or "")[:80])


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    v = _sched().get(job_id)
    if v is None:
        raise HTTPException(404, GONE_TEXT)
    return v


@router.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, request: Request, force: int = 0):
    owner, name = _who(request)
    r = _sched().cancel(job_id, owner, name, bool(force))
    if r.get("code") == "not_owner":
        raise HTTPException(403, {k: r[k] for k in ("code", "owner_name", "message")})
    return r


@router.get("/api/queue")
def queue_view(request: Request):
    return _sched().view(_who(request)[0], None)


# ---------------- fakes (tests, --fake-gpu dry runs) ----------------

class FakeEngine:
    def __init__(self, step_s: float = 0.15):
        self.lock = threading.Lock()
        self.pipes = {"distilled": True}
        self.status = "idle"
        self.step_s = step_s
        self.intervals: list[tuple[float, float]] = []
        self.calls: list[dict] = []

    def loaded(self):
        return {"distilled": "distilled" in self.pipes, "base": "base" in self.pipes, "pup": False}

    def generate(self, prompt, images, mode, width, height, steps, guidance, seed, on_step=None):
        t0 = time.time()
        try:
            if mode not in self.pipes:
                time.sleep(self.step_s)
                self.pipes[mode] = True
            if on_step:
                on_step(0, steps)
            for i in range(steps):
                time.sleep(self.step_s)
                if on_step:
                    on_step(i + 1, steps)
            g = Image.linear_gradient("L").resize((width, height))
            img = Image.merge("RGB", (g, g.transpose(Image.Transpose.FLIP_LEFT_RIGHT), Image.new("L", g.size, 140)))
            d = ImageDraw.Draw(img)
            d.text((width // 2 - 40, height // 2), f"TEST {seed}", fill=(255, 255, 255))
            return img
        finally:
            t1 = time.time()
            self.intervals.append((t0, t1))
            self.calls.append({"prompt": prompt, "seed": seed, "width": width, "height": height, "steps": steps,
                               "start": t0, "end": t1})

    def unload(self, what=None):
        pass

    def empty_cache(self):
        pass


class FakeUpsampler:
    START_WAIT_S = 240
    BATCH = 3

    def __init__(self, delay: float = 1.0, state: str = "up"):
        self.delay, self.state = delay, state
        self.batches: list[int] = []
        self._stats: dict = {}

    def status(self, fresh=False):
        return {"backend": "vllm" if self.state == "up" else "template", "state": self.state, "model": "fake"}

    @staticmethod
    def mode_of(images, boxes):
        return ("i2i" if images else "t2i") + ("_bbox" if boxes else "")

    @staticmethod
    def _template(item):
        boxes = item.get("boxes")
        return f"{item['request']} {json.dumps(boxes, ensure_ascii=False)}" if boxes else item["request"]

    def caption_many(self, items, cancels=None, on_progress=None):
        self.batches.append(len(items))
        if self.state != "up":
            return [{"text": self._template(it), "backend": "template", "seconds": 0.0, "tokens": None, "cached": False}
                    for it in items]
        out, live, t0 = [None] * len(items), set(range(len(items))), time.time()
        while live:
            el = time.time() - t0
            for i in sorted(live):
                if cancels and cancels[i] is not None and cancels[i].is_set():
                    out[i] = {"error": "abgebrochen", "cancelled": True}
                    live.discard(i)
                elif el >= self.delay:  # the fake vLLM keeps a layout table last, like reconcile_boxes
                    out[i] = {"text": "[fake] " + self._template(items[i]), "backend": "vllm",
                              "seconds": round(el, 2), "tokens": 100, "cached": False}
                    live.discard(i)
                elif on_progress:
                    on_progress(i, int(100 * el / self.delay), 100)
            if live:
                time.sleep(0.05)
        return out

    def caption(self, item, cancel=None, on_progress=None):
        return self.caption_many([item], [cancel], on_progress and (lambda i, a, b: on_progress(0, a, b)))[0]

    def retry_caption(self, caption: str, hint: str) -> str:
        text, rows = split_caption(caption)
        suffix = judge.retry_suffix(hint)
        tail = [json.dumps(rows, ensure_ascii=False)] if rows else []
        return " ".join(x for x in [text, suffix, *tail] if x)

    def estimate_s(self, mode=None, backend=None):
        return 0.0 if backend == "template" else self.delay

    def expected_tokens(self, mode=None):
        return 100

    def cache_has(self, item):
        return False

    def stats(self):
        return dict(self._stats)

    def load_stats(self, d):
        self._stats = dict(d or {})
