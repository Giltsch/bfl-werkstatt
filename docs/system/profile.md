# profile.py: company profile and trade packs (Phase 5)

Status: built and unit-tested on 2026-10-18. Not wired into `server.py` yet, so nothing is reachable until the integrator applies the Integration section below. No new ports, no network access, no new dependencies.

## What it is

The profile records how this workshop usually builds, so the assistant (intent/agent) can fill sensible defaults instead of asking. Values are layered, and a later layer wins:

1. **Catalog**: `workshop.CATALOG` dims (`w, h, d`) and `elev`.
2. **Trade pack**: `packs/<id>.json`. There are 5 packs: `schreiner`, `kueche`, `treppe`, `terrassendach`, `schiebetuer`.
3. **Company**: `projects/profile.json`, which is written atomically (mkstemp, fsync, `os.replace`) with mode 600, under a module RLock.

In the stored profile, `None` means "not set here, inherit from the layer below". A JSON merge patch with `null` therefore resets a value to the pack default.

### Stored profile (`projects/profile.json`)

```
{"version": 1, "trade": "schreiner",
 "company": {"material": "eiche"|"custom:<Text>"|null, "material_custom": "", "handles": "push"|"keine"|null,
             "plinth": cm 0–30|null, "board": mm 8–200|null},
 "types": {"schrank": {"w","h","d": cm, "elev": cm, "full_height": bool, "doors": {"per": cm 20–200},
                       "material", "material_custom", "handles"}, ...},       # catalog types or pack template ids
 "aliases": {"type": {"Wort": "<type|template>"}, "material": {"Wort": "<id|custom:Text>"}, "handles": {"Wort": "<id|keine>"}},
 "wording": {"notes_de" (≤1000), "prompt_en" (≤600), "judge_en" (≤600)},
 "quality": "entwurf"|"fein"|null,
 "auto_send": {"enabled": false, "min_overall": 7.5, "require_pass": true, "max_images": 2},
 "learning": {"enabled": true, "threshold": 3},
 "learned": {"counts": {...}, "suggestions": [...]},      # written only by learn()/accept()/dismiss()
 "updated": iso, "updated_by": owner}
```

- **IDs match the app.** Types are `projects.PIECE_LABELS`, which equals `workshop.CATALOG["pieces"]`. Materials are `projects.MATERIAL_LABELS`, plus `custom` / `custom:<Text>`. Handles are `projects.HANDLE_LABELS`, plus `keine`. A test checks that every default from every pack passes `projects._piece_value`.
- **Explicitly no handles.** `handles: "keine"` means "no handles at all" (stairs, terrace roofs). It resolves to `None` in the effective view and in `piece_defaults`.
- **Woods outside the catalog.** `"custom:Douglasie natur"` covers woods the catalog lacks (Lärche, Douglasie, Buche, Spiegelglas …). It becomes `material="custom"` with `material_custom="Douglasie natur"`.
- **Doors rule.** `doors: {"per": 50}` means one door leaf per 50 cm. `doors_for(defaults, w) = round(w / per)`, clamped to 1–12, which is the same rounding `mockup.parts` uses.

### Trade packs (`packs/*.json`)

Each pack has: `id, label, description, types` (catalog types offered first), `templates`, `company`, `type_defaults`, `aliases`, `wording` (`notes_de`, `prompt_en`, `judge_en`) and `quality`.

- **Templates** are named presets on top of a catalog type, for example `schiebetuerschrank` (based on `schrank`) or `kochinsel` (based on `kueche`). They carry a German description `text` and their own dims and doors.
- **Free-text templates.** `terrassendach`, `pergola` and `carport` have `type: null` because the catalog has no outdoor types. They run as free-text pieces (`type` None + `text`), which `projects` and `workshop.piece_desc` already accept.
- **What a template inherits.** It takes its base type's pack defaults and the company's material and handles for that base type. It keeps its own dims.

| Pack | Templates | Aliases | Company defaults | Quality |
|---|---|---|---|---|
| schreiner | 3 (dachschraege, nischenschrank, lowboard) | 87 | eiche, push, plinth 8, board 19 | entwurf |
| kueche | 4 (kochinsel, kuechenzeile, hochschrankwand, unterschrank) | 46 | weiss, leiste, plinth 10 | entwurf |
| treppe | 5 (wangen-, faltwerk-, podest-, aufgesattelte Treppe, treppenregal) | 26 | eiche, keine Griffe, board 40 | fein |
| terrassendach | 4 (terrassendach, pergola, carport, sichtschutz) | 19 | custom:Douglasie, keine, board 120 | fein |
| schiebetuer | 4 (schiebetuerschrank, raumteiler, schiebe_dachschraege, nische_schiebe) | 29 | weiss, leiste, plinth 0, schrank d 65 / doors per 100 | entwurf |

Template texts are written so that `mockup._count` finds the door count. For example "2 Türen als Schiebetüren …" is counted, while "2 Schiebetüren" would not be.

## API (Python)

| Function | Returns / does |
|---|---|
| `get()` | Stored profile, normalized (a missing file gives pack defaults). Never writes. |
| `put(patch, who)` | RFC 7386 merge patch. Strict validation raises `profile.Bad` with a German message, and the file stays untouched on error. `{"learned": null}` forgets everything that was learned. |
| `effective(prof=None)` | Resolved view: `company`, `types` (every catalog type and pack template, each with a `source` map per field: `standard` / `katalog` / `paket` / `firma`), merged `aliases`, `wording`, `quality`, `auto_send`, `learning` and the `pack` summary. |
| `defaults_for(type)` | `{type, template, label, text, w, h, d, elev, full_height, doors, doors_n, material, material_custom, handles, plinth, board, source}`. An unknown type or `None` gives the company defaults with no dims. |
| `piece_defaults(type)` | Only `projects.PIECE_FIELDS`, ready for an `add_piece` op. `w` stays None (it is the order's own measure). `h` is None when the type is `full_height` (raumhoch). |
| `doors_for(defaults, w)` | Door count from the rule. |
| `resolve(text)` | Spoken words to ids, e.g. `{"type", "template", "material", "material_custom", "handles", "hits": [...]}`. Matching is case- and umlaut-insensitive, so "Push-to-open" equals "push to open". Longest phrase wins ("Eiche weiß geölt" beats "Eiche"). Word boundaries apply ("Hängeschrank" does not match "Schrank"). Simple German endings are allowed ("Messinggriffen"). |
| `vocabulary_hint(limit=1500)` | Short lines like "Weißlack, … → material weiss" for LLM prompts. |
| `voice_words(limit=60)` | Alias phrases for the Whisper domain prompt. |
| `prompt_notes()` | `{"notes_de", "prompt_en", "judge_en"}`: pack wording followed by the company's own wording. |
| `auto_send_decision(verdicts)` | `{"send", "picks", "why"}`. Always `send: False` while the rule is off, which is the default. Otherwise it picks the best verdicts with `overall ≥ min_overall` (and `pass` if required), at most `max_images`. |
| `learn(before_piece, after_piece, who, fields=None)` | Records corrections and returns the newly opened suggestions (see Learning rules below). |
| `learn_doc(before_doc, after_doc, who, ops=None)` | Runs `learn` for every piece present in both docs. With `ops`, only fields set explicitly by `set pieces.<id>.<field>` ops count, never side effects. |
| `suggestions(status="open")`, `accept(id, who)`, `dismiss(id, who)` | `accept` writes the value into `types[type][field]` (accepting an `h` also sets `full_height: false`). Both reset that counter. `KeyError` for an unknown id. Idempotent. |

### Learning rules

- **Learned fields:** `material`, `handles`, `d`, `h`, `elev`. `w` is never learned: it differs per order.
- **When a counter goes up:** only when the new value differs from both the old value and the current effective default.
- **One vote per piece:** editing the same piece back and forth counts once.
- **Ignored changes:** type changes, and custom materials.
- **Suggestion threshold:** after `threshold` distinct pieces (default 3) a suggestion opens, for example "Einbauschrank: Tiefe standardmäßig 65 cm statt 60 cm? (3× so korrigiert)". There is no duplicate while it is open.
- **After a dismissal:** counting starts again from zero.
- **Limits:** 400 counters (the oldest are dropped first), 100 suggestions, and 30 piece ids per counter.

## HTTP routes (`profile.router`)

| Route | Result |
|---|---|
| `GET /api/profile` | `{"profile": stored, "effective": {...}}` |
| `PUT /api/profile` (merge patch) | Same shape. 400 with a German `detail`, e.g. "Sockelhöhe muss zwischen 0 und 30 cm liegen.", "Unbekanntes Fachpaket „x“.", "Firmenprofil: unbekanntes Feld „x“." |
| `GET /api/profile/packs` | `{"packs": [{id, label, description, types:[{id,label}], templates:[{id,label,type}], quality}], "current": trade}` |
| `GET /api/profile/suggestions` | `{"suggestions": [open ones]}` |
| `POST /api/profile/suggestions/{id}/accept` | `{"suggestion", "profile", "effective"}`. 404 "Vorschlag nicht gefunden." |
| `POST /api/profile/suggestions/{id}/dismiss` | Same shape. This route is an addition to the SPEC list, so the settings page can say "Nein danke". |

`who` comes from `request.state.owner`, which the team middleware sets.

## Tests and results

`.venv/bin/python -m pytest -q tests/test_profile.py`: **40 passed** in 1.9 s. They use tmp dirs via a monkeypatched `projects.STORE`.

Coverage:

- **Packs and IDs:** all 5 packs validate strictly, and every pack default passes the `projects` PATCH validator.
- **Storage:** mode 600, no temp files left behind, merge patch with null reset.
- **Validation:** 16 bad PUTs are rejected with German messages, and the file stays unchanged after each.
- **Defaults:** trade switch, template inheritance, alias resolution (longest match, word boundaries, `custom:` values, company override, templates), wording append, quality precedence.
- **Learning:** threshold, one vote per piece, already-default values ignored, type change ignored, `w` and custom material ignored, accept, dismiss and recount, height accept turning off raumhoch, `learn_doc` with and without ops, threshold and on/off switch, bounded counters.
- **Concurrency:** 8 threads × 5 learns give exactly 40 counted.
- **Robustness:** auto-send off by default and pick order, a corrupt file is moved aside, lenient load of bad values from disk.
- **cProfile shim:** checked (see the stdlib note under Known limits).
- **Router:** round-trip, 400/404 and the accept flow, via `TestClient` on an app that includes only this router.

Full suite: `pytest tests/`: **421 passed, 1 skipped** (no regressions).

Speed on the DGX: `resolve()` takes about 1.8 ms per call and `defaults_for()` about 1.4 ms per call, with packs re-read only when their mtime changes.

No live model calls: this module uses no LLM.

## Known limits

- **Shadowing the stdlib.** The name `profile.py` (prescribed by the SPEC) shadows the stdlib `profile` module, and `cProfile` does `import profile`. A PEP 562 `__getattr__` at the top of the module forwards unknown attributes to the real stdlib module, so `cProfile.run` keeps working (tested). Anything that needs `isinstance(x, profile.Profile)` against the stdlib class should import `cProfile` directly.
- **Aliases are a deterministic helper, not understanding.** "Der alte Schrank kommt weg" still yields `type: schrank`, and "drei Türen" in a cabinet sentence is not treated as a door, because the bare "Tür" alias was removed on purpose. Context such as removals or negations is intent.py's job (via the LLM). Use `vocabulary_hint()` in its prompt.
- **Some pack types are only free text.** Terrace roofs, pergolas and carports have no catalog type. They are free-text pieces (`type: null`), so `mockup.parts` draws them as plain blocks and the 3D or plan check knows nothing special about them. Real catalog types would need `workshop.CATALOG` + `projects.PIECE_LABELS` + `mockup.parts` changes (not done: shared files).
- **Plinth and board thickness are informational.** `plinth` and `board` are stored and returned, but no generator reads them yet (`mockup.parts` hard-codes an 8 cm plinth). Integration can feed them in later.
- **Scope of learning.** Learning works per type, not per customer or per team member. Only one server process may write the file (a thread lock, no file lock), which matches the current single-process app.
- **Switching trade.** Per-type company overrides keyed on the old pack's templates are dropped when the trade changes, because they no longer resolve.

## Integration (exact code for shared files)

### 1. `server.py`: register the router

Add the import next to the other module imports (after `import projects`):

```python
import profile
```

Then add it to the router loop:

```python
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, profile.router):
    app.include_router(r)
```

The routes live under `/api/profile*`, which is behind the team middleware like every other `/api/` route. No middleware change is needed.

### 2. `projects.py`: learn from manual corrections (PATCH path)

Replace `patch_project` with the version below. The import is lazy because `profile` imports `workshop`, which imports `projects`. Learning must never break a save, so errors are only printed.

```python
@router.patch("/api/projects/workshop/{pid}")
def patch_project(pid: str, body: dict, request: Request):
    before = load("workshop", pid)
    doc = apply_ops(pid, body.get("base_rev"), body.get("ops"), _who(request))
    try:
        import profile as company_profile  # lazy: profile → workshop → projects
        company_profile.learn_doc(before, doc, _who(request), body.get("ops"))
    except Exception:
        import traceback
        traceback.print_exc()
    return {"rev": doc["rev"], "doc": doc}
```

### 3. Consumers (agent.py, intent.py, judge): when those modules are integrated

- **intent.py.** `understand(text, doc, profile=None)` should get `profile.effective()` or use `profile.defaults_for(type)` / `profile.piece_defaults(type)` for missing fields. Add `profile.vocabulary_hint()` to the system prompt and run `profile.resolve(text)` as a deterministic pre-pass. Use `doors_for(defaults_for(t), w)` to write "N Türen" into the text when no door count was spoken.
- **voice.py.** Domain prompt: `", ".join(profile.voice_words())`.
- **agent.py.** Generation quality comes from `profile.effective()["quality"]`. After judging, call `profile.auto_send_decision([v["judge"] for v in variants])`; it only allows sending when the owner switched the rule on. Corrections the agent applies with `set pieces.<id>.<field>` should call `profile.learn_doc(before, doc, who, ops)`, the same as the PATCH hook.
- **Prompt and judge wording (optional).** In `workshop.build_room_job`, append `profile.prompt_notes()["prompt_en"]` to the prompt. In `judge.request_text(req)`, add `profile.prompt_notes()["judge_en"]` as "Trade notes: …". Both are empty-safe strings.

### 4. Frontend (settings page "Firmenprofil", for the frontend phase)

A new entry in `SECTIONS` in `static/ws/settings.js` and in `app.js`: `["profil", "Firmenprofil"]`. The section would contain:

- a trade-pack picker (`GET /api/profile/packs`, then `PUT {"trade"}`);
- company defaults (material, handles, plinth, board);
- per-type defaults from `effective.types`, showing `source` as "Paket" / "Firma";
- an alias table;
- wording text fields;
- quality;
- an auto-send toggle, with a warning that results then go out without being looked at;
- open suggestions with "Übernehmen" / "Nein danke" buttons (accept / dismiss routes).
