// Screenshot walk for the photo + voice system frontend: dashboard entry, „Neuer Auftrag per Foto + Sprache“, the
// Assistent conversation, the mic + „Verstanden:“ card in step ② and the settings Firmenprofil / Kanäle / API & Webhooks.
// Runs against the Playwright mock plus the agent/voice/profile/channel/key routes below (no server, no network).
//   node tests/ui/system.shots.mjs [phone,tablet-land]   → SCRATCH/system-shots/sys-*.png, exit 1 on any violation
import {join} from "node:path";
import {readFileSync, writeFileSync, mkdirSync} from "node:fs";
import {installMock, FIX} from "./mock_api.mjs";

// check.mjs reads WS_SHOTS when it loads, so it is imported after the default is set
process.env.WS_SHOTS ||= join(process.env.WS_SCRATCH || "/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad", "system-shots");
const {launch, checkPage, checkSecureFallbacks, trackRequests, VIEWPORTS, stopServer} = await import("./check.mjs");
const FULL = "a1b2c3d4e5f6", NEW = "0e1e2e3e4e5e", STARTED = "9f8e7d6c5b4a";
const ROOM = readFileSync(join(FIX, "room.png")), V1 = readFileSync(join(FIX, "variant-1.jpg")), V2 = readFileSync(join(FIX, "variant-2.jpg"));
const DEMO = JSON.parse(readFileSync(join(FIX, "agent-demo.json"), "utf8")).events;
const SET = JSON.parse(readFileSync(join(FIX, "system-settings.json"), "utf8"));
const only = (process.argv[2] || "").split(",").filter(Boolean);
const problems = [];
const fail = msg => { problems.push(msg); console.log("  ✗", msg); };
const ok = (cond, msg) => { if (!cond) fail(msg); };
const clone = v => JSON.parse(JSON.stringify(v));
const now = () => new Date().toISOString().slice(0, 19);
const J = (route, status, obj) => route.fulfill({status, contentType: "application/json", body: JSON.stringify(obj)});
const WAV = (() => {   // 1 s of 440 Hz, 16 kHz mono PCM: a real audio file for the file-capture fallback
  const n = 16000, b = Buffer.alloc(44 + n * 2);
  b.write("RIFF", 0); b.writeUInt32LE(36 + n * 2, 4); b.write("WAVEfmt ", 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(16000, 24); b.writeUInt32LE(32000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) b.writeInt16LE(Math.round(8000 * Math.sin(2 * Math.PI * 440 * i / 16000)), 44 + i * 2);
  return b;
})();

const HEARD = "Unter das Fenster ein Sideboard in Eiche, eins achtzig breit. Links daneben ein Wandregal in Weißlack. Die Kiste kommt weg.";
function understood(text) {
  const w = /nein|160/.test(text) ? 160 : 180;
  return {status: "ok", text, summary_de: `Sideboard ${w} × 75 × 45 cm, Eiche natur geölt; Wandregal × 40 × 25 cm, Lack weiß matt; aus dem Foto entfernen: die Kiste`,
    pieces: [{ref: null, id: "nsb001", type: "sideboard", w, h: 75, d: 45, elev: null, material: "eiche", material_custom: "", handles: "push", text: "", where: "unter das Fenster", full_height: false},
      {ref: null, id: "nwr002", type: "wandregal", w: null, h: 40, d: 25, elev: 150, material: "weiss", material_custom: "", handles: null, text: "", where: "links neben dem Sideboard", full_height: false}],
    removes: [{what: "die Kiste"}], drops: [], warnings: [],
    questions: [{piece: 1, field: "w", question_de: "Wie breit soll das Wandregal werden?", default: 120}],
    ops: [{op: "add_piece", value: {id: "nsb001", type: "sideboard", text: "", material: "eiche", material_custom: "", handles: "push", w, h: 75, d: 45, where: "unter das Fenster", placement: {mode: "none", pose: null, box: null}}},
      {op: "add_piece", value: {id: "nwr002", type: "wandregal", text: "", material: "weiss", material_custom: "", handles: null, w: null, h: 40, d: 25, where: "links neben dem Sideboard", elev: 150, placement: {mode: "none", pose: null, box: null}}}],
    confidence: 0.76, seconds: 6.8};
}

// ---------------- the mocked agent: a small script over the real demo conversation
function agentRoutes(calls) {
  const conv = {
    [FULL]: {state: "preview", events: clone(DEMO.slice(0, 10)), polls: 0},
    [NEW]: {state: null, events: [], polls: 0},
  };
  const result = clone(DEMO.find(e => e.kind === "result"));
  const preview = clone(DEMO.filter(e => e.kind === "preview").at(-1));
  const view = (pid, after = 0, fresh) => {
    const c = conv[pid];
    return {pid, state: c.state, busy: ["analyzing", "understanding"].includes(c.state), events: c.events.filter(e => e.n > after),
      next: c.events.length ? c.events.at(-1).n : 0, preview: preview.image, ...(fresh ? {new: fresh} : {})};
  };
  const push = (pid, role, kind, text, extra = {}) => {
    const c = conv[pid], e = {n: (c.events.at(-1)?.n || 0) + 1, at: now(), role, kind, text, ...extra};
    c.events.push(e);
    return e;
  };
  const advance = pid => {   // what the worker would do between two polls
    const c = conv[pid];
    c.polls++;
    if (c.state === "analyzing" && c.polls >= 2) {
      push(pid, "agent", "text", "Verstanden: Einbauschrank 200 × raumhoch × 60 cm, Lack weiß matt, grifflos (Push), 3 Türen (an die Rückwand)");
      push(pid, "agent", "preview", preview.text, {image: preview.image, data: preview.data});
      c.state = "preview";
    } else if (c.state === "generating" && c.polls >= 3) {
      push(pid, "agent", "result", result.text, {image: result.image, data: result.data});
      c.state = "done";
    }
  };
  const field = (req, name) => {
    const raw = (req.postDataBuffer() || Buffer.alloc(0)).toString("latin1");
    const m = new RegExp(`name="${name}"(?:; filename="[^"]*")?\\r\\n(?:Content-Type: [^\\r]+\\r\\n)?\\r\\n([\\s\\S]*?)\\r\\n--`).exec(raw);
    return m ? Buffer.from(m[1], "latin1").toString("utf8") : null;
  };
  return [
    [/\/api\/agent(\?.*)?$/, route => J(route, 200, {conversations: [
      {pid: FULL, title: "TEST Assistent Demo Wohnzimmer", state: conv[FULL].state, updated: now(), channel: "web", last: DEMO.at(-1).text.slice(0, 200), thumb: "uploads/room.png"}]})],
    ["**/api/agent/start", route => {
      const req = route.request();
      calls.push({start: {text: field(req, "text"), title: field(req, "title"), photo: /name="photo"/.test((req.postDataBuffer() || "").toString("latin1"))}});
      conv[STARTED] = {state: "analyzing", events: [], polls: 0};
      push(STARTED, "user", "photo", "Raumfoto", {image: "uploads/room.png"});
      if (field(req, "text")) push(STARTED, "user", "text", field(req, "text"));
      push(STARTED, "agent", "status", "Foto erhalten – ich vermesse den Raum …");
      return J(route, 200, view(STARTED));
    }],
    [/\/api\/agent\/[a-z0-9]+\/events/, route => {
      const u = new URL(route.request().url()), pid = u.pathname.split("/")[3];
      if (!conv[pid]) return J(route, 404, {detail: "Auftrag nicht gefunden."});
      advance(pid);
      return J(route, 200, view(pid, +(u.searchParams.get("after") || 0)));
    }],
    [/\/api\/agent\/[a-z0-9]+\/message/, route => {
      const req = route.request(), pid = new URL(req.url()).pathname.split("/")[3], c = conv[pid] ||= {state: null, events: [], polls: 0};
      const text = field(req, "text"), raw = (req.postDataBuffer() || Buffer.alloc(0)).toString("latin1");
      calls.push({message: {pid, text, audio: /name="audio"/.test(raw), photo: /name="photo"/.test(raw)}});
      const before = c.events.at(-1)?.n || 0;
      c.polls = 0;
      if (!text) {   // a voice message
        push(pid, "user", "audio", "Sprachnachricht");
        push(pid, "agent", "text", "Verstanden: „Und links daneben ein Wandregal.“");
        push(pid, "agent", "question", "Wie breit soll das Wandregal werden? (Vorschlag: 120 cm – „ja“ nimmt den Vorschlag)", {data: {field: "w", piece: "nwr002", default: 120}});
        c.state = "asking";
      } else {
        push(pid, "user", "text", text);
        if (/^(ja|ja, so rechnen|nochmal)$/i.test(text)) { push(pid, "agent", "status", "Ich rechne 2 Vorschläge – Bilder in ca. 3 Min., Bewertung ca. 30 Sek. danach."); c.state = "generating"; }
        else if (/^hilfe$/i.test(text)) push(pid, "agent", "text", "Sagen Sie, was gebaut werden soll – z. B. „links ein Einbauschrank, 220 breit, raumhoch, Weißlack“.\nKorrekturen: „20 cm breiter“, „nimm Eiche“, „weiter links“, „Raumhöhe 2,60“.\n„ja“ rechnet, „stopp“ bricht ab, „senden“ bereitet eine Kundenversion vor.");
        else if (/^stopp$/i.test(text)) { push(pid, "agent", "text", "Abgebrochen."); c.state = "preview"; }
        else { push(pid, "agent", "text", `Verstanden: ${text}`); push(pid, "agent", "preview", preview.text, {image: preview.image, data: preview.data}); c.state = "preview"; }
      }
      const fresh = c.events.filter(e => e.n > before);
      return J(route, 200, view(pid, before, fresh));
    }],
    ["**/outputs/uploads/2026*", route => route.fulfill({status: 200, contentType: "image/png", body: ROOM})],
    ["**/outputs/exports/**", route => route.fulfill({status: 200, contentType: "image/png", body: ROOM})],
    ["**/outputs/2026*", route => route.fulfill({status: 200, contentType: "image/jpeg", body: /485531494|233851/.test(route.request().url()) ? V2 : V1})],
    ["**/api/voice/status", route => J(route, 200, {loaded: false, model: "openai/whisper-large-v3-turbo", ffmpeg: true})],
    ["**/api/voice/transcribe", async route => {
      calls.push({transcribe: (route.request().postDataBuffer() || Buffer.alloc(0)).length});
      await new Promise(r => setTimeout(r, 900));
      return J(route, 200, {status: "ok", text: HEARD, language: "de", duration: 6.2, seconds: 1.1});
    }],
    [/\/api\/workshop\/[a-z0-9]+\/understand/, async route => {
      const body = route.request().postDataJSON();
      calls.push({understand: body});
      await new Promise(r => setTimeout(r, 700));
      return J(route, 200, understood(body.text));
    }],
  ];
}

function settingsRoutes(calls) {
  let channels = clone(SET.channels), keys = [], hooks = {enabled: false, events: ["order.question", "order.preview", "order.result"], hooks: [], recent: []};
  const prof = clone(SET.profile);
  prof.profile.learned = {counts: {}, suggestions: []};
  const sugg = [{id: "s1", type: "schrank", field: "d", value: 65, count: 3, status: "open", text_de: "Einbauschrank: Tiefe standardmäßig 65 cm statt 60 cm? (3× so korrigiert)"}];
  return [
    ["**/api/profile", route => {
      if (route.request().method() === "PUT") { calls.push({profile: route.request().postDataJSON()}); return J(route, 200, prof); }
      return J(route, 200, prof);
    }],
    ["**/api/profile/packs", route => J(route, 200, SET.packs)],
    ["**/api/profile/suggestions", route => J(route, 200, {suggestions: sugg})],
    ["**/api/profile/suggestions/*/*", route => { calls.push({suggestion: route.request().url()}); sugg.length = 0; return J(route, 200, {suggestion: {}, ...prof}); }],
    ["**/api/channels", route => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON();
        calls.push({channels: body});
        for (const [cid, p] of Object.entries(body)) Object.assign(channels.channels[cid], p, p.token ? {token: {set: true}} : {}, p.password ? {password: {set: true}} : {});
      }
      return J(route, 200, channels);
    }],
    ["**/api/channels/*/check", route => { calls.push({check: route.request().postDataJSON()}); return J(route, 200, {ok: false, issues: ["Bot-Token fehlt (von @BotFather)."], online: null, enabled: false, note: "Nur offline geprüft: der Kanal ist ausgeschaltet."}); }],
    ["**/api/keys", route => {
      if (route.request().method() === "POST") {
        const k = {id: "k1", name: route.request().postDataJSON().name, scopes: ["orders"], created: now(), created_by: "anna", last_used: null, hint: "wk_TeSt…only"};
        keys.push(k);
        calls.push({key: k.name});
        return J(route, 200, {...k, key: "wk_TESTONLY_not_a_real_key_0123456789abcdef", note: "Der Schlüssel wird nur jetzt angezeigt – bitte sicher aufbewahren."});
      }
      return J(route, 200, {keys, scopes: ["orders"]});
    }],
    ["**/api/webhooks", route => {
      if (route.request().method() === "PUT") { hooks.enabled = route.request().postDataJSON().enabled; calls.push({webhooks: hooks.enabled}); }
      if (route.request().method() === "POST") {
        const b = route.request().postDataJSON();
        const hk = {id: "h1", url: b.url, events: b.events, enabled: false, key: b.key, created: now(), last: null, secret_hint: "…9f2a"};
        hooks.hooks.push(hk);
        return J(route, 200, {...hk, secret: "whsec_TESTONLY_0123456789", note: "Das Geheimnis wird nur jetzt angezeigt."});
      }
      return J(route, 200, hooks);
    }],
  ];
}

async function open(vp, {insecure = false, fakeMic = false} = {}) {
  const args = fakeMic ? ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] : [];
  const {context, base, close} = await launch({viewport: vp, insecure, args});
  const calls = [];
  const mock = await installMock(context, {scenario: "default", routes: [...agentRoutes(calls), ...settingsRoutes(calls)]});
  if (fakeMic && !insecure) await context.grantPermissions(["microphone"], {origin: base});
  const page = await context.newPage();
  page.on("pageerror", e => fail(`${vp.name}: page error ${e.message}`));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource: the server responded with a status of 40[49]/.test(m.text())) fail(`${vp.name}: console error ${m.text()}`); });
  page.on("dialog", d => { fail(`${vp.name}: native dialog ${d.message()}`); d.dismiss().catch(() => {}); });
  const foreign = trackRequests(page, base);
  return {context, base, close, mock, page, calls, foreign};
}
const shot = async (page, vp, name, opts = {}) => (await checkPage(page, `sys-${vp.name}-${name}`, opts)).forEach(fail);
const settle = (page, ms = 400) => page.waitForTimeout(ms);
const btn = (page, text) => page.locator("button, a.btn, a.chip", {hasText: text}).filter({visible: true}).first();

async function dashboardAndNew(vp) {
  const s = await open(vp), {page, base, calls} = s;
  await page.goto(`${base}/werkstatt#/`);
  await page.waitForSelector(".order");
  await settle(page);
  const entry = page.locator("#ws-actions .dash-voice");
  ok(await entry.isVisible(), `${vp.name}: dashboard entry „Foto + Sprache“ visible`);
  await shot(page, vp, "dash", {primary: "#ws-actions .btn.primary"});
  await entry.click();
  await page.waitForSelector(".as-new");
  await settle(page);
  ok((await page.locator(".hdr-title").textContent()).includes("Foto + Sprache"), `${vp.name}: title of the new-order screen`);
  ok(await page.locator(".as-recent a").count() === 1, `${vp.name}: recent assistant conversations listed`);
  await shot(page, vp, "neu", {primary: "#ws-actions .btn.primary"});
  // start without anything → German hint, nothing sent
  await page.locator("#ws-actions .btn.primary").click();
  await settle(page, 300);
  ok(/Raumfoto/.test(await page.locator(".toast").last().textContent()), `${vp.name}: start without input explains what is missing`);
  const [chooser] = await Promise.all([page.waitForEvent("filechooser"), btn(page, "Foto wählen").click()]);
  await chooser.setFiles(join(FIX, "room.png"));
  await page.waitForSelector(".as-new img.photo");
  await page.fill(".as-new textarea", "TEST An die Rückwand ein Einbauschrank, zwei Meter breit, raumhoch, Weißlack, grifflos.");
  await page.fill(".as-new input[type=text]", "TEST Müller – Einbauschrank");
  await settle(page);
  await shot(page, vp, "neu-filled", {primary: "#ws-actions .btn.primary"});
  await page.locator("#ws-actions .btn.primary").click();
  await page.waitForURL(`**#/p/${STARTED}/assistent`);
  const st = calls.find(c => c.start)?.start;
  ok(st && st.photo && /Einbauschrank/.test(st.text || "") && /Müller/.test(st.title || ""), `${vp.name}: start sends photo, text and title (${JSON.stringify(st)})`);
  await page.waitForSelector(".as-typing:not([hidden])");
  await shot(page, vp, "chat-busy");
  await page.waitForSelector(".as-msg.agent img", {timeout: 12000});
  await settle(page, 600);
  ok(await page.locator(".as-quick .chip", {hasText: "Ja, so rechnen"}).isVisible(), `${vp.name}: preview → quick reply „Ja, so rechnen“`);
  ok(!s.foreign.length, `${vp.name}: no foreign requests (${s.foreign.join(", ")})`);
  await s.close();
}

async function chat(vp) {
  const s = await open(vp, {fakeMic: true}), {page, base, calls} = s;
  // empty conversation on a normal order: intro text
  await page.goto(`${base}/werkstatt#/p/${NEW}/assistent`);
  await page.waitForSelector(".as-intro:not([hidden])");
  await settle(page);
  ok(await page.locator(".pbar .tab[aria-current=page]", {hasText: "Assistent"}).count() === 1, `${vp.name}: Assistent tab marked`);
  await shot(page, vp, "chat-intro");
  // the demo conversation, waiting for the OK
  await page.goto(`${base}/werkstatt#/p/${FULL}/assistent`);
  await page.waitForSelector(".as-msg.agent .as-pic img");
  await settle(page, 700);
  ok(await page.locator(".as-log > li").count() === 10, `${vp.name}: 10 demo events shown (got ${await page.locator(".as-log > li").count()})`);
  ok(await page.evaluate(() => [...document.querySelectorAll(".as-pic img")].every(i => i.complete && i.naturalWidth > 0)), `${vp.name}: chat images load`);
  await shot(page, vp, "chat-preview");
  await page.locator(".as-msg.agent .as-pic").last().click();
  await page.waitForSelector(".sheet img");
  await settle(page);
  await shot(page, vp, "chat-preview-big");
  await page.keyboard.press("Escape");
  // a typed correction → new preview
  await page.fill(".as-bar input", "Das Sideboard 20 cm nach rechts");
  await page.locator(".as-bar .btn.primary").click();
  await page.waitForFunction(() => document.querySelectorAll(".as-log > li").length >= 13);
  ok(calls.some(c => c.message?.text === "Das Sideboard 20 cm nach rechts"), `${vp.name}: typed message sent as text`);
  ok(await page.inputValue(".as-bar input") === "", `${vp.name}: input cleared after sending`);
  // a voice message (localhost is a secure context; the fake mic records)
  await page.locator(".as-bar .vc-mic").click();
  await page.waitForSelector(".as-bar .vc-mic.rec");
  await page.waitForTimeout(1300);
  await shot(page, vp, "chat-recording");
  await page.locator(".as-bar .vc-mic").click();
  await page.waitForSelector(".as-msg.q");
  await settle(page, 500);
  ok(calls.some(c => c.message?.audio), `${vp.name}: voice message sent as audio`);
  ok(await page.locator(".as-quick .chip", {hasText: "Vorschlag nehmen: 120 cm"}).isVisible(), `${vp.name}: question → suggestion chip`);
  await shot(page, vp, "chat-asking");
  await page.locator(".as-quick .chip", {hasText: "Vorschlag nehmen"}).click();
  await page.waitForFunction(() => document.querySelector(".as-head .badge")?.textContent.includes("Rechnet"));
  await settle(page, 300);
  await shot(page, vp, "chat-generating");
  await page.waitForSelector(".as-res img", {timeout: 15000});
  await settle(page, 800);
  ok(await page.locator(".as-links a", {hasText: "Kundenblatt (PDF)"}).count() === 1, `${vp.name}: result shows the PDF link`);
  ok(await page.locator(".as-links a", {hasText: "Zeichnung (DXF)"}).count() === 1, `${vp.name}: result shows the DXF link`);
  ok(await page.locator(".as-quick .chip", {hasText: "Nochmal rechnen"}).isVisible(), `${vp.name}: done → „Nochmal rechnen“`);
  await shot(page, vp, "chat-done");
  await page.locator(".as-res .as-pic").first().click();
  await page.waitForSelector(".sheet img");
  await settle(page);
  await shot(page, vp, "chat-result-big");
  await page.keyboard.press("Escape");
  await s.close();
}

async function moebel(vp) {
  const s = await open(vp, {fakeMic: true}), {page, base, calls, mock} = s;
  await page.goto(`${base}/werkstatt#/p/${FULL}/moebel`);
  await page.waitForSelector(".vs .vc-mic");
  await settle(page);
  ok(!(await page.locator(".vs .vc-hint").isVisible()), `${vp.name}: secure context → no fallback hint`);
  await shot(page, vp, "moebel-card", {primary: "#ws-actions .btn.primary"});
  await page.locator(".vs .vc-mic").click();
  await page.waitForSelector(".vs .vc-mic.rec");
  await page.waitForTimeout(1400);
  await shot(page, vp, "moebel-recording", {primary: "#ws-actions .btn.primary"});
  await page.locator(".vs .vc-mic").click();
  await page.waitForSelector(".vs .vs-status .bar");
  await shot(page, vp, "moebel-hearing", {primary: "#ws-actions .btn.primary"});
  await page.waitForSelector(".vs-got");
  await settle(page);
  ok(calls.some(c => c.transcribe > 1000), `${vp.name}: recorded audio sent to /api/voice/transcribe`);
  const u = calls.find(c => c.understand)?.understand;
  ok(u && u.text === HEARD && "active_piece" in u, `${vp.name}: transcript sent to understand with the active piece`);
  ok((await page.inputValue(".vs textarea")) === HEARD, `${vp.name}: transcript shown for correction`);
  await page.locator(".vs-got").scrollIntoViewIfNeeded();
  await shot(page, vp, "moebel-verstanden", {primary: "#ws-actions .btn.primary"});
  // a typed correction before „Übernehmen“ joins the text
  await page.fill(".vs textarea", HEARD + " Nein, das Sideboard nur 160.");
  await btn(page, "Verstehen").click();
  await page.waitForFunction(() => /160 × 75/.test(document.querySelector(".vs-got")?.textContent || ""));
  await page.fill(".vs-q input", "140");
  await btn(page, "Übernehmen").click();
  await page.waitForSelector(".toast .btn", {timeout: 5000});
  await settle(page, 600);
  const doc = mock.doc(FULL);
  const wr = doc.pieces.find(p => p.id === "nwr002"), sb = doc.pieces.find(p => p.id === "nsb001");
  ok(doc.pieces.length === 4 && wr?.w === 140 && sb?.w === 160 && wr?.elev === 150, `${vp.name}: Übernehmen applied via PATCH (pieces ${doc.pieces.map(p => `${p.id}:${p.w}`).join(",")})`);
  ok(await page.locator(".mb-card.open#pc-nsb001").count() === 1, `${vp.name}: first new piece opened`);
  await shot(page, vp, "moebel-applied", {primary: "#ws-actions .btn.primary"});
  await page.locator(".toast .btn", {hasText: "Rückgängig"}).click();
  await page.waitForFunction(() => document.querySelectorAll(".mb-card").length === 2, null, {timeout: 5000}).catch(() => fail(`${vp.name}: undo did not remove the two pieces`));
  ok(mock.doc(FULL).pieces.length === 2, `${vp.name}: undo restored 2 pieces on the server`);
  await s.close();
}

async function moebelInsecure(vp) {
  const s = await open(vp, {insecure: true}), {page, base, calls} = s;
  await page.goto(`${base}/werkstatt#/p/${FULL}/moebel`);
  await page.waitForSelector(".vs .vc-mic");
  await settle(page);
  ok(await page.locator(".vs .vc-hint").isVisible(), `${vp.name}: http LAN → fallback hint visible`);
  ok(/https/.test(await page.locator(".vs .vc-hint").textContent()), `${vp.name}: hint names https`);
  await shot(page, vp, "moebel-insecure", {primary: "#ws-actions .btn.primary"});
  const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.locator(".vs .vc-mic").click()]);
  ok(await chooser.element().evaluate(e => e.accept.includes("audio/") && e.hasAttribute("capture")), `${vp.name}: file fallback is <input accept=audio/* capture>`);
  await chooser.setFiles({name: "memo.wav", mimeType: "audio/wav", buffer: WAV});
  await page.waitForSelector(".vs-got");
  ok(calls.some(c => c.transcribe > 1000), `${vp.name}: picked audio file transcribed`);
  await btn(page, "Warum geht das Mikrofon nicht?").click();
  await page.waitForSelector(".sheet");
  await settle(page);
  await shot(page, vp, "moebel-mic-help");
  await page.keyboard.press("Escape");
  (await checkSecureFallbacks(page)).forEach(fail);
  // the Assistent composer on http: mic opens the recorder app (file input)
  await page.goto(`${base}/werkstatt#/p/${FULL}/assistent`);
  await page.waitForSelector(".as-msg.agent .as-pic img");
  await settle(page, 500);
  await shot(page, vp, "chat-insecure");
  await s.close();
}

async function settings(vp) {
  const s = await open(vp), {page, base, calls} = s;
  await page.goto(`${base}/werkstatt#/einstellungen/profil`);
  await page.waitForSelector(".settings [role=radiogroup]");
  await settle(page);
  ok(await page.locator("button", {hasText: "Übernehmen"}).count() >= 1, `${vp.name}: learned suggestion offered`);
  await shot(page, vp, "set-profil", {fullPage: true});
  await page.locator("details > summary", {hasText: "Maße je Möbelart"}).click();
  await page.locator("details details", {hasText: "Einbauschrank"}).first().locator("summary").click();
  await settle(page);
  await shot(page, vp, "set-profil-type");
  await page.locator("details details", {hasText: "Einbauschrank"}).first().locator("input[type=number]").nth(2).fill("65");
  await page.locator("details details", {hasText: "Einbauschrank"}).first().locator("button", {hasText: "Speichern"}).click();
  await settle(page);
  ok(calls.some(c => c.profile?.types?.schrank?.d === 65), `${vp.name}: type default saved as merge patch ${JSON.stringify(calls.filter(c => c.profile))}`);

  await page.goto(`${base}/werkstatt#/einstellungen/kanaele`);
  await page.waitForSelector(".settings h2:text('Telegram')");
  await settle(page);
  ok(/Internet/.test(await page.locator(".card.warn").first().textContent()), `${vp.name}: channel warning shown`);
  await shot(page, vp, "set-kanaele", {fullPage: true});
  await btn(page, "Eingaben prüfen").click();
  await settle(page);
  ok(calls.some(c => c.check && c.check.enabled === false), `${vp.name}: „Eingaben prüfen“ is offline only (enabled:false)`);
  // switching on with missing data is refused before any dialog
  await page.locator(".toggle-row", {hasText: "Ausgeschaltet"}).first().click();
  await settle(page);
  ok(!calls.some(c => c.channels?.telegram?.enabled), `${vp.name}: incomplete channel not switched on`);
  ok(await page.locator(".toggle-row[aria-checked=true]", {hasText: "Ausgeschaltet"}).count() === 0, `${vp.name}: toggle back off`);
  // with a token and a chat the owner must type the confirmation word
  await page.fill("input[type=password]", "123456:ABCdefGhIJKlmnoPQRstuVWxyz12345678");
  await page.locator("textarea").first().fill("123456789 Peter");
  await btn(page, "Speichern").click();
  await settle(page);
  ok(calls.some(c => c.channels?.telegram?.token && !c.channels.telegram.enabled), `${vp.name}: saving keeps the channel off`);
  await s.close();

  const s2 = await open(vp), p2 = s2.page;
  await p2.route("**/api/channels", r => r.request().method() === "GET" ? J(r, 200, {...SET.channels, channels: {...SET.channels.channels,
    telegram: {...SET.channels.channels.telegram, token: {set: true}, allowed_chats: [{id: 123456789, name: "Peter"}], issues: []}}}) : r.fallback());
  await p2.goto(`${s2.base}/werkstatt#/einstellungen/kanaele`);
  await p2.waitForSelector(".settings h2:text('Telegram')");
  await p2.locator(".toggle-row", {hasText: "Ausgeschaltet"}).first().click();
  await p2.waitForSelector(".dialog");
  await settle(p2);
  ok(await p2.locator(".dialog .btn.danger").isDisabled(), `${vp.name}: „Einschalten“ disabled until the word is typed`);
  await shot(p2, vp, "set-kanaele-confirm");
  await p2.locator(".dialog .btn.secondary").click();
  await settle(p2);
  ok(!s2.calls.some(c => c.channels?.telegram?.enabled), `${vp.name}: cancelled → nothing switched on`);
  await s2.close();

  const s3 = await open(vp), p3 = s3.page;
  await p3.goto(`${s3.base}/werkstatt#/einstellungen/api`);
  await p3.waitForSelector(".settings h2:text('Webhooks')");
  await settle(p3);
  await shot(p3, vp, "set-api", {fullPage: true});
  await btn(p3, "Neuer Schlüssel").click();
  await p3.fill(".sheet input", "TEST n8n");
  await p3.locator(".sheet .btn.primary").click();
  await p3.waitForSelector(".sheet textarea.copy-area");
  await settle(p3);
  ok((await p3.inputValue(".sheet textarea.copy-area")).startsWith("wk_TESTONLY"), `${vp.name}: new key shown once`);
  await shot(p3, vp, "set-api-key");
  await p3.locator(".sheet .btn.primary", {hasText: "Fertig"}).click();
  await settle(p3);
  await p3.locator(".toggle-row", {hasText: "Webhooks ausgeschaltet"}).click();
  await p3.waitForSelector(".dialog");
  await p3.locator(".dialog .btn.secondary").click();
  await settle(p3);
  ok(!s3.calls.some(c => c.webhooks === true), `${vp.name}: webhooks stay off without the typed confirmation`);
  await s3.close();
}

const VPS = [VIEWPORTS.phone, VIEWPORTS.tabletLandscape].filter(v => !only.length || only.includes(v.name));
for (const vp of VPS) {
  for (const [name, fn] of [["dashboard+neu", dashboardAndNew], ["chat", chat], ["moebel", moebel], ["moebel-http", moebelInsecure], ["settings", settings]]) {
    console.log(`▶ ${vp.name} ${name}`);
    try { await fn(vp); } catch (e) { fail(`${vp.name} ${name}: ${e.message.split("\n")[0]}`); }
  }
}
await stopServer();
mkdirSync(process.env.WS_SHOTS, {recursive: true});
writeFileSync(join(process.env.WS_SHOTS, "result.json"), JSON.stringify({problems}, null, 1));
console.log(problems.length ? `\n${problems.length} problem(s)` : "\nall checks passed");
process.exit(problems.length ? 1 : 0);
