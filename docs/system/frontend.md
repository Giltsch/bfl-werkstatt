# Frontend: voice in the Möbel step, the Assistent panel, settings for the profile, channels and API

Status 2026-10-19: built, node-tested, checked in Playwright against mocks (phone 390×844, tablet 1180×820) and live
against the running flux-studio. Nothing is public. No new port and no new route on the server: the frontend only
uses routes that already exist. flux-studio was **not** restarted, because static files are served directly
(`/static`, `/werkstatt` with `no-cache`).

## What was built

| File | |
|---|---|
| `static/ws/voice.js` (new) | Mic button. It uses MediaRecorder (webm/opus, ogg or mp4, depending on the browser) in a secure context. Otherwise it falls back to `<input type=file accept="audio/*" capture>`, which opens the recorder on Android and the file picker on iPhone (Sprachmemos). It shows a hint line, a help sheet („Warum geht das Mikrofon nicht?“) and German messages for every getUserMedia error. It also has `transcribe()` for `/api/voice/transcribe`. |
| `static/ws/verstanden.js` (new) | The „Per Sprache beschreiben“ card in step ② (details below). |
| `static/ws/assistent.js` (new) | `#/p/<pid>/assistent`, the conversation per order, and `#/assistent`, „Neuer Auftrag per Foto + Sprache“ (details below). |
| `static/ws/settings-connect.js` (new) | Settings sections Firmenprofil, Kanäle and API & Webhooks (details below). |
| `tests/ws_system.test.mjs` (new) | 16 node tests for the pure helpers. |
| `tests/ui/system.shots.mjs` (new) + fixtures `agent-demo.json`, `system-settings.json` | Playwright walk with mocked routes. The fixtures are the events of the TEST demo order and the default profile/channel GETs. |
| `static/ws/app.js` | Routes `#/assistent` and `#/p/<pid>/assistent`, a third order tab „Assistent“, the settings sections `profil`, `kanaele` and `api`, the presence text, and a smaller tab padding on phones. |
| `static/ws/dashboard.js` | Action bar entry „Foto + Sprache“ (on ≥ 700 px: „Neuer Auftrag per Foto + Sprache“), and a link in the „Neuer Auftrag“ sheet. |
| `static/ws/step-moebel.js` | Mounts the card at the top of step ②. It is one instance per visit, so a re-render moves the node and a recording or result survives. |
| `static/ws/settings.js` | SECTIONS and the dispatch to the new sections. |
| `static/ws/ui.js` | `shrink` is exported, and new icons: mic, send, stop, chat, key. |
| `tests/ui/check.mjs` | `launch({args})`, used for the fake microphone. |

Pre-change copies are in `/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/frontend-pre/` (app, settings, dashboard, step-moebel, ui).

### „Per Sprache beschreiben“ card (verstanden.js)

The flow:
1. Speak, or tap „Tippen statt sprechen“.
2. The audio goes to `/api/voice/transcribe`.
3. The transcript is shown in an editable text field.
4. `POST /api/workshop/{pid}/understand {text, active_piece}` runs as a preview; nothing is saved yet.
5. The **„Verstanden:“** card shows:
   - pieces with dimensions, wood, handles and „Wo: …“;
   - corrections as a change list;
   - pieces that will be dropped;
   - open size questions as number fields, pre-filled with the suggestion;
   - photo removals, with a pointer to step 3 („Etwas aus dem Bild entfernen“) or the assistant;
   - warnings, and a low-confidence note.

**„Übernehmen“:**
- Fills every open size question with its value or suggestion.
- Sends the ops through `store.patch`, the normal PATCH path, so conflicts, the offline queue and the profile learner apply.
- Opens the first new piece.
- Shows a toast with „Rückgängig“. The exact inverse ops come from `undoOps`, and the node test checks the round trip.

A vague text (a question with no default) blocks „Übernehmen“ with „Bitte genauer sagen …“. Speaking again before
„Übernehmen“ appends to the description („nein, 160 breit“), and the model reads both together.

### Assistent panel (assistent.js)

**Timeline:**
- user text, voice messages and photos on the right;
- agent text, questions and errors on the left;
- status lines centred and muted;
- previews as a large picture, tap to enlarge, plus „Im Foto selbst verschieben …“ (→ step 3);
- results as an image grid with score badges (✓/⚠/✗/ungeprüft), „Bewertungen“ (collapsible), „Angebotstext“
  (collapsible, with a copy button), download buttons (PDF, DXF, GLB per piece, offer TXT) and „Zum Ergebnis & Kunde“;
- a busy indicator, which uses the latest status line, while the worker runs.

**Composer** (in the app footer): quick replies, then [Foto] [Text] [Mic] [Senden]. The quick replies depend on the state:

| State | Quick replies |
|---|---|
| preview | „Ja, so rechnen“ / „Ändern …“ |
| asking (size) | „Vorschlag nehmen: 160 cm“ |
| asking (where) | „Egal“ |
| generating | „Stopp“ / „Wie weit?“ |
| done | „Nochmal rechnen“ / „Ändern …“ / „Zum Ergebnis“ |
| need_photo | „Foto senden“ |

**Polling:** `GET /api/agent/{pid}/events?after=n` every 1.5 s while busy or generating, 5 s otherwise, and 15 s in a
hidden tab. A hidden tab gets `attention()` for new previews, results, questions and errors.

**Robustness:**
- Images are lazy-loaded.
- A pruned preview (agent.py keeps the newest 5) becomes „Bild nicht mehr gespeichert.“ instead of a broken image.
- On an order without a conversation, an intro explains what to say. The first message starts the conversation.

**„Neuer Auftrag per Foto + Sprache“:**
1. Photo: camera, or pick one; it is shrunk to 2560 px.
2. What goes in: speak (with playback and discard) and/or type.
3. Optional title.
4. „Assistent starten“ calls `POST /api/agent/start` and then opens the panel.

The screen also lists „Zuletzt mit dem Assistenten“ (`GET /api/agent`).

### Settings sections (settings-connect.js)

**Firmenprofil:**
- trade pack (radio cards, with a confirmation);
- learned suggestions (Übernehmen / Nein danke);
- company defaults (wood, handles, plinth, board);
- „Maße je Möbelart“ (collapsible per type; an empty field inherits, through `typePatch` merge patches; „Standard
  wiederherstellen“);
- own words (aliases, through merge patches with `null` for deletion);
- quality (pack / Entwurf / Fein);
- „Kundenversion automatisch bereitstellen“ (with a confirmation; it says honestly that this prepares a version and
  does not send it);
- learning on/off;
- texts for the image AI and the judge, with the honest note that these texts do not affect the images yet.

**Kanäle** (Telegram, E-Mail):
- A prominent warning card: „Nur mit Entscheidung des Inhabers“.
- Every channel shows „Aus – keine Verbindung ins Internet“.
- Switching on is refused while `issues` are open. Otherwise it needs a typed confirmation: the owner types
  „einschalten“, and the button stays disabled until then.
- „Eingaben prüfen“ always sends `enabled: false`, so it is offline only and never connects. „Verbindung testen“ exists
  only once a channel is saved as on, and it asks first.
- Secrets are write-only: the field shows „gespeichert“, an empty field keeps the value, and „Entfernen“ sends `null`.
- The refused list has a „Freigeben“ button.

**API & Webhooks:**
- Keys: list (hint, created, last used), create (the key is shown once with a copy button and a curl example), delete
  with confirmation.
- A collapsible curl/MCP example.
- Webhook master switch, with the same typed confirmation and a warning.
- Per-hook toggle, „Testen“ (disabled while off), delete, and add (events as chips, optional key binding). The secret
  is shown once.

## How to use

- Möbel step: tap „Sprechen“, say the order, tap again. Check the „Verstanden:“ card, then tap „Übernehmen“.
- Dashboard → „Foto + Sprache“ → photo, speak → „Assistent starten“. Any order → tab „Assistent“.
- Einstellungen → Firmenprofil / Kanäle (Telegram, E-Mail) / API & Webhooks.
- **Microphone on phones in the LAN:** `http://192.168.x.x:7860` is not a secure context, so the browser blocks
  getUserMedia. The mic button then opens the phone's recorder (Android) or the file picker (iPhone: record with
  Sprachmemos, then choose the file). The card explains this. Live recording works on `localhost` or over https.

## Tests and results (real numbers)

- `node --test tests/*.test.mjs`: **101 passed**, 0 failed (85 before + 16 new in `ws_system.test.mjs`).
- `node tests/ui/system.shots.mjs`: **all checks passed** at phone 390×844 and tablet 1180×820. Each run checks no
  console errors, no page errors, no horizontal scroll, tap targets ≥ 44 px, text ≥ 14 px, the primary action visible
  and no foreign requests.
  - **New order:** start without input explains what is missing; photo + text + title are sent as multipart; busy →
    preview.
  - **Chat:** a typed message is sent and the field is cleared. A fake-mic recording is sent as audio, then the
    question, the suggestion chip, generating and the result with PDF/DXF links.
  - **Möbel card:**
    - A fake-mic recording is sent to `/api/voice/transcribe`; the transcript goes to understand together with
      `active_piece`.
    - A correction before „Übernehmen“ changed the sideboard from 180 to 160, and the shelf question was answered with 140.
    - The PATCH gave 4 pieces with the right values, and the first new piece opened.
    - „Rückgängig“ returned the doc to 2 pieces.
  - **Insecure origin** (`werkstatt.lan`): the fallback hint is shown, the picker is `<input accept=audio/* capture>`,
    and the picked WAV is transcribed. No secure-only API was called (`checkSecureFallbacks`).
  - **Settings:** the profile type default is saved as `{types: {schrank: {d: 65}}}`. „Eingaben prüfen“ sends
    `enabled: false`. An incomplete channel is not switched on. A complete one opens the typed confirmation with the
    button disabled; cancelling switches nothing on. A new key is shown once. Webhooks stay off without the confirmation.
- Screenshots of every screen: `/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/system-shots/sys-*.png` (52 files). I looked at them and fixed
  what was wrong:
  - a literal „null“ rendered by native `append(null)`;
  - overlapping pack options;
  - a 41 px title field;
  - broken-image tap targets;
  - empty space in the card;
  - chat not staying at the bottom while images load;
  - underlined chip links.
- `.venv/bin/python -m pytest -q`: **675 passed, 1 skipped** (unchanged; no Python was touched).
- `tests/ui/editor.shots.mjs phone` shows 4 failures (dims prefilled, tap overlay text, drag, remove hint). They are
  **old**: a copy with the pre-change frontend fails the same 4. The walk predates the elev field and the newer
  step-3 hint texts.

### Live check against flux-studio (real Qwen, real Whisper), script `/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/frontend-live/live.mjs`

- Setup: a throwaway order „TEST Frontend Sprache“ with its own uploaded copy of evalset room `20261018-193206` and
  its stored geometry. It was purged at the end (`files: 2`).
- Dashboard, `#/assistent`, the 3 settings sections, and the kept demo order's conversation (read-only, nothing sent):
  34 events, all 15 images load, no console errors, no horizontal scroll. Run at phone size on `127.0.0.1` and at tablet
  size on `http://192.168.178.111:7860`.
- **Möbel card, typed** (phone, localhost: record mode). Input: „Unter das Fenster ein Sideboard in Eiche, eins achtzig
  breit, grifflos. Links neben die Tür ein Wandregal in Weißlack.“
  - understand took 10.0 s;
  - the card showed the sideboard 180 × 75 × 45 Eiche and the Wandregal with the width question (suggestion 120);
  - after „Übernehmen“ the order had `sideboard 180/75/45 eiche push „unter das Fenster“` and
    `wandregal 120/40/25 weiss push „links neben die Tür“`.
- **Möbel card, voice over the LAN** (tablet, http: file mode). The English system clip
  `/usr/share/sounds/speech-dispatcher/dummy-message.wav` was picked. Transcription plus understanding took 14.1 s
  with Whisper warm. The transcript was correct, and the card answered „Kein Möbel erkannt. Was soll gebaut oder
  geändert werden?“ with „Übernehmen“ blocked. Correct behaviour. Whisper was unloaded afterwards.
- Real data: the md5 of every `projects/workshop/*.json` and of `settings.json` is unchanged. No `profile.json`,
  `channels.json`, `apikeys.json` or `webhooks.json` was created; every settings write was tested only against mocks.
  Nothing was switched on, and nothing contacted the internet.

## Known limits

- **German speech is still untested live** (no TTS on the machine); only the English clip went through the real
  pipeline. Live MediaRecorder capture on a real phone was only tested with Chromium's fake microphone. On the http LAN
  the file fallback is the normal path, and on iPhone it means recording in Sprachmemos first (no live mic without
  https).
- **The active piece is not used by the server yet.** The card sends `active_piece`, but `/api/workshop/{pid}/understand`
  ignores it. A bare correction such as „achtzig tief“ after „Übernehmen“ therefore goes to the last piece, not the
  open one (see Integration).
- „Übernehmen“ applies understand's ops as they were computed. If the order changed in between, the PATCH conflict
  handling of the store applies, but the ops are not recomputed.
- intent gives the open Wandregal „grifflos (Push)“ handles in the live run. That is an intent/profile default (also
  noted in agent.md); the card only displays it.
- Undo of an „Übernehmen“ that dropped pieces while 4 pieces exist can hit MAX_PIECES.
- The Assistent panel polls; there is no push. Many open tabs mean many polls (5 s each while idle).
- The texts for the image AI and the judge are stored but not used by generation yet; the page says so.
- The phone order tabs now hold 3 entries, and „Kunde & Auftrag“ wraps to two lines at 390 px.
- `tests/ui/editor.shots.mjs` is stale (4 old failures, see above) and was not updated in this step.

## Integration

The frontend changes are applied: static files take effect without a restart, and no server change is needed for them.
One optional server hook makes corrections go to the piece that is open in the Möbel step. In
`intent.py`, `understand_route`, replace

```python
    doc = projects.load("workshop", pid)
    res = understand(text, doc, _profile())
```

with

```python
    doc = projects.load("workshop", pid)
    ap = body.get("active_piece")
    if isinstance(ap, str) and any(p.get("id") == ap for p in doc.get("pieces") or []):
        doc = {**doc, "active_piece": ap}   # the piece open in the Möbel step gets bare corrections
    res = understand(text, doc, _profile())
```

`understand()` already reads `doc["active_piece"]`, which is how agent.py uses it. No other shared file needs a change.
