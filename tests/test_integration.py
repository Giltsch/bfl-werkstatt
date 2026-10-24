"""The integrated server: every new router is mounted, /api/v1 is reachable by API key alone, the new team routes stay
behind the login, and the lifespan starts no channel. server.py builds its scheduler and engine at import time, so it
runs in a subprocess with --fake-gpu, no customer app and FLUX_DATA in a tmp dir (no real store, no GPU, no network)."""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

SCRIPT = r'''
import json, sys
sys.argv = ["server.py", "--fake-gpu", "--public-port", "0", "--preload", ""]
import server, team, projects, connect
from fastapi.testclient import TestClient

out = {}
paths = set(server.app.openapi()["paths"])   # app.routes nests included routers in this FastAPI version
out["routes"] = sorted(p for p in paths if p.startswith(("/api/voice", "/api/profile", "/api/channels", "/api/keys",
                                                         "/api/webhooks", "/api/v1", "/aufnahme"))
                       or p.endswith(("/understand", "/locate", "/export/{name}")))
with TestClient(server.app) as c:
    s = c.get("/api/status").json()
    out["voice_loaded"] = s["loaded"].get("voice")
    out["unload_voice"] = c.post("/api/unload/voice").status_code
    out["aufnahme"] = c.get("/aufnahme").status_code
    ch = c.get("/api/channels").json()["channels"]
    out["channels_enabled"] = {k: v.get("enabled") for k, v in ch.items() if isinstance(v, dict) and "enabled" in v}
    out["webhooks_enabled"] = c.get("/api/webhooks").json().get("enabled")
    out["profile"] = c.get("/api/profile").status_code
    out["key_without_password"] = c.post("/api/keys", json={"name": "TEST integration"}).status_code
    out["webhooks_on_without_password"] = c.put("/api/webhooks", json={"enabled": True}).status_code
    projects.update_settings(lambda s: s["access"].update(password=team.hash_password("geheim123")))
    key = connect.create_key("TEST integration")[1]
    auth = {"Authorization": f"Bearer {key}"}
    out["v1_with_key"] = c.get("/api/v1/orders/abcdef123456", headers=auth).status_code
    out["v1_without_key"] = c.get("/api/v1/orders/abcdef123456").status_code
    out["v1_foreign_origin"] = c.post("/api/v1/orders", headers={**auth, "Origin": "http://evil.example"}).status_code
    out["v1_bad_host"] = c.get("/api/v1/orders/abcdef123456", headers={**auth, "Host": "evil.example.com"}).status_code
    out["v1_big"] = c.post("/api/v1/orders", headers={**auth, "Content-Length": str(team.MAX_BODY + 1)},
                           content=b"").status_code
    out["team_routes_locked"] = {p: c.get(p, headers=auth).status_code
                                 for p in ("/api/voice/samples", "/api/voice/status", "/api/profile", "/api/channels",
                                           "/api/keys", "/api/webhooks")}
    out["sample_locked"] = c.post("/api/voice/sample", headers=auth).status_code
print("RESULT" + json.dumps(out))
'''


def test_server_integration(tmp_path):
    env = {**os.environ, "FLUX_DATA": str(tmp_path), "FLUX_FAKE_STEP_S": "0.01"}
    r = subprocess.run([sys.executable, "-c", SCRIPT], cwd=HERE, env=env, capture_output=True, text=True, timeout=240)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout[-3000:] + r.stderr[-3000:]
    out = json.loads(line[len("RESULT"):])
    for p in ("/api/voice/transcribe", "/api/voice/sample", "/api/workshop/{pid}/understand",
              "/api/workshop/{pid}/locate", "/api/profile", "/api/profile/suggestions/{sid}/accept", "/api/channels",
              "/api/channels/{cid}/check", "/api/keys", "/api/webhooks", "/api/workshop/{pid}/export/{name}",
              "/api/v1/orders", "/api/v1/orders/{pid}/files/{name}", "/aufnahme"):
        assert p in out["routes"], p
    assert out["voice_loaded"] is False and out["unload_voice"] == 200 and out["aufnahme"] == 200
    assert out["channels_enabled"] and not any(out["channels_enabled"].values())   # off by default
    assert out["webhooks_enabled"] is False and out["profile"] == 200
    assert out["key_without_password"] == 409 and out["webhooks_on_without_password"] == 409   # LAN guests cannot
    assert out["v1_with_key"] in (404, 503)   # past the team password; 503 while agent.py does not exist
    assert out["v1_without_key"] == 401
    assert out["v1_foreign_origin"] == 403 and out["v1_bad_host"] == 400 and out["v1_big"] == 413
    assert set(out["team_routes_locked"].values()) == {401}, out["team_routes_locked"]
    assert out["sample_locked"] == 401
    assert "channels: agent.py missing" in r.stdout or "channels: none enabled" in r.stdout
    assert not (tmp_path / "projects" / "channels.json").exists()   # reading the config writes nothing
