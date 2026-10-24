// Werkstatt 3 app shell (spec §10.1–10.2, §11): hash router, page frame (header, project bar, footer = dock +
// action bar), immersive phone mode, Rechner chip, identity picker, conflict dialogs, presence, dock, attention.
import {$, h, icon, toast, sheet, choose, menu, api, attention, applyTheme, fmt, statusLabel, statusTone, jobProgress, jobLine} from "./ui.js";
import {store} from "./store.js";

export const STEPS = ["raum", "moebel", "platzieren", "berechnen", "ergebnis"];
export const STEP_NAMES = {raum: "Raum", moebel: "Möbel", platzieren: "Platzieren", berechnen: "Berechnen", ergebnis: "Ergebnis & Kunde",
  auftrag: "Kunde & Auftrag", assistent: "Assistent"};
export const SECTIONS = ["firma", "profil", "team", "zugang", "kundenlink", "kanaele", "api", "datenschutz", "darstellung", "system"];
const MODULES = {dashboard: "./dashboard.js", neu: "./dashboard.js", project: "./editor.js", auftrag: "./auftrag.js", settings: "./settings.js",
  assistent: "./assistent.js", "assistent-neu": "./assistent.js"};
const COLORS = ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9", "#f87171", "#38bdf8"];

export function parseRoute(hash) {
  const [path, qs] = String(hash || "").replace(/^#/, "").split("?");
  const query = Object.fromEntries(new URLSearchParams(qs || ""));
  const s = (path || "/").split("/").filter(Boolean);
  if (!s.length) return {name: "dashboard", query};
  if (s[0] === "neu" && s.length === 1) return {name: "neu", query};
  if (s[0] === "assistent" && s.length === 1) return {name: "assistent-neu", query};
  if (s[0] === "p" && /^[a-z0-9]{4,32}$/.test(s[1] || "")) {
    if (s.length === 2) return {name: "project", pid: s[1], step: null, query};
    if (s.length === 3 && s[2] === "auftrag") return {name: "auftrag", pid: s[1], query};
    if (s.length === 3 && s[2] === "assistent") return {name: "assistent", pid: s[1], query};
    if (s.length === 3 && STEPS.includes(s[2])) return {name: "project", pid: s[1], step: s[2], query};
  }
  if (s[0] === "einstellungen" && (s.length === 1 || (s.length === 2 && SECTIONS.includes(s[1])))) return {name: "settings", section: s[1] || null, query};
  return null;
}

export function initials(name) {
  const p = String(name || "?").trim().split(/\s+/);
  return ((p[0] || "?")[0] + (p.length > 1 ? p[p.length - 1][0] : "")).toUpperCase();
}
const possessive = n => /[sxzß]$/i.test(n) ? `${n}’` : `${n}s`;

const F = {};                          // frame elements
let cur = null, routeSeq = 0, imm = null, viewTitled = false;

export function nav(hash, {replace = false} = {}) {
  if (replace) location.replace(hash);
  else if (location.hash === hash) route();
  else location.hash = hash;
}

function avatar(user, cls = "") {
  if (!user) return h("span", {class: `avatar ${cls}`, style: {background: "var(--panel2)", color: "var(--muted)"}, html: icon("user")});
  return h("span", {class: `avatar ${cls}`, style: {background: user.color || "#59a7ff"}, title: user.name}, initials(user.name));
}

// ---------- frame ----------
function frame() {
  const app = $("#app");
  F.back = h("a", {class: "btn ghost hdr-back", href: "#/", "aria-label": "Zu den Aufträgen", html: icon("back")});
  F.backText = h("span", {class: "hdr-back-text"}, "Aufträge");
  F.back.append(F.backText);
  F.title = h("span", {class: "hdr-title"}, "Werkstatt");
  F.step = h("span", {class: "hdr-step"});
  F.sub = h("span", {class: "hdr-sub"});
  F.immPresence = h("span", {class: "imm-presence avatars"});
  F.chip = h("button", {class: "rchip", type: "button", "aria-label": "Rechner", on: {click: queueSheet}});
  F.avatar = h("button", {class: "avatar-btn", type: "button", "aria-label": "Person und Menü", on: {click: e => appMenu(e.currentTarget)}});
  F.more = h("button", {class: "btn ghost icon-only imm-more", type: "button", "aria-label": "Menü", html: icon("more"), on: {click: e => appMenu(e.currentTarget, true)}});
  F.hdr = h("header", {class: "hdr"}, F.back, h("div", {class: "hdr-titles"}, h("div", {class: "hdr-title-row ellipsis"}, F.title, " ", F.step), F.sub),
    F.immPresence, F.chip, F.avatar, F.more);
  F.offline = h("div", {class: "banner", role: "status", hidden: true}, h("span", {html: icon("wifi")}),
    h("span", {}, "Keine Verbindung – Ihre Änderungen werden gespeichert, sobald die Verbindung zurück ist."));
  F.pbar = h("div", {class: "pbar", hidden: true});
  F.view = h("main", {id: "view", tabindex: "-1"});
  F.dock = h("div", {id: "ws-dock"});
  F.actions = h("div", {id: "ws-actions"});
  F.footer = h("footer", {class: "ws-footer empty"}, F.dock, F.actions);
  app.replaceChildren(F.hdr, F.offline, F.pbar, F.view, F.footer, h("div", {class: "toasts", role: "status", "aria-live": "polite"}));
  const sync = () => {
    const empty = !F.dock.childElementCount && !F.actions.childElementCount;
    F.footer.classList.toggle("empty", empty);
    document.documentElement.style.setProperty("--footer-h", (empty ? 0 : F.footer.offsetHeight) + "px");
  };
  F.syncFooter = sync;
  if (typeof ResizeObserver === "function") new ResizeObserver(sync).observe(F.footer);
  new MutationObserver(sync).observe(F.footer, {childList: true, subtree: true});
  const style = h("style", {}, "@media (max-width:699px){.hdr-back-text{display:none}.tabs .tab{padding:8px 4px;font-size:14px;text-align:center}}.hdr-back{padding:0 10px}.hdr-title-row{line-height:1.2}");
  document.head.append(style);
}

function setActions(nodes) {
  const list = nodes == null ? [] : Array.isArray(nodes) ? nodes : [nodes];
  F.actions.replaceChildren(...list.filter(Boolean));
  F.syncFooter();
}

function setTitle(text, sub = "") {
  viewTitled = true;
  F.title.textContent = text || "";
  F.sub.textContent = sub || "";
  F.sub.hidden = !sub;
  document.title = `Werkstatt – ${text || "Aufträge"}`;
}

function immersive(on, opts = {}) {
  imm = on ? opts : null;
  document.body.classList.toggle("immersive", !!on);
  renderHeader();
  renderDock();
  renderPresence();
  F.syncFooter();
}

function renderHeader() {
  const r = cur && cur.route;
  if (imm) {
    const pid = r && r.pid;
    F.back.hidden = false;
    F.back.setAttribute("href", imm.back || (pid ? `#/p/${pid}/moebel` : "#/"));
    F.back.setAttribute("aria-label", "Zurück");
    F.backText.textContent = "Zurück";
    // the project title stays; the active piece goes into the step line („Schritt 3/5 · Einbauschrank“)
    const d = store.doc && pid && store.doc.id === pid ? store.doc : null;
    if (d) F.title.textContent = d.title || "Ohne Titel";
    const i = r && r.step ? STEPS.indexOf(r.step) + 1 : 3;
    F.step.textContent = `Schritt ${imm.step || i}/${imm.total || STEPS.length}${imm.title ? ` · ${imm.title}` : ""}`;
    return;
  }
  F.step.textContent = "";
  if (r && r.pid && store.doc && store.doc.id === r.pid && r.name === "project") {
    F.title.textContent = store.doc.title || "Ohne Titel";
    F.sub.textContent = store.doc.customer || "";
    F.sub.hidden = !store.doc.customer;
  }
  const top = !r || r.name === "dashboard" || r.name === "neu";
  F.back.hidden = top;
  F.back.setAttribute("href", "#/");
  F.back.setAttribute("aria-label", "Zu den Aufträgen");
  F.backText.textContent = "Aufträge";
  renderNews();
}

// new customer approvals (by link, not yet opened): a count on „‹ Aufträge“ until somebody opens the order
function renderNews() {
  if (imm) return;
  const n = store.news || 0;
  F.backText.textContent = "Aufträge";
  if (n) F.backText.append(h("span", {class: "news-badge", "aria-label": `${n} neue Freigabe${n === 1 ? "" : "n"}`}, String(n)));
  F.back.classList.toggle("has-news", !!n);
}
const seenSent = new Set();
function markSeen(d) {   // opening an order the customer approved by link clears its „Neu“
  const a = d && d.approved;
  if (!a || a.via !== "link" || a.seen_at || seenSent.has(d.id)) return;
  seenSent.add(d.id);
  api("POST", `/api/workshop/${d.id}/approval/seen`).catch(() => seenSent.delete(d.id));
}

// ---------- Rechner chip & queue sheet ----------
export function chipState(queue, online) {
  const q = queue || {};
  const pup = q.pup || {};
  const cap = !online || !q.pup || pup.state === "up" ? null
    : pup.state === "starting" ? {long: "Beschreibungs-KI startet …", short: "KI startet …"} : {long: "Beschreibungs-KI aus – Notbetrieb", short: "Notbetrieb"};
  if (!online) return {tone: "bad", long: "Keine Verbindung", short: "Offline", cap: null};
  const mine = (q.my_jobs || []).filter(j => j.state === "running" || j.position === 0);
  if (mine.length) {
    const eta = Math.max(0, ...mine.map(j => j.eta_done_s || 0));
    return {tone: "mine", long: `Ihr Auftrag läuft · ${fmt.rest(eta)}`, short: fmt.restShort(eta), cap};
  }
  if (q.busy === undefined) return {tone: "start", long: "Rechner startet …", short: "Startet …", cap};
  if (q.busy) return {tone: "busy", long: `Rechner belegt · ca. ${fmt.min(q.wait_s || 0)} Min.`, short: `Belegt · ${fmt.min(q.wait_s || 0)} Min.`, cap};
  return {tone: "free", long: "Rechner frei", short: "Frei", cap};
}

function renderChip() {
  const s = chipState(store.queue, store.online);
  F.chip.dataset.tone = s.tone;
  F.chip.setAttribute("aria-label", [s.long, s.cap && s.cap.long].filter(Boolean).join(" – "));
  F.chip.replaceChildren(h("span", {class: "rdot", "aria-hidden": "true"}),
    h("span", {class: "rtext"}, h("span", {class: "rmain"}, h("span", {class: "rshort"}, s.short), h("span", {class: "rlong"}, s.long)),
      s.cap ? h("span", {class: "rcap"}, h("span", {class: "rshort"}, s.cap.short), h("span", {class: "rlong"}, s.cap.long)) : null));
}

function queueSheet() {
  const q = store.queue || {}, s = chipState(q, store.online), pup = q.pup || {};
  const body = h("div", {}, h("p", {}, s.long.replace(/\.?$/, ".")));
  if (!store.online) body.append(h("p", {class: "muted"}, "Ihre Änderungen werden gespeichert, sobald die Verbindung zurück ist."));
  if (store.online && q.pup && pup.state !== "up") body.append(h("div", {class: "card warn"}, h("p", {}, pup.state === "starting"
    ? "Beschreibungs-KI startet … Andere Berechnungen laufen solange weiter; startet sie nicht rechtzeitig, werden die Bilder ohne sie berechnet (Qualität evtl. geringer)."
    : "Beschreibungs-KI aus – Notbetrieb: Die Bilder werden ohne sie berechnet (Qualität evtl. geringer).")));
  const jobs = q.jobs || [];
  const me = store.me && store.me.user ? store.me.user.id : null;
  if (!jobs.length) body.append(h("p", {class: "muted"}, "Keine Berechnungen in der Warteschlange."));
  else {
    body.append(h("h3", {}, "Warteschlange"));
    const ul = h("ul", {class: "queue-list"});
    let s2;
    for (const j of jobs) {
      const mine = j.owner === me || (store.myJobs() || []).some(x => x.id === j.id);
      const li = h("li", {class: mine ? "mine" : ""},
        h("div", {style: "font-weight:600"}, `${mine ? "Sie" : j.owner_name || "Jemand"} – ${j.project_title || "Auftrag"} (${j.label || "Berechnung"})`),
        h("div", {class: "muted small"}, j.stage_text || ""));
      if (mine && j.project) li.append(h("a", {class: "btn ghost", href: `#/p/${j.project}/ergebnis`, on: {click: () => s2 && s2.close()}}, "Ansehen"));
      ul.append(li);
    }
    body.append(ul);
    s2 = sheet({title: "Rechner", body});
    return;
  }
  sheet({title: "Rechner", body});
}

// ---------- user, identity ----------
function renderUser() {
  const u = store.me && store.me.user;
  F.avatar.replaceChildren(avatar(u));
  F.avatar.setAttribute("aria-label", u ? `${u.name} – Menü` : "Person wählen – Menü");
}

function appMenu(anchor, withHome = false) {
  const items = [];
  if (withHome) items.push({label: "Zu den Aufträgen", icon: "list", onClick: () => nav("#/")});
  items.push({label: "Person wechseln", icon: "user", onClick: switchPerson},
    {label: "Einstellungen", icon: "settings", onClick: () => nav("#/einstellungen")},
    {label: "Bildstudio (Experten)", icon: "image", href: "/studio"},
    {label: "Buch-Werkstatt", icon: "pdf", href: "/book"},
    {label: "System", icon: "chip", onClick: () => nav("#/einstellungen/system")});
  menu(anchor, items, {title: "Menü"});
}

async function switchPerson() {
  if (!(await store.switchUser())) return;
  identitySheet({switching: true});
}

export function identitySheet({switching = false} = {}) {
  const team = store.team || [];
  const first = !team.length;
  let color = COLORS[team.length % COLORS.length], s;
  const choose = async (u) => {
    const r = await api("POST", "/api/me", {id: u.id});
    store.me = {...(store.me || {}), user: r.user || u};
    try { localStorage.removeItem("ws.anon"); } catch { /* ignore */ }
    renderUser();
    s.close();
    if (switching) toast(`Hallo ${u.name}!`, {kind: "ok"});
  };
  const name = h("input", {type: "text", id: "ws-id-name", autocomplete: "given-name", maxLength: 40, enterKeyHint: "done", autofocus: first});
  const err = h("div", {class: "error", hidden: true});
  const swatches = h("div", {class: "swatches", role: "radiogroup", "aria-label": "Farbe"});
  const paint = () => [...swatches.children].forEach(b => b.setAttribute("aria-pressed", String(b.dataset.c === color)));
  for (const c of COLORS) swatches.append(h("button", {class: "swatch", type: "button", "data-c": c, style: {background: c}, "aria-label": `Farbe ${c}`,
    on: {click: () => { color = c; paint(); }}}));
  paint();
  const create = async () => {
    const n = name.value.trim();
    if (!n) { err.textContent = "Bitte Ihren Namen eingeben."; err.hidden = false; name.focus(); return false; }
    try {
      const u = await api("POST", "/api/team", {name: n, color});
      store.team = [...(store.team || []), u];
      if (store.me) store.me.team = store.team;
      await choose(u);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    return false;
  };
  const form = h("div", {class: "col", hidden: !first, style: "gap:0"},
    h("div", {class: "field"}, h("label", {for: "ws-id-name"}, "Ihr Name"), name, err),
    h("div", {class: "field"}, h("span", {class: "label"}, "Farbe"), swatches),
    h("button", {class: "btn primary block", type: "button", on: {click: create}}, "Weiter"));
  name.addEventListener("keydown", e => { if (e.key === "Enter") create(); });
  const body = h("div", {});
  if (!first) {
    body.append(h("p", {}, "Tippen Sie auf Ihren Namen. Dieses Gerät merkt es sich."));
    const me = store.me && store.me.user;
    body.append(h("div", {class: "identity-list"}, team.map(u => h("button", {class: "who", type: "button", "aria-pressed": String(!!me && me.id === u.id),
      on: {click: () => choose(u).catch(e => toast(e.message, {kind: "err"}))}}, avatar(u, "lg"), h("span", {class: "ellipsis"}, u.name)))));
    const neu = h("button", {class: "btn secondary block", type: "button", on: {click: () => { neu.hidden = true; form.hidden = false; name.focus(); }}}, "＋ Ich bin neu hier");
    body.append(neu);
  }
  body.append(form);
  s = sheet({title: first ? "Willkommen! Wie heißen Sie?" : "Wer arbeitet hier?", body, actions: [
    {label: "Ohne Namen weiter", kind: "ghost", onClick: () => { try { localStorage.setItem("ws.anon", "1"); } catch { /* ignore */ } }}]});
  s.el.classList.add("identity-sheet");
  return s;
}

// ---------- project bar & presence ----------
function renderPbar() {
  const r = cur && cur.route;
  const show = !!(r && r.pid);
  F.pbar.hidden = !show;
  if (!show) return;
  const d = store.doc && store.doc.id === r.pid ? store.doc : null;
  const status = d ? d.status : null;
  const tab = (href, label, on) => h("a", {class: "tab", href, "aria-current": on ? "page" : null}, label);
  F.pres = h("span", {class: "avatars"});
  F.presLine = h("div", {class: "presence-line", hidden: true});
  F.saved = h("span", {class: "saved", "aria-live": "polite"});
  F.pbar.replaceChildren(
    h("div", {class: "pbar-row"},
      status ? h("a", {class: `badge ${statusTone(status)}`, href: `#/p/${r.pid}/auftrag`, style: "min-height:44px;padding:6px 12px;font-size:14px;text-decoration:none",
        "aria-label": `Status: ${statusLabel(status)}`}, statusLabel(status)) : null,
      h("span", {class: "pbar-cust"}, d ? [d.ref && `Vorgang ${d.ref}`, d.contact && d.contact.city].filter(Boolean).join(" · ") : ""),
      F.pres, F.saved),
    F.presLine,
    h("nav", {class: "tabs", "aria-label": "Bereiche"}, tab(`#/p/${r.pid}`, "Visualisierung", r.name === "project"),
      tab(`#/p/${r.pid}/auftrag`, "Kunde & Auftrag", r.name === "auftrag"),
      tab(`#/p/${r.pid}/assistent`, "Assistent", r.name === "assistent")));
  renderPresence();
  renderSaved();
}

export function presenceText(p) {
  const who = `${p.name || "Jemand"} (${p.device || "Gerät"})`;
  if (!STEP_NAMES[p.view]) return `${who} hat diesen Auftrag gerade offen`;
  return `${who} ist gerade ${p.view === "auftrag" || p.view === "assistent" ? "bei" : "im Schritt"} „${STEP_NAMES[p.view]}“`;
}

function renderPresence() {
  const list = store.presence || [];
  const av = list.map(p => avatar({name: p.name, color: p.color}, "sm"));
  if (F.pres) {
    F.pres.replaceChildren(...av);
    F.presLine.hidden = !list.length;
    F.presLine.textContent = list.map(presenceText).join(" · ");
  }
  F.immPresence.replaceChildren(...(imm ? list.slice(0, 2).map(p => avatar({name: p.name, color: p.color}, "sm")) : []));
}

function renderSaved() {
  if (!F.saved) return;
  F.saved.textContent = store.saveState === "saved" ? (store.doc ? "Gespeichert" : "") : "Wird gespeichert …";
}

function projectTitle() {
  if (viewTitled || !cur || !cur.route.pid) return;
  const d = store.doc;
  if (d && d.id === cur.route.pid) {
    F.title.textContent = d.title || "Ohne Titel";
    F.sub.textContent = d.customer || "";
    F.sub.hidden = !d.customer;
    document.title = `Werkstatt – ${d.title || "Ohne Titel"}`;
  }
}

// ---------- dock ----------
const progress = jobProgress;

function renderDock() {
  const jobs = (store.myJobs() || []).filter(j => j.project !== store.pid);
  if (imm) {
    F.dock.replaceChildren(...(jobs.length ? [h("div", {class: "dock-line6", role: "progressbar", "aria-label": "Berechnung läuft",
      "aria-valuenow": Math.round(progress(jobs[0]) * 100)}, h("i", {style: {width: `${Math.round(progress(jobs[0]) * 100)}%`}}))] : []));
  } else {
    F.dock.replaceChildren(...jobs.slice(0, 2).map(j => h("button", {class: "dock-card", type: "button",
      on: {click: () => j.project && nav(`#/p/${j.project}/ergebnis`)}},
      h("span", {class: "dock-text"}, h("span", {class: "dock-line"}, `${j.project_title || "Auftrag"} · ${jobLine(j)}`),
        h("span", {class: "bar"}, h("i", {style: {width: `${Math.round(progress(j) * 100)}%`}}))),
      h("span", {html: icon("back"), style: "transform:rotate(180deg);display:inline-flex"}))));
  }
  F.syncFooter();
}

// ---------- conflict dialogs ----------
function valueNode(v) {
  if (v && typeof v === "object" && v.thumb) return h("img", {class: "thumb-val", src: v.thumb, alt: "Foto"});
  return h("span", {}, String(v ?? ""));
}

async function onConflict(evt) {
  if (evt.kind === "conflict") {
    const choices = {};
    for (const c of evt.conflicts) {
      const pics = [c.mineText, c.theirsText].some(x => x && typeof x === "object");
      const text = pics ? `${c.by_name} hat „${c.label}“ gerade geändert.`
        : `${c.by_name} hat „${c.label}“ gerade auf ${c.theirsText} geändert. Sie hatten ${c.mineText} eingetragen.`;
      const body = h("div", {class: "conflict-vals"},
        h("div", {}, h("span", {class: "cv-label"}, "Ihr Wert"), valueNode(c.mineText)),
        h("div", {}, h("span", {class: "cv-label"}, `Wert von ${c.by_name}`), valueNode(c.theirsText)));
      const v = await choose({title: "Gleichzeitig geändert", text, body, dismiss: false, buttons: [
        {label: `${possessive(c.by_name)} Wert behalten`, value: "theirs", kind: "secondary"},
        {label: "Meins übernehmen", value: "mine", kind: "primary"}]});
      choices[c.path] = v || "theirs";
    }
    evt.resolve(choices);
  } else if (evt.kind === "gone" || evt.kind === "locked") {
    toast(evt.message, {kind: "err", ms: 7000});
  } else if (evt.kind === "changed") {
    const v = await choose({title: "Auftrag geändert", text: evt.message, buttons: [
      {label: "Ansehen", value: "view", kind: "secondary"}, {label: "Trotzdem starten", value: "force", kind: "primary"}]});
    evt.resolve(v || "view");
  } else if (evt.kind === "running") {
    const pid = store.pid;
    const v = await choose({title: "Berechnung läuft schon", text: evt.message, buttons: [
      {label: "Trotzdem starten", value: "force", kind: "secondary"}, {label: "Zur laufenden Berechnung", value: "goto", kind: "primary"}]});
    if (v === "goto" && pid) nav(`#/p/${pid}/ergebnis`);
    evt.resolve(v);
  }
}

function wireStore() {
  store.on("conflict", onConflict);
  store.on("toast", e => toast(e.text));
  store.on("error", e => toast(e.message, {kind: "err"}));
  store.on("offline", () => { F.offline.hidden = false; renderChip(); });
  store.on("online", () => { F.offline.hidden = true; renderChip(); });
  store.on("queue", () => { renderChip(); renderDock(); });
  store.on("presence", renderPresence);
  store.on("news", renderNews);
  store.on("me", renderUser);
  store.on("save", renderSaved);
  store.on("restart", () => toast("Der Rechner wurde neu gestartet. Laufende Berechnungen gehen weiter.", {ms: 6000}));
  store.on("deleted", () => { toast("Auftrag gelöscht."); nav("#/"); });
  store.on("doc", e => {
    if (e.source === "load" || e.paths.some(p => ["status", "customer", "ref", "title"].includes(p))) { renderPbar(); projectTitle(); }
    if (cur && cur.route.pid && store.doc && store.doc.id === cur.route.pid) markSeen(store.doc);
    renderSaved();
  });
  store.on("jobdone", ({job}) => {
    if (!job || job.state === "cancelled") return;
    if (job.state === "error") { toast(`Fehler: ${job.error || "Berechnung fehlgeschlagen"} – ${job.project_title || ""}`, {kind: "err"}); return; }
    const n = job.count || (job.results || []).length || 1;
    attention(`✓ ${n === 1 ? "1 Vorschlag" : `${n} Vorschläge`} fertig – ${job.project_title || "Auftrag"}`);
  });
}

// ---------- routing ----------
function placeholder(text) {
  return h("div", {class: "empty"}, h("span", {html: icon("warn")}), h("h2", {}, text),
    h("p", {}, "Bitte die Seite neu laden. Wenn das nicht hilft, ist dieser Teil noch nicht installiert."));
}

function makeCtx(r) {
  const ctx = {store, route: r, nav, actions: setActions, title: setTitle, immersive, toast};
  return ctx;
}

export async function route() {
  const r = parseRoute(location.hash);
  if (!r) { location.replace("#/"); return; }
  const mod = MODULES[r.name];
  store.setView(r.step || r.name);
  if (cur && cur.mod === mod && cur.inst && typeof cur.inst.onRoute === "function" && (cur.route.pid || null) === (r.pid || null)) {
    cur.route = r; cur.ctx.route = r;
    renderHeader(); renderPbar();
    try { cur.inst.onRoute(r); } catch (e) { console.error(e); }
    return;
  }
  const seq = ++routeSeq;
  if (cur && cur.inst) { try { cur.inst.unmount && cur.inst.unmount(); } catch (e) { console.error(e); } }
  immersive(false);
  setActions(null);
  viewTitled = false;
  const ctx = makeCtx(r);
  cur = {route: r, mod, inst: null, ctx};
  F.view.replaceChildren();
  F.view.className = "";
  renderHeader();
  if (r.pid) {
    F.title.textContent = ""; F.sub.textContent = "";
    store.open(r.pid).then(() => { if (seq === routeSeq) { renderPbar(); projectTitle(); } }, e => {
      if (seq !== routeSeq) return;
      toast(e.status === 404 ? "Auftrag nicht gefunden." : e.message, {kind: "err"});
      if (e.status === 404) nav("#/", {replace: true});
    });
  } else {
    store.close();
    setTitle(r.name === "settings" ? "Einstellungen" : r.name === "assistent-neu" ? "Neuer Auftrag per Foto + Sprache" : "Aufträge");
    viewTitled = false;
  }
  renderPbar();
  renderDock();
  let m = null;
  try { m = (await import(mod)).default; } catch (e) { console.error(e); }
  if (seq !== routeSeq) return;
  if (!m || typeof m.mount !== "function") { F.view.replaceChildren(placeholder("Dieser Bereich ist noch nicht verfügbar.")); return; }
  try { cur.inst = m.mount(F.view, ctx) || {}; }
  catch (e) { console.error(e); F.view.replaceChildren(placeholder("Unerwarteter Fehler – bitte die Seite neu laden.")); }
}

async function start() {
  try {
    await store.init();
  } catch (e) {
    if (e && e.status === 0) {
      F.offline.hidden = false; store.online = false; renderChip();
      setTimeout(() => start().then(() => cur && !cur.inst && route()), 5000);
      return;
    }
    toast(e.message || "Unerwarteter Fehler – bitte die Seite neu laden.", {kind: "err"});
    return;
  }
  F.offline.hidden = store.online;
  renderUser(); renderChip(); renderDock();
  let anon = false;
  try { anon = localStorage.getItem("ws.anon") === "1"; } catch { /* ignore */ }
  if (store.me && !store.me.user && !anon) identitySheet();
  if (cur && cur.route.pid) { renderPbar(); projectTitle(); }
}

function boot() {
  applyTheme();
  frame();
  wireStore();
  renderUser(); renderChip();
  // views mount after me/settings/catalog are known (or after the first failed attempt, offline)
  start().finally(() => { addEventListener("hashchange", route); route(); });
}

if (typeof document !== "undefined" && document.getElementById("app") && !globalThis.__WS_NO_BOOT) boot();
