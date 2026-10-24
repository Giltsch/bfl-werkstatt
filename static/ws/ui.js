// Werkstatt 3 UI helpers (spec §10.4). DOM access only inside functions, so node tests can import this module.
// Escaping contract: every string that came from a doc, a customer or a job goes in via textContent or esc() — h()
// turns string children into text nodes; only `html:` (trusted constants such as icon()) is parsed as markup.

const hasWin = typeof window !== "undefined";
export const $ = (s, el = document) => el.querySelector(s), $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const COARSE = hasWin && typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
export const SECURE = hasWin && window.isSecureContext === true;
export const img = rel => `/outputs/${rel}`;

const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") typeof v === "string" ? (el.style.cssText = v) : Object.assign(el.style, v);
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "html") el.innerHTML = v;                        // trusted markup only (icons)
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("data-") || k.startsWith("aria-") || k === "role" || k === "for" || k === "list") el.setAttribute(k, v === true ? "" : v);
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

function append(el, kids) {
  for (const c of kids) {
    if (c == null || c === false || c === true) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export class ApiError extends Error {
  constructor(status, detail, message) {
    const data = detail && typeof detail === "object" ? detail : null;
    super(message || (data && data.message) || (typeof detail === "string" ? detail : "") ||
      (status === 0 ? "Server nicht erreichbar." : "Unerwarteter Fehler – bitte die Seite neu laden."));
    this.status = status; this.detail = detail; this.data = data; this.code = data ? data.code || null : null;
  }
}

function loginRedirect() {
  if (hasWin) location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search + location.hash);
}

export async function api(method, url, body, {signal} = {}) {
  const opts = {method, credentials: "same-origin", signal, headers: {}};
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch(url, opts); }
  catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, null);
  }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    if (r.status === 401) loginRedirect();
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(r.status, detail);
  }
  return data;
}

// ---------- secure-context gated platform features (the LAN app runs on plain HTTP) ----------

export async function copyText(text) {
  if (SECURE && navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); return "copied"; } catch { /* fall through */ }
  }
  let ok = false;
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
    document.body.append(ta);
    ta.select(); ta.setSelectionRange?.(0, text.length);
    ok = document.execCommand("copy");
    ta.remove();
  } catch { ok = false; }
  if (ok) return "copied";
  const area = h("textarea", {class: "copy-area", readOnly: true, value: text, "aria-label": "Text zum Kopieren"});
  sheet({title: "Text zum Kopieren", body: h("div", {}, h("p", {class: "muted"}, "Text markiert – jetzt kopieren"), area),
    actions: [{label: "Fertig", kind: "primary"}]});
  setTimeout(() => { area.focus(); area.select(); }, 50);
  return "shown";
}

export async function shareOrFallback({title, text, url, files} = {}) {
  if (!(SECURE && typeof navigator !== "undefined" && navigator.share)) return "fallback";
  const data = {title, text, url};
  if (files && files.length) {
    if (!(navigator.canShare && navigator.canShare({files}))) return "fallback";
    data.files = files;
  }
  try { await navigator.share(data); return "shared"; }
  catch (e) { return e && e.name === "AbortError" ? "shared" : "fallback"; }
}

// Links for the caller's fallback row (WhatsApp is text only; e-mail attachments go by hand).
export function shareLinks({subject = "", text = ""} = {}) {
  return {whatsapp: "https://wa.me/?text=" + encodeURIComponent(text),
          mail: "mailto:?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(text)};
}

let attentionText = null, baseTitle = null;
export function attention(text) {
  if (!document.hidden) { toast(text, {kind: "ok", ms: 6000}); return; }
  if (attentionText == null) {
    baseTitle = document.title.replace(/^✓ /, "");
    document.addEventListener("visibilitychange", function back() {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", back);
      document.title = baseTitle;
      toast(attentionText, {kind: "ok", ms: 8000});
      attentionText = null;
    });
  }
  attentionText = text;
  document.title = "✓ " + baseTitle;
  if (COARSE) navigator.vibrate?.(200);
}

export function setTheme(mode) {
  try { localStorage.setItem("ws.theme", mode); } catch { /* private mode */ }
  applyTheme();
}

let themeMq = null;
export function applyTheme() {
  let mode = "light";
  try { mode = localStorage.getItem("ws.theme") || "light"; } catch { /* default */ }
  if (!themeMq && hasWin && window.matchMedia) {
    themeMq = matchMedia("(prefers-color-scheme: dark)");
    themeMq.addEventListener?.("change", applyTheme);
  }
  const dark = mode === "dark" || (mode === "auto" && themeMq && themeMq.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#1b1d20" : "#2f6b4f");
  return mode;
}

// ---------- files ----------

export async function shrink(file, maxSide) {   // also used for photos sent to the assistant
  if (!/^image\/(jpeg|png|webp)$/.test(file.type) || typeof createImageBitmap !== "function") return file;
  let bmp;
  try { bmp = await createImageBitmap(file, {imageOrientation: "from-image"}); } catch { return file; }
  const s = maxSide / Math.max(bmp.width, bmp.height);
  if (s >= 1) { bmp.close?.(); return file; }
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
  const g = c.getContext("2d");
  g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
  g.drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close?.();
  const blob = await new Promise(res => c.toBlob(res, "image/jpeg", 0.9));
  return blob ? new File([blob], (file.name || "foto").replace(/\.\w+$/, "") + ".jpg", {type: "image/jpeg"}) : file;
}

export async function upload(file, {maxSide = 2560, onProgress} = {}) {
  const f = await shrink(file, maxSide);
  const fd = new FormData();
  fd.append("file", f, f.name || "foto.jpg");
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/upload?max_side=${maxSide}`);
    x.withCredentials = true;
    x.upload.onprogress = e => e.lengthComputable && onProgress?.(e.loaded / e.total);
    x.onerror = () => reject(new ApiError(0, null));
    x.onload = () => {
      let d = null;
      try { d = JSON.parse(x.responseText); } catch { /* not json */ }
      if (x.status >= 200 && x.status < 300) return resolve(d);
      if (x.status === 401) loginRedirect();
      reject(new ApiError(x.status, d && typeof d === "object" && "detail" in d ? d.detail : d));
    };
    x.send(fd);
  });
}

export function pickImage({camera = false} = {}) {
  return new Promise(resolve => {
    const inp = h("input", {type: "file", accept: "image/*,.heic,.heif", style: "position:fixed;left:-1000px;top:0;opacity:0"});
    if (camera) inp.setAttribute("capture", "environment");
    let done = false;
    const finish = f => { if (done) return; done = true; inp.remove(); resolve(f); };
    inp.addEventListener("change", () => finish(inp.files && inp.files[0] || null));
    inp.addEventListener("cancel", () => finish(null));
    document.body.append(inp);
    inp.click();
  });
}

// ---------- feedback ----------

function toastHost() {
  return $(".toasts") || document.body.appendChild(h("div", {class: "toasts", role: "status", "aria-live": "polite"}));
}

export function toast(msg, {kind = "info", undo, ms, action} = {}) {
  const t = h("div", {class: `toast ${kind}`}, h("span", {class: "t-msg"}, msg));
  let timer;
  const close = () => { clearTimeout(timer); t.remove(); };
  if (action) t.append(h("button", {class: "btn", type: "button", on: {click: () => { close(); action.run(); }}}, action.label));
  if (undo) t.append(h("button", {class: "btn", type: "button", on: {click: () => { close(); undo(); }}}, "Rückgängig"));
  toastHost().append(t);
  timer = setTimeout(close, ms || (undo || action ? 7000 : kind === "err" ? 6000 : 3500));
  return {close};
}

const FOCUSABLE = 'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function modal(box, {center = false, onClose, dismiss = true, focusBox = false} = {}) {
  const back = h("div", {class: "backdrop" + (center ? " center" : "")}, box);
  const prev = document.activeElement;
  let closed = false;
  const close = (why) => {
    if (closed) return; closed = true;
    back.remove(); document.removeEventListener("keydown", key, true);
    prev && prev.focus && document.contains(prev) && prev.focus({preventScroll: true});
    onClose?.(why);
  };
  const key = e => {
    if (e.key === "Escape" && dismiss) { e.stopPropagation(); close("esc"); }
    if (e.key === "Tab") {
      const f = $$(FOCUSABLE, box).filter(x => x.offsetParent !== null);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  };
  back.addEventListener("mousedown", e => { if (e.target === back && dismiss) close("backdrop"); });
  document.addEventListener("keydown", key, true);
  document.body.append(back);
  setTimeout(() => (box.querySelector("[autofocus]") || (!focusBox && $$(FOCUSABLE, box).find(x => !x.classList.contains("sheet-x"))) || box).focus?.({preventScroll: true}), 30);
  return close;
}

function actionButtons(actions, closeFn) {
  return actions.map(a => h("button", {class: `btn ${a.kind || "secondary"}`, type: "button", disabled: a.disabled,
    on: {click: async e => {
      const btn = e.currentTarget;
      if (!a.onClick) return closeFn("action");
      btn.classList.add("busy");
      try { if ((await a.onClick({close: closeFn, button: btn})) !== false) closeFn("action"); }
      finally { btn.classList.remove("busy"); }
    }}}, a.label));
}

// actions: [{label, kind, onClick}] — onClick may be async; the sheet closes afterwards unless it returns false.
export function sheet({title, body, actions = [], onClose, dismiss = true} = {}) {
  const box = h("div", {class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": title || "", tabindex: "-1"});
  let close;
  box.append(h("div", {class: "sheet-head"}, h("h2", {}, title || ""),
    dismiss ? h("button", {class: "btn ghost icon-only sheet-x", type: "button", "aria-label": "Schließen", html: icon("x"), on: {click: () => close("x")}}) : null));
  box.append(h("div", {class: "sheet-body"}, body || null));
  const acts = h("div", {class: "sheet-actions"});
  box.append(acts);
  close = modal(box, {onClose, dismiss, focusBox: true});
  acts.append(...actionButtons(actions, close));
  if (!actions.length) acts.remove();
  return {close, el: box};
}

// Generic choice dialog → resolves the chosen button's value (null on Esc/backdrop).
export function choose({title, text, body, buttons = [], dismiss = true}) {
  return new Promise(resolve => {
    let value = null;
    const box = h("div", {class: "dialog", role: "alertdialog", "aria-modal": "true", "aria-label": title || "", tabindex: "-1"},
      h("div", {class: "dialog-head"}, h("h2", {}, title || "")),
      h("div", {class: "dialog-body"}, text ? h("p", {}, text) : null, body || null));
    const acts = h("div", {class: "dialog-actions"});
    box.append(acts);
    const close = modal(box, {center: true, dismiss, focusBox: true, onClose: () => resolve(value)});
    for (const b of buttons) {
      acts.append(h("button", {class: `btn ${b.kind || "secondary"}`, type: "button", disabled: b.disabled, autofocus: b.autofocus,
        on: {click: () => { value = b.value; close("action"); }}}, b.label));
    }
  });
}

export function confirmDialog({title, text, ok = "OK", cancel = "Abbrechen", danger = false, expect} = {}) {
  let body = null, okBtn;
  if (expect != null) {
    const inp = h("input", {class: "input", type: "text", autocomplete: "off", autofocus: true, "aria-label": "Bestätigung",
      on: {input: () => { okBtn.disabled = inp.value.trim() !== String(expect).trim(); }}});
    body = h("div", {class: "field"}, inp);
  }
  const p = choose({title, text, body, buttons: [
    {label: cancel, value: false, kind: "secondary"},
    {label: ok, value: true, kind: danger ? "danger" : "primary", disabled: expect != null}]});
  if (expect != null) okBtn = body.closest(".dialog").querySelector(".dialog-actions .btn:last-child");
  return p.then(v => v === true);
}

export function menu(anchorEl, items, {title} = {}) {
  const txt = (anchorEl?.textContent || "").replace(/[⋯…]/g, "").trim();
  title = title || anchorEl?.getAttribute?.("aria-label") || (txt.length <= 40 ? txt : "") || "Menü";
  const mk = (it, close) => h(it.href ? "a" : "button", {class: "menu-item" + (it.danger ? " danger" : ""), type: it.href ? null : "button",
    href: it.href || null, html: it.icon ? icon(it.icon) : null,
    on: {click: () => { close(); it.onClick?.(); }}}, it.label);
  if (!hasWin || innerWidth < 700) {
    let s;
    const list = h("div", {class: "col", style: "gap:2px"});
    s = sheet({title, body: list});
    list.append(...items.map(it => mk(it, () => s.close())));
    return s;
  }
  const pop = h("div", {class: "menu-pop", role: "menu", "aria-label": title});
  const close = modal(pop, {});
  pop.parentElement.style.background = "transparent";
  pop.append(...items.map(it => mk(it, close)));
  const r = anchorEl.getBoundingClientRect();
  pop.style.top = Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8) + "px";
  pop.style.left = Math.max(8, Math.min(r.right - pop.offsetWidth, innerWidth - pop.offsetWidth - 8)) + "px";
  return {close};
}

// Toggle row (whole row tappable) — the replacement for native checkboxes.
export function toggleRow({title, text, checked = false, disabled = false, onChange}) {
  const sub = h("span", {class: "tr-sub"}, text || "");
  const row = h("button", {class: "toggle-row", type: "button", role: "switch", "aria-checked": String(!!checked), "aria-disabled": disabled ? "true" : null},
    h("span", {class: "tr-text"}, h("span", {class: "tr-title"}, title), text ? sub : null), h("span", {class: "switch", "aria-hidden": "true"}));
  row.addEventListener("click", () => {
    if (row.getAttribute("aria-disabled") === "true") return;
    const v = row.getAttribute("aria-checked") !== "true";
    row.setAttribute("aria-checked", String(v));
    onChange?.(v);
  });
  row.set = v => row.setAttribute("aria-checked", String(!!v));
  row.setText = t => { sub.textContent = t || ""; if (t && !sub.parentNode) row.firstChild.append(sub); };
  return row;
}

// ---------- icons (24 px line icons, stroke = currentColor) ----------
const P = {
  back: '<path d="M15 5l-7 7 7 7"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.01"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  undo: '<path d="M9 7L4 12l5 5"/><path d="M4 12h10a6 6 0 010 12h-2" transform="translate(0 -6)"/>',
  rotate: '<path d="M20 12a8 8 0 11-2.3-5.7"/><path d="M20 4v5h-5"/>',
  wall: '<path d="M3 4v16M7 6h14M7 12h14M7 18h14M12 6v6M17 12v6"/>',
  ruler: '<rect x="2.5" y="8" width="19" height="8" rx="1.5"/><path d="M6.5 8v3M10 8v4M13.5 8v3M17 8v4"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  share: '<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.2 10.8l7.6-3.6M8.2 13.2l7.6 3.6"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  star: '<path d="M12 3.8l2.5 5.2 5.7.8-4.1 4 1 5.6-5.1-2.7-5.1 2.7 1-5.6-4.1-4 5.7-.8z"/>',
  "star-fill": '<path fill="currentColor" d="M12 3.8l2.5 5.2 5.7.8-4.1 4 1 5.6-5.1-2.7-5.1 2.7 1-5.6-4.1-4 5.7-.8z"/>',
  pdf: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 13h6M9 17h4"/>',
  link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4.5 4.2-6.5 8-6.5s7 2 8 6.5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
  phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  home: '<path d="M3 11l9-7 9 7v9H3z"/><path d="M10 20v-6h4v6"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3.5 6v.01M3.5 12v.01M3.5 18v.01"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  wifi: '<path d="M2 9a15 15 0 0120 0M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0"/><path d="M12 19.5v.01"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21M8.5 21h7"/>',
  send: '<path d="M4 12l16-8-6 16-2.5-6.5z"/><path d="M11.5 13.5L20 4"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3M14 9l2 2"/>',
};
export function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${P[name] || P.more}</svg>`;
}

// ---------- formatting ----------
const nf = (d) => new Intl.NumberFormat("de-DE", {minimumFractionDigits: d, maximumFractionDigits: d});
const WD = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const pad2 = n => String(n).padStart(2, "0");
const toDate = iso => { const d = iso instanceof Date ? iso : new Date(iso); return isNaN(d) ? null : d; };
export const fmt = {
  num(n, d = 0) { return n == null || isNaN(n) ? "" : nf(d).format(Number(n)); },
  cm(n) { return n == null || n === "" ? "" : `${fmt.num(n)} cm`; },
  m(cm) { return cm == null || cm === "" ? "" : `${fmt.num(cm / 100, 2)} m`; },
  min(s) { return Math.max(1, Math.round((Number(s) || 0) / 60)); },
  // „ca. 40 Sek.“ / „ca. 3½ Min.“ — the same rounding as the server's eta_text (jobs.dur_de)
  span(s) {
    s = Number(s) || 0;
    if (s < 45) return `ca. ${Math.max(10, Math.floor(s / 10 + 0.5) * 10)} Sek.`;
    if (s >= 600) return `ca. ${Math.round(s / 60)} Min.`;
    const m = Math.floor(s / 30 + 0.5) / 2;
    return `ca. ${Math.floor(m)}${m % 1 ? "½" : ""} Min.`;
  },
  dur(s) {
    if (s == null || isNaN(s)) return "";
    if (s < 10) return "gleich";
    if (s < 60) return `ca. ${Math.max(10, Math.round(s / 10) * 10)} Sek.`;
    return `ca. ${fmt.min(s)} Min.`;
  },
  // remaining time of a running job: never a frozen „1 s“ — the last seconds read „gleich fertig …“
  rest(s) {
    if (s == null || isNaN(s)) return "";
    if (s <= 20) return "gleich fertig …";
    if (s < 60) return `noch ca. ${Math.max(30, Math.round(s / 10) * 10)} Sek.`;
    return `noch ca. ${fmt.min(s)} Min.`;
  },
  restShort(s) { return s == null || isNaN(s) ? "" : s <= 20 ? "gleich" : s < 60 ? "< 1 Min." : `${fmt.min(s)} Min.`; },
  date(iso) { const d = toDate(iso); return d ? `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}.${d.getFullYear()}` : ""; },
  dateTime(iso) {
    const d = toDate(iso);
    return d ? `${WD[d.getDay()]} ${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}., ${pad2(d.getHours())}:${pad2(d.getMinutes())}` : "";
  },
  ago(iso, now = Date.now()) {
    const d = toDate(iso);
    if (!d) return "";
    const s = (now - d.getTime()) / 1000;
    if (s < 60) return "gerade eben";
    if (s < 3600) return `vor ${Math.floor(s / 60)} Min.`;
    if (s < 86400) return `vor ${Math.floor(s / 3600)} Std.`;
    if (s < 172800) return "gestern";
    if (s < 14 * 86400) return `vor ${Math.floor(s / 86400)} Tagen`;
    return fmt.date(d);
  },
};

export const STATUS = [
  {id: "anfrage", label: "Anfrage", tone: "info"},
  {id: "termin", label: "Vor-Ort-Termin", tone: "info"},
  {id: "entwurf", label: "Entwurf", tone: "warn"},
  {id: "kunde", label: "Beim Kunden", tone: "info"},
  {id: "freigegeben", label: "Freigegeben", tone: "ok"},
  {id: "auftrag", label: "Auftrag", tone: "ok"},
  {id: "fertig", label: "Abgeschlossen", tone: "muted"},
  {id: "abgesagt", label: "Abgesagt", tone: "bad"},
];
export function statusLabel(id) { return (STATUS.find(s => s.id === id) || {}).label || id || ""; }
export function statusTone(id) { return (STATUS.find(s => s.id === id) || {}).tone || "muted"; }

export function ratingOf(judge) {
  if (!judge) return {tone: "none", text: "ohne Prüfung", short: "–"};
  if (judge.status === "pending") return {tone: "pending", text: "Prüfung läuft …", short: "…"};
  if (judge.status !== "ok") return {tone: "none", text: "Prüfung nicht möglich", short: "–"};
  const x = fmt.num(judge.overall ?? 0, 1);
  if (judge.pass) return {tone: "ok", text: `✓ Gut (${x})`, short: `✓ ${x}`};
  if ((judge.overall ?? 0) >= 5) return {tone: "warn", text: `⚠ Bitte prüfen (${x})`, short: `⚠ ${x}`};
  return {tone: "bad", text: `✗ Mangelhaft (${x})`, short: `✗ ${x}`};
}

export function pieceLabel(piece, i = 0, catalog = null) {
  if (!piece) return `Möbel ${i + 1}`;
  const t = piece.type && catalog && catalog.pieces && catalog.pieces[piece.type];
  if (t) return t.de;
  if (piece.type && piece.label) return piece.label;
  const text = (piece.text || "").trim();
  if (text) return text.length > 32 ? text.slice(0, 30).trimEnd() + " …" : text;
  return piece.label || `Möbel ${i + 1}`;
}

export function positionsText(doc, variantId, catalog = null) {
  const v = (doc.variants || []).find(x => x.id === variantId) || (doc.trash || []).find(x => x.id === variantId);
  if (!v) return "";
  const live = Object.fromEntries((doc.pieces || []).map(p => [p.id, p]));
  const rows = (v.pieces && v.pieces.length ? v.pieces : (v.piece_ids || []).map(id => live[id]).filter(Boolean));
  const lines = rows.map((r, i) => {
    const dims = [["B", r.w], ["H", r.h], ["T", r.d]].filter(([, x]) => x != null && x !== "");
    return [r.label || pieceLabel(live[r.id] || r, i, catalog), r.material_label, r.handles_label,
      dims.length ? dims.map(([k, x]) => `${k} ${x}`).join(" × ") + " cm" : ""].filter(Boolean).join(", ");
  });
  const ver = [...(doc.versions || [])].reverse().find(x => (x.variant_ids || []).includes(variantId) &&
    (!doc.approved || doc.approved.variant_id !== variantId || doc.approved.version === x.n)) ;
  const parts = [doc.ref ? `Visualisierung Vorgang ${doc.ref}` : `Visualisierung ${doc.title || ""}`.trim()];
  if (ver) parts.push(`Version ${ver.n}`, `Entwurf ${ver.variant_ids.indexOf(variantId) + 1}`);
  let tail = parts.join(", ");
  if (doc.approved && doc.approved.variant_id === variantId) tail += ` – freigegeben am ${fmt.date(doc.approved.at)}`;
  return [...lines, `(${tail})`].join("\n");
}

// Phone layout (portrait < 700 px wide, or landscape < 500 px high): step ③ runs immersive there (§10.2).
export function phoneLayout() {
  return hasWin && matchMedia("(max-width: 699px), (max-height: 499px)").matches;
}

/** Stable numbers of the proposals („Entwurf 1“, „Entwurf 2“ …): chronological, deleted ones keep their number. */
export function variantNumbers(doc) {
  const seen = new Set(), list = [];
  for (const v of [...(doc?.variants || []), ...(doc?.trash || [])]) {
    if (!v || v.kind === "render" || seen.has(v.id)) continue;
    seen.add(v.id); list.push(v);
  }
  list.sort((a, b) => String(a.image).localeCompare(String(b.image)));
  return new Map(list.map((v, i) => [v.id, i + 1]));
}
/** A version counts as sent after a send button, the customer's first view or an approval (older versions: always). */
export const versionSent = v => !!v?.sent_at || !(v && "sent_at" in v);
/** „Version 2 · gesendet 18.10.2026 von Anna“ / „Version 2 · bereitgestellt 18.10.2026, noch nicht gesendet“. */
export function versionText(v, nameOf = x => x) {
  const sent = versionSent(v), who = nameOf(sent ? v.sent_by || v.by : v.by);
  return `Version ${v.n} · ${sent ? `gesendet ${fmt.date(v.sent_at || v.created)}` : `bereitgestellt ${fmt.date(v.created)}`}${who ? ` von ${who}` : ""}${sent ? "" : ", noch nicht gesendet"}`;
}
/** [label, tone] of a version's badge. */
export function versionBadge(v) {
  if (v.status === "offen") return versionSent(v) ? ["gesendet", "info"] : ["noch nicht gesendet", "warn"];
  return { angesehen: ["angesehen", "info"], freigegeben: ["freigegeben", "ok"], aenderung: ["Änderungswunsch", "warn"],
           zurueckgezogen: ["zurückgezogen", "none"] }[v.status] || [v.status, "info"];
}
export function variantName(doc, v, nums = null) {
  if (!v) return "Entwurf";
  if (v.kind === "render") return "Skizzen-Rendering";
  const n = (nums || variantNumbers(doc)).get(v.id);
  return n ? `Entwurf ${n}` : "Entwurf";
}

/** Overall progress (0–1) of a job: the server's figure (time-based over caption + images), else from the images. */
export function jobProgress(j) {
  if (!j) return 0;
  if (Number.isFinite(j.progress)) return Math.max(0, Math.min(1, j.progress));
  const n = Math.max(1, j.count || 1);
  return Math.min(1, ((j.done_images || 0) + (j.phase === "image" && j.steps ? (j.step || 0) / j.steps : 0)) / n);
}
/** „Bild 1 von 2 wird gerechnet … · noch ca. 2 Min.“ — the one progress line of a job. */
export function jobLine(j) {
  if (!j) return "";
  const stage = (j.stage_text || "In der Warteschlange …").replace(/ Schritt \d+\/\d+$/, "");
  if (j.eta_text != null) return j.eta_text && (j.state === "running" || /pending|running/.test(j.judge_state || "")) ? `${stage} · ${j.eta_text}` : stage;  // server text: every device the same
  if (j.state !== "running" || !(j.eta_done_s >= 0) || /gleich fertig/.test(stage)) return stage;
  return `${stage} · ${fmt.rest(j.eta_done_s)}`;
}

export function deviceLabel() {
  if (!hasWin) return "PC";
  const small = Math.min(screen.width || innerWidth, screen.height || innerHeight);
  if (COARSE) return small < 600 ? "Handy" : "Tablet";
  return "PC";
}

const ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789";
export function uid(prefix = "") {
  const a = new Uint8Array(10);
  (globalThis.crypto && crypto.getRandomValues) ? crypto.getRandomValues(a) : a.forEach((_, i) => (a[i] = Math.random() * 256));
  return prefix + [...a].map(b => ALNUM[b % 36]).join("").replace(/^[0-9]/, "k");
}

export function debounce(fn, ms) {
  let t = null, args = null;
  const run = () => { t = null; const a = args; args = null; fn(...a); };
  const d = (...a) => { args = a; clearTimeout(t); t = setTimeout(run, ms); };
  d.flush = () => { if (t) { clearTimeout(t); run(); } };
  d.cancel = () => { clearTimeout(t); t = null; args = null; };
  d.pending = () => t != null;
  return d;
}
