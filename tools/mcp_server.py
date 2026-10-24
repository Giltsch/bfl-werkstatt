#!/usr/bin/env python3
"""Werkstatt as an MCP server (stdio, hand-rolled JSON-RPC 2.0 — the `mcp` package is not installed).

An MCP client (Claude Desktop, Claude Code, …) starts it as a subprocess; it talks to the local Werkstatt /api/v1 with
an API key (Einstellungen → API & Webhooks). Nothing listens on a port: stdin/stdout only, log lines go to stderr.

    WERKSTATT_API_KEY=wk_… .venv/bin/python tools/mcp_server.py
    env: WERKSTATT_API_URL (default http://127.0.0.1:7860/api/v1)

Claude Code:  claude mcp add werkstatt -e WERKSTATT_API_KEY=wk_… -- /path/.venv/bin/python /path/tools/mcp_server.py

Tools: create_order(photo_path, text), send_message(order, text), get_order(order), get_results(order)."""

import base64
import io
import json
import mimetypes
import os
import sys
from pathlib import Path

import httpx

NAME, VERSION = "werkstatt", "1.0.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")   # newest first; we answer with the client's if we know it
DEFAULT_URL = "http://127.0.0.1:7860/api/v1"
TIMEOUT = 120.0                                            # creating an order uploads a photo and analyses the room
MAX_FILE = 30 * 2**20
THUMB = 1024                                               # include_images: long side of the embedded JPEGs

TOOLS = [
    {"name": "create_order",
     "description": "Neuen Werkstatt-Auftrag anlegen: Foto des Kundenraums + Beschreibung der Möbel auf Deutsch "
                    "(z. B. 'links neben dem Fenster ein Einbauschrank, 220 breit, raumhoch, Weißlack, drei Türen grifflos'). "
                    "Der Assistent analysiert den Raum, fragt bei Bedarf nach und erzeugt Entwürfe. Gibt die Auftrags-ID zurück.",
     "inputSchema": {"type": "object", "properties": {
         "photo_path": {"type": "string", "description": "Absoluter Pfad zum Raumfoto (JPEG/PNG) auf diesem Rechner."},
         "text": {"type": "string", "description": "Beschreibung der gewünschten Möbel."},
         "audio_path": {"type": "string", "description": "Optional: Sprachnachricht (ogg/webm/m4a/wav/mp3) statt Text."}},
         "required": ["photo_path", "text"], "additionalProperties": False}},
    {"name": "send_message",
     "description": "Nachricht an den Assistenten eines Auftrags: Antwort auf eine Rückfrage, Korrektur ('achtzig tief', "
                    "'nimm Eiche'), Freigabe der Vorschau ('ja') oder 'nochmal'.",
     "inputSchema": {"type": "object", "properties": {
         "order": {"type": "string", "description": "Auftrags-ID aus create_order."},
         "text": {"type": "string", "description": "Die Nachricht."},
         "photo_path": {"type": "string", "description": "Optional: ein weiteres Foto."}},
         "required": ["order", "text"], "additionalProperties": False}},
    {"name": "get_order",
     "description": "Stand eines Auftrags: Zustand, offene Rückfragen, Verlauf (ab Ereignis 'after').",
     "inputSchema": {"type": "object", "properties": {
         "order": {"type": "string"}, "after": {"type": "integer", "minimum": 0, "default": 0}},
         "required": ["order"], "additionalProperties": False}},
    {"name": "get_results",
     "description": "Ergebnisse eines Auftrags: beste Entwürfe mit Bewertung, Angebotstext (Positionen) und Dateien "
                    "(Bilder, DXF-Zeichnung, 3D-Modell, Zuschnittliste). Optional Dateien in einen Ordner laden.",
     "inputSchema": {"type": "object", "properties": {
         "order": {"type": "string"},
         "include_images": {"type": "boolean", "default": False, "description": "Die besten Entwürfe als Bilder mitsenden."},
         "download_dir": {"type": "string", "description": "Optional: Ordner, in den alle Dateien geladen werden."}},
         "required": ["order"], "additionalProperties": False}},
]


class ToolError(Exception):
    """A failure the model should read (returned as isError content, not as a protocol error)."""


def log(*a):
    print("[werkstatt-mcp]", *a, file=sys.stderr, flush=True)


class Api:
    def __init__(self, base: str, key: str | None, transport=None):
        self.base, self.key, self.transport = base.rstrip("/"), key, transport

    def _client(self):
        if not self.key:
            raise ToolError("WERKSTATT_API_KEY ist nicht gesetzt (Einstellungen → API & Webhooks → Schlüssel anlegen).")
        return httpx.Client(base_url=self.base, transport=self.transport, timeout=TIMEOUT, follow_redirects=False,
                            headers={"Authorization": f"Bearer {self.key}", "User-Agent": f"werkstatt-mcp/{VERSION}"})

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            with self._client() as c:
                r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ToolError(f"Werkstatt nicht erreichbar ({self.base}): {type(e).__name__}")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise ToolError(f"Werkstatt-Fehler {r.status_code}: {detail or r.text[:300]}")
        return r

    def json(self, method: str, path: str, **kw) -> dict:
        return self.request(method, path, **kw).json()

    def base_path(self) -> str:
        """'/api/v1' of base: file URLs in responses are absolute paths on the server."""
        return httpx.URL(self.base).path.rstrip("/")


def _order_id(v) -> str:
    if not isinstance(v, str) or not v.isalnum() or not 6 <= len(v) <= 32:
        raise ToolError("Ungültige Auftrags-ID.")
    return v.lower()


def _file(path, what: str) -> tuple[str, bytes, str]:
    if not isinstance(path, str) or not path.strip():
        raise ToolError(f"{what}: bitte einen Dateipfad angeben.")
    p = Path(path).expanduser()
    if not p.is_file():
        raise ToolError(f"{what}: Datei nicht gefunden: {p}")
    if p.stat().st_size > MAX_FILE:
        raise ToolError(f"{what}: Datei zu groß (höchstens {MAX_FILE // 2**20} MB).")
    return p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def _summary(view: dict) -> str:
    lines = [f"Auftrag {view.get('order')}: {view.get('title') or ''}".rstrip(": "), f"Zustand: {view.get('state') or 'unbekannt'}"]
    for q in view.get("questions") or []:
        lines.append(f"Rückfrage: {q.get('text')}")
    for e in (view.get("new") or view.get("events") or [])[-8:]:
        if e.get("role") == "agent" and e.get("text"):
            lines.append(f"[{e.get('kind')}] {e.get('text')}")
    if view.get("results"):
        lines.append(f"{len(view['results'])} Entwürfe fertig – get_results zeigt sie.")
    return "\n".join(lines)


def _rel(api: Api, url: str) -> str:
    """Server path '/api/v1/orders/…' → client path relative to base."""
    bp = api.base_path()
    return url[len(bp):] if bp and url.startswith(bp) else url


def _thumb(data: bytes) -> tuple[str, str]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB, THUMB))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:   # no PIL in the client's Python: send the original
        return base64.b64encode(data).decode(), "image/png"


def call_tool(api: Api, name: str, args: dict) -> list[dict]:
    """→ MCP content list. ToolError for failures the model should see."""
    if name == "create_order":
        files = {"photo": _file(args.get("photo_path"), "Foto")}
        if args.get("audio_path"):
            files["audio"] = _file(args["audio_path"], "Sprachnachricht")
        text = args.get("text")
        if not isinstance(text, str):
            raise ToolError("Bitte eine Beschreibung (text) angeben.")
        view = api.json("POST", "/orders", files=files, data={"text": text} if text.strip() else {})
        return [{"type": "text", "text": _summary(view)}, {"type": "text", "text": json.dumps(view, ensure_ascii=False)}]
    if name == "send_message":
        pid = _order_id(args.get("order"))
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("Bitte eine Nachricht (text) angeben.")
        files = {"photo": _file(args["photo_path"], "Foto")} if args.get("photo_path") else None
        view = api.json("POST", f"/orders/{pid}/messages", data={"text": text}, files=files)
        return [{"type": "text", "text": _summary(view)}, {"type": "text", "text": json.dumps(view, ensure_ascii=False)}]
    if name == "get_order":
        pid = _order_id(args.get("order"))
        after = args.get("after") or 0
        if not isinstance(after, int) or after < 0:
            raise ToolError("after muss eine Zahl ≥ 0 sein.")
        view = api.json("GET", f"/orders/{pid}", params={"after": after})
        return [{"type": "text", "text": _summary(view)}, {"type": "text", "text": json.dumps(view, ensure_ascii=False)}]
    if name == "get_results":
        pid = _order_id(args.get("order"))
        view = api.json("GET", f"/orders/{pid}")
        res, files_ = view.get("results") or [], view.get("files") or []
        out = []
        if not res:
            out.append({"type": "text", "text": f"Noch keine Entwürfe (Zustand: {view.get('state') or 'unbekannt'})."})
        else:
            out.append({"type": "text", "text": "\n".join(
                f"Entwurf {r['variant']}: Bewertung {r.get('score') if r.get('score') is not None else '–'}"
                f"{' (bestanden)' if r.get('pass') else ''}{' ★' if r.get('favourite') else ''}" for r in res)})
        offer_url = res[0]["offer"] if res else next((f["url"] for f in files_ if f["name"] == "angebot.txt"), None)
        if offer_url:
            out.append({"type": "text", "text": api.request("GET", _rel(api, offer_url)).text})
        if args.get("include_images"):
            for r in res[:3]:
                data, mime = _thumb(api.request("GET", _rel(api, r["image"])).content)
                out.append({"type": "image", "data": data, "mimeType": mime})
        if args.get("download_dir"):
            d = Path(args["download_dir"]).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            saved = []
            for f in files_:
                name = Path(f["name"]).name
                (d / name).write_bytes(api.request("GET", _rel(api, f["url"])).content)
                saved.append(str(d / name))
            out.append({"type": "text", "text": "Gespeichert:\n" + "\n".join(saved) if saved else "Keine Dateien."})
        out.append({"type": "text", "text": json.dumps({"results": res, "files": files_}, ensure_ascii=False)})
        return out
    raise KeyError(name)


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg, api: Api) -> dict | None:
    """One JSON-RPC message → response (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:   # notification (notifications/initialized, notifications/cancelled …): no answer
        return None
    if not isinstance(params, dict):
        return _error(mid, -32602, "Invalid params")
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": "Werkstatt (Schreinerei): Aufträge per Raumfoto + Beschreibung anlegen, Rückfragen beantworten, "
                            "Entwürfe, Angebotstext und Dateien abholen. Ablauf: create_order → get_order (Rückfragen) → "
                            "send_message → … → get_results."}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(args, dict) or name not in {t["name"] for t in TOOLS}:
            return _error(mid, -32602, f"Unknown tool: {name}")
        try:
            content, err = call_tool(api, name, args), False
        except ToolError as e:
            content, err = [{"type": "text", "text": str(e)}], True
        except Exception as e:   # a bug here must not kill the session
            log("tool failed:", repr(e))
            content, err = [{"type": "text", "text": f"Interner Fehler: {type(e).__name__}"}], True
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": err}}
    return _error(mid, -32601, f"Method not found: {method}")


def serve(stdin=None, stdout=None, api: Api | None = None) -> None:
    """Newline-delimited JSON-RPC on stdin/stdout (the MCP stdio transport) until EOF."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    api = api or Api(os.environ.get("WERKSTATT_API_URL") or DEFAULT_URL, os.environ.get("WERKSTATT_API_KEY"))
    log(f"ready ({api.base})")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            replies = [_error(None, -32700, "Parse error")]
        else:
            if isinstance(msg, list):   # a batch (older protocol versions) is answered with one array
                replies = [r for r in (handle(m, api) for m in msg) if r is not None] if msg else None
                if replies:
                    stdout.write(json.dumps(replies, ensure_ascii=False) + "\n")
                    stdout.flush()
                replies = [] if msg else [_error(None, -32600, "Invalid Request")]
            else:
                replies = [r for r in [handle(msg, api)] if r is not None]
        for r in replies:
            stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
        stdout.flush()


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout):   # JSON-RPC is UTF-8 whatever the locale says
        stream.reconfigure(encoding="utf-8")
    serve()
