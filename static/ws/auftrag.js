// Kunde & Auftrag: status, customer, Montageort, appointment (.ics), Angebots-Nr., internal notes, versions, activity,
// export / archive / purge. Inputs go through store.bind (§5.2); every doc string is rendered as text.
import { h, fmt, STATUS, statusLabel, api, toast, confirmDialog, copyText, positionsText, versionText, versionBadge } from "./ui.js";

export const PIPELINE = ["anfrage", "termin", "entwurf", "kunde", "freigegeben", "auftrag", "fertig"];
export const KINDS = ["Aufmaß", "Präsentation", "Montage", "Sonstiges"];
const KIND_MIN = {"Aufmaß": 60, "Präsentation": 60, "Montage": 240, "Sonstiges": 60};
export const VERSION_STATUS = {offen: ["offen", "info"], angesehen: ["angesehen", "info"], freigegeben: ["freigegeben", "ok"],
  aenderung: ["Änderungswunsch", "warn"], zurueckgezogen: ["zurückgezogen", ""]};
const VIA = {link: "per Link", telefon: "telefonisch", papier: "auf Papier", vor_ort: "vor Ort"};

// a backward move (asks first) = both statuses in the pipeline and the target earlier; „Abgesagt“ is outside it
export const isBackward = (from, to) => PIPELINE.includes(from) && PIPELINE.includes(to) && PIPELINE.indexOf(to) < PIPELINE.indexOf(from);

export function nameOf(by, team = []) {
  if (!by) return "";
  const m = team.find(t => t.id === by);
  if (m) return m.name;
  if (by === "kunde") return "Kunde";
  if (by === "system") return "System";
  if (String(by).startsWith("dev:")) return `Gerät ${String(by).slice(4, 8)}`;
  return String(by);
}

const alive = doc => {
  const gone = new Set(doc.deleted_variants || []);
  return (doc.variants || []).filter(v => !gone.has(v.id));
};
// approved proposal first, else the first one chosen with ★
export function positionsVariant(doc) {
  const vs = alive(doc), a = doc.approved && doc.approved.variant_id;
  if (a && vs.some(v => v.id === a)) return a;
  const fav = vs.find(v => v.fav);
  return fav ? fav.id : null;
}

export const telHref = p => { const n = String(p || "").replace(/[^\d+]/g, ""); return n.length >= 3 ? `tel:${n}` : null; };
export const mailHref = (e, subject = "") => /^[^\s@<>"]+@[^\s@<>"]+\.[^\s@<>"]+$/.test(String(e || "").trim())
  ? `mailto:${String(e).trim()}${subject ? "?subject=" + encodeURIComponent(subject) : ""}` : null;
export function routeHref(c = {}) {
  const q = [c.street, [c.zip, c.city].filter(Boolean).join(" ")].map(s => String(s || "").trim()).filter(Boolean).join(", ");
  return q ? `https://www.openstreetmap.org/search?query=${encodeURIComponent(q)}` : null;
}

// RFC 5545 text: escape \ ; , and newlines, fold at 75 octets, CRLF
const icsEsc = s => String(s || "").replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
function fold(line) {
  const out = [], enc = new TextEncoder();
  let cur = "";
  for (const ch of line) {
    if (enc.encode(cur + ch).length > (out.length ? 74 : 75)) { out.push(cur); cur = ""; }
    cur += ch;
  }
  out.push(cur);
  return out.join("\r\n ");
}
const icsTime = d => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}T` +
  `${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}00`;

// Calendar entry for the appointment in local (floating) time; internal notes never go in.
export function icsText(doc, {company = {}, now = new Date()} = {}) {
  const ap = doc.appointment;
  if (!ap || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(ap.at || "")) return null;
  const start = new Date(ap.at.slice(0, 16));
  const end = new Date(start.getTime() + (KIND_MIN[ap.what] || 60) * 60000);
  const c = doc.contact || {};
  const who = doc.customer || doc.title || "Kunde";
  const loc = [c.street, [c.zip, c.city].filter(Boolean).join(" ")].filter(Boolean).join(", ");
  const desc = [doc.title && doc.title !== who ? `Projekt: ${doc.title}` : "", doc.ref ? `Vorgang ${doc.ref}` : "",
    doc.ext_ref ? `Angebot Nr. ${doc.ext_ref}` : "", c.phone ? `Telefon: ${c.phone}` : "", c.email ? `E-Mail: ${c.email}` : ""]
    .filter(Boolean).join("\n");
  const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d+/, "");
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", `PRODID:-//${icsEsc(company.name || "Werkstatt")}//Werkstatt//DE`,
    "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
    `UID:${doc.id || "auftrag"}-${icsTime(start)}@werkstatt`, `DTSTAMP:${stamp}`,
    `DTSTART:${icsTime(start)}`, `DTEND:${icsTime(end)}`,
    `SUMMARY:${icsEsc(`${ap.what || "Termin"}: ${who}`)}`,
    loc ? `LOCATION:${icsEsc(loc)}` : "", desc ? `DESCRIPTION:${icsEsc(desc)}` : "",
    "END:VEVENT", "END:VCALENDAR"].filter(Boolean);
  return lines.map(fold).join("\r\n") + "\r\n";
}
export const icsName = doc => `Termin_${String(doc.customer || doc.title || "Auftrag").replace(/[^\wäöüÄÖÜß-]+/g, "_").slice(0, 40)}.ics`;

const el = (tag, attrs = {}, ...kids) => h(tag, Object.fromEntries(Object.entries(attrs).filter(([, v]) => v != null && v !== false)),
  ...kids.flat(9).filter(k => k != null && k !== false && k !== ""));

function css() {
  if (document.getElementById("ws-auftrag-css")) return;
  document.head.append(el("style", {id: "ws-auftrag-css"}, `
.auftrag{display:grid;gap:14px;padding:12px 0 20px;grid-template-columns:minmax(0,1fr)}
.auftrag .card{display:grid;gap:12px;min-width:0;margin:0}
.auftrag .field,.auftrag .list,.auftrag p{margin:0}
.auftrag h2{font-size:19px;margin:0}
.auftrag .row2{display:grid;gap:12px;grid-template-columns:minmax(0,1fr)}
.auftrag .zip{display:grid;gap:12px;grid-template-columns:minmax(0,7rem) minmax(0,1fr)}
.auftrag .acts{display:flex;flex-wrap:wrap;gap:8px}
.auftrag .acts .btn{flex:1 1 150px}
.auftrag .steps{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(0,1fr))}
.auftrag .steps .chip{justify-content:flex-start;width:100%;min-height:48px;white-space:normal;text-align:left;border-radius:12px;line-height:1.25}
.auftrag .steps .chip.done{opacity:.85}
.auftrag .steps .chip.cancel{grid-column:1/-1}
.auftrag .seg>button{flex:1 1 7rem}
.auftrag textarea{min-height:120px}
.auftrag .act-list{list-style:none;margin:0;padding:0;display:grid;gap:0}
.auftrag .act-list li{padding:10px 0;border-top:1px solid var(--line);display:grid;gap:2px;overflow-wrap:anywhere}
.auftrag .act-list .w{color:var(--muted);font-size:14px}
.auftrag .ver{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;min-height:48px;text-decoration:none;color:var(--text)}
.auftrag .hint{color:var(--muted);font-size:14px;margin:0}
.auftrag .danger-zone{border-color:var(--danger)}
@media (min-width:700px){.auftrag .row2{grid-template-columns:repeat(2,minmax(0,1fr))}.auftrag .steps{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media (min-width:1100px){.auftrag{grid-template-columns:minmax(0,1fr) minmax(0,1fr);align-items:start}.auftrag .col{display:grid;gap:14px;min-width:0}}
`));
}

export default {
  mount(root, ctx) {
    css();
    const {store} = ctx, pid = ctx.route.pid;
    let dead = false, shown = 20, kind = "Aufmaß";
    const off = [], unbind = [];
    root.replaceChildren(el("p", {class: "muted", style: "padding:16px 0"}, "Auftrag wird geladen …"));

    const field = (label, input, hint) => el("label", {class: "field"}, el("span", {class: "label"}, label), input, hint ? el("small", {class: "hint"}, hint) : null);
    const input = (attrs = {}) => el("input", {type: "text", autocomplete: "off", ...attrs});
    const btnLink = (label, cls = "btn secondary") => el("a", {class: cls, role: "button"}, label);
    const setLink = (a, href, ext) => {
      if (href) { a.setAttribute("href", href); a.removeAttribute("aria-disabled"); a.classList.remove("disabled"); a.hidden = false; }
      else { a.removeAttribute("href"); a.hidden = true; }
      if (ext) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
    };

    async function start() {
      try { if (store.pid !== pid || !store.doc) await store.open(pid); }
      catch (e) {
        if (dead) return;
        root.replaceChildren(el("div", {class: "empty"}, el("p", {}, e.status === 404 ? "Auftrag nicht gefunden." : e.message),
          el("a", {class: "btn secondary", href: "#/"}, "Zu den Aufträgen")));
        return;
      }
      if (!dead) build();
    }

    function build() {
      const doc = store.doc;
      ctx.title && ctx.title(doc.title || "Ohne Titel", "Kunde & Auftrag");
      ctx.actions(null);
      const bind = (inp, path, opts) => unbind.push(store.bind(inp, path, opts));

      // Status
      const title = input({maxlength: "200"}), ref = input({maxlength: "20"});
      bind(title, "title"); bind(ref, "ref");
      const steps = el("div", {class: "steps", role: "group", "aria-label": "Status"});
      const statusCard = el("section", {class: "card"}, el("h2", {}, "Status"), steps,
        el("div", {class: "row2"}, field("Titel", title), field("Vorgangs-Nr.", ref)));

      // Kunde
      const customer = input({maxlength: "200", autocapitalize: "words"});
      const phone = input({type: "tel", inputmode: "tel", maxlength: "40"});
      const email = input({type: "email", inputmode: "email", maxlength: "120", autocapitalize: "off", spellcheck: "false"});
      bind(customer, "customer"); bind(phone, "contact.phone"); bind(email, "contact.email");
      const call = btnLink("Anrufen"), mail = btnLink("E-Mail schreiben");
      const kundeCard = el("section", {class: "card"}, el("h2", {}, "Kunde"), field("Name", customer),
        el("div", {class: "row2"}, field("Telefon", phone), field("E-Mail", email)), el("div", {class: "acts"}, call, mail));

      // Montageort
      const street = input({maxlength: "120", autocomplete: "off"}), zip = input({maxlength: "12", inputmode: "numeric"});
      const city = input({maxlength: "80", autocapitalize: "words"});
      bind(street, "contact.street"); bind(zip, "contact.zip"); bind(city, "contact.city");
      const route = btnLink("Route");
      const ortCard = el("section", {class: "card"}, el("h2", {}, "Montageort"), field("Straße und Nr.", street),
        el("div", {class: "zip"}, field("PLZ", zip), field("Ort", city)), el("div", {class: "acts"}, route));

      // Termin (one doc value {at, what} edited by three controls)
      const date = el("input", {type: "date"}), time = el("input", {type: "time", step: "300"});
      const apAt = (d, t) => d ? {at: `${d}T${t || "09:00"}`, what: (store.doc.appointment || {}).what || kind} : null;
      bind(date, "appointment", {parse: v => apAt(v, time.value), format: a => a && a.at ? a.at.slice(0, 10) : "", debounce: 600});
      bind(time, "appointment", {parse: v => apAt(date.value, v), format: a => a && a.at ? a.at.slice(11, 16) : "", debounce: 600});
      const kinds = el("div", {class: "seg", role: "group", "aria-label": "Art"});
      const ics = el("button", {type: "button", class: "btn secondary", on: {click: downloadIcs}}, "In Kalender (.ics)");
      const clearAp = el("button", {type: "button", class: "btn ghost", on: {click: () => store.patch([{op: "set", path: "appointment", value: null}], {now: true})}}, "Termin entfernen");
      const terminCard = el("section", {class: "card"}, el("h2", {}, "Termin"),
        el("div", {class: "row2"}, field("Datum", date), field("Uhrzeit", time)),
        el("div", {class: "field"}, el("span", {class: "label"}, "Art"), kinds), el("div", {class: "acts"}, ics, clearAp));

      // Angebot
      const ext = input({maxlength: "60"});
      bind(ext, "ext_ref");
      const copy = el("button", {type: "button", class: "btn secondary", on: {click: copyPositions}}, "Positionstext kopieren");
      const copyHint = el("p", {class: "hint"});
      const angebotCard = el("section", {class: "card"}, el("h2", {}, "Angebot"),
        field("Angebots-Nr. (aus Ihrer Handwerkersoftware)", ext), el("div", {class: "acts"}, copy), copyHint);

      // Interne Notizen
      const notes = el("textarea", {maxlength: "4000", rows: "5"});
      bind(notes, "internal_notes");
      const notesCard = el("section", {class: "card"}, el("h2", {}, "Interne Notizen"),
        field("Notizen", notes, "Nur intern – erscheint nie beim Kunden."));

      const versions = el("div", {class: "list"});
      const versCard = el("section", {class: "card"}, el("h2", {}, "Versionen"), versions);
      const acts = el("ul", {class: "act-list"}), more = el("button", {type: "button", class: "btn ghost", on: {click: () => { shown += 20; renderActivity(); }}}, "Mehr anzeigen");
      const actCard = el("section", {class: "card"}, el("h2", {}, "Verlauf"), acts, more);
      const exportA = el("a", {class: "btn secondary", href: `/api/workshop/${encodeURIComponent(pid)}/export`, download: ""}, "Alle Daten exportieren (ZIP)");
      const archive = el("button", {type: "button", class: "btn secondary", on: {click: toggleArchive}});
      const purge = el("button", {type: "button", class: "btn danger", on: {click: doPurge}}, "Endgültig löschen …");
      const dataCard = el("section", {class: "card danger-zone"}, el("h2", {}, "Daten & Datenschutz"), el("div", {class: "acts"}, exportA, archive, purge));

      for (const [inp, fn] of [[phone, renderLinks], [email, renderLinks], [street, renderLinks], [zip, renderLinks], [city, renderLinks]])
        inp.addEventListener("input", fn);
      root.replaceChildren(el("div", {class: "auftrag"},
        el("div", {class: "col"}, statusCard, kundeCard, ortCard, terminCard, angebotCard),
        el("div", {class: "col"}, notesCard, versCard, actCard, dataCard)));

      function renderStatus() {
        const cur = store.doc.status, ci = PIPELINE.indexOf(cur);
        steps.replaceChildren(...[...PIPELINE, "abgesagt"].map((id, i) => el("button", {type: "button",
          class: "chip" + (id === cur ? " active" : "") + (ci >= 0 && i < ci ? " done" : "") + (id === "abgesagt" ? " cancel" : ""),
          "aria-pressed": String(id === cur), on: {click: () => setStatus(id)}},
          (id === "abgesagt" ? "" : `${i + 1}. `) + statusLabel(id) + (ci >= 0 && i < ci ? " ✓" : ""))));
      }
      function renderLinks() {
        const c = {phone: phone.value, email: email.value, street: street.value, zip: zip.value, city: city.value};
        setLink(call, telHref(c.phone)); setLink(mail, mailHref(c.email, store.doc.title)); setLink(route, routeHref(c), true);
      }
      function renderTermin() {
        const ap = store.doc.appointment;
        if (ap && ap.what) kind = ap.what;
        kinds.replaceChildren(...KINDS.map(k => el("button", {type: "button", "aria-pressed": String(k === kind), on: {click: () => {
            kind = k; renderTermin();
            const a = store.doc.appointment;
            if (a && a.what !== k) store.patch([{op: "set", path: "appointment", value: {...a, what: k}}], {now: true});
          }}}, k)));
        ics.hidden = clearAp.hidden = !ap;
      }
      function renderAngebot() {
        const vid = positionsVariant(store.doc);
        copy.disabled = !vid;
        copyHint.textContent = vid ? (store.doc.approved ? "Text des freigegebenen Entwurfs." : "Text des mit ★ ausgewählten Entwurfs.")
          : "Erst einen Entwurf mit ★ für den Kunden auswählen.";
      }
      function renderVersions() {
        const vs = [...(store.doc.versions || [])].reverse(), ap = store.doc.approved;
        const rows = vs.map(v => {
          const [label, tone] = versionBadge(v);
          return el("a", {class: "list-item ver", href: `#/p/${encodeURIComponent(pid)}/ergebnis`},
            el("span", {}, versionText(v, id => nameOf(id, store.team))),
            el("span", {class: `badge ${tone}`}, label));
        });
        if (ap) {
          const ver = (store.doc.versions || []).find(v => v.n === ap.version);
          const k = ((ver && ver.snapshot && ver.snapshot.variants) || []).find(x => x.id === ap.variant_id);
          rows.unshift(el("p", {class: "badge ok", style: "white-space:normal;line-height:1.4;padding:8px 12px;font-size:15px;margin:0"},
            `Freigegeben: ${k ? `Entwurf ${k.n} aus ` : ""}Version ${ap.version || "–"}, ${VIA[ap.via] || ap.via || ""}` +
            `${ap.name ? " (" + ap.name + ")" : ""} am ${fmt.date(ap.at)}`));
        }
        versions.replaceChildren(...(rows.length ? rows : [el("p", {class: "hint"}, "Noch nichts an den Kunden gegeben. Das geht im Schritt „Ergebnis & Kunde“.")]));
      }
      function renderActivity() {
        const all = [...(store.doc.activity || [])].reverse();
        acts.replaceChildren(...all.slice(0, shown).map(a => el("li", {},
          el("span", {class: "w"}, [fmt.dateTime(a.at), nameOf(a.by, store.team)].filter(Boolean).join(" · ")),
          el("span", {}, a.text || a.what || ""))));
        if (!all.length) acts.replaceChildren(el("li", {}, el("span", {class: "hint"}, "Noch keine Einträge.")));
        more.hidden = all.length <= shown;
      }
      function renderData() {
        archive.textContent = store.doc.archived_at ? "Aus dem Archiv holen" : "Archivieren";
      }
      function renderAll() {
        if (!store.doc) return;
        ctx.title && ctx.title(store.doc.title || "Ohne Titel", "Kunde & Auftrag");
        renderStatus(); renderLinks(); renderTermin(); renderAngebot(); renderVersions(); renderActivity(); renderData();
      }
      renderAll();
      off.push(store.on("deleted", e => {
        if (dead || (e && e.pid !== pid)) return;
        toast("Dieser Auftrag wurde gelöscht.", {kind: "err"}); ctx.nav("#/");
      }), store.on("doc", () => {
        if (dead) return;
        if (!store.doc || store.doc.id !== pid) return;
        renderAll();
      }));
    }

    async function setStatus(id) {
      const cur = store.doc.status;
      if (id === cur) return;
      if (isBackward(cur, id) && !await confirmDialog({title: "Status ändern", text: `Status zurück auf „${statusLabel(id)}“ setzen?`, ok: "Zurücksetzen"})) return;
      store.patch([{op: "set", path: "status", value: id}], {now: true}).catch(e => toast(e.message, {kind: "err"}));
    }

    function downloadIcs() {
      const text = icsText(store.doc, {company: (store.settings || {}).company || {}});
      if (!text) return;
      const url = URL.createObjectURL(new Blob([text], {type: "text/calendar;charset=utf-8"}));
      const a = el("a", {href: url, download: icsName(store.doc), style: "display:none"});
      document.body.append(a); a.click(); a.remove();   // iOS opens the .ics from the download link in Kalender
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }

    async function copyPositions() {
      const vid = positionsVariant(store.doc);
      if (!vid) return;
      const r = await copyText(positionsText(store.doc, vid, store.catalog));
      if (r === "copied") toast("Text kopiert – in das Angebot einfügen.", {kind: "ok"});
    }

    function toggleArchive() {
      const op = store.doc.archived_at ? "unarchive" : "archive";
      store.patch([{op}], {now: true}).then(() => toast(op === "archive" ? "Archiviert." : "Aus dem Archiv geholt.", {kind: "ok"}),
        e => toast(e.message, {kind: "err"}));
    }

    async function doPurge() {
      const t = store.doc.title || "", keep = ((store.settings || {}).backup || {}).keep || 14;
      const expect = t || "LÖSCHEN";
      const ok = await confirmDialog({title: "Auftrag endgültig löschen?", danger: true, ok: "Endgültig löschen", expect,
        text: "Alle Fotos, Bilder, Blätter und Kundenlinks dieses Auftrags werden gelöscht. Das lässt sich nicht rückgängig machen. " +
          `In älteren Datensicherungen bleibt er bis zu ${keep} Tage erhalten. Zum Bestätigen den Titel eintippen: „${expect}“`});
      if (!ok) return;
      try {
        await store.flush().catch(() => {});
        await api("POST", `/api/workshop/${encodeURIComponent(pid)}/purge`, {confirm: expect});
        store.close();
        toast("Auftrag gelöscht.", {kind: "ok"});
        ctx.nav("#/");
      } catch (e) { toast(e.message, {kind: "err"}); }
    }

    start();
    return {unmount() { dead = true; off.forEach(fn => typeof fn === "function" && fn()); unbind.forEach(fn => typeof fn === "function" && fn()); }};
  },
};
