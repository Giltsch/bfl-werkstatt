# intent.py: understanding the order (Phase 1)

Status 2026-10-18: built, unit-tested, evaluated live against Qwen. **Not integrated yet**: `server.py` is unchanged,
so the router is not mounted and nothing is reachable. See "Integration" below.

## What it does

The carpenter's spoken or typed order goes in, for example "links neben dem Fenster ein Einbauschrank, zwei-zwanzig breit,
raumhoch, Weißlack, drei Türen grifflos, der alte Schrank kommt weg". What comes out:

```
{"status": "ok", "text": "<normalised text>",
 "summary_de": "Einbauschrank 220 × raumhoch × 60 cm, Lack weiß matt, grifflos (Push), 3 Türen (links neben dem Fenster); aus dem Foto entfernen: der alte Schrank",
 "pieces": [{"ref": null, "id": "k3x9a1", "type": "schrank", "w": 220, "h": 255, "d": 60, "elev": null,
             "material": "weiss", "material_custom": "", "handles": "push", "text": "3 Türen",
             "where": "links neben dem Fenster", "full_height": true}],
 "removes": [{"what": "der alte Schrank"}],
 "drops": [], "questions": [], "warnings": [],
 "ops": [{"op": "set", "path": "pieces.k3x9a1.type", "value": "schrank"}, ...],
 "confidence": 0.95, "seconds": 6.4}
```

The fields follow the SPEC contract. These are **extensions** (additive; consumers may ignore them):
- `pieces[].id`: the piece id the ops write to. It is an existing id for a correction, the order's empty default
  piece when that gets filled, or a fresh id for an `add_piece`. agent/place can use it to place the piece.
- `pieces[].changes` (corrections only): German change list, e.g. `["Tiefe 80 cm"]`.
- `drops`: `[{"id", "label"}]`, order pieces the customer no longer wants, turned into `remove_piece` ops. SPEC
  `removes` stays reserved for objects **in the photo**.
- `warnings`: German notes, e.g. "Raumhöhe nicht gemessen – 250 cm angenommen." or "Höchstens 4 Möbel …".
- `text`: the normalised input. `seconds`: the call duration.

### Pipeline
1. **`normalize_de(text)`** is deterministic and needs no model. It turns every length into `N cm` and number words
   into digits:
   - spoken metre forms: `zwei zwanzig`, `zwo-zwanzig`, `zweizwanzig`, `einsachtzig`, `einsfünfundachtzig`,
     `zwei null fünf`
   - with "Meter": `ein Meter achtzig`, `einen Meter zwanzig`, `1 m 20`, `1m20`, `zweieinhalb Meter`,
     `anderthalb Meter`, `einen halben Meter`
   - decimals: `2,20`, `2.20 m`, `0,6`
   - units and suffixes: `sechzig Zentimeter`, `600 mm`, `220er`, `60er Tiefe` → `60 cm tief`, `sechziger Tiefe`
   - bare numbers next to a dimension word: `achtzig tief`, `Tiefe 60`, `zwanzig breiter`, `Unterkante 140`
   - chains: `220 mal 60 mal 240` → `220 × 60 × 240 cm`
   - ceiling phrases: `raumhoch`, `deckenhoch`, `Raum hoch`, `bis unter die/d' Decke`, `bis zur Decke`,
     `vom Boden bis zur Decke` → `raumhoch`. `halbe Höhe` / `halbhoch` → `halbe Raumhöhe`.
   - Bavarian/Franconian: `zwoa`, `zwa`, `oans`, `fuchzg`/`fuchzich`/`fuffzig`, `sechzg`, `zwanzich`, `zwoarazwanzg`
     (22), `oanazwanzg`, `hundat`, `fünfe`, plus `broad`/`hoach`/`dief` → breit/hoch/tief.

   Counts stay counts: `drei Türen` → `3 Türen`, `dreizehn Fächer` → `13 Fächer`, `zwei sechzig breite Schränke` →
   `2 × 60 cm breite Schränke`. Non-lengths stay as they are: `12 m²`, `2,50 Euro`, `19 mm`, `1.000`, and the article
   `ein`. The function is idempotent.
2. **Model call** (`judge.ask`, strict JSON schema, thinking off, temperature 0 with one re-ask at 0.3):
   - The model only extracts what was said. The schema enums are the catalog types, materials (incl. `custom`) and
     handle ids. `ref`/`drop` are enums of the order's real piece ids, so the model cannot invent ids.
   - Integers carry `minimum`/`maximum`. Without bounds, vLLM's guided decoding produced runaway digits
     (`"w": 22000000…`).
   - **No field is required, and the prompt asks for compact one-line JSON without empty fields.** This cut the answer
     from about 146 to about 34 tokens per piece. Decoding runs at about 7 tokens/s while FLUX shares the GPU, so the
     median fell from 21.5 s to 6.5–7 s.
   - The prompt carries the catalog with spoken synonyms, the profile's vocabulary (`profile.vocabulary_hint()` when
     available), the ceiling height, and the order's pieces with the ACTIVE one marked.
3. **`plan(reading, doc, profile, text)`** is pure Python and does everything else:
   - validation of every field
   - defaults: the profile first, else the catalog: Einbauschrank 60 deep and full room height; Sideboard 45/75;
     Hängeschrank 35/60 hung at 140; Wandregal 25/40 hung at 150; Küche 60/220; Garderobe 40/200; Bad 48/55.
     Handles default to `push` only for types with fronts.
   - `raumhoch` → the measured ceiling (`geometry.ceiling.height` if `geometry.ok`, else
     `calibration.room_height_cm`, else 250 plus a warning).
   - a wall-hung piece "bis unter die Decke" with its own height → `elev = ceiling − h`
   - questions only for essentials: `w` for types that need a width (unless `full_width` was said), and `type` for
     vague requests. Each question has a `default`.
   - corrections write only the changed fields. A type change moves untouched defaults the way the type tiles do
     (`static/ws/step-moebel.js typeOps`). A bare correction without a reference goes to the active piece.
   - MAX_PIECES (4) is enforced with a warning. The order's empty default piece is filled with `set` ops before any
     `add_piece`. `remove_piece` of the last piece carries a `replacement` (the new piece if there is one, else an
     empty piece).
   - deterministic safety nets for two observed model slips: the material/handles word in the text when the model
     left it out (single new piece, only if unambiguous), and `raumhoch` in the text when the model filed it under
     `text` instead of `full_height`.
4. **Router** `POST /api/workshop/{pid}/understand {text, apply?: bool, base_rev?: int}`:
   - Without `apply` it is a preview and nothing is written.
   - With `apply: true`, `base_rev` is required (400 otherwise). The ops go through
     `projects.apply_ops(pid, base_rev, ops, who)`, so conflicts come back as that function's normal 409. The response
     adds `doc` and `rev`.
   - The model being down gives `status: "unavailable"` with HTTP 200 and nothing applied.
   - Empty text or text over 2000 characters gives a German 400.

## How to use

```python
import intent
intent.normalize_de("zwo-zwanzig breit, sechzg dief")          # → "220 cm breit, 60 cm tief"
doc = projects.load("workshop", pid)
r = intent.understand("nein, achtzig tief", doc, profile=None)  # profile: module/object with defaults_for(type) or a dict
if r["status"] == "ok" and not r["questions"]:
    projects.apply_ops(pid, doc["rev"], r["ops"], who)
for q in r["questions"]:          # ask the carpenter, then send the answer as a new utterance (it becomes a correction)
    print(q["question_de"], "(Vorschlag:", q["default"], ")")
```

- `photo=` takes PIL or bytes and is passed to the VLM as context. It is optional; text-only is faster.
- The profile is duck-typed:
  - an object or module with `defaults_for(type)` returning `{d, h, w?, elev, material, material_custom, handles,
    full_height}`; `vocabulary_hint()` is used if present
  - or a dict `{"types": {type: {...}}}` or `{type: {...}}`; `{"aliases": {field: {phrase: id}}}` is used for the
    prompt
  - The router uses the installed `profile.py` when it has `defaults_for`, else the built-ins.

## Tests and evaluation (real numbers)

**Unit tests:** `.venv/bin/python -m pytest -q tests/test_intent.py` → **172 passed** (0.6 s, no model, tmp stores).
Covered:
- 104 `normalize_de` variants plus idempotence, `parse_number` and `spoken` tables
- `plan()` defaults, questions, profile duck-typing, custom material, corrections, type change, active-piece fallback,
  drops/replacement, MAX_PIECES, ceiling-hung `elev`
- every produced op set applied with `projects.apply_ops` on a tmp project store (new order, two pieces, corrections,
  drops, type changes)
- `understand()` with a mocked vLLM (prompt contents, ref enum in the schema, unavailable, error after re-ask, sloppy
  JSON repaired)
- the router through `TestClient` (validation, preview vs. apply, 409 conflict, unavailable does not apply)

`tests/test_projects.py` and `tests/test_judge.py` still pass (252 together with test_intent).

**Live evaluation:** `.venv/bin/python tools/eval_understand.py [--profile] [-k id] [--json out.json] [--tmp dir]`
- 59 cases in `evalset/cases.json`: Einbauschrank, Dachschräge, Sideboard/Lowboard/Kommode, Küche, Regalwand,
  Wandregal, Hängeschrank, Garderobe, Bad, Tisch, Bett, Sitzbank, Treppe, Wandverkleidung, Tür, custom materials, two
  pieces, a correction plus a new piece, 10 corrections, drops, photo removals, replacement, vague texts, a full order,
  mm figures, Bavarian/Franconian, and Whisper-style split words.
- The ops are applied on a throw-away store under `--tmp`. The real store is never touched.

| Run (Qwen3.8-27B-FP8, FLUX sharing the GPU) | cases | all fields | cases fully right | median / p90 / max |
|---|---|---|---|---|
| 1: first prompt, all fields required | 53 | 341/344 = 99.1 % | 51/53 | 21.5 / 23.7 / 37.5 s |
| 2: optional fields + compact JSON | 53 | 343/344 = 99.7 % | 52/53 | 6.5 / 8.5 / 10.4 s |
| 4: +6 harder cases, before the raumhoch net | 59 | 380/382 = 99.5 % | 58/59 | 6.7 / 8.7 / 10.4 s |
| **5: final code** | 59 | **382/382 = 100 %** | 59/59 | 7.0 / 9.1 / 10.5 s |
| **6: final code, repeat** | 59 | **382/382 = 100 %** | 59/59 | 6.7 / 8.8 / 9.9 s |
| 7: final code with `--profile` (profile.py, default pack) | 59 | 381/382 = 99.7 % | 58/59 | 7.0 / 8.8 / 10.1 s |

Per field in the final runs, 100 % each: type 46/46, w 46/46, h 19/19, d 20/20, elev 4/4, material 39/39,
handles 13/13, ref 55/55, full_height 6/6, piece count 57/57, removes 6/6, drops 11/11, questions 59/59, and
ops accepted by apply_ops 59/59. The profile run had one miss: "Lack schwarz" became `anthrazit` instead of `custom`.

How the misses were fixed along the way:
- "höchste Stelle 200 cm" under a Dachschräge → prompt rule
- "streichen, stattdessen" modelled as a type change → prompt rule
- "Esche" left out → `spoken()` safety net
- "Hängeschrank bis unter die Decke, 60 hoch" → elev rule
- "raumhoch" filed under text → safety net

## Known limits (be honest)

- **The eval set is self-written.** I wrote both the cases and the prompt (no real carpenter recordings yet), so 100 %
  overstates real-world accuracy. Temperature-0 output still varies between runs: runs 4 and 7 each had one different
  miss. The next step is to replay real transcripts from `evalset/audio/` (voice.py) once carpenters have recorded
  some.
- **Latency is about 7 s per utterance.** Almost all of it is decoding at about 7 tokens/s on the shared GPU. Two
  pieces take about 9–10 s, and a vague text with an empty answer about 5 s. With `EXCLUSIVE` set to the scheduler's
  exclusive(), a running FLUX image would add its remaining time, so it is not set by default.
- **Photo removals are only named** (`removes[].what`), and no `set removes` op is produced. A removal needs an
  outline from ground.find_object plus SAM, which is the agent's job.
- No placement: `where` is only passed on. place.py and ground.py turn it into a placement.
- Normalisation heuristics:
  - "zwei sechzig" before an inflected adjective without an article (`zwei sechzig breite Schränke`) is read as
    2 × 60 cm. With an article (`eine zwei sechzig breite Kommode`) it is 260 cm.
  - A lone single-digit pair like "zwei fünf" is not read as 205; "zwei null fünf" is.
  - Plain numbers become cm only next to a dimension word or unit, or when they are a decimal with one integer digit.
- `halbe Raumhöhe` is resolved by the model, which sees the ceiling height. It is right in the eval but not
  deterministic.
- "Zwei Hängeschränke à 60" becomes one piece or two depending on the model. There is no test for it.
- Relative corrections ("20 cm breiter", "doppelt so breit") are computed by the model from the listed current dims.
  They were right in all runs, but this is not guaranteed.

## Integration

Exact code for the shared files (the integrator applies it; nothing here was changed by this module):

**server.py**, next to the other imports:
```python
import intent
```
and add `intent.router` to the router loop:
```python
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, intent.router):
    app.include_router(r)
```
- The route lives under `/api/workshop/…`, so `team.install` already protects it. No new port, no public exposure,
  nothing to change in team.py.
- Optional, **not recommended** for interactive use: `intent.EXCLUSIVE = sched.exclusive` (next to
  `workshop.configure(...)`) would serialise the call with FLUX images.
- Frontend (later, "Verstanden:" card in the Möbel step):
  1. `POST /api/workshop/{pid}/understand {text}` → show `summary_de`, `questions`, `warnings`
  2. on "Übernehmen": the same call with `{text, apply: true, base_rev: store.doc.rev}`, then take `doc`/`rev` from
     the response like a PATCH. A 409 has the same shape as the PATCH conflict.

  Alternatively, send `ops` through the existing store PATCH path, which keeps undo working.
- agent.py: call `intent.understand(text, doc, profile)` and apply `ops` with `projects.apply_ops`. Then use
  `pieces[].id` + `pieces[].where` for ground/place, and `removes[].what` for `ground.find_object`.
