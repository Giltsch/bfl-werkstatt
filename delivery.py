"""Delivery to the customer: versions (the chosen images frozen as JPEGs + Kundenblatt + ZIP in an immutable folder),
share tokens for the customer link (public.py), approval capture, draft sheets, the DSGVO export and the hard delete.

Heavy file work (JPEGs, PDF, ZIP, export, deleting) never runs under the project lock: the GPU worker records every
image through that lock, so a version build would otherwise stall it and every PATCH for seconds. A version number is
reserved under the lock, the folder is built as v<n>.tmp-*, renamed to v<n> and only then committed to the doc.
A LAN link is never written into any file (PDF, ZIP, LIESMICH.txt) — only a configured public address is.
"""

import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image
from reportlab.graphics.barcode.qr import QrCodeWidget
from starlette.background import BackgroundTask

import printing
import projects

DATA = Path(os.environ.get("FLUX_DATA") or Path(__file__).parent)
OUT, STORE, JOBS = DATA / "outputs", DATA / "projects", DATA / "jobs"
MAX_VARIANTS = 4
JPEG_SIDE = 2048
EXIF_DESC = "KI-Visualisierung - unverbindlich"  # EXIF ImageDescription is ASCII
VIAS = {"telefon": "telefonisch", "papier": "auf Papier", "vor_ort": "vor Ort"}
KEEP_DRAFTS = 3
PID = re.compile(r"[a-z0-9]{6,32}")
SHARES_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
RESERVED: dict[str, set[int]] = {}  # pid -> version numbers being built right now
_RES_LOCK = threading.Lock()
jobs_hook: Callable[[str], bool] | None = None  # set by the server: cancel the project's jobs, wait ≤ 30 s; False = still running
router = APIRouter()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def slug(text, n: int = 40) -> str:
    s = str(text or "")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9-]", "-", s)).strip("-")
    return s[:n].strip("-")


def safe_rel(x) -> bool:
    return isinstance(x, str) and bool(re.fullmatch(r"[\w./-]+", x)) and not x.startswith("/") and ".." not in x.split("/")


def check_pid(pid: str) -> str:
    if not PID.fullmatch(pid or ""):
        raise HTTPException(404, "Auftrag nicht gefunden.")
    return pid


def exports_dir(pid: str) -> Path:
    return OUT / "exports" / f"workshop-{check_pid(pid)}"


def owner(request: Request) -> str:
    return getattr(request.state, "owner", None) or "anon"


def name_of(who: str | None) -> str:
    who = who or ""
    if who.startswith("dev:"):
        return f"Gerät {who[4:8]}"
    if who == "kunde":
        return "Kunde"
    for m in projects.settings().get("team") or []:
        if isinstance(m, dict) and m.get("id") == who:
            return m.get("name") or who
    return who


def company() -> dict:
    c = dict(projects.settings().get("company") or {})
    c["contact"] = projects.company_line(c)
    return c


def public_link(token: str) -> str | None:
    base = str(projects.settings().get("public_base_url") or "").strip()
    return f"{base.rstrip('/')}/k/{token}" if base else None


def lan_ip() -> str | None:
    try:  # no packet is sent: connect() on UDP only picks the interface that routes to the LAN
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_base(request: Request) -> str:
    """The shop's address as other devices reach it: a carpenter working on localhost gets the machine's LAN IP."""
    host = request.headers.get("host") or request.url.netloc
    u = urlsplit(f"//{host}")
    name = (u.hostname or "").lower()
    if name in ("localhost", "::1", "0.0.0.0") or name.startswith("127."):
        ip = lan_ip()
        if ip and not ip.startswith("127."):
            host = f"{ip}:{u.port}" if u.port else ip
    return f"{request.url.scheme}://{host}"


def qr_svg(text: str) -> str:
    """Black modules on white, 4-module quiet zone, viewBox in modules, no external references."""
    qr = QrCodeWidget(text).qr
    if qr.modules is None:
        qr.make()
    n, q = qr.getModuleCount(), 4
    d = "".join(f"M{c + q} {r + q}h1v1h-1z" for r in range(n) for c in range(n) if qr.isDark(r, c))
    s = n + 2 * q
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" shape-rendering="crispEdges">'
            f'<rect width="{s}" height="{s}" fill="#fff"/><path d="{d}" fill="#000"/></svg>')


def find_version(doc: dict, n: int) -> dict:
    for v in doc.get("versions") or []:
        if isinstance(v, dict) and v.get("n") == n:
            return v
    raise HTTPException(404, "Version nicht gefunden.")


# ---------------- shares (token -> version) ----------------
# projects/shares.json: {"<token>": {"pid", "n", "expires"}}; a withdrawn link stays as a tombstone
# ("revoked": true) so the customer sees „nicht mehr gültig“ (410) instead of „unbekannt“; purge removes it entirely.

def read_shares() -> dict:
    try:
        s = json.loads((STORE / "shares.json").read_text())
        return s if isinstance(s, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def edit_shares(fn: Callable[[dict], None]) -> None:
    with SHARES_LOCK:
        s = read_shares()
        fn(s)
        write_json(STORE / "shares.json", s)


def share(token: str) -> dict | None:
    s = read_shares().get(token)
    return s if isinstance(s, dict) else None


# ---------------- versions ----------------

def _src(rel) -> Path | None:
    if not safe_rel(rel):
        return None
    p = OUT / rel
    return p if p.is_file() and OUT.resolve() in p.resolve().parents else None


def _jpeg(src: Path, dst: Path, ai: bool) -> None:
    with Image.open(src) as im:
        img = printing.rgb(im)
    if max(img.size) > JPEG_SIDE:
        img.thumbnail((JPEG_SIDE, JPEG_SIDE), Image.LANCZOS)
    exif = Image.Exif()
    if ai:
        exif[0x010E] = EXIF_DESC  # ImageDescription
    img.save(dst, "JPEG", quality=90, exif=exif.tobytes())


def _rows(doc: dict, v: dict) -> list[dict]:
    return [dict(zip(("piece", "material", "handles", "dims"), map(str, r))) for r in printing.sheet_rows(doc, v)]


def focus(doc: dict, v: dict) -> list | None:
    """[left, right] (0-1) of the variant's pieces on the photo — its own boxes or its room ancestor's — so the
    before/after slider can open with the whole piece on the „Nachher“ side."""
    by_id = {x.get("id"): x for x in (doc.get("variants") or []) + (doc.get("trash") or []) if isinstance(x, dict)}
    seen = {v.get("id")}
    while not v.get("boxes") and v.get("from") in by_id and v["from"] not in seen:
        seen.add(v["from"])
        v = by_id[v["from"]]
    bs = [b for b in (v.get("boxes") or {}).values() if isinstance(b, list) and len(b) == 4]
    return [round(min(b[1] for b in bs) / 1000, 3), round(max(b[3] for b in bs) / 1000, 3)] if bs else None


def _readme(snap: dict, ver: dict, comp: dict, link: str | None) -> str:
    lines = [f"Visualisierung {snap['title'] or ''} – Version {ver['n']} vom {printing.fmt_date(snap['date'])}".replace("  ", " ")]
    lines += [x for x in (f"Für: {snap['customer']}" if snap["customer"] else "", f"Vorgang {snap['ref']}" if snap["ref"] else "",
                          f"zum Angebot Nr. {snap['ext_ref']}" if snap["ext_ref"] else "") if x]
    if ver["customer_note"]:
        lines += ["", ver["customer_note"]]
    lines += ["", "Inhalt:"] + [f"  {f}" for f in ver["files"]["images"]] + [f"  {ver['files']['pdf']} – Kundenblatt zum Ausdrucken"]
    lines += ["", printing.AI_NOTE, printing.MEASURES_NOTE, printing.PRIVACY_NOTE]
    if link:
        lines += ["", f"{printing.QR_CAPTION}: {link}"]
    lines += ["", comp.get("name") or "", comp.get("contact") or ""]
    return "\r\n".join(lines).rstrip() + "\r\n"


def _build(doc: dict, pid: str, n: int, ids: list[str], note: str, comp: dict, token: str, days: int, who: str,
           link: str | None) -> dict:
    """Builds v<n> (tmp folder → os.replace) and returns the version object; on failure nothing is left behind."""
    d = exports_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"v{n}.tmp-", dir=d))
    final = d / f"v{n}"
    try:
        os.chmod(tmp, 0o755)
        by_id = {v["id"]: v for v in doc.get("variants") or []}
        images, befores, snap_variants, frozen = [], {}, [], []

        def add(src: Path, label: str, ai: bool) -> str:
            name = f"{len(images) + 1:02d}_{label}.jpg"
            _jpeg(src, tmp / name, ai)
            images.append(name)
            return name

        for i, vid in enumerate(ids, 1):
            v = by_id[vid]
            src = _src(v.get("image"))
            if not src:
                raise printing.ExportError("Die Bilder der gewählten Entwürfe fehlen.")
            before, b_label, a_label = printing.before_of(doc, v)
            bsrc = _src(before) if before != v.get("image") else None
            bname = None
            if bsrc:
                bname = befores.get(before) or add(bsrc, slug(b_label) or "Vorher", False)
                befores[before] = bname
            rows = _rows(doc, v)
            wood = slug("-".join(dict.fromkeys(r["material"] for r in rows if r["material"])))
            aname = add(src, f"Entwurf-{i}" + (f"_{wood}" if wood else "") + "_KI-Visualisierung", True)
            labels = (b_label, a_label) if bname else (None, "Entwurf")
            snap_variants.append({"n": i, "id": vid, "before": bname, "after": aname, "before_label": labels[0],
                                  "after_label": labels[1], "rows": rows, "focus": focus(doc, v)})
            frozen.append({**v, "image": aname, "before": bname, "before_label": labels[0], "after_label": labels[1]})
        logo = None
        lsrc = _src(comp.get("logo"))
        if lsrc:
            with Image.open(lsrc) as im:
                im.convert("RGBA").save(tmp / "logo.png")
            logo = "logo.png"
        today = date.today().isoformat()
        base = f"Visualisierung_{slug(doc.get('ref')) or pid}" + (f"_{slug(doc.get('customer'))}" if slug(doc.get("customer")) else "")
        ver = {"n": n, "created": now(), "by": who, "variant_ids": ids, "customer_note": note, "status": "offen",
               "token": token, "expires": (date.today() + timedelta(days=days)).isoformat(), "viewed_at": None,
               "sent_at": None, "sent_via": None, "sent_by": None,
               "response": None, "dir": f"exports/workshop-{pid}/v{n}", "link": link, "spec": projects.design_spec(doc),
               "files": {"pdf": f"{base}_V{n}.pdf", "zip": f"{base}_V{n}.zip", "logo": logo, "images": images},
               "snapshot": {"title": doc.get("title") or "", "customer": doc.get("customer") or "", "ref": doc.get("ref") or "",
                            "ext_ref": doc.get("ext_ref") or "", "date": today,
                            "company": {"name": comp.get("name") or "", "contact": comp.get("contact") or "",
                                        "color": comp.get("color") or "#3d5a4a"},
                            "variants": snap_variants}}
        sheet_doc = {k: doc.get(k) for k in ("title", "customer", "ref", "ext_ref", "contact", "room", "pieces", "approved")}
        printing.build_sheet({**sheet_doc, "variants": frozen}, tmp, tmp, company={**comp, "logo": logo}, version=ver,
                             link=link, filename=ver["files"]["pdf"])
        (tmp / "LIESMICH.txt").write_text(_readme(ver["snapshot"], ver, comp, link), encoding="utf-8")
        with zipfile.ZipFile(tmp / ver["files"]["zip"], "w", zipfile.ZIP_DEFLATED) as z:
            for name in images + [ver["files"]["pdf"], "LIESMICH.txt"]:
                z.write(tmp / name, name)
        if final.exists():  # v<n> folders are immutable
            raise RuntimeError(f"{final} exists")
        os.replace(tmp, final)
        return ver
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def create_version(pid: str, variant_ids, customer_note, expires_days, who: str, lan_base: str, pin: bool = False) -> dict:
    ids = list(dict.fromkeys(x for x in variant_ids or [] if isinstance(x, str))) if isinstance(variant_ids, list) else []
    if not ids:
        raise HTTPException(400, "Bitte mindestens einen Entwurf wählen.")
    if len(ids) > MAX_VARIANTS:
        raise HTTPException(400, "Höchstens 4 Entwürfe pro Version.")
    note = str(customer_note or "").strip()[:2000]
    try:
        days = min(365, max(1, int(expires_days or 90)))
    except (TypeError, ValueError):
        days = 90
    check_pid(pid)
    with projects.locked("workshop", pid):  # short: validate, reserve the number, snapshot
        doc = projects.load("workshop", pid)
        live = {v.get("id") for v in doc.get("variants") or [] if isinstance(v, dict)}
        if any(i not in live for i in ids):
            raise HTTPException(404, "Vorschlag nicht gefunden.")
        with _RES_LOCK:
            taken = {v.get("n") for v in doc.get("versions") or [] if isinstance(v, dict)} | RESERVED.setdefault(pid, set())
            n = max([0, *(x for x in taken if isinstance(x, int))]) + 1
            while (exports_dir(pid) / f"v{n}").exists():
                n += 1
            RESERVED[pid].add(n)
        comp = company()
    try:
        token = secrets.token_urlsafe(24)
        pub = public_link(token)
        link = pub or f"{lan_base.rstrip('/')}/k/{token}"  # the LAN link only reaches customers in the shop Wi-Fi
        try:
            ver = _build(doc, pid, n, ids, note, comp, token, days, who, link)
        except printing.ExportError as e:
            raise HTTPException(400, str(e))
        if pin:   # 6 digits, told to the customer separately (phone, SMS); team-only, never in the customer data
            ver["pin"] = f"{secrets.randbelow(10**6):06d}"
        final = exports_dir(pid) / f"v{n}"
        try:
            edit_shares(lambda s: s.__setitem__(token, {"pid": pid, "n": n, "expires": ver["expires"]}))

            def commit(d):
                alive = {v.get("id") for v in d.get("variants") or []}
                if any(i not in alive for i in ids):
                    raise HTTPException(409, "Ein gewählter Entwurf wurde inzwischen gelöscht – bitte erneut senden.")
                d.setdefault("versions", []).append(ver)   # only prepared: „Beim Kunden“ once it is really sent
                k = len(ids)
                projects.log(d, "version_ready", f"Version {n} für den Kunden bereitgestellt ({k} {'Entwurf' if k == 1 else 'Entwürfe'})", who)

            doc = projects.mutate("workshop", pid, commit, who=who, auto=True)
        except BaseException:
            edit_shares(lambda s: s.pop(token, None))
            shutil.rmtree(final, ignore_errors=True)
            raise
    finally:
        with _RES_LOCK:
            RESERVED.get(pid, set()).discard(n)
    api = f"/api/workshop/{pid}/versions/{n}"
    return {"version": find_version(doc, n), "doc_rev": doc.get("rev"), "link": f"{lan_base.rstrip('/')}/k/{token}",
            "public_link": pub, "share_link": link, "lan_only": pub is None, "qr_svg": qr_svg(link),
            "pdf": f"{api}/pdf", "zip": f"{api}/zip"}


SENT_VIA = {"email": "per E-Mail", "whatsapp": "per WhatsApp", "link": "Link kopiert", "share": "geteilt", "qr": "per QR-Code",
            "pdf": "PDF geteilt", "opened": "vom Kunden geöffnet", "approval": "Freigabe erfasst"}


def is_sent(v: dict) -> bool:
    return bool(v.get("sent_at")) or "sent_at" not in v   # versions from before this field went out at creation


def mark_sent(d: dict, v: dict, via: str, who: str) -> bool:
    """First real hand-over of a version (a send button, the customer opening the link, a recorded approval)."""
    if is_sent(v) or v.get("status") == "zurueckgezogen":
        return False
    v["sent_at"], v["sent_via"], v["sent_by"] = now(), via, who
    projects.advance(d, "kunde", who)
    if via != "opened":   # the customer's view is logged as „Kunde hat Version n angesehen“
        projects.log(d, "version_sent", f"Version {v['n']} an den Kunden gesendet ({SENT_VIA.get(via, via)})", who)
    return True


def sent(pid: str, n: int, via, who: str) -> dict:
    if via not in SENT_VIA or via in ("opened", "approval"):
        raise HTTPException(400, "Unbekannter Versandweg.")
    doc = projects.mutate("workshop", check_pid(pid), lambda d: mark_sent(d, find_version(d, n), via, who), who=who, auto=True)
    return {"version": find_version(doc, n), "status": doc.get("status"), "doc_rev": doc.get("rev")}


def withdraw(pid: str, n: int, who: str) -> dict:
    ver = find_version(projects.load("workshop", check_pid(pid)), n)
    if ver.get("token"):  # revoke first: if anything below fails, the link is dead anyway
        edit_shares(lambda s: s.__setitem__(ver["token"], {"pid": pid, "n": n, "expires": ver.get("expires"), "revoked": True}))

    def fn(d):
        v = find_version(d, n)
        if v.get("status") != "zurueckgezogen":
            v["status"], v["withdrawn_at"] = "zurueckgezogen", now()
            projects.log(d, "withdrawn", f"Version {n} zurückgezogen", who)

    return {"version": find_version(projects.mutate("workshop", pid, fn, who=who, auto=True), n)}


def approve(pid: str, body: dict, who: str) -> dict:
    try:
        n = int(body.get("version"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bitte eine Version angeben.")
    vid, via = body.get("variant_id"), body.get("via")
    if via not in VIAS:
        raise HTTPException(400, "Bitte angeben, wie die Freigabe kam (telefonisch, auf Papier oder vor Ort).")
    name, comment = str(body.get("name") or "").strip()[:80], str(body.get("comment") or "").strip()[:1000]

    def fn(d):
        v = find_version(d, n)
        if v.get("status") == "zurueckgezogen":
            raise HTTPException(409, "Diese Version wurde zurückgezogen.")
        if vid not in (v.get("variant_ids") or []):
            raise HTTPException(404, "Vorschlag nicht gefunden.")
        at = now()
        d["approved"] = {"version": n, "variant_id": vid, "at": at, "by": who, "via": via, "name": name, "comment": comment,
                         "spec": v.get("spec") or projects.design_spec(d)}
        mark_sent(d, v, "approval", who)
        v["status"] = "freigegeben"
        v["response"] = {"variant_id": vid, "action": "freigeben", "name": name, "comment": comment, "at": at, "via": via}
        projects.advance(d, "freigegeben", who)
        k = v["variant_ids"].index(vid) + 1
        projects.log(d, "approved", f"Entwurf {k} freigegeben – {VIAS[via]} (erfasst von {name_of(who)})", who)

    doc = projects.mutate("workshop", check_pid(pid), fn, who=who, auto=True)
    return {"approved": doc.get("approved"), "doc_rev": doc.get("rev")}


def unapprove(pid: str, who: str) -> dict:
    def fn(d):
        a = d.get("approved")
        if not a:
            return
        v = next((x for x in d.get("versions") or [] if x.get("n") == a.get("version")), None)
        if v and v.get("status") == "freigegeben":  # the customer may choose again through the link
            v["status"], v["response"] = ("angesehen" if v.get("viewed_at") else "offen"), None
        d["approved"] = None
        projects.log(d, "unapproved", "Freigabe zurückgenommen", who)

    doc = projects.mutate("workshop", check_pid(pid), fn, who=who, auto=True)
    return {"approved": doc.get("approved"), "doc_rev": doc.get("rev")}


def draft_sheet(pid: str, variant_ids=None) -> dict:
    """Kundenblatt of the chosen proposals in a fresh entwurf-<stamp>/ folder (newest 3 kept); v<n> is never touched."""
    with projects.locked("workshop", check_pid(pid)):
        doc = projects.load("workshop", pid)
    comp = company()
    d = exports_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    t = datetime.now()
    stamp = f"{t:%Y%m%d-%H%M%S}-{t.microsecond // 1000:03d}"  # never reuses a pruned folder's URL
    for k in range(1, 100):
        folder = d / (f"entwurf-{stamp}" + (f"-{k:02d}" if k > 1 else ""))  # sorts by name = by age
        try:
            folder.mkdir()
            break
        except FileExistsError:
            continue
    ids = [x for x in variant_ids if isinstance(x, str)][:MAX_VARIANTS] if isinstance(variant_ids, list) else None
    name = f"Visualisierung_{slug(doc.get('ref')) or pid}" + (f"_{slug(doc.get('customer'))}" if slug(doc.get("customer")) else "") + "_Entwurf.pdf"
    skipped = []
    try:
        pdf = printing.build_sheet(doc, OUT, folder, skipped, company=comp if comp.get("name") else None,
                                   variant_ids=ids or None, filename=name)
    except printing.ExportError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, str(e))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    for old in sorted((p for p in d.glob("entwurf-*") if p.is_dir()), key=lambda p: p.name)[:-KEEP_DRAFTS]:
        if old != folder:
            shutil.rmtree(old, ignore_errors=True)
    return {"pdf": f"/outputs/{pdf.relative_to(OUT).as_posix()}", "skipped": skipped}


# ---------------- DSGVO: references, export, purge ----------------

def referenced_files(doc: dict, data_root: Path | None = None, scan: bool = True) -> list[Path]:
    """Every file of this order: photos, sketches, renders, samples, variant (and trashed variant) images with their
    .json sidecars and the refs those list, replaced photos and renders that fell out of the trash (old_files), every
    output whose sidecar names this order as its source (scan), guide images, the export folder, the geometry cache of
    each room photo (a depth picture of the customer's room), the pre-v3 backup of the doc and the refs of its persisted jobs."""
    root = Path(data_root) if data_root else DATA
    out, store, jobs = root / "outputs", root / "projects", root / "jobs"
    pid = str(doc.get("id") or "")
    rels, rooms = set(), set()

    def add(x):
        if safe_rel(x):
            rels.add(x)

    def with_sidecar(image):
        add(image)
        side = str(Path(image).with_suffix(".json"))
        add(side)
        try:
            meta = json.loads((out / side).read_text())
            for r in meta.get("refs") or [] if isinstance(meta, dict) else []:
                add(r)
        except (OSError, ValueError):
            pass

    for k in ("room", "sample", "sketch"):
        add(doc.get(k))
    rooms.add(doc.get("room"))
    for x in doc.get("old_files") or []:
        if safe_rel(x):
            with_sidecar(x)
            rooms.add(x)
    if scan and PID.fullmatch(pid) and out.is_dir():
        for f in out.glob("*.json"):
            try:
                meta = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("source") == f"workshop:{pid}":
                with_sidecar(f"{f.stem}.png")
    for p in doc.get("pieces") or []:
        if isinstance(p, dict):
            for k in ("sketch", "render", "sample"):
                add(p.get(k))
    for v in (doc.get("variants") or []) + (doc.get("trash") or []):
        if not isinstance(v, dict):
            continue
        for k in ("room", "sketch"):
            add(v.get(k))
        rooms.add(v.get("room"))
        if safe_rel(v.get("image")):
            with_sidecar(v["image"])
    for room in rooms:
        if safe_rel(room):
            stem = Path(room).stem
            rels.update(f"geometry/{stem}{s}" for s in (".json", "-depth.npy", "-depth.jpg"))
    files = {out / r for r in rels}
    if PID.fullmatch(pid):
        files.update((out / "guides").glob(f"{pid}-*.png"))
        exp = out / "exports" / f"workshop-{pid}"
        if exp.is_dir():
            files.update(p for p in exp.rglob("*") if p.is_file())
        files.add(store / "backup-v2" / "workshop" / f"{pid}.json")
        job_ids = {str(v.get("job")) for v in (doc.get("variants") or []) + (doc.get("trash") or []) if isinstance(v, dict) and v.get("job")}
        for jid in job_ids:   # unblended FLUX images (jobs.py keeps them for re-blending)
            if re.fullmatch(r"[0-9a-f]{6,32}", jid):
                files.update((out / "raw").glob(f"{jid}-*.png"))
        for j in jobs.glob("*.json") if jobs.is_dir() else []:
            try:
                job = json.loads(j.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(job, dict) and job.get("project") == pid:
                files.update(out / r for r in job.get("refs") or [] if safe_rel(r))
    return sorted(p for p in files if p.is_file())


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def referenced_elsewhere(pid: str) -> set[Path]:
    """Files any other project (book or workshop) still points to — they survive a purge."""
    keep = set()
    for kind in ("workshop", "book"):
        for f in (STORE / kind).glob("*.json"):
            if kind == "workshop" and f.stem == pid:
                continue
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            keep.update(OUT / s for s in _strings(d) if safe_rel(s))
            if kind == "workshop" and isinstance(d, dict):
                keep.update(referenced_files(d, DATA, scan=False))
    return {p.resolve() for p in keep}


def export_zip(pid: str, who: str) -> tuple[Path, str]:
    """DSGVO Art. 15: projekt.json + every referenced file + LIESMICH.txt, zipped outside the lock into a temp file."""
    with projects.locked("workshop", check_pid(pid)):
        doc = projects.load("workshop", pid)
        files = referenced_files(doc, DATA)
    (OUT / "exports").mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".zip", dir=OUT / "exports")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("projekt.json", json.dumps(doc, ensure_ascii=False, indent=1))
            names = []
            for p in files:
                arc = p.relative_to(DATA).as_posix() if DATA in p.parents else f"dateien/{p.name}"
                z.write(p, arc)
                names.append(arc)
            label = " · ".join(x for x in (doc.get("ref"), doc.get("title"), doc.get("customer")) if x) or pid
            z.writestr("LIESMICH.txt", "\r\n".join(
                [f"Datenexport vom {date.today():%d.%m.%Y} für Auftrag {label}", "",
                 "projekt.json – alle gespeicherten Angaben zum Auftrag",
                 *(f"{n}" for n in names), ""]))
    except BaseException:
        os.unlink(tmp)
        raise
    try:
        projects.mutate("workshop", pid, lambda d: projects.log(d, "export", "Daten exportiert (DSGVO)", who), who=who, auto=True)
    except HTTPException:
        pass
    return Path(tmp), f"Datenexport_{slug(doc.get('ref')) or pid}_{date.today():%Y-%m-%d}.zip"


def purge(pid: str, confirm, who: str) -> int:
    """Hard delete: the project's jobs are cancelled first (late results are deleted by the scheduler), then the doc
    and every file only it references. Writes one line without personal data to projects/deletions.log."""
    doc = projects.load("workshop", check_pid(pid))
    expect = str(doc.get("title") or "").strip() or "LÖSCHEN"
    if str(confirm or "").strip() != expect:
        raise HTTPException(400, "Bestätigung stimmt nicht – bitte den Titel genau eintippen.")
    if jobs_hook is not None and not jobs_hook(pid):
        raise HTTPException(503, "Eine Berechnung für diesen Auftrag lässt sich gerade nicht stoppen – bitte gleich nochmal versuchen.")
    with projects.locked("workshop", pid):
        doc = projects.load("workshop", pid)  # incl. whatever was recorded until the jobs stopped
        mine = referenced_files(doc, DATA)
        keep = referenced_elsewhere(pid)
        drop = getattr(projects, "delete_project", None)  # lets projects drop its caches and bump list_rev
        if drop:
            drop("workshop", pid)
        (STORE / "workshop" / f"{pid}.json").unlink(missing_ok=True)
    count = 1
    roots = (OUT.resolve(), STORE.resolve())
    for p in mine:
        rp = p.resolve()
        if rp not in keep and any(r in rp.parents for r in roots):
            try:
                p.unlink()
                count += 1
            except FileNotFoundError:
                pass
    exp = exports_dir(pid)
    if exp.exists():
        count += sum(1 for p in exp.rglob("*") if p.is_file())
        shutil.rmtree(exp, ignore_errors=True)
    for j in JOBS.glob("*.json") if JOBS.is_dir() else []:
        try:
            if json.loads(j.read_text()).get("project") == pid:
                j.unlink()
                count += 1
        except (OSError, ValueError, AttributeError):
            pass
    bk = STORE / "backup-v2" / "workshop" / f"{pid}.json"
    if bk.exists():
        bk.unlink()
        count += 1
    edit_shares(lambda s: [s.pop(t) for t in [t for t, x in s.items() if isinstance(x, dict) and x.get("pid") == pid]])
    with LOG_LOCK, open(STORE / "deletions.log", "a", encoding="utf-8") as f:
        f.write(f"{now()} workshop {pid} auf-wunsch {count}-dateien\n")
    return count


# ---------------- routes ----------------

@router.post("/api/workshop/{pid}/versions")
def post_version(pid: str, request: Request, body: dict = Body(...)):
    return create_version(pid, body.get("variant_ids"), body.get("customer_note"), body.get("expires_days"), owner(request),
                          lan_base(request), pin=body.get("pin") is True)


@router.get("/api/workshop/{pid}/versions/{n}/{what}")
def get_version_file(pid: str, n: int, what: str, request: Request):
    ver = find_version(projects.load("workshop", check_pid(pid)), n)
    if what == "qr.svg":
        tok = ver.get("token") if ver.get("status") != "zurueckgezogen" else None
        link = tok and (public_link(tok) or f"{lan_base(request)}/k/{tok}")
        if not link:
            raise HTTPException(404, "Dieser Link wurde zurückgezogen.")
        return Response(qr_svg(link), media_type="image/svg+xml")
    if what not in ("pdf", "zip"):
        raise HTTPException(404, "Datei nicht gefunden.")
    name = (ver.get("files") or {}).get(what)
    p = OUT / str(ver.get("dir")) / str(name)
    if not (safe_rel(ver.get("dir")) and safe_rel(name) and p.is_file()):
        raise HTTPException(404, "Datei nicht gefunden.")
    return FileResponse(p, filename=name, media_type="application/pdf" if what == "pdf" else "application/zip")


@router.post("/api/workshop/{pid}/versions/{n}/sent")
def post_sent(pid: str, n: int, request: Request, body: dict | None = Body(None)):
    return sent(pid, n, (body or {}).get("via"), owner(request))


@router.post("/api/workshop/{pid}/versions/{n}/withdraw")
def post_withdraw(pid: str, n: int, request: Request):
    return withdraw(pid, n, owner(request))


@router.post("/api/workshop/{pid}/approval")
def post_approval(pid: str, request: Request, body: dict = Body(...)):
    return approve(pid, body, owner(request))


@router.post("/api/workshop/{pid}/approval/seen")
def post_approval_seen(pid: str, request: Request):
    """Somebody in the shop opened the project after the customer approved by link: the dashboard's „Neu“ goes away."""
    who = owner(request)

    def fn(d):
        a = d.get("approved")
        if isinstance(a, dict) and a.get("via") == "link" and not a.get("seen_at"):
            a["seen_at"], a["seen_by"] = now(), who

    projects.mutate("workshop", check_pid(pid), fn, who=who, auto=True)
    return {"ok": True}


@router.delete("/api/workshop/{pid}/approval")
def delete_approval(pid: str, request: Request):
    return unapprove(pid, owner(request))


@router.post("/api/workshop/{pid}/sheet")
def post_sheet(pid: str, body: dict | None = Body(None)):
    return draft_sheet(pid, (body or {}).get("variant_ids"))


@router.get("/api/workshop/{pid}/export")
def get_export(pid: str, request: Request):
    path, name = export_zip(pid, owner(request))
    return FileResponse(path, filename=name, media_type="application/zip", background=BackgroundTask(os.unlink, path))


@router.post("/api/workshop/{pid}/purge")
def post_purge(pid: str, request: Request, body: dict = Body(...)):
    return {"ok": True, "files": purge(pid, body.get("confirm"), owner(request))}
