"""Dry run of the v3 migration on a copy of a project store; never writes to --src.

    .venv/bin/python tools/migrate_check.py [--src /home/peter_behninger/Dokumente/Projekte/bfl/projects]

Copies the store to a temp dir, runs projects.migrate_all there and checks: valid JSON, version 3, rev >= 1, byte
backup present, status valid, unique refs, next_ref above every ref, variants/pieces/geometry/title/customer equal to
what the v2 code (tests/fixtures/projects_v2.py.txt) makes of the original, a second run is a no-op, and a simulated
rollback round trip (v3 doc -> v2 normalize_workshop -> migrate_all keeps ref, status, activity and raises rev).
Prints a table, exits 1 on any violation.
"""

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import projects  # noqa: E402

V2_SRC = ROOT / "tests" / "fixtures" / "projects_v2.py.txt"
DERIVED_V3 = {"locked"}


def load_v2(tmp: Path):
    """The v2 projects module from a temp copy (its module-level mkdirs land in the temp dir, never in LIVE)."""
    d = tmp / "v2code"
    d.mkdir()
    shutil.copy(V2_SRC, d / "projects_v2.py")
    spec = importlib.util.spec_from_file_location("projects_v2", d / "projects_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.job_running = None
    return mod


def digest(store: Path) -> dict:
    return {str(p.relative_to(store)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(store.rglob("*")) if p.is_file()}


def strip(v: dict) -> dict:
    return {k: x for k, x in v.items() if k not in DERIVED_V3}


def check(src: Path) -> int:
    errors, rows = [], []
    before_src = digest(src)
    with tempfile.TemporaryDirectory(prefix="migrate-check-") as t:
        tmp = Path(t)
        store = tmp / "projects"
        shutil.copytree(src, store, ignore=shutil.ignore_patterns(".secret"))
        (store / "workshop").mkdir(parents=True, exist_ok=True)
        v2 = load_v2(tmp)
        originals = {}
        for f in sorted((store / "workshop").glob("*.json")):
            try:
                originals[f.stem] = (f.read_bytes(), json.loads(f.read_text()))
            except ValueError as e:
                errors.append(f"{f.stem}: invalid JSON before migration ({e})")
        report = {r["id"]: r for r in projects.migrate_all(store)}
        refs = {}
        nr = projects.settings(store)["next_ref"]
        for pid, (raw_bytes, raw) in originals.items():
            f = store / "workshop" / f"{pid}.json"
            err = []
            try:
                doc = json.loads(f.read_text())
            except ValueError as e:
                errors.append(f"{pid}: invalid JSON after migration ({e})")
                continue
            ver = projects._int(raw.get("version"), 1)
            if doc.get("version") != 3:
                err.append("version != 3")
            if not (isinstance(doc.get("rev"), int) and doc["rev"] >= 1):
                err.append("rev < 1")
            if doc.get("status") not in projects.STATUS:
                err.append(f"status {doc.get('status')!r}")
            if ver < 3:
                bk = store / "backup-v2" / "workshop" / f"{pid}.json"
                if not bk.exists() or bk.read_bytes() != raw_bytes:
                    err.append("backup missing or not byte-equal")
                if pid not in report:
                    err.append("not in migrate_all report")
                if sum(a.get("what") == "migrated" for a in doc.get("activity") or []) != 1:
                    err.append("not exactly one 'migrated' activity")
            ref = doc.get("ref") or ""
            if not doc.get("test") and not re.fullmatch(r"\d{4}-\d{3,}", ref):
                err.append(f"ref {ref!r}")
            if ref:
                if ref in refs:
                    err.append(f"ref {ref} also used by {refs[ref]}")
                refs[ref] = pid
                y, n = ref.split("-", 1)
                if n.isdigit() and nr.get(y, 1) <= int(n):
                    err.append(f"next_ref[{y}] = {nr.get(y)} not above {ref}")
            if ver < 3:  # nothing lost relative to the v2 code's view of the original
                old = v2.migrate_workshop(copy.deepcopy(raw))
                for k in ("title", "customer", "geometry", "pieces"):
                    if old.get(k) != doc.get(k):
                        err.append(f"{k} differs from v2")
                if [strip(v) for v in old["variants"]] != [strip(v) for v in doc["variants"]]:
                    err.append("variants differ from v2")
            rows.append([pid, str(ver), doc.get("status"), ref or "-", str(len(doc.get("variants") or [])),
                         str(doc.get("rev")), "ok" if not err else "; ".join(err)])
            errors += [f"{pid}: {e}" for e in err]

        snap = digest(store)
        if projects.migrate_all(store) or digest(store) != snap:
            errors.append("second migrate_all run is not a no-op")

        # rollback round trip: the v2 code saves every doc as version 2 (keeping the v3 keys), then v3 starts again
        nr_before = projects.settings(store)["next_ref"]
        kept = {}
        for f in sorted((store / "workshop").glob("*.json")):
            doc = json.loads(f.read_text())
            kept[f.stem] = {k: copy.deepcopy(doc.get(k)) for k in ("ref", "status", "activity", "rev")}
            back = v2.normalize_workshop(copy.deepcopy(doc))
            if back.get("version") != 2 or any(k not in back for k in ("ref", "status", "activity", "frev")):
                errors.append(f"{f.stem}: v2 normalize dropped v3 keys or kept version 3")
            f.write_text(json.dumps(back, ensure_ascii=False, indent=1))
        projects.migrate_all(store)
        for pid, k in kept.items():
            doc = json.loads((store / "workshop" / f"{pid}.json").read_text())
            bk = store / "backup-v2" / "workshop" / f"{pid}.json"
            if pid in originals and projects._int(originals[pid][1].get("version"), 1) < 3 and bk.read_bytes() != originals[pid][0]:
                errors.append(f"{pid}: rollback overwrote the v2 backup")
            for key in ("ref", "status", "activity"):
                if doc.get(key) != k[key]:
                    errors.append(f"{pid}: rollback round trip changed {key}")
            if doc.get("rev") != k["rev"] + 1 or doc.get("frev_floor") != doc.get("rev"):
                errors.append(f"{pid}: rollback round trip rev {k['rev']} -> {doc.get('rev')}, frev_floor {doc.get('frev_floor')}")
        if projects.settings(store)["next_ref"] != nr_before:
            errors.append("rollback round trip consumed a Vorgangs-Nr.")

    if digest(src) != before_src:
        errors.append("SOURCE STORE CHANGED")
    head = ["id", "from", "status", "ref", "variants", "rev", "check"]
    w = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
    for r in [head] + rows:
        print("  ".join(c.ljust(w[i]) for i, c in enumerate(r)))
    print(f"next_ref: {nr}")
    for e in errors:
        print("VIOLATION:", e)
    print("OK" if not errors else f"{len(errors)} violation(s)")
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, default=ROOT / "projects", help="project store to check (read-only)")
    a = ap.parse_args(argv)
    if not (a.src / "workshop").is_dir():
        print(f"no workshop store under {a.src}", file=sys.stderr)
        return 1
    return check(a.src.resolve())


if __name__ == "__main__":
    sys.exit(main())
