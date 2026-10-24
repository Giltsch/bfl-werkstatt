# Review fixes (fixer pass, 2026-10-19)

## What I did

I fixed 12 verified review findings: 11 completely, and 1 only in part (finding 10b, the grounding, is still open).
Every fix is a small change with a regression test.
Before editing, I copied the original files to
`/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/fixer-bk/`.

| # | Finding | Result | Where |
|---|---------|--------|-------|
| 1 | The channel `/check` body could retarget the stored mail password and go online although the channel was disabled | **fixed** | `channels/__init__.py` (check, merge), `channels/base.py` + `channels/mail.py` (`TARGETS`) |
| 2 | Webhook SSRF (loopback, LAN, metadata) | **fixed** | `connect.py` (`_check_url`, `_target`, `_allowed_ip`, `_own_ips`, `lan` opt-in) |
| 3 | Without a team password, keys, webhooks and channels were open to every LAN device | **fixed** | `connect.py`, `channels/__init__.py`, `static/ws/settings-connect.js` |
| 4 | ffmpeg followed ffconcat/HLS playlists from uploaded "audio" | **fixed** | `voice.py` `decode()` |
| 5 | A message during generation broke the watcher, so no result was ever sent (high) | **fixed** | `agent.py` `_poll`, `_on_finished`, `_on_input`, `recover` |
| 6 | Double full stop after the ETA („Min..“) | **fixed** | `agent.py` `_go` |
| 7 | Flaky `test_judge_unavailable_no_unit` | **fixed** | `tests/test_jobs.py` |
| 8 | A correction in the Möbel card went to the wrong piece (`active_piece` was ignored) | **fixed** | `intent.py` `understand_route` (snippet from frontend.md applied) |
| 9 | On the phone, the state badge overlapped the help button | **fixed** | `static/ws/assistent.js` CSS |
| 10 | A plan-check conflict (window, door, radiator) still showed „passt das? / Ja“ as the main answer | **fixed** (UX part); grounding part **deferred** | `agent.py` `_preview`, `_on_text`; `assistent.js` `quickReplies` |
| 11 | No busy indicator during the 2–3 min generation | **fixed** | `assistent.js` `drawTyping` |
| 12 | Customer offer text contained the spoken phrase „Lage: rechts neben den Fernseher an die Wand“ | **fixed** (Lage line dropped) | `offer.py` |

### Details

**5 – agent watcher (high).**
- Root cause: `_poll` returned True for every state other than `generating`. Understanding or transcribing a message
  takes longer than `WATCH_S`, so the watcher took it for the end of the job.
- Fix: `_poll` now keeps following the jobs while the state is `generating` **or** a busy state (`analyzing`,
  `understanding`), and only the jobs decide when the generation is over.
- If the job finishes while the worker is handling a message, the "finished" item is queued behind that message, so it
  runs once the state is back on `generating`.
- `_on_finished` checks the jobs again before it reports „keine Bilder“, and starts watching again if they are still
  running.
- A new photo sent during a generation now stops that generation („… abgebrochen, weil ein neues Foto kam“), because
  those images would show the old photo.
- `recover()` starts watching again when a restart interrupted a message that was sent during a generation.
- Tests: `test_chatter_during_generation_keeps_watching`, `test_voice_during_generation_keeps_watching`,
  `test_job_finishing_while_understanding_still_delivers`, `test_new_photo_during_generation_cancels`. Against the old
  `agent.py`, three of the four fail (checked in a shadow tree). The fourth, the job finishing while a message is being
  understood, passed even with the old code, so it guards the new path but does not show the bug.

**10 – plan-check conflict.**
- When the KI-Plancheck reports a problem with a fix, the preview no longer says „So geplant – passt das?“. It starts
  with „So passt es noch nicht:“, then the problem and the suggestion, then the plan.
- The event data carries `conflict: {piece, n, fix, send}`.
- The chat offers **„Vorschlag übernehmen“** (primary), then „Ja, trotzdem rechnen“ and „Ändern …“.
- Answering „Vorschlag“ / „Vorschlag übernehmen“ in any channel (Telegram and mail too) runs the suggestion text through
  understanding as a correction of that piece. With several pieces, the piece label and number go first.
- „ja, trotzdem rechnen“ still generates („trotzdem“/„dennoch“ count as yes words).
- **Deferred:** grounding „neben dem Fenster“ onto the window itself (ground/agent `_clear_of`). It needs a live
  investigation on the evalset room with the VLM. The conflict UX now stops the 3-minute wrong generation, but the
  first placement is still wrong in that case.

**2/3 – connectivity.** See `connect.md` and `channels.md` → „Security fixes after review“.

**4 – voice.**
- `decode()` sniffs the container by magic bytes (webm/ogg/m4a/wav/mp3, plus flac) and refuses anything else.
- It forces the demuxer (`-f matroska|ogg|mov|wav|mp3|flac`) with `-protocol_whitelist file`, and writes the input into
  a private `TemporaryDirectory` instead of the shared /tmp.
- Test `test_decode_never_follows_playlists` fails against the old code.

## Test results (real numbers)

- Full Python suite: `.venv/bin/python -m pytest -q tests/` → **689 passed, 1 skipped** (105 s).
  - I changed only `static/ws/assistent.js` after this run. Its node tests pass (below).
  - New and updated tests: agent +5, connect +5, channels +2 (and 2 rewritten), voice +1, intent +1, integration (keys
    and webhooks give 409 without a password).
- Node: `w3d` 19, `ws_editor` 16, `ws_orders` 11, `ws_results` 15, `ws_store` 24, `ws_system` 16 → **101 pass, 0
  fail**.
  - `tests/place_xcheck.mjs` is a harness that `test_place.py` runs, not a test file of its own.

## Live checks

- **No flux-studio restart.** The permission system refused `systemctl --user restart flux-studio`, so I did not try
  any other way. **The running server (pid 1680306) still runs the old code.** The owner has to restart it to activate
  these fixes.
- **Intent on the real Qwen (vLLM :8002), the reviewer's sequence.** „nein, nur dreißig tief“ with a cabinet and a
  Wandregal:
  - no active piece → `pieces.p1schr.d = 30`;
  - `active_piece = p2regal` → `pieces.p2regal.d = 30`;
  - `active_piece = p1schr` → `pieces.p1schr.d = 30`.
  - Each call took about 5.1 s.
- **Offline security probe** (tmp store, fake IMAP, no DNS, no network): `scratchpad/fixer_probe4.py`.
  - A retargeting check body: online `None`, no IMAP connection at all.
  - PUT with a new host: the stored password is dropped.
  - Webhooks to 127.0.0.1:8002, 169.254.169.254, [::1]:7861 and the machine's own LAN IP: 400. The own LAN IP is
    refused even with `lan: true`.
  - The reviewer's `sec/probe3.py` (no password set): channel check `None`, nothing contacted, webhooks 409.
- **UI, Playwright on the repo's own mock harness** (`tests/ui/check.mjs` + `mock_api.mjs`, ephemeral 127.0.0.1 static
  server; no real API): `scratchpad/fixer_shots.mjs`.
  - Phone 390 px and tablet: badge and help button no longer overlap. On the phone the badge now ends at x=167 and the
    button starts at x=175; before the fix about 48 px overlapped.
  - Generating: the indeterminate bar shows „Bilder werden gerechnet …“. It does not repeat the status line that is
    already in the log.
  - Conflict preview: chips are „Vorschlag übernehmen“ (primary), „Ja, trotzdem rechnen“, „Ändern …“.
  - No layout problems, no page errors, no horizontal page scroll.
  - Screenshots: `/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad/fixer-shots/`.
  - The chip row scrolls sideways on the phone, so „Ändern …“ sits off-screen. This is the existing design, the same as
    in other states.

## Incident (honest note)

One draft of `test_enabling_needs_a_team_password` called `monkeypatch.undo()`, which also undid the tmp-store patch.
For one run it wrote to the real `projects/` folder:
- It created `projects/channels.json` with the fake test token (telegram `enabled: true`, chat 42).
- It rewrote `projects/settings.json`.

What I found and did:
- `settings.json` is **byte-identical** to the copy in `bfl-backups/code-pre-system-20261018.tar.gz` (checked with
  `cmp`): the invalid test hash was normalised away and the epoch was not changed. Only its modification time changed.
- No channel was started: it would have needed the new password guard, and the live server never re-read the file.
- I moved the leaked `channels.json` to `scratchpad/fixer-restore/channels.json.testleak`. Without the file the
  defaults apply (all channels off). Nothing else in `projects/` or `outputs/` changed.
- The test now keeps the tmp store and asserts that `projects.STORE` lies inside the tmp dir.

## Known limits

- Finding 10b (grounding next to a window) is still open, see above.
- The webhook DNS pin connects to the first resolved address. All resolved addresses must pass the check. Userinfo in a
  hook URL is dropped by the pin.
- Channels or webhooks that are already running keep running until the next restart when the team password is removed.
  At start and on every delivery, nothing goes out without a password.
- Tailscale/CGNAT (100.64/10) receivers are refused even with `lan: true`.

## Integration

All edits are already in place in the shared files (fixer role): `agent.py`, `connect.py`, `channels/*`, `voice.py`,
`intent.py`, `offer.py`, `static/ws/assistent.js`, `static/ws/settings-connect.js`, and the tests. Nothing else needs to
be hooked in. Only this remains:

```bash
systemctl --user restart flux-studio   # activates the fixes; was refused by the permission system in this session
```
