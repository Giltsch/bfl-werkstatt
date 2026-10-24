/* „Per Sprache beschreiben“ in step ② Möbel: speak (or type) → /api/voice/transcribe → /api/workshop/{pid}/understand
   (preview, nothing saved) → „Verstanden:“ card with the pieces, open questions and notes → „Übernehmen“ sends the ops
   through store.patch (the normal PATCH path, so conflicts, offline queue and undo work as for every other edit).
   Pure helpers are exported for tests/ws_system.test.mjs; the DOM lives in understandCard(). */
import { h, api, toast, fmt } from "./ui.js";
import { voiceButton, transcribe, micHelp } from "./voice.js";
import { getPath } from "./store.js";

export const FIELD_DE = { type: "Möbelart", w: "Breite", h: "Höhe", d: "Tiefe", elev: "Abstand vom Boden" };
const clone = v => (v == null ? v : JSON.parse(JSON.stringify(v)));

/** „Einbauschrank“, „220 × raumhoch (255) × 60 cm“, details and location of one understood piece. */
export function pieceLine(p, catalog = {}) {
  const t = p.type && catalog.pieces?.[p.type];
  const title = t ? t.de : (p.text || "").trim().slice(0, 40) || "Möbel";
  const hTxt = p.full_height && p.h ? `raumhoch (${p.h})` : p.h ?? "–";
  const dims = [p.w, p.h, p.d].some(x => x != null) ? `${p.w ?? "–"} × ${hTxt} × ${p.d ?? "–"} cm` : "";
  const mat = p.material === "custom" ? (p.material_custom || "eigene Oberfläche") : catalog.materials?.[p.material]?.de?.replace(/…$/, "") || "";
  const hd = p.handles ? catalog.handles?.[p.handles]?.de || "" : "";
  const details = [mat, hd, t && p.text ? p.text : "", +p.elev > 0 ? `hängt ${p.elev} cm hoch` : ""].filter(Boolean);
  return { title, dims, details, where: p.where || "", changes: p.changes || [], correction: !!p.ref };
}

/** The result with one question answered: the matching add_piece / set op and the piece row carry the value. */
export function withAnswer(res, q, value) {
  const out = clone(res);
  out.questions = (out.questions || []).filter(x => !(x.piece === q.piece && x.field === q.field));
  const row = q.piece != null ? (out.pieces || [])[q.piece] : null;
  if (!row || !q.field || q.field === "type") return out;
  row[q.field] = value;
  const add = (out.ops || []).find(o => o.op === "add_piece" && o.value?.id === row.id);
  if (add) { add.value[q.field] = value; return out; }
  const path = `pieces.${row.id}.${q.field}`, set = (out.ops || []).find(o => o.op === "set" && o.path === path);
  if (set) set.value = value;
  else (out.ops ||= []).push({ op: "set", path, value });
  return out;
}

/** Remaining questions answered with their defaults (what „ja“ means in the assistant); null when one has none. */
export function withDefaults(res) {
  let out = res;
  for (const q of res.questions || []) {
    if (q.default == null) return null;
    out = withAnswer(out, q, q.default);
  }
  return out;
}

/** Ops that undo `ops` on `doc` (the doc before they were applied), in reverse order. */
export function undoOps(doc, ops) {
  const pieces = doc?.pieces || [], out = [];
  for (const op of ops || []) {
    if (op.op === "set") out.push({ op: "set", path: op.path, value: clone(getPath(doc, op.path) ?? null) });
    else if (op.op === "add_piece" && op.value?.id) out.push({ op: "remove_piece", id: op.value.id });
    else if (op.op === "remove_piece") {
      const gone = pieces.find(p => p.id === op.id);
      if (gone) {
        if (op.replacement?.id) out.push({ op: "remove_piece", id: op.replacement.id });
        out.push({ op: "add_piece", value: clone(gone) });
      }
    }
  }
  // re-adds first, removes last: apply_ops always keeps at least one piece
  const rev = out.reverse(), by = k => rev.filter(o => o.op === k);
  return [...by("add_piece"), ...by("set"), ...by("remove_piece")];
}

/** A correction spoken before „Übernehmen“ refers to the whole description: the model reads both together. */
export function joinText(prev, next) {
  const a = String(prev || "").trim(), b = String(next || "").trim();
  if (!a) return b;
  if (!b) return a;
  return `${a}${/[.!?]$/.test(a) ? "" : "."} ${b}`;
}

/** Can „Übernehmen“ run, and with which result (open size questions take their suggestion). */
export function applicable(res) {
  if (!res || res.status !== "ok") return { ok: false, why: null };
  const full = withDefaults(res);
  if (!full) return { ok: false, why: "Bitte genauer sagen, was gebaut werden soll." };
  if (!(full.ops || []).length) return { ok: false, why: res.removes?.length ? null : "Nichts zu übernehmen." };
  return { ok: true, res: full };
}

/** The piece to open after applying: the first new or changed one. */
export const focusOf = res => (res?.pieces || []).map(p => p.id).find(Boolean) || null;

/** Status line while waiting: Whisper's first load takes up to a minute on the shared GPU. */
export const waitText = (stage, loaded) => stage === "hear"
  ? (loaded ? "Spracherkennung läuft …" : "Spracherkennung startet – beim ersten Mal bis zu 1 Minute …")
  : "Wird verstanden … (ca. 10 Sek.)";

// ------------------------------------------------------------------ DOM

function css() {
  if (document.getElementById("ws-vs-css")) return;
  document.head.append(h("style", { id: "ws-vs-css" }, `
.vs{display:flex;flex-direction:column;gap:12px;margin:0}
.vs h3{margin:0;font-size:17px}
.vs .vs-top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap}
.vs .vs-top p{margin:0;flex:1 1 220px}
.vs .vs-in{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.vs textarea{width:100%;min-height:76px;font-size:16px;line-height:1.4}
.vs .vs-acts{display:flex;gap:8px;flex-wrap:wrap}.vs .vs-acts .btn{flex:1 1 150px}
.vs .vs-status{display:flex;flex-direction:column;gap:6px;font-size:15px}
.vs .vs-status:empty,.vs>div:empty{display:none}
.vs .vs-got{border:1px solid var(--accent);background:var(--accent-soft);border-radius:var(--r);padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.vs .vs-got h4{margin:0;font-size:16px}
.vs .vs-piece{display:flex;flex-direction:column;gap:2px;padding-top:8px;border-top:1px solid var(--line)}
.vs .vs-piece:first-of-type{border-top:0;padding-top:0}
.vs .vs-piece b{font-size:16px}
.vs .vs-piece span{font-size:15px;color:var(--text)}
.vs .vs-piece small{font-size:14px;color:var(--muted)}
.vs .vs-q{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:10px;background:var(--panel);border:1px solid var(--line)}
.vs .vs-q .u{display:flex;align-items:center;gap:8px}.vs .vs-q input{width:7.5em;min-height:48px;font-size:18px;text-align:center}
.vs .vs-note{font-size:14px;color:var(--muted);margin:0}
.vs .vs-warn{font-size:15px;color:var(--warn);margin:0}
.vs .linkish{background:none;border:0;padding:0;min-height:44px;color:var(--accent);font:inherit;font-size:14px;text-decoration:underline;cursor:pointer;align-self:flex-start}
`));
}

/** The card. ctx: {store, select(pieceId)} — select opens the piece card after „Übernehmen“. */
export function understandCard({ store, select }) {
  css();
  let res = null, stage = null, err = null, heardLoaded = false, typing = false, lastText = "", seq = 0;
  const pid = () => store.doc?.id;
  const cat = () => store.catalog || { pieces: {}, materials: {}, handles: {} };

  const text = h("textarea", { rows: 2, maxLength: 2000, "aria-label": "Beschreibung",
    placeholder: "z. B. Links neben dem Fenster ein Einbauschrank, zwei-zwanzig breit, raumhoch, Weißlack, drei Türen grifflos" });
  const statusEl = h("div", { class: "vs-status", role: "status", "aria-live": "polite" });
  const gotEl = h("div");
  const mic = voiceButton({ label: "Sprechen", hint: true, onAudio: async (blob, name) => {
    const my = ++seq;
    err = null; stage = "hear"; draw();
    try { heardLoaded = !!(await api("GET", "/api/voice/status"))?.loaded; draw(); } catch { /* only the wording */ }
    let heard;
    try { heard = await transcribe(api, blob, name); }
    catch (e) { if (my === seq) { stage = null; err = e.message || "Spracherkennung nicht möglich."; draw(); } return; }
    if (my !== seq) return;
    if (!heard.text) { stage = null; err = heard.message || "Keine Sprache erkannt – bitte nochmal."; draw(); return; }
    typing = true;
    text.value = res ? joinText(text.value, heard.text) : heard.text;   // before „Übernehmen“ a correction adds to the text
    understand();
  } });
  const typeBtn = h("button", { class: "btn secondary", type: "button", on: { click: () => { typing = true; draw(); text.focus(); } } }, "Tippen statt sprechen");
  const goBtn = h("button", { class: "btn primary", type: "button", on: { click: () => understand() } }, "Verstehen");
  const help = h("button", { class: "linkish", type: "button", on: { click: micHelp } }, "Warum geht das Mikrofon nicht?");
  const el = h("section", { class: "card vs", "aria-label": "Per Sprache beschreiben" },
    h("div", { class: "vs-top" }, h("div", {}, h("h3", {}, "Per Sprache beschreiben"),
      h("p", { class: "vs-note" }, "Sagen, was hin soll – mit Maßen, Holz und Griffen. Fehlt etwas, wird nachgefragt."))),
    h("div", { class: "vs-in" }, mic.el, typeBtn),
    mic.mode === "file" ? help : null,
    h("div", { class: "vs-text" }, text, h("div", { class: "vs-acts", style: "margin-top:8px" }, goBtn,
      h("button", { class: "btn ghost", type: "button", on: { click: reset } }, "Leeren"))),
    statusEl, gotEl);

  text.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) understand(); });

  function reset() { seq++; res = null; err = null; stage = null; text.value = ""; typing = false; draw(); }

  async function understand() {
    const t = text.value.trim();
    if (!t) { err = "Bitte zuerst sprechen oder etwas eintippen."; draw(); return; }
    if (!pid()) return;
    const my = ++seq;
    err = null; stage = "understand"; draw();
    try { await store.flush?.(); } catch { /* the server reads its own copy anyway */ }
    let r;
    try { r = await api("POST", `/api/workshop/${pid()}/understand`, { text: t, active_piece: store.view?.()?.active_piece || null }); }
    catch (e) { if (my === seq) { stage = null; err = e.message; draw(); } return; }
    if (my !== seq) return;
    stage = null; lastText = t;
    if (r.status === "unavailable") { res = null; err = "Die Sprach-KI ist gerade nicht erreichbar. Bitte gleich nochmal – oder unten von Hand eintragen."; }
    else if (r.status !== "ok") { res = null; err = "Das konnte nicht verstanden werden. Bitte anders formulieren."; }
    else res = r;
    draw();
  }

  async function apply() {
    const a = applicable(collect());
    if (!a.ok) return;
    const before = clone(store.doc), ops = a.res.ops, undo = undoOps(before, ops);
    try { await store.patch(ops, { now: true }); }
    catch (e) { toast(e.message || "Übernehmen fehlgeschlagen.", { kind: "err" }); return; }
    const first = focusOf(a.res);
    const said = (a.res.summary_de || "").split(";")[0];
    reset();
    if (first && (store.doc?.pieces || []).some(p => p.id === first)) select?.(first);
    toast(`Übernommen${said ? ": " + said : ""}.`, { kind: "ok", undo: () => store.patch(undo, { now: true }).catch(e => toast(e.message, { kind: "err" })) });
  }

  // the size inputs of open questions → the result with those answers
  function collect() {
    let r = res;
    for (const inp of gotEl.querySelectorAll("input[data-q]")) {
      const q = (res.questions || [])[+inp.dataset.q], v = parseInt(inp.value, 10);
      if (q && v >= 1 && v <= 2000) r = withAnswer(r, q, v);
    }
    return r;
  }

  function draw() {
    const busy = !!stage;
    mic.busy(busy); goBtn.disabled = busy; typeBtn.hidden = typing || !!res;
    el.querySelector(".vs-text").hidden = !(typing || res || text.value);
    statusEl.replaceChildren(...(busy ? [h("span", {}, waitText(stage, heardLoaded)), h("div", { class: "bar indeterminate" }, h("i"))]
      : err ? [h("p", { class: "vs-warn", style: "margin:0" }, err)] : []));
    gotEl.replaceChildren(...(res && !busy ? [got()] : []));
  }

  function got() {
    const c = cat(), box = h("div", { class: "vs-got" }, h("h4", {}, "Verstanden:"));
    const rows = (res.pieces || []).map(p => pieceLine(p, c));
    for (const r of rows) box.append(h("div", { class: "vs-piece" },
      h("b", {}, r.correction ? `${r.title} – Änderung` : r.title),
      r.correction && r.changes.length ? h("span", {}, r.changes.join(", ")) : h("span", {}, [r.dims, ...r.details].filter(Boolean).join(" · ")),
      r.where ? h("small", {}, `Wo: ${r.where}`) : null));
    for (const d of res.drops || []) box.append(h("div", { class: "vs-piece" }, h("b", {}, `${d.label || "Möbel"} – wird gestrichen`)));
    if (!rows.length && !(res.drops || []).length && !(res.removes || []).length) box.append(h("p", { class: "vs-note" }, "Kein Möbel erkannt."));
    (res.questions || []).forEach((q, i) => {
      const row = q.piece != null ? res.pieces?.[q.piece] : null;
      if (q.field === "type" || q.default == null) { box.append(h("div", { class: "vs-q" }, h("b", {}, q.question_de), h("small", { class: "vs-note" }, "Bitte nochmal sprechen oder ergänzen."))); return; }
      const inp = h("input", { type: "number", inputmode: "numeric", min: "1", max: "2000", value: String(q.default), "data-q": String(i),
        "aria-label": `${FIELD_DE[q.field] || q.field}${row ? " " + pieceLine(row, c).title : ""} in cm` });
      box.append(h("label", { class: "vs-q" }, h("b", {}, q.question_de), h("span", { class: "u" }, inp, "cm"),
        h("small", { class: "vs-note" }, `Vorschlag: ${fmt.cm(q.default)} – einfach so lassen oder ändern.`)));
    });
    if ((res.removes || []).length) box.append(h("p", { class: "vs-note" },
      `Aus dem Foto entfernen: ${res.removes.map(r => r.what).join(", ")} – in Schritt 3 mit „Etwas aus dem Bild entfernen“ markieren (oder im Assistenten sagen).`));
    for (const w of res.warnings || []) box.append(h("p", { class: "vs-warn" }, "⚠ " + w));
    if (res.confidence != null && res.confidence < 0.5 && !(res.questions || []).some(q => q.default == null)) box.append(h("p", { class: "vs-warn" }, "⚠ Unsicher verstanden – bitte genau prüfen."));
    const a = applicable(res);
    box.append(...[h("div", { class: "vs-acts" },
      h("button", { class: "btn primary", type: "button", disabled: !a.ok, on: { click: apply } }, "Übernehmen"),
      h("button", { class: "btn ghost", type: "button", on: { click: reset } }, "Verwerfen")),
      a.why ? h("p", { class: "vs-note" }, a.why) : null,
      h("p", { class: "vs-note" }, "Etwas falsch? Einfach nochmal sprechen („nein, achtzig tief“) oder den Text oben ändern und „Verstehen“ tippen.")].filter(Boolean));
    return box;
  }

  draw();
  return { el, reset, get busy() { return !!stage; }, get text() { return lastText; } };
}
