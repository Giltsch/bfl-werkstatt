"""tools/mcp_server.py: JSON-RPC handling in-process, then the real script as a subprocess speaking MCP over stdio to
a test-only HTTP server on 127.0.0.1 (ephemeral port) that serves connect.v1 with the fake agent of test_connect."""
import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

import connect
import mcp_server
from test_connect import env, png_bytes  # noqa: F401  (fixture)


def rpc(lines, api=None):
    out = io.StringIO()
    mcp_server.serve(io.StringIO("".join(json.dumps(m) + "\n" if not isinstance(m, str) else m for m in lines)), out,
                     api or mcp_server.Api("http://127.0.0.1:9/api/v1", "wk_x"))
    return [json.loads(l) for l in out.getvalue().splitlines()]


def test_protocol_basics():
    r = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": "p", "method": "ping"},
             {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
             "this is not json\n",
             {"jsonrpc": "1.0", "id": 4, "method": "ping"},
             {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}},
             [{"jsonrpc": "2.0", "id": 6, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}]])
    init = r[0]["result"]
    assert r[0]["id"] == 1 and init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "werkstatt"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    names = [t["name"] for t in r[1]["result"]["tools"]]
    assert names == ["create_order", "send_message", "get_order", "get_results"]
    assert all(t["inputSchema"]["type"] == "object" for t in r[1]["result"]["tools"])
    assert r[2] == {"jsonrpc": "2.0", "id": "p", "result": {}}
    assert r[3]["error"]["code"] == -32601
    assert r[4]["error"]["code"] == -32700 and r[4]["id"] is None
    assert r[5]["error"]["code"] == -32600
    assert r[6]["error"]["code"] == -32602
    assert r[7] == [{"jsonrpc": "2.0", "id": 6, "result": {}}]
    assert len(r) == 8   # the notification got no answer
    old = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}])
    assert old[0]["result"]["protocolVersion"] == mcp_server.PROTOCOLS[0]


def test_tool_errors_are_results_not_protocol_errors(tmp_path):
    def call(name, args, api=None):
        return rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}], api)[0]

    r = call("create_order", {"photo_path": str(tmp_path / "nope.jpg"), "text": "Schrank"})
    assert r["result"]["isError"] is True and "nicht gefunden" in r["result"]["content"][0]["text"]
    r = call("get_order", {"order": "../../x"})
    assert r["result"]["isError"] and "Ungültige Auftrags-ID" in r["result"]["content"][0]["text"]
    r = call("get_order", {"order": "abcdef123456"}, mcp_server.Api("http://127.0.0.1:9/api/v1", None))
    assert r["result"]["isError"] and "WERKSTATT_API_KEY" in r["result"]["content"][0]["text"]
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(401, json={"detail": "API-Schlüssel fehlt oder ist ungültig."})
    api = mcp_server.Api("http://werkstatt.test/api/v1", "wk_bad", httpx.MockTransport(handler))
    r = call("get_order", {"order": "abcdef123456"}, api)
    assert r["result"]["isError"] and "401" in r["result"]["content"][0]["text"]
    assert seen[0].headers["authorization"] == "Bearer wk_bad" and seen[0].url.path == "/api/v1/orders/abcdef123456"


# ---------------- the real script against a test-only HTTP server ----------------

@pytest.fixture
def server(env):
    """connect.v1 on 127.0.0.1:<ephemeral> for this test only (uvicorn in a thread on a socket bound here)."""
    app = FastAPI()
    app.include_router(connect.v1)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    srv.install_signal_handlers = lambda: None
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started
    yield env, f"http://127.0.0.1:{port}/api/v1"
    srv.should_exit = True
    t.join(5)
    sock.close()


class Session:
    def __init__(self, url, key):
        e = {**os.environ, "WERKSTATT_API_URL": url, "WERKSTATT_API_KEY": key, "PYTHONIOENCODING": "utf-8"}
        self.p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mcp_server.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, text=True, encoding="utf-8")
        self.n = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        r = json.loads(self.p.stdout.readline())
        assert r["id"] == self.n
        return r

    def tool(self, name, **args):
        r = self.send("tools/call", {"name": name, "arguments": args})["result"]
        return r, [c for c in r["content"] if c["type"] == "text"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)
        return self.p.stderr.read()


def test_mcp_subprocess_end_to_end(server, tmp_path):
    env, url = server
    view, key = connect.create_key("MCP")
    photo = tmp_path / "raum.png"
    photo.write_bytes(png_bytes())
    s = Session(url, key)
    try:
        init = s.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "pytest", "version": "1"}})
        assert init["result"]["serverInfo"]["name"] == "werkstatt"
        s.send("notifications/initialized", notify=True)
        assert len(s.send("tools/list")["result"]["tools"]) == 4
        assert s.send("ping")["result"] == {}

        r, texts = s.tool("create_order", photo_path=str(photo), text="Links ein Einbauschrank, zwei-zwanzig breit, Weißlack")
        assert r["isError"] is False, texts
        created = json.loads(texts[-1]["text"])
        pid = created["order"]
        assert "Rückfrage: Wie tief soll der Schrank sein?" in texts[0]["text"]
        call = env.agent.calls[0]
        assert call[1] == png_bytes() and call[2] == "API: MCP" and call[4].endswith("Weißlack")

        r, texts = s.tool("get_order", order=pid, after=1)
        assert json.loads(texts[-1]["text"])["state"] == "asking" and [e["n"] for e in json.loads(texts[-1]["text"])["events"]] == [2, 3]

        r, texts = s.tool("send_message", order=pid, text="sechzig tief")
        assert r["isError"] is False and env.agent.calls[-1][:5] == ("handle", pid, "API: MCP", "api", "sechzig tief")
        assert "Zustand: preview" in texts[0]["text"]

        out = tmp_path / "dl"
        r, texts = s.tool("get_results", order=pid, include_images=True, download_dir=str(out))
        assert r["isError"] is False
        assert texts[0]["text"].startswith("Entwurf vb22: Bewertung 7.4 (bestanden)")
        assert "Pos. 1" in texts[1]["text"] and "Lieferung und fachgerechte Montage" in texts[1]["text"]
        images = [c for c in r["content"] if c["type"] == "image"]
        assert len(images) == 2 and images[0]["mimeType"] == "image/jpeg" and base64.b64decode(images[0]["data"])[:2] == b"\xff\xd8"
        saved = {p.name for p in out.iterdir()}
        assert {"angebot.txt", "zeichnung.dxf", "bild_vb22.png"} <= saved
        assert (out / "bild_vb22.png").read_bytes() == (env.out / "res2.png").read_bytes()

        other = connect.create_key("fremd")[1]
        s2 = Session(url, other)
        try:
            s2.send("initialize", {"protocolVersion": "2025-06-18"})
            r2, t2 = s2.tool("get_order", order=pid)
            assert r2["isError"] is True and "404" in t2[0]["text"]          # another key cannot read this order
        finally:
            s2.close()
    finally:
        err = s.close()
    assert "ready" in err and s.p.returncode == 0
