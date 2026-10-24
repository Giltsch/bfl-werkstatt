// Einstellungen: Firmendaten (server-side, one-time takeover from the old per-browser storage), Team, Zugang, Kundenlink,
// Datenschutz with Datensicherung (one route: app.js knows no separate section), Darstellung, System. The developer telemetry lives only in „System“.
import { h, fmt, img, api, toast, confirmDialog, sheet, pickImage, upload, toggleRow, setTheme } from "./ui.js";
import { profilSection, kanaeleSection, apiSection } from "./settings-connect.js";

export const SECTIONS = [["firma", "Firmendaten"], ["profil", "Firmenprofil"], ["team", "Team"], ["zugang", "Zugang"], ["kundenlink", "Kundenlink"],
  ["kanaele", "Kanäle (Telegram, E-Mail)"], ["api", "API & Webhooks"],
  ["datenschutz", "Datenschutz & Datensicherung"], ["darstellung", "Darstellung"], ["system", "System"]];   // = app.js SECTIONS
export const LEGACY_KEY = "werkstatt.company", MIGRATED_KEY = "ws.company.migrated";
const COLORS = ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9", "#f87171", "#38bdf8"];
const HEX = /^#[0-9a-f]{6}$/i;

// the v2 page kept {name, contact, color, logo} per browser; only valid values are taken over
export function legacyCompany(raw) {
  let c;
  try { c = JSON.parse(raw || "null"); } catch { return null; }
  if (!c || typeof c !== "object" || !String(c.name || "").trim()) return null;
  const out = {name: String(c.name).trim().slice(0, 200)};
  if (String(c.contact || "").trim()) out.contact = String(c.contact).trim().slice(0, 300);
  if (HEX.test(c.color || "")) out.color = c.color.toLowerCase();
  if (/^uploads\/[\w.-]+\.png$/.test(c.logo || "")) out.logo = c.logo;
  return out;
}

// runs once per device: PUTs the old company data when the server has none yet; returns the new settings or null
export async function migrateCompany({settings, storage, put}) {
  let done = null, raw = null;
  if (!storage) return null;
  try { done = storage.getItem(MIGRATED_KEY); raw = storage.getItem(LEGACY_KEY); } catch { return null; }
  if (done) return null;
  const c = legacyCompany(raw);
  if (!c) return null;
  const mark = () => { try { storage.setItem(MIGRATED_KEY, "1"); } catch {} };
  if (String((settings && settings.company && settings.company.name) || "").trim()) { mark(); return null; }
  const s = await put({company: c});
  mark();
  return s;
}

export function fmtBytes(n) {
  if (!(n > 0)) return "0 MB";
  if (n >= 1e9) return `${(n / 1e9).toLocaleString("de-DE", {maximumFractionDigits: 1})} GB`;
  return `${Math.max(1, Math.round(n / 1e6)).toLocaleString("de-DE")} MB`;
}
export const validBaseUrl = u => u === "" || /^https:\/\/[^\s/]+/.test(u);

export const local = () => { try { return globalThis.localStorage || null; } catch { return null; } };

const el = (tag, attrs = {}, ...kids) => h(tag, Object.fromEntries(Object.entries(attrs).filter(([, v]) => v != null && v !== false)),
  ...kids.flat(9).filter(k => k != null && k !== false && k !== ""));

function css() {
  if (document.getElementById("ws-settings-css")) return;
  document.head.append(el("style", {id: "ws-settings-css"}, `
.settings{display:grid;gap:14px;padding:12px 0 20px;grid-template-columns:minmax(0,1fr)}
.settings .snav{display:grid;gap:8px;align-content:start}
.settings .snav a{display:flex;align-items:center;justify-content:space-between;min-height:52px;padding:0 16px;border:1px solid var(--line);
 border-radius:var(--r);background:var(--panel);color:var(--text);text-decoration:none;font-weight:600}
.settings .snav a[aria-current=page]{border-color:var(--accent);background:var(--accent-soft)}
.settings .snav a::after{content:"›";font-size:22px;color:var(--muted)}
.settings .sbody{display:grid;gap:14px;min-width:0;align-content:start}
.settings .card{display:grid;gap:12px;min-width:0;margin:0}
.settings .field,.settings p,.settings .toggle-row{margin:0}
.settings h2{font-size:20px;margin:0}
.settings h3{font-size:17px;margin:0}
.settings .hint{color:var(--muted);font-size:14px;margin:0}
.settings .row2{display:grid;gap:12px;grid-template-columns:minmax(0,1fr)}
.settings .zip{display:grid;gap:12px;grid-template-columns:minmax(0,7rem) minmax(0,1fr)}
.settings .acts{display:flex;flex-wrap:wrap;gap:8px}
.settings .acts .btn{flex:1 1 160px}
.settings input[type=color]{width:100%;min-height:48px;padding:4px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.settings .logo{max-height:72px;max-width:220px;object-fit:contain;background:#fff;border:1px solid var(--line);border-radius:8px;padding:6px}
.settings .member{display:grid;grid-template-columns:44px minmax(0,1fr);gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.settings .member .mfields{display:grid;gap:8px;grid-template-columns:minmax(0,1fr) 72px}
.settings .member .btn{grid-column:2}
.settings .dot{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700}
.settings .kv{display:grid;grid-template-columns:minmax(0,11rem) minmax(0,1fr);gap:8px 12px;margin:0}
.settings .kv dt{color:var(--muted)}.settings .kv dd{margin:0;overflow-wrap:anywhere}
.settings .warnbox{border-left:4px solid var(--warn);padding:8px 12px;background:var(--panel2);border-radius:8px;margin:0}
.settings .back{justify-self:start}
@media (max-width:899px){.settings.has-sec .snav{display:none}.settings:not(.has-sec) .sbody{display:none}}
@media (min-width:900px){.settings{grid-template-columns:260px minmax(0,1fr);align-items:start}.settings .back{display:none}
 .settings .snav{position:sticky;top:calc(var(--top-h,56px) + 12px)}}
@media (min-width:700px){.settings .row2{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:420px){.settings .kv{grid-template-columns:minmax(0,1fr)}.settings .kv dd{margin-bottom:6px}}
`));
}

export default {
  mount(root, ctx) {
    css();
    const {store} = ctx;
    let dead = false, timer = null, route = ctx.route || {};
    const shell = el("div", {class: "settings"}), nav = el("nav", {class: "snav", "aria-label": "Einstellungen"}), body = el("div", {class: "sbody"});
    shell.append(nav, body);
    root.replaceChildren(shell);
    ctx.title && ctx.title("Einstellungen");

    const settings = () => store.settings || {};
    const put = async part => { const s = await api("PUT", "/api/settings", part); store.settings = {...settings(), ...s}; return s; };
    const field = (label, input, hint) => el("label", {class: "field"}, el("span", {class: "label"}, label), input, hint ? el("small", {class: "hint"}, hint) : null);
    const input = (value, attrs = {}) => { const i = el("input", {type: "text", autocomplete: "off", ...attrs}); i.value = value ?? ""; return i; };
    const card = (title, ...kids) => el("section", {class: "card"}, title ? el("h2", {}, title) : null, ...kids);
    const errMsg = e => (e && e.message) || "Server nicht erreichbar.";

    function render() {
      if (dead) return;
      clearInterval(timer); timer = null;
      const sec = SECTIONS.some(s => s[0] === route.section) ? route.section : null, cur = sec || "firma";
      shell.classList.toggle("has-sec", !!sec);
      nav.replaceChildren(...SECTIONS.map(([id, label]) => el("a", {href: `#/einstellungen/${id}`, "aria-current": id === cur ? "page" : null}, label)));
      ctx.actions(null);
      body.replaceChildren(el("a", {class: "btn ghost back", href: "#/einstellungen"}, "‹ Alle Einstellungen"),
        ...({firma, team, zugang, kundenlink, datenschutz: () => [...datenschutz(), ...sicherung()], darstellung, system,
          profil: () => profilSection({store}), kanaele: kanaeleSection, api: apiSection}[cur])());
      ctx.title && ctx.title("Einstellungen", sec ? (SECTIONS.find(s => s[0] === cur) || [])[1] : "");
    }

    function firma() {
      const c = settings().company || {};
      const f = {name: input(c.name, {maxlength: "200"}), street: input(c.street, {maxlength: "120"}), zip: input(c.zip, {maxlength: "12", inputmode: "numeric"}),
        city: input(c.city, {maxlength: "80"}), phone: input(c.phone, {type: "tel", inputmode: "tel", maxlength: "40"}),
        email: input(c.email, {type: "email", inputmode: "email", maxlength: "120"}), web: input(c.web, {type: "url", inputmode: "url", maxlength: "120"})};
      const contact = c.contact ? input(c.contact, {maxlength: "300"}) : null;
      const color = el("input", {type: "color", "aria-label": "Hausfarbe"}); color.value = HEX.test(c.color || "") ? c.color : "#3d5a4a";
      let logo = c.logo || null;
      const prev = el("div");
      const showLogo = () => prev.replaceChildren(...(logo ? [el("img", {class: "logo", src: img(logo), alt: "Logo"}),
        el("button", {type: "button", class: "btn ghost", on: {click: () => { logo = null; showLogo(); }}}, "Logo entfernen")] : [el("p", {class: "hint"}, "Noch kein Logo.")]));
      showLogo();
      const logoBtn = el("button", {type: "button", class: "btn secondary", on: {click: async () => {
        const file = await pickImage();
        if (!file) return;
        try { logo = (await upload(file, {maxSide: 1024})).path; showLogo(); } catch (e) { toast(errMsg(e), {kind: "err"}); }
      }}}, "Logo hochladen");
      const save = el("button", {type: "button", class: "btn primary block", on: {click: async () => {
        const company = Object.fromEntries(Object.entries(f).map(([k, i]) => [k, i.value.trim()]));
        if (contact) company.contact = contact.value.trim();
        company.color = color.value; company.logo = logo;
        save.disabled = true;
        try { await put({company}); toast("Gespeichert.", {kind: "ok"}); render(); }
        catch (e) { toast(errMsg(e), {kind: "err"}); save.disabled = false; }
      }}}, "Speichern");
      // the section list alone (phone, no section chosen) has no save button
      if (route.section === "firma" || (globalThis.matchMedia && matchMedia("(min-width: 900px)").matches)) ctx.actions([save]);
      return [card("Firmendaten", el("p", {class: "hint"}, "Erscheint auf Kundenblatt und Kundenlink."),
        field("Firmenname", f.name), field("Straße und Nr.", f.street), el("div", {class: "zip"}, field("PLZ", f.zip), field("Ort", f.city)),
        el("div", {class: "row2"}, field("Telefon", f.phone), field("E-Mail", f.email)), field("Webseite", f.web),
        contact ? field("Kontaktzeile (vom alten Gerät)", contact, "Wird statt der Einzelfelder gedruckt. Leeren, um die Felder oben zu verwenden.") : null,
        field("Hausfarbe", color),
        el("div", {class: "field"}, el("span", {class: "label"}, "Logo"), prev, el("div", {class: "acts"}, logoBtn)))];
    }

    function team() {
      const list = el("div");
      const refresh = async () => { try { const me = await api("GET", "/api/me"); store.team = me.team || []; store.me = me; } catch {} draw(); };
      const draw = () => list.replaceChildren(...(store.team || []).map(m => {
        const name = input(m.name, {maxlength: "40", "aria-label": "Name", class: "input"});
        const color = el("input", {type: "color", "aria-label": "Farbe"}); color.value = HEX.test(m.color || "") ? m.color : COLORS[0];
        const patch = async body => { try { await api("PATCH", `/api/team/${encodeURIComponent(m.id)}`, body); await refresh(); } catch (e) { toast(errMsg(e), {kind: "err"}); } };
        name.addEventListener("change", () => name.value.trim() && name.value.trim() !== m.name && patch({name: name.value.trim()}));
        color.addEventListener("change", () => patch({color: color.value}));
        const me = !!(store.me && store.me.user && store.me.user.id === m.id);
        return el("div", {class: "member"},
          el("span", {class: "dot avatar", style: `background:${HEX.test(m.color || "") ? m.color : COLORS[0]}`, "aria-hidden": "true"}, (m.name || "?").slice(0, 1).toUpperCase()),
          el("div", {class: "mfields"}, name, color),
          me ? el("p", {class: "hint", style: "grid-column:2"}, "Das sind Sie auf diesem Gerät.") : null,
          me ? null : el("button", {type: "button", class: "btn ghost", on: {click: async () => {   // never yourself, by accident
            if (!await confirmDialog({title: "Person entfernen", text: `„${m.name}“ aus dem Team entfernen? Aufträge und Verlauf bleiben erhalten.`, ok: "Person entfernen", danger: true})) return;
            try { await api("DELETE", `/api/team/${encodeURIComponent(m.id)}`); await refresh(); } catch (e) { toast(errMsg(e), {kind: "err"}); }
          }}}, "Person entfernen"));
      }));
      draw();
      const add = el("button", {type: "button", class: "btn secondary", on: {click: () => {
        const name = input("", {maxlength: "40", autocapitalize: "words"});
        const color = el("input", {type: "color"}); color.value = COLORS[(store.team || []).length % COLORS.length];
        const ok = el("button", {type: "button", class: "btn primary block"}, "Hinzufügen");
        const s = sheet({title: "Person hinzufügen", body: el("div", {style: "display:grid;gap:12px"}, field("Name", name), field("Farbe", color), ok), actions: []});
        ok.addEventListener("click", async () => {
          if (!name.value.trim()) { name.focus(); return; }
          ok.disabled = true;
          try { await api("POST", "/api/team", {name: name.value.trim(), color: color.value}); s.close(); await refresh(); }
          catch (e) { ok.disabled = false; toast(errMsg(e), {kind: "err"}); }
        });
        setTimeout(() => name.focus(), 60);
      }}}, "＋ Person hinzufügen");
      return [card("Team", el("p", {class: "hint"}, "Die Namen erscheinen im Verlauf und bei gleichzeitigen Änderungen. Jedes Gerät wählt seine Person einmal aus."),
        list, el("div", {class: "acts"}, add))];
    }

    function zugang() {
      const set = !!(settings().access || {}).password_set;
      const cur = input("", {type: "password", autocomplete: "current-password"}), neu = input("", {type: "password", autocomplete: "new-password", minlength: "8"});
      const send = async (password, label) => {
        if (password !== null && password.length < 8) { toast("Bitte mindestens 8 Zeichen.", {kind: "err"}); neu.focus(); return; }
        try {
          const r = await api("PUT", "/api/access", {password, current: set ? cur.value : undefined});
          store.settings = {...settings(), access: {password_set: !!r.password_set}};
          toast(label, {kind: "ok"}); render();
        } catch (e) { toast(errMsg(e), {kind: "err"}); }
      };
      return [card("Zugang schützen",
        el("p", {}, "Ohne Passwort kann jeder im Firmen-WLAN die Werkstatt öffnen – auch Gäste. Mit Passwort meldet sich jedes Gerät einmal an."),
        el("div", {}, el("span", {class: `badge ${set ? "ok" : "warn"}`, style: "font-size:14px"}, set ? "Passwortschutz ist an" : "Kein Passwort gesetzt")),
        set ? field("Aktuelles Passwort", cur) : null, field("Neues Passwort", neu),
        el("div", {class: "acts"},
          el("button", {type: "button", class: "btn primary", on: {click: () => send(neu.value, "Gespeichert.")}}, set ? "Passwort ändern" : "Passwort festlegen"),
          set ? el("button", {type: "button", class: "btn danger", on: {click: async () => {
            if (await confirmDialog({title: "Passwortschutz aufheben", text: "Danach kann jeder im Firmen-WLAN die Werkstatt öffnen.", ok: "Passwortschutz aufheben", danger: true}))
              send(null, "Passwortschutz aufgehoben.");
          }}}, "Passwortschutz aufheben") : null))];
    }

    function kundenlink() {
      const s = settings();
      const url = input(s.public_base_url || "", {type: "url", inputmode: "url", placeholder: "https://…", maxlength: "200"});
      let trusted = !!s.trusted_proxy;
      const proxyRow = toggleRow({title: "Vertrauenswürdiger Proxy (Tunnel)", text: "Nur einschalten, wenn ein Tunnel auf diesem Rechner die Kundenlinks weiterleitet.",
        checked: trusted, onChange: v => { trusted = v; }});
      const save = el("button", {type: "button", class: "btn primary", on: {click: async () => {
        const u = url.value.trim().replace(/\/+$/, "");
        if (!validBaseUrl(u)) { toast("Die Adresse muss mit https:// beginnen.", {kind: "err"}); url.focus(); return; }
        try { await put({public_base_url: u, trusted_proxy: trusted}); toast("Gespeichert.", {kind: "ok"}); render(); } catch (e) { toast(errMsg(e), {kind: "err"}); }
      }}}, "Speichern");
      return [card("Kundenlink",
        el("p", {}, "Damit Kunden den Link in E-Mail und WhatsApp auch zu Hause öffnen können, braucht die Werkstatt eine Internet-Adresse. Ohne sie funktionieren Kundenlinks nur im WLAN der Werkstatt."),
        field("Internet-Adresse für Kundenlinks", url, "z. B. https://entwurf.schreinerei-muster.de – einrichten lässt sich das mit einem Tunnel, der nur Port 7861 weiterleitet (am besten vom IT-Betreuer)."),
        proxyRow, el("div", {class: "acts"}, save))];
    }

    function datenschutz() {
      const r = settings().retention || {};
      const a = input(r.abgesagt_months ?? 6, {type: "number", inputmode: "numeric", min: "1", max: "120"});
      const b = input(r.fertig_months ?? 12, {type: "number", inputmode: "numeric", min: "1", max: "120"});
      const save = el("button", {type: "button", class: "btn primary", on: {click: async () => {
        const n = i => Math.max(1, Math.min(120, parseInt(i.value, 10) || 1));
        try { await put({retention: {abgesagt_months: n(a), fertig_months: n(b)}}); toast("Gespeichert.", {kind: "ok"}); render(); } catch (e) { toast(errMsg(e), {kind: "err"}); }
      }}}, "Speichern");
      return [card("Datenschutz",
        field("Abgesagte oder archivierte Aufträge zur Löschung vorschlagen nach … Monaten", a),
        field("Abgeschlossene Aufträge zur Löschung vorschlagen nach … Monaten", b),
        el("p", {class: "hint"}, "Es wird nie automatisch gelöscht."),
        el("p", {class: "hint"}, "Fällige Aufträge zeigt die Auftragsliste unter „Zur Löschung fällig“. Löschen geht im Auftrag unter „Kunde & Auftrag“ › „Daten & Datenschutz“."),
        el("div", {class: "acts"}, save))];
    }

    function darstellung() {
      let mode = "light";
      try { mode = localStorage.getItem("ws.theme") || "light"; } catch {}
      const seg = el("div", {class: "seg", role: "group", "aria-label": "Darstellung"},
        [["light", "Hell"], ["dark", "Dunkel"], ["auto", "Automatisch"]].map(([id, label]) => el("button", {type: "button",
          "aria-pressed": String(mode === id), on: {click: () => { setTheme(id); render(); }}}, label)));
      return [card("Darstellung", seg, el("p", {class: "hint"}, "Gilt nur für dieses Gerät."))];
    }

    function sicherung() {
      const info = el("div", {class: "card"}, el("h2", {}, "Datensicherung"), el("p", {class: "hint"}, "Wird geladen …"));
      const b = settings().backup || {};
      const target = input(b.target || "", {maxlength: "300", spellcheck: "false", autocapitalize: "off"});
      const keep = input(b.keep ?? 14, {type: "number", inputmode: "numeric", min: "1", max: "60"});
      const save = el("button", {type: "button", class: "btn secondary", on: {click: async () => {
        const t = target.value.trim();
        if (!t.startsWith("/")) { toast("Bitte einen vollständigen Ordnerpfad angeben (beginnt mit /).", {kind: "err"}); return; }
        try { await put({backup: {target: t, keep: Math.max(1, Math.min(60, parseInt(keep.value, 10) || 14))}}); toast("Gespeichert.", {kind: "ok"}); load(); }
        catch (e) { toast(errMsg(e), {kind: "err"}); }
      }}}, "Speichern");
      const now = el("button", {type: "button", class: "btn primary"}, "Jetzt sichern");
      now.addEventListener("click", async () => {
        now.disabled = true; now.setAttribute("aria-busy", "true"); now.textContent = "Sichert …";
        try { draw(await api("POST", "/api/backup")); toast("Sicherung fertig.", {kind: "ok"}); }
        catch (e) { toast(errMsg(e), {kind: "err"}); load(); }
        finally { if (!dead) { now.disabled = false; now.removeAttribute("aria-busy"); now.textContent = "Jetzt sichern"; } }
      });
      const draw = st => {
        const last = st && st.last, k = (st && st.keep) || b.keep || 14;
        if (st && st.running) { now.disabled = true; now.textContent = "Sichert …"; }
        info.replaceChildren(el("h2", {}, "Datensicherung"),
          !last ? el("p", {}, "Noch keine Sicherung")
            : last.ok ? el("p", {}, `Letzte Sicherung: ${fmt.dateTime(last.at)} (${fmtBytes(last.bytes)})`)
              : el("p", {class: "warnbox"}, `Sicherung fehlgeschlagen: ${last.error || "unbekannter Fehler"}`),
          st && st.count ? el("p", {class: "hint"}, `${st.count} ${st.count === 1 ? "Sicherung" : "Sicherungen"} im Ziel-Ordner.`) : null,
          st && st.same_disk ? el("p", {class: "warnbox"}, "Das Ziel liegt auf derselben Festplatte. Für echten Schutz eine USB-Platte oder ein NAS als Ziel wählen.") : null,
          el("div", {class: "acts"}, now),
          el("p", {class: "hint"}, `Gelöschte Aufträge bleiben in älteren Sicherungen, bis diese nach ${k} Tagen überschrieben werden.`));
      };
      const load = async () => { try { draw(await api("GET", "/api/backup")); } catch (e) { info.replaceChildren(el("h2", {}, "Datensicherung"), el("p", {class: "warnbox"}, errMsg(e))); } };
      load();
      return [info, card("Sicherungs-Ziel", field("Ziel-Ordner", target), field("Anzahl Sicherungen (eine pro Tag)", keep), el("div", {class: "acts"}, save))];
    }

    function system() {
      const box = el("section", {class: "card"}, el("h2", {}, "Rechner"), el("p", {class: "hint"}, "Wird geladen …"));
      const queueBox = el("section", {class: "card"}, el("h2", {}, "Warteschlange"));
      const LOADED = {distilled: "Bildmodell", base: "Bildmodell (Basis)", pup: "Beschreibungs-KI", depth: "Raumvermessung"};
      const pupText = p => !p ? "–" : p.state === "up" ? `läuft (schnell)${p.backend === "local" ? " – im Bildprozess" : ""}`
        : p.state === "starting" ? "startet …" : "aus – Notbetrieb ohne Beschreibungs-KI";
      const load = async () => {
        let st = null, q = null;
        try { [st, q] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/queue").catch(() => null)]); }
        catch (e) { box.replaceChildren(el("h2", {}, "Rechner"), el("p", {class: "warnbox"}, errMsg(e))); return; }
        if (dead) return;
        const loaded = Object.entries(st.loaded || {}).filter(([, v]) => v).map(([k]) => LOADED[k] || k);
        const j = st.judge || {};
        const kv = [["Zustand", st.busy || st.engine !== "idle" ? `beschäftigt${st.wait_s ? " · " + fmt.dur(st.wait_s) : ""}` : "frei"],
          ["Modelle geladen", loaded.join(", ") || "keine"],
          ["Freier Speicher", st.mem_free_gb != null ? `${fmt.num(st.mem_free_gb, 1)} von ${fmt.num(st.mem_total_gb, 1)} GB` : "–"],
          ["Beschreibungs-KI", pupText(st.pup || (q && q.pup))],
          ["Qualitätsprüfung", j.available ? `bereit${j.model ? " (" + j.model + ")" : ""}` : j.state ? `nicht bereit (${j.state})` : "nicht bereit"],
          ["Version", [st.version, st.started ? "gestartet " + fmt.dateTime(st.started) : ""].filter(Boolean).join(" · ") || "–"]];
        box.replaceChildren(el("h2", {}, "Rechner"), el("dl", {class: "kv"}, kv.map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])));
        const jobs = (q && q.jobs) || [];
        queueBox.replaceChildren(el("h2", {}, "Warteschlange"), jobs.length
          ? el("div", {class: "list"}, jobs.map(x => el("div", {class: "list-item", style: "display:grid;gap:2px;padding:8px 0"},
            el("strong", {}, [x.owner_name, x.project_title || x.source, x.label].filter(Boolean).join(" · ")),
            el("span", {class: "hint"}, x.stage_text || x.stage || x.state))))
          : el("p", {class: "hint"}, "Keine Berechnungen."));
      };
      load();
      timer = setInterval(() => { if (!document.hidden) load(); }, 5000);
      return [box, queueBox, card("Werkzeuge für Experten", el("div", {class: "acts"},
        el("a", {class: "btn secondary", href: "/studio"}, "Bildstudio öffnen"), el("a", {class: "btn secondary", href: "/book"}, "Buch-Werkstatt öffnen")))];
    }

    render();
    migrateCompany({settings: settings(), storage: local(), put}).then(s => {
      if (!s || dead) return;
      toast("Firmendaten von diesem Gerät übernommen.", {kind: "ok"});
      render();
    }, e => toast(errMsg(e), {kind: "err"}));

    return {
      unmount() { dead = true; clearInterval(timer); },
      onRoute(r) { route = r || {}; render(); },
    };
  },
};
