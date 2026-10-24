"""Live accuracy check of ground.py on the FLUX evaluation rooms (evalset/ground.json): hit rate per method and latency.
Talks only to the local VLM (judge server, port 8002) and loads SAM on this machine's GPU; nothing else is contacted.

  .venv/bin/python tools/eval_ground.py                       # native pointing, SAM box prompt
  .venv/bin/python tools/eval_ground.py --methods native,grid,grid2 --sam box,point --out /path/report.json
  .venv/bin/python tools/eval_ground.py --only g01,g05 -v
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ground  # noqa: E402
import judge  # noqa: E402

MARGIN = 25   # grid units of slack around the expected region for the "near" rate (a hand-drawn region is not exact)


def load(path: Path) -> dict:
    data = json.loads(path.read_text())
    for r in data["rooms"].values():
        g = ROOT / r["geometry"]
        r["geo"] = json.loads(g.read_text()) if g.is_file() else None
    return data


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    return f"mean {statistics.mean(xs):.2f} s, p50 {statistics.median(xs):.2f} s, max {max(xs):.2f} s"


def rate(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return (sum(bool(v) for v in vals), len(vals))


def fmt(hit):
    k, n = hit
    return f"{k}/{n} = {100 * k / n:.0f} %" if n else "-"


def run(args) -> dict:
    data = load(Path(args.cases))
    cases = data["cases"]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    images = {}
    report = {"place": {}, "object": {}, "cases": []}
    if not judge.status(force=True)["available"]:
        sys.exit("VLM (judge server) not available")
    for method in args.methods.split(","):
        rows = []
        for c in [c for c in cases if c["kind"] == "place"]:
            room = data["rooms"][c["room"]]
            img = images.setdefault(c["room"], ground._image(ROOT / room["image"]))
            res = ground.locate(img, c["phrase"], room["geo"], method=method)
            p = res.get("point")
            row = {"id": c["id"], "method": method, "phrase": c["phrase"], "status": res["status"], "point": p,
                   "hit": bool(p and ground.inside(p, c["expect"])),
                   "near": bool(p and ground.inside(p, c["expect"], MARGIN)), "surface": res.get("surface"),
                   "want_surface": c.get("surface"), "wall": res.get("wall"), "confidence": res.get("confidence"),
                   "why": res.get("why"), "seconds": res.get("seconds")}
            row["surface_ok"] = (row["surface"] == c["surface"]) if c.get("surface") and row["surface"] else None
            rows.append(row)
            if args.verbose:
                print(f'{c["id"]} {method:6} {"HIT " if row["hit"] else "near" if row["near"] else "MISS"} {p} '
                      f'exp {c["expect"]} {res.get("surface")}/{res.get("wall")} {res["seconds"]}s  {c["phrase"]} → {res.get("why")}',
                      flush=True)
        report["place"][method] = {"hit": rate(rows, "hit"), "near": rate(rows, "near"),
                                   "surface_agree": rate(rows, "surface_ok"),
                                   "latency": stats([r["seconds"] for r in rows])}
        report["cases"] += rows
    for prompt in args.sam.split(","):
        rows = []
        if not any(c["kind"] == "object" for c in cases):
            break
        for c in [c for c in cases if c["kind"] == "object"]:
            room = data["rooms"][c["room"]]
            img = images.setdefault(c["room"], ground._image(ROOT / room["image"]))
            res = ground.find_object(img, c["phrase"], refine_with_sam=prompt != "none", sam_prompt=prompt if prompt != "none" else "box")
            p, box = res.get("point"), res.get("box")
            final = ground.union_box(res.get("parts") or [])
            row = {"id": c["id"], "sam": prompt, "phrase": c["phrase"], "status": res["status"], "point": p, "box": box,
                   "hit": bool(p and ground.inside(p, c["expect"])),
                   "vlm_iou": round(ground.iou(box, c["expect"]), 3) if box else 0.0,
                   "final_iou": round(ground.iou(final, c["expect"]), 3) if final else 0.0,
                   "refined": res.get("refined"), "sam_vs_vlm": res.get("iou"), "seconds": res.get("seconds"),
                   "vlm_seconds": (res.get("timing") or {}).get("vlm"), "sam_seconds": (res.get("timing") or {}).get("sam")}
            row["vlm_iou50"] = row["vlm_iou"] >= 0.5
            row["final_iou50"] = row["final_iou"] >= 0.5
            rows.append(row)
            if args.verbose:
                print(f'{c["id"]} sam={prompt:5} {"HIT " if row["hit"] else "MISS"} vlm IoU {row["vlm_iou"]:.2f} final IoU '
                      f'{row["final_iou"]:.2f} refined={row["refined"]} {res["seconds"]}s  {c["phrase"]} → {res.get("label")}',
                      flush=True)
        report["object"][prompt] = {"point_hit": rate(rows, "hit"), "vlm_box_iou50": rate(rows, "vlm_iou50"),
                                    "final_iou50": rate(rows, "final_iou50"),
                                    "mean_vlm_iou": round(statistics.mean(r["vlm_iou"] for r in rows), 3) if rows else None,
                                    "mean_final_iou": round(statistics.mean(r["final_iou"] for r in rows), 3) if rows else None,
                                    "refined": rate(rows, "refined"), "latency": stats([r["seconds"] for r in rows]),
                                    "latency_vlm": stats([r["vlm_seconds"] for r in rows]),
                                    "latency_sam": stats([r["sam_seconds"] for r in rows])}
        report["cases"] += rows
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "evalset" / "ground.json"))
    ap.add_argument("--methods", default="native", help="place methods: native,grid,grid2")
    ap.add_argument("--sam", default="box", help="object refinement: box,point,none")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    report = run(args)
    print("\nPLACE  method        hit (strict)      near (±25)        geometry surface agrees   latency")
    for m, r in report["place"].items():
        print(f"       {m:12}  {fmt(r['hit']):16}  {fmt(r['near']):16}  {fmt(r['surface_agree']):24}  {r['latency']}")
    print("\nOBJECT sam      point hit         VLM box IoU≥.5    final IoU≥.5      mean IoU vlm/final  latency")
    for m, r in report["object"].items():
        print(f"       {m:7}  {fmt(r['point_hit']):16}  {fmt(r['vlm_box_iou50']):16}  {fmt(r['final_iou50']):16}  "
              f"{r['mean_vlm_iou']}/{r['mean_final_iou']}          {r['latency']}")
        print(f"                 VLM part: {r['latency_vlm']};  SAM part: {r['latency_sam']}")
    print(f"\n{round(time.time() - t0)} s total")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
