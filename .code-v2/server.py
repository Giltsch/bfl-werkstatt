"""Local web UI for FLUX 3 Image Klein: one GPU worker, a job queue, and a gallery on disk.

    .venv/bin/python server.py [--host 0.0.0.0] [--port 7860]
"""

import argparse
import copy
import io
import json
import queue
import random
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, PngImagePlugin

import geometry_api
import judge
import projects
import workshop
from engine import MAX_REFERENCES, MODES, Engine, Progress, snap
from keep import keep_outside
from projects import router as projects_router

HERE = Path(__file__).parent
OUT = HERE / "outputs"
REFS = OUT / "refs"
OUT.mkdir(exist_ok=True)
REFS.mkdir(exist_ok=True)

engine = Engine()
app = FastAPI(title="FLUX 3 Klein Studio")
app.include_router(projects_router)
app.include_router(geometry_api.router)
app.include_router(workshop.router)
app.include_router(judge.router)
jobs: dict[str, dict] = {}
work: "queue.Queue[str]" = queue.Queue()
judged: "queue.Queue[str]" = queue.Queue()  # finished jobs waiting for the judge
judge_pool = ThreadPoolExecutor(4)


# ---------------- worker ----------------

def save_ref(img: Image.Image) -> str:
    name = f"{uuid.uuid4().hex[:12]}.png"
    img.save(REFS / name)
    return f"refs/{name}"


def run_job(job: dict):
    p = job["params"]
    images = job["_images"]  # kept: the judge and an auto-retry need the room/sketch refs
    boxes = p.get("boxes") or None  # 0-1000 grid rows from the editor

    def stage(text, done=None, total=None):
        job.update(stage=text, step=done, steps=total)

    caption = p["prompt"].strip()
    if p["upsample"]:
        stage("Upsampling prompt…")
        unit_boxes = [to_unit(b) for b in boxes] if boxes else None
        caption = engine.upsample(caption, images, unit_boxes, p["width"], p["height"])
    elif boxes:
        caption = f"{caption} {json.dumps(boxes, ensure_ascii=False)}"
    job["caption"] = caption

    if job["kind"] == "upsample":
        return

    ref_paths = [save_ref(i) for i in images]
    for n in range(p["count"]):
        seed = p["seed"] + n
        label = f"Image {n + 1}/{p['count']}" if p["count"] > 1 else "Generating"
        stage(f"{label}: loading model…" if p["mode"] not in engine.pipes else label)
        Progress.callback = lambda done, total, label=label: stage(label, done, total)
        t0 = time.time()
        img = engine.generate(caption, images, p["mode"], p["width"], p["height"], p["steps"], p["guidance"], seed)
        Progress.callback = None
        if p.get("keep") and images:
            stage(f"{label}: restoring the unchanged areas…")
            img = keep_outside(img, images[0], p["keep"])
        meta = {
            "id": None, "created": datetime.now().isoformat(timespec="seconds"),
            "task": p["task"], "request": p["prompt"], "caption": caption, "upsampled": p["upsample"],
            "mode": p["mode"], "width": img.width, "height": img.height, "steps": p["steps"],
            "guidance": p["guidance"], "seed": seed, "refs": ref_paths, "boxes": boxes,
            "seconds": round(time.time() - t0, 1), "source": p.get("source"), "keep": p.get("keep"),
            "extra": p.get("meta"), "judge": {"status": "pending"} if p.get("judge") else None,
        }
        meta["id"] = save_output(img, meta)
        job["results"].append(meta)


def keep_regions(v):
    """Optional [[top, left, bottom, right], ...] on the 0-1000 grid: only these areas of the first reference may change."""
    try:
        regions = [[max(0, min(1000, float(x))) for x in r] for r in (v or []) if len(r) == 4]
    except (TypeError, ValueError):
        raise HTTPException(400, "keep must be a list of [top, left, bottom, right] boxes.")
    return regions or None


def to_unit(row: dict) -> dict:
    """An editor row (0-1000) in the 0-1 form diffusers' upsampler takes."""
    u = lambda b: [v / 1000 for v in b] if b else None
    out = {"id": row["id"], "desc": row.get("desc", "")}
    if "bbox" in row:
        out["bbox"] = u(row["bbox"])
    else:
        out.update({"from": row.get("from") or "target", "src_bbox": u(row.get("src_bbox")), "tgt_bbox": u(row.get("tgt_bbox"))})
    return out


def save_output(img: Image.Image, meta: dict) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{meta['seed']}"
    info = PngImagePlugin.PngInfo()
    info.add_text("flux3", json.dumps(meta, ensure_ascii=False))
    img.save(OUT / f"{name}.png", pnginfo=info)
    meta["id"] = name
    (OUT / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return name


def worker():
    while True:
        job_id = work.get()
        job = jobs[job_id]
        if job["state"] == "cancelled":
            job.pop("_images", None)
            if job["kind"] == "generate":
                workshop.record(job)  # drops the id from the project's pending_jobs
            continue
        job.update(state="running", started=time.time())
        state = "error"
        try:
            with engine.lock:
                run_job(job)
            state = "done"
        except Exception as e:
            traceback.print_exc()
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            Progress.callback = None
            torch.cuda.empty_cache()  # unified memory: cached blocks are lost to the judge, Ollama and DepthPro
            # images saved before a failure are recorded and graded too; judge_state is set before the final
            # state, so no poll or project save ever sees a finished job that is about to be graded as done
            to_judge = job["kind"] == "generate" and bool(job["params"].get("judge")) and bool(job["results"])
            if to_judge:
                job["judge_state"] = "pending"  # _images stay for the judge thread
            else:
                job.pop("_images", None)
            job.update(state=state, finished=time.time())
            if job["kind"] == "generate":
                workshop.record(job)  # persists workshop variants (never raises)
            if to_judge:
                judged.put(job_id)


def judge_worker():
    """Grades the results of finished jobs (concurrently, vLLM batches them); one auto-retry if every variant fails."""
    while True:
        job = jobs[judged.get()]
        p, images, cfg = job["params"], job.get("_images") or [], job["params"]["judge"]
        ref = lambda k: images[cfg[k]] if isinstance(cfg.get(k), int) and 0 <= cfg[k] < len(images) else None
        results = job["results"]
        job.update(judge_state="running", judge_stage=f"Prüfung 0/{len(results)}…")

        def one(r):
            with Image.open(OUT / f"{r['id']}.png") as res:
                r["judge"] = judge.grade({"room": ref("room_ref"), "result": res.convert("RGB"), "sketch": ref("sketch_ref")},
                                         cfg.get("request") or {})
            (OUT / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))

        try:
            for i, f in enumerate(as_completed([judge_pool.submit(one, r) for r in results])):
                try:
                    f.result()
                except Exception:
                    traceback.print_exc()
                job["judge_stage"] = f"Prüfung {i + 1}/{len(results)}…"
                workshop.record(job, judge_only=True)
            for r in results:
                if (r.get("judge") or {}).get("status") == "pending":
                    r["judge"] = {"status": "error", "comment": "Bewertung fehlgeschlagen"}
            verdicts = [r["judge"] for r in results]
            if p.get("retry") and job["state"] == "done" and judge.needs_retry(verdicts):
                raw = job["_raw"]
                meta = copy.deepcopy(raw.get("meta") or {})
                meta.setdefault("variant", {})["retry_of"] = job["id"]
                hint = judge.retry_suffix(judge.retry_hint(verdicts))
                follow = enqueue({**raw, "seed": -1, "count": 1, "retry": 0, "retry_of": job["id"], "meta": meta,
                                  "prompt": f"{raw.get('prompt', '')} {hint}".strip()}, images)
                job["retry_job"] = follow["id"]
        except Exception:
            traceback.print_exc()
        finally:
            job.update(judge_state="done", judge_stage=None)
            job.pop("_images", None)
            workshop.record(job, judge_only=True)  # final verdicts; drops the job from pending_jobs, adds retry_job


threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=judge_worker, daemon=True).start()


# ---------------- API ----------------

@app.post("/api/jobs")
async def create_job(params: str = Form(...), refs: list[UploadFile] = File(default=[])):
    if len(refs) > MAX_REFERENCES:
        raise HTTPException(400, f"At most {MAX_REFERENCES} reference images.")
    images = [Image.open(io.BytesIO(await f.read())) for f in refs]
    for im in images:
        im.load()
    return enqueue(json.loads(params), images)


def enqueue(p: dict, images: list[Image.Image]) -> dict:
    """Validate raw job params and queue the job; thread-safe (HTTP threads, the workshop router and the judge thread)."""
    if len(images) > MAX_REFERENCES:
        raise HTTPException(400, f"At most {MAX_REFERENCES} reference images.")
    if not (p.get("prompt") or "").strip() and not p.get("boxes"):
        raise HTTPException(400, "Write a prompt first.")
    mode = p.get("mode") if p.get("mode") in MODES else "distilled"
    seed = int(p.get("seed", -1))
    params = {
        "task": p.get("task", "create"),
        "prompt": p.get("prompt") or "",
        "upsample": bool(p.get("upsample", True)),
        "mode": mode,
        "width": snap(p.get("width", 1024)),
        "height": snap(p.get("height", 1024)),
        "steps": max(1, min(100, int(p.get("steps") or MODES[mode]["steps"]))),
        "guidance": float(p["guidance"]) if mode == "base" and p.get("guidance") is not None else MODES[mode]["guidance"],
        "seed": seed if seed >= 0 else random.randint(0, 2**31 - 1),
        "count": max(1, min(8, int(p.get("count", 1)))),
        "boxes": p.get("boxes") or None,
        "source": str(p["source"])[:60] if p.get("source") else None,
        "keep": keep_regions(p.get("keep")),
        "judge": p["judge"] if isinstance(p.get("judge"), dict) else None,
        "retry": 1 if p.get("retry") else 0,
        "meta": p["meta"] if isinstance(p.get("meta"), dict) else None,
    }
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "id": job_id, "kind": "upsample" if p.get("caption_only") else "generate", "state": "queued",
        "params": params, "stage": "Queued", "step": None, "steps": None, "results": [], "caption": None,
        "error": None, "created": time.time(), "_images": images,
        "judge_state": "off", "judge_stage": None, "retry_job": None, "retry_of": p.get("retry_of"), "_raw": p,
    }
    work.put(job_id)
    return public(jobs[job_id])


workshop.configure(enqueue)
projects.job_running = lambda jid: jid in jobs and not (
    jobs[jid]["state"] in ("done", "error", "cancelled") and jobs[jid]["judge_state"] in ("off", "done"))


def public(job):
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    out["position"] = sum(1 for j in list(jobs.values()) if j["state"] == "queued" and j["created"] < job["created"])
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    return public(jobs[job_id])


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    if job_id in jobs and jobs[job_id]["state"] == "queued":
        jobs[job_id]["state"] = "cancelled"
    return {"ok": True}


@app.get("/api/status")
def status():
    # unified memory: the CUDA free figure leaves out reclaimable page cache, MemAvailable does not
    mem = dict(l.split(":") for l in Path("/proc/meminfo").read_text().splitlines())
    free = int(mem["MemAvailable"].split()[0]) * 1024
    total = int(mem["MemTotal"].split()[0]) * 1024
    return {
        "loaded": {**engine.loaded(), "depth": geometry_api.depth_loaded()}, "engine": engine.status,
        "queue": sum(1 for j in list(jobs.values()) if j["state"] in ("queued", "running")),
        "mem_free_gb": round(free / 2**30, 1), "mem_total_gb": round(total / 2**30, 1),
        "modes": {k: {"steps": v["steps"], "guidance": v["guidance"]} for k, v in MODES.items()},
        "judge": {k: v for k, v in judge.status().items() if k in ("available", "state", "model")},
    }


@app.post("/api/unload/{what}")
def unload(what: str):
    if what == "depth":
        geometry_api.unload()
    else:
        engine.unload(what)
    return status()


@app.get("/api/gallery")
def gallery():
    items = []
    for f in sorted(OUT.glob("*.json"), reverse=True):
        try:
            items.append(json.loads(f.read_text()))
        except Exception:
            pass
    return items


@app.delete("/api/gallery/{item_id}")
def delete_item(item_id: str):
    if "/" in item_id or ".." in item_id:
        raise HTTPException(400)
    for ext in ("png", "json"):
        (OUT / f"{item_id}.{ext}").unlink(missing_ok=True)
    return {"ok": True}


app.mount("/outputs", StaticFiles(directory=OUT), name="outputs")


@app.get("/")
def start_page():
    return RedirectResponse("/werkstatt")  # the workshop page is the start page


@app.get("/studio")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/book")
def book_page():
    return FileResponse(HERE / "static" / "book.html")


@app.get("/werkstatt")
def workshop_page():
    return FileResponse(HERE / "static" / "werkstatt.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def preload(modes):
    with engine.lock:
        for m in modes:
            if m == "pup":
                engine.load_pup()
            else:
                engine.pipe(m)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--preload", default="distilled,pup", help="comma list of distilled,base,pup to load at start ('' for none)")
    args = ap.parse_args()
    if args.preload:
        threading.Thread(target=preload, args=([m for m in args.preload.split(",") if m],), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port)
