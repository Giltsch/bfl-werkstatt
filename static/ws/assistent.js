// Assistent: the conversation per order (#/p/<pid>/assistent) and „Neuer Auftrag per Foto + Sprache“ (#/assistent).
// Talks to agent.py: POST /api/agent/start, POST /api/agent/{pid}/message, GET /api/agent/{pid}/events?after=n,
// GET /api/agent (recent conversations). Every string from the server goes in as text (h() makes text nodes).
// Pure helpers are exported for tests/ws_system.test.mjs; DOM work only inside mount().
import { h, api, toast, sheet, icon, img, fmt, pickImage, shrink, attention, copyText, confirmDialog } from "./ui.js";
import { voiceButton, micHelp, currentSupport } from "./voice.js";

export const STATE_DE = {
  need_photo: ["Wartet auf ein Foto", "warn"], analyzing: ["Vermisst den Raum …", "info"], understanding: ["Liest mit …", "info"],
  asking: ["Frage an Sie", "warn"], preview: ["Vorschau – wartet auf Ihr OK", "warn"], generating: ["Rechnet Bilder …", "info"],
  done: ["Fertig", "ok"], error: ["Problem", "bad"],
};
export const BUSY = new Set(["analyzing", "understanding"]);
export const MAX_TEXT = 2000;

/** Server references → URLs: "uploads/x.png" / "exports/…" live under /outputs, absolute paths and http(s) stay. */
export function fileUrl(ref) {
  const s = String(ref || "");
  if (!s) return "";
  if (/^(https?:)?\/\//.test(s) || s.startsWith("/")) return s;
  return img(s.replace(/^\.?\/*/, ""));
}

/** Events by n, without duplicates (a POST answer and a poll can carry the same event). */
export function mergeEvents(list, incoming) {
  const by = new Map((list || []).map(e => [e.n, e]));
  for (const e of incoming || []) if (e && Number.isFinite(e.n)) by.set(e.n, e);
  return [...by.values()].sort((a, b) => a.n - b.n);
}

export const lastAgent = (events, kinds = null) => [...(events || [])].reverse()
  .find(e => e.role === "agent" && (!kinds || kinds.includes(e.kind))) || null;

/** The tap answers under the chat for the current state. {label, send} sends text, {action: "edit"|"photo"}, {href}. */
export function quickReplies(view, pid) {
  const st = view?.state, evs = view?.events || [];
  if (!st || view?.busy || BUSY.has(st)) return [];
  if (st === "need_photo") return [{ label: "Foto senden", action: "photo", kind: "primary" }];
  if (st === "preview") {
    // the plan check found the piece in front of a window/door/radiator: its suggestion first, "ja" only on purpose
    const fix = lastAgent(evs, ["preview"])?.data?.conflict?.fix;
    if (fix) return [{ label: "Vorschlag übernehmen", send: "Vorschlag übernehmen", kind: "primary" },
      { label: "Ja, trotzdem rechnen", send: "ja, trotzdem rechnen" }, { label: "Ändern …", action: "edit" }];
    return [{ label: "Ja, so rechnen", send: "ja", kind: "primary" }, { label: "Ändern …", action: "edit" }];
  }
  if (st === "generating") return [{ label: "Stopp", send: "stopp" }, { label: "Wie weit?", send: "status" }];
  if (st === "done") return [{ label: "Nochmal rechnen", send: "nochmal" }, { label: "Ändern …", action: "edit" },
    { label: "Zum Ergebnis", href: `#/p/${pid}/ergebnis` }];
  if (st === "asking") {
    const q = lastAgent(evs, ["question"]), d = q?.data || {};
    if (d.field === "photo") return [{ label: "Foto senden", action: "photo", kind: "primary" }];
    if (d.field === "where") return [{ label: "Egal – ohne feste Stelle", send: "egal" }, { label: "Antworten …", action: "edit" }];
    if (d.default != null && ["w", "h", "d"].includes(d.field)) return [{ label: `Vorschlag nehmen: ${fmt.cm(d.default)}`, send: "ja", kind: "primary" },
      { label: "Anderes Maß …", action: "edit" }];
    return [{ label: "Antworten …", action: "edit" }];
  }
  return [{ label: "Was kann ich sagen?", send: "hilfe" }];
}

/** Download links of a result event (PDF, DXF, 3D models, offer text). */
export function resultLinks(data, pid) {
  const d = data || {}, out = [];
  if (d.pdf) out.push({ label: "Kundenblatt (PDF)", href: fileUrl(d.pdf), icon: "pdf" });
  for (const f of [].concat(d.files || [])) {
    const ref = typeof f === "string" ? f : f?.path || f?.url || "";
    if (!ref) continue;
    const name = ref.split("/").pop();
    out.push({ label: /\.dxf$/i.test(name) ? "Zeichnung (DXF)" : name, href: fileUrl(ref), icon: "download" });
  }
  for (const m of d.models || []) if (m?.glb) out.push({ label: `3D: ${m.label || "Möbel"} (GLB)`, href: fileUrl(m.glb), icon: "download" });
  if (d.offer_text && pid) out.push({ label: "Angebotstext (TXT)", href: `/api/workshop/${pid}/export/angebot.txt`, icon: "download" });
  return out;
}

/** „✓ 8,5“ / „⚠ 7,3“ / „✗ 4,0“ / „ungeprüft“ and its badge tone. */
export function scoreOf(v) {
  if (!v || v.status !== "ok" || v.score == null) return { text: "ungeprüft", tone: "muted" };
  const x = fmt.num(v.score, 1);
  if (v.pass) return { text: `✓ ${x}`, tone: "ok" };
  return v.score >= 5 ? { text: `⚠ ${x}`, tone: "warn" } : { text: `✗ ${x}`, tone: "bad" };
}

/** First line of an agent text as the headline, the rest as body. */
export function splitText(text) {
  const lines = String(text || "").split("\n");
  return { head: lines[0] || "", rest: lines.slice(1).join("\n").trim() };
}

/** Poll interval: fast while the assistant works, slow while it waits, very slow in a hidden tab. */
export function pollDelay(view, hidden = false) {
  if (hidden) return 15000;
  if (!view) return 3000;
  return view.busy || BUSY.has(view.state) || view.state === "generating" ? 1500 : 5000;
}

export const hhmm = iso => { const m = /T(\d{2}:\d{2})/.exec(String(iso || "")); return m ? m[1] : ""; };

/** What „Assistent starten“ needs: a photo or something said; returns the German reason or null. */
export function startProblem({ photo, audio, text }) {
  if (!photo && !audio && !String(text || "").trim()) return "Bitte ein Raumfoto machen und sagen oder schreiben, was hin soll.";
  if (String(text || "").length > MAX_TEXT) return `Text zu lang (höchstens ${MAX_TEXT} Zeichen).`;
  return null;
}

// ------------------------------------------------------------------ DOM

function css() {
  if (document.getElementById("ws-as-css")) return;
  document.head.append(h("style", { id: "ws-as-css" }, `
.as{display:flex;flex-direction:column;gap:12px;max-width:820px;margin:0 auto;width:100%}
.as-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.as-head .grow{flex:1;min-width:0}
.as-head .grow .badge{white-space:normal;max-width:100%;border-radius:12px}
.as-log{display:flex;flex-direction:column;gap:10px;margin:0;padding:0;list-style:none}
.as-msg{display:flex;flex-direction:column;max-width:min(88%,620px);gap:4px}
.as-msg.user{align-self:flex-end;align-items:flex-end}
.as-msg.agent{align-self:flex-start}
.as-bub{border-radius:16px;padding:10px 14px;font-size:16px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);background:var(--panel)}
.as-msg.user .as-bub{background:var(--accent-soft);border-color:transparent;border-bottom-right-radius:6px}
.as-msg.agent .as-bub{border-bottom-left-radius:6px}
.as-msg.err .as-bub{border-color:var(--danger);background:var(--bad-soft)}
.as-msg.q .as-bub{border-color:var(--warn);background:var(--warn-soft)}
.as-bub b{display:block;margin-bottom:2px}
.as-time{font-size:13px;color:var(--muted);padding:0 6px}
.as-status{align-self:center;font-size:14px;color:var(--muted);text-align:center;max-width:90%;padding:2px 10px}
.as-pic{display:block;padding:0;border:0;background:var(--panel2);border-radius:12px;overflow:hidden;cursor:zoom-in;max-width:100%;min-width:44px;min-height:44px}
.as-pic img{display:block;width:100%;height:auto;max-height:420px;object-fit:contain}
.as-msg.user .as-pic img{max-height:200px;width:auto;max-width:100%}
.as-res{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.as-res .as-pic{position:relative}
.as-res .as-pic img{aspect-ratio:4/3;object-fit:cover;max-height:none}
.as-res .badge{position:absolute;left:6px;top:6px}
.as-links{display:flex;flex-wrap:wrap;gap:8px}
.as-links .btn{flex:1 1 170px;justify-content:flex-start}
.as-offer pre{white-space:pre-wrap;font:inherit;font-size:15px;margin:0 0 8px;overflow-wrap:anywhere}
.as-typing{display:flex;flex-direction:column;gap:6px;align-self:flex-start;min-width:min(320px,90%);font-size:15px;color:var(--muted)}
.as-intro p{margin:0 0 6px}.as-intro ul{margin:0;padding-left:20px}
#ws-actions>.as-compose{flex:1 1 100%;display:flex;flex-direction:column;gap:8px;min-width:0}
.as-quick{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}
.as-quick::-webkit-scrollbar{display:none}
.as-quick a.chip{text-decoration:none;color:var(--text)}
.as-bub details>summary{color:var(--accent);font-weight:600}
.as-quick .chip.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:650}
.as-bar{display:flex;gap:8px;align-items:center;min-width:0}
.as-bar input{flex:1;min-width:0;min-height:48px;font-size:16px;border-radius:999px;padding:8px 16px}
.as-bar .btn.icon-only{width:48px;min-height:48px;border-radius:50%;flex:none}
.as-bar .vc{flex:none}
.as-bar .vc-row{flex-wrap:nowrap}
.as-new{display:flex;flex-direction:column;gap:12px;max-width:720px;margin:0 auto;width:100%}
.as-new .card{margin:0;display:flex;flex-direction:column;gap:10px}
.as-new h2{margin:0;font-size:18px}
.as-new .acts{display:flex;gap:8px;flex-wrap:wrap}.as-new .acts .btn{flex:1 1 160px}
.as-new .photo{max-height:240px;border-radius:12px;object-fit:contain;background:var(--panel2);align-self:flex-start;max-width:100%}
.as-new textarea{width:100%;min-height:88px;font-size:16px}
.as-new input[type=text]{width:100%;min-height:48px;font-size:16px}
.as-new audio{width:100%;max-width:420px}
.as-new .hint{color:var(--muted);font-size:14px;margin:0}
.as-recent{display:flex;flex-direction:column;gap:8px;margin:0;padding:0;list-style:none}
.as-recent a{display:grid;grid-template-columns:56px minmax(0,1fr);gap:4px 12px;align-items:center;padding:8px;border:1px solid var(--line);border-radius:var(--r);background:var(--panel);color:var(--text);text-decoration:none;min-height:72px}
.as-recent .th{width:56px;height:56px;border-radius:8px;object-fit:cover;background:var(--panel2);grid-row:span 2}
.as-recent .t{font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.as-recent .l{font-size:14px;color:var(--muted);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
`));
}

// previews are pruned after a while (agent.py keeps the newest 5): a missing picture becomes a short note
function gone(e) {
  const pic = e.target.closest(".as-pic") || e.target;
  pic.replaceWith(h("div", { class: "as-bub muted small" }, "Bild nicht mehr gespeichert."));
}

function bigImage(title, src, ...extra) {
  sheet({ title, body: h("div", { style: "display:flex;flex-direction:column;gap:10px" },
    h("img", { src, alt: title, style: "width:100%;height:auto;border-radius:10px;background:var(--panel2)" }), ...extra),
  actions: [{ label: "Schließen", kind: "secondary" }] });
}

export default {
  mount(root, ctx) {
    css();
    return ctx.route.name === "assistent-neu" ? mountNew(root, ctx) : mountChat(root, ctx);
  },
};

// ---------------------------------------------------------------- conversation

function mountChat(root, ctx) {
  const { store } = ctx, pid = ctx.route.pid;
  let view = null, events = [], dead = false, timer = null, sending = false, loadErr = null, lastSeen = 0;
  const sup = currentSupport();

  const stateEl = h("span", { class: "badge" });
  const log = h("ol", { class: "as-log", "aria-live": "polite", "aria-label": "Verlauf" });
  const typing = h("div", { class: "as-typing", hidden: true });
  const intro = h("div", { class: "card as-intro", hidden: true });
  root.replaceChildren(h("div", { class: "as" },
    h("div", { class: "as-head" }, h("div", { class: "grow" }, stateEl),
      h("button", { class: "btn ghost", type: "button", on: { click: () => send({ text: "hilfe" }) } }, "Was kann ich sagen?")),
    intro, log, typing));

  // composer in the app's footer: quick replies + [Foto] [Text] [Mikrofon] [Senden]
  const quick = h("div", { class: "as-quick", role: "group", "aria-label": "Schnellantworten" });
  const input = h("input", { type: "text", maxLength: MAX_TEXT, enterKeyHint: "send", autocomplete: "off", "aria-label": "Nachricht an den Assistenten",
    placeholder: "Nachricht, z. B. „20 cm breiter“" });
  const photoBtn = h("button", { class: "btn secondary icon-only", type: "button", "aria-label": "Foto senden", html: icon("camera"), on: { click: sendPhoto } });
  const sendBtn = h("button", { class: "btn primary icon-only", type: "button", "aria-label": "Senden", html: icon("send"), on: { click: () => sendText() } });
  const mic = voiceButton({ small: true, label: "Sprachnachricht", hint: false, maxSeconds: 300,
    onAudio: (blob, name) => send({ audio: blob, name }), onRecording: on => { input.hidden = on; photoBtn.hidden = on; sendBtn.hidden = on; } });
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); sendText(); } });
  const composer = h("div", { class: "as-compose" }, quick, h("div", { class: "as-bar" }, photoBtn, input, mic.el, sendBtn));
  ctx.actions([composer]);

  function sendText(t = input.value) {
    t = String(t || "").trim();
    if (!t) { input.focus(); return; }
    send({ text: t }).then(ok => { if (ok) input.value = ""; });
  }
  async function sendPhoto() {
    const f = await pickImage({ camera: true });
    if (!f) return;
    send({ photo: await shrink(f, 2560), name: "foto.jpg" });
  }

  async function send({ text, audio, photo, name }) {
    if (sending) return false;
    const fd = new FormData();
    if (text) fd.append("text", text);
    if (audio) fd.append("audio", audio, name || "nachricht.webm");
    if (photo) fd.append("photo", photo, name || "foto.jpg");
    sending = true; drawComposer(); drawTyping("Wird gesendet …");
    try {
      const r = await api("POST", `/api/agent/${pid}/message`, fd);
      apply(r);
      return true;
    } catch (e) {
      toast(e.status === 404 ? "Auftrag nicht gefunden." : e.message, { kind: "err" });
      return false;
    } finally { sending = false; drawComposer(); drawTyping(); schedule(); }
  }

  function apply(v) {
    if (!v) return;
    const had = events.length ? events[events.length - 1].n : 0;
    events = mergeEvents(events, [...(v.events || []), ...(v.new || [])]);
    view = { ...view, ...v, events };
    drawLog(had);
    drawHead(); drawComposer(); drawTyping();
    const fresh = events.filter(e => e.n > Math.max(had, lastSeen) && e.role === "agent" && ["preview", "result", "question", "error"].includes(e.kind));
    if (had && fresh.length && document.hidden) attention(`Assistent: ${splitText(fresh[fresh.length - 1].text).head.slice(0, 80)}`);
    lastSeen = Math.max(lastSeen, ...events.map(e => e.n));
  }

  async function poll() {
    timer = null;
    if (dead) return;
    try {
      const after = events.length ? events[events.length - 1].n : 0;
      apply(await api("GET", `/api/agent/${pid}/events?after=${after}`));
      loadErr = null;
    } catch (e) {
      if (e.status === 404) { loadErr = "Auftrag nicht gefunden."; dead = true; }
      else loadErr = e.message;
      drawHead();
    }
    schedule();
  }
  function schedule() {
    if (dead) return;
    clearTimeout(timer);
    timer = setTimeout(poll, pollDelay(view, document.hidden));
  }
  const onVis = () => { if (!document.hidden) { clearTimeout(timer); poll(); } };
  document.addEventListener("visibilitychange", onVis);

  function drawHead() {
    const [label, tone] = loadErr ? [loadErr, "bad"] : STATE_DE[view?.state] || (view ? ["Noch kein Gespräch", "muted"] : ["Wird geladen …", "muted"]);
    stateEl.className = `badge ${tone}`;
    stateEl.textContent = label;
    intro.hidden = !(view && !events.length);
    if (!intro.hidden && !intro.childElementCount) intro.append(...[
      h("p", {}, h("b", {}, "Mit dem Assistenten planen")),
      h("p", {}, "Sagen oder schreiben Sie, was in diesem Auftrag gebaut werden soll. Der Assistent kennt das Raumfoto und die Möbel, fragt nur nach, was fehlt, und zeigt erst eine Vorschau. Gerechnet wird erst nach Ihrem „Ja“."),
      h("ul", {}, h("li", {}, "„Links neben dem Fenster ein Einbauschrank, zwei-zwanzig breit, raumhoch, Weißlack.“"),
        h("li", {}, "„Das Sideboard 20 cm nach rechts.“ · „Raumhöhe 2,60.“ · „Nimm Eiche.“")),
      sup.mode === "file" ? h("p", { class: "muted small", style: "margin-top:6px" }, "Mikrofon: Ohne https öffnet der Knopf die Sprachaufnahme des Handys. ",
        h("button", { class: "btn ghost", type: "button", style: "min-height:44px;padding:0 6px", on: { click: micHelp } }, "Warum?")) : null].filter(Boolean));
  }

  function drawTyping(text) {
    // generating takes minutes: the moving bar shows it is still working (the status line carries the estimate)
    const busy = sending || view?.busy || BUSY.has(view?.state) || view?.state === "generating";
    typing.hidden = !busy;
    if (!busy) return;
    const st = lastAgent(events, ["status"]);
    const gen = !sending && !view?.busy && view?.state === "generating";   // its status line is already in the log
    typing.replaceChildren(h("span", {}, text || (gen ? "Bilder werden gerechnet …" : st && st.n > (lastAgent(events, ["text", "preview", "question", "result"])?.n || 0) ? st.text : "Assistent arbeitet …")),
      h("div", { class: "bar indeterminate" }, h("i")));
  }

  function drawComposer() {
    const off = sending || dead;
    input.disabled = off; sendBtn.disabled = off; photoBtn.disabled = off; mic.busy(off);
    const qs = view ? quickReplies(view, pid) : [];
    quick.hidden = !qs.length;
    quick.replaceChildren(...qs.map(q => q.href
      ? h("a", { class: "chip", href: q.href }, q.label)
      : h("button", { class: "chip" + (q.kind === "primary" ? " primary" : ""), type: "button", disabled: off, on: { click: () => {
        if (q.send) send({ text: q.send });
        else if (q.action === "photo") sendPhoto();
        else if (q.action === "edit") { input.placeholder = view?.state === "asking" ? "Ihre Antwort …" : "Was soll anders? z. B. „20 cm breiter“, „nimm Eiche“, „weiter links“"; input.focus(); }
      } } }, q.label)));
  }

  function bubble(e, ...kids) {
    const cls = ["as-msg", e.role === "user" ? "user" : "agent", e.kind === "error" ? "err" : "", e.kind === "question" ? "q" : ""].filter(Boolean).join(" ");
    return h("li", { class: cls, "data-n": String(e.n) }, ...kids, h("span", { class: "as-time" }, hhmm(e.at)));
  }

  function eventNode(e) {
    if (e.role === "agent" && e.kind === "status") return h("li", { class: "as-status", "data-n": String(e.n) }, e.text);
    if (e.kind === "photo") return bubble(e, e.image ? h("button", { class: "as-pic", type: "button", "aria-label": "Foto groß ansehen",
      on: { click: () => bigImage("Raumfoto", fileUrl(e.image)) } }, h("img", { src: fileUrl(e.image), alt: "Raumfoto", loading: "lazy", on: { error: gone } })) : h("div", { class: "as-bub" }, e.text || "Foto"));
    if (e.kind === "audio") return bubble(e, h("div", { class: "as-bub" }, "🎤 " + (e.text || "Sprachnachricht")));
    if (e.kind === "preview") return previewNode(e);
    if (e.kind === "result") return resultNode(e);
    return bubble(e, h("div", { class: "as-bub" }, e.text || ""));
  }

  function previewNode(e) {
    const { head, rest } = splitText(e.text), src = e.image ? fileUrl(e.image) : null;
    return bubble(e, src ? h("button", { class: "as-pic", type: "button", "aria-label": "Vorschau groß ansehen",
      on: { click: () => bigImage("Vorschau", src, h("p", { class: "muted", style: "margin:0;white-space:pre-wrap" }, rest)) } },
    h("img", { src, alt: "Vorschau: Möbel im Raumfoto eingezeichnet", loading: "lazy", on: { error: gone } })) : null,
    h("div", { class: "as-bub" }, h("b", {}, head), rest),
    h("a", { class: "btn ghost", href: `#/p/${pid}/platzieren`, style: "align-self:flex-start" }, "Im Foto selbst verschieben …"));
  }

  function resultNode(e) {
    const d = e.data || {}, { head, rest } = splitText(e.text);
    const vs = (d.variants || []).filter(v => v && v.image);
    const grid = vs.length ? h("div", { class: "as-res" }, vs.map((v, i) => {
      const sc = scoreOf(v);
      return h("button", { class: "as-pic", type: "button", "aria-label": `Bild ${i + 1} groß ansehen`, on: { click: () => bigImage(`Bild ${i + 1}`, fileUrl(v.image),
        h("p", { style: "margin:0" }, h("span", { class: `badge ${sc.tone}` }, sc.text), " ", v.comment || ""),
        h("a", { class: "btn secondary", href: `#/p/${pid}/ergebnis` }, "Im Ergebnis ansehen und an den Kunden senden")) } },
      h("img", { src: fileUrl(v.image), alt: `Bild ${i + 1}`, loading: "lazy", on: { error: gone } }), h("span", { class: `badge ${sc.tone}` }, sc.text));
    })) : null;
    const links = resultLinks(d, pid);
    const offer = d.offer_text ? h("details", { class: "details as-offer" }, h("summary", {}, "Angebotstext"),
      h("div", {}, h("pre", {}, d.offer_text), h("button", { class: "btn secondary", type: "button", on: { click: async () => {
        if (await copyText(d.offer_text) === "copied") toast("Angebotstext kopiert.", { kind: "ok" });
      } } }, "Kopieren"))) : null;
    return bubble(e, grid, h("div", { class: "as-bub" }, h("b", {}, head), rest ? h("details", {}, h("summary", { style: "min-height:44px;display:flex;align-items:center;cursor:pointer" }, "Bewertungen"), rest) : null),
      offer, links.length ? h("div", { class: "as-links" }, links.map(l => h("a", { class: "btn secondary", href: l.href, download: "", target: l.href.endsWith(".pdf") ? "_blank" : null,
        html: icon(l.icon) }, l.label))) : null,
      h("a", { class: "btn primary", href: `#/p/${pid}/ergebnis`, style: "align-self:flex-start" }, "Zum Ergebnis & Kunde"));
  }

  function drawLog(had) {
    const atBottom = innerHeight + scrollY >= document.documentElement.scrollHeight - 120;
    const shown = new Set([...log.children].map(li => +li.dataset.n));
    const stick = (!had && events.length > 0) || atBottom, down = () => scrollTo(0, document.documentElement.scrollHeight);
    for (const e of events) if (!shown.has(e.n)) {
      const node = eventNode(e);
      log.append(node);
      if (stick) node.querySelectorAll("img").forEach(i => i.addEventListener("load", down, { once: true }));
    }
    if (stick) requestAnimationFrame(down);
  }

  drawHead(); drawComposer();
  poll();
  return {
    unmount() {
      dead = true; clearTimeout(timer); document.removeEventListener("visibilitychange", onVis);
      if (mic.recording) mic.cancel();
      ctx.actions(null);
    },
  };
}

// ---------------------------------------------------------------- new order by photo + voice

function mountNew(root, ctx) {
  let photo = null, photoUrl = null, audio = null, audioUrl = null, audioName = null, busy = false, dead = false;
  ctx.title?.("Neuer Auftrag per Foto + Sprache");

  const photoBox = h("div", { class: "col", style: "gap:8px" });
  const audioBox = h("div", { class: "col", style: "gap:8px" });
  const text = h("textarea", { maxLength: MAX_TEXT, rows: 3, "aria-label": "Beschreibung (statt sprechen)",
    placeholder: "oder tippen: z. B. An die Rückwand ein Einbauschrank, zwei Meter breit, raumhoch, Weißlack, grifflos. Der alte Schrank kommt weg." });
  const title = h("input", { type: "text", maxLength: 200, autocomplete: "off", "aria-label": "Titel", placeholder: "z. B. Müller – Einbauschrank Flur" });
  const msg = h("p", { class: "hint", role: "status" }, "Der Assistent vermisst den Raum, plant und zeigt zuerst eine Vorschau (1–2 Min.). Bilder rechnet er erst nach Ihrem „Ja“.");
  const recent = h("div");

  const choose = async camera => {
    const f = await pickImage({ camera });   // first, inside the tap (iOS drops the gesture after an await)
    if (!f) return;
    photo = await shrink(f, 2560);
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = URL.createObjectURL(photo);
    drawPhoto();
  };
  function drawPhoto() {
    photoBox.replaceChildren(...[...(photo
      ? [h("img", { class: "photo", src: photoUrl, alt: "Raumfoto" }), h("div", { class: "acts" },
        h("button", { class: "btn secondary", type: "button", on: { click: () => choose(true) } }, "Neu fotografieren"),
        h("button", { class: "btn ghost", type: "button", on: { click: () => choose(false) } }, "Anderes Foto wählen"))]
      : [h("div", { class: "acts" },
        h("button", { class: "btn primary", type: "button", html: icon("camera"), on: { click: () => choose(true) } }, "Raum fotografieren"),
        h("button", { class: "btn secondary", type: "button", html: icon("image"), on: { click: () => choose(false) } }, "Foto wählen"))])].filter(Boolean));
  }
  const mic = voiceButton({ label: "Sprechen", maxSeconds: 300, onAudio: async (blob, name) => {
    audio = blob; audioName = name;
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(blob);
    drawAudio();
  } });
  function drawAudio() {
    audioBox.replaceChildren(...[...(audio
      ? [h("p", { style: "margin:0" }, "✓ Sprachaufnahme bereit – wird beim Start übertragen."), h("audio", { controls: true, src: audioUrl, preload: "metadata" }),
        h("div", { class: "acts" }, h("button", { class: "btn ghost", type: "button", on: { click: () => { audio = null; drawAudio(); } } }, "Aufnahme verwerfen"))]
      : [mic.el, mic.mode === "file" ? h("button", { class: "btn ghost", type: "button", style: "align-self:flex-start", on: { click: micHelp } }, "Warum geht das Mikrofon nicht?") : null])].filter(Boolean));
  }

  const startBtn = h("button", { class: "btn primary", type: "button", "data-primary": "1", on: { click: start } }, "Assistent starten");
  async function start() {
    if (busy) return;
    const why = startProblem({ photo, audio, text: text.value });
    if (why) { toast(why, { kind: "err" }); return; }
    if (!photo && !(await confirmDialog({ title: "Ohne Raumfoto starten?", text: "Der Assistent fragt dann zuerst nach einem Foto.", ok: "Trotzdem starten" }))) return;
    const fd = new FormData();
    if (photo) fd.append("photo", photo, "raum.jpg");
    if (audio) fd.append("audio", audio, audioName || "aufnahme.webm");
    if (text.value.trim()) fd.append("text", text.value.trim());
    if (title.value.trim()) fd.append("title", title.value.trim());
    busy = true; startBtn.disabled = true; startBtn.classList.add("busy");
    msg.textContent = "Wird übertragen …";
    try {
      const r = await api("POST", "/api/agent/start", fd);
      if (!dead) ctx.nav(`#/p/${r.pid}/assistent`);
    } catch (e) {
      toast(e.message, { kind: "err" });
      msg.textContent = "Nicht gestartet – bitte nochmal versuchen.";
    } finally { busy = false; startBtn.disabled = false; startBtn.classList.remove("busy"); }
  }

  async function loadRecent() {
    let list = [];
    try { list = (await api("GET", "/api/agent?limit=8")).conversations || []; } catch { return; }
    if (dead || !list.length) return;
    recent.replaceChildren(h("h2", { class: "section-title" }, "Zuletzt mit dem Assistenten"), h("ul", { class: "as-recent" }, list.map(c => {
      const [label, tone] = STATE_DE[c.state] || ["", "muted"];
      return h("li", {}, h("a", { href: `#/p/${encodeURIComponent(c.pid)}/assistent` },
        c.thumb ? h("img", { class: "th", src: img(c.thumb), alt: "", loading: "lazy", on: { error: gone } }) : h("span", { class: "th", "aria-hidden": "true" }),
        h("span", { class: "t" }, c.title || "Ohne Titel", " ", label ? h("span", { class: `badge ${tone}` }, label) : null),
        h("span", { class: "l" }, c.last || fmt.ago(c.updated))));
    })));
  }

  drawPhoto(); drawAudio();
  root.replaceChildren(h("div", { class: "as-new" },
    h("section", { class: "card" }, h("h2", {}, "1. Raumfoto"), h("p", { class: "hint" }, "Den ganzen Bereich fotografieren, in den gebaut wird – möglichst mit Boden und Decke."), photoBox),
    h("section", { class: "card" }, h("h2", {}, "2. Sagen, was hin soll"),
      h("p", { class: "hint" }, "Mit Maßen, Holz, Griffen – und was weg soll. Fehlt etwas, fragt der Assistent nach."), audioBox, text),
    h("section", { class: "card" }, h("h2", {}, "3. Titel (optional)"), title,
      h("p", { class: "hint" }, "Ohne Titel benennt der Assistent den Auftrag selbst.")),
    msg, recent));
  ctx.actions([startBtn]);
  loadRecent();
  return {
    unmount() { dead = true; if (mic.recording) mic.cancel(); [photoUrl, audioUrl].forEach(u => u && URL.revokeObjectURL(u)); ctx.actions(null); },
  };
}
