"""Light team identity for the shop LAN: a person picked per device (signed cookie, no PINs), an optional team
password (the server listens on 0.0.0.0), a same-origin check on every write and a Host allowlist (DNS rebinding)."""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import projects

router = APIRouter()
COOKIE = {"httponly": True, "samesite": "lax", "path": "/"}
YEAR = 365 * 24 * 3600
PW_ROUNDS = 200_000
SAFE_NEXT = re.compile(r"^/(?![/\\])[^\x00-\x1f\x7f]*$")
# /api/v1/ skips the cookie/password check only: connect.v1 demands an API key on every route, and the Host allowlist,
# body-size limit and same-origin check below still apply
OPEN = ("/k/", "/static/kunde.html", "/static/ws/icon", "/static/ws/manifest.json", "/api/v1/")
OPEN_EXACT = ("/login", "/api/login")
LOOPBACK = ("127.0.0.1", "::1")
PW_MIN = 8
FREE_FAILS, LOCK_S, LOCK_MAX_S = 10, 60, 3600  # wrong passwords per IP before a lockout that doubles each time
MAX_BODY = 200 * 2**20  # request bodies (Content-Length); /api/upload caps each image itself
LOCAL_SUFFIXES = (".local", ".home.arpa", ".internal", ".lan")
_KEYS: dict[str, bytes] = {}
_ATTEMPTS: dict[str, list[float]] = {}
_FAILS: dict[str, list[float]] = {}  # ip → [failures, time of the last one]
_ALOCK = threading.Lock()
clock = time.time


# ---------------- signing ----------------

def secret() -> bytes:
    """projects/.secret: 32 random bytes as hex, mode 600, created on first use."""
    p = Path(projects.STORE) / ".secret"
    if str(p) in _KEYS:
        return _KEYS[str(p)]
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    key = bytes.fromhex(p.read_text().strip())
    _KEYS[str(p)] = key
    return key


def _sig(msg: str) -> str:
    return hmac.new(secret(), msg.encode(), hashlib.sha256).hexdigest()


def sign_user(uid: str, days: int = 365) -> str:
    exp = int(clock()) + days * 86400
    return f"{uid}.{exp}.{_sig(f'user.{uid}.{exp}')}"


def sign_auth(epoch: int) -> str:
    exp = int(clock()) + YEAR
    return f"{epoch}.{exp}.{_sig(f'auth.{epoch}.{exp}')}"


def _check(value, kind: str) -> str | None:
    """The payload of a valid, unexpired cookie of this kind, else None."""
    parts = value.rsplit(".", 2) if isinstance(value, str) else []
    if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) < clock():
        return None
    return parts[0] if hmac.compare_digest(parts[2], _sig(f"{kind}.{parts[0]}.{parts[1]}")) else None


# ---------------- passwords ----------------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PW_ROUNDS)
    return f"pbkdf2_sha256${PW_ROUNDS}${salt.hex()}${h.hex()}"


def verify_password(pw, stored) -> bool:
    try:
        _, rounds, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", str(pw).encode(), bytes.fromhex(salt), int(rounds))
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(got.hex(), h)


def _limited(request: Request) -> bool:
    """Login attempts: 5 per minute per device+IP, and 20 per minute per IP (cookie-less clients get a new device each
    time); after FREE_FAILS wrong passwords from one IP a lockout of 1, 2, 4 … 60 min follows each further one."""
    ip = request.client.host if request.client else "?"
    keys = (f"{ip}|{getattr(request.state, 'device', '')}", ip)
    t = clock()
    with _ALOCK:
        n, last = _FAILS.get(ip, (0, 0.0))
        if n >= FREE_FAILS and t < last + min(LOCK_MAX_S, LOCK_S * 2 ** (n - FREE_FAILS)):
            return True
        for k in [k for k, v in _ATTEMPTS.items() if not v or v[-1] < t - 60]:
            del _ATTEMPTS[k]
        hits = [[x for x in _ATTEMPTS.get(k, []) if x > t - 60] for k in keys]
        if len(hits[0]) >= 5 or len(hits[1]) >= 20:
            return True
        for k, h in zip(keys, hits):
            _ATTEMPTS[k] = h + [t]
    return False


def _failed(request: Request, ok: bool) -> None:
    ip = request.client.host if request.client else "?"
    with _ALOCK:
        if ok:
            _FAILS.pop(ip, None)
            return
        n, last = _FAILS.get(ip, (0, 0.0))
        _FAILS[ip] = [1 if clock() - last > 86400 else n + 1, clock()]


# ---------------- identity ----------------

def owner_name(owner_key) -> str:
    """„Anna“ for a member id, „Gerät 8f3a“ for "dev:8f3a…", the id itself for an unknown member."""
    return projects.name_of(owner_key)


def user(request: Request):
    return getattr(request.state, "user", None)


def owner(request: Request) -> str:
    return getattr(request.state, "owner", None) or "anon"


def is_team(request: Request) -> bool:
    """A team device (a picked person or a valid login cookie): its view of a customer link is not the customer's."""
    s = projects.settings()
    if _member(_check(request.cookies.get("ws_user"), "user"), s):
        return True
    return bool(s["access"]["password"]) and _auth_ok(request, s)


def _member(uid, s=None):
    return next((m for m in (s or projects.settings())["team"] if m["id"] == uid), None)


def slug(name: str) -> str:
    s = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("æ", "ae"), ("ø", "oe"), ("œ", "oe"), ("ł", "l")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:20].strip("-")
    return s or "person"


def _auth_ok(request: Request, s: dict) -> bool:
    if not s["access"]["password"]:
        return True
    epoch = _check(request.cookies.get("ws_auth"), "auth")
    return epoch is not None and epoch == str(s["access"]["epoch"])


def _host_of(url: str) -> str | None:
    try:
        u = urlsplit(url)
    except ValueError:
        return None
    return u.netloc.lower() if u.scheme in ("http", "https") and u.netloc else None


def _foreign(request: Request) -> bool:
    """A write from another web page: Origin (or else Referer) host[:port] differs from Host. Neither header → allowed."""
    host = (request.headers.get("host") or "").lower()
    origin, referer = request.headers.get("origin"), request.headers.get("referer")
    if origin is not None:
        return _host_of(origin) != host
    if referer is not None:
        return _host_of(referer) != host
    return False


def _open_path(path: str, method: str, request: Request | None = None) -> bool:
    """No login needed: the customer link, the login page, GET /api/me, and GET /api/status from this machine (the
    ExecStartPre wait loops of flux-pup/flux-judge and the tools poll it without a cookie)."""
    if path in OPEN_EXACT or path.startswith(OPEN) or (path == "/api/me" and method == "GET"):
        return True
    return (path == "/api/status" and method in ("GET", "HEAD") and request is not None and request.client is not None
            and request.client.host in LOOPBACK and "origin" not in request.headers)


def _host_names() -> set[str]:
    names = {"localhost"}
    for n in (socket.gethostname(), socket.getfqdn()):
        if n:
            names.add(n.lower())
    names.update(h.strip().lower() for h in os.environ.get("FLUX_ALLOWED_HOSTS", "").split(",") if h.strip())
    return names


_NAMES = _host_names()


def host_ok(host: str, s: dict) -> bool:
    """DNS rebinding guard: an IP literal, a single-label or local-only name (*.local, *.home.arpa, …), this machine's
    names, FLUX_ALLOWED_HOSTS or the host of the configured public link. A rebound attacker domain is none of them."""
    h = (host or "").strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if "." not in h.rstrip(".") or h.rstrip(".").endswith(LOCAL_SUFFIXES) or h in _NAMES:
        return True
    pub = _host_of(str(s.get("public_base_url") or ""))
    return bool(pub) and h == pub.rsplit(":", 1)[0]


def install(app) -> None:
    @app.middleware("http")
    async def team_middleware(request: Request, call_next):
        dev = request.cookies.get("ws_dev")
        new_dev = not (isinstance(dev, str) and re.fullmatch(r"[0-9a-f]{16}", dev))
        if new_dev:
            dev = secrets.token_hex(8)
        s = projects.settings()
        uid = _check(request.cookies.get("ws_user"), "user")
        m = _member(uid, s) if uid else None
        request.state.device = dev
        request.state.user = dict(m) if m else None
        request.state.owner = m["id"] if m else f"dev:{dev[:8]}"
        request.state.owner_name = m["name"] if m else f"Gerät {dev[:4]}"
        path, method = request.url.path, request.method
        try:
            big = int(request.headers.get("content-length") or 0) > MAX_BODY
        except ValueError:
            big = True
        if not host_ok(request.headers.get("host") or "", s):
            resp = JSONResponse({"detail": "Unbekannter Hostname – bitte die Werkstatt über ihre IP-Adresse oder ihren "
                                 "Rechnernamen öffnen (weitere Namen: FLUX_ALLOWED_HOSTS)."}, 400)
        elif big:
            resp = JSONResponse({"detail": "Die Anfrage ist zu groß."}, 413)
        elif method not in ("GET", "HEAD", "OPTIONS") and _foreign(request):
            resp = JSONResponse({"detail": "Anfrage von einer fremden Seite abgelehnt."}, 403)
        elif not _open_path(path, method, request) and not _auth_ok(request, s):
            if method in ("GET", "HEAD") and not path.startswith(("/api/", "/outputs/", "/static/")):
                resp = RedirectResponse(f"/login?next={quote(path)}", 303)
            else:
                resp = JSONResponse({"detail": "Bitte anmelden."}, 401)
        else:
            resp = await call_next(request)
        if new_dev:
            resp.set_cookie("ws_dev", dev, max_age=2 * YEAR, **COOKIE)
        return resp


# ---------------- endpoints ----------------

def _team_view(s: dict) -> list:
    return [{"id": m["id"], "name": m["name"], "color": m["color"]} for m in s["team"]]


@router.get("/api/me")
def get_me(request: Request):
    s = projects.settings()
    ok = _auth_ok(request, s)
    return {"user": user(request) if ok else None, "device": getattr(request.state, "device", None),
            "auth": {"required": bool(s["access"]["password"]), "ok": ok}, "team": _team_view(s) if ok else []}


@router.post("/api/me")
def set_me(body: dict):
    m = _member(body.get("id")) if isinstance(body.get("id"), str) else None
    if not m:
        raise HTTPException(404, "Unbekannte Person.")
    resp = JSONResponse({"user": m})
    resp.set_cookie("ws_user", sign_user(m["id"]), max_age=YEAR, **COOKIE)
    return resp


@router.delete("/api/me")
def clear_me():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ws_user", path="/", httponly=True, samesite="lax")
    return resp


def _name(v) -> str:
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > 40:
        raise HTTPException(400, "Bitte einen Namen angeben (höchstens 40 Zeichen).")
    return v.strip()


def _color(v) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not re.fullmatch(projects.HEX, v):
        raise HTTPException(400, "Ungültige Farbe.")
    return v


@router.post("/api/team")
def add_member(body: dict):
    name, color = _name(body.get("name")), _color(body.get("color"))
    out = {}

    def fn(s):
        ids, base = {m["id"] for m in s["team"]}, slug(name)
        uid, i = base, 1
        while uid in ids:
            i += 1
            uid = f"{base[:20 - len(str(i)) - 1]}-{i}"
        out.update(id=uid, name=name, color=color or projects.COLORS[len(s["team"]) % len(projects.COLORS)])
        s["team"].append(dict(out))
    projects.update_settings(fn)
    return out


@router.patch("/api/team/{uid}")
def edit_member(uid: str, body: dict):
    name = _name(body["name"]) if "name" in body else None
    color = _color(body.get("color"))
    if not _member(uid):
        raise HTTPException(404, "Unbekannte Person.")

    def fn(s):
        for m in s["team"]:
            if m["id"] == uid:
                m.update({k: v for k, v in (("name", name), ("color", color)) if v})
    return _member(uid, projects.update_settings(fn))


@router.delete("/api/team/{uid}")
def remove_member(uid: str):
    if not _member(uid):
        raise HTTPException(404, "Unbekannte Person.")
    projects.update_settings(lambda s: s.update(team=[m for m in s["team"] if m["id"] != uid]))
    return {"ok": True}


@router.post("/api/login")
def login(body: dict, request: Request):
    if _limited(request):
        raise HTTPException(429, "Zu viele Versuche – bitte eine Minute warten.")
    s = projects.settings()
    ok = not s["access"]["password"] or verify_password(body.get("password"), s["access"]["password"])
    _failed(request, ok)
    if not ok:
        raise HTTPException(403, "Passwort stimmt nicht.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ws_auth", sign_auth(s["access"]["epoch"]), max_age=YEAR, **COOKIE)
    return resp


@router.put("/api/access")
def set_access(body: dict, request: Request):
    """Sets, changes or removes the team password; every change bumps the epoch (all other devices log in again)."""
    s = projects.settings()
    if s["access"]["password"]:
        if _limited(request):
            raise HTTPException(429, "Zu viele Versuche – bitte eine Minute warten.")
        ok = verify_password(body.get("current"), s["access"]["password"])
        _failed(request, ok)
        if not ok:
            raise HTTPException(403, "Das aktuelle Passwort stimmt nicht.")
    pw = body.get("password")
    if pw is not None and (not isinstance(pw, str) or len(pw) < PW_MIN or len(pw) > 200):
        raise HTTPException(400, f"Das Passwort muss mindestens {PW_MIN} Zeichen haben.")
    new = hash_password(pw) if pw else None
    s = projects.update_settings(lambda s: s["access"].update(password=new, epoch=s["access"]["epoch"] + 1))
    resp = JSONResponse({"password_set": bool(new)})
    resp.set_cookie("ws_auth", sign_auth(s["access"]["epoch"]), max_age=YEAR, **COOKIE)
    return resp


def safe_next(nxt) -> str:
    return nxt if isinstance(nxt, str) and SAFE_NEXT.match(nxt) else "/werkstatt"


LOGIN_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Werkstatt – Anmeldung</title>
<style>
:root{color-scheme:light dark;--bg:#f6f4ef;--card:#fff;--fg:#1f2421;--mut:#5d6660;--acc:#3d5a4a;--err:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#161a18;--card:#212724;--fg:#eef1ee;--mut:#a7b0aa;--acc:#7cc4a0;--err:#f97066}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{background:var(--card);width:100%;max-width:380px;padding:28px 24px;border-radius:16px;box-shadow:0 6px 24px #0002}
h1{font-size:22px;margin:0 0 20px}label{display:block;font-weight:600;margin-bottom:6px}
input{width:100%;font:inherit;padding:12px 14px;min-height:48px;border:1px solid var(--mut);border-radius:10px;background:transparent;color:inherit}
button{width:100%;margin-top:16px;min-height:52px;border:0;border-radius:12px;background:var(--acc);color:#fff;font:600 17px/1 inherit;cursor:pointer}
#err{color:var(--err);min-height:24px;margin:10px 0 0}
</style></head><body>
<form id="f"><h1>Werkstatt – Anmeldung</h1><label for="pw">Passwort</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
<p id="err" role="alert"></p><button type="submit">Anmelden</button></form>
<noscript><p>Bitte JavaScript aktivieren.</p></noscript>
<script>
const NEXT = __NEXT__;
const safe = n => (typeof n === "string" && /^\\/(?![\\/\\\\])[^\\x00-\\x1f\\x7f]*$/.test(n)) ? n : "/werkstatt";
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const err = document.getElementById("err"); err.textContent = "";
  try {
    const r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({password: document.getElementById("pw").value})});
    if (r.ok) { location.href = safe(NEXT); return; }
    const d = await r.json().catch(() => ({}));
    err.textContent = typeof d.detail === "string" ? d.detail : "Anmeldung fehlgeschlagen.";
  } catch { err.textContent = "Server nicht erreichbar."; }
});
</script></body></html>"""


@router.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/werkstatt"):
    target = json.dumps(safe_next(next)).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return HTMLResponse(LOGIN_PAGE.replace("__NEXT__", target), headers={"Cache-Control": "no-store"})
