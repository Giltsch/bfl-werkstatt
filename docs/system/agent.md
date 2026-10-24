# agent.py: the assistant conversation (Phase 3)

Status 2026-10-19: built, unit-tested, **integrated into server.py and live in flux-studio** (restarted for this
step, allowed by the task). Nothing is public: the routes live in the main app on 7860 behind the team middleware,
no new port, no network access beyond the local vLLM, and channels and webhooks remain off.

## What it does

A carpenter sends a room photo and describes, by voice or text, what should go into the room. The assistant then:

1. creates a normal Werkstatt order "… (Assistent)" and stores the photo copy;
2. measures the room (DepthPro, the geometry_api path);
3. transcribes voice (voice.py);
4. understands the description (intent.py);
5. finds the objects to remove (ground.find_object + SAM);
6. places the pieces (ground.locate + place.py);
7. draws a **"So?" preview** and runs the KI-Plancheck;
8. asks only for what is missing;
9. on "ja", generates through the normal job queue, waits for the images and the judge (one corrected retry when
   the judge fails);
10. answers with the best images, scores, the offer text, a DXF drawing, GLB models and a PDF.

Corrections can come at any point, also during a generation.

Every change goes through `projects.apply_ops` (the PATCH path), and generation goes through `workshop.generate`.
The order is therefore a **normal order**: the web app shows and edits everything the assistant did, and an edit in
the web app after a preview is detected (see "Robustness").

## Files

| File | |
|---|---|
| `agent.py` | the conversation: storage, state machine, worker threads, generation watcher, preview drawing, router |
| `tests/test_agent.py` | 30 tests with fake models, a fake scheduler and a fake generation, on tmp stores |
| `server.py` | 4 small changes (see Integration) |

## Conversation model

**Storage.** `projects/agent/<pid>.json` (mode 600, atomic `projects.write_json`). It holds:
- `state` and `stable` (the last non-busy state);
- `events`, append-only, at most 500;
- open `questions`;
- `todo` (pieces still to place);
- `jobs` (the running generation, including the scheduler's retry);
- `preview` and `preview_rev`;
- `focus` (the piece a bare correction means);
- `removed` (the names of the removed objects);
- `spots` (grounding confidence per piece);
- `queued` (text that arrived before the photo);
- `auto_title`.

**States.** `need_photo → analyzing → understanding → asking → preview → generating → done`, plus `error`.
`analyzing` and `understanding` are busy states. If an exception or a restart interrupts one of them, the
conversation goes back to `stable`, so it can never get stuck.

**Events** follow the SPEC: `{"n", "at", "role": "user"|"agent", "kind": "text"|"audio"|"photo"|"question"|"preview"|"result"|"status"|"error", "text", "image"?, "data"?}`.
- `image` is a path relative to `outputs/`, which is what connect.py and channels/ expect.
- Previews are stored as `outputs/exports/workshop-<pid>/assistent/vorschau-<n>.jpg`; the newest 5 are kept.
- The DXF and GLB files of a result are stored in the same folder.
- That folder sits inside the order's export folder, so `delivery.purge` deletes it and the DSGVO export includes it.

`data` per kind:
- `question`: `{field: "w"|"h"|"d"|"where"|"type"|"photo", piece, default}`.
- `preview`: `{rev, pieces: [{n, id, type, label, dims, material, handles, text, where, elev, placed, wall}], removes, plan_check, url}`.
- `result`: `{variants: [{id, image, score, pass, comment, status}], images, offer_text, pdf, files: [dxf], models: [{piece, label, glb, stl}], url}`.
  These are the keys `channels/base.render` and `connect.on_agent_event` already read.

**Threads.**
- **One worker thread per order.** Inputs are queued and handled strictly one after the other (tested: 5 messages,
  never two at once).
- `handle()` only appends the user event and enqueues the message, so it never blocks on a model.
- **One watcher thread** follows running generations every 3 s and hands their end back to the order's worker. While
  the images are being computed, the worker stays free for corrections.

## API

```python
import agent
pid = agent.start(photo_bytes, who, channel, text=None, audio=None, title=None)  # → pid at once; title is an extension
new = agent.handle(pid, who, channel, text=None, audio=None, photo=None)         # → the events appended now
agent.events(pid, after=0)  # → events with n > after ([] for unknown / purged orders, whose conversation is deleted)
agent.state(pid); agent.busy(pid); agent.wait_idle(pid, timeout)
unsub = agent.subscribe(cb)  # cb(pid, event) for every agent event
agent.recover(); agent.stop()  # server lifespan
agent.render_preview(doc) -> PIL.Image
```

- `handle()` works on **any** order. A web-app order gets a conversation on its first message, which is useful for
  the planned "Assistent" panel.
- A purged or unknown order raises `projects.Gone` (404 "Auftrag nicht gefunden.").

### Routes (team-protected, in the main app)

| Route | |
|---|---|
| `POST /api/agent/start` | multipart `photo`, `text`, `audio`, `title` (optional) → `{pid, state, busy, events, next, preview}` |
| `POST /api/agent/{pid}/message` | multipart `text` \| `audio` \| `photo` → the same view plus `new` |
| `GET /api/agent/{pid}/events?after=n` | view with the events after n (poll this; `busy` true while the worker runs) |
| `GET /api/agent/{pid}/preview.png` | the latest preview. It is a JPEG; the SPEC fixed the name. 404 "Noch keine Vorschau." |
| `GET /api/agent` | conversations, newest first `{pid, title, state, updated, channel, last, thumb}` (for the panel) |

Errors return German messages:
- 400 for no input, an unreadable photo, or text over 2000 characters;
- 413 for files that are too large;
- 404 for unknown orders.

### What the carpenter can say (handled deterministically, no language model)

| Input | Effect |
|---|---|
| ja / passt / ok / los / rechnen / "ja, Standard passt" | preview → generate; an open question → take its default |
| nein | "Was soll anders werden? …" |
| nochmal / neue Bilder | generate again with the same plan |
| senden / an den Kunden | `delivery.create_version(…, pin=True)`: a customer version with a LAN link and PIN is **prepared, not sent** |
| stopp / abbrechen | cancel the running generation |
| status, hilfe | state and the current pieces, or the help text |
| in fein rechnen / als Entwurf | set the order's quality |
| Raumhöhe 2,50 / "die Decke ist 2,60 hoch" | calibrate (geometry_api.calibrate like step ①); placements scale, raumhohe pieces follow |
| "20 cm nach links", "den Schrank etwas weiter rechts" | move along its wall in the photo's left/right; a neighbour on the same wall is pushed along when there is room |
| a bare size answer ("zwei zwanzig", "1,80") to an open size question | that field, in cm |

Everything else goes to `intent.understand` with the focused piece as `active_piece`. Examples: "achtzig tief",
"nimm Eiche", "der Fernseher soll auch weg", "stell das Sideboard links neben das Fenster", a new piece. The
resulting ops are applied, the changed pieces are re-projected or placed again, and a new preview follows.

## How placement works (and why it is not the plain tap)

`ground.locate` returns the *spot*: in practice the VLM points where wall and floor meet, and it does not know how
tall the piece is. `place.placement_for` (tap semantics) instead puts the **middle of the box** on the point. For a
297 cm wardrobe that failed on the first live room: every variant returned None. So `_anchor()`:

1. Uses the wall `ground` reports, or the nearest wall within 60 cm of the floor point. The piece stands, or hangs
   at its own `elev`, with its back on that wall, centred along the wall at the point.
2. Shifts the piece so it stays within the measured wall extent.
3. Keeps it from standing inside a piece already on that wall. If neither side is free but both fit, the neighbour
   makes room. Pieces that hang above another one are ignored here.
4. **"neben X"** ("links neben der Tür", "rechts neben dem Fernsehschrank"):
   - finds X's frame with one extra VLM call (`find_object` without SAM);
   - slides the piece along the wall in 5 cm steps until it clears X on the named side, with 15 grid units of
     margin, because VLM frames leave out door casings;
   - if the piece cannot clear X within the wall, the preview says so ("passt nicht ganz links neben …").
5. "an seine Stelle", "anstelle", "dort" after a removal becomes "wo <removed object> steht", which
   `ground.snap_to_remove` snaps onto the marked region.

If the anchor gives nothing, the exact tap (`placement_for` with against_wall True/None/False) is the fallback. A
result whose box fills more than 85 % of the photo is refused. A grounding confidence below 0.35, `not_found`, or no
placement leads to a question ("Wo genau soll … hin? … oder „egal“"); "egal" leaves the piece unplaced, and the
prompt then carries the `where` text. Removals of furniture are searched as "… mit allem, was darauf steht" (see
the live results). Front-less pieces (shelf, table, bed …) lose a default "grifflos (Push)" unless handles were
said, because otherwise the prompt asks for push-to-open fronts on an open shelf.

## Generation, judge, retry

- On "ja" the assistant calls `workshop.generate(pid, {kind, base_rev: preview_rev, count: 2})` (the web app's path)
  under the order's quality. New assistant orders get `quality` from `profile.effective()` and
  `judge.auto_retry = true`.
- If the design changed since the preview, for example in the web app, the assistant shows a new preview instead of
  generating. It checks `projects.changed_since(…, workshop.DESIGN)` and also handles the generate route's own 409
  "changed".
- A 409 "läuft schon" attaches to the running job.
- **Retry.** The scheduler makes one corrected image when every verdict fails. If the order has `auto_retry` off and
  nothing retried, the assistant generates one image itself, once.
- Done means none of the jobs, including the retry, is in `pending_jobs` and none is still being graded.
- **Result.**
  - Up to 3 images ranked by (pass, overall), each with its score text and judge comment;
  - `offer.offer_text(doc, best)`;
  - `delivery.draft_sheet` PDF;
  - DXF via `connect.render_file`;
  - one GLB per piece.
  If `profile.auto_send_decision` allows it (off by default), the customer version is prepared automatically.
- **Judge down.** Images arrive "ungeprüft (Prüfer nicht erreichbar)" with no retry, and the plan check note says
  "KI-Plancheck gerade nicht möglich."
- **Language model down.** The assistant waits about 20 s and checks again; if it is still down it says so in German
  and returns to the previous state.
- **Grounding down.** Pieces stay unplaced (their `where` text goes into the prompt), and the preview says so.
- **One GPU.** Grounding and SAM run under `workshop.EXCLUSIVE`, so they never run alongside a FLUX image. A status
  message warns when the GPU or the engine lock is busy (for example the model preload after a restart).
- **MAX_PIECES.** intent's warning ("Höchstens 4 Möbel …") is shown, and the order never gets more than 4 pieces
  (tested).

## Robustness

- Per-order RLock around every read-modify-write of the conversation file.
- `_apply` re-reads the order and retries once on a 409 (a concurrent web edit).
- Exceptions in a step produce a German error event and the previous stable state.
- `recover()` at start:
  - re-attaches the watcher for `generating` conversations;
  - turns interrupted busy states into an error event ("Bitte die letzte Nachricht noch einmal schicken");
  - deletes conversations of purged orders.
  The first agent route that meets a purged order deletes its conversation too, because `delivery.purge` does not
  know `projects/agent/`.
- A description starting with "TEST" gives an automatic title "TEST Auftrag vom …", which takes no Vorgangsnummer
  and can be purged. Reason: untitled orders (API, Telegram, mail) call `projects.next_ref`, which writes
  settings.json. That is correct for real orders, but not wanted for the owner's first trials.
- Audio is never stored, only the transcript.

## Tests

```
.venv/bin/python -m pytest -q tests/test_agent.py   → 30 passed (≈ 13 s; 4 repeated runs green)
.venv/bin/python -m pytest -q                       → 675 passed, 1 skipped (100 s; before: 645 + 30 new)
node --test tests/*.test.mjs                        → 85 passed
```

The fakes stand in for the models only. The real `intent.plan()` turns scripted model readings into ops. The real
`place.py`, the PATCH path, `workshop._check_rev`, the preview drawing, the offer text, the DXF and GLB export and the
router all run, on tmp stores. A socket guard fails any network connection.

Covered:
- the full flow photo + text → preview → "ja" → result (DXF bytes, GLB, PDF path, offer text, mode 600, gapless n);
- the width question with an answer and with the default, a not-understood answer that repeats the question, and
  shelf handles cleared;
- a correction with re-projection and focus;
- moves left and right without the language model, and a neighbour pushed along (a too-far move is refused);
- "neben X" sliding;
- "not found" → question → "egal";
- a where answer that triggers relocation;
- judge fail → exactly one retry → best of 3;
- judge unavailable;
- plan check problems shown in the preview;
- a correction during a generation (cancel and new preview), and "stopp";
- a web edit after the preview, which gives a new preview instead of a generation;
- need_photo with queued text, and photo only → "Was soll gebaut werden?";
- audio ok and audio unavailable;
- intent unavailable;
- MAX_PIECES;
- room height calibration;
- "senden" (fake create_version);
- subscribe/unsubscribe and `events(after)`;
- a web-app order getting a conversation, and a purged order losing it;
- one worker per order;
- recover;
- the command parser and the removal phrase;
- the router (400/404/413 paths, preview.png, list);
- the preview pixels (wireframe colour inside the box, red removal outline).

## Live end-to-end (flux-studio on 7860, real Qwen3.8 VLM, DepthPro, SAM 2.1, FLUX, judge)

All through the HTTP routes with copies of FLUX evalset rooms. Timings are wall-clock, including cold model loads.
After each restart FLUX preloaded for about 2 min and held the engine lock.

### Kept demo order: `fc44e668d04b` "TEST Assistent Demo Wohnzimmer" (living room 20261018-193206)

| Step | Input | Answer | Time |
|---|---|---|---|
| start | photo + "An die Rückwand rechts neben den Fernsehschrank kommt ein Einbauschrank, zwei Meter breit, raumhoch, Weißlack, drei Türen grifflos. Der alte Fernsehschrank kommt weg, an seine Stelle ein Sideboard in Eiche, eins achtzig breit." | understood correctly (2 pieces, 1 removal); preview: wardrobe against the back wall, sideboard where the TV unit stands, TV unit outlined | 132.9 s (DepthPro + intent 93 s, removal 14 s, 2 × locate + plan check + drawing 26 s) |
| correction | "Raumhöhe ist 2,80" | calibrated; wardrobe 297 → 280 cm; new preview | 10.6 s |
| round 1 | "Ja, passt, rechnen." (Entwurf) | 2 images + 1 scheduler retry; judge 7.2 / 7.2 / 7.0, all fail | 249 s |
| correction | "Der Fernseher soll auch weg." | TV found and outlined, new preview | 144.8 s (about 2 min waiting for the preload's engine lock) |
| quality | "Bitte in fein rechnen" | quality fein | 1.5 s |
| round 2 | "ja" (Fein) | 3 images: 7.0 / 6.1 / 5.5, all fail | ≈ 5 min |
| correction | "Das Sideboard nur eins fünfzig breit." / "Das Sideboard 20 Zentimeter nach rechts." | 150 cm; moved, and the wardrobe moved with it | 16.6 s / 9.1 s |
| round 3 | "ja, los" (Fein) | 3 images: 7.3 / 7.3 / 6.7, all fail per judge | ≈ 5 min |

What I saw in the images (I looked at every one):
- **Round 1:** composition right, but every image had a thin **ghost strip above the sideboard**. SAM had cut out
  only the TV unit, not the TV standing on it, and the protected photo kept the TV's top edge. Fix in code: furniture
  removals are searched as "… mit allem, was darauf steht" (VLM box top 495 → 416, now including the TV).
- **Round 2:** the 180 cm sideboard reached into the potted plant, and FLUX left half-erased leaves on the wall.
  The wall is 396 cm wide and the plan needs 380 cm, so the plant could not stay. The conversation fixed it (narrower
  sideboard plus the move that pushes the wardrobe). Improvements that came from this round: a 3D overlap warning
  along walls, and pushing neighbours on moves.
- **Round 3** (variants `xoc2ynbt` and `yslppn4m`, the best of this order): **customer-ready in my judgement.** TV
  and unit are gone without a trace, the plant is intact, the oak sideboard has 3 flush doors, the white 3-door
  handleless built-in reaches the ceiling, and the room shell is unchanged. The judge still failed them, with
  "Einbauschrank zu niedrig, erreicht die Decke nicht" / "zu schmal (150 statt 200 cm)", which contradicts the
  measured plan the images follow. That is a judge calibration issue (see limits). Each of these rounds therefore
  triggered the scheduler retry: 1 extra image, about 1 min.

The order keeps all 9 variants (3 rounds), 5 previews, the PDF drafts, the DXF and the GLBs for the frontend work.

### Scenario 2 (purged afterwards): empty living room 20261018-211325

- "Unter das Fenster kommt ein Sideboard in Nussbaum mit Griffleiste. Und links neben die Tür ein Wandregal in Eiche,
  ein Meter zwanzig breit. Die Umzugskartons kommen weg." → the first question after 89.7 s.
  - intent missed the shelf's width, so there were two questions: "eins sechzig" → 1.5 s, "ja" → default 120.
  - "ja, Standard passt" was not recognised at first; fixed and tested.
  - The boxes were removed with a tight SAM outline. Preview after 39 s.
- The first preview put the shelf over the door frame; the plan check flagged it. That led to the "neben X" logic,
  and after it the shelf cleared the door.
- The plan check flagged the sideboard in front of the radiator.
  - "nur ein Meter zwanzig breit" → 18 s.
  - "links neben das Fenster" → 38 s. Honestly reported as "passt nicht ganz": the wall strip there is too short.
  - "an die Rückwand rechts neben die Tür, wo die Kartons stehen" → 36 s, clean plan check.
- "passt so, rechnen" (Entwurf) → **2 images, both passed the judge, 8.5 / 8.5, after 158 s.** Customer-ready in my
  judgement: boxes gone, walnut sideboard right of the door, oak shelf left of it, room unchanged. The judge's only
  remark (shelf "open instead of push fronts") came from the default handles and is now fixed.

### Voice round-trip

**No TTS exists on the machine** (no espeak, espeak-ng, pico2wave or piper), so German speech could not be tested.
Instead, the 29 s English system wav (`/usr/share/sounds/speech-dispatcher/dummy-message.wav`) went in as a voice
message:
- the transcript came back correctly after 107 s (cold Whisper load while FLUX preloaded);
- intent found no order in it, and the assistant asked "Was soll gebaut oder geändert werden?";
- Whisper was unloaded afterwards.

### /api/v1 → agent (connect.py front door)

Temporary API key; `POST /api/v1/orders` with a bathroom photo and "TEST Unter das Waschbecken ein Badmöbel, ein
Meter breit, in Eiche, zwei Schubkästen.":
- 201 in 0.6 s; the preview followed after 45.7 s;
- title "TEST Auftrag vom …", renamed to "TEST Badmöbel (Assistent)";
- the preview image is served as `ereignis_6.jpg` (200 image/jpeg); without the key, 401;
- the key was deleted, the order purged, and the empty `apikeys.json` and `v1_orders.json` (which did not exist
  before) removed.

The preview showed a geometry limit: DepthPro estimated the camera at 105 cm, 11 % floor inliers, no ceiling, so the
vanity (correctly against the back wall under the basin) is drawn far too large.

### Clean-up and real data

- md5 of every real `projects/workshop/*.json` and of `settings.json`: unchanged.
- All 59 new files under `outputs/` belong to the kept demo order (checked with `delivery.referenced_files`), and no
  file disappeared.
- The conversations of the purged orders were deleted on the first 404.
- Channels stayed off, webhooks stayed off, and nothing contacted the internet.

## Known limits (honest)

- **The judge is strict on built-ins.** It failed 9 of 9 images in the living room, several clearly good, with size
  claims that contradict the plan ("zu niedrig", "zu schmal"). Every such round costs a retry image (about 1 min),
  and the result text says "keins hat die KI-Prüfung bestanden". It passed 2 of 2 in scenario 2. Retuning the judge
  is not this module's job, but it affects the assistant's tone.
- **The geometry decides the scale.** On the small bathroom DepthPro's measurement was poor and the preview drew the
  vanity at the wrong size. The assistant only shows "Maße geschätzt …" and cannot detect a bad measurement.
  "Raumhöhe …" only rescales when a ceiling was detected.
- **Placement heuristics.**
  - Wall packing looks at one wall; corners between two walls are not checked.
  - "neben X" costs an extra VLM call (about 10 s) and trusts X's frame.
  - A height calibration scales poses and can create overlaps; they are warned about, not fixed.
  - "weiter links/rechts" moves along the wall; "nach vorne/hinten" is not supported.
- **The plan check (judge) is sometimes wrong**, e.g. "blocks the sink" for a vanity under the basin. Its notes are
  shown, never acted on.
- **Latency.**
  - Preview: 40–130 s, mostly DepthPro on a new photo, intent at about 7 s and grounding at 6–14 s per phrase.
  - After a server restart, grounding waits up to about 2 min for the FLUX preload (engine lock).
  - Generation: 2.5–5 min.
- **intent misses** can cause an extra question; for example, the shelf width in scenario 2 was not attached.
- **"senden"** prepares a customer version (LAN link and PIN, nothing sent). It was only tested with a fake
  `create_version`; no real version was created live, to keep customer links out of the test.
- **Not exercised live:**
  - real German speech (no TTS);
  - Telegram, mail and webhooks (hard rule 2);
  - `profile.auto_send` (off by default).
- **No frontend yet:** Assistent panel, mic button, "Neuer Auftrag per Foto + Sprache". The routes and event data
  are ready.
- **Private helpers used:** `projects._store_upload`, `geometry_api._analyze`, `workshop._open/_geo/_is3d/_relations`,
  `place._wall_hung`. A public helper in those modules would be cleaner.
- The conversation file is not deleted by `delivery.purge` itself. It goes on the first agent call for that order or
  at the next server start.

## Integration (applied in this step)

`server.py`: 4 changes. The pre-change copy is
`/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/agent/server.py.pre-agent` (session scratchpad).

```python
import agent            # with the other module imports (before `import backup`)
```

In `start_front_doors()`, before the subscription:

```python
    try:
        print("agent:", agent.recover(), flush=True)   # follow running generations again, unstick interrupted steps
    except Exception:
        traceback.print_exc()
```

The existing code then subscribes `connect.on_agent_event` (webhooks, a no-op while they are off) and calls
`channels.start_all(agent, out_dir=OUT)`. That starts only enabled channels: "channels: none enabled".

Shutdown, after `connect.stop`:

```python
    await run_in_threadpool(agent.stop)
```

Router loop: `…, channels.router, connect.router, connect.v1, agent.router)`.

No change in `team.py`: `/api/agent/*` is a normal team route. Nothing else was touched. flux-studio was restarted.
The start log shows `agent: {'watching': [], 'interrupted': [], 'removed': []}` and `channels: none enabled`, with
no tracebacks.

### For the frontend (next phase)

- **Assistent panel per order.**
  - Poll `GET /api/agent/{pid}/events?after=n` every 1.5 s while `busy` or `state == "generating"`.
  - Show `preview` and `result` images from `/outputs/<image>`.
  - Send with `POST /api/agent/{pid}/message` (FormData: `text` or a recorded `audio` blob).
  - A question's `data.default` can become a "Vorschlag übernehmen" button that sends "ja".
- **"Neuer Auftrag per Foto + Sprache":** `POST /api/agent/start` (FormData `photo`, `text` | `audio`, `title`), then
  open the panel.
- **List:** `GET /api/agent`.
