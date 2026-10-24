# Werkstatt as a system: photo + voice in, results out

Status: build spec, 2026-10-18. Everything stays private: nothing is exposed to the internet.

## Goal

A carpenter takes a photo of the customer's room and *talks*: "links neben dem Fenster ein Einbauschrank, zwei-zwanzig breit,
raumhoch, Weißlack, drei Türen grifflos, der alte Schrank kommt weg". The system understands, asks back only for what is
missing, places the pieces in the photo, shows a quick preview, generates, judges, and returns the results (images,
dimensions, offer text, 3D/DXF files). The web app stays the control room; channels (Telegram, e-mail, API/MCP) are
alternative front doors to the same agent.

## Hard rules (every agent)

1. **Nothing public.** No tunnels, no new listening ports, no binding to new interfaces, no DNS/router changes. All new
   HTTP routes live in the existing main app (port 7860, behind the team middleware). The customer app (7861) is not touched.
2. **Channels and webhooks are OFF by default.** Telegram/e-mail/webhook code is built and unit-tested with fake
   transports; it only ever talks to the internet when the owner has entered credentials *and* switched it on. Tests and
   live checks must never contact api.telegram.org, mail servers or webhook URLs.
3. **Real data is sacred.** Never modify `projects/workshop/0db93df20e67.json`, `bb505aeddc04.json`, `7e57e55a1111.json`,
   book projects, `projects/settings.json`, or files in `outputs/` that you did not create. Test orders are titled `TEST …`,
   use their own uploaded photo copies (POST /api/upload), and are purged afterwards. Never reuse a real order's upload.
   Evaluation photos are FLUX-generated rooms (copies under `evalset/rooms/`), never customer photos.
4. **Do not touch** `~/Dokumente/Projekte/KILaw` or any lawai service. Keep ≥ 15 GB RAM available (`judge.mem_available_gb()`).
5. **Do not restart `flux-studio`** in the build phase (other agents are testing against it). Test new routers with
   `fastapi.testclient.TestClient` on a small FastAPI app that includes only your router; call models directly.
6. **Own your files.** Create the files listed for your module; do not edit existing shared files (`server.py`, `team.py`,
   `projects.py`, `workshop.py`, `static/ws/*.js` …). Write what must be hooked into shared files into
   `docs/system/<module>.md` → "Integration" (exact code). The integrator applies it.
7. **Match the codebase:** FastAPI `APIRouter`, `HTTPException` with German user-facing messages, English code comments
   (sparse, explaining *why*), no new pip dependencies unless unavoidable (ffmpeg, httpx, numpy, PIL, torch, transformers,
   cv2 are available; `openai`, `faster_whisper`, `soundfile`, `mcp`, `ezdxf` are NOT installed — do not add them).
8. Python: `.venv/bin/python`. Tests: `.venv/bin/python -m pytest -q tests/test_<module>.py`. Use tmp dirs for storage in tests
   (monkeypatch module-level paths like the existing tests do).

## Existing building blocks (read before using)

- **LLM / VLM:** Qwen/Qwen3.8-27B-FP8 on vLLM `http://127.0.0.1:8002` (OpenAI-compatible, vision, JSON-schema output).
  Reuse `judge.ask(system, content, schema, name, timeout, max_tokens, temperature)`, `judge.content_of(parts, text)`,
  `judge.status()`, `judge.wait_memory()`, `judge.loads_lenient()`. The judge shares this server: keep prompts short,
  timeouts sane, one request at a time per task.
- **Orders:** `projects.load("workshop", pid)`, `projects.apply_ops(pid, base_rev, ops, who)` (the validated PATCH path —
  always use it for changes), `projects.mutate(...)` only for bookkeeping. Ops: `add_piece`, `remove_piece`,
  `{"op":"set","path":"pieces.<id>.<field>","value":…}`, `set removes`, `set room/room_size/geometry/calibration`.
  `projects.PIECE_FIELDS`, `projects.PIECE_LABELS`, `projects.MAX_PIECES` (= 4). Piece types = `workshop.CATALOG`.
- **Geometry:** `POST /api/geometry/analyze {room}` (`geometry_api`), geometry dict (intrinsics, floor, frame, walls…).
  Client-side placement math lives in `static/werkstatt3d.js` (W3D: `init`, `place`, `centreAt`, `hang`, `snapToWall`,
  `projectBox`, grid boxes 0–1000 `[top,left,bottom,right]`). `tests/w3d.test.mjs` shows how to run it in node.
- **Segmentation:** `segment.masks_at(img, points)`, `segment.outline(mask)` (SAM 2.1).
- **Generation:** `workshop.generate(pid, req, request)` logic → `workshop.build_room_job` + `workshop._submit`;
  jobs via `jobs` scheduler; results land as order variants with judge verdicts. Plan check: `workshop.check_plan`.
- **Delivery:** `delivery.create_version(pid, variant_ids, note, expires_days, who, lan_base, pin=…)`, PDF/ZIP, PIN links.
- **3D:** `export3d.panels/glb/stl/cut_list`. Mock-up/preview render: `mockup.render`, `workshop.guide_image`.

## Modules and contracts

### voice.py — speech to text (Phase 1)
- `decode(data: bytes) -> np.ndarray` float32 mono 16 kHz via ffmpeg subprocess (webm/opus, ogg/opus, m4a, wav, mp3).
- `transcribe(data: bytes, lang: str = "de", prompt: str | None = None) -> dict`
  `{"status": "ok"|"error"|"unavailable", "text": str, "language": str, "duration": float, "seconds": float}`.
  Model `openai/whisper-large-v3-turbo` (in the HF cache) via transformers, lazy load on CUDA fp16, `loaded()`, `unload()`,
  idle unload after 10 min, memory guard. Domain prompt (carpenter vocabulary) to help with "Weißlack, grifflos, Korpus".
- Router: `POST /api/voice/transcribe` (multipart `audio`, optional `lang`), `GET /api/voice/status`.
- Also: `static/aufnahme.html` — a LAN page for carpenters to record evaluation samples (photo optional + voice), stored
  under `evalset/audio/<id>.{webm,json}` via `POST /api/voice/sample`. Explain on the page that it is only for test data.

### place.py — server-side placement (Phase 2)
- Exact Python port of the W3D math needed to place a piece from an image point: scene from geometry (+ calibration
  scale), `floor_point`, `centre_at`, `wall_hit`, `hang`, `place`, `nearest_wall`, `snap_to_wall`, `project_box` → grid
  box, `relations` (if cheap). Same numbers as JS (tolerance ≤ 1e-6 relative / ≤ 1 grid unit).
- `placement_for(doc, piece, point_grid, against_wall: bool | None = None, yaw=None) -> dict | None` → the exact
  `placement` value the UI would store after a tap (+ snap to the nearest wall when close or when asked; wall-hung types use
  `hang` and return `elev`). Read `static/ws/step-platzieren.js` to mirror what a tap commits.
- Cross-check test runs `static/werkstatt3d.js` in node on the same geometries (real FLUX demo rooms analysed) and compares.

### intent.py — understanding the order (Phase 1)
- `normalize_de(text) -> str`: deterministic German normalisation of dimensions ("zwei zwanzig" / "zwo-zwanzig" →
  "220 cm", "ein Meter achtzig" → "180 cm", "sechzig tief" → "60 cm tief", "raumhoch" kept), unit tests with many variants.
- `understand(text, doc, profile=None, photo=None) -> dict`:
  ```
  {"status": "ok"|"unavailable"|"error",
   "summary_de": "Einbauschrank 220 × raumhoch × 60, Weißlack, 3 Türen grifflos; alter Schrank wird entfernt",
   "pieces": [{"ref": <existing piece id or null>, "type": <catalog type>, "w","h","d": int|null, "elev": int|null,
               "material": <material id or null>, "material_custom": str, "handles": <id or null>, "text": str,
               "where": "links neben dem Fenster" (location phrase, verbatim-ish), "full_height": bool}],
   "removes": [{"what": "der alte Schrank"}],
   "questions": [{"piece": <index>, "field": "d", "question_de": "Wie tief soll der Schrank sein?", "default": 60}],
   "ops": [<projects ops without placement, ready for apply_ops>],
   "confidence": 0..1}
  ```
  Uses the profile for defaults (material/handles/depths) and fills sensible defaults instead of asking for everything;
  asks only for essentials that cannot be defaulted (typically width). "raumhoch" → room height from calibration/geometry.
  Corrections ("nein, achtzig tief") update the existing piece (`ref`).
- Router: `POST /api/workshop/{pid}/understand {text, apply?: bool, base_rev?}` → result (+ new doc when applied).
- Evaluation: `evalset/cases.json` (≥ 40 German descriptions incl. dialect/colloquial numbers, corrections, multi-piece,
  removals; expected structured output) and `tools/eval_understand.py` (scores fields; prints a table; runs live on Qwen).

### ground.py — finding places and objects in the photo (Phase 2)
- `locate(photo, phrase, geo=None, doc=None) -> {"status", "point": [x, y] (0–1000 grid), "wall": id|None, "confidence", "why"}`
  for location phrases ("links neben dem Fenster", "in der Ecke rechts", "wo die alte Kommode steht", "Mitte des Raums").
  Use the VLM with a coordinate grid overlay and/or Qwen's native 0–1000 pointing; validate the point (on floor or wall as
  appropriate, inside the image), combine with geometry where helpful.
- `find_object(photo, phrase) -> {"status", "point", "parts": [{"poly", "box"}] }` — VLM points at the object, SAM cuts it out
  (pick the candidate mask that matches the VLM box best).
- Router: `POST /api/workshop/{pid}/locate {phrase, kind: "place"|"object"}`.
- Live accuracy check on FLUX rooms with hand-written expectations (`evalset/ground.json`), reported as hit rate.

### profile.py — adaptable company profile (Phase 5)
- Storage `projects/profile.json` (mode 600). `get()`, `put(patch)`, `defaults_for(type) -> {d, h, elev, material, handles,
  plinth…}`, vocabulary aliases (e.g. "Weißlack" → material "weiss", "Push-to-open" → handles "push"), trade packs in
  `packs/*.json` (schreiner, kueche, treppe, terrassendach, schiebetuer: types, default dims, wording, judge hints),
  `learn(before_piece, after_piece, who)` records corrections; after 3 identical corrections → suggestion
  (`suggestions()`), accepted suggestions become defaults (`accept(id)`).
- Router: `GET/PUT /api/profile`, `GET /api/profile/packs`, `GET /api/profile/suggestions`, `POST /api/profile/suggestions/{id}/accept`.

### agent.py — the conversation (Phase 3)
- One conversation per order: `projects/agent/<pid>.json` (events list, state). States: `need_photo → analyzing →
  understanding → asking → preview → generating → done`, plus `error`. Events: `{"n", "at", "role": "user"|"agent",
  "kind": "text"|"audio"|"photo"|"question"|"preview"|"result"|"status"|"error", "text", "image", "data"}`.
- `start(photo: bytes|None, who, channel, text=None, audio=None) -> pid` (creates a `… (Assistent)` order, uploads the
  photo copy, analyses the room), `handle(pid, who, channel, text=None, audio=None, photo=None) -> list[event]`
  (non-blocking: long steps run in a worker thread and append events), `events(pid, after=0)`, `subscribe(cb)` for channels.
- Flow: transcribe → understand → apply ops → locate/find objects → place → preview image (photo with outlines and labels;
  reuse W3D-equivalent projection) → ask "So? (ja / ändern …)" → on "ja": plan check, generate (quality from profile),
  wait for job + judge, auto-retry once when the judge fails → result event (best images, scores, offer text, file links).
  Free text at any point = correction ("breiter", "achtzig tief", "nimm Eiche", "nochmal", "senden").
- Router: `POST /api/agent/start` (multipart photo, text|audio), `POST /api/agent/{pid}/message` (multipart text|audio|photo),
  `GET /api/agent/{pid}/events?after=n`, `GET /api/agent/{pid}/preview.png`.

### channels/ — front doors (Phase 4)
- `channels/base.py` (interface), `channels/telegram.py` (Bot API long polling via httpx — outbound only; allowlist of
  chat ids; chat ↔ order mapping; photo + voice in → agent; agent events out as messages/photos/documents),
  `channels/mail.py` (IMAP poll + SMTP send; photo attachments + text/voice memo in, results out).
- Config `projects/channels.json` (mode 600; secrets write-only in the API). Router `GET/PUT /api/channels`,
  `POST /api/channels/{id}/check` (validates config offline; only contacts the service when enabled).
  `start_all(agent)` / `stop_all()` for the server lifespan; nothing starts unless enabled.

### connect.py — connectivity (Phase 6)
- API keys `projects/apikeys.json` (hashed, name, scopes, created, last_used); team routes `GET/POST/DELETE /api/keys`
  (the plain key is shown once).
- `/api/v1/*` with `Authorization: Bearer <key>`: `POST /orders` (photo + text|audio → agent.start), `GET /orders/{pid}`
  (state, questions, results), `POST /orders/{pid}/messages`, `GET /orders/{pid}/files/{name}`. The team middleware must
  let `/api/v1/` through without cookie (integration note) while the router enforces the key.
- Webhooks `projects/webhooks.json`: events `order.question`, `order.preview`, `order.result`; HMAC-SHA256 signature header,
  retry with backoff, disabled by default.
- Exports: `export3d`-based DXF (front + top view, R12 ASCII, hand-written writer) and `offer_text(doc, variant_id)`
  (Positionstext). `tools/mcp_server.py`: MCP over stdio (hand-rolled JSON-RPC 2.0: initialize, tools/list, tools/call)
  exposing create_order / send_message / get_order / get_results via the local /api/v1 with a key from the environment.

### Frontend (after the backend)
- Mic button + "Verstanden:" card in the Möbel step (speak → transcript → understood pieces → apply / correct by voice).
- "Assistent" panel per order (chat: photo, voice, text; preview; results) and a "Neuer Auftrag per Foto + Sprache" entry.
- Settings sections: Firmenprofil (profile, packs, suggestions), Kanäle (Telegram, E-Mail — off by default, clear warning
  that switching on connects to the internet), API & Webhooks (keys, webhook URLs).
