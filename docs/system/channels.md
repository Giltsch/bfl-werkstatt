# channels/: Telegram and e-mail front doors

Status: 2026-10-18. Built and unit-tested with fake transports only. **Nothing has been connected to Telegram or any mail
server**, and nothing is hooked into `server.py` yet. Every channel is **off by default**.

## What was built

| File | Contents |
|---|---|
| `channels/__init__.py` | Config `projects/channels.json` (mode 600), router `GET/PUT /api/channels`, `POST /api/channels/{id}/check`, `start_all(agent)`, `stop_all()`, `status()` |
| `channels/base.py` | `Channel` base class (poll loop with exponential backoff, outbox sender thread, catch-up sweep, dedupe by event `n`, per-channel state file with mode 600, refusal log), `AgentAdapter` (the agent contract + safe file reads under `outputs/`), `render(event)` → text/photos/documents, `split_text`, `scrub` (removes secrets from error text) |
| `channels/telegram.py` | `TelegramChannel`: Bot API over httpx long polling (`getUpdates` with offset and timeout), `sendMessage` / `sendPhoto` / `sendDocument`, `getFile` + download for photos (also images sent "as file") and voice notes/audio; chat allowlist; `/start`, `/hilfe`, `/neu`, `/status`; chat ↔ order mapping and offset persisted |
| `channels/mail.py` | `MailChannel`: IMAP4_SSL polling of one folder (UNSEEN, fetched with `BODY.PEEK[]`), sender allowlist, photo + text body + audio attachment → agent, SMTP (SSL or STARTTLS) replies with images/PDFs attached, mails marked `\Seen` and optionally moved |
| `tests/test_channels.py` | 25 tests, all with fake transports and a guard that fails any real socket connection |

Outbound connections only: no webhook, no new port, no listener. Telegram long polling is an outgoing HTTPS request to
api.telegram.org; mail uses outgoing IMAP/SMTP.

## How it works

### Config: `projects/channels.json` (mode 600)

```json
{"telegram": {"enabled": false, "token": "", "allowed_chats": [{"id": 123456, "name": "Peter", "who": ""}],
              "poll_timeout": 25, "who": ""},
 "mail": {"enabled": false, "imap_host": "", "imap_port": 993, "smtp_host": "", "smtp_port": 465,
          "smtp_security": "ssl", "user": "", "password": "", "from_addr": "", "folder": "INBOX",
          "processed_folder": "", "allowed_senders": ["kunde@example.de", "@schreinerei.de"], "poll_s": 60, "who": ""}}
```

- Secrets: `telegram.token` and `mail.password`. **GET never returns them.** It shows `{"set": true|false}` instead.
  In a PUT, a missing key, `""` or the echoed `{"set": …}` keeps the stored value, a non-empty string replaces it, and
  `null` clears it.
- `normalize()` is strict: host names are limited to `[a-z0-9.-]`, ports must be 1–65535, chat ids are ints (negative
  ids are groups), senders must be a full address or `@domain.tld`, poll intervals are clamped (Telegram 5–50 s,
  mail 30–3600 s).
- `validate()` (offline, German messages): Telegram needs a token in the right format and at least one allowed chat.
  Mail needs hosts, user, password, a valid sender address and at least one allowed sender. The channel's own address
  must not be on the allowlist (that would make a mail loop). Port 587 together with SSL gets a warning.
- **An invalid config cannot be switched on** (PUT returns 400). `start_all` also skips invalid or disabled channels.
- `who` is the string passed to `agent.start/handle` as `who`. The order is: the chat entry's `who` (Telegram), then the
  channel's `who`, then `"telegram"` or `"mail"`.

### API (team routes, behind the existing team middleware)

- `GET /api/channels` → `{"channels": {"telegram": {...config without secrets, "label", "issues": [...], "status":
  {...}}, "mail": {...}}, "warning": "Ein eingeschalteter Kanal verbindet die Werkstatt mit dem Internet …"}`.
  `status` contains `running`, `since`, `last_poll`, `last_error`, `fatal`, `received`, `sent`, `orders` and `refused`
  (the last 20 unknown chats or senders with name, time and count). The owner can add a refused chat id to the
  allowlist from there. `refused` is shown even while the channel is off.
- `PUT /api/channels` with `{"telegram": {partial}, "mail": {partial}}` merges, validates and saves. Unknown keys such as
  `label`, `issues` and `status` are ignored, so the UI can send a channel view back. Once `start_all` has run (that is,
  in the server), a changed channel is restarted: stopped, then started again if it is enabled and valid. In the tests
  and before integration, nothing starts.
- `POST /api/channels/{id}/check` with an optional partial body (merged, **never saved**) returns `{"ok", "issues",
  "online", "enabled", "note"}`. The check contacts the service **only if the effective config has `enabled: true`** and
  passes offline validation:
  - Telegram calls `getMe` and returns the bot's username.
  - Mail does an IMAP login and a read-only select, then an SMTP login. No mail is read or sent.
  - Otherwise `online` is `null` and `note` says "Nur offline geprüft".

### Telegram flow

1. `getUpdates(offset, timeout=poll_timeout, allowed_updates=["message"])` runs in a loop. The offset only advances
   **after** an update has been processed and is persisted after each one. After a crash or a stop, unprocessed updates
   come again.
2. **Unknown chat:** the first time, it gets one polite refusal (`REFUSAL`). It is logged in `state.refused` with name,
   @username, type and a short text, and printed to the server log. It is never served. Later messages from that chat
   are counted but get no reply.
3. **Allowed chat:** commands are `/start`, `/hilfe` and unknown commands (help text), `/neu` (the next message starts a
   new order) and `/status` (the last agent text of the current order). Photos use the largest size; an image sent as a
   document is taken at full resolution. A voice note or audio is downloaded through `getFile` (up to 20 MB). Caption or
   text goes along too.
   - If the chat has no current order: `agent.start(photo, who, "telegram", text=…, audio=…)`. The chat is mapped to the
     new pid, and events that already exist are swept immediately.
   - Otherwise: `agent.handle(pid, …)`. The events it returns are delivered too, deduplicated.
   - If the agent raises an exception, the chat gets the German `detail` of an `HTTPException`, or else a generic line.
     Internal details never reach the chat.
4. **Outgoing:** only events with `role == "agent"` and `kind` in text/question/preview/result/status/error.
   - A single image with short text is sent as `sendPhoto` with a caption.
   - Otherwise the text goes as `sendMessage` (split at 4000 characters), then the photos.
   - Files go as `sendDocument`. Images over 10 MB are sent as documents.
   - If a chat was removed from the allowlist, its orders get nothing more.
5. **Errors:** network errors and HTTP 5xx use exponential backoff from 1 s up to 300 s. HTTP 429 waits for
   `retry_after`. HTTP 409 (a webhook is set, or another poller uses the token) waits 30 s. HTTP 400 or 403 on a send
   drops that one message. 401/404 is **fatal**: the channel stops, and `status.fatal` says "Token prüfen". The token is
   removed from every error text (httpx errors can contain the URL).
6. **Stop:** `stop()` sets the flag, wakes the sender and joins for at most `timeout` seconds. A long poll still in
   flight is abandoned as a daemon thread. When it returns, it sees the stop flag and neither processes anything nor
   moves the offset (tested).

### Mail flow

1. Every `poll_s` seconds: IMAP4_SSL with certificate verification, login, select the folder, check `UIDVALIDITY`
   (if it changed, the list of seen UIDs is reset), `UID SEARCH UNSEEN`, at most 10 mails per poll, each fetched with
   `BODY.PEEK[]` so it is not marked read.
2. Skipped and left unread: mails with several or no From addresses, mails from our own address, auto mails
   (`Auto-Submitted` not `no`, `Precedence: bulk/list/junk/auto_reply`, `List-Id`, `X-Autoreply`, empty
   `Return-Path`), and senders not on the allowlist. Unknown senders are logged once per UID. They get **no** reply
   (no backscatter).
3. Allowed mails: the text body is taken from the plain part, or from the HTML with tags removed. Quotes (`>`), the
   signature (`-- `) and the quoted history ("Am … schrieb …:", "On … wrote:", "Von:", "-----Original…") are cut off.
   Then the first image attachment (inline images count) and the first audio attachment (`audio/*` or a known audio
   extension such as .m4a/.ogg/.mp3/.wav) are taken.
4. Which order a mail belongs to:
   - a `[W-<pid>]` tag in the subject, or a known Message-ID in `In-Reply-To` / `References`;
   - in both cases **only if the order was started by the same sender**;
   - the subject or first line "neu" forces a new order;
   - otherwise `agent.start`.
5. Processed mails get `+FLAGS (\Seen)` and are moved to `processed_folder` if one is set. That uses `UID MOVE` if the
   server supports it, otherwise COPY plus `\Deleted`. There is deliberately no EXPUNGE, because it would also purge
   messages the owner deleted.
6. **Outgoing:** events are collected for 8 s (`coalesce_s`) and sent as **one** reply per order:
   - Subject `Re: <original subject> [W-<pid>]`, with `In-Reply-To` and `References` set.
   - `Auto-Submitted: auto-replied` is set so other robots do not answer.
   - Images and PDFs are attached, up to 20 MB in total; anything larger is listed as "zu groß".
   - SMTP authentication errors are fatal. Other SMTP errors are retried 3 times with backoff.

### Agent contract used (code against SPEC agent.py)

`channels.start_all(agent)` wraps the given object in `AgentAdapter`. The agent needs:

- `agent.start(photo: bytes | None, who, channel, text=None, audio=None) -> pid`
- `agent.handle(pid, who, channel, text=None, audio=None, photo=None) -> list[event]`
- `agent.events(pid, after=0) -> list[event]`, where events have `n`, `role` and `kind`, plus optionally `text`, `image`
  and `data`.
- `agent.subscribe(cb)` (optional). The callback is called as `cb(pid, event)`; `cb(event)` with `event["pid"]` is
  accepted too. It may return an unsubscribe callable. **Without subscribe, channels still work**: a sweep thread
  polls `events(pid, after=last_n)` every 5 s for followed orders (orders older than 14 days without traffic are no
  longer followed). Subscription and sweep together send each event once, deduplicated by `n` (tested).
- Images and files in events: `event["image"]` and `data.images` / `data.files` / `data.pdf` / `data.zip`. Each can be
  a string or a `{"path"|"image"|"file"|"url": …}` dict, or a list of these.
  - Paths are relative to `/outputs`, with or without the `/outputs/` prefix, or absolute inside `outputs/`. Anything
    outside `outputs/` is refused (path traversal is tested).
  - `http(s)://` values are sent as links.
  - `data.offer_text` is appended to result texts, and `data.links` are sent as links.
  - Optional: `agent.file_bytes(ref) -> (name, bytes) | None` for other references, such as a rendered
    `preview.png` that is not stored under outputs.

## Tests

```
.venv/bin/python -m pytest -q tests/test_channels.py      → 25 passed (2.2 s; 5 repeated runs, all green)
.venv/bin/python -m pytest -q tests/                      → 421 passed, 1 skipped (full suite, no interference)
```

- An autouse fixture replaces `socket.socket.connect`, `socket.create_connection` and `socket.getaddrinfo` with a
  function that fails. A scratch test confirmed that a real `httpx.get("https://api.telegram.org/")` and
  `imaplib.IMAP4_SSL(...)` both fail under it.
- Telegram is tested with `httpx.MockTransport`, which simulates the Bot API.
- IMAP is tested with a fake `IMAP4_SSL` (login, select, UIDVALIDITY, `UID SEARCH/FETCH/STORE/MOVE`). SMTP is tested
  with a fake `SMTP_SSL`, and plain `SMTP` asserts that it is never used with SSL configs.

What the tests cover:

- **Router:**
  - defaults are off and secrets are hidden;
  - GET never writes;
  - secrets are write-only (keep with `""` or the echoed view, clear with `null`);
  - `channels.json` has mode 600;
  - enabling an invalid config is rejected, and so is the mail-loop config;
  - unknown channels get 400/404;
  - the check is offline while disabled (a client factory that would fail if called proves it), online only when
    enabled, and never saves;
  - `start_all` starts no threads by default;
  - end to end: PUT `enabled` after `start_all` starts the channel; message → agent → subscription → Telegram, delivered
    exactly once; disabling stops it; `stop_all` unsubscribes.
- **Telegram:**
  - an unknown chat gets one refusal, is logged with a count, and the agent is never called;
  - the largest photo size plus the caption reach `start`; a voice note reaches `handle`;
  - the mapping and offset are persisted (mode 600) and resumed after a restart;
  - `/start`, `/status` (also `/status@bot`), `/neu`;
  - agent errors give the German detail or the generic line;
  - outgoing: preview as a photo with caption, result photo with offer text and link, PDF as a document;
    deduplication; user events are not sent; a chat removed from the allowlist gets nothing;
  - backoff after ConnectError, 500 and 429, then recovery; the token never appears in the status;
  - 401 is fatal and stops the channel;
  - stop takes under 1.5 s during a blocked long poll, and the late poll result is not processed.
- **Mail:**
  - an allowlisted mail with photo, text and .m4a reaches `start` and is then `\Seen` and moved;
  - spam and auto-replied mails stay unread, are logged once and are not processed twice;
  - a reply with the subject tag and a quoted history continues the order (the quote is cut off);
  - another sender cannot take over an order through the tag;
  - "neu" starts a new order;
  - events go out as one reply with subject tag, In-Reply-To, Auto-Submitted and the PNG and PDF attachments;
  - a reply found only through In-Reply-To continues the order;
  - a wrong password is fatal;
  - the online check runs only when enabled, and the password never appears in responses;
  - the threaded sender sends two events as one mail;
  - unit tests for `body_text`, `split_text`, `render` and strict `normalize`.

**Live checks: none, on purpose.** Following hard rule 2, nothing was sent to api.telegram.org or any mail server. The
real Bot API and IMAP/SMTP servers have **not** been exercised. The first real test belongs to the owner, after the
integration: enter a token or credentials, press "Prüfen" with "eingeschaltet", and send a test photo from an
allowlisted chat.

## Known limits

- **Mail sender check uses the From header, which can be forged.** A forged mail from an allowlisted address could start
  an order and use GPU time. The reply goes to the real allowlisted address, so the forger sees nothing.
  `Authentication-Results` (SPF/DKIM/DMARC) is not checked yet, because many small providers do not add it. If needed,
  add a `require_auth` option that demands `dmarc=pass`.
- Telegram group chats: if a group id (negative) is allowlisted, **every member** of the group is served.
- One photo per message or mail goes to the agent; further photos are ignored, and mail says so. Telegram albums arrive
  as separate messages: the first starts the order, and the others go to `handle` as new photos. What that means is up
  to the agent.
- The 20 MB download limit (Bot API) applies to incoming files. Outgoing photos over 10 MB go as documents. Mail
  attachments over 20 MB in total are listed, not attached.
- An event whose send finally fails after 3 tries is dropped and logged. `last_n` has already moved past it, so it is
  not resent. `/status` shows the last state.
- After a crash between "agent.handle succeeded" and "offset persisted", a Telegram update can be handed to the agent
  twice (at-least-once). The window is milliseconds.
- `stop()` does not interrupt a running long poll; the poll is abandoned (daemon thread, at most `poll_timeout` + 15 s
  until the socket closes).
- The event shape for `result` (`data.images` / `files` / `pdf` / `offer_text` / `links`) is my reading of the SPEC.
  If agent.py uses other keys, adjust `base.render`, or let the agent provide `file_bytes`.
- No frontend has been built yet (Settings → Kanäle); the API is ready for it.

## Security fixes after review (2026-10-19)

- `POST /api/channels/{id}/check` tests online **only the saved config of a channel that is switched on**. The body
  (unsaved form values) is validated offline only. It can neither switch the test online (`enabled` in the body is
  ignored) nor point the stored secret at another server. With unsaved changes, the note says „erst speichern“.
- `merge()`: when a target field (mail: `imap_host`, `imap_port`, `smtp_host`, `smtp_port`, `user`; Telegram has a
  fixed API host) changes and no new secret comes with the patch, the stored secret is cleared. A changed target always
  needs the password typed again.
- Switching a channel on needs a team password (409 otherwise), and `start_all` skips enabled channels while no
  password is set. `GET/PUT /api/channels` return `password_set`.
- Tests: `test_stored_mail_password_never_goes_to_another_host` (the reviewer's probe),
  `test_enabling_needs_a_team_password`, plus the updated online-check tests.

## Integration

Exact changes for the integrator. **Do not apply these during the build phase** (no flux-studio restart). Even after
integration, nothing connects until the owner switches a channel on.

### server.py

1. Imports, next to the other module imports:

```python
import channels
```

2. Router: add `channels.router` to the include loop:

```python
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, channels.router):
    app.include_router(r)
```

3. Lifespan: start after `sched.start()` (and after the agent module exists), stop before draining the scheduler:

```python
@asynccontextmanager
async def lifespan(app):
    print("migration:", projects.migrate_all(), flush=True)
    print("recovered jobs:", sched.recover(), flush=True)
    sched.start()
    live.started = now()
    if args.preload and not args.fake_gpu:
        threading.Thread(target=preload, args=([m for m in args.preload.split(",") if m],), daemon=True).start()
    if args.public_port:
        start_public(args.public_port)
    threading.Thread(target=backup_loop, daemon=True, name="backup").start()
    try:
        import agent  # built in Phase 3; channels stay off without it
        print("channels:", channels.start_all(agent, out_dir=OUT) or "none enabled", flush=True)
    except ImportError:
        print("channels: agent.py missing, not started", flush=True)
    yield
    await run_in_threadpool(channels.stop_all)
    await run_in_threadpool(sched.drain, 200)
```

If `agent.py` exists by then, use a plain `import agent` at the top and drop the try/except.

### team.py

No change. `/api/channels*` are normal team routes: the cookie/password check and same-origin check on writes apply,
which is intended. The channels open no route to the outside.

### backup.py

`projects/channels.json` and `projects/channels/*.json` are under `projects/` and so already part of the backup. Note
that the backup then contains the bot token and mail password, as it already contains `.secret`.

### Frontend (Settings → "Kanäle", separate task)

- Load: `GET /api/channels`. Show the `warning` prominently, plus the toggle per channel, `issues`, and `status.fatal` /
  `last_error` / `last_poll`.
- `status.refused` lists unknown chat ids with a "Freigeben" button. It adds `{id, name}` to `allowed_chats` with
  `PUT /api/channels {"telegram": {"allowed_chats": [...]}}`.
- Secret fields: show "gespeichert" when `{"set": true}`. Send a value only when it has been typed; "Entfernen" sends
  `null`.
- "Prüfen" button: `POST /api/channels/{id}/check` with the form values (unsaved) plus `enabled`. Before an online check,
  show a confirm dialog: "verbindet sich mit Telegram / Ihrem Mail-Server".
