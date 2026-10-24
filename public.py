"""The customer link: /k/<token> routes only. Included in the main app (preview in the shop Wi-Fi) and served alone by
`public_app` (127.0.0.1:7861, the only thing a tunnel may ever expose: no /api, /outputs, /static or docs).

The customer sees one version: its frozen images, the customer note and the furniture table, and can *choose* a
design („auswählen – keine Bestellung“) or ask for a change. The JSON is built from an explicit whitelist: no contact
data, internal notes, activity, prompts, captions, verdicts, seeds, other versions or team names.
"""

import hmac
import re
import threading
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

import delivery
import projects
import team

PAGE = Path(__file__).parent / "static" / "kunde.html"
TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
NAME = re.compile(r"[\w.-]{1,120}")
LIMIT_TOKEN, LIMIT_IP, WINDOW = 10, 30, 3600  # respond: per token, per client IP, per hour
PIN_TOKEN, PIN_IP, PIN_WINDOW = 5, 20, 900  # PIN tries: per link and per client IP in 15 minutes
PIN_DAYS = 30  # an unlocked device stays unlocked this long (cookie on /k/<token> only)
PIN_NEEDED = "Bitte geben Sie die PIN ein, die Sie von Ihrer Schreinerei bekommen haben."
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": CSP}
TEXTS = {"ai": "KI-Visualisierung – unverbindlich",
         "privacy": "Datenschutz: Ihre Fotos und Daten speichern wir nur auf unserem eigenen Rechner in der Werkstatt und "
                    "nutzen sie nur für Ihr Angebot. Es werden keine Cloud-Dienste verwendet. Auf Wunsch löschen wir "
                    "alles – eine kurze Nachricht genügt.",
         "measures": "Maße laut Aufmaß. Preise und Ausführung siehe Angebot."}
GONE = "Dieser Link ist nicht mehr gültig."
STUB = ("<!doctype html><html lang=\"de\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
        "<title>Ihre Entwürfe</title><p>Ihre Entwürfe werden geladen … Bitte JavaScript aktivieren oder das beiliegende "
        "PDF öffnen.</p></html>")


def firm(snap_company: dict | None = None) -> dict:
    """Who sent the link: the version's frozen company data, gaps filled from today's Firmendaten (a version sent before
    they were entered would otherwise show nothing). Business contact only — never the customer's."""
    try:
        cur = delivery.company()
    except Exception:
        cur = {}
    snap = snap_company or {}
    s = lambda x: str(x or "").strip()
    return {"name": s(snap.get("name")) or s(cur.get("name")), "contact": s(snap.get("contact")) or s(cur.get("contact")),
            "color": s(snap.get("color")) or s(cur.get("color")) or "#3d5a4a",
            "tel": s(cur.get("phone")), "mail": s(cur.get("email")), "web": s(cur.get("web"))}


class Gone(HTTPException):
    def __init__(self, snap_company: dict | None = None, status: int = 410, detail: str = GONE):
        super().__init__(status, detail)
        f = firm(snap_company)
        self.firm = {"company": f["name"], "contact": f["contact"], "tel": f["tel"], "mail": f["mail"]}


def unknown() -> Gone:
    return Gone(None, 404, "Dieser Link ist unbekannt.")


class _Route(APIRoute):
    """Security headers on every response of these routes, errors included (they bypass the app's handlers here)."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def h(request: Request):
            try:
                resp = await handler(request)
            except HTTPException as e:
                body = {"detail": e.detail, **{k: v for k, v in (getattr(e, "firm", None) or {}).items() if v}}
                resp = JSONResponse(body, e.status_code, headers=e.headers)
            except RequestValidationError:
                resp = JSONResponse({"detail": "Ungültige Anfrage."}, 422)
            for k, v in HEADERS.items():
                resp.headers.setdefault(k, v)
            resp.headers.setdefault("Cache-Control", "private, no-store")
            return resp

        return h


router = APIRouter(route_class=_Route)
_HITS: dict[str, deque] = {}
_HITS_LOCK = threading.Lock()


def _hit(key: str, limit: int, window: int = WINDOW) -> bool:
    """Sliding-window counter; False when the key already used its `limit` requests in the last `window` seconds."""
    t = time.monotonic()
    with _HITS_LOCK:
        if len(_HITS) > 10_000:  # forget idle keys
            for k in [k for k, q in _HITS.items() if not q or q[-1] <= t - WINDOW]:
                del _HITS[k]
        q = _HITS.setdefault(key, deque())
        while q and q[0] <= t - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        return True


def client_ip(request: Request) -> str:
    """The TCP peer; behind a tunnel on this machine (peer 127.0.0.1 and settings.trusted_proxy) the forwarded IP,
    otherwise every customer would share the tunnel's address and one bucket."""
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1") and projects.settings().get("trusted_proxy"):
        fwd = request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0]
        if fwd.strip():
            return fwd.strip()[:64]
    return peer


def _expired(ver: dict) -> bool:
    try:
        return date.fromisoformat(str(ver.get("expires"))[:10]) < date.today()
    except ValueError:
        return False


def _valid(ver: dict, revoked: bool = False):
    if revoked or ver.get("status") == "zurueckgezogen" or _expired(ver):
        raise Gone((ver.get("snapshot") or {}).get("company"))


def lookup(token: str) -> tuple[str, dict, dict]:
    """(pid, doc, version) for a live token; 404 unknown, 410 withdrawn/expired."""
    s = delivery.share(token) if TOKEN.fullmatch(token or "") else None
    if not s:
        raise unknown()
    try:
        doc = projects.load("workshop", str(s.get("pid")))
    except HTTPException:
        raise unknown()
    ver = next((v for v in doc.get("versions") or [] if isinstance(v, dict) and v.get("n") == s.get("n")
                and v.get("token") == token), None)
    if not ver:
        raise unknown()
    _valid(ver, bool(s.get("revoked")))
    return s["pid"], doc, ver


def _pin_cookie(token: str) -> str:
    return "kp_" + token[:12]


def _pin_sig(token: str, pin: str, exp: int) -> str:
    return team._sig(f"kpin.{token}.{pin}.{exp}")   # bound to the PIN: a new PIN locks every unlocked device out


def unlocked(request: Request, token: str, ver: dict) -> bool:
    """A link without PIN, a team device (preview in the shop), or a device that entered the PIN (signed cookie)."""
    pin = ver.get("pin")
    if not pin or team.is_team(request):
        return True
    exp, _, sig = str(request.cookies.get(_pin_cookie(token)) or "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _pin_sig(token, str(pin), int(exp)))


def need_pin(request: Request, token: str, ver: dict) -> None:
    if not unlocked(request, token, ver):
        f = firm((ver.get("snapshot") or {}).get("company"))
        raise HTTPException(401, {"message": PIN_NEEDED, "pin": True, "company": f["name"], "tel": f["tel"], "mail": f["mail"]})


def payload(doc: dict, ver: dict) -> dict:
    """GET /k/<token>/data (spec §9.4) — whitelist only; URLs relative to /k/<token>/."""
    snap, files = ver.get("snapshot") or {}, ver.get("files") or {}
    comp = firm(snap.get("company"))
    appr = doc.get("approved") if isinstance(doc.get("approved"), dict) and doc["approved"].get("version") == ver.get("n") else None
    resp = ver.get("response") if isinstance(ver.get("response"), dict) else None
    s = lambda x: str(x) if x is not None else ""
    img = lambda x: f"img/{x}" if x else None
    return {
        "respond": ver.get("status") in ("offen", "angesehen") and not _expired(ver),
        "status": ver.get("status"), "expires": ver.get("expires"),
        "company": {**comp, "logo": img(files.get("logo"))},
        "title": s(snap.get("title")), "customer": s(snap.get("customer")), "ref": s(snap.get("ref")),
        "version": ver.get("n"), "date": s(snap.get("date")), "note": s(ver.get("customer_note")),
        "variants": [{"n": v.get("n"), "id": v.get("id"), "label": f"Entwurf {v.get('n')}",
                      "before": img(v.get("before")), "after": img(v.get("after")),
                      "before_label": v.get("before_label") if v.get("before") else None, "after_label": s(v.get("after_label")),
                      "focus": v.get("focus") if isinstance(v.get("focus"), list) else None,
                      "rows": [{k: s(r.get(k)) for k in ("piece", "material", "handles", "dims")}
                               for r in v.get("rows") or [] if isinstance(r, dict)]}
                     for v in snap.get("variants") or [] if isinstance(v, dict)],
        "response": {k: resp.get(k) for k in ("variant_id", "action", "name", "comment", "at")} if resp else None,
        "approved_variant": appr.get("variant_id") if appr else None,
        "pdf": "pdf", "zip": "zip", "texts": TEXTS,
    }


def _file(ver: dict, name: str):
    p = delivery.OUT / str(ver.get("dir")) / name
    if not (delivery.safe_rel(ver.get("dir")) and NAME.fullmatch(name) and p.is_file()):
        raise HTTPException(404, "Datei nicht gefunden.")
    return p


@router.get("/k/{token}")
def page(token: str):
    if not TOKEN.fullmatch(token):
        raise unknown()
    html = PAGE.read_text(encoding="utf-8") if PAGE.is_file() else STUB
    return HTMLResponse(html, headers={"Cache-Control": "private, no-cache"})


@router.get("/k/{token}/data")
def data(token: str, request: Request):
    pid, doc, ver = lookup(token)
    need_pin(request, token, ver)
    if ver.get("status") == "offen" and not team.is_team(request):  # the shop checking its own link is no view
        n = ver["n"]

        def viewed(d):
            v = next((x for x in d.get("versions") or [] if x.get("n") == n), None)
            if v and v.get("status") == "offen":
                delivery.mark_sent(d, v, "opened", "kunde")   # opened before the shop tapped a send button: it is out
                v["status"], v["viewed_at"] = "angesehen", v.get("viewed_at") or datetime.now().isoformat(timespec="seconds")
                projects.log(d, "viewed", f"Kunde hat Version {n} angesehen", "kunde")

        doc = projects.mutate("workshop", pid, viewed, who="kunde", auto=True)
        ver = next(v for v in doc["versions"] if v.get("n") == n)
    return payload(doc, ver)


@router.get("/k/{token}/img/{name}")
def image(token: str, name: str, request: Request):
    _, _, ver = lookup(token)
    need_pin(request, token, ver)
    files = ver.get("files") or {}
    if name not in (files.get("images") or []) and name != files.get("logo"):
        raise HTTPException(404, "Datei nicht gefunden.")
    return FileResponse(_file(ver, name), headers={"Cache-Control": "private, max-age=3600"})


@router.get("/k/{token}/pdf")
def pdf(token: str, request: Request):
    _, _, ver = lookup(token)
    need_pin(request, token, ver)
    name = str((ver.get("files") or {}).get("pdf"))
    return FileResponse(_file(ver, name), filename=name, media_type="application/pdf")


@router.get("/k/{token}/zip")
def zip_(token: str, request: Request):
    _, _, ver = lookup(token)
    need_pin(request, token, ver)
    name = str((ver.get("files") or {}).get("zip"))
    return FileResponse(_file(ver, name), filename=name, media_type="application/zip")


@router.post("/k/{token}/respond")
def respond(token: str, request: Request, body: dict = Body(...)):
    """„Entwurf freigeben“ = choosing a design (no order: the offer follows) or „Änderung wünschen“."""
    if not _hit(f"t:{token}", LIMIT_TOKEN) or not _hit(f"ip:{client_ip(request)}", LIMIT_IP):
        raise HTTPException(429, "Zu viele Anfragen – bitte später erneut versuchen.")
    action = body.get("action")
    if action not in ("freigeben", "aenderung"):
        raise HTTPException(400, "Ungültige Anfrage.")
    name = body.get("name").strip() if isinstance(body.get("name"), str) else ""
    comment = body.get("comment") if isinstance(body.get("comment"), str) else ""
    if not name:
        raise HTTPException(400, "Bitte Ihren Namen angeben.")
    if len(name) > 80:
        raise HTTPException(400, "Der Name ist zu lang (höchstens 80 Zeichen).")
    if len(comment) > 1000:
        raise HTTPException(400, "Die Nachricht ist zu lang (höchstens 1000 Zeichen).")
    if body.get("confirm") is not True:
        raise HTTPException(400, "Bitte bestätigen Sie Ihre Auswahl.")
    pid, _, ver = lookup(token)
    need_pin(request, token, ver)
    n, ids, vid = ver["n"], list(ver.get("variant_ids") or []), body.get("variant_id")
    if vid is None and len(ids) == 1:
        vid = ids[0]
    if vid not in ids:
        if action == "freigeben":
            raise HTTPException(400, "Bitte einen Entwurf auswählen.")
        vid = None
    labels = getattr(projects, "STATUS_LABELS", {}) or {}

    def fn(d):
        v = next((x for x in d.get("versions") or [] if x.get("n") == n and x.get("token") == token), None)
        if v is None:
            raise unknown()
        _valid(v)
        if v.get("status") == "freigegeben":
            raise HTTPException(409, "Dieser Entwurf wurde bereits freigegeben.")
        if v.get("status") == "aenderung":
            raise HTTPException(409, "Ihr Änderungswunsch ist bereits bei uns angekommen.")
        delivery.mark_sent(d, v, "opened", "kunde")
        at = datetime.now().isoformat(timespec="seconds")
        v["viewed_at"] = v.get("viewed_at") or at
        v["response"] = {"variant_id": vid, "action": action, "name": name, "comment": comment, "at": at, "via": "link"}
        if action == "freigeben":
            v["status"] = "freigegeben"
            d["approved"] = {"version": n, "variant_id": vid, "at": at, "by": "kunde", "via": "link", "name": name, "comment": comment,
                             "spec": v.get("spec") or projects.design_spec(d)}
            projects.advance(d, "freigegeben", "kunde")
            projects.log(d, "approved", f"Entwurf {ids.index(vid) + 1} freigegeben – per Link ({name})", "kunde")
        else:
            v["status"] = "aenderung"
            if d.get("status") == "kunde":  # the one automatic backward move
                d["status"] = "entwurf"
                projects.log(d, "status", f"Status: {labels.get('kunde', 'Beim Kunden')} → {labels.get('entwurf', 'Entwurf')}", "kunde")
            text = f"Änderungswunsch zu Version {n}" + (f": „{comment.strip()}“" if comment.strip() else "")
            projects.log(d, "change_request", text, "kunde")

    projects.mutate("workshop", pid, fn, who="kunde", auto=True)
    return {"ok": True, "status": "freigegeben" if action == "freigeben" else "aenderung"}


@router.post("/k/{token}/pin")
def unlock(token: str, request: Request, body: dict = Body(...)):
    """PIN entry: 5 tries per link and 20 per IP in 15 minutes (a 6-digit PIN then takes years to guess)."""
    _, _, ver = lookup(token)
    if not ver.get("pin"):
        return {"ok": True}
    if not _hit(f"pt:{token}", PIN_TOKEN, PIN_WINDOW) or not _hit(f"pi:{client_ip(request)}", PIN_IP, PIN_WINDOW):
        raise HTTPException(429, "Zu viele Versuche – bitte in 15 Minuten erneut versuchen oder Ihre Schreinerei anrufen.")
    got = re.sub(r"\s", "", str(body.get("pin") or ""))
    if not hmac.compare_digest(got.encode(), str(ver["pin"]).encode()):
        raise HTTPException(403, "Die PIN stimmt nicht.")
    exp = int(time.time()) + PIN_DAYS * 86400
    resp = JSONResponse({"ok": True})
    https = request.url.scheme == "https" or (projects.settings().get("trusted_proxy")
                                              and request.headers.get("x-forwarded-proto") == "https")
    resp.set_cookie(_pin_cookie(token), f"{exp}.{_pin_sig(token, str(ver['pin']), exp)}", max_age=PIN_DAYS * 86400,
                    path=f"/k/{token}", httponly=True, samesite="strict", secure=bool(https))
    return resp


public_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
public_app.include_router(router)


@public_app.middleware("http")
async def _headers(request: Request, call_next):  # also on the app's own 404/405 for anything that is not /k/…
    resp = await call_next(request)
    for k, v in HEADERS.items():
        resp.headers.setdefault(k, v)
    resp.headers.setdefault("Cache-Control", "private, no-store")
    return resp
