"""Werkstatt + studio server: routers, the GPU scheduler (jobs.py) and the customer-link app (public.py).

    .venv/bin/python server.py [--host 0.0.0.0] [--port 7860] [--preload distilled] [--fake-gpu] [--public-port 7861]
"""

import argparse
import json
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import agent
import backup
import channels
import connect
import delivery
import geometry
import geometry_api
import ground
import imaging
import intent
import jobs
import judge
import live
import profile  # the company profile (profile.py shadows the stdlib module on purpose, see there)
import projects
import public
import team
import upsampler
import voice
import workshop
from keep import keep_outside

HERE = Path(__file__).parent
DATA = Path(os.environ.get("FLUX_DATA") or HERE)
OUT = DATA / "outputs"
(OUT / "refs").mkdir(parents=True, exist_ok=True)
geometry_api.OUT, geometry_api.GEO_DIR = OUT, OUT / "geometry"
geometry.GEO_DIR, workshop.OUT = OUT / "geometry", OUT
ground.OUT = OUT


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--preload", default="distilled", help="comma list of FLUX modes (distilled,base) to load at start")
    ap.add_argument("--fake-gpu", action="store_true", help="FakeEngine + FakeUpsampler (dry runs, no GPU)")
    ap.add_argument("--public-port", type=int, default=7861, help="customer-link app on 127.0.0.1 (0 = off)")
    return ap.parse_known_args(argv)[0]


args = parse_args()
if args.fake_gpu:
    engine = jobs.FakeEngine(step_s=float(os.environ.get("FLUX_FAKE_STEP_S") or 0.15))
    ups = jobs.FakeUpsampler(delay=float(os.environ.get("FLUX_FAKE_CAPTION_S") or 1.0))
else:
    from engine import Engine
    engine = Engine()
    upsampler.configure(engine)
    ups = upsampler


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_event(pid, what, text, who):
    try:
        projects.mutate("workshop", pid, lambda d: projects.log(d, what, text, who), who, auto=True)
    except HTTPException:
        pass


sched = jobs.Scheduler(engine, ups, keep_outside, out_dir=OUT, jobs_dir=DATA / "jobs",
                       hooks={"record": workshop.record, "project_title": projects.title, "log": log_event})
jobs.scheduler = sched
workshop.configure(sched.submit, estimate=sched.estimate, wait=sched.wait_s, exclusive=sched.exclusive,
                   busy=lambda: sched.status()["busy"], judge_est=sched.judge_estimate)
projects.job_running = sched.running
live.queue_view = sched.view
delivery.jobs_hook = lambda pid: sched.cancel_project(pid, by="system", timeout=30)


def preload(modes):
    for m in modes:
        if m in imaging.MODE_DEFAULTS:
            try:
                with engine.lock:
                    engine.pipe(m)
            except Exception:
                traceback.print_exc()


def start_public(port):
    server = uvicorn.Server(uvicorn.Config(public.public_app, host="127.0.0.1", port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True, name="public").start()


def backup_loop():
    time.sleep(120)  # let the models load first
    while True:
        try:
            if not sched.status()["busy"]:
                backup.maybe_run()
        except Exception:
            traceback.print_exc()
        time.sleep(3600)


def start_front_doors():
    """Channels (Telegram, e-mail) and webhooks need agent.py. Both stay off unless the owner entered credentials and
    switched them on, so with the default config this starts nothing and contacts nobody."""
    try:
        import agent
    except ImportError:
        print("channels: agent.py missing, not started", flush=True)
        return
    try:
        print("agent:", agent.recover(), flush=True)   # follow running generations again, unstick interrupted steps
    except Exception:
        traceback.print_exc()
    try:
        if callable(getattr(agent, "subscribe", None)):  # optional in the agent contract
            agent.subscribe(connect.on_agent_event)
        print("channels:", channels.start_all(agent, out_dir=OUT) or "none enabled", flush=True)
    except Exception:
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app):
    print("migration:", projects.migrate_all(), flush=True)  # backup + v3, before anything is served
    print("recovered jobs:", sched.recover(), flush=True)
    sched.start()
    live.started = now()
    if args.preload and not args.fake_gpu:
        threading.Thread(target=preload, args=([m for m in args.preload.split(",") if m],), daemon=True).start()
    if args.public_port:
        start_public(args.public_port)
    threading.Thread(target=backup_loop, daemon=True, name="backup").start()
    start_front_doors()
    yield
    await run_in_threadpool(channels.stop_all)
    await run_in_threadpool(connect.stop)
    await run_in_threadpool(agent.stop)
    await run_in_threadpool(sched.drain, 200)


app = FastAPI(title="Werkstatt", lifespan=lifespan)
team.install(app)
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, voice.router, intent.router, ground.router, profile.router,
          channels.router, connect.router, connect.v1, agent.router):
    app.include_router(r)


@app.get("/api/status")
def status():
    # unified memory: the CUDA free figure leaves out reclaimable page cache, MemAvailable does not
    mem = dict(l.split(":") for l in Path("/proc/meminfo").read_text().splitlines())
    free = int(mem["MemAvailable"].split()[0]) * 1024
    total = int(mem["MemTotal"].split()[0]) * 1024
    pup = {k: v for k, v in ups.status().items() if k in ("backend", "state", "model")}
    s = sched.status()
    last = backup.status()["last"]
    return {
        "loaded": {**engine.loaded(), "pup": pup.get("state") == "up", "depth": geometry_api.depth_loaded(),
                   "voice": voice.loaded()},
        "engine": engine.status, "queue": s["queue"],
        "mem_free_gb": round(free / 2**30, 1), "mem_total_gb": round(total / 2**30, 1),
        "modes": {k: {"steps": v["steps"], "guidance": v["guidance"]} for k, v in imaging.MODE_DEFAULTS.items()},
        "judge": {k: v for k, v in judge.status().items() if k in ("available", "state", "model")},
        "pup": pup, "busy": s["busy"], "wait_s": round(s["wait_s"]),
        "backup": {"at": last.get("at"), "ok": last.get("ok")} if last else None,
        "version": live.VERSION, "started": live.started,
    }


@app.post("/api/unload/{what}")
def unload(what: str):
    if what == "depth":
        geometry_api.unload()
    elif what == "pup":  # the upsampler is the flux-pup service; the in-process one only exists with FLUX_PUP_LOCAL=1
        if getattr(engine, "pup", None) is not None:
            engine.unload("pup")
        getattr(ups, "stop", lambda: None)()
    elif what == "voice":
        voice.unload()
    elif what in ("distilled", "base"):
        engine.unload(what)
    else:
        raise HTTPException(400, "Unknown model.")
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


@app.get("/aufnahme")
def recorder_page():
    return FileResponse(HERE / "static" / "aufnahme.html", headers={"Cache-Control": "no-cache"})


@app.get("/book")
def book_page():
    return FileResponse(HERE / "static" / "book.html")


@app.get("/werkstatt")
def workshop_page():
    return FileResponse(HERE / "static" / "ws" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port)
