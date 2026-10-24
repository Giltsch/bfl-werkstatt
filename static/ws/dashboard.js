// Aufträge: the order list (search, status chips, attention badges, retention filter) and the camera-first new order.
// Pure helpers are exported for tests/ws_orders.test.mjs; DOM work only inside mount().
import { h, fmt, STATUS, statusLabel, statusTone as toneOf, img, api, toast, sheet, pickImage, upload, icon as iconSvg } from "./ui.js";
// icon() returns fixed SVG markup (never user data), so it may go in via innerHTML

export const ACTIVE = s => s !== "fertig" && s !== "abgesagt";
const ATT = {aenderung: 0, ueberfaellig: 1, freigegeben: 2, termin_heute: 3};
export const SORTS = [["wichtig", "Wichtig zuerst"], ["updated", "Zuletzt bearbeitet"], ["termin", "Termin"], ["nummer", "Nummer"]];
export const CHIPS = [["aktiv", "Alle aktiven"], ...STATUS.map(s => [s.id, s.label])];

const el = (tag, attrs = {}, ...kids) => h(tag, Object.fromEntries(Object.entries(attrs).filter(([, v]) => v != null && v !== false)),
  ...kids.flat(9).filter(k => k != null && k !== false && k !== ""));

export function matches(p, q) {
  const hay = p.q || [p.title, p.customer, p.ref, p.ext_ref, p.city].join(" ").toLowerCase();
  return String(q || "").toLowerCase().split(/\s+/).filter(Boolean).every(t => hay.includes(t));
}

// f = {q, chip: "aktiv" | status | "due", archive, test}. A search under „Alle aktiven“ looks through everything
// (finished, cancelled and archived orders too) – finding an old customer is what the search is for.
export function filterList(list, f) {
  const q = String(f.q || "").trim();
  return list.filter(p => {
    if (!f.test && p.test) return false;
    if (q && !matches(p, q)) return false;
    if (f.chip === "due") return !!p.retention_due;
    if (!(q && f.chip === "aktiv" && !f.archive) && !!p.archived_at !== !!f.archive) return false;
    if (f.chip === "aktiv") return f.archive || q ? true : ACTIVE(p.status);
    return p.status === f.chip;
  });
}

export function counts(list, f) {
  return Object.fromEntries([...CHIPS.map(([id]) => id), "due"].map(id => [id, filterList(list, {...f, chip: id}).length]));
}

export const dueCount = (list, f) => list.filter(p => p.retention_due && (f.test || !p.test)).length;

const today = now => { const d = new Date(now); d.setHours(0, 0, 0, 0); return d; };
const at = p => p.appointment && p.appointment.at ? new Date(p.appointment.at) : null;

// next appointment that still matters: today or later, or overdue (flagged by the server)
export function nextAppt(p, now = new Date()) {
  const t = at(p);
  return t && (t >= today(now) || p.attention === "ueberfaellig") ? t : null;
}

export function sortList(list, sort, now = new Date()) {
  const upd = (a, b) => String(b.updated || "").localeCompare(String(a.updated || ""));
  const appt = (a, b) => {
    const x = nextAppt(a, now), y = nextAppt(b, now);
    return x && y ? x - y : x ? -1 : y ? 1 : 0;
  };
  const cmp = {
    wichtig: (a, b) => ((ATT[a.attention] ?? 9) - (ATT[b.attention] ?? 9)) || appt(a, b) || upd(a, b),
    updated: upd,
    termin: (a, b) => { const x = at(a), y = at(b); return (x && y ? x - y : x ? -1 : y ? 1 : 0) || upd(a, b); },
    nummer: (a, b) => (!a.ref - !b.ref) || String(b.ref || "").localeCompare(String(a.ref || ""), "de", {numeric: true}) || upd(a, b),
  }[sort] || upd;
  return [...list].sort(cmp);
}

export function attentionBadge(a) {
  return {aenderung: {text: "Änderungswunsch", tone: "warn"}, freigegeben: {text: "Freigegeben ✓", tone: "ok"},
    termin_heute: {text: "heute", tone: "info"}, ueberfaellig: {text: "überfällig", tone: "bad"}}[a] || null;
}

/** The team app is reachable without a password (settings loaded and no password set). */
export const openAccess = store => !!store?.settings?.access && !store.settings.access.password_set;
export const running = (queue, pid) => ((queue && queue.jobs) || []).some(j => j.project === pid && j.state !== "done");

const VIEW_KEY = "ws.dash";
const loadView = () => { try { return JSON.parse(localStorage.getItem(VIEW_KEY) || "{}"); } catch { return {}; } };
const saveView = v => { try { localStorage.setItem(VIEW_KEY, JSON.stringify(v)); } catch {} };

function css() {
  if (document.getElementById("ws-dash-css")) return;
  document.head.append(el("style", {id: "ws-dash-css"}, `
.dash{display:grid;gap:12px;padding-bottom:16px}
.dash-top{position:sticky;top:var(--top-h,56px);z-index:5;background:var(--bg);margin:calc(-1 * var(--pad)) calc(-1 * var(--pad)) 0;padding:10px var(--pad) 6px;display:grid;gap:8px;min-width:0}
.dash-search{position:relative}
.dash-search input{width:100%;min-height:48px;font-size:17px;padding:10px 14px 10px 44px;border:1px solid var(--line);border-radius:var(--r);background:var(--panel);color:var(--text)}
.dash-search .ico{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:22px;height:22px;color:var(--muted);pointer-events:none}
.dash-search .ico svg{width:22px;height:22px}
.dash-opts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:-4px}
.dash-opts select{min-height:44px;font-size:16px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text);padding:0 10px;max-width:100%}
.dash-opts label{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:14px}
.dash-list{display:grid;gap:10px;margin:0;padding:0;list-style:none}
.dash-head{display:none}
.order{display:grid;grid-template-columns:76px minmax(0,1fr);grid-template-areas:"th main" "th meta";gap:4px 12px;align-items:start;
 padding:10px;min-height:96px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);color:var(--text);text-decoration:none}
.order:focus-visible{outline:3px solid var(--focus)}
.order .th{grid-area:th;width:76px;height:76px;border-radius:10px;background:var(--panel2);object-fit:cover;display:flex;align-items:center;justify-content:center;color:var(--muted)}
.order .th svg{width:30px;height:30px}
.order .main{grid-area:main;min-width:0;display:grid;gap:2px}
.order .t{font-weight:700;font-size:17px;overflow-wrap:anywhere}
.order .c{color:var(--muted);font-size:15px;overflow-wrap:anywhere}
.order .meta{grid-area:meta;display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:14px}
.order .when{color:var(--muted);font-size:14px}
.order .appt.bad{color:var(--danger);font-weight:600}
.order .col{display:none}
.dash .card{margin:0}
.dash-note{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.dash-note p{margin:0;flex:1 1 220px}
.dash-alert{border:2px solid var(--danger);background:var(--bad-soft)}
.dash-alert strong{color:var(--danger)}
.new-order{display:grid;gap:12px}
.new-order .field,.new-order p{margin:0}
.new-order .btn{width:100%}
.dash-voice .ico{display:inline-flex}.dash-voice .long{display:none}
@media (min-width:700px){.dash-voice .short{display:none}.dash-voice .long{display:inline}}
@media (max-height:520px){.dash-top{position:static}}
@media (min-width:900px){
 .dash-head{display:grid;grid-template-columns:64px minmax(0,2.2fr) 1fr 1.1fr 1.3fr 1.3fr;gap:12px;padding:0 12px;color:var(--muted);font-size:14px}
 .order{grid-template-columns:64px minmax(0,2.2fr) 1fr 1.1fr 1.3fr 1.3fr;grid-template-areas:"th main city st appt when";align-items:center;min-height:76px;padding:6px 12px}
 .order .th{width:64px;height:64px}
 .order .meta{display:none}
 .order .col{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:15px}
 .order .city{grid-area:city}.order .st{grid-area:st}.order .ap{grid-area:appt}.order .wh{grid-area:when;color:var(--muted)}
}`));
}

export default {
  mount(root, ctx) {
    css();
    const {store} = ctx;
    const v = loadView();
    const f = {q: "", chip: CHIPS.some(c => c[0] === v.chip) ? v.chip : "aktiv", archive: !!v.archive, test: !!v.test,
      sort: SORTS.some(s => s[0] === v.sort) ? v.sort : "wichtig"};
    let list = null, error = null, dead = false, openSheet = null;
    const off = [];

    const search = el("input", {type: "search", placeholder: "Kunde, Projekt oder Nummer suchen", "aria-label": "Suchen",
      enterkeyhint: "search", autocomplete: "off"});
    search.addEventListener("input", () => { f.q = search.value; render(); });
    const chipsEl = el("div", {class: "chips scroll", role: "toolbar", "aria-label": "Status"});
    const optsEl = el("div", {class: "dash-opts"});
    const noteEl = el("div");
    const listEl = el("div");
    root.replaceChildren(el("div", {class: "dash"},
      el("div", {class: "dash-top"},
        el("div", {class: "dash-search"}, el("span", {class: "ico", "aria-hidden": "true"}), search), chipsEl), optsEl,
      noteEl, listEl));
    root.querySelector(".dash-search .ico").innerHTML = iconSvg("search");

    const persist = () => saveView({chip: f.chip, archive: f.archive, test: f.test, sort: f.sort});
    const setChip = id => { f.chip = id; persist(); render(); };

    function renderChips(all) {
      const n = counts(all, f);
      const chips = CHIPS.map(([id, label]) => el("button", {type: "button", class: "chip" + (f.chip === id ? " active" : ""),
        "aria-pressed": String(f.chip === id), on: {click: () => setChip(id)}}, label, el("span", {class: "count"}, String(n[id]))));
      if (f.chip === "due") chips.unshift(el("button", {type: "button", class: "chip active", "aria-pressed": "true",
        on: {click: () => setChip("aktiv")}}, "Zur Löschung fällig", el("span", {class: "count"}, String(n.due))));
      chipsEl.replaceChildren(...chips);
      const tog = (key, label) => el("button", {type: "button", class: "chip" + (f[key] ? " active" : ""), "aria-pressed": String(f[key]),
        on: {click: () => { f[key] = !f[key]; persist(); render(); }}}, (f[key] ? "✓ " : "") + label);
      const sel = el("select", {"aria-label": "Sortieren", title: "Sortieren"}, SORTS.map(([id, label]) => el("option", {value: id}, "Sortieren: " + label)));
      sel.value = f.sort;
      sel.addEventListener("change", () => { f.sort = sel.value; persist(); render(); });
      optsEl.replaceChildren(tog("archive", "Archiv"), tog("test", "Test-Projekte"), sel);
    }

    function card(p) {
      const att = attentionBadge(p.attention), ap = nextAppt(p) || (p.attention === "ueberfaellig" ? at(p) : null);
      const apText = ap ? `Termin: ${fmt.dateTime(p.appointment.at)}` : "";
      const apBad = p.attention === "ueberfaellig";
      // an approval via link is the status itself – one badge „Freigegeben ✓“ instead of two
      const status = el("span", {class: `badge ${p.attention === "freigegeben" ? "ok" : toneOf(p.status)}`},
        p.attention === "freigegeben" ? att.text : statusLabel(p.status));
      const badges = () => [
        p.new ? el("span", {class: "badge bad", title: "Kunde hat freigegeben – noch nicht angesehen"}, "Neu") : null,
        p.outdated ? el("span", {class: "badge warn"}, "Freigabe veraltet") : null,
        att && p.attention !== "freigegeben" ? el("span", {class: `badge ${att.tone}`}, att.text) : null,
        p.archived_at ? el("span", {class: "badge"}, "Archiviert") : null,
        running(store.queue, p.id) ? el("span", {class: "badge info"}, "Berechnung läuft …") : null];
      const who = p.updated ? `zuletzt: ${p.updated_by_name ? p.updated_by_name + ", " : ""}${fmt.ago(p.updated)}` : "";
      const nv = p.variants ? (p.variants === 1 ? "1 Vorschlag" : `${p.variants} Vorschläge`) : "";
      const thumb = p.thumb ? el("img", {class: "th", src: img(p.thumb), alt: "", loading: "lazy", decoding: "async"})
        : el("span", {class: "th", "aria-hidden": "true"});
      if (!p.thumb) thumb.innerHTML = iconSvg("image");
      return el("a", {class: "order list-item", href: `#/p/${encodeURIComponent(p.id)}`, "data-id": p.id},
        thumb,
        el("div", {class: "main"},
          el("div", {class: "t"}, p.title || "Ohne Titel"),
          el("div", {class: "c"}, [p.customer, p.ref].filter(Boolean).join(" · ") || "–")),
        el("div", {class: "meta"}, status, badges(),
          apText ? el("span", {class: "appt" + (apBad ? " bad" : "")}, apText) : null,
          nv ? el("span", {class: "when"}, nv) : null,
          who ? el("span", {class: "when"}, who) : null),
        el("div", {class: "col city"}, p.city || ""),
        el("div", {class: "col st"}, status.cloneNode(true), badges()),
        el("div", {class: "col ap"}, apText ? el("span", {class: "appt" + (apBad ? " bad" : "")}, apText) : null),
        el("div", {class: "col wh"}, who));
    }

    function render() {
      if (dead) return;
      if (error && !list) {
        listEl.replaceChildren(el("div", {class: "empty"}, el("p", {}, error),
          el("button", {type: "button", class: "btn secondary", on: {click: load}}, "Nochmal versuchen")));
        return;
      }
      if (!list) { listEl.replaceChildren(el("p", {class: "muted"}, "Aufträge werden geladen …")); return; }
      renderChips(list);
      const due = dueCount(list, f);
      noteEl.replaceChildren(...[openAccess(store) ? el("div", {class: "card dash-note dash-alert", role: "alert"},
        el("p", {}, el("strong", {}, "Kein Passwort gesetzt. "), "Jeder im WLAN – auch Gäste – kann alle Aufträge, Raumfotos und Kundendaten öffnen."),
        el("a", {class: "btn primary", href: "#/einstellungen/zugang"}, "Passwort setzen")) : null,
        due && f.chip !== "due" ? el("div", {class: "card dash-note"},
        el("p", {}, `${due} ${due === 1 ? "Auftrag ist" : "Aufträge sind"} zur Löschung fällig (Datenschutz).`),
        el("button", {type: "button", class: "btn secondary", on: {click: () => setChip("due")}}, "Ansehen")) : null].filter(Boolean));
      const real = list.filter(p => f.test || !p.test);
      if (!real.length) {
        listEl.replaceChildren(el("div", {class: "empty"},
          el("p", {}, "Noch keine Aufträge. Legen Sie den ersten an – am besten direkt beim Kunden mit dem Handy.")));
        return;
      }
      const rows = sortList(filterList(list, f), f.sort);
      if (!rows.length) {
        const q = f.q.trim();
        listEl.replaceChildren(el("div", {class: "empty"}, el("p", {}, q ? `Nichts gefunden für „${q}“.` : "Keine Aufträge in dieser Ansicht."),
          el("button", {type: "button", class: "btn secondary", on: {click: () => { f.q = ""; search.value = ""; setChip("aktiv"); }}}, "Alle aktiven zeigen")));
        return;
      }
      listEl.replaceChildren(
        el("div", {class: "dash-head", "aria-hidden": "true"}, el("span"), el("span", {}, "Auftrag"), el("span", {}, "Ort"),
          el("span", {}, "Status"), el("span", {}, "Termin"), el("span", {}, "Zuletzt")),
        el("div", {class: "dash-list"}, rows.map(card)));
    }

    async function load() {
      try { list = await api("GET", "/api/projects/workshop?archived=all"); error = null; }
      catch (e) { error = e.message || "Server nicht erreichbar."; }
      render();
    }

    function newOrder() {
      if (openSheet) return;
      const field = (label, attrs) => {
        const inp = el("input", {type: "text", autocomplete: "off", ...attrs});
        return [inp, el("label", {class: "field"}, el("span", {class: "label"}, label), inp)];
      };
      const [cust, custF] = field("Kunde (Name)", {maxlength: "200", autocapitalize: "words"});
      const [title, titleF] = field("Projekt (optional, z. B. Einbauschrank Flur)", {maxlength: "200"});
      let salutation = "";
      const salSeg = el("div", {class: "seg", role: "radiogroup", "aria-label": "Anrede"});
      const drawSal = () => salSeg.replaceChildren(...[["", "keine"], ["Herr", "Herr"], ["Frau", "Frau"], ["Familie", "Familie"]].map(([k, l]) =>
        el("button", {type: "button", role: "radio", "aria-checked": String(salutation === k), class: salutation === k ? "active" : "",
          on: {click: () => { salutation = k; drawSal(); }}}, l)));
      drawSal();
      const salF = el("div", {class: "field"}, el("span", {class: "label"}, "Anrede (optional, für die Nachricht an den Kunden)"), salSeg);
      const [phone, phoneF] = field("Telefon (optional, für WhatsApp)", {type: "tel", inputmode: "tel", maxlength: "40", autocomplete: "tel"});
      const [email, emailF] = field("E-Mail (optional)", {type: "email", inputmode: "email", maxlength: "120", autocomplete: "email"});
      const [city, cityF] = field("Ort (optional)", {maxlength: "80", autocapitalize: "words"});
      const msg = el("p", {class: "muted", role: "status"}, "Alles lässt sich später ändern.");
      let busy = false;
      const camBtn = el("button", {type: "button", class: "btn primary block"}, "📷 Anlegen und Raum fotografieren");
      const plainBtn = el("button", {type: "button", class: "btn secondary block"}, "Anlegen ohne Foto");
      const setBusy = (b, text) => { busy = b; camBtn.disabled = plainBtn.disabled = b; if (text) msg.textContent = text; };
      const create = () => api("POST", "/api/projects/workshop", {title: title.value.trim(), customer: cust.value.trim(),
        contact: {salutation, phone: phone.value.trim(), email: email.value.trim(), city: city.value.trim()}, appointment: null});
      camBtn.addEventListener("click", async () => {
        if (busy) return;
        const file = await pickImage({camera: true});   // first, inside the tap – iOS drops the gesture after an await
        if (!file) return;
        setBusy(true, "Auftrag wird angelegt …");
        let doc;
        try { doc = await create(); } catch (e) { setBusy(false, "Alles lässt sich später ändern."); toast(e.message, {kind: "err"}); return; }
        try {
          msg.textContent = "Foto wird hochgeladen …";
          const r = await upload(file);
          await api("PATCH", `/api/projects/workshop/${doc.id}`, {base_rev: doc.rev, ops: [
            {op: "set", path: "room", value: r.path}, {op: "set", path: "room_size", value: [r.width, r.height]}]});
        } catch (e) { toast(`Foto konnte nicht gespeichert werden: ${e.message}`, {kind: "err"}); }
        close();
        ctx.nav(`#/p/${doc.id}/raum`);
      });
      plainBtn.addEventListener("click", async () => {
        if (busy) return;
        setBusy(true, "Auftrag wird angelegt …");
        try { const doc = await create(); close(); ctx.nav(`#/p/${doc.id}`); }
        catch (e) { setBusy(false, "Alles lässt sich später ändern."); toast(e.message, {kind: "err"}); }
      });
      const voiceLink = el("a", {class: "btn ghost block", href: "#/assistent", on: {click: () => close()}}, "Lieber per Foto + Sprache (Assistent)");
      const body = el("div", {class: "new-order"}, salF, custF, titleF, phoneF, emailF, cityF, msg, camBtn, plainBtn, voiceLink);
      openSheet = sheet({title: "Neuer Auftrag", body, actions: [], onClose: () => {
        openSheet = null;
        if (!dead && !leaving && ctx.route && ctx.route.name === "neu") ctx.nav("#/");
      }});
      let leaving = false;
      const close = () => { leaving = true; const s = openSheet; openSheet = null; s && s.close(); };
      setTimeout(() => cust.focus(), 60);
    }

    const newBtn = el("button", {type: "button", class: "btn primary block", on: {click: newOrder}}, "＋ Neuer Auftrag");
    // the assistant's front door: photo + spoken description → planned order (assistent.js)
    const voiceBtn = el("a", {class: "btn secondary dash-voice", href: "#/assistent", "aria-label": "Neuer Auftrag per Foto + Sprache"},
      el("span", {class: "ico", "aria-hidden": "true"}), el("span", {class: "short"}, "Foto + Sprache"), el("span", {class: "long"}, "Neuer Auftrag per Foto + Sprache"));
    voiceBtn.querySelector(".ico").innerHTML = iconSvg("mic");
    ctx.actions([voiceBtn, newBtn]);
    const titled = () => ctx.title && ctx.title("Aufträge", store.news ? (store.news === 1 ? "1 neue Freigabe vom Kunden" : `${store.news} neue Freigaben vom Kunden`) : "");
    titled();
    let runSig = "";   // every sync emits "queue": rebuild only when a „Berechnung läuft“ badge changes (open <select>, focus)
    const onQueue = () => {
      if (!list) return;
      const sig = list.filter(p => running(store.queue, p.id)).map(p => p.id).join();
      if (sig !== runSig) { runSig = sig; render(); }
    };
    off.push(store.on("list", load), store.on("queue", onQueue), store.on("online", load), store.on("news", titled));
    render();
    load();
    if (ctx.route && ctx.route.name === "neu") newOrder();

    return {
      unmount() { dead = true; off.forEach(fn => typeof fn === "function" && fn()); openSheet && openSheet.close(); },
      onRoute(route) { ctx.route = route; if (route.name === "neu") newOrder(); },
    };
  },
};
