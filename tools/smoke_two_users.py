"""Two devices against a running Werkstatt server (API level): the CONCURRENCY scenarios S1–S7, queue fairness/cancel,
parallel versions and draft sheets. TEST projects only, purged at the end.

    .venv/bin/python tools/smoke_two_users.py --base http://127.0.0.1:7870 [--images 1]
"""
import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:7870")
ap.add_argument("--images", type=int, default=1, help="images per GPU job (0 = skip the scenarios that need a job)")
args = ap.parse_args()
A = httpx.Client(base_url=args.base, timeout=600)   # device A (tablet)
B = httpx.Client(base_url=args.base, timeout=600)   # device B (office PC)
created = {"workshop": [], "book": []}
R = {}


def log(k, ok, msg):
    R[k] = {"ok": bool(ok), "msg": msg}
    print(f"[{'ok' if ok else 'PROBLEM'}] {k}: {msg}", flush=True)


def doc(c, pid):
    return c.get(f"/api/projects/workshop/{pid}").json()


def patch(c, pid, base_rev, *ops):
    return c.patch(f"/api/projects/workshop/{pid}", json={"base_rev": base_rev, "ops": list(ops)})


def put_(path, value):
    return {"op": "set", "path": path, "value": value}


def new_project(title, **kw):
    d = A.post("/api/projects/workshop", json={"title": title, **kw}).json()
    created["workshop"].append(d["id"])
    return d


def wait_job(c, jid, limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] in ("done", "error", "cancelled") and j.get("judge_state") not in ("pending", "running"):
            return j
        time.sleep(1.5)
    raise TimeoutError(jid)


def typed(pid, title="Sideboard"):
    d = doc(A, pid)
    p = d["pieces"][0]["id"]
    r = patch(A, pid, d["rev"], put_(f"pieces.{p}.type", "sideboard"), put_(f"pieces.{p}.w", 180),
              put_("setting", "heller Raum mit Holzboden"), put_("count", 1))
    r.raise_for_status()
    return p


def documents():
    d = new_project("TEST smoke dokument")
    pid, rev = d["id"], d["rev"]
    p = typed(pid)
    rev = doc(A, pid)["rev"]
    # S1: different fields from the same base → both persist
    ra = patch(A, pid, rev, put_("title", "TEST smoke (A)"), put_("customer", "Familie Müller (A)"), put_(f"pieces.{p}.w", 222))
    rb = patch(B, pid, rev, put_("room_changes", "B: Wand hell streichen"))
    s = doc(A, pid)
    log("S1 different fields merge", ra.status_code == rb.status_code == 200 and s["title"] == "TEST smoke (A)"
        and s["pieces"][0]["w"] == 222 and s["room_changes"].startswith("B:"),
        f"A {ra.status_code}, B {rb.status_code}; title={s['title']!r} w={s['pieces'][0]['w']} room_changes={s['room_changes']!r}")
    # same field → 409 with readable conflict data, nothing applied
    rev = s["rev"]
    patch(A, pid, rev, put_(f"pieces.{p}.w", 240)).raise_for_status()
    rb = patch(B, pid, rev, put_(f"pieces.{p}.w", 220), put_("ext_ref", "B-1"))
    det = rb.json().get("detail", {}) if rb.status_code == 409 else {}
    c = (det.get("conflicts") or [{}])[0]
    s = doc(A, pid)
    log("S1b same field → conflict", rb.status_code == 409 and det.get("code") == "conflict" and c.get("path") == f"pieces.{p}.w"
        and c.get("mine") == 220 and c.get("theirs") == 240 and s["pieces"][0]["w"] == 240 and s["ext_ref"] == "",
        f"B got {rb.status_code} {det.get('code')} {c}; server w={s['pieces'][0]['w']} ext_ref={s['ext_ref']!r} (nothing applied)")
    # S2: A adds a piece, B (stale) edits something else → piece stays
    rev = s["rev"]
    patch(A, pid, rev, {"op": "add_piece", "value": {"id": "smk2pc", "type": "regal", "material": "nuss", "handles": "push",
                                                      "placement": {"mode": "none", "pose": None, "box": None}}}).raise_for_status()
    rb = patch(B, pid, rev, put_("internal_notes", "B notiert etwas"))
    s = doc(A, pid)
    log("S2 added piece survives a stale edit", rb.status_code == 200 and [x["id"] for x in s["pieces"]] == [p, "smk2pc"],
        f"B {rb.status_code}; pieces={[x['id'] for x in s['pieces']]}")
    # S5: B's job request built from an old state → 409 changed (A changed the design meanwhile); A's own request works
    old = s["rev"]
    patch(A, pid, old, put_(f"pieces.{p}.type", "kueche"), put_(f"pieces.{p}.material", "weiss")).raise_for_status()
    rb = B.post(f"/api/workshop/{pid}/generate", json={"kind": "free", "base_rev": old, "piece_id": p, "client_key": "smoke-s5"})
    if rb.status_code == 200:
        B.delete(f"/api/jobs/{rb.json()['job']['id']}?force=1")
    det = rb.json().get("detail", {}) if rb.status_code == 409 else {}
    ra = A.post(f"/api/workshop/{pid}/generate", json={"kind": "free", "dry_run": True, "piece_id": p})
    prompt = ra.json().get("plan", {}).get("params", {}).get("prompt", "") if ra.status_code == 200 else ""
    log("S5 job never built from someone else's state", rb.status_code == 409 and det.get("code") == "changed" and "kitchen" in prompt.lower(),
        f"B (stale) → {rb.status_code} {det.get('code')} „{det.get('message', '')}“; A → {ra.status_code}, prompt: {prompt[:70]!r}")
    # stale base below frev_floor is never silently accepted: an old PUT is refused outright
    r = A.put(f"/api/projects/workshop/{pid}", json=s)
    log("S1c whole-doc PUT retired", r.status_code == 409, f"PUT → {r.status_code} {r.json().get('detail')!r}")
    return pid


def books():
    bk = A.post("/api/projects/book", json={"title": "TEST smoke book", "pages": [{"text": "x" * 200, "image": None} for _ in range(40)]}).json()
    bid = bk["id"]
    created["book"].append(bid)

    def one(i):
        d = {**bk, "title": f"TEST smoke book {i}", "pages": [{"text": f"{i}-" + "y" * 300, "image": None} for _ in range(40)]}
        try:
            return (A if i % 2 else B).put(f"/api/projects/book/{bid}", json=d).status_code
        except Exception as e:
            return repr(e)
    codes = []
    for _ in range(5):
        with ThreadPoolExecutor(16) as ex:
            codes += list(ex.map(one, range(16)))
    r = A.get(f"/api/projects/book/{bid}")
    bad = [c for c in codes if c != 200]
    log("S6 80 concurrent book saves", not bad and r.status_code == 200 and len(r.json()["pages"]) == 40,
        f"{len(codes)} PUTs, non-200: {len(bad)} {sorted(set(map(str, bad)))[:3]}; GET {r.status_code}, rev {r.json().get('rev')}")
    b1, b2 = A.get(f"/api/projects/book/{bid}").json(), B.get(f"/api/projects/book/{bid}").json()
    b1["pages"][3]["image"] = "TESTpage.png"
    A.put(f"/api/projects/book/{bid}", json=b1)
    b2["title"] = "TEST smoke book (B)"
    B.put(f"/api/projects/book/{bid}", json=b2)
    lost = A.get(f"/api/projects/book/{bid}").json()["pages"][3]["image"] != "TESTpage.png"
    log("S7 book stale save (known: book keeps whole-doc saves)", True,
        f"page image {'lost' if lost else 'kept'} after a stale save – the book page was not redesigned (spec §4.1)")


def jobs_and_delivery():
    a = new_project("TEST smoke A", customer="Familie A")
    b = new_project("TEST smoke B", customer="Familie B")
    pa, pb = typed(a["id"]), typed(b["id"])
    # dedupe: both devices press „Bilder berechnen“ within a second → one job, the other gets 409 running
    body = lambda pid, p: {"kind": "free", "piece_id": p, "count": args.images, "client_key": f"k-{time.time()}-{threading.get_ident()}"}
    with ThreadPoolExecutor(2) as ex:
        r1, r2 = list(ex.map(lambda c: c.post(f"/api/workshop/{a['id']}/generate", json=body(a["id"], pa)), (A, B)))
    codes = sorted([r1.status_code, r2.status_code])
    dup = r1 if r1.status_code == 409 else r2
    det = dup.json().get("detail", {}) if dup.status_code == 409 else {}
    log("S8 double start from two devices", codes == [200, 409] and det.get("code") == "running",
        f"codes {codes}; „{det.get('message', '')}“")
    ja = (r1 if r1.status_code == 200 else r2).json()["job"]
    jb = B.post(f"/api/workshop/{b['id']}/generate", json=body(b["id"], pb)).json()["job"]
    q = B.get("/api/queue").json()
    mine = next((j for j in q["jobs"] if j["id"] == jb["id"]), {})
    log("Q1 queue position + ETA + who is ahead", mine.get("position", 0) >= 1 and mine.get("eta_start_s", 0) > 0 and mine.get("ahead"),
        f"B's job: position {mine.get('position')}, eta_start {mine.get('eta_start_s')} s, eta_done {mine.get('eta_done_s')} s, "
        f"ahead {mine.get('ahead')}, „{mine.get('stage_text')}“")
    # cancel someone else's job: 403 not_owner unless forced
    owner_of_a = (A if r1.status_code == 200 else B)
    other = B if owner_of_a is A else A
    extra = owner_of_a.post(f"/api/workshop/{a['id']}/generate", json={**body(a["id"], pa), "force": True}).json()["job"]
    r = other.delete(f"/api/jobs/{extra['id']}")
    det = r.json().get("detail", {}) if r.status_code == 403 else {}
    r2 = other.delete(f"/api/jobs/{extra['id']}?force=1")
    log("Q2 cancel someone else's job asks first", r.status_code == 403 and det.get("code") == "not_owner" and r2.status_code == 200,
        f"without force {r.status_code} „{det.get('message', '')}“; with force {r2.status_code} {r2.json()}")
    # A edits the project while its job records images and verdicts → never a conflict
    stop, errs = threading.Event(), []

    def edit_loop():
        i = 0
        while not stop.is_set():
            d = doc(owner_of_a, a["id"])
            r = patch(owner_of_a, a["id"], d["rev"], put_("internal_notes", f"Notiz {i}"))
            if r.status_code != 200:
                errs.append(r.status_code)
            i += 1
            time.sleep(0.7)
    th = threading.Thread(target=edit_loop)
    th.start()
    fa = wait_job(owner_of_a, ja["id"])
    fb = wait_job(B, jb["id"])
    stop.set()
    th.join()
    da = doc(A, a["id"])
    log("S9 job records while a person edits", not errs and len(da["variants"]) >= args.images and fa["state"] == "done",
        f"edits during the job: non-200 {errs}; variants {len(da['variants'])}, job {fa['state']} ({fa.get('caption_backend')}), "
        f"B's job {fb['state']} started {round((fb.get('started') or 0) - (fb.get('created') or 0))} s after submit")
    v = da["variants"][0]["id"]
    # S3: A stars a proposal, B (stale) edits the title → star stays
    rev = da["rev"]
    patch(A, a["id"], rev, put_(f"variants.{v}.fav", True)).raise_for_status()
    rb = patch(B, a["id"], rev, put_("title", "TEST smoke A (B renamed)"))
    s = doc(A, a["id"])
    log("S3 star survives a stale edit", rb.status_code == 200 and s["variants"][0]["fav"] is True,
        f"B {rb.status_code}; fav={s['variants'][0]['fav']} title={s['title']!r}")
    # parallel versions and draft sheets → distinct numbers and folders
    with ThreadPoolExecutor(2) as ex:
        vs = list(ex.map(lambda c: c.post(f"/api/workshop/{a['id']}/versions", json={"variant_ids": [v], "customer_note": "TEST"}), (A, B)))
    ns = sorted(r.json()["version"]["n"] for r in vs if r.status_code == 200)
    dirs = {r.json()["version"]["dir"] for r in vs if r.status_code == 200}
    log("S10 parallel versions", ns == [1, 2] and len(dirs) == 2, f"codes {[r.status_code for r in vs]}, n {ns}, dirs {sorted(dirs)}")
    with ThreadPoolExecutor(2) as ex:
        sh = list(ex.map(lambda c: c.post(f"/api/workshop/{a['id']}/sheet", json={"variant_ids": [v]}), (A, B)))
    pdfs = {r.json()["pdf"] for r in sh if r.status_code == 200}
    log("S11 parallel draft sheets", len(pdfs) == 2, f"codes {[r.status_code for r in sh]}, {sorted(pdfs)}")
    # S4: delete a proposal that is not in a version; a stale device cannot bring it back or star it
    extra_v = [x["id"] for x in doc(A, b["id"])["variants"]]
    if extra_v:
        if True:
            rev = doc(A, b["id"])["rev"]
            patch(A, b["id"], rev, {"op": "delete_variant", "id": extra_v[0]}).raise_for_status()
            rb = patch(B, b["id"], rev, put_(f"variants.{extra_v[0]}.fav", True))
            s = doc(A, b["id"])
            log("S4 deleted proposal stays deleted", rb.status_code == 409 and rb.json()["detail"].get("code") == "gone"
                and extra_v[0] not in [x["id"] for x in s["variants"]], f"stale star → {rb.status_code} {rb.json().get('detail', {}).get('code')}")


try:
    documents()
    books()
    if args.images:
        jobs_and_delivery()
finally:
    for pid in created["workshop"]:
        d = A.get(f"/api/projects/workshop/{pid}")
        if d.status_code == 200:
            r = A.post(f"/api/workshop/{pid}/purge", json={"confirm": d.json().get("title") or "LÖSCHEN"})
            print("purge", pid, r.status_code, r.json())
    for bid in created["book"]:
        print("delete book", bid, A.delete(f"/api/projects/book/{bid}").status_code)
bad = [k for k, v in R.items() if not v["ok"]]
print(json.dumps({"ok": not bad, "problems": bad}, ensure_ascii=False))
raise SystemExit(1 if bad else 0)
