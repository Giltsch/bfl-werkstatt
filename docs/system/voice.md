# voice.py: speech to text (Phase 1)

Status: built and tested on 2026-10-18. Not wired into `server.py` yet (see Integration). Nothing is public: no new port,
and the model loads from the local HF cache only (`local_files_only=True`).

## What it does

- `voice.decode(data, max_seconds=600) -> np.ndarray` decodes any container (webm/opus, ogg/opus, m4a/aac, wav, mp3,
  and stereo or 44.1/48 kHz input) to float32 mono 16 kHz with an ffmpeg subprocess. It writes a temp file instead of
  using a pipe, because iPhone m4a keeps its index at the end of the file and ffmpeg cannot read that from stdin. It
  raises `ValueError` for empty or unreadable input. soundfile is not used.
- `voice.transcribe(data, lang="de", prompt=None) -> dict` returns
  `{"status": "ok"|"error"|"unavailable", "text", "language", "duration", "seconds"}`, plus a German `"message"` when
  the status is not ok or nothing was heard (and `"detail"` for internal errors). It never raises.
  - Model: `openai/whisper-large-v3-turbo` through transformers (`AutoModelForSpeechSeq2Seq` + `AutoProcessor`) on CUDA
    in fp16. It loads on first use. One lock serialises loading, inference and unloading, so it is thread-safe.
  - Long audio: `windows()` cuts the audio into windows of at most 30 s. Each cut lands on the quietest 100 ms in the
    window's last 6 s, so words are not split. The windows are decoded in batches of 8, so a 2-minute memo takes one
    `generate()` call.
  - Silence: windows whose 95th-percentile 30 ms RMS is below 0.002 (about -54 dBFS) are skipped and never reach the
    model, because Whisper invents text on silence.
  - Domain prompt `PROMPT_DE`: used for `lang="de"` or auto-detect. It covers carpenter vocabulary (Weißlack, grifflos,
    Korpus, Push-to-open, Sockelblende, Schubkästen, Dachschräge …) and asks for digits in dimensions. `prompt=""`
    turns it off, and any other string replaces it. `lang=""` means auto-detect, reported as `"language": "auto"`.
  - Anti-hallucination:
    1. A window that comes back as a repetition loop (gzip ratio > 2.4, Whisper's own test) is decoded again without the
       prompt. This happened live: a 440 Hz tone produced "Dachschräge, Wandboard, …" 20 times.
    2. `clean()` collapses phrases repeated 3 or more times in a row and drops the known subtitle hallucinations
       ("Untertitel im Auftrag des ZDF", "Vielen Dank fürs Zuschauen" …).
    3. `clean()` also drops a verbatim echo of a full prompt sentence (≥ 20 characters). A single word such as
       "Weißlack." is kept.
  - Memory guard: before loading, it waits up to 30 s for `judge.mem_available_gb() >= judge.MIN_FREE_GB + 3`, then
    returns `unavailable` ("Gerade ist zu wenig Speicher frei …").
  - Idle unload: a daemon thread calls `idle_check()` every 30 s and unloads after `IDLE_UNLOAD = 600` s without use.
    It never unloads while an inference holds the lock. `loaded()` and `unload()` are public.
- Evaluation samples: `save_sample(audio, text, photo, who, lang, note)` writes:
  - `evalset/audio/<id>.<ext>`: the audio bytes unchanged. The extension comes from the magic bytes: webm, ogg, m4a, wav
    or mp3. Safari records mp4, so a fixed `.webm` would be wrong.
  - `<id>.jpg`: the optional photo, re-encoded as JPEG with a long side of at most 2048 and **without EXIF**, because
    phone photos carry the GPS position of the customer's home.
  - `<id>.json`, written atomically: `{id, at, audio, photo, duration, silent, language, text (what the speaker meant),
    note, who, consent: true}`.

  Ids look like `YYYYMMDD-HHMMSS-xxxxxx`. `list_samples()` and `delete_sample(id)` validate the id format, so no path
  traversal is possible.

### Routes (`voice.router`)

| Route | Purpose |
|---|---|
| `GET /api/voice/status` | `{loaded, model, ffmpeg, load_seconds, idle_s, unload_after_s, mem_free_gb, max_seconds, max_mb}` |
| `POST /api/voice/transcribe` | multipart `audio` (≤ 25 MB), optional form `lang` (default `de`). Returns the result dict. Unreadable audio → 400 "Die Aufnahme konnte nicht gelesen werden.", no model or no memory → 503 with the German message, too large → 413. Silence → 200 with `text: ""` and `message: "Keine Sprache erkannt."` |
| `POST /api/voice/sample` | multipart `audio` (≤ 25 MB), `consent` (required: 1/true/on/ja), optional `text`, `note`, `lang`, `photo` (≤ 40 MB). Returns the stored meta. 400 without consent, for an unknown format, for audio < 0.5 s or for a broken photo. `who` comes from `request.state.owner_name` (team middleware) |
| `GET /api/voice/samples` | `{count, minutes, recent: [10 newest]}` |
| `DELETE /api/voice/sample/{id}` | withdraws a sample: deletes the audio, the photo and the json |

### Recorder page `static/aufnahme.html`

The page is in German, mobile-first and self-contained (no imports, light and dark mode).
- A yellow note says that the page is only for test data, that recordings stay on the workshop computer in the LAN, and
  that nobody should record customer names, addresses or phone numbers or use photos of people.
- Steps:
  1. Record with MediaRecorder (opus/webm, ogg or mp4, whichever the browser supports; at most 5 minutes; timer).
  2. Enter the intended text (optional) and a note.
  3. Attach a photo (optional).
  4. Tick the consent checkbox. "Probe speichern" stays disabled until it is ticked.
- "Erkennung testen" shows what Whisper understood. It does not fill in the intended text, so the eval labels stay
  unbiased.
- The list shows the stored samples. Samples made in this browser session can be deleted.
- **The main limit:** browsers allow the microphone only in a secure context (HTTPS or localhost). On
  `http://192.168.x.x:7860` the page detects this and offers a file picker instead (`accept="audio/*" capture`). Many
  Android phones open the recorder directly from it. On an iPhone, record with the Sprachmemos app and choose the m4a
  file. HTTPS on the LAN would need a certificate, and nothing about the serving setup was changed.

## Tests and live results

`.venv/bin/python -m pytest -q tests/test_voice.py` → **36 passed** (≈ 4-6 s, no GPU, no network, tmp evalset).
The tests cover:
- decoding all 5 containers, stereo 44.1 kHz input, garbage and empty input, the length cap
- windows (2-minute audio: contiguous windows of ≤ 30 s with every cut in a pause; audio without pauses)
- the silence detector, `clean`, `looping`
- `transcribe` with a fake Whisper: prompt choice, the 2-minute memo in 4 windows with the silent window skipped,
  silence never reaching the model, the loop retry without prompt, bad audio → error, low memory → unavailable after
  the wait, a broken model → unavailable, 4 parallel calls never overlapping
- idle unload (not while busy)
- the routes: 200/400/413/422/503
- samples: consent required, real container kept, photo downscaled with EXIF/GPS removed, list and delete, id validation

Live check on the Spark (2026-10-18, flux-studio and the judge were running, GPU at 96 % utilisation from other jobs):

- **No local TTS** exists (no espeak-ng, espeak, pico2wave or piper), so there is **no German speech test yet**. Test
  audio came from system files: `/usr/share/sounds/speech-dispatcher/dummy-message.wav` (29.4 s of English speech) and
  llama.cpp's `test-2.mp3` (17.4 s of English speech), re-encoded to webm/opus, m4a and mp3. A 120.7 s webm was built
  from 4 × the 29 s clip with 1 s gaps. A 10 s 440 Hz tone and 10 s of silence were added.
- Transcripts were correct in every container. The 120 s memo came back as 1589 characters (all 4 repetitions).
  Silence → `""` in 0.07 s without the model. Tone → `""` after the loop retry (5.6 s).
- **Load time: 21-23 s.** Measured breakdown: torch + CUDA init 1.9 s, transformers import 3.1 s, reading the weights
  0.2 s, and **copying to the GPU 17 s**. A plain 1.6 GB `torch.empty(...).cuda()` copy also took 10.9 s while the GPU
  was this busy, so the cost comes from the shared GPU and unified memory, not from the code. The first request after
  an idle unload pays this once.
- **Memory:** GPU allocated 1.54 GB, peak 1.77 GB during the 120 s batch. Process RSS 1.8 GB (unified memory, so this
  is the same memory). MemAvailable dropped from 36.6 to about 34 GB.
- **Speed (warm):**

  | Audio | Wall time | Per audio minute |
  |---|---|---|
  | 29 s clip | 1.15-1.9 s | 2.3-3.9 s |
  | 17 s mp3 | 1.7 s | 5.9 s |
  | 120 s memo | 2.4-3.2 s | 1.2-1.6 s |

  Short clips cost more per minute because each window is padded to 30 s.
- Route on the real model (TestClient, no port, cold): 200 in 22.2 s including the load.
- `unload()` at the end: allocated memory back to 0.03 GB and MemAvailable back to 36 GB. **The model is not loaded
  anymore.**
- Recorder page in headless Chromium (phone viewport 390 px, every request mocked through `page.route`, no server):
  - Over `http` it shows the fallback file picker.
  - Over `https` with a fake microphone: recorded 2.5 s, Save stayed disabled until consent was ticked, saving worked,
    and the list showed the sample with a delete button.
  - No JS errors and no horizontal scrolling.
  - The real multipart body Chrome sent (MediaRecorder webm without a duration header) was replayed into the real
    `/api/voice/sample` route: 200, `<id>.webm` + `<id>.json` written, duration 2.46 s.

## Known limits

- Accuracy on German carpenter speech (dialect, workshop noise, spoken numbers like "zwo-zwanzig") is **unmeasured**:
  there is no German audio and no TTS. The first real numbers will come from samples recorded with
  `static/aufnahme.html`. A WER script over `evalset/audio/*.json` (the intended `text` vs `transcribe()`) is a small
  follow-up.
- Whisper writes numbers in varying styles ("2,20 m", "zwei zwanzig", "220"). The prompt pushes towards digits.
  `intent.normalize_de` has to handle all three.
- No live microphone over plain HTTP on the LAN (browser rule). The page falls back to file upload.
- The first request after an idle unload waits about 20 s for the load while the GPU is busy. If that is too slow,
  `IDLE_UNLOAD` can be raised; the model holds 1.6 GB.
- One transcription at a time (shared lock). A 2-minute memo blocks other voice requests for about 3 s.
- `decode()` caps audio at 10 min. `save_sample` stores the full upload (≤ 25 MB) but measures at most 10 min.
- transformers prints deprecation notes about `max_length` and the generation config on each call. They are harmless.
- Note for the integrator: `profile.py` (another module) shadows the stdlib `profile` that torch._dynamo, and so
  transformers' generation code, imports through cProfile. During this build that broke `AutoModelForSpeechSeq2Seq`
  once. The current `profile.py` contains a shim that loads the stdlib module, and with it the transformers imports work
  again (verified). If that shim is ever removed, voice and SAM will turn "unavailable". Renaming the module (e.g.
  `company_profile.py`) would be the clean fix.

## Integration (exact code for shared files)

`server.py`: import and router:

```python
import voice
```
```python
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, voice.router):
    app.include_router(r)
```

`server.py`: optional page route (the page also works as `/static/aufnahme.html`, because that path is already
served):

```python
@app.get("/aufnahme")
def recorder_page():
    return FileResponse(HERE / "static" / "aufnahme.html", headers={"Cache-Control": "no-cache"})
```

`server.py`: show and unload the model like the other models:

```python
# in status(): "loaded": {...}
"loaded": {**engine.loaded(), "pup": pup.get("state") == "up", "depth": geometry_api.depth_loaded(),
           "voice": voice.loaded()},
```
```python
# in unload(what):
    elif what == "voice":
        voice.unload()
```

Optional, so an empty evalset folder exists for backups and eval tools (`voice` creates it on the first sample anyway):

```python
(DATA / "evalset" / "audio").mkdir(parents=True, exist_ok=True)
```

No change is needed in `team.py`: the routes sit behind the team login, and `MAX_BODY` (200 MB) is far above the
route limits. The agent and channels call `voice.transcribe(bytes, lang)` directly.
