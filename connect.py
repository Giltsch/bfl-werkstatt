"""Connectivity: API keys, the /api/v1 order API (another front door to the agent), webhooks and file exports.

    API keys     projects/apikeys.json (mode 600): sha256 of the key, name, scopes, created, last_used. The plain key is
                 shown once (POST /api/keys) and never stored. Team routes GET/POST/DELETE /api/keys.
    /api/v1      Authorization: Bearer <key>. POST /orders, GET /orders/{pid}, POST /orders/{pid}/messages,
                 GET /orders/{pid}/files[/{name}]. A key sees only the orders it created (projects/v1_orders.json);
                 per-key rate limits. The team middleware lets /api/v1/ through without a cookie, the router checks the key.
    Webhooks     projects/webhooks.json (mode 600): order.question / order.preview / order.result, signed with
                 HMAC-SHA256 (X-Werkstatt-Signature), delivered by a background thread with retries. OFF by default:
                 nothing is sent unless the master switch and the hook are both on. Only public internet addresses are
                 allowed (checked when saved and again before every delivery, the checked IP is the one connected to):
                 a hook must not become a probe into this machine (vLLM, customer app) or the shop LAN. A hook with
                 "lan": true (the owner's explicit choice, e.g. n8n on another computer) may also use private LAN
                 addresses; this machine itself, loopback, link-local/metadata and multicast stay refused.
    Team password  Creating keys and switching webhooks on need a team password: without one, every device on the
                 LAN could do it (the server listens on 0.0.0.0). Without a password no webhook is sent.

The agent (agent.py: start / handle / events / subscribe) is imported lazily: the API answers 503 until it exists."""

import hashlib
import heapq
import hmac
import importlib
import ipaddress
import itertools
import json
import re
import secrets
import socket
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

import projects

router = APIRouter()                                          # team routes (cookie / team password)

SCOPES = ("orders",)
KEY_PREFIX = "wk_"                                            # recognisable in logs and by secret scanners
MAX_KEYS, MAX_HOOKS = 50, 20
MAX_TEXT = 4000
MAX_PHOTO = 30 * 2**20
MAX_AUDIO = 25 * 2**20
TOUCH_S = 60                                                  # last_used is written at most once a minute per key
LIMITS = {"read": (120, 60.0), "write": (30, 60.0), "create": (20, 3600.0)}   # requests per window (s) per key
AUTH_FAILS = (20, 60.0)                                       # wrong keys per IP before 429
EVENTS = ("order.question", "order.preview", "order.result")
KIND_EVENT = {"question": "order.question", "preview": "order.preview", "result": "order.result"}
RETRIES = (5, 30, 120, 600)                                   # seconds before attempt 2, 3, 4, 5
HOOK_TIMEOUT = 10.0
TRANSPORT = None                                              # httpx transport for webhooks (tests: httpx.MockTransport)
RESOLVE = socket.getaddrinfo                                  # DNS for the webhook address check (tests: a fake)
NEED_PASSWORD = ("Bitte zuerst ein Team-Passwort setzen (Einstellungen → Zugang). Ohne Passwort könnte jedes Gerät im "
                 "WLAN Daten der Werkstatt nach außen schicken.")
RECENT_MAX = 50
FILE_RE = re.compile(r"(bild|angebot|zeichnung|modell|zuschnitt|ereignis)(?:_([a-z0-9]{1,16}))?\.(png|jpg|webp|txt|dxf|glb|stl|csv)")
MEDIA = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "txt": "text/plain; charset=utf-8",
         "dxf": "image/vnd.dxf", "glb": "model/gltf-binary", "stl": "model/stl", "csv": "text/csv; charset=utf-8"}

_LOCK = threading.RLock()                                     # apikeys.json, v1_orders.json, webhooks.json
_RLOCK = threading.Lock()
_RATE: dict[tuple, list[float]] = {}
_FAILS: dict[str, list[float]] = {}
_TOUCHED: dict[str, float] = {}
clock = time.time


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path(name: str) -> Path:
    return Path(projects.STORE) / name   # resolved per call: tests point projects.STORE at a tmp dir


def _read(name: str, default):
    try:
        data = json.loads(_path(name).read_text())
    except (FileNotFoundError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write(name: str, obj) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    projects.write_json(p, obj)   # a NamedTemporaryFile renamed into place: mode 600 already
    p.chmod(0o600)


# ---------------- API keys ----------------

def _hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def _key_view(k: dict) -> dict:
    return {x: k.get(x) for x in ("id", "name", "scopes", "created", "created_by", "last_used", "hint")}


def list_keys() -> list[dict]:
    return [_key_view(k) for k in _read("apikeys.json", {}).get("keys", [])]


def create_key(name, scopes=None, who=None) -> tuple[dict, str]:
    """→ (public record, plain key). The plain key exists only in this return value."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
        raise HTTPException(400, "Bitte einen Namen für den Schlüssel angeben (höchstens 60 Zeichen).")
    scopes = list(scopes) if isinstance(scopes, (list, tuple)) and scopes else list(SCOPES)
    if any(s not in SCOPES for s in scopes):
        raise HTTPException(400, f"Unbekannte Berechtigung (erlaubt: {', '.join(SCOPES)}).")
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    rec = {"id": secrets.token_hex(4), "name": name.strip(), "hash": _hash(plain), "scopes": sorted(set(scopes)),
           "created": now(), "created_by": who, "last_used": None, "hint": f"{plain[:7]}…{plain[-4:]}"}
    with _LOCK:
        data = _read("apikeys.json", {})
        keys = data.setdefault("keys", [])
        if len(keys) >= MAX_KEYS:
            raise HTTPException(400, f"Höchstens {MAX_KEYS} Schlüssel.")
        keys.append(rec)
        _write("apikeys.json", data)
    return _key_view(rec), plain


def delete_key(kid: str) -> bool:
    with _LOCK:
        data = _read("apikeys.json", {})
        keys = data.get("keys", [])
        rest = [k for k in keys if k.get("id") != kid]
        if len(rest) == len(keys):
            return False
        data["keys"] = rest
        _write("apikeys.json", data)
    _TOUCHED.pop(kid, None)
    return True


def check_key(plain) -> dict | None:
    """The key record for a presented key, else None. Every stored hash is compared (constant time each, no early
    exit), so the answer time does not tell how close a guess was or which key matched."""
    if not isinstance(plain, str) or not plain or len(plain) > 200:
        return None
    h = _hash(plain)
    found = None
    for k in _read("apikeys.json", {}).get("keys", []):
        if hmac.compare_digest(str(k.get("hash") or ""), h) and found is None:
            found = k
    return found


def touch(kid: str) -> None:
    t = clock()
    if t - _TOUCHED.get(kid, 0) < TOUCH_S:
        return
    _TOUCHED[kid] = t
    with _LOCK:
        data = _read("apikeys.json", {})
        for k in data.get("keys", []):
            if k.get("id") == kid:
                k["last_used"] = now()
                _write("apikeys.json", data)
                return


# ---------------- rate limits ----------------

def _hit(table: dict, key, limit: int, window: float, count: bool = True) -> float:
    """Sliding window: 0 when allowed (and counted), else the seconds until the next request would be allowed."""
    t = clock()
    with _RLOCK:
        if len(table) > 10_000:   # stale entries of keys or IPs not seen for a while
            for k in [k for k, v in table.items() if not v or v[-1] < t - 3600]:
                del table[k]
        hits = [x for x in table.get(key, []) if x > t - window]
        if len(hits) >= limit:
            table[key] = hits
            return max(1.0, hits[0] + window - t)
        if count:
            hits.append(t)
        table[key] = hits
    return 0.0


def _limit(kid: str, bucket: str) -> None:
    n, window = LIMITS[bucket]
    wait = _hit(_RATE, (kid, bucket), n, window)
    if wait:
        raise HTTPException(429, f"Zu viele Anfragen – bitte in {int(wait) + 1} s erneut versuchen.",
                            headers={"Retry-After": str(int(wait) + 1)})


def api_key(request: Request) -> dict:
    """Dependency of every /api/v1 route: a valid key with the "orders" scope, not rate limited."""
    ip = request.client.host if request.client else "?"
    if _hit(_FAILS, ip, *AUTH_FAILS, count=False):
        raise HTTPException(429, "Zu viele ungültige Schlüssel – bitte eine Minute warten.", headers={"Retry-After": "60"})
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    rec = check_key(token.strip()) if scheme.lower() == "bearer" else None
    if rec is None:
        if auth:
            _hit(_FAILS, ip, 10**9, AUTH_FAILS[1])
        raise HTTPException(401, "API-Schlüssel fehlt oder ist ungültig.", headers={"WWW-Authenticate": "Bearer"})
    if "orders" not in (rec.get("scopes") or []):
        raise HTTPException(403, "Dieser Schlüssel darf keine Aufträge bearbeiten.")
    _limit(rec["id"], "read" if request.method in ("GET", "HEAD") else "write")
    touch(rec["id"])
    request.state.api_key = rec
    return rec


# every /api/v1 route depends on api_key (FastAPI caches it per request: the routes' own Depends reuse the result)
v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(api_key)])


# ---------------- order ownership ----------------

def _orders() -> dict:
    return _read("v1_orders.json", {}).get("orders", {})


def remember_order(pid: str, kid: str) -> None:
    with _LOCK:
        data = _read("v1_orders.json", {})
        data.setdefault("orders", {})[pid] = {"key": kid, "created": now()}
        _write("v1_orders.json", data)


def key_of(pid: str) -> str | None:
    rec = _orders().get(pid)
    return rec.get("key") if isinstance(rec, dict) else None


def _own(pid: str, rec: dict) -> str:
    """The order if this key created it; any other order (or none) is the same 404, so a key cannot probe ids."""
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9]{6,32}", pid) or key_of(pid) != rec["id"]:
        raise HTTPException(404, "Auftrag nicht gefunden.")
    return pid


# ---------------- agent access ----------------

def _agent():
    try:
        return importlib.import_module("agent")
    except ImportError:
        raise HTTPException(503, "Der Assistent ist noch nicht verfügbar.")


def _conversation(ag, pid: str) -> tuple[list, str | None]:
    """(all events, state) whatever shape agent.events returns (a list, or {"events", "state"})."""
    res = ag.events(pid, 0)
    state = None
    if isinstance(res, dict):
        state, res = res.get("state"), res.get("events")
    events = [e for e in res or [] if isinstance(e, dict)]
    if state is None and callable(getattr(ag, "state", None)):
        state = ag.state(pid)
    if state is None:   # SPEC: projects/agent/<pid>.json carries the state
        state = _read(f"agent/{pid}.json", {}).get("state")
    return events, state


def _event_file(pid: str, e: dict) -> str | None:
    img = e.get("image")
    if not isinstance(img, str) or not _safe_rel(img):
        return None
    ext = img.rsplit(".", 1)[-1].lower()
    return f"ereignis_{e.get('n')}.{ext}" if ext in ("png", "jpg", "webp") and isinstance(e.get("n"), int) else None


def _url(pid: str, name: str) -> str:
    return f"/api/v1/orders/{pid}/files/{name}"


def public_event(pid: str, e: dict) -> dict:
    out = {k: e.get(k) for k in ("n", "at", "role", "kind", "text") if k in e}
    if isinstance(e.get("data"), (dict, list, str, int, float, bool)):
        out["data"] = e["data"]
    f = _event_file(pid, e)
    if f:
        out["image"] = _url(pid, f)
    return out


def _questions(events: list) -> list[dict]:
    """The agent's open questions: those after the last user event."""
    last_user = max([e.get("n", 0) for e in events if e.get("role") == "user"] + [0])
    return [{"n": e.get("n"), "text": e.get("text"), "data": e.get("data")} for e in events
            if e.get("kind") == "question" and e.get("n", 0) > last_user]


# ---------------- results and files ----------------

def _safe_rel(x) -> bool:
    return isinstance(x, str) and bool(re.fullmatch(r"[\w./-]+", x)) and not x.startswith("/") and ".." not in x.split("/")


def _out_file(rel) -> Path | None:
    if not _safe_rel(rel):
        return None
    root = Path(projects.OUT).resolve()
    p = (root / rel).resolve()
    return p if root in p.parents and p.is_file() else None


def _score(v: dict) -> float:
    j = v.get("judge") if isinstance(v.get("judge"), dict) else {}
    return float(j.get("overall") or 0) + (100 if j.get("pass") else 0) + (1000 if v.get("fav") else 0)


def results(pid: str, doc: dict, limit: int = 8) -> list[dict]:
    """Variants with an image, best first (favourite, passed, score)."""
    out = []
    for v in sorted((v for v in doc.get("variants") or [] if isinstance(v, dict) and v.get("id")), key=_score, reverse=True):
        if not _out_file(v.get("image")):
            continue
        j = v.get("judge") if isinstance(v.get("judge"), dict) else {}
        out.append({"variant": v["id"], "image": _url(pid, f"bild_{v['id']}.png"), "offer": _url(pid, f"angebot_{v['id']}.txt"),
                    "score": j.get("overall"), "pass": j.get("pass"), "comment": j.get("comment"),
                    "favourite": bool(v.get("fav"))})
        if len(out) >= limit:
            break
    return out


def files(pid: str, doc: dict, events: list | None = None) -> list[dict]:
    pieces = [p for p in doc.get("pieces") or [] if isinstance(p, dict) and p.get("type") and p.get("id")]
    names = []
    if pieces:
        names += ["angebot.txt", "zeichnung.dxf"]
        for p in pieces:
            names += [f"zeichnung_{p['id']}.dxf", f"modell_{p['id']}.glb", f"modell_{p['id']}.stl", f"zuschnitt_{p['id']}.csv"]
    for r in results(pid, doc):
        names += [r["image"].rsplit("/", 1)[1], r["offer"].rsplit("/", 1)[1]]
    names += [f for f in (_event_file(pid, e) for e in events or []) if f]
    return [{"name": n, "url": _url(pid, n), "type": MEDIA[n.rsplit(".", 1)[1]].split(";")[0]} for n in names]


def _slug(s: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^A-Za-z0-9-]+", "_", s).strip("_")[:60] or "Auftrag"


def render_file(pid: str, doc: dict, name: str, events: list | None = None) -> tuple[bytes, str]:
    """(content, media type) of one listed file; HTTPException 404 for anything not in files()."""
    import dxf
    import export3d
    import offer
    m = FILE_RE.fullmatch(name or "")
    if not m:
        raise HTTPException(404, "Datei nicht gefunden.")
    kind, ident, ext = m.groups()
    pieces = {p.get("id"): p for p in doc.get("pieces") or [] if isinstance(p, dict) and p.get("type")}
    title = (doc.get("title") or "").strip()
    if kind == "bild" and ext == "png" and ident:
        v = next((v for v in doc.get("variants") or [] if isinstance(v, dict) and v.get("id") == ident), None)
        f = _out_file((v or {}).get("image"))
        if f and f.suffix == ".png":
            return f.read_bytes(), MEDIA["png"]
    elif kind == "angebot" and ext == "txt":
        try:
            return offer.offer_text(doc, ident).encode(), MEDIA["txt"]
        except KeyError:
            pass
    elif kind == "zeichnung" and ext == "dxf":
        chosen = [pieces[ident]] if ident in pieces else list(pieces.values()) if ident is None else []
        if chosen:
            items = [(p, offer.piece_dims(p), projects.PIECE_LABELS.get(p["type"], "Möbel")) for p in chosen]
            lines = [f"{lab}: {' × '.join(map(str, d))} cm, {projects.MATERIAL_LABELS.get(p.get('material'), p.get('material_custom') or '')}"
                     for p, d, lab in items]
            return dxf.drawing(items, f"{title or 'Auftrag'} – Maße in mm", lines), MEDIA["dxf"]
    elif kind in ("modell", "zuschnitt") and ident in pieces:
        p = pieces[ident]
        dims = offer.piece_dims(p)
        if kind == "modell" and ext == "glb":
            return export3d.glb({**p, "label": projects.PIECE_LABELS.get(p["type"], "Möbel")}, dims), MEDIA["glb"]
        if kind == "modell" and ext == "stl":
            return export3d.stl(p, dims), MEDIA["stl"]
        if kind == "zuschnitt" and ext == "csv":
            return ("﻿" + export3d.cut_list(p, dims)).encode(), MEDIA["csv"]
    elif kind == "ereignis" and ident and ident.isdigit():
        e = next((e for e in events or [] if e.get("n") == int(ident)), None)
        if e and _event_file(pid, e) == name:
            f = _out_file(e["image"])
            if f:
                return f.read_bytes(), MEDIA[ext]
    raise HTTPException(404, "Datei nicht gefunden.")


# ---------------- /api/v1 ----------------

async def _form(request: Request) -> tuple[bytes | None, bytes | None, str | None]:
    """(photo, audio, text) of a multipart body. Read here, after api_key ran: a request without a valid key never
    gets its upload parsed."""
    import imaging
    try:
        form = await request.form(max_files=3, max_fields=10)
    except Exception:
        raise HTTPException(400, "Bitte die Daten als multipart/form-data senden (photo, audio, text).")
    try:
        out = []
        for field, cap, what in (("photo", min(MAX_PHOTO, imaging.MAX_UPLOAD), "Das Foto"),
                                 ("audio", MAX_AUDIO, "Die Sprachnachricht")):
            f = form.get(field)
            if f is None or isinstance(f, str):
                out.append(None)
                continue
            data = await f.read(cap + 1)
            if len(data) > cap:
                raise HTTPException(413, f"{what} ist zu groß (höchstens {cap // 2**20} MB).")
            out.append(data or None)
        t = form.get("text")
        return out[0], out[1], _text(t if isinstance(t, str) else None)
    finally:
        await form.close()


def _check_photo(data: bytes | None) -> None:
    if data is None:
        return
    from PIL import Image
    import io
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        raise HTTPException(400, "Das Foto ist kein lesbares Bild.")


def _text(v) -> str | None:
    if v is None or not str(v).strip():
        return None
    if len(v) > MAX_TEXT:
        raise HTTPException(400, f"Der Text ist zu lang (höchstens {MAX_TEXT} Zeichen).")
    return str(v).strip()


def _who(rec: dict) -> str:
    return f"API: {rec['name']}"[:60]


def order_view(pid: str, after: int = 0) -> dict:
    ag = _agent()
    events, state = _conversation(ag, pid)
    try:
        doc = projects.load("workshop", pid)
    except HTTPException:
        raise HTTPException(404, "Auftrag nicht gefunden.")
    return {"order": pid, "title": doc.get("title"), "state": state, "questions": _questions(events),
            "events": [public_event(pid, e) for e in events if isinstance(e.get("n"), int) and e["n"] > after],
            "next": max([e["n"] for e in events if isinstance(e.get("n"), int)] + [0]),
            "results": results(pid, doc), "files": files(pid, doc, events)}


@v1.post("/orders", status_code=201)
async def v1_create(request: Request, rec: dict = Depends(api_key)):
    photo_b, audio_b, text = await _form(request)
    if not (photo_b or audio_b or text):
        raise HTTPException(400, "Bitte ein Foto und eine Beschreibung (Text oder Sprachnachricht) senden.")
    await run_in_threadpool(_check_photo, photo_b)
    ag = _agent()
    _limit(rec["id"], "create")
    pid = await run_in_threadpool(lambda: ag.start(photo_b, _who(rec), "api", text=text, audio=audio_b))
    if not isinstance(pid, str):
        raise HTTPException(500, "Der Assistent hat keinen Auftrag angelegt.")
    remember_order(pid, rec["id"])
    return await run_in_threadpool(order_view, pid)


@v1.get("/orders/{pid}")
def v1_get(pid: str, after: int = 0, rec: dict = Depends(api_key)):
    return order_view(_own(pid, rec), max(0, after))


@v1.post("/orders/{pid}/messages")
async def v1_message(pid: str, request: Request, rec: dict = Depends(api_key)):
    _own(pid, rec)
    photo_b, audio_b, text = await _form(request)
    if not (photo_b or audio_b or text):
        raise HTTPException(400, "Bitte einen Text, eine Sprachnachricht oder ein Foto senden.")
    await run_in_threadpool(_check_photo, photo_b)
    ag = _agent()
    new = await run_in_threadpool(lambda: ag.handle(pid, _who(rec), "api", text=text, audio=audio_b, photo=photo_b))
    view = await run_in_threadpool(order_view, pid)
    view["new"] = [public_event(pid, e) for e in new or [] if isinstance(e, dict)]
    return view


@v1.get("/orders/{pid}/files")
def v1_files(pid: str, rec: dict = Depends(api_key)):
    _own(pid, rec)
    events, _ = _conversation(_agent(), pid)
    return {"files": files(pid, projects.load("workshop", pid), events)}


@v1.get("/orders/{pid}/files/{name}")
def v1_file(pid: str, name: str, rec: dict = Depends(api_key)):
    _own(pid, rec)
    try:
        doc = projects.load("workshop", pid)
    except HTTPException:
        raise HTTPException(404, "Auftrag nicht gefunden.")
    events = _conversation(_agent(), pid)[0] if name.startswith("ereignis_") else None
    data, media = render_file(pid, doc, name, events)
    fn = f"{_slug(doc.get('title') or 'Auftrag')}_{name}"
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{fn}"',
                                                     "Cache-Control": "no-store"})


# ---------------- webhooks ----------------

def _hooks_cfg() -> dict:
    data = _read("webhooks.json", {})
    data["enabled"] = data.get("enabled") is True
    data["hooks"] = [h for h in data.get("hooks") or [] if isinstance(h, dict) and h.get("id")]
    return data


def _hook_view(h: dict) -> dict:
    return {k: h.get(k) for k in ("id", "url", "events", "enabled", "key", "created", "last")} | \
        {"lan": h.get("lan") is True, "secret_hint": f"…{str(h.get('secret') or '')[-4:]}"}


def password_set() -> bool:
    return bool(projects.settings()["access"]["password"])


def require_password(what: str) -> None:
    if not password_set():
        raise HTTPException(409, f"{what} geht erst mit Team-Passwort. " + NEED_PASSWORD)


_OWN = {"at": 0.0, "ips": frozenset()}


def _own_ips() -> frozenset:
    """This machine's interface addresses (its public IPv6 too): a webhook never goes back into this machine."""
    if time.monotonic() - _OWN["at"] > 60:
        try:
            import psutil
            ips = {a.address.split("%")[0] for addrs in psutil.net_if_addrs().values() for a in addrs
                   if a.family in (socket.AF_INET, socket.AF_INET6)}
            _OWN["ips"] = frozenset(str(ipaddress.ip_address(x)) for x in ips)
        except Exception:
            traceback.print_exc()
        _OWN["at"] = time.monotonic()
    return _OWN["ips"]


def _allowed_ip(ip, lan: bool = False) -> bool:
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if str(ip) in _own_ips() or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast \
            or ip.is_reserved:
        return False
    return ip.is_global or (lan and ip.is_private)


_LOCAL_NAMES = (".local", ".lan", ".internal", ".home.arpa", ".fritz.box")


def _name_refused(host: str, lan: bool) -> bool:
    return host == "localhost" or host.endswith(".localhost") or (not lan and host.endswith(_LOCAL_NAMES))


def _resolve(host: str, port: int) -> list[str]:
    """All addresses of `host` (an IP literal is itself). Raises OSError when the name does not resolve."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    infos = RESOLVE(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0].split("%")[0] for i in infos))


INTERNAL = ("Webhooks dürfen nur an Adressen im Internet gehen – nicht an diesen Rechner oder Geräte im eigenen Netz "
            "(dafür „Adresse im Firmennetz“ wählen).")
INTERNAL_LAN = "Diese Adresse ist nicht erlaubt: Webhooks gehen nie an diesen Rechner selbst."


def _check_url(url, lan: bool = False) -> str:
    u = None
    try:
        u = urlsplit(url) if isinstance(url, str) and len(url) <= 500 else None
        port = u.port if u else None
    except ValueError:
        u = None
    if u is None or u.scheme not in ("http", "https") or not u.hostname or any(c in url for c in " \r\n\t"):
        raise HTTPException(400, "Bitte eine gültige http(s)-Adresse angeben.")
    host = u.hostname.rstrip(".").lower()
    if _name_refused(host, lan):
        raise HTTPException(400, INTERNAL_LAN if lan else INTERNAL)
    try:
        ips = _resolve(host, port or (443 if u.scheme == "https" else 80))
    except (OSError, UnicodeError):
        ips = []          # not resolvable now: checked again before every delivery
    if any(not _allowed_ip(ip, lan) for ip in ips):
        raise HTTPException(400, INTERNAL_LAN if lan else INTERNAL)
    return url


def _target(url: str, lan: bool = False) -> tuple[str, str, dict]:
    """→ (url with the checked IP, Host header, httpx extensions). Resolves once and connects to exactly that address,
    so a DNS answer that changes between check and request (rebinding) cannot reach an internal address.
    Raises PermissionError for an internal address, OSError when the name does not resolve."""
    u = urlsplit(url)
    host = u.hostname.rstrip(".").lower()
    port = u.port or (443 if u.scheme == "https" else 80)
    if _name_refused(host, lan):
        raise PermissionError(host)
    ips = _resolve(host, port)
    if not ips or any(not _allowed_ip(ip, lan) for ip in ips):
        raise PermissionError(host)
    ip = ips[0]
    netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{u.port}" if u.port else "")
    pinned = u._replace(netloc=netloc).geturl()
    ext = {"sni_hostname": u.hostname} if u.scheme == "https" else {}
    return pinned, u.netloc.rsplit("@", 1)[-1], ext


def _check_events(ev) -> list[str]:
    ev = list(EVENTS) if ev is None else ev
    if not isinstance(ev, list) or not ev or any(e not in EVENTS for e in ev):
        raise HTTPException(400, f"Unbekanntes Ereignis (erlaubt: {', '.join(EVENTS)}).")
    return sorted(set(ev))


def _check_hook_key(k):
    if k is not None and not any(x["id"] == k for x in list_keys()):
        raise HTTPException(400, "Unbekannter API-Schlüssel.")
    return k


def webhooks_view() -> dict:
    cfg = _hooks_cfg()
    return {"enabled": cfg["enabled"], "events": list(EVENTS), "hooks": [_hook_view(h) for h in cfg["hooks"]],
            "recent": list(_RECENT), "password_set": password_set()}


def set_webhooks_enabled(on: bool) -> dict:
    with _LOCK:
        cfg = _hooks_cfg()
        cfg["enabled"] = bool(on)
        _write("webhooks.json", cfg)
    return webhooks_view()


def add_hook(url, events=None, key=None, enabled=False, lan=False) -> tuple[dict, str]:
    """→ (view, secret). The secret is returned once; receivers verify X-Werkstatt-Signature with it.
    lan: the owner allows a private LAN address for this hook (never this machine)."""
    lan = lan is True
    h = {"id": secrets.token_hex(4), "url": _check_url(url, lan), "events": _check_events(events),
         "key": _check_hook_key(key), "enabled": enabled is True, "lan": lan, "secret": secrets.token_urlsafe(32),
         "created": now(), "last": None}
    with _LOCK:
        cfg = _hooks_cfg()
        if len(cfg["hooks"]) >= MAX_HOOKS:
            raise HTTPException(400, f"Höchstens {MAX_HOOKS} Webhooks.")
        cfg["hooks"].append(h)
        _write("webhooks.json", cfg)
    return _hook_view(h), h["secret"]


def edit_hook(hid: str, patch: dict) -> dict:
    with _LOCK:
        cfg = _hooks_cfg()
        h = next((x for x in cfg["hooks"] if x["id"] == hid), None)
        if h is None:
            raise HTTPException(404, "Webhook nicht gefunden.")
        if "lan" in patch:
            if not isinstance(patch["lan"], bool):
                raise HTTPException(400, "lan muss true oder false sein.")
            h["lan"] = patch["lan"]
        if "url" in patch or "lan" in patch:
            h["url"] = _check_url(patch.get("url", h["url"]), h.get("lan") is True)
        if "events" in patch:
            h["events"] = _check_events(patch["events"])
        if "key" in patch:
            h["key"] = _check_hook_key(patch["key"])
        if "enabled" in patch:
            if not isinstance(patch["enabled"], bool):
                raise HTTPException(400, "enabled muss true oder false sein.")
            h["enabled"] = patch["enabled"]
        _write("webhooks.json", cfg)
    return _hook_view(h)


def delete_hook(hid: str) -> bool:
    with _LOCK:
        cfg = _hooks_cfg()
        rest = [h for h in cfg["hooks"] if h["id"] != hid]
        if len(rest) == len(cfg["hooks"]):
            return False
        cfg["hooks"] = rest
        _write("webhooks.json", cfg)
    return True


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: str, ts: str, body: bytes, header: str, tolerance: float = 900) -> bool:
    """What a receiver does (documented for integrators): recompute, compare in constant time, reject old timestamps."""
    try:
        fresh = abs(clock() - int(ts)) <= tolerance
    except (TypeError, ValueError):
        return False
    return fresh and hmac.compare_digest(sign(secret, ts, body), header or "")


class _Delivery:
    __slots__ = ("id", "hook", "event", "body", "attempt")

    def __init__(self, hook: str, event: str, body: bytes):
        self.id, self.hook, self.event, self.body, self.attempt = uuid.uuid4().hex, hook, event, body, 0


_HEAP: list = []
_SEQ = itertools.count()
_COND = threading.Condition()
_STATE = {"thread": None, "stop": False, "busy": 0}
_RECENT: list = []


def _ensure_worker() -> None:
    with _COND:
        t = _STATE["thread"]
        if t is None or not t.is_alive():
            _STATE["stop"] = False
            t = threading.Thread(target=_worker, daemon=True, name="webhooks")
            _STATE["thread"] = t
            t.start()


def emit(event: str, pid: str, data: dict) -> int:
    """Queues `event` for every enabled hook that wants it (master switch on). Returns the number of deliveries."""
    if event not in EVENTS and event != "ping":
        return 0
    cfg = _hooks_cfg()
    if not cfg["enabled"] or not password_set():
        return 0
    owner = key_of(pid) if pid else None
    n = 0
    for h in cfg["hooks"]:
        if not h.get("enabled") or (event != "ping" and event not in (h.get("events") or [])):
            continue
        if h.get("key") and event != "ping" and h["key"] != owner:   # a hook bound to a key sees only that key's orders
            continue
        body = json.dumps({"event": event, "order": pid, "at": now(), "data": data}, ensure_ascii=False).encode()
        _push(_Delivery(h["id"], event, body), 0.0)
        n += 1
    if n:
        _ensure_worker()
    return n


def _push(d: _Delivery, delay: float) -> None:
    with _COND:
        heapq.heappush(_HEAP, (time.monotonic() + delay, next(_SEQ), d))
        _COND.notify_all()


def _record(d: _Delivery, status, ok: bool, error: str | None) -> None:
    last = {"at": now(), "event": d.event, "status": status, "ok": ok, "attempts": d.attempt, "error": error,
            "delivery": d.id}
    _RECENT.insert(0, {"hook": d.hook, **last})
    del _RECENT[RECENT_MAX:]
    with _LOCK:
        cfg = _hooks_cfg()
        for h in cfg["hooks"]:
            if h["id"] == d.hook:
                h["last"] = last
                _write("webhooks.json", cfg)
                break


def _send(d: _Delivery) -> tuple[bool, bool, object, str | None]:
    """One attempt → (ok, retry, status, error). Re-reads the config: a hook switched off or deleted stops at once."""
    cfg = _hooks_cfg()
    h = next((x for x in cfg["hooks"] if x["id"] == d.hook), None)
    if not cfg["enabled"] or h is None or not h.get("enabled"):
        return False, False, None, "ausgeschaltet"
    if not password_set():
        return False, False, None, "ausgeschaltet"
    try:
        url, host, ext = _target(h["url"], h.get("lan") is True)
    except PermissionError:
        return False, False, None, "Adresse im internen Netz – nicht erlaubt"   # never contacted, no status to leak
    except (OSError, UnicodeError):
        return False, True, None, "Adresse nicht auflösbar"
    ts = str(int(clock()))
    headers = {"Content-Type": "application/json", "User-Agent": "Werkstatt-Webhook/1", "X-Werkstatt-Event": d.event,
               "X-Werkstatt-Delivery": d.id, "X-Werkstatt-Timestamp": ts, "X-Werkstatt-Signature": sign(h["secret"], ts, d.body),
               "Host": host}
    try:
        with httpx.Client(transport=TRANSPORT, timeout=HOOK_TIMEOUT, follow_redirects=False, trust_env=False) as c:
            r = c.post(url, content=d.body, headers=headers, extensions=ext)
    except httpx.HTTPError as e:
        return False, True, None, type(e).__name__
    if 200 <= r.status_code < 300:
        return True, False, r.status_code, None
    return False, r.status_code >= 500 or r.status_code in (408, 429), r.status_code, f"HTTP {r.status_code}"


def _worker() -> None:
    while True:
        with _COND:
            while not _STATE["stop"] and (not _HEAP or _HEAP[0][0] > time.monotonic()):
                _COND.wait(None if not _HEAP else max(0.0, _HEAP[0][0] - time.monotonic()))
            if _STATE["stop"]:
                return
            d = heapq.heappop(_HEAP)[2]
            _STATE["busy"] += 1
        try:
            d.attempt += 1
            ok, retry, status, err = _send(d)
            if retry and d.attempt <= len(RETRIES):
                _push(d, RETRIES[d.attempt - 1])
            elif err != "ausgeschaltet":
                _record(d, status, ok, err)
        except Exception:
            traceback.print_exc()
        finally:
            with _COND:
                _STATE["busy"] -= 1
                _COND.notify_all()


def flush(timeout: float = 10.0) -> bool:
    """Waits until nothing is queued or being sent (tests, shutdown). False on timeout."""
    end = time.monotonic() + timeout
    with _COND:
        while _HEAP or _STATE["busy"]:
            left = end - time.monotonic()
            if left <= 0:
                return False
            _COND.wait(min(left, 0.05))
    return True


def stop() -> None:
    """Server lifespan: ends the worker; queued deliveries are dropped (they are best effort, not a store)."""
    with _COND:
        _STATE["stop"] = True
        _HEAP.clear()
        _COND.notify_all()
    t = _STATE["thread"]
    if t is not None:
        t.join(timeout=HOOK_TIMEOUT + 1)
    _STATE["thread"] = None


def on_agent_event(pid, event=None) -> None:
    """agent.subscribe callback: questions, previews and results go out as webhooks (nothing when switched off).
    Accepts cb(pid, event) and cb(event) with event["pid"]."""
    if event is None and isinstance(pid, dict):
        pid, event = pid.get("pid"), pid
    if not isinstance(event, dict) or not isinstance(pid, str):
        return
    name = KIND_EVENT.get(event.get("kind"))
    if not name:
        return
    try:
        data = public_event(pid, event)
        if name == "order.result":
            try:
                doc = projects.load("workshop", pid)
                data["results"], data["files"] = results(pid, doc), files(pid, doc)
            except HTTPException:
                pass
        emit(name, pid, data)
    except Exception:
        traceback.print_exc()   # a webhook problem must never break the conversation


# ---------------- team routes (keys, webhooks) ----------------

def _owner(request: Request) -> str:
    return getattr(request.state, "owner", None) or "anon"


@router.get("/api/keys")
def get_keys():
    return {"keys": list_keys(), "scopes": list(SCOPES), "password_set": password_set()}


@router.post("/api/keys")
def post_key(request: Request, body: dict | None = None):
    require_password("Ein API-Schlüssel")
    body = body or {}
    view, plain = create_key(body.get("name"), body.get("scopes"), _owner(request))
    return {**view, "key": plain, "note": "Der Schlüssel wird nur jetzt angezeigt – bitte sicher aufbewahren."}


@router.delete("/api/keys/{kid}")
def remove_key(kid: str):
    if not delete_key(kid):
        raise HTTPException(404, "Schlüssel nicht gefunden.")
    return {"ok": True}


@router.get("/api/webhooks")
def get_webhooks():
    return webhooks_view()


@router.put("/api/webhooks")
def put_webhooks(body: dict):
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(400, "enabled muss true oder false sein.")
    if body["enabled"]:
        require_password("Webhooks einschalten")
    return set_webhooks_enabled(body["enabled"])


@router.post("/api/webhooks")
def post_webhook(body: dict):
    if body.get("enabled") is True or body.get("lan") is True:
        require_password("Webhooks einschalten" if body.get("enabled") is True else "Eine Adresse im Firmennetz")
    view, secret = add_hook(body.get("url"), body.get("events"), body.get("key"), body.get("enabled") is True,
                            body.get("lan") is True)
    return {**view, "secret": secret, "note": "Das Geheimnis wird nur jetzt angezeigt – damit prüft der Empfänger die Signatur."}


@router.patch("/api/webhooks/{hid}")
def patch_webhook(hid: str, body: dict):
    if isinstance(body, dict) and body.get("enabled") is True:
        require_password("Webhooks einschalten")
    if isinstance(body, dict) and body.get("lan") is True:
        require_password("Eine Adresse im Firmennetz")
    return edit_hook(hid, body)


@router.delete("/api/webhooks/{hid}")
def remove_webhook(hid: str):
    if not delete_hook(hid):
        raise HTTPException(404, "Webhook nicht gefunden.")
    return {"ok": True}


@router.post("/api/webhooks/{hid}/test")
def test_webhook(hid: str):
    """Sends a "ping" to this hook — only when webhooks and the hook are switched on (it contacts the URL)."""
    cfg = _hooks_cfg()
    h = next((x for x in cfg["hooks"] if x["id"] == hid), None)
    if h is None:
        raise HTTPException(404, "Webhook nicht gefunden.")
    if not cfg["enabled"] or not h.get("enabled"):
        raise HTTPException(409, "Webhooks sind ausgeschaltet – erst einschalten, dann testen.")
    require_password("Webhooks testen")
    body = json.dumps({"event": "ping", "order": None, "at": now(), "data": {"hook": hid}}).encode()
    _push(_Delivery(hid, "ping", body), 0.0)
    _ensure_worker()
    return {"queued": True}


@router.get("/api/workshop/{pid}/export/{name}")
def team_export(pid: str, name: str):
    """The same files for the web app (team): offer text, DXF, models, images of any order (no key, no ownership)."""
    try:
        doc = projects.load("workshop", pid)
    except HTTPException:
        raise HTTPException(404, "Auftrag nicht gefunden.")
    events = _conversation(_agent(), pid)[0] if name.startswith("ereignis_") else None
    data, media = render_file(pid, doc, name, events)
    return Response(data, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{_slug(doc.get("title") or "Auftrag")}_{name}"', "Cache-Control": "no-store"})
