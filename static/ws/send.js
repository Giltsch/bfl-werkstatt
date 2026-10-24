// Versand: version → PDF/ZIP share or download (works without internet), mailto/WhatsApp, customer link + QR only
// with a public address; Freigabe erfassen (phone, paper, on site).
import { h, api, img, toast, sheet, choose, confirmDialog, ratingOf, copyText, shareOrFallback, SECURE, pickImage, upload, variantNumbers, variantName } from "./ui.js";
import { ensureStyle } from "./present.js";

export const MAX_SEND = 4;
const CSS = `
.sd-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:10px;margin:8px 0 4px}
.sd-tile{position:relative;display:flex;flex-direction:column;align-items:stretch;gap:4px;padding:6px;min-height:44px;border:2px solid var(--line);border-radius:var(--r);background:var(--panel);color:var(--text);font:inherit;font-size:14px;text-align:left;cursor:pointer}
.sd-tile img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:8px;background:var(--stage)}
.sd-tile[aria-pressed=true]{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
.sd-tile .sd-check{position:absolute;top:10px;right:10px;width:28px;height:28px;border-radius:50%;background:var(--panel);border:2px solid var(--line);display:flex;align-items:center;justify-content:center;font-weight:700}
.sd-tile[aria-pressed=true] .sd-check{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.sd-meta{color:var(--muted);font-size:14px}
.sd-col{display:flex;flex-direction:column;gap:10px}
.sd-row{display:flex;flex-wrap:wrap;gap:10px}.sd-row>.btn{flex:1 1 180px}
.sd-qr{display:block;width:200px;height:200px;background:#fff;border-radius:8px;padding:8px;margin:4px 0}
.sd-link{width:100%;font:inherit;font-size:15px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--text);min-height:44px}
.sd-warn{margin:0;padding-left:20px}.sd-warn li{margin:6px 0}
.sd-hint{color:var(--muted);font-size:14px;margin:0}
.sd-check{display:flex;gap:12px;align-items:flex-start;padding:12px;border:1px solid var(--line);border-radius:10px;cursor:pointer;line-height:1.35}
.sd-check input{width:22px;height:22px;flex:none;margin:1px 0 0;accent-color:var(--accent)}
.sd-pinnote{border-left-color:var(--accent)}
.sd-pin{font:700 30px/1.1 ui-monospace,monospace;letter-spacing:.12em;margin:2px 0;font-variant-numeric:tabular-nums}
.sd-alert{color:var(--warn);font-weight:600}
.sd-name{font-weight:650}
.sd-logo{max-height:56px;max-width:160px;object-fit:contain;background:#fff;border:1px solid var(--line);border-radius:8px;padding:4px}
.sd-note{border-left:4px solid var(--warn);background:var(--panel2);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:6px}
.sd-note p{margin:0}.sd-note .btn{align-self:flex-start}
`;

const versionNo = doc => Math.max(0, ...(doc.versions || []).map(v => v.n || 0)) + 1;
const companyName = store => (store.settings?.company?.name || "").trim();
const proposals = doc => {
  const list = [...(doc.variants || [])].reverse();  // newest first, like step ⑤
  return [...list.filter(v => v.kind !== "render"), ...list.filter(v => v.kind === "render")];
};

/** „Guten Tag Herr Müller,“ — Anrede from the order (Herr/Frau/Familie) with the surname; without one just „Guten Tag,“. */
export function greeting(doc) {
  const name = (doc?.customer || "").trim(), sal = (doc?.contact?.salutation || "").trim();
  if (/^(Herr|Frau|Familie|Fam\.)\s/i.test(name)) return `Guten Tag ${name},`;
  if (!sal || !name) return "Guten Tag,";
  const last = name.split(/\s+/).filter(w => !/^TEST$/.test(w)).pop() || name;
  return `Guten Tag ${sal} ${last},`;
}
/** Default customer note (§16.9). */
export function defaultNote(doc, company = {}) {
  const what = (doc.title || "").trim() || "Ihr Projekt", firm = (company.name || "").trim();
  return `${greeting(doc)}\nanbei unsere Entwürfe für ${what}. Sagen Sie uns gern, welcher Ihnen gefällt.\nViele Grüße${firm ? "\n" + firm : ""}`;
}
/** null when the selection can be sent, else the German reason. */
export function validateSelection(ids) {
  if (!ids || !ids.length) return "Bitte mindestens einen Entwurf wählen.";
  if (ids.length > MAX_SEND) return "Höchstens 4 Entwürfe pro Version.";
  return null;
}
/** One list of everything worth a second look before the customer gets it; n = number in the version. */
export function sendWarnings(selected) {
  const out = [];
  selected.forEach((v, i) => {
    const n = i + 1, r = ratingOf(v.judge || null);
    if (r.tone === "bad" || r.tone === "warn") out.push({ n, id: v.id, kind: "fail", text: `Entwurf ${n} hat die Prüfung nicht bestanden – trotzdem senden?` });
    if (v.caption_backend === "template") out.push({ n, id: v.id, kind: "template", text: `Entwurf ${n} wurde ohne Beschreibungs-KI berechnet – trotzdem senden?` });
    if (Number.isFinite(v.steps) && v.steps < 16) out.push({ n, id: v.id, kind: "draft", text: `Entwurf ${n} ist in Entwurfsqualität – für den Kunden besser „Fein“ berechnen.` });
  });
  return out;
}
/** Short text when there is no note: with the link if there is one. */
export function shareText(firm, link) {
  const von = firm ? ` von ${firm}` : "";
  return link ? `Ihre Entwürfe${von}: ${link}` : `Ihre Entwürfe${von} – das PDF sende ich Ihnen gleich mit.`;
}
export const LINK_INTRO = "Hier können Sie die Entwürfe ansehen und freigeben:";
export const LAN_HINT = "Der Kundenlink funktioniert bisher nur im WLAN Ihrer Werkstatt. Damit Kunden ihn auch zu Hause öffnen können, richten Sie einmal eine Internet-Adresse ein.";
export const LAN_CONFIRM = "Link funktioniert nur im Werkstatt-WLAN – trotzdem senden?";
/** The message for E-Mail and WhatsApp: the carpenter's note with the customer link before the closing. */
export function messageText(note, link, firm = "") {
  const n = String(note || "").trim();
  if (!link) return n || shareText(firm, null);
  if (!n) return shareText(firm, link);
  const block = `${LINK_INTRO}\n${link}`, k = n.search(/\n(Viele|Beste|Freundliche) Grüße/);
  return k >= 0 ? `${n.slice(0, k)}\n\n${block}\n${n.slice(k)}` : `${n}\n\n${block}`;
}
export const mailtoUrl = (to, subject, body) =>
  `mailto:${encodeURIComponent(to || "").replace(/%40/g, "@")}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
/** „0981 123456“ → „49981123456“ (wa.me wants the international number without +), "" when there is none. */
export function waNumber(phone) {
  let d = String(phone || "").trim().replace(/[\s()./-]/g, "");
  if (!d) return "";
  if (d.startsWith("+")) d = d.slice(1);
  else if (d.startsWith("00")) d = d.slice(2);
  else if (d.startsWith("0")) d = "49" + d.slice(1);
  return /^\d{6,15}$/.test(d) ? d : "";
}
export const waUrl = (text, phone = "") => `https://wa.me/${waNumber(phone)}?text=${encodeURIComponent(text)}`;
export const validEmail = e => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(String(e || "").trim());
export function isLocalLink(link) {
  try { const host = new URL(link).hostname; return host === "localhost" || host === "[::1]" || /^127\./.test(host); } catch { return false; }
}
/** Download/share URLs of a stored version (also for re-sending later). */
export function versionLinks(pid, version, settings = {}) {
  const base = `/api/workshop/${pid}/versions/${version.n}`;
  const pub = (settings.public_base_url || "").replace(/\/+$/, "");
  const live = version.status !== "zurueckgezogen" && version.token;
  const lan = live ? version.link && !/^https:/.test(version.link) ? version.link
    : typeof location !== "undefined" ? `${location.origin}/k/${version.token}` : null : null;
  return { pdf: `${base}/pdf`, zip: `${base}/zip`,
           public_link: live && pub ? `${pub}/k/${version.token}` : null,
           qr: live ? `${base}/qr.svg` : null,
           link: lan };
}

function tile(v, label, badgeText, pressed, onClick, sub) {
  const r = ratingOf(v.judge || null);
  const b = h("button", { type: "button", class: "sd-tile", "aria-pressed": String(!!pressed), on: { click: () => onClick(b) } },
    h("img", { src: img(v.image), alt: "", loading: "lazy" }),
    h("span", { class: "sd-check", "aria-hidden": "true" }, badgeText || ""),
    h("span", { class: "sd-name" }, label),
    h("span", { class: "sd-meta" }, sub || [v.fav ? "★" : "", r.short || r.text].filter(Boolean).join(" ")));
  return b;
}
const field = (label, input, hint) => h("label", { class: "field" }, h("span", {}, label), input, hint ? h("small", {}, hint) : null);

/** Firmendaten asked once, inline, before the first delivery: the customer must see who sent it. */
function companyStep(ctx) {
  return new Promise(resolve => {
    const store = ctx.store, c = { ...(store.settings?.company || {}) };
    const inp = (type, value, attrs = {}) => { const i = h("input", { type, autocomplete: "off", ...attrs }); i.value = value || ""; return i; };
    const name = inp("text", c.name, { maxlength: "120", autocomplete: "organization" }), phone = inp("tel", c.phone, { maxlength: "40", inputmode: "tel" });
    const email = inp("email", c.email, { maxlength: "120", inputmode: "email" });
    let logo = c.logo || null;
    const prev = h("div", { class: "sd-row", style: "align-items:center" });
    const showLogo = () => prev.replaceChildren(logo ? h("img", { class: "sd-logo", src: img(logo), alt: "Logo" }) : h("span", { class: "sd-hint" }, "Kein Logo (optional)"),
      h("button", { type: "button", class: "btn secondary", on: { click: async () => {
        const f = await pickImage({});
        if (!f) return;
        try { logo = (await upload(f, { maxSide: 1024 })).path; showLogo(); } catch (e) { toast(e.message, { kind: "err" }); }
      } } }, logo ? "Anderes Logo" : "Logo hinzufügen"));
    showLogo();
    let done = false;
    const noPublic = !(store.settings?.public_base_url || "").trim();
    let s = null;
    s = sheet({ title: "Wer schickt die Entwürfe?", body: h("div", { class: "sd-col" },
      h("p", {}, "Einmalig: Ihre Firmendaten stehen auf dem Kundenblatt und auf der Kundenseite, damit der Kunde weiß, von wem die Entwürfe sind."),
      field("Firmenname", name), field("Telefon", phone), field("E-Mail", email), h("strong", {}, "Logo"), prev,
      noPublic ? h("div", { class: "sd-note" }, h("p", {}, LAN_HINT),
        h("a", { class: "btn ghost", href: "#/einstellungen/kundenlink", on: { click: () => s?.close() } }, "Kundenlink einrichten")) : null,
      h("p", { class: "sd-hint" }, "Ändern können Sie das später unter Einstellungen › Firmendaten.")),
      actions: [{ label: "Speichern und weiter", kind: "primary", onClick: async () => {
        if (!name.value.trim()) { toast("Bitte den Firmennamen eintragen.", { kind: "err" }); name.focus(); return false; }
        try {
          const r = await api("PUT", "/api/settings", { company: { name: name.value.trim(), phone: phone.value.trim(), email: email.value.trim(), logo } });
          store.settings = { ...(store.settings || {}), ...r };
          done = true; resolve(true);
        } catch (e) { toast(e.message || "Speichern fehlgeschlagen", { kind: "err" }); return false; }
      } }],
      onClose: () => { if (!done) resolve(false); } });
    setTimeout(() => name.focus(), 60);
  });
}

/** „An Kunden senden …“: (Firmendaten once), choose 1–4, note, one warning dialog, POST version, result sheet. */
export async function openSend(ctx, { preselect = [], note: noteText = "" } = {}) {
  ensureStyle("sd-style", CSS);
  const store = ctx.store;
  if (!companyName(store) && !(await companyStep(ctx))) return null;
  const doc = store.doc, pid = doc.id;
  const nums = variantNumbers(doc), list = proposals(doc).sort((a, b) => (nums.get(a.id) || 99) - (nums.get(b.id) || 99));
  const byNo = (a, b) => (nums.get(a) || 99) - (nums.get(b) || 99);
  const chosen = preselect.filter(id => list.some(v => v.id === id)).sort(byNo).slice(0, MAX_SEND);
  const count = h("p", { class: "sd-hint", "aria-live": "polite" });
  const grid = h("div", { class: "sd-tiles" });
  const draw = () => {
    grid.replaceChildren(...list.map(v => {
      const k = chosen.indexOf(v.id), name = variantName(doc, v, nums);
      return tile(v, name, k >= 0 ? "✓" : "", k >= 0, () => {
        if (k >= 0) chosen.splice(k, 1);
        else if (chosen.length >= MAX_SEND) return toast("Höchstens 4 Entwürfe pro Version.", { kind: "err" });
        else { chosen.push(v.id); chosen.sort(byNo); }
        draw();
      }, k >= 0 && name !== `Entwurf ${k + 1}` ? `beim Kunden: Entwurf ${k + 1}` : null);
    }));
    count.textContent = `${chosen.length} von höchstens ${MAX_SEND} gewählt`;
  };
  draw();
  const note = h("textarea", { rows: "5" });
  note.value = noteText || defaultNote(doc, store.settings?.company || {});
  const n = versionNo(doc);
  const pinBox = h("input", { type: "checkbox", id: "sd-pin" });
  try { pinBox.checked = localStorage.getItem("ws_send_pin") === "1"; } catch {}
  pinBox.addEventListener("change", () => { try { localStorage.setItem("ws_send_pin", pinBox.checked ? "1" : "0"); } catch {} });
  const body = h("div", { class: "sd-col" },
    h("p", {}, "Welche Entwürfe bekommt der Kunde? (höchstens 4)"), grid, count,
    field("Nachricht an den Kunden", note, "Steht auf dem Kundenblatt, auf der Kundenseite und in der E-Mail/WhatsApp."),
    h("label", { class: "sd-check", for: "sd-pin" }, pinBox, h("span", {}, h("strong", {}, "Link mit PIN schützen"),
      h("span", { class: "sd-hint" }, " – wer den Link weiterleitet, kann ihn ohne die 6-stellige PIN nicht öffnen. Die PIN sagen Sie dem Kunden getrennt, z. B. am Telefon."))),
    h("p", { class: "sd-hint" }, `Als Nächstes: Version ${n} wird erstellt (Kundenblatt, Bilder, Link) – dann schicken Sie sie per E-Mail oder WhatsApp.`));
  let busy = false;
  const s = sheet({ title: "An Kunden senden", body, actions: [{ label: "Für Kunden bereitstellen", kind: "primary", onClick: send }] });
  async function send() {
    if (busy) return false;
    const why = validateSelection(chosen);
    if (why) { toast(why, { kind: "err" }); return false; }
    const sel = chosen.map(id => list.find(v => v.id === id));
    const warns = sendWarnings(sel);
    if (warns.length) {
      const choice = await warnDialog(warns);
      if (choice === "fein") { s.close(); recomputeFine(ctx, warns.filter(w => w.kind === "draft").length, note.value.trim()); return; }
      if (choice !== "send") return false;
    }
    busy = true;
    try {
      const r = await api("POST", `/api/workshop/${pid}/versions`, { variant_ids: [...chosen], customer_note: note.value.trim(), pin: pinBox.checked });
      s.close();
      await refreshDoc(store);
      openResult(ctx, r);
    } catch (e) { toast(e.message || "Speichern fehlgeschlagen", { kind: "err" }); return false; }
    finally { busy = false; }
  }
  return s;
}

function warnDialog(warns) {
  const draft = warns.some(w => w.kind === "draft");
  const buttons = [{ label: "Abbrechen", value: null, kind: "ghost" },
                   { label: "Trotzdem senden", value: "send", kind: draft ? "secondary" : "primary" }];
  if (draft) buttons.push({ label: "Fein berechnen, danach senden", value: "fein", kind: "primary", autofocus: true });
  return choose({ title: "Bitte kurz prüfen", body: h("div", {},
    h("ul", { class: "sd-warn" }, ...warns.map(w => h("li", {}, w.text))),
    draft ? h("p", { class: "sd-hint" }, "„Fein berechnen, danach senden“: die Bilder werden in Fein neu berechnet (ca. 2–3 Min.); danach öffnet sich der Versand von selbst mit den neuen Bildern.") : null), buttons });
}

async function recomputeFine(ctx, n, note) {
  const store = ctx.store, doc = store.doc;
  try {
    const r = await store.generate({ kind: doc.room ? "room" : "free", quality: "fein", count: Math.max(1, Math.min(4, n || 1)) });
    if (r?.job) {
      const v = store.view(doc.id);
      v.sendAfter = { job: r.job.id, note: note || "", at: Date.now() };  // step ⑤ opens the send sheet when it is done
      store.saveView();
      toast("Wird in „Fein“ berechnet – danach öffnet sich der Versand.", { kind: "ok", ms: 6000 });
      ctx.nav?.(`#/p/${doc.id}/ergebnis`);
    }
  } catch (e) { if (!e?.handled) toast(e.message || "Start fehlgeschlagen", { kind: "err" }); }
}

/** Ask the sync loop for the new doc now if the store can, else it arrives with the next poll (≤ 3 s). */
async function refreshDoc(store) {
  try { await (store.syncNow || store.refresh)?.call(store); } catch {}
}

async function fetchFile(url, name, type) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (!r.ok) throw new Error("Datei nicht verfügbar.");
  const b = await r.blob();
  return new File([b], name, { type: type || b.type });
}
const canShareFiles = () => {
  try { return SECURE && !!navigator.share && !!navigator.canShare && navigator.canShare({ files: [new File([""], "x.pdf", { type: "application/pdf" })] }); }
  catch { return false; }
};

/** Result sheet after creating a version — also used to re-send a stored version („Erneut senden …“). */
export function openResult(ctx, r) {
  ensureStyle("sd-style", CSS);
  const store = ctx.store, doc = store.doc, ver = r.version, firm = companyName(store);
  const files = ver.files || {};
  const pdfName = files.pdf || `Visualisierung_V${ver.n}.pdf`, zipName = files.zip || `Visualisierung_V${ver.n}.zip`;
  const link = r.share_link || r.public_link || r.link || null, isPublic = !!r.public_link;
  const subject = `Ihre Entwürfe: ${doc.title || "Ihr Projekt"}${firm ? ` – ${firm}` : ""}`;
  const text = messageText(ver.customer_note, link, firm);
  const col = h("div", { class: "sd-col" });
  const lanOnly = !!link && !isPublic;
  // the version counts as sent (status „Beim Kunden“) only once one of these hand-overs is used
  let marked = !!ver.sent_at || !("sent_at" in ver);
  const markSent = via => {
    if (marked) return;
    marked = true;
    api("POST", `/api/workshop/${doc.id}/versions/${ver.n}/sent`, { via }).then(() => refreshDoc(store), () => { marked = false; });
  };
  if (lanOnly) col.append(h("div", { class: "sd-note" },   // before the send buttons: a carpenter must see it first
    h("p", { class: "sd-alert" }, isLocalLink(link) ? "Achtung: Dieser Link funktioniert nur auf diesem Rechner." : "Achtung: Der Link funktioniert nur im Werkstatt-WLAN."),
    h("p", {}, isLocalLink(link) ? "Bitte zuerst eine Internet-Adresse für den Kundenlink einrichten." : LAN_HINT),
    h("a", { class: "btn secondary", href: "#/einstellungen/kundenlink", on: { click: () => s.close() } }, "Kundenlink einrichten")));

  // 1. send: e-mail / WhatsApp, recipient from the order; missing → asked right here (saved to the order)
  const contact = doc.contact || {};
  const kind = lanOnly ? "btn secondary" : "btn primary";
  const mail = h("a", { class: kind, target: "_blank", rel: "noopener" }, "Per E-Mail senden");
  const wa = h("a", { class: kind, target: "_blank", rel: "noopener noreferrer" }, "Per WhatsApp senden");
  const emailIn = h("input", { type: "email", inputmode: "email", autocomplete: "off", placeholder: "kunde@beispiel.de", "aria-label": "E-Mail des Kunden" });
  const phoneIn = h("input", { type: "tel", inputmode: "tel", autocomplete: "off", placeholder: "z. B. 0171 1234567", "aria-label": "Handynummer des Kunden" });
  emailIn.value = contact.email || ""; phoneIn.value = contact.phone || "";
  const refresh = () => {
    mail.href = mailtoUrl(emailIn.value.trim(), subject, text);
    wa.href = waUrl(text, phoneIn.value);
  };
  const save = (path, inp) => inp.addEventListener("change", () => { const v = inp.value.trim(); if (v !== ((store.doc.contact || {})[path] || "")) store.set(`contact.${path}`, v, { now: true }); });
  emailIn.addEventListener("input", refresh); phoneIn.addEventListener("input", refresh);
  save("email", emailIn); save("phone", phoneIn);
  refresh();
  const askMail = !validEmail(contact.email), askPhone = !waNumber(contact.phone);
  const sendRow = h("div", { class: "sd-row" }, mail, wa);
  col.append(h("strong", {}, "So bekommt der Kunde die Entwürfe"),
    h("p", { class: "sd-hint" }, "Die Nachricht enthält Ihren Text und den Link, über den der Kunde die Entwürfe ansieht, das Kundenblatt (PDF) lädt und einen Entwurf freigibt."),
    askMail ? field("E-Mail des Kunden (fehlt noch)", emailIn) : h("p", { class: "sd-hint" }, `E-Mail an: ${contact.email}`),
    askPhone ? field("Handynummer für WhatsApp (fehlt noch)", phoneIn) : h("p", { class: "sd-hint" }, `WhatsApp an: ${contact.phone}`),
    sendRow);
  const okMail = () => validEmail(emailIn.value) || (toast("Bitte die E-Mail-Adresse des Kunden eintragen.", { kind: "err" }), emailIn.focus(), false);
  const okPhone = () => !!waNumber(phoneIn.value) || (toast("Bitte die Handynummer des Kunden eintragen.", { kind: "err" }), phoneIn.focus(), false);
  // WLAN-only link: the first tap asks, the second (a real link, so the browser opens it) sends
  const confirmRow = (a, label, via) => {
    const yes = h("a", { class: "btn primary", href: a.href, target: "_blank", rel: a.rel, on: { click: () => { markSent(via); sendRow.replaceChildren(mail, wa); } } }, label);
    sendRow.replaceChildren(h("div", { class: "sd-note", style: "flex:1 1 100%" }, h("p", { class: "sd-alert" }, LAN_CONFIRM),
      h("div", { class: "sd-row" }, yes, h("button", { type: "button", class: "btn ghost", on: { click: () => sendRow.replaceChildren(mail, wa) } }, "Abbrechen"))));
  };
  mail.addEventListener("click", e => {
    if (!okMail()) return e.preventDefault();
    if (lanOnly) { e.preventDefault(); return confirmRow(mail, "Ja, trotzdem per E-Mail", "email"); }
    markSent("email");
  });
  wa.addEventListener("click", e => {
    if (!okPhone()) return e.preventDefault();
    if (lanOnly) { e.preventDefault(); return confirmRow(wa, "Ja, trotzdem per WhatsApp", "whatsapp"); }
    markSent("whatsapp");
  });

  // the PIN goes to the customer on another channel than the link
  if (ver.pin && ver.status !== "zurueckgezogen") col.append(h("div", { class: "sd-note sd-pinnote" },
    h("strong", {}, "PIN für diesen Link"), h("p", { class: "sd-pin" }, ver.pin.replace(/(\d{3})(\d{3})/, "$1 $2")),
    h("p", { class: "sd-hint" }, "Nicht in dieselbe Nachricht wie den Link schreiben – am besten am Telefon oder per SMS sagen. Sie finden die PIN jederzeit wieder unter „Erneut senden …“."),
    h("button", { class: "btn secondary", type: "button", on: { click: async () => { if (await copyText(ver.pin) === "copied") toast("PIN kopiert.", { kind: "ok" }); } } }, "PIN kopieren")));

  // 2. the link itself (copy, QR for the tablet on site)
  if (link) {
    const inp = h("input", { class: "sd-link", readonly: "", value: link, "aria-label": "Link für den Kunden" });
    const qrSrc = r.qr_svg ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.qr_svg)}` : r.qr || null;
    col.append(h("strong", {}, "Link für den Kunden"), inp,
      h("div", { class: "sd-row" },
        h("button", { class: "btn secondary", on: { click: async () => { if (await copyText(link) === "copied") { toast("Link kopiert.", { kind: "ok" }); markSent("link"); } } } }, "Link kopieren"),
        SECURE && navigator.share ? h("button", { class: "btn secondary", on: { click: () => { markSent("share"); shareOrFallback({ title: subject, text, url: link }); } } }, "Teilen …") : null),
      qrSrc ? h("details", { class: "details", on: { toggle: e => { if (e.target.open) markSent("qr"); } } }, h("summary", {}, "QR-Code zeigen (Kunde scannt mit dem Handy)"),
        h("img", { class: "sd-qr", src: qrSrc, alt: "QR-Code zum Kundenlink" })) : null);
  }

  // 3. the files, for printing or sending by hand
  const fileRow = h("div", { class: "sd-row" },
    h("a", { class: "btn secondary", href: r.pdf, download: pdfName }, "Kundenblatt (PDF) herunterladen"),
    h("a", { class: "btn secondary", href: r.zip, download: zipName }, "Bilder und PDF (ZIP)"));
  if (canShareFiles()) {
    const pre = { pdf: fetchFile(r.pdf, pdfName, "application/pdf") };
    pre.pdf.catch(() => {});
    fileRow.prepend(h("button", { class: "btn secondary", on: { click: async () => {
      try { markSent("pdf"); await shareOrFallback({ title: subject, text, files: [await pre.pdf] }); } catch { /* the download links stay */ }
    } } }, "PDF teilen …"));
  }
  col.append(h("strong", {}, "Dateien"), fileRow);

  const s = sheet({ title: `Version ${ver.n} ist bereit – jetzt senden`, body: col, actions: [
    { label: "Präsentieren", kind: "secondary", onClick: async () => { s.close(); (await import("./present.js")).present(ctx, { variantIds: ver.variant_ids, start: ver.variant_ids[0] }); } },
    { label: "Freigabe erfassen", kind: "secondary", onClick: () => { s.close(); openApproval(ctx, ver); } },
    { label: "Fertig", kind: "ghost" },
  ] });
  return s;
}

/** Re-open the send sheet for a stored version (links derived from settings and the stored LAN link). */
export function openDelivered(ctx, version) {
  const L = versionLinks(ctx.store.doc.id, version, ctx.store.settings || {});
  return openResult(ctx, { version, pdf: L.pdf, zip: L.zip, public_link: L.public_link, qr: L.qr, qr_svg: null,
                           link: L.link, share_link: L.public_link || L.link, lan_only: !L.public_link });
}

/** Freigabe erfassen: welcher Entwurf, wie (telefonisch / auf Papier / vor Ort), Name, Bemerkung. */
export function openApproval(ctx, version, { variantId = null, via = null } = {}) {
  ensureStyle("sd-style", CSS);
  const store = ctx.store, doc = store.doc, pid = doc.id;
  const vars = (version.variant_ids || []).map(id => doc.variants.find(v => v.id === id) || (doc.trash || []).find(v => v.id === id)).filter(Boolean);
  let pick = variantId && vars.some(v => v.id === variantId) ? variantId
    : doc.approved?.version === version.n ? doc.approved.variant_id : vars.length === 1 ? vars[0].id : null;
  let how = via || doc.approved?.via && doc.approved.via !== "link" && doc.approved.via || "telefon";
  const grid = h("div", { class: "sd-tiles" });
  const nums = variantNumbers(doc);
  const drawTiles = () => grid.replaceChildren(...vars.map((v, i) => {
    const own = variantName(doc, v, nums), label = `Entwurf ${i + 1}`;   // the number the customer sees in this version
    return tile(v, label, pick === v.id ? "✓" : "", pick === v.id, () => { pick = v.id; drawTiles(); }, own !== label ? `= ${own}` : null);
  }));
  drawTiles();
  const HOW = [["telefon", "telefonisch"], ["papier", "auf Papier"], ["vor_ort", "vor Ort"]];
  const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Wie?" });
  const drawSeg = () => seg.replaceChildren(...HOW.map(([k, label]) => h("button", { type: "button", role: "radio", "aria-checked": String(how === k),
    class: how === k ? "active" : "", on: { click: () => { how = k; drawSeg(); } } }, label)));
  drawSeg();
  const name = h("input", { type: "text", autocomplete: "off" });
  name.value = doc.approved?.name || doc.customer || "";
  const comment = h("textarea", { rows: "3" });
  comment.value = doc.approved?.comment || "";
  const body = h("div", { class: "sd-col" },
    h("p", {}, `Version ${version.n}`), h("strong", {}, "Welcher Entwurf?"), grid,
    h("strong", {}, "Wie?"), seg, field("Name des Kunden", name), field("Bemerkung", comment));
  let busy = false;
  const actions = [{ label: "Freigabe speichern", kind: "primary", onClick: async () => {
    if (busy) return false;
    if (!pick) { toast("Bitte den gewählten Entwurf antippen.", { kind: "err" }); return false; }
    busy = true;
    try {
      await api("POST", `/api/workshop/${pid}/approval`, { version: version.n, variant_id: pick, via: how, name: name.value.trim(), comment: comment.value.trim() });
      s.close(); toast("Freigabe gespeichert.", { kind: "ok" }); await refreshDoc(store);
    } catch (e) { toast(e.message || "Speichern fehlgeschlagen", { kind: "err" }); return false; }
    finally { busy = false; }
  } }];
  if (doc.approved) actions.unshift({ label: "Freigabe zurücknehmen", kind: "danger", onClick: async () => {
    if (!await confirmDialog({ title: "Freigabe zurücknehmen", text: "Die erfasste Freigabe wird entfernt. Der Status bleibt.", ok: "Freigabe zurücknehmen", danger: true })) return false;
    try { await api("DELETE", `/api/workshop/${pid}/approval`); s.close(); toast("Freigabe zurückgenommen."); await refreshDoc(store); }
    catch (e) { toast(e.message, { kind: "err" }); return false; }
  } });
  const s = sheet({ title: "Freigabe erfassen", body, actions });
  return s;
}

/** „Dieser gefällt“ in the presentation: approval for the newest live version holding it; else make one first. */
export async function approveFromPresentation(ctx, variantId) {
  const store = ctx.store, doc = store.doc;
  const ver = [...(doc.versions || [])].reverse().find(v => v.status !== "zurueckgezogen" && (v.variant_ids || []).includes(variantId));
  if (ver) return openApproval(ctx, ver, { variantId, via: "vor_ort" });
  const ok = await confirmDialog({ title: "Freigabe erfassen",
    text: "Für diesen Entwurf gibt es noch keine Version. Jetzt eine Version erstellen und die Freigabe vor Ort erfassen?", ok: "Version erstellen" });
  if (!ok) return null;
  try {
    const r = await api("POST", `/api/workshop/${doc.id}/versions`, { variant_ids: [variantId], customer_note: defaultNote(doc, store.settings?.company || {}) });
    await refreshDoc(store);
    return openApproval(ctx, r.version, { variantId, via: "vor_ort" });
  } catch (e) { toast(e.message, { kind: "err" }); return null; }
}
