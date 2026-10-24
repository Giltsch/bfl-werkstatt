"""Steps A/B for the `entwurf` preset (room jobs only): do 8 (or 12) FLUX steps grade as well as 16?

    .venv/bin/python tools/ab_steps.py captions --out DIR [--url http://127.0.0.1:8003]   # (a) needs the vLLM pup
    .venv/bin/python tools/ab_steps.py run --out DIR [--arms 16,8] [--live http://127.0.0.1:7860]  # (c)-(f), pup stopped

Rooms: the distinct room photos in outputs/uploads (duplicates and drawn sketches skipped), each with one synthetic
sideboard (180×75×45, 2D box [560, 300, 860, 700], tidy, protect) built by workshop.build_room_job on an in-memory
doc (nothing saved). `captions` writes DIR/captions.json. `run` submits one job at a time to the live server's
POST /api/jobs (caption as prompt, upsample false, the plan's keep, the room photo as ref; seeds --seeds), waits while
a user job is queued, grades every result with judge.grade (live judge, the plan's judge request), writes
DIR/results.json + side-by-side JPEGs, and finally deletes every bench output (DELETE /api/gallery/{id}) and its
outputs/refs/*.png (each path logged) — also after an error. Decision per arm s against 16:
ok(s) ⇔ mean overall(s) ≥ mean overall(16) − 0.3 and pass-rate(s) ≥ pass-rate(16) − 10 pp and mean artifacts(s) ≥
mean artifacts(16) − 0.5.
"""

import argparse
import hashlib
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image, ImageDraw

import workshop

BOX = [560, 300, 860, 700]


def rooms():
    """Distinct photos in outputs/uploads (by content), drawn sketches (mostly near-white) skipped."""
    seen, out = set(), []
    for f in sorted((workshop.OUT / "uploads").glob("*.png")):
        h = hashlib.md5(f.read_bytes()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        with Image.open(f) as im:
            g = im.convert("L").resize((64, 64))
        if sum(1 for v in g.getdata() if v > 235) / 4096 > 0.6:
            continue
        out.append(f"uploads/{f.name}")
    return out


def plan_for(rel):
    with Image.open(workshop.OUT / rel) as im:
        w, h = im.size
    doc = {"id": "abbench00000", "room": rel, "room_size": [w, h], "removes": [], "tidy": True, "protect": True,
           "guide": False, "count": 1, "quality": "fein", "room_changes": "", "judge": {"enabled": True, "auto_retry": False},
           "pieces": [{"id": "pbench", "type": "sideboard", "text": "", "w": 180, "h": 75, "d": 45, "material": "eiche",
                       "material_custom": "", "handles": "push", "sketch": None, "render": None, "use_render": False,
                       "sample": None, "where": "", "placement": {"mode": "2d", "pose": None, "box": BOX}}]}
    return workshop.build_room_job(doc)


def captions(a):
    import upsampler as up
    up.URL = a.url
    if up.status(fresh=True)["state"] != "up":
        sys.exit("pup not up")
    out = []
    for rel in rooms():
        plan = plan_for(rel)
        p = plan["params"]
        r = up.caption({"request": p["prompt"], "images": workshop.load_refs(plan["refs"], plan["roles"]),
                        "boxes": p.get("boxes"), "width": p["width"], "height": p["height"]})
        if r.get("backend") != "vllm":
            sys.exit(f"caption for {rel} not from vLLM: {r}")
        out.append({"room": rel, "caption": r["text"], "seconds": r["seconds"], "tokens": r["tokens"],
                    "width": p["width"], "height": p["height"], "keep": p["keep"], "judge": p["judge"]["request"]})
        print(rel, r["seconds"], "s", r["tokens"], "tokens", flush=True)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "captions.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


def submit(live, params, img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = httpx.post(f"{live}/api/jobs", data={"params": json.dumps(params)}, files=[("refs", ("room.png", buf.getvalue(), "image/png"))],
                   timeout=60)
    r.raise_for_status()
    return r.json()


def wait_idle(live):
    while int(httpx.get(f"{live}/api/status", timeout=10).json().get("queue") or 0) > 0:
        print("live queue busy – waiting", flush=True)
        time.sleep(10)


def render(live, c, steps, seed, room_img):
    wait_idle(live)
    params = {"task": "layout", "prompt": c["caption"], "upsample": False, "mode": "distilled", "steps": steps, "seed": seed,
              "count": 1, "keep": c["keep"], "width": c["width"], "height": c["height"], "source": "bench"}
    job = submit(live, params, room_img)
    while True:
        j = httpx.get(f"{live}/api/jobs/{job['id']}", timeout=10).json()
        if j["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(1)
    if j["state"] != "done" or not j["results"]:
        raise RuntimeError(f"bench job {job['id']}: {j['state']} {j.get('error')}")
    r = j["results"][0]
    return {"id": r["id"], "refs": r.get("refs") or [], "seconds": r.get("seconds")}


def cleanup(live, made, log):
    for m in made:
        try:
            httpx.delete(f"{live}/api/gallery/{m['id']}", timeout=10).raise_for_status()
            log.append(f"deleted output {m['id']}.png/.json")
        except Exception as e:
            log.append(f"FAILED to delete output {m['id']}: {e}")
        for ref in m.get("refs") or []:
            p = (workshop.OUT / ref).resolve()
            if p.parent == (workshop.OUT / "refs").resolve() and p.suffix == ".png":
                p.unlink(missing_ok=True)
                log.append(f"deleted {ref}")
    print("\n".join(log), flush=True)


def summary(rows, arm):
    vs = [r["judge"] for r in rows if r["steps"] == arm and (r.get("judge") or {}).get("status") == "ok"]
    if not vs:
        return None
    mean = lambda xs: round(sum(xs) / len(xs), 3)
    return {"n": len(vs), "overall": mean([v["overall"] for v in vs]), "pass_rate": mean([1.0 if v["pass"] else 0.0 for v in vs]),
            "artifacts": mean([v["scores"]["artifacts"] for v in vs]),
            "room_preserved": mean([v["scores"]["room_preserved"] or 0 for v in vs]),
            "seconds": mean([r["seconds"] or 0 for r in rows if r["steps"] == arm])}


def ok(s, ref):
    return bool(s and ref and s["overall"] >= ref["overall"] - 0.3 and s["pass_rate"] >= ref["pass_rate"] - 0.10
                and s["artifacts"] >= ref["artifacts"] - 0.5)


def side_by_side(out, c, rows, seeds, arms):
    with Image.open(workshop.OUT / c["room"]) as im:
        room = workshop.rgb(im)
    th = 480
    for seed in seeds:
        tiles = [room] + [Image.open(workshop.OUT / f"{r['id']}.png").convert("RGB") for arm in arms for r in rows
                          if r["room"] == c["room"] and r["seed"] == seed and r["steps"] == arm]
        tiles = [t.resize((round(t.width * th / t.height), th)) for t in tiles]
        sheet = Image.new("RGB", (sum(t.width for t in tiles), th + 28), "white")
        x, d = 0, ImageDraw.Draw(sheet)
        for i, t in enumerate(tiles):
            sheet.paste(t, (x, 28))
            r = None if i == 0 else [r for arm in arms for r in rows if r["room"] == c["room"] and r["seed"] == seed and r["steps"] == arm][i - 1]
            label = "Foto" if r is None else f"{r['steps']} Schritte · {(r.get('judge') or {}).get('overall')}"
            d.text((x + 6, 6), label, fill="black")
            x += t.width
        sheet.save(Path(out) / f"{Path(c['room']).stem}_seed{seed}.jpg", quality=85)


def run(a):
    import judge
    out = Path(a.out)
    caps = json.loads((out / "captions.json").read_text())
    arms, seeds = [int(x) for x in a.arms.split(",")], [int(x) for x in a.seeds.split(",")]
    res_path = out / "results.json"
    rows = json.loads(res_path.read_text())["rows"] if res_path.exists() else []
    made, log = [], []
    try:
        for c in caps:
            room_img = workshop.load_refs([c["room"]], ["room"])[0]
            for seed in seeds:
                for arm in arms:
                    if any(r["room"] == c["room"] and r["seed"] == seed and r["steps"] == arm for r in rows):
                        continue
                    m = render(a.live, c, arm, seed, room_img)
                    made.append(m)
                    with Image.open(workshop.OUT / f"{m['id']}.png") as im:
                        v = judge.grade({"room": room_img, "result": im.convert("RGB"), "sketch": None}, c["judge"])
                    rows.append({"room": c["room"], "seed": seed, "steps": arm, "image": m["id"], "id": m["id"], "seconds": m["seconds"],
                                 "judge": {k: v.get(k) for k in ("status", "overall", "pass", "scores", "comment")}})
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
                    res_path.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1))
            side_by_side(out, c, rows, seeds, arms)
    finally:
        cleanup(a.live, made, log)
        (out / "cleanup.log").write_text("\n".join(log))
    sm = {arm: summary(rows, arm) for arm in sorted({r["steps"] for r in rows})}
    decision = {arm: ok(sm[arm], sm.get(16)) for arm in sm if arm != 16}
    res_path.write_text(json.dumps({"rows": rows, "summary": sm, "ok": decision}, ensure_ascii=False, indent=1))
    print(json.dumps({"summary": sm, "ok": decision}, indent=1), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["captions", "run"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8003")
    ap.add_argument("--live", default="http://127.0.0.1:7860")
    ap.add_argument("--arms", default="16,8")
    ap.add_argument("--seeds", default="11,22")
    a = ap.parse_args()
    captions(a) if a.phase == "captions" else run(a)


if __name__ == "__main__":
    main()
