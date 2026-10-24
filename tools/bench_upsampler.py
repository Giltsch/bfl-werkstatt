"""Benchmark + acceptance check of the vLLM prompt upsampler (serve_pup.sh) through upsampler.py, on real order plans.

    .venv/bin/python tools/bench_upsampler.py --url http://127.0.0.1:8003 --plans plan_a.json plan_b.json --out bench.json
        [--live http://127.0.0.1:7860] [--guard-pgid PGID] [--kill-pgid PGID] [--runs 2]

Each plan file is a generate dry-run response ({"plan": {"params", "refs", "roles"}}); its refs are read (read-only)
from outputs/ via workshop.load_refs. Every case runs `--runs` times (cache cleared), then two layout captions
concurrently. Checks per caption: every locked row keeps its exact box, `<moebel_1>` is cited, keep/pixel wording,
the order's cm numbers. Pass: layout ≤ 75 s, edit ≤ 20 s, ≥ 18 tok/s, two concurrent layouts ≤ 75 s, all valid.
Then a cancel against the real server (vllm:num_requests_running must drop to 0 within 2 s of the cancel) and, with
--kill-pgid, a kill of the vLLM process group mid-request (the item must come back as template within 3 s).
--guard-pgid: polls the live studio every 5 s and kills that process group (the bench pup) as soon as a user job is
queued there — a live FLUX image at 1536 px needs 12 GB more than the bench leaves. Exit code 1 if anything fails.
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

import upsampler as up
import workshop
from diffusers.modular_pipelines.flux3 import prompt_upsampler as U

LIMITS = {"i2i_bbox": 75, "i2i": 20, "t2i_bbox": 75, "t2i": 30}
MIN_TOK_S = 18
KEEP_WORDS = re.compile(r"pixel[- ]for[- ]pixel|identical|unchanged|preserv|keep", re.I)


def live_queue(live):
    try:
        return int(httpx.get(f"{live}/api/status", timeout=5).json().get("queue") or 0)
    except Exception:
        return 0


def guard(live, pgid, stop):
    while not stop.is_set():
        if live_queue(live) > 0:
            print(f"GUARD: user job in the live queue – stopping the bench pup (pgid {pgid})", flush=True)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os._exit(3)
        stop.wait(5)


def load_item(path):
    plan = json.load(open(path))["plan"]
    p = plan["params"]
    return {"request": p["prompt"], "images": workshop.load_refs(plan["refs"], plan["roles"]), "boxes": p.get("boxes") or None,
            "width": p["width"], "height": p["height"]}


def check(item, text):
    """{name: bool} validity checks of one caption."""
    out = {"keep_wording": bool(KEEP_WORDS.search(text))}
    cms = set(re.findall(r"(\d+) cm (?:wide|high|deep)", item["request"]))
    out["cm_numbers"] = all(re.search(rf"\b{n}\b", text) for n in cms)
    if item["boxes"]:
        locked = U.boxes_to_canvas([up.to_unit(b) for b in item["boxes"]])
        caption, rows = U.split_caption(text)
        by_id = {r.get("id"): r for r in rows or [] if isinstance(r, dict)}
        out["locked_boxes"] = all(r["id"] in by_id and all(by_id[r["id"]].get(f) == r.get(f) for f in ("bbox", "src_bbox", "tgt_bbox") if f in r)
                                  for r in locked)
        out["moebel_cited"] = all(f"<{r['id']}>" in caption for r in locked if r["id"].startswith("moebel_"))
    return out


def one(item, label):
    up._cache.clear()
    mode = up.mode_of(item["images"], item["boxes"])
    t0 = time.time()
    r = up.caption(item)
    dt = time.time() - t0
    rec = {"case": label, "mode": mode, "backend": r.get("backend"), "seconds": round(dt, 1), "tokens": r.get("tokens"),
           "tok_s": round((r.get("tokens") or 0) / dt, 1) if dt else None, "error": r.get("error")}
    rec["checks"] = check(item, r.get("text") or "") if r.get("text") else {}
    rec["valid"] = rec["backend"] == "vllm" and bool(rec["checks"]) and all(rec["checks"].values())
    rec["pass"] = rec["valid"] and dt <= LIMITS[mode] and (rec["tok_s"] or 0) >= MIN_TOK_S
    rec["caption"] = r.get("text")
    print(json.dumps({k: v for k, v in rec.items() if k != "caption"}), flush=True)
    return rec


def running(url):
    txt = httpx.get(f"{url}/metrics", timeout=5).text
    vals = [float(m) for m in re.findall(r"^vllm:num_requests_running\{[^}]*\}\s+([0-9.e+-]+)", txt, re.M)]
    return sum(vals)


def cancel_test(url, item, after=10.0):
    up._cache.clear()
    ev, res = threading.Event(), {}
    t = threading.Thread(target=lambda: res.update(r=up.caption(item, cancel=ev)))
    t.start()
    time.sleep(after)
    busy = running(url)
    ev.set()
    t0 = time.time()
    t.join(5)
    returned = time.time() - t0
    zero_at = None
    while time.time() - t0 < 5:
        if running(url) == 0:
            zero_at = time.time() - t0
            break
        time.sleep(0.1)
    rec = {"running_before_cancel": busy, "returned_s": round(returned, 2), "zero_after_s": round(zero_at, 2) if zero_at is not None else None,
           "result": {k: v for k, v in (res.get("r") or {}).items() if k != "text"}}
    rec["pass"] = busy >= 1 and (res.get("r") or {}).get("cancelled") is True and zero_at is not None and zero_at <= 2
    print(json.dumps({"cancel": rec}), flush=True)
    return rec


def kill_test(item, pgid, after=5.0):
    up._cache.clear()
    res = {}
    t = threading.Thread(target=lambda: res.update(r=up.caption(item)))
    t.start()
    time.sleep(after)
    os.killpg(pgid, signal.SIGKILL)
    t0 = time.time()
    t.join(30)
    dt = time.time() - t0
    rec = {"returned_s": round(dt, 2), "backend": (res.get("r") or {}).get("backend")}
    rec["pass"] = rec["backend"] == "template" and dt <= 3
    print(json.dumps({"kill": rec}), flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8003")
    ap.add_argument("--plans", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--live", default="http://127.0.0.1:7860")
    ap.add_argument("--guard-pgid", type=int)
    ap.add_argument("--kill-pgid", type=int)
    a = ap.parse_args()
    up.URL = a.url
    stop = threading.Event()
    if a.guard_pgid:
        threading.Thread(target=guard, args=(a.live, a.guard_pgid, stop), daemon=True).start()
    if up.status(fresh=True)["state"] != "up":
        sys.exit(f"pup at {a.url} is not up")
    items = [(Path(p).stem, load_item(p)) for p in a.plans]
    out = {"cases": [], "concurrent": None, "cancel": None, "kill": None, "tokens": {}}
    for _ in range(a.runs):
        for label, it in items:
            out["cases"].append(one(it, label))
    layouts = [it for _, it in items if it["boxes"] and it["images"]]
    if layouts:
        up._cache.clear()
        t0 = time.time()
        rs = up.caption_many([layouts[0], layouts[0]])
        dt = time.time() - t0
        ok = all(r.get("backend") == "vllm" and all(check(layouts[0], r["text"]).values()) for r in rs)
        out["concurrent"] = {"seconds": round(dt, 1), "valid": ok, "pass": ok and dt <= 75, "tokens": [r.get("tokens") for r in rs]}
        print(json.dumps({"concurrent": out["concurrent"]}), flush=True)
        out["cancel"] = cancel_test(a.url, layouts[0])
    for c in out["cases"]:
        if c["tokens"]:
            out["tokens"].setdefault(c["mode"], []).append(c["tokens"])
    out["tokens"] = {m: round(sum(v) / len(v)) for m, v in out["tokens"].items()}
    out["stats"] = up.stats()
    stop.set()
    if a.kill_pgid and layouts:
        out["kill"] = kill_test(layouts[0], a.kill_pgid)
    parts = [c["pass"] for c in out["cases"]] + [x["pass"] for x in (out["concurrent"], out["cancel"], out["kill"]) if x]
    out["pass"] = all(parts)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("PASS" if out["pass"] else "FAIL", "tokens per mode:", out["tokens"], flush=True)
    sys.exit(0 if out["pass"] else 1)


if __name__ == "__main__":
    main()
