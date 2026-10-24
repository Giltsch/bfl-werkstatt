"""Live evaluation of intent.understand against Qwen (judge server on :8002) with evalset/cases.json.

Scores every expected field (type, w, h, d, elev, material, handles, ref, full_height per piece; piece count, removes,
drops, questions per case), checks that the ops apply cleanly with projects.apply_ops on a throw-away store, and prints a
per-case table, per-field accuracy and latency. Never touches the real projects store.

    .venv/bin/python tools/eval_understand.py                # all cases, live
    .venv/bin/python tools/eval_understand.py -k korr        # only cases whose id contains "korr"
    .venv/bin/python tools/eval_understand.py --json out.json --profile   # with the company profile module
"""

import argparse
import copy
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import intent    # noqa: E402
import judge     # noqa: E402
import projects  # noqa: E402

PIECE_FIELDS = ("ref", "type", "w", "h", "d", "elev", "material", "handles", "full_height")


def build_doc(case: dict) -> dict:
    spec = case.get("doc") or {}
    pieces = []
    for p in spec.get("pieces") or []:
        pieces.append({"material": "eiche", "handles": None, "text": "", "where": "",
                       "placement": {"mode": "none", "pose": None, "box": None}, **p})
    doc = {"id": f"eval{abs(hash(case['id'])) % 10**8:08d}", "title": f"TEST eval {case['id']}", "rev": 1,
           "pieces": pieces, "frev": {}, "frev_floor": 0}
    if spec.get("room_h"):
        doc["geometry"] = {"ok": True, "ceiling": {"height": spec["room_h"]}, "floor": {}, "scale": 1.0}
    if spec.get("active"):
        doc["active_piece"] = spec["active"]
    return projects.normalize_workshop(doc)


def ops_apply(doc: dict, ops: list, store: Path) -> tuple[bool, str, dict | None]:
    """Applies the ops to a copy of the doc in the throw-away store; (ok, error, saved doc)."""
    if not ops:
        return True, "", doc
    d = copy.deepcopy(doc)
    projects.write_json(store / "workshop" / f"{d['id']}.json", d)
    try:
        new = projects.apply_ops(d["id"], d["rev"], ops, "eval")
        return True, "", new
    except Exception as e:  # HTTPException carries the German message
        return False, str(getattr(e, "detail", e))[:200], None


def match(expected: list[dict], got: list[dict]) -> list[tuple[dict, dict | None]]:
    """Pairs expected with produced pieces: same ref first, then same type, then by order."""
    free = list(got)
    pairs = []
    for e in expected:
        cand = None
        if e.get("ref"):
            cand = next((g for g in free if g.get("ref") == e["ref"]), None)
        if cand is None and e.get("type"):
            cand = next((g for g in free if g.get("type") == e["type"] and (g.get("ref") == e.get("ref"))), None) or \
                next((g for g in free if g.get("type") == e["type"]), None)
        if cand is None and free and not e.get("ref"):
            cand = next((g for g in free if not g.get("ref")), None)
        if cand is not None:
            free.remove(cand)
        pairs.append((e, cand))
    return pairs


def score_case(case: dict, res: dict, ok_ops: bool) -> tuple[dict, list[str]]:
    """{field: [hits, total]} and the list of misses (for the table)."""
    exp = case["expect"]
    s: dict[str, list[int]] = {}
    miss: list[str] = []

    def hit(field, ok, why=""):
        s.setdefault(field, [0, 0])
        s[field][1] += 1
        s[field][0] += bool(ok)
        if not ok:
            miss.append(f"{field}{': ' + why if why else ''}")

    if "pieces" in exp:
        hit("n_pieces", len(res["pieces"]) == len(exp["pieces"]), f"{len(res['pieces'])}≠{len(exp['pieces'])}")
        for e, g in match(exp["pieces"], res["pieces"]):
            for f in PIECE_FIELDS:
                if f in e:
                    v = None if g is None else (g.get(f) if f != "full_height" else bool(g.get(f)))
                    if f == "elev" and not v:
                        v = None if e[f] is None else 0
                    hit(f, g is not None and v == e[f], f"{v!r}≠{e[f]!r}")
    if "removes" in exp:
        hit("removes", len(res["removes"]) == exp["removes"], f"{len(res['removes'])}≠{exp['removes']}")
    if "drops" in exp:
        got = sorted(d["id"] for d in res.get("drops") or [])
        hit("drops", got == sorted(exp["drops"]), f"{got}≠{sorted(exp['drops'])}")
    if "questions" in exp:
        got = sorted(q["field"] for q in res["questions"])
        hit("questions", got == sorted(exp["questions"]), f"{got}≠{sorted(exp['questions'])}")
    if exp.get("questions_any"):
        hit("questions", bool(res["questions"]), "keine Rückfrage")
    if exp.get("warnings_any"):
        hit("warnings", bool(res.get("warnings")), "keine Warnung")
    hit("ops_apply", ok_ops, "apply_ops abgelehnt")
    return s, miss


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only cases whose id contains this")
    ap.add_argument("--cases", default=str(ROOT / "evalset" / "cases.json"))
    ap.add_argument("--json", default="", help="write the raw results here")
    ap.add_argument("--profile", action="store_true", help="use the company profile module (default: built-ins)")
    ap.add_argument("--tmp", default=None, help="directory for the throw-away project store")
    a = ap.parse_args()

    if not judge.status(force=True)["available"]:
        print("Qwen (judge server) nicht erreichbar – Abbruch.")
        return 2
    cases = [c for c in json.loads(Path(a.cases).read_text())["cases"] if a.k in c["id"]]
    prof = intent._profile() if a.profile else None

    tmp = Path(tempfile.mkdtemp(prefix="eval_understand_", dir=a.tmp))
    for k in projects.KINDS:
        (tmp / "projects" / k).mkdir(parents=True)
    projects.STORE, projects.OUT = tmp / "projects", tmp / "outputs"
    projects.UPLOADS, projects.EXPORTS = tmp / "outputs" / "uploads", tmp / "outputs" / "exports"
    projects.job_running = None

    totals: dict[str, list[int]] = {}
    lat, rows, raw, perfect = [], [], [], 0
    print(f"{len(cases)} Fälle, Modell {judge.JUDGE_MODEL}\n")
    for c in cases:
        doc = build_doc(c)
        t0 = time.time()
        res = intent.understand(c["text"], doc, prof)
        dt = time.time() - t0
        ok_ops, err, _ = ops_apply(doc, res["ops"], projects.STORE) if res["status"] == "ok" else (False, res["status"], None)
        sc, miss = score_case(c, res, ok_ops)
        for f, (h, n) in sc.items():
            totals.setdefault(f, [0, 0])
            totals[f][0] += h
            totals[f][1] += n
        if res["status"] == "ok":
            lat.append(dt)
        perfect += not miss
        print(f"{'OK ' if not miss else 'XX '} {c['id']:<24} {dt:5.1f}s  {res['status']:<5} {res['summary_de'][:70]}"
              + (f"\n      ✗ {'; '.join(miss)}" + (f" [{err}]" if err else "") if miss else ""), flush=True)
        raw.append({"id": c["id"], "text": c["text"], "normalized": res.get("text"), "seconds": round(dt, 2),
                    "result": {k: v for k, v in res.items() if k != "doc"}, "misses": miss})
        rows.append((c["id"], not miss))

    print("\nFeld            Treffer   Genauigkeit")
    for f in ("type", "w", "h", "d", "elev", "material", "handles", "ref", "full_height", "n_pieces", "removes", "drops",
              "questions", "warnings", "ops_apply"):
        if f in totals:
            h, n = totals[f]
            print(f"{f:<15} {h:>3}/{n:<4}  {100 * h / n:5.1f} %")
    fields = [f for f in totals if f not in ("ops_apply",)]
    h_all, n_all = sum(totals[f][0] for f in fields), sum(totals[f][1] for f in fields)
    print(f"{'ALLE Felder':<15} {h_all:>3}/{n_all:<4}  {100 * h_all / max(1, n_all):5.1f} %")
    print(f"Fälle komplett richtig: {perfect}/{len(cases)}")
    if lat:
        lat_s = sorted(lat)
        print(f"Latenz: Median {statistics.median(lat):.1f} s, p90 {lat_s[int(0.9 * (len(lat_s) - 1))]:.1f} s, "
              f"max {max(lat):.1f} s, Mittel {statistics.mean(lat):.1f} s (n={len(lat)})")
    if a.json:
        Path(a.json).write_text(json.dumps({"totals": totals, "latency": lat, "cases": raw}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
