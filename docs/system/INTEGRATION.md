# Integration of the Werkstatt system modules (2026-10-18)

The modules voice, place, intent, ground, profile, channels and connect are now wired into the main app (port 7860).
Nothing is public: no new port, no new interface, no tunnel. Channels and webhooks stay off until the owner enters
credentials and switches them on. `agent.py` (Phase 3) does not exist yet, so the agent-dependent parts are wired but
idle. See the end of this file.

## What changed in shared files

### server.py
- Imports: `channels`, `connect`, `ground`, `intent`, `profile`, `voice`.
- `ground.OUT = OUT`, next to the other output-path wiring.
- Router loop: `voice.router, intent.router, ground.router, profile.router, channels.router, connect.router, connect.v1`
  added after `public.router`.
- `start_front_doors()` runs in the lifespan after the backup thread. It imports `agent` lazily. If the import fails,
  it logs `channels: agent.py missing, not started` and returns. Otherwise it subscribes `connect.on_agent_event`
  (only when `agent.subscribe` exists) and calls `channels.start_all(agent, out_dir=OUT)`, which starts only channels
  that are enabled. Any exception is printed and never blocks startup.
- Shutdown, after `yield`: `channels.stop_all`, then `connect.stop`, then `sched.drain` as before.
- `/api/status`: `"voice": voice.loaded()` added to `loaded`. `/api/unload/voice` calls `voice.unload()`.
- New page route `GET /aufnahme` serves `static/aufnahme.html`. It also stays reachable as `/static/aufnahme.html`.

### team.py
- `OPEN` gained `"/api/v1/"`, with a comment. Only the cookie/password check is skipped. `connect.v1` requires a
  Bearer API key on every route through a router-level dependency. The Host allowlist (400), the body-size limit (413)
  and the same-origin check on writes (403) still run first for `/api/v1/`. All other new routes (`/api/voice/*`
  including `/api/voice/sample`, `/api/profile*`, `/api/channels*`, `/api/keys`, `/api/webhooks*`,
  `/api/workshop/{pid}/understand|locate|export/{name}`) stay behind team auth.

### projects.py
- `patch_project` loads the doc before `apply_ops` and then calls `profile.learn_doc(before, doc, who, ops)` through a
  lazy import (profile → workshop → projects is circular), inside `try/except` with `traceback.print_exc()`. Learning
  can never break a save. Also added `import traceback`.

### Tests
- `tests/test_connect.py::test_team_password_lets_v1_through_with_the_integration_hook`: it used to assert the
  pre-integration state ("not hooked yet" → 401) and monkeypatch OPEN. It now asserts that `"/api/v1/"` is in
  `team.OPEN` and checks the same behaviour without the monkeypatch.
- New `tests/test_integration.py`. It imports the real `server.py` in a subprocess with `--fake-gpu --public-port 0`
  and `FLUX_DATA` set to a tmp dir, so there is no GPU, no real store and no network, and runs the lifespan. It
  checks:
  - all new routes are mounted;
  - `voice` shows up in `/api/status` and can be unloaded;
  - `/aufnahme` is served;
  - both channels are disabled and webhooks are off;
  - with a team password set: a key passes to `/api/v1`, no key gives 401, a foreign Origin 403, a foreign Host 400
    and an oversized body 413;
  - every new team route gives 401 even with an API key;
  - the lifespan logs `channels: agent.py missing`;
  - reading the channel config writes no `channels.json`.

Nothing else was touched: no module code besides the test fix, no static/ws files, no backup.py, no public.py.
The optional consumer hooks (profile wording in `workshop.build_room_job` / `judge.request_text`, `profile.voice_words`
in the voice prompt, `intent.EXCLUSIVE = sched.exclusive`) were deliberately **not** applied. They change generation
prompts or serialisation and belong in a separate, evaluated step.

## Test results

- `.venv/bin/python -m pytest -q`: **645 passed, 1 skipped, 0 failed** (88 s). Before integration: 644 passed,
  1 skipped. The difference is the new integration test. The one failure after wiring was the connect test above,
  which asserted the pre-integration state, and it is fixed.
- `node --test tests/*.test.mjs`: **85 passed, 0 failed** (w3d, ws_editor, ws_orders, ws_results, ws_store).
  `tests/place_xcheck.mjs` is not a standalone test. It is the cross-check harness that `tests/test_place.py` runs
  with a cases file. Run on its own without an argument, it exits with ERR_INVALID_ARG_TYPE, which is expected.

## Live check (flux-studio restarted 22:37)

`systemctl --user restart flux-studio`, then `/api/status` answered within about 10 s. The journal showed
`migration: []`, `recovered jobs: []` and `channels: agent.py missing, not started`, with no tracebacks. The smoke
script (`/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/smoke.py`) ran twice against 127.0.0.1:7860. Each run used a throwaway order "TEST Integration"
with its own uploaded copy of `evalset/rooms/20261018-193206-1992693316.png` and that room's stored geometry, and
purged the order at the end. Result: **27/27 checks OK** in both runs:

| Route | Result |
|---|---|
| GET /aufnahme, /api/voice/status, /api/voice/samples | 200 |
| POST /api/voice/transcribe (29 s English wav, cold, while FLUX distilled was still loading) | 200 in 46.4 s, correct text |
| POST /api/voice/sample (consent) → DELETE /api/voice/sample/{id} | 200 / 200 (sample removed again) |
| POST /api/unload/voice | 200, `voice: false` |
| POST /api/workshop/{pid}/understand (preview / apply) | 200 in 8.7 s / 8.9 s: "Einbauschrank 220 × raumhoch × 60 cm, Lack weiß matt, grifflos (Push), 3 Türen (links neben dem Fenster)" → piece schrank 220×297×60 |
| POST /api/workshop/{pid}/locate place "links neben dem Fenster" | 200 in 5.8 s, point [100, 350], wall w1 |
| POST /api/workshop/{pid}/locate object "das Fenster" | 200 in 13.6 s (including the first SAM load), 1 part |
| PATCH correction d=62 | 200. The profile learner counted `schrank|d|62` (learn hook works) |
| GET /api/profile, /packs, /suggestions | 200 |
| GET /api/channels | 200, telegram false, mail false |
| POST /api/channels/telegram/check | 200, offline only (`online: null`), no network |
| GET /api/webhooks | 200, enabled false |
| POST /api/keys → GET /api/v1/orders/{pid} with key / without | 404 "Auftrag nicht gefunden." (the key is accepted; the order was not created through v1) / 401 |
| GET /api/workshop/{pid}/export/angebot.txt, zeichnung.dxf | 200 (offer text, 11 263 B DXF) |
| DELETE /api/keys/{id}, POST purge | 200 / 200 (2 files) |

Clean-up afterwards: the md5 sums of all `projects/workshop/*.json` files and `settings.json` were unchanged. The
TEST-learned `projects/profile.json`, the then-empty `projects/apikeys.json` and the empty `evalset/audio/` were
removed. None of them existed before. No new files remain in `outputs/`. Whisper is unloaded. SAM stayed loaded in
the server after the object query (segment.py's normal behaviour).

## Known limits / open points

- **agent.py does not exist.** As a result:
  - `/api/v1/orders` (create/message) answers 503 "Assistent …";
  - channels cannot start even when enabled (the lifespan logs that);
  - webhooks get no agent events;
  - `tools/mcp_server.py` only works against a real agent once one exists (its end-to-end test uses a fake agent).

  When agent.py lands, restart the service. `start_front_doors()` picks it up without code changes.
- Channels, webhooks and MCP were never run against real services (hard rule 2). The first online check belongs to
  the owner.
- `profile.py` shadows the stdlib `profile` module. A PEP 562 shim forwards to the real module, which keeps cProfile
  and torch._dynamo working. Renaming it (e.g. `company_profile.py`) would be cleaner. That needs a SPEC decision and
  changes in server.py, projects.py and the tests.
- The PATCH learn hook writes `projects/profile.json` on the first real manual correction of a piece field (type,
  dims, material, handles…). That is intended. Suggestions appear only after 3 identical corrections, and nothing
  changes defaults without "accept".
- `apikeys.json` and `webhooks.json` (and `channels.json` with secrets, once configured) sit in `projects/` and are
  therefore in the daily backup tar.
- Frontend parts (mic button / "Verstanden:" card, Assistent panel, settings sections Firmenprofil / Kanäle /
  API & Webhooks) are not built. The routes exist.
- The cold Whisper load is slow on the shared GPU: 46 s in the live run, while FLUX was loading at the same time.
- German speech accuracy and real-photo grounding are still unmeasured (FLUX rooms and English audio only). See
  voice.md and ground.md.

## Rollback

The pre-integration copies of `server.py`, `team.py` and `projects.py` are in `/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/pre/` (session scratchpad, temporary). The
code backup also has them. Restore the three files, delete `tests/test_integration.py`, revert the 6-line change in
`tests/test_connect.py`, then `systemctl --user restart flux-studio`.

## Update 2026-10-19: agent.py integrated

`agent.py` exists now and is wired into `server.py`: import, `agent.router` in the router loop, `agent.recover()` in
`start_front_doors()` before the subscription, and `agent.stop` on shutdown. As a result:
- `/api/v1/orders` create and message reach the real assistant;
- `connect.on_agent_event` receives agent events (webhooks are still off);
- `channels.start_all` gets the real agent (all channels are still off).

flux-studio was restarted; the log shows `agent: {...}` and `channels: none enabled`. Details, tests and the live
end-to-end runs are in `agent.md`.
