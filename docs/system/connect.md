# connect — API keys, /api/v1, webhooks, MCP, DXF and offer text (Phase 6)

Status 2026-10-18: built and unit-tested. **Not mounted in the running server** (no flux-studio restart in the
build phase); everything below the "Integration" heading still has to be applied by the integrator. Nothing here
opens a port or contacts the internet: /api/v1 lives in the existing app on 7860; webhooks are off by default.

## What was built

| File | What it does |
|---|---|
| `connect.py` | API keys (`projects/apikeys.json`), the `/api/v1` router (key auth, per-key rate limits, order ownership in `projects/v1_orders.json`), files of an order (images, offer text, DXF, GLB, STL, cut list, event images), webhooks (`projects/webhooks.json`, HMAC-signed, background delivery with retries), team routes for keys and webhooks, `on_agent_event` for `agent.subscribe` |
| `offer.py` | `offer_text(doc, variant_id=None)`: German Positionstext per piece (type, B × H × T, material, handles, construction counted from `export3d.panels`, wall mounting height, notes, location) + removal and delivery/assembly positions. No prices. |
| `dxf.py` | Hand-written DXF R12 (AC1009, ASCII) writer: front view + top view (first-angle, Draufsicht below) of `export3d.panels`, mm 1:1, overall dimensions (width, height, depth, mounting height + floor line „OKFF“ for wall-hung pieces), several pieces side by side, layers, hidden panels dashed on `VERDECKT`. Only `LINE` and `TEXT` entities. |
| `tools/mcp_server.py` | MCP server over stdio (hand-rolled JSON-RPC 2.0: `initialize`, `ping`, `tools/list`, `tools/call`, notifications ignored, batches answered) with the tools `create_order`, `send_message`, `get_order`, `get_results`; talks to `http://127.0.0.1:7860/api/v1` with `WERKSTATT_API_KEY`. |
| `tests/test_connect.py` | 28 tests: keys, auth, isolation, files, rate limits, webhooks, offer text, DXF |
| `tests/test_mcp.py` | 3 tests: protocol, tool errors, the real script as a subprocess against a test-only server |

`export3d.py` was not edited (only imported).

## How to use it

### API keys (team routes, behind the team middleware)

```
GET    /api/keys                 → {"keys": [{id, name, scopes, created, created_by, last_used, hint}], "scopes": ["orders"]}
POST   /api/keys {name, scopes?} → {..., "key": "wk_…"}   the plain key, shown ONLY in this response
DELETE /api/keys/{id}
GET    /api/workshop/{pid}/export/{name}   the files below for any order, for the web app (team only, no key)
```

Keys are `"wk_" + secrets.token_urlsafe(32)` (the prefix makes leaked keys recognisable to secret scanners). Stored:
sha256 hex, name, scopes, created, created_by, last_used (written at most once a minute), a hint (`wk_AbCd…xyz1`).
`check_key` compares the hash against every stored key with `hmac.compare_digest` (no early exit). File mode 600.

### /api/v1 (Authorization: Bearer wk_…)

```
POST /api/v1/orders                      multipart: photo (image), text and/or audio      → 201 order view
GET  /api/v1/orders/{pid}?after=n        → order view
POST /api/v1/orders/{pid}/messages       multipart: text | audio | photo                  → order view + "new"
GET  /api/v1/orders/{pid}/files          → {"files": [{name, url, type}]}
GET  /api/v1/orders/{pid}/files/{name}   → the file (attachment)
```

Order view:
```json
{"order": "ab12…", "title": "… (Assistent)", "state": "asking",
 "questions": [{"n": 3, "text": "Wie tief soll der Schrank sein?", "data": {...}}],
 "events": [{"n", "at", "role", "kind", "text", "data", "image": "/api/v1/orders/<pid>/files/ereignis_2.png"}],
 "next": 3,
 "results": [{"variant", "image": ".../bild_<vid>.png", "offer": ".../angebot_<vid>.txt", "score", "pass", "comment", "favourite"}],
 "files": [{"name": "zeichnung.dxf", "url": "...", "type": "image/vnd.dxf"}, ...]}
```

File names: `bild_<vid>.png`, `angebot.txt` (current plan), `angebot_<vid>.txt` (as the variant shows it),
`zeichnung.dxf` (all pieces), `zeichnung_<piece>.dxf`, `modell_<piece>.glb|stl`, `zuschnitt_<piece>.csv`,
`ereignis_<n>.png|jpg|webp` (an agent event's image, only when it is a safe relative path inside `outputs/`).

Rules:
- Every `/api/v1` route depends on `api_key` (router-level dependency; FastAPI caches it, so it runs once per request).
- Multipart bodies are parsed **after** the key check (no upload parsing for unauthenticated requests).
- A key sees only orders it created (`projects/v1_orders.json`: `{"orders": {pid: {"key": kid, "created"}}}`).
  Any other id — another key's order, a web-app order, a purged order, garbage — is the same `404 Auftrag nicht gefunden.`
  A deleted key's orders are unreachable.
- Rate limits per key (sliding window): read 120/min, write 30/min, order creation 20/hour → `429` + `Retry-After`.
  20 wrong keys per IP and minute → `429` for that IP.
- Limits: text 4000 chars, photo 30 MB (and ≤ `imaging.MAX_UPLOAD`), audio 25 MB; the photo must open with PIL.
- The agent is imported lazily (`importlib.import_module("agent")`); until `agent.py` exists the API answers
  `503 Der Assistent ist noch nicht verfügbar.` Calls used: `agent.start(photo, who, "api", text=, audio=) -> pid`,
  `agent.handle(pid, who, "api", text=, audio=, photo=) -> [events]`, `agent.events(pid, 0)` (a list, or
  `{"events", "state"}`); state falls back to `agent.state(pid)` and then to `projects/agent/<pid>.json["state"]`.
  `who` is `"API: <key name>"`.

curl example (once integrated):
```bash
curl -H "Authorization: Bearer $KEY" -F photo=@raum.jpg -F text="links ein Einbauschrank, 220 breit, raumhoch, Weißlack" \
     http://127.0.0.1:7860/api/v1/orders
```

### Webhooks (team routes)

```
GET    /api/webhooks                       → {enabled, events, hooks: [{id, url, events, enabled, key, created, last, secret_hint}], recent}
PUT    /api/webhooks {enabled: bool}       master switch (default false)
POST   /api/webhooks {url, events?, key?, enabled?}  → hook + "secret" (shown once); hooks are created disabled
PATCH  /api/webhooks/{id} {url?, events?, key?, enabled?}
DELETE /api/webhooks/{id}
POST   /api/webhooks/{id}/test             queues a "ping" — 409 unless master switch AND hook are on
```

Events `order.question`, `order.preview`, `order.result` (from agent events of kind question / preview / result).
A hook with `key` only receives events of orders created by that API key. Body:
`{"event", "order", "at", "data": {n, at, role, kind, text, data, image, results?, files?}}`.
Headers: `X-Werkstatt-Event`, `X-Werkstatt-Delivery` (same id across retries), `X-Werkstatt-Timestamp` (unix s),
`X-Werkstatt-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + body)>`. Receivers verify like
`connect.verify(secret, ts, body, header)` (constant-time compare, 15 min tolerance).
Delivery: one daemon thread with a time-ordered queue; 2xx = ok; 5xx/408/429/network errors are retried after
5 s, 30 s, 2 min, 10 min (5 attempts); other 4xx are not retried; switching the master or the hook off stops pending
retries; redirects are not followed; timeout 10 s. Last result per hook is stored in `webhooks.json`, the last 50 in memory.
Nothing is ever sent while `enabled` is false (the default) — tests use `httpx.MockTransport` via `connect.TRANSPORT`.

### MCP server

```bash
WERKSTATT_API_KEY=wk_… /home/peter_behninger/Dokumente/Projekte/bfl/.venv/bin/python \
    /home/peter_behninger/Dokumente/Projekte/bfl/tools/mcp_server.py
# Claude Code:
claude mcp add werkstatt -e WERKSTATT_API_KEY=wk_… -- /home/peter_behninger/Dokumente/Projekte/bfl/.venv/bin/python \
    /home/peter_behninger/Dokumente/Projekte/bfl/tools/mcp_server.py
```
`WERKSTATT_API_URL` overrides the base (default `http://127.0.0.1:7860/api/v1`). Tools:
- `create_order(photo_path, text, audio_path?)` — uploads the local photo, returns a summary + the order view JSON.
- `send_message(order, text, photo_path?)`
- `get_order(order, after?)` — state, open questions, events.
- `get_results(order, include_images?, download_dir?)` — scores, the offer text, optional JPEG thumbnails (≤ 1024 px, best 3)
  and optional download of every file.
Tool failures (missing key, HTTP errors, bad ids, missing files) come back as `isError: true` content; unknown
tools/methods as JSON-RPC errors. Protocol versions 2025-06-18 / 2025-03-26 / 2024-11-05. Logs on stderr only.

### offer_text / DXF in Python

```python
import offer, dxf
offer.offer_text(doc)            # current plan
offer.offer_text(doc, "va11")    # as variant va11 shows it (its snapshot dims/labels); KeyError for unknown variants
dxf.piece_dxf(piece, (w, h, d), title="…", lines=[...]) -> bytes
dxf.drawing([(piece, dims, caption), ...], title, lines) -> bytes
```

## Test / eval results (real numbers)

- `.venv/bin/python -m pytest -q tests/test_connect.py tests/test_mcp.py` → **31 passed** (2.7 s).
- Whole suite `.venv/bin/python -m pytest -q tests/` after all changes → **472 passed, 1 skipped** (101 s; the count
  includes tests other modules added in the meantime).
- MCP end-to-end: the real `tools/mcp_server.py` as a subprocess, `connect.v1` served by uvicorn on 127.0.0.1 with an
  ephemeral port inside the test only, fake agent: initialize → tools/list → ping → create_order (photo bytes arrive
  unchanged at `agent.start`) → get_order → send_message → get_results (offer text, 2 JPEG thumbnails, files downloaded,
  image bytes identical) → a second key gets 404 for that order. Passed.
- Isolation test: for key B, a web-app order, an unknown id and `../../etc`, all of GET order / files list / image /
  DXF / POST message answer 404, and the agent is never called.
- Rate-limit test confirms one count per request (codes 404, 404, 404, 429 with limit 3).
- Real data, read-only (`projects.load`, nothing written): offer text + DXF for the three real orders
  0db93df20e67 (Küche 240 × 250 × 60), bb505aeddc04 (Esstisch + Regalwand), 7e57e55a1111 (Esstisch):
  offer_text 0.1–0.4 ms, DXF 1.1–1.6 ms, 6.6–16 kB.
- DXF validation (no ezdxf available): structural parser in the tests (sections, group codes, layers referenced exist,
  LINE code order, extents) **and** LibreOffice 7 Draw import (`soffice --headless --convert-to pdf`): geometry, dashed
  hidden lines, rotated dimension text and umlauts (`Hängeschrank – Vorderansicht`, `Maße`, `×`) render correctly.
  First version wrote cp1252 bytes with `$DWGCODEPAGE ANSI_1252`; LibreOffice ignored the code page and showed DOS
  characters, so text is now pure ASCII with `\U+XXXX` escapes (verified in LibreOffice).

## Known limits

- Not live-tested against the running server or a real `agent.py` (neither mounted nor existing yet). The agent
  contract used is the SPEC's; if `agent.events` returns another shape, `_conversation` is the one place to adapt.
- DXF: hidden lines are approximated (a panel is dashed when a 6 × 6 sample grid of it is covered by nearer panels;
  partly hidden panels are drawn solid). No DIMENSION entities (dimension lines are LINE + TEXT, oblique ticks), no
  side view, no hatching, no per-front dimensions. Not opened in AutoCAD — LibreOffice only.
- The offer text has no prices and no quantities beyond „1 Stück“; door/drawer and shelf counts are those of the
  `mockup.parts` model (e.g. the kitchen shows its standard module count), not a real construction.
- Rate-limit and auth-failure counters are in memory (reset on restart; fine for one process).
- `v1_orders.json` entries of purged orders stay (they only resolve to 404). Webhook delivery is best effort (the
  queue is in memory; `connect.stop()` drops pending retries).
- `webhooks.json` (secrets) and `apikeys.json` (hashes) are inside `projects/` and therefore in the daily backup tar
  (backup.py only skips `.secret`) — same as `channels.json` will be. Decide whether that is wanted.
- Webhook URLs are not restricted to public hosts (a LAN n8n is a valid target); only the team can configure them.

## Security fixes after review (2026-10-19)

- **Webhook targets are checked (SSRF).** Only public internet addresses are allowed. The check runs when a hook is
  saved and again before every delivery. The request goes to the IP that was checked, with the `Host` header and TLS SNI
  set to the original name, so a DNS answer that changes in between (rebinding) cannot reach an internal address.
  Refused: loopback, link-local/metadata (169.254.x), unspecified, multicast, reserved, CGNAT, `localhost`, and every
  address of this machine itself (psutil interface list, which includes its public IPv6).
  A refused target is never contacted. `last.status` stays `null` and the error reads „Adresse im internen Netz – nicht
  erlaubt“, so no status code leaks. A name that does not resolve is accepted when saved and retried at delivery.
- **LAN receivers are an opt-in.** A hook with `"lan": true` (settings: „Adresse im Firmennetz“, e.g. n8n on another
  computer) may also use private RFC1918/ULA addresses and `.lan`/`.local` names. This machine, loopback and
  link-local stay refused even then. Setting `lan` needs the team password.
- **Team password required.** `POST /api/keys`, switching webhooks on (master switch, `enabled: true` on create or
  PATCH), `lan: true` and the test ping return 409 with a German hint while `settings.access.password` is empty.
  `emit()`/`_send()` also send nothing without a password, so removing the password stops webhooks at once.
  `GET /api/keys` and `GET /api/webhooks` return `password_set`, and the settings page shows the hint with a link to
  „Zugang“.
- The offer text no longer has a „Lage:“ line. `where` is the carpenter's spoken phrase, which is not offer German.

## Integration

Apply these to shared files (exact code). Nothing else is needed.

### 1. `team.py` — let `/api/v1/` through the cookie/password check (the router enforces the key)

```python
# replace
OPEN = ("/k/", "/static/kunde.html", "/static/ws/icon", "/static/ws/manifest.json")
# with
OPEN = ("/k/", "/static/kunde.html", "/static/ws/icon", "/static/ws/manifest.json", "/api/v1/")
```
The Host allowlist, the body-size limit and the same-origin check on writes still apply to /api/v1 (API clients send
no Origin header). `tests/test_connect.py::test_team_password_lets_v1_through_with_the_integration_hook` exercises
exactly this change with a team password set.

### 2. `server.py` — routers, agent events, shutdown

```python
# imports (with the others)
import connect

# router tuple: add connect.router and connect.v1
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, connect.router, connect.v1):
    app.include_router(r)
```

In `lifespan`, after `sched.start()`:
```python
    try:   # webhooks for agent questions / previews / results (sends nothing while webhooks are switched off)
        import agent
        agent.subscribe(connect.on_agent_event)
    except ImportError:
        pass
```
and after `yield`, before/after `sched.drain`:
```python
    connect.stop()
```

`connect.on_agent_event` accepts `cb(pid, event)` and `cb(event)` with `event["pid"]`; it never raises.

### 3. Frontend (Settings → „API & Webhooks“), for the frontend phase

- Keys: list `GET /api/keys`; create `POST /api/keys {name}` and show `key` once with a copy button and the note;
  delete `DELETE /api/keys/{id}`.
- Webhooks: master switch `PUT /api/webhooks {enabled}` with the warning that switching on sends data to the entered
  URLs; per hook URL, events checkboxes, optional key binding, enable toggle, „Testen“ (`POST /api/webhooks/{id}/test`,
  409 while off), last delivery from `last`; show `secret` once after creation.
- Per order (web app): `GET /api/workshop/{pid}/export/{name}` (team route in `connect.router`, same file names as
  /api/v1, e.g. `angebot.txt`, `zeichnung.dxf`, `zuschnitt_<piece>.csv`) — buttons „Angebotstext“ and „DXF-Zeichnung“
  in the results/Möbel step can link to it.
