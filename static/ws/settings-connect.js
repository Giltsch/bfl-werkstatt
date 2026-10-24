// Einstellungen › Firmenprofil (profile.py), Kanäle (channels/: Telegram, E-Mail) and API & Webhooks (connect.py).
// Channels and webhooks are OFF by default; switching one on connects the workshop to the internet, so it needs the
// owner's explicit decision (typed confirmation). Secrets are write-only: the server only says whether one is stored.
// settings.js calls the section functions with its small helper bag; pure helpers are exported for the node tests.
import { h, api, toast, confirmDialog, sheet, toggleRow, copyText, fmt } from "./ui.js";

export const SOURCE_DE = { firma: "eigener Wert", paket: "Fachpaket", katalog: "Katalog", standard: "Standard" };
export const DIM_FIELDS = [["w", "Breite"], ["h", "Höhe"], ["d", "Tiefe"], ["elev", "Abstand vom Boden"]];
export const ALIAS_KINDS = [["type", "Möbelart"], ["material", "Holz & Oberfläche"], ["handles", "Griffe"]];
export const ENABLE_WORD = "einschalten";

const intOrNull = v => { const s = String(v ?? "").trim(); if (!s) return null; const n = Math.round(Number(s.replace(",", "."))); return Number.isFinite(n) ? n : NaN; };

/** Merge patch for one type's defaults: an emptied field inherits again (null), a new value is stored, the rest stays. */
export function typePatch(stored, form) {
  const out = {};
  for (const [k, v] of Object.entries(form)) {
    const had = stored?.[k] ?? null;
    if (k === "full_height") { if (v !== (had ?? null) && v !== null) out[k] = v; else if (v === null && had !== null) out[k] = null; continue; }
    if (k === "doors") { const per = v?.per ?? null; if (per !== (had?.per ?? null)) out[k] = per == null ? null : { per }; continue; }
    if (v === null && had !== null) out[k] = null;
    else if (v !== null && v !== had) out[k] = v;
  }
  return out;
}

/** Alias dict changes → merge patch (removed words → null). */
export function aliasPatch(before, after) {
  const out = {};
  for (const [kind] of ALIAS_KINDS) {
    const a = before?.[kind] || {}, b = after?.[kind] || {}, p = {};
    for (const w of Object.keys(a)) if (!(w in b)) p[w] = null;
    for (const [w, v] of Object.entries(b)) if (a[w] !== v) p[w] = v;
    if (Object.keys(p).length) out[kind] = p;
  }
  return out;
}

/** A secret field: typed → replace, empty → keep (omit), explicit removal → null. */
export function secretValue(typed, remove = false) {
  if (remove) return { set: true, value: null };
  const t = String(typed || "").trim();
  return t ? { set: true, value: t } : { set: false };
}

/** „a@b.de, @firma.de“ / one per line → unique trimmed list. */
export function parseList(text) {
  return [...new Set(String(text || "").split(/[\n,;]+/).map(s => s.trim()).filter(Boolean))];
}

/** Telegram chat rows from the form ("123456 Peter") → [{id, name}]; bad ids are reported. */
export function parseChats(text) {
  const chats = [], bad = [];
  for (const line of String(text || "").split("\n").map(s => s.trim()).filter(Boolean)) {
    const m = /^(-?\d{3,20})\s*(.*)$/.exec(line);
    if (m) chats.push({ id: Number(m[1]), name: m[2].trim().slice(0, 60) });
    else bad.push(line);
  }
  return { chats, bad };
}
export const chatsText = list => (list || []).map(c => `${c.id}${c.name ? " " + c.name : ""}`).join("\n");

export function curlExample(origin, key = "wk_…") {
  return `curl -H "Authorization: Bearer ${key}" \\\n  -F photo=@raum.jpg \\\n  -F text="Links ein Einbauschrank, 220 breit, raumhoch, Weißlack" \\\n  ${origin}/api/v1/orders`;
}

/** „läuft seit …“ / „aus“ / „Fehler: …“ line of a channel. */
export function channelLine(ch) {
  const st = ch?.status || {};
  if (st.fatal) return { tone: "bad", text: `Gestoppt: ${st.fatal}` };
  if (ch?.enabled && st.running) return { tone: "ok", text: `Eingeschaltet – verbunden${st.last_poll ? `, zuletzt ${fmt.ago(st.last_poll)}` : ""}` };
  if (ch?.enabled) return { tone: "warn", text: st.last_error ? `Eingeschaltet, Fehler: ${st.last_error}` : "Eingeschaltet, startet beim nächsten Neustart" };
  return { tone: "muted", text: "Aus – keine Verbindung ins Internet" };
}

const errOf = e => (e && e.message) || "Server nicht erreichbar.";
const label = (t, ...kids) => h("label", { class: "field" }, h("span", { class: "label" }, t), ...kids);
const num = (v, attrs = {}) => { const i = h("input", { type: "number", inputmode: "numeric", ...attrs }); i.value = v ?? ""; return i; };
const txt = (v, attrs = {}) => { const i = h("input", { type: "text", autocomplete: "off", ...attrs }); i.value = v ?? ""; return i; };
const select = (opts, cur) => { const s = h("select", {}, opts.map(([v, l]) => h("option", { value: v }, l))); s.value = cur ?? ""; return s; };
const loading = title => h("section", { class: "card" }, h("h2", {}, title), h("p", { class: "hint" }, "Wird geladen …"));

/** Owner decision before anything connects outward: typed confirmation. */
/** The note that a team password is needed first (keys, webhooks, channels: the server is reachable on the LAN). */
function needPassword(text) {
  return h("p", { class: "warnbox" }, text, " ", h("a", { href: "#/einstellungen/zugang" }, "Team-Passwort setzen"));
}

export function confirmOnline(what) {
  return confirmDialog({ title: `${what} einschalten?`, ok: "Einschalten", danger: true, expect: ENABLE_WORD,
    text: `Damit verbindet sich die Werkstatt über das Internet mit ${what === "Webhooks" ? "den eingetragenen Adressen" : what === "Telegram" ? "den Telegram-Servern" : "Ihrem Mail-Server"}. ` +
      `Das entscheidet der Inhaber. Zum Bestätigen „${ENABLE_WORD}“ eintippen.` });
}

// ------------------------------------------------------------------ Firmenprofil

export function profilSection(env) {
  const { store } = env;
  const box = h("div", { style: "display:grid;gap:14px;min-width:0" }, loading("Firmenprofil"));
  let data = null, packs = null, sugg = [];
  const cat = () => store.catalog || { pieces: {}, materials: {}, handles: {} };

  const put = async (patch, ok = "Gespeichert.") => {
    try { data = await api("PUT", "/api/profile", patch); toast(ok, { kind: "ok" }); draw(); return true; }
    catch (e) { toast(errOf(e), { kind: "err" }); return false; }
  };

  async function load() {
    try {
      [data, packs, sugg] = await Promise.all([api("GET", "/api/profile"), api("GET", "/api/profile/packs"),
        api("GET", "/api/profile/suggestions").then(r => r.suggestions || [], () => [])]);
    } catch (e) { box.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Firmenprofil"), h("p", { class: "warnbox" }, errOf(e)))); return; }
    draw();
  }

  function draw() {
    if (!data) return;
    const eff = data.effective || {}, prof = data.profile || {};
    box.replaceChildren(...[trade(eff, prof), suggestions(), company(eff, prof), types(eff, prof), aliases(eff, prof), quality(eff, prof), wording(eff, prof)].filter(Boolean));
  }

  function trade(eff, prof) {
    const list = h("div", { role: "radiogroup", "aria-label": "Fachgebiet", style: "display:grid;gap:8px" },
      (packs?.packs || []).map(p => h("button", { type: "button", role: "radio", "aria-checked": String(p.id === prof.trade),
        class: "toggle-row", style: `display:flex;flex-direction:column;align-items:flex-start;gap:2px;margin:0;${p.id === prof.trade ? "border-color:var(--accent);background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--accent)" : ""}`,
        on: { click: async () => {
          if (p.id === prof.trade) return;
          if (!await confirmDialog({ title: `Fachgebiet „${p.label}“?`, text: "Standardmaße, Begriffe und Qualität kommen dann aus diesem Fachpaket. Eigene Werte für Möbelarten, die es dort nicht gibt, fallen weg.", ok: "Wechseln" })) return;
          put({ trade: p.id }, `Fachgebiet: ${p.label}`);
        } } }, h("span", { class: "tr-title" }, (p.id === prof.trade ? "✓ " : "") + p.label), h("span", { class: "tr-sub" }, p.description))));
    return h("section", { class: "card" }, h("h2", {}, "Fachgebiet"),
      h("p", { class: "hint" }, "Der Assistent füllt damit Maße, Holz und Griffe vor, statt alles zu fragen."), list);
  }

  function suggestions() {
    if (!sugg.length) return null;
    return h("section", { class: "card", style: "border-color:var(--accent)" }, h("h2", {}, "Vorschläge aus Ihren Korrekturen"),
      ...sugg.map(s => h("div", { class: "col", style: "gap:8px;border-top:1px solid var(--line);padding-top:10px" }, h("p", {}, s.text_de),
        h("div", { class: "acts" },
          h("button", { class: "btn primary", type: "button", on: { click: () => act(s, "accept") } }, "Übernehmen"),
          h("button", { class: "btn ghost", type: "button", on: { click: () => act(s, "dismiss") } }, "Nein danke")))));
  }
  async function act(s, what) {
    try {
      const r = await api("POST", `/api/profile/suggestions/${encodeURIComponent(s.id)}/${what}`);
      data = { profile: r.profile, effective: r.effective };
      sugg = sugg.filter(x => x.id !== s.id);
      toast(what === "accept" ? "Übernommen – gilt ab jetzt als Standard." : "Verworfen.", { kind: "ok" });
      draw();
    } catch (e) { toast(errOf(e), { kind: "err" }); }
  }

  function company(eff, prof) {
    const c = prof.company || {}, e = eff.company || {}, mats = cat().materials || {}, hds = cat().handles || {};
    const matName = id => (id || "").startsWith("custom:") ? id.slice(7) : mats[id]?.de?.replace(/…$/, "") || id || "–";
    const mat = select([["", `Wie Fachpaket (${matName(eff.pack?.company?.material || e.material)})`],
      ...Object.entries(mats).filter(([k]) => k !== "custom").map(([k, v]) => [k, v.de.replace(/…$/, "")])], c.material || "");
    const hd = select([["", "Wie Fachpaket"], ...Object.entries(hds).map(([k, v]) => [k, v.de]), ["keine", "keine Griffe"]], c.handles || "");
    const plinth = num(c.plinth, { min: "0", max: "30", placeholder: String(e.plinth ?? "") });
    const board = num(c.board, { min: "8", max: "200", placeholder: String(e.board ?? "") });
    const save = h("button", { class: "btn primary", type: "button", on: { click: () => {
      const p = intOrNull(plinth.value), b = intOrNull(board.value);
      if (Number.isNaN(p) || Number.isNaN(b)) return toast("Bitte ganze Zahlen eingeben.", { kind: "err" });
      put({ company: { material: mat.value || null, handles: hd.value || null, plinth: p, board: b } });
    } } }, "Speichern");
    return h("section", { class: "card" }, h("h2", {}, "Standard für neue Möbel"),
      h("p", { class: "hint" }, `Gilt, wenn beim Auftrag nichts anderes gesagt wird. Jetzt: ${matName(e.material)}, ${e.handles ? hds[e.handles]?.de || e.handles : "keine Griffe"}.`),
      h("div", { class: "row2" }, label("Holz & Oberfläche", mat), label("Griffe", hd)),
      h("div", { class: "row2" }, label("Sockelhöhe (cm)", plinth), label("Plattenstärke (mm)", board)),
      h("div", { class: "acts" }, save));
  }

  function types(eff, prof) {
    const rows = Object.entries(eff.types || {});
    const own = rows.filter(([, t]) => Object.values(t.source || {}).includes("firma")).length;
    return h("details", { class: "details card", style: "padding:0" }, h("summary", {}, `Maße je Möbelart${own ? ` (${own} eigene)` : ""}`),
      h("div", { class: "col", style: "gap:8px" },
        h("p", { class: "hint" }, "Leeres Feld = Wert aus Fachpaket bzw. Katalog. Die Breite misst der Assistent pro Auftrag – hier nur der Vorschlag."),
        ...rows.map(([id, t]) => typeRow(id, t, prof.types?.[id] || {}))));
  }
  function typeRow(id, t, stored) {
    const own = Object.values(t.source || {}).includes("firma");
    const dims = `${t.w ?? "–"} × ${t.full_height ? "raumhoch" : t.h ?? "–"} × ${t.d ?? "–"} cm${t.elev ? `, ${t.elev} cm über Boden` : ""}`;
    const inputs = Object.fromEntries(DIM_FIELDS.map(([k]) => [k, num(stored[k], { min: k === "elev" ? "0" : "1", max: k === "elev" ? "400" : "2000", placeholder: String(t[k] ?? "") })]));
    let full = stored.full_height ?? null;
    const fullRow = toggleRow({ title: "Raumhoch", text: `bis unter die Decke (Fachpaket: ${t.full_height ? "ja" : "nein"})`, checked: !!t.full_height, onChange: v => { full = v; } });
    const per = t.doors ? num(stored.doors?.per, { min: "20", max: "200", placeholder: String(t.doors.per ?? "") }) : null;
    const save = h("button", { class: "btn primary", type: "button", on: { click: () => {
      const form = Object.fromEntries(DIM_FIELDS.map(([k]) => [k, intOrNull(inputs[k].value)]));
      if (Object.values(form).some(Number.isNaN)) return toast("Bitte ganze Zahlen eingeben.", { kind: "err" });
      form.full_height = full;
      if (per) { const n = intOrNull(per.value); if (Number.isNaN(n)) return toast("Bitte ganze Zahlen eingeben.", { kind: "err" }); form.doors = { per: n }; }
      const p = typePatch(stored, form);
      if (!Object.keys(p).length) return toast("Nichts geändert.");
      put({ types: { [id]: p } }, `${t.label}: gespeichert.`);
    } } }, "Speichern");
    const reset = own ? h("button", { class: "btn ghost", type: "button", on: { click: () => put({ types: { [id]: null } }, `${t.label}: Standard wiederhergestellt.`) } }, "Standard wiederherstellen") : null;
    return h("details", { class: "details" }, h("summary", {}, h("span", { style: "display:flex;flex-direction:column;min-width:0" }, t.label,
      h("small", { class: "hint", style: "font-weight:450" }, dims)), own ? h("span", { class: "badge info" }, "eigene Werte") : null),
    h("div", { class: "col", style: "gap:10px" },
      h("div", { class: "row2" }, ...DIM_FIELDS.map(([k, l]) => label(`${l} (cm)`, inputs[k], h("small", { class: "hint" }, SOURCE_DE[t.source?.[k]] || "")))),
      fullRow, per ? label("Eine Tür je … cm Breite", per) : null, h("div", { class: "acts" }, save, reset)));
  }

  function aliases(eff, prof) {
    const mine = JSON.parse(JSON.stringify(prof.aliases || {}));
    const total = Object.values(eff.aliases || {}).reduce((n, d) => n + Object.keys(d || {}).length, 0);
    const mats = cat().materials || {}, hds = cat().handles || {};
    const targets = {
      type: Object.entries(eff.types || {}).map(([k, t]) => [k, t.label]),
      material: Object.entries(mats).filter(([k]) => k !== "custom").map(([k, v]) => [k, v.de.replace(/…$/, "")]),
      handles: [...Object.entries(hds).map(([k, v]) => [k, v.de]), ["keine", "keine Griffe"]] };
    const nameOf = (kind, v) => (targets[kind].find(([k]) => k === v) || [, String(v || "").replace(/^custom:/, "")])[1];
    const groups = ALIAS_KINDS.map(([kind, title]) => {
      const list = Object.entries(mine[kind] || {});
      const word = txt("", { placeholder: "Wort, z. B. Schleiflack", maxLength: 60, "aria-label": `Neues Wort (${title})` });
      const to = select(targets[kind], targets[kind][0]?.[0]);
      to.setAttribute("aria-label", `Bedeutung (${title})`);
      return h("div", { class: "col", style: "gap:8px" }, h("h3", {}, title),
        list.length ? h("div", { class: "list" }, list.map(([w, v]) => h("div", { class: "list-item" },
          h("span", { class: "li-main" }, h("span", { class: "li-title" }, `„${w}“`), h("span", { class: "li-sub" }, `→ ${nameOf(kind, v)}`)),
          h("button", { class: "btn ghost", type: "button", "aria-label": `„${w}“ löschen`, on: { click: () => put({ aliases: { [kind]: { [w]: null } } }, "Gelöscht.") } }, "Löschen"))))
          : h("p", { class: "hint" }, "Noch keine eigenen Wörter."),
        h("div", { class: "row2" }, word, to),
        h("div", { class: "acts" }, h("button", { class: "btn secondary", type: "button", on: { click: () => {
          const w = word.value.trim();
          if (w.length < 2) return toast("Bitte ein Wort mit mindestens 2 Zeichen.", { kind: "err" });
          put({ aliases: aliasPatch(prof.aliases, { ...mine, [kind]: { ...(mine[kind] || {}), [w]: to.value } }) }, `„${w}“ gelernt.`);
        } } }, "＋ Wort hinzufügen")));
    });
    return h("details", { class: "details card", style: "padding:0" }, h("summary", {}, "Eigene Begriffe"),
      h("div", { class: "col", style: "gap:14px" }, h("p", { class: "hint" }, `So sagen Sie es in der Werkstatt – der Assistent versteht dann z. B. „Schleiflack“ als Weißlack. Das Fachpaket kennt schon ${total} Begriffe.`), ...groups));
  }

  function quality(eff, prof) {
    const q = prof.quality ?? null, a = prof.auto_send || {}, lr = prof.learning || {};
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Qualität" }, [[null, `Wie Fachpaket (${eff.quality === "fein" ? "Fein" : "Entwurf"})`], ["entwurf", "Entwurf"], ["fein", "Fein"]]
      .map(([v, l]) => h("button", { type: "button", "aria-pressed": String(q === v), on: { click: () => put({ quality: v }) } }, l)));
    const auto = toggleRow({ title: "Kundenversion automatisch bereitstellen", checked: !!a.enabled,
      text: `Nach guter KI-Bewertung (ab ${fmt.num(a.min_overall ?? 7.5, 1)}${a.require_pass !== false ? ", bestanden" : ""}) legt der Assistent ohne Ansehen eine Kundenversion mit Link und PIN an. Gesendet wird sie erst von Ihnen.`,
      onChange: async v => {
        if (v && !await confirmDialog({ title: "Automatisch bereitstellen?", text: "Dann entsteht eine Kundenversion, ohne dass jemand die Bilder vorher angesehen hat. Die KI-Bewertung kann sich irren.", ok: "Einschalten" })) { auto.set(false); return; }
        put({ auto_send: { enabled: v } });
      } });
    const learn = toggleRow({ title: "Aus Korrekturen lernen", checked: lr.enabled !== false,
      text: `Ändern Sie ${lr.threshold || 3}× denselben Wert, schlägt die Werkstatt ihn als neuen Standard vor. Nichts ändert sich ohne Ihr „Übernehmen“.`,
      onChange: v => put({ learning: { enabled: v } }) });
    return h("section", { class: "card" }, h("h2", {}, "Qualität & Automatik"),
      h("div", { class: "field" }, h("span", { class: "label" }, "Bilder vom Assistenten rechnen in"), seg), auto, learn);
  }

  function wording(eff, prof) {
    const w = prof.wording || {}, pw = eff.wording || {};
    const t = (k, n, ph) => { const a = h("textarea", { maxLength: n, rows: 3, placeholder: ph }); a.value = w[k] || ""; return a; };
    const notes = t("notes_de", 1000, "z. B. Wir bauen grundsätzlich mit Schattenfuge zur Decke."), prompt = t("prompt_en", 600, "English, e.g. flush doors with 3 mm reveals"),
      judge = t("judge_en", 600, "English, e.g. doors must be handleless");
    return h("details", { class: "details card", style: "padding:0" }, h("summary", {}, "Texte für Bild-KI und Prüfung"),
      h("div", { class: "col", style: "gap:10px" },
        h("p", { class: "warnbox" }, "Wird gespeichert, wirkt aber noch nicht auf die Bilder: Bild-KI und Prüfung lesen diese Texte erst nach einem späteren Update."),
        label("Hinweise (deutsch)", notes, h("small", { class: "hint" }, `Fachpaket: ${pw.notes_de || "–"}`)),
        label("Zusatz für die Bild-KI (englisch)", prompt), label("Zusatz für die Prüfung (englisch)", judge),
        h("div", { class: "acts" }, h("button", { class: "btn primary", type: "button", on: { click: () =>
          put({ wording: { notes_de: notes.value.trim(), prompt_en: prompt.value.trim(), judge_en: judge.value.trim() } }) } }, "Speichern"))));
  }

  load();
  return [box];
}

// ------------------------------------------------------------------ Kanäle

export function kanaeleSection() {
  const box = h("div", { style: "display:grid;gap:14px" }, loading("Kanäle"));
  let data = null;

  const save = async (cid, patch, ok = "Gespeichert.") => {
    try { data = await api("PUT", "/api/channels", { [cid]: patch }); toast(ok, { kind: "ok" }); draw(); return true; }
    catch (e) { toast(errOf(e), { kind: "err" }); return false; }
  };
  async function load() {
    try { data = await api("GET", "/api/channels"); } catch (e) { box.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Kanäle"), h("p", { class: "warnbox" }, errOf(e)))); return; }
    draw();
  }
  function draw() {
    const ch = data.channels || {};
    box.replaceChildren(
      h("section", { class: "card warn", role: "note" }, h("h2", {}, "Nur mit Entscheidung des Inhabers"),
        h("p", {}, data.warning || "Ein eingeschalteter Kanal verbindet die Werkstatt mit dem Internet."),
        h("p", { class: "hint" }, "Alle Kanäle sind ausgeschaltet, bis hier jemand bewusst einschaltet. Ausgeschaltet geht nichts ins Internet – auch „Prüfen“ prüft dann nur die Eingaben."),
        data.password_set === false ? needPassword("Einschalten geht erst mit Team-Passwort – sonst könnte jedes Gerät im WLAN die Werkstatt mit dem Internet verbinden.") : null),
      telegram(ch.telegram || {}), mail(ch.mail || {}));
  }

  async function check(cid, form) {
    try {
      const r = await api("POST", `/api/channels/${cid}/check`, { ...form, enabled: false });   // offline only: never connects
      toast(r.issues?.length ? `Noch offen: ${r.issues.join(" ")}` : "Eingaben in Ordnung (nur offline geprüft).", { kind: r.issues?.length ? "err" : "ok", ms: 8000 });
    } catch (e) { toast(errOf(e), { kind: "err" }); }
  }
  async function online(cid, what) {
    if (!await confirmDialog({ title: "Verbindung testen?", text: `Das verbindet sich jetzt mit ${what}. Es wird nichts gesendet oder gelesen.`, ok: "Testen" })) return;
    try {
      const r = await api("POST", `/api/channels/${cid}/check`, {});
      toast(r.ok ? `Verbindung in Ordnung${r.online?.bot ? ` (@${r.online.bot})` : ""}.` : `Fehler: ${r.online?.error || r.issues?.join(" ") || "unbekannt"}`, { kind: r.ok ? "ok" : "err", ms: 8000 });
    } catch (e) { toast(errOf(e), { kind: "err" }); }
  }
  function enableRow(cid, ch, what, form) {
    const line = channelLine(ch);
    const row = toggleRow({ title: ch.enabled ? "Eingeschaltet" : "Ausgeschaltet", text: line.text, checked: !!ch.enabled, onChange: async v => {
      if (v) {
        let f;
        try { f = form(); } catch (e) { row.set(false); return toast(e.message, { kind: "err" }); }
        if (ch.issues?.length) { row.set(false); return toast(`Erst ausfüllen und speichern: ${ch.issues.join(" ")}`, { kind: "err", ms: 8000 }); }
        if (!await confirmOnline(what)) { row.set(false); return; }
        if (!await save(cid, { ...f, enabled: true }, `${what} eingeschaltet.`)) row.set(false);
      } else if (!await save(cid, { enabled: false }, `${what} ausgeschaltet.`)) row.set(true);
    } });
    return h("div", {}, row, h("span", { class: `badge ${line.tone}` }, ch.enabled ? "verbindet ins Internet" : "aus"));
  }
  function secretField(title, st) {
    const inp = h("input", { type: "password", autocomplete: "new-password", placeholder: st?.set ? "gespeichert – nur zum Ändern eintippen" : "" });
    let remove = false;
    const rm = st?.set ? h("button", { class: "btn ghost", type: "button", on: { click: () => { remove = true; inp.value = ""; inp.placeholder = "wird beim Speichern entfernt"; } } }, "Entfernen") : null;
    return { el: label(title, inp, rm, h("small", { class: "hint" }, st?.set ? "Ist gespeichert (wird nie angezeigt)." : "Noch nicht gespeichert.")),
      value: () => secretValue(inp.value, remove) };
  }
  function issues(ch) {
    return ch.issues?.length ? h("ul", { class: "hint", style: "margin:0;padding-left:18px" }, ch.issues.map(x => h("li", {}, x))) : h("p", { class: "hint ok-text" }, "Alle Angaben vollständig.");
  }
  function refused(ch, add) {
    const r = ch.status?.refused || [];
    if (!r.length) return null;
    return h("div", { class: "col", style: "gap:6px" }, h("h3", {}, "Abgewiesen (unbekannt)"),
      h("div", { class: "list" }, r.map(x => h("div", { class: "list-item" },
        h("span", { class: "li-main" }, h("span", { class: "li-title" }, x.name || x.key), h("span", { class: "li-sub" }, `${x.key} · ${x.count || 1}× · ${fmt.ago(x.at)}`)),
        h("button", { class: "btn secondary", type: "button", on: { click: () => add(x) } }, "Freigeben")))));
  }

  function telegram(ch) {
    const token = secretField("Bot-Token (von @BotFather)", ch.token);
    const chats = h("textarea", { rows: 3, placeholder: "123456789 Peter\n-100987654 Werkstatt-Gruppe" }); chats.value = chatsText(ch.allowed_chats);
    const who = txt(ch.who, { maxLength: 40, placeholder: "telegram" });
    const form = () => {
      const { chats: list, bad } = parseChats(chats.value);
      if (bad.length) throw new Error(`Chat-ID ungültig: ${bad[0]}`);
      const f = { allowed_chats: list, who: who.value.trim() }, t = token.value();
      if (t.set) f.token = t.value;
      return f;
    };
    const guard = fn => () => { let f; try { f = form(); } catch (e) { return toast(e.message, { kind: "err" }); } fn(f); };
    return h("section", { class: "card" }, h("h2", {}, "Telegram"),
      h("p", { class: "hint" }, "Foto und Sprachnachricht an den Werkstatt-Bot schicken – Vorschau, Bilder und PDF kommen als Antwort. Nur freigegebene Chats werden bedient."),
      enableRow("telegram", ch, "Telegram", () => form()), issues(ch),
      token.el, label("Freigegebene Chats (eine Chat-ID je Zeile, dahinter ein Name)", chats), label("Erscheint im Auftrag als", who),
      refused(ch, x => save("telegram", { allowed_chats: [...(ch.allowed_chats || []), { id: Number(x.key), name: x.name || "" }] }, "Chat freigegeben.")),
      h("div", { class: "acts" }, h("button", { class: "btn primary", type: "button", on: { click: guard(f => save("telegram", f)) } }, "Speichern"),
        h("button", { class: "btn secondary", type: "button", on: { click: guard(f => check("telegram", f)) } }, "Eingaben prüfen"),
        ch.enabled ? h("button", { class: "btn ghost", type: "button", on: { click: () => online("telegram", "Telegram") } }, "Verbindung testen") : null));
  }

  function mail(ch) {
    const f = {
      imap_host: txt(ch.imap_host, { placeholder: "imap.ihr-anbieter.de", spellcheck: false, autocapitalize: "off" }), imap_port: num(ch.imap_port, { min: "1", max: "65535" }),
      smtp_host: txt(ch.smtp_host, { placeholder: "smtp.ihr-anbieter.de", spellcheck: false, autocapitalize: "off" }), smtp_port: num(ch.smtp_port, { min: "1", max: "65535" }),
      user: txt(ch.user, { autocapitalize: "off", spellcheck: false }), from_addr: txt(ch.from_addr, { type: "email", inputmode: "email", placeholder: "werkstatt@firma.de" }),
      folder: txt(ch.folder || "INBOX"), processed_folder: txt(ch.processed_folder, { placeholder: "leer = bleibt im Posteingang" }),
      who: txt(ch.who, { maxLength: 40, placeholder: "mail" }) };
    const sec = select([["ssl", "SSL/TLS (meist Port 465)"], ["starttls", "STARTTLS (meist Port 587)"]], ch.smtp_security || "ssl");
    const pw = secretField("Passwort", ch.password);
    const senders = h("textarea", { rows: 3, placeholder: "meister@firma.de\n@firma.de" }); senders.value = (ch.allowed_senders || []).join("\n");
    const form = () => {
      const out = { imap_host: f.imap_host.value.trim(), imap_port: intOrNull(f.imap_port.value) || 993, smtp_host: f.smtp_host.value.trim(),
        smtp_port: intOrNull(f.smtp_port.value) || 465, smtp_security: sec.value, user: f.user.value.trim(), from_addr: f.from_addr.value.trim(),
        folder: f.folder.value.trim() || "INBOX", processed_folder: f.processed_folder.value.trim(), who: f.who.value.trim(), allowed_senders: parseList(senders.value) };
      const p = pw.value();
      if (p.set) out.password = p.value;
      return out;
    };
    return h("section", { class: "card" }, h("h2", {}, "E-Mail"),
      h("p", { class: "hint" }, "Fotos und Sprachmemos per Mail an ein eigenes Postfach; die Antwort kommt mit Bildern und PDF zurück. Nur freigegebene Absender werden gelesen."),
      enableRow("mail", ch, "E-Mail", form), issues(ch),
      h("div", { class: "row2" }, label("IMAP-Server (Empfang)", f.imap_host), label("IMAP-Port", f.imap_port)),
      h("div", { class: "row2" }, label("SMTP-Server (Versand)", f.smtp_host), label("SMTP-Port", f.smtp_port)),
      label("Verschlüsselung beim Versand", sec),
      h("div", { class: "row2" }, label("Benutzername", f.user), pw.el),
      label("Absenderadresse", f.from_addr),
      h("div", { class: "row2" }, label("Ordner", f.folder), label("Erledigte verschieben nach", f.processed_folder)),
      label("Freigegebene Absender (Adresse oder @domain, eine je Zeile)", senders), label("Erscheint im Auftrag als", f.who),
      refused(ch, x => save("mail", { allowed_senders: [...(ch.allowed_senders || []), x.key] }, "Absender freigegeben.")),
      h("div", { class: "acts" }, h("button", { class: "btn primary", type: "button", on: { click: () => save("mail", form()) } }, "Speichern"),
        h("button", { class: "btn secondary", type: "button", on: { click: () => check("mail", form()) } }, "Eingaben prüfen"),
        ch.enabled ? h("button", { class: "btn ghost", type: "button", on: { click: () => online("mail", "Ihrem Mail-Server") } }, "Verbindung testen") : null));
  }

  load();
  return [box];
}

// ------------------------------------------------------------------ API & Webhooks

export function apiSection() {
  const keysBox = loading("API-Schlüssel"), hooksBox = loading("Webhooks");
  let keys = [], hooks = null, keysPw = true;
  const origin = typeof location !== "undefined" ? location.origin : "http://werkstatt:7860";

  function shownOnce(title, secret, note, extra) {
    sheet({ title, body: h("div", { class: "col", style: "gap:10px" }, h("p", { class: "warnbox" }, note),
      h("textarea", { class: "copy-area", readOnly: true, value: secret, "aria-label": title, style: "min-height:72px" }), extra || null),
    actions: [{ label: "Kopieren", kind: "secondary", onClick: async () => { if (await copyText(secret) === "copied") toast("Kopiert.", { kind: "ok" }); return false; } },
      { label: "Fertig", kind: "primary" }] });
  }

  async function loadKeys() {
    try { const r = await api("GET", "/api/keys"); keys = r.keys || []; keysPw = r.password_set !== false; } catch (e) { keysBox.replaceChildren(h("h2", {}, "API-Schlüssel"), h("p", { class: "warnbox" }, errOf(e))); return; }
    drawKeys();
  }
  function drawKeys() {
    keysBox.replaceChildren(h("h2", {}, "API-Schlüssel"),
      h("p", { class: "hint" }, "Für eigene Programme, n8n oder Claude (MCP) im Firmennetz: Aufträge mit Foto und Text anlegen und Ergebnisse abholen. Ein Schlüssel sieht nur die Aufträge, die er selbst angelegt hat."),
      keys.length ? h("div", { class: "list" }, keys.map(k => h("div", { class: "list-item" },
        h("span", { class: "li-main" }, h("span", { class: "li-title" }, k.name || "Schlüssel"),
          h("span", { class: "li-sub" }, `${k.hint || ""} · angelegt ${fmt.date(k.created)}${k.last_used ? ` · zuletzt benutzt ${fmt.ago(k.last_used)}` : " · noch nie benutzt"}`)),
        h("button", { class: "btn ghost", style: "color:var(--danger)", type: "button", on: { click: async () => {
          if (!await confirmDialog({ title: "Schlüssel löschen?", text: `„${k.name}“ funktioniert danach nicht mehr. Seine Aufträge bleiben in der Werkstatt.`, ok: "Löschen", danger: true })) return;
          try { await api("DELETE", `/api/keys/${encodeURIComponent(k.id)}`); toast("Gelöscht.", { kind: "ok" }); loadKeys(); } catch (e) { toast(errOf(e), { kind: "err" }); }
        } } }, "Löschen"))))
        : h("p", { class: "hint" }, "Noch kein Schlüssel – die Schnittstelle ist damit für Programme verschlossen."),
      keysPw ? null : needPassword("Neue Schlüssel gehen erst mit Team-Passwort – sonst könnte jedes Gerät im WLAN einen anlegen."),
      h("div", { class: "acts" }, h("button", { class: "btn secondary", type: "button", disabled: !keysPw, on: { click: newKey } }, "＋ Neuer Schlüssel")),
      h("details", { class: "details" }, h("summary", {}, "Beispiel und MCP"),
        h("div", { class: "col", style: "gap:8px" }, h("pre", { class: "copy-area", style: "white-space:pre-wrap;overflow-wrap:anywhere;min-height:0;margin:0" }, curlExample(origin)),
          h("p", { class: "hint" }, "Claude/MCP: tools/mcp_server.py mit WERKSTATT_API_KEY starten (Anleitung in docs/system/connect.md)."))));
  }
  function newKey() {
    const name = txt("", { maxLength: 60, placeholder: "z. B. n8n Büro" });
    sheet({ title: "Neuer API-Schlüssel", body: h("div", {}, label("Name", name), h("p", { class: "hint" }, "Der Schlüssel wird nur einmal angezeigt.")),
      actions: [{ label: "Abbrechen", kind: "ghost" }, { label: "Anlegen", kind: "primary", onClick: async () => {
        if (!name.value.trim()) { name.focus(); return false; }
        try {
          const r = await api("POST", "/api/keys", { name: name.value.trim() });
          shownOnce("Ihr neuer Schlüssel", r.key, r.note || "Nur jetzt sichtbar – bitte sicher aufbewahren.", h("pre", { class: "hint", style: "white-space:pre-wrap;overflow-wrap:anywhere;margin:0" }, curlExample(origin, r.key)));
          loadKeys();
        } catch (e) { toast(errOf(e), { kind: "err" }); return false; }
      } }] });
  }

  async function loadHooks() {
    try { hooks = await api("GET", "/api/webhooks"); } catch (e) { hooksBox.replaceChildren(h("h2", {}, "Webhooks"), h("p", { class: "warnbox" }, errOf(e))); return; }
    drawHooks();
  }
  const EV_DE = { "order.question": "Rückfrage", "order.preview": "Vorschau", "order.result": "Ergebnis" };
  function drawHooks() {
    const master = toggleRow({ title: hooks.enabled ? "Webhooks eingeschaltet" : "Webhooks ausgeschaltet", checked: !!hooks.enabled,
      text: hooks.enabled ? "Ereignisse werden an die eingetragenen Adressen geschickt." : "Aus – es wird nichts verschickt.",
      onChange: async v => {
        if (v && !await confirmOnline("Webhooks")) { master.set(false); return; }
        try { hooks = await api("PUT", "/api/webhooks", { enabled: v }); toast(v ? "Webhooks eingeschaltet." : "Webhooks ausgeschaltet.", { kind: "ok" }); drawHooks(); }
        catch (e) { master.set(!v); toast(errOf(e), { kind: "err" }); }
      } });
    const patch = async (hk, body, ok) => {
      try { await api("PATCH", `/api/webhooks/${encodeURIComponent(hk.id)}`, body); toast(ok, { kind: "ok" }); loadHooks(); } catch (e) { toast(errOf(e), { kind: "err" }); loadHooks(); }
    };
    hooksBox.replaceChildren(h("h2", {}, "Webhooks"),
      h("p", { class: "hint" }, "Meldet Rückfragen, Vorschauen und Ergebnisse von API-Aufträgen an eine Adresse (z. B. n8n). Signiert mit HMAC-SHA256."),
      h("p", { class: "warnbox" }, "Eine Adresse außerhalb des Firmennetzes bekommt Auftragsdaten über das Internet. Einschalten entscheidet der Inhaber."),
      hooks.password_set === false ? needPassword("Einschalten geht erst mit Team-Passwort – sonst könnte jedes Gerät im WLAN Auftragsdaten nach außen leiten.") : null,
      master,
      ...(hooks.hooks || []).map(hk => h("div", { class: "col", style: "gap:8px;border-top:1px solid var(--line);padding-top:10px" },
        h("strong", { style: "overflow-wrap:anywhere" }, hk.url, hk.lan ? h("span", { class: "badge muted", style: "margin-left:6px" }, "Firmennetz") : null),
        h("span", { class: "hint" }, `${(hk.events || []).map(e => EV_DE[e] || e).join(", ")} · Geheimnis ${hk.secret_hint || ""}${hk.last ? ` · zuletzt ${fmt.ago(hk.last.at)}: ${hk.last.ok ? "ok" : hk.last.error || hk.last.status}` : ""}`),
        toggleRow({ title: "Diese Adresse beliefern", checked: !!hk.enabled, onChange: v => patch(hk, { enabled: v }, v ? "Adresse eingeschaltet." : "Adresse ausgeschaltet.") }),
        h("div", { class: "acts" },
          h("button", { class: "btn secondary", type: "button", disabled: !(hooks.enabled && hk.enabled), title: hooks.enabled && hk.enabled ? null : "Erst einschalten",
            on: { click: async () => { try { await api("POST", `/api/webhooks/${encodeURIComponent(hk.id)}/test`); toast("Test-Ping verschickt.", { kind: "ok" }); } catch (e) { toast(errOf(e), { kind: "err" }); } } } }, "Testen"),
          h("button", { class: "btn ghost", style: "color:var(--danger)", type: "button", on: { click: async () => {
            if (!await confirmDialog({ title: "Webhook löschen?", text: hk.url, ok: "Löschen", danger: true })) return;
            try { await api("DELETE", `/api/webhooks/${encodeURIComponent(hk.id)}`); loadHooks(); } catch (e) { toast(errOf(e), { kind: "err" }); }
          } } }, "Löschen")))),
      h("div", { class: "acts" }, h("button", { class: "btn secondary", type: "button", on: { click: newHook } }, "＋ Adresse hinzufügen")));
  }
  function newHook() {
    const url = txt("", { type: "url", inputmode: "url", placeholder: "https://n8n.example.de/webhook/werkstatt", spellcheck: false, autocapitalize: "off" });
    const evs = new Set(hooks?.events || Object.keys(EV_DE));
    const chips = h("div", { class: "chips" }, Object.entries(EV_DE).map(([k, l]) => {
      const b = h("button", { class: "chip", type: "button", "aria-pressed": "true", on: { click: () => {
        evs.has(k) ? evs.delete(k) : evs.add(k); b.setAttribute("aria-pressed", String(evs.has(k)));
      } } }, l);
      return b;
    }));
    const keySel = select([["", "alle API-Aufträge"], ...keys.map(k => [k.id, `nur „${k.name}“`])], "");
    let lan = false;
    const lanRow = toggleRow({ title: "Adresse im Firmennetz", text: "z. B. n8n auf einem anderen Rechner im Büro. Sonst sind nur Adressen im Internet erlaubt – dieser Rechner selbst nie.",
      onChange: v => { lan = v; } });
    sheet({ title: "Webhook-Adresse", body: h("div", {}, label("Adresse", url), lanRow, h("div", { class: "field" }, h("span", { class: "label" }, "Ereignisse"), chips),
      label("Für", keySel), h("p", { class: "hint" }, "Neue Adressen sind ausgeschaltet, bis Sie sie einschalten.")),
    actions: [{ label: "Abbrechen", kind: "ghost" }, { label: "Hinzufügen", kind: "primary", onClick: async () => {
      if (!/^https?:\/\/\S+$/.test(url.value.trim())) { toast("Bitte eine Adresse mit http:// oder https:// angeben.", { kind: "err" }); return false; }
      if (!evs.size) { toast("Bitte mindestens ein Ereignis wählen.", { kind: "err" }); return false; }
      try {
        const r = await api("POST", "/api/webhooks", { url: url.value.trim(), events: [...evs], key: keySel.value || null, lan });
        shownOnce("Geheimnis für die Signatur", r.secret, r.note || "Nur jetzt sichtbar.");
        loadHooks();
      } catch (e) { toast(errOf(e), { kind: "err" }); return false; }
    } }] });
  }

  keysBox.classList.add("card"); hooksBox.classList.add("card");
  loadKeys(); loadHooks();
  return [keysBox, hooksBox];
}
