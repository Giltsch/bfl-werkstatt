// Before/after slider (shared with step ⑤) and the fullscreen customer presentation.
import { h, img } from "./ui.js";

const CSS = `
.rs-wrap{display:flex;justify-content:center;width:100%}
.rs-cmp{position:relative;overflow:hidden;border-radius:var(--r);background:var(--stage);touch-action:pan-y;user-select:none;-webkit-user-select:none;cursor:ew-resize;outline-offset:3px}
.rs-cmp img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;pointer-events:none;-webkit-user-drag:none}
.rs-cmp.single{cursor:default}
.rs-handle{position:absolute;top:0;bottom:0;width:0;border-left:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.35);pointer-events:none}
.rs-knob{position:absolute;top:50%;left:-23px;width:44px;height:44px;margin-top:-22px;border-radius:50%;background:#fff;color:#1d1b18;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 1px 6px rgba(0,0,0,.4)}
.rs-lab{position:absolute;top:10px;padding:4px 10px;border-radius:999px;background:rgba(0,0,0,.6);color:#fff;font-size:14px;pointer-events:none}
.rs-lab.l{left:10px}.rs-lab.r{right:10px}
.pr-ov{position:fixed;inset:0;z-index:1000;background:var(--stage);color:#fff;display:flex;flex-direction:column;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
.pr-top{display:flex;align-items:center;gap:12px;padding:8px 16px;min-height:56px}
.pr-top img{max-height:40px;max-width:120px;object-fit:contain}
.pr-top .t{flex:1;min-width:0}.pr-top .t b{display:block;font-size:18px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pr-top .t span{font-size:14px;opacity:.8}
.pr-mid{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:0 8px;position:relative}
.pr-bot{display:flex;align-items:center;gap:8px;padding:8px 16px 12px;flex-wrap:wrap;justify-content:center}
.pr-bot .n{min-width:120px;text-align:center;font-size:16px}
.pr-bot .hint{width:100%;text-align:center;font-size:14px;opacity:.75}
.pr-ov .btn.ghost,.pr-ov .btn.secondary{color:#fff;border-color:rgba(255,255,255,.4);background:transparent}
`;
export function ensureStyle(id, css) {
  if (typeof document === "undefined" || document.getElementById(id)) return;
  document.head.append(h("style", { id }, css));
}

const KNOWN = s => s && s[0] > 0 && s[1] > 0;
/** What the slider shows left of a proposal: {src, labels: [left, right]} or null (single image). */
export function beforeOf(v, doc) {
  if (!v) return null;
  const kind = v.kind || (doc?.room ? "room" : "free");
  const all = [...(doc?.variants || []), ...(doc?.trash || [])];
  if (kind === "render") {
    const sk = v.sketch || (doc?.pieces || []).find(p => p.id === v.piece_ids?.[0])?.sketch;
    return sk ? { src: sk, labels: ["Skizze", "Entwurf"] } : null;
  }
  if (v.from) {
    const parent = all.find(x => x.id === v.from);
    return parent?.image ? { src: parent.image, labels: ["Vorher", "Nachher"] } : null;
  }
  if (kind === "room") {
    if (v.room) return { src: v.room, labels: ["Vorher", "Nachher"] };
    // older variants did not store their photo: the current one only if the aspect matches
    const a = KNOWN(v.size) && v.size[0] / v.size[1], b = KNOWN(doc?.room_size) && doc.room_size[0] / doc.room_size[1];
    if (doc?.room && a && b && Math.abs(a - b) < 0.02) return { src: doc.room, labels: ["Vorher", "Nachher"] };
  }
  return null;
}
export const aspectOf = (v, doc) => KNOWN(v?.size) ? v.size[0] / v.size[1]
  : KNOWN(doc?.room_size) ? doc.room_size[0] / doc.room_size[1] : 4 / 3;
export const clampPos = p => Math.max(0, Math.min(100, Number.isFinite(+p) ? +p : 50));
/** [left, right] (0–1) of the furniture on the photo: the variant's boxes, else its room ancestor's, else null. */
export function focusOf(v, doc) {
  if (Array.isArray(v?.focus)) return v.focus;
  const all = [...(doc?.variants || []), ...(doc?.trash || [])], seen = new Set([v?.id]);
  let x = v;
  while (x && !(x.boxes && Object.keys(x.boxes).length) && x.from && !seen.has(x.from)) { seen.add(x.from); x = all.find(y => y.id === x.from); }
  const bs = Object.values(x?.boxes || {}).filter(b => Array.isArray(b) && b.length === 4);
  return bs.length ? [Math.min(...bs.map(b => b[1])) / 1000, Math.max(...bs.map(b => b[3])) / 1000] : null;
}
/** Where the slider starts: just left of the furniture, so all of it is on the „Nachher“ side (unknown: 10 %). */
export function startPos(v, doc) {
  const f = focusOf(v, doc);
  return f ? Math.max(0, Math.min(85, Math.round(f[0] * 100 - 3))) : 10;
}
const revealed = new Set();

/**
 * Slider: Vorher on the LEFT (the before image is clipped to 0..pos %), Nachher on the right.
 * Drag, tap, double-tap (ganz vorher / ganz nachher), arrow keys. `swipe(dir)` (presentation): on touch only a drag
 * that starts at the handle moves it; any other horizontal swipe calls swipe(±1).
 * fit(): {w, h} available — the slider keeps the image's aspect inside it.
 */
export function slider(v, doc, { pos = null, onPos, swipe, fit, keys = true, reveal = true } = {}) {
  ensureStyle("rs-style", CSS);
  const b = beforeOf(v, doc), ratio = aspectOf(v, doc);
  const wrap = h("div", { class: "rs-wrap" });
  const after = h("img", { src: img(v.image), alt: b ? b.labels[1] : "Entwurf", draggable: "false" });
  const c = h("div", { class: "rs-cmp" + (b ? "" : " single") }, after);
  wrap.append(c);
  if (pos == null) pos = startPos(v, doc);
  let p = clampPos(pos), full = p >= 50, ro = null;
  const size = () => {
    const box = fit ? fit() : { w: wrap.clientWidth || 360, h: Math.round((globalThis.innerHeight || 800) * 0.6) };
    let w = Math.max(80, box.w), hh = w / ratio;
    if (hh > box.h) { hh = Math.max(80, box.h); w = hh * ratio; }
    c.style.width = `${Math.round(w)}px`; c.style.height = `${Math.round(hh)}px`;
  };
  if (typeof ResizeObserver !== "undefined") { ro = new ResizeObserver(() => size()); ro.observe(wrap); }
  addEventListener("resize", size);
  requestAnimationFrame?.(size);
  if (!b) {
    if (swipe) swipeOnly(c, swipe);
    return { el: wrap, set() {}, destroy() { ro?.disconnect(); removeEventListener("resize", size); } };
  }
  const before = h("img", { src: img(b.src), alt: b.labels[0], draggable: "false" });
  const handle = h("div", { class: "rs-handle" }, h("span", { class: "rs-knob", "aria-hidden": "true" }, "⇔"));
  c.append(before, handle, h("span", { class: "rs-lab l" }, b.labels[0]), h("span", { class: "rs-lab r" }, b.labels[1]));
  c.tabIndex = 0;
  c.setAttribute("role", "slider");
  c.setAttribute("aria-label", `${b.labels[0]} / ${b.labels[1]}`);
  c.setAttribute("aria-valuemin", "0"); c.setAttribute("aria-valuemax", "100");
  const set = (q, save = true) => {
    p = clampPos(q);
    before.style.clipPath = `inset(0 ${100 - p}% 0 0)`;
    handle.style.left = `${p}%`;
    c.setAttribute("aria-valuenow", String(Math.round(p)));
    if (save) onPos?.(p);
  };
  set(p, false);
  // first look at a proposal: wipe from „Vorher“ to the start position, the furniture appears (once per proposal)
  const calm = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reveal && !calm && !revealed.has(v.id) && typeof requestAnimationFrame === "function") {
    revealed.add(v.id);
    const target = p, t0 = performance.now() + 450;
    set(100, false);
    const step = t => {
      if (drag) return;
      const k = Math.max(0, Math.min(1, (t - t0) / 1100)), e = 1 - Math.pow(1 - k, 3);
      set(100 + (target - 100) * e, false);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  const at = e => { const r = c.getBoundingClientRect(); set((e.clientX - r.left) / r.width * 100); };
  let drag = null, last = { t: -1e9, x: 0, y: 0 };
  c.addEventListener("pointerdown", e => {
    if (e.button > 0) return;
    const r = c.getBoundingClientRect(), onHandle = Math.abs(e.clientX - (r.left + r.width * p / 100)) < 32;
    drag = { id: e.pointerId, moved: false, x: e.clientX, y: e.clientY, touch: e.pointerType === "touch",
             mode: swipe && e.pointerType === "touch" && !onHandle ? "swipe" : "slide" };
    if (drag.mode === "slide") { try { c.setPointerCapture(e.pointerId); } catch {} }
    if (!drag.touch && drag.mode === "slide") at(e);  // touch waits: a vertical scroll must not jump the slider first
  });
  c.addEventListener("pointermove", e => {
    if (!drag || drag.mode !== "slide") return;
    if (drag.moved || Math.abs(e.clientX - drag.x) > 3) { drag.moved = true; at(e); }
  });
  c.addEventListener("pointerup", e => {
    if (!drag) return;
    const d = drag; drag = null;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (d.mode === "swipe") {
      if (Math.abs(dx) > 60 && Math.abs(dx) > 1.5 * Math.abs(dy)) return swipe(dx < 0 ? 1 : -1);
    } else if (!d.moved && !d.touch) at(e);
    else if (!d.moved && d.touch && !swipe) at(e);
    if (Math.hypot(dx, dy) < 10 && e.timeStamp - last.t < 320 && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 24) {
      full = !full; set(full ? 100 : 0); last.t = -1e9;
    } else if (Math.hypot(dx, dy) < 10) last = { t: e.timeStamp, x: e.clientX, y: e.clientY };
  });
  c.addEventListener("pointercancel", () => { drag = null; });
  if (keys) c.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); e.stopPropagation(); set(p + (e.key === "ArrowRight" ? 5 : -5)); }
    else if (e.key === "Home" || e.key === "End") { e.preventDefault(); set(e.key === "End" ? 100 : 0); }
  });
  return { el: wrap, set, get pos() { return p; }, get dragging() { return !!drag; }, destroy() { ro?.disconnect(); removeEventListener("resize", size); } };
}
function swipeOnly(el, swipe) {
  let s = null;
  el.addEventListener("pointerdown", e => { s = { x: e.clientX, y: e.clientY }; });
  el.addEventListener("pointerup", e => {
    if (!s) return;
    const dx = e.clientX - s.x, dy = e.clientY - s.y; s = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > 1.5 * Math.abs(dy)) swipe(dx < 0 ? 1 : -1);
  });
}

let OPEN = null;
/** Fullscreen presentation for the customer: no editing controls, „Dieser gefällt“ → approval (vor Ort). */
export function present(ctx, { variantIds, start } = {}) {
  ensureStyle("rs-style", CSS);
  OPEN?.close();
  const store = ctx.store, doc = store.doc;
  const list = (variantIds || []).map(id => doc.variants.find(v => v.id === id)).filter(Boolean);
  if (!list.length) return null;
  let i = Math.max(0, list.findIndex(v => v.id === start)), sl = null;
  const company = store.settings?.company || {};
  const logo = company.logo ? h("img", { src: img(company.logo), alt: "" }) : null;
  const top = h("div", { class: "pr-top" }, logo,
    h("div", { class: "t" }, h("b", {}, doc.title || "Ihre Entwürfe"), h("span", {}, company.name || doc.customer || "")));
  const mid = h("div", { class: "pr-mid" });
  const count = h("span", { class: "n", "aria-live": "polite" });
  const prev = h("button", { class: "btn ghost", "aria-label": "Vorheriger Entwurf", on: { click: () => go(-1) } }, "‹");
  const next = h("button", { class: "btn ghost", "aria-label": "Nächster Entwurf", on: { click: () => go(1) } }, "›");
  const close = h("button", { class: "btn secondary", on: { click: () => api.close() } }, "Schließen");
  const like = h("button", { class: "btn primary", on: { click: () => liked() } }, "Dieser gefällt");
  const bot = h("div", { class: "pr-bot" }, close, prev, count, next, like,
    list.length > 1 ? h("span", { class: "hint" }, "Wischen: nächster Entwurf") : null);
  const ov = h("div", { class: "pr-ov", role: "dialog", "aria-modal": "true", "aria-label": "Präsentation" }, top, mid, bot);
  const show = () => {
    sl?.destroy();
    const v = list[i];
    sl = slider(v, doc, { swipe: list.length > 1 ? go : null, keys: false,
                          fit: () => ({ w: mid.clientWidth - 16, h: mid.clientHeight - 8 }) });
    mid.replaceChildren(sl.el);
    count.textContent = `Entwurf ${i + 1} von ${list.length}`;
    prev.hidden = next.hidden = list.length < 2;
  };
  function go(d) { i = (i + d + list.length) % list.length; show(); }
  async function liked() {
    const v = list[i];
    const { approveFromPresentation } = await import("./send.js");
    api.close();
    approveFromPresentation(ctx, v.id);
  }
  const onKey = e => {
    if (e.key === "Escape") { e.preventDefault(); api.close(); }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); go(1); }
    else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); go(-1); }
  };
  const onFs = () => { if (!(document.fullscreenElement || document.webkitFullscreenElement) && ov.dataset.fs) api.close(); };
  const api = {
    el: ov,
    close() {
      if (OPEN !== api) return;
      OPEN = null;
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("fullscreenchange", onFs); document.removeEventListener("webkitfullscreenchange", onFs);
      sl?.destroy(); ov.remove();
      const fe = document.fullscreenElement || document.webkitFullscreenElement;
      if (fe) (document.exitFullscreen || document.webkitExitFullscreen)?.call(document)?.catch?.(() => {});
    },
    go, get index() { return i; },
  };
  OPEN = api;
  document.body.append(ov);
  document.addEventListener("keydown", onKey, true);
  document.addEventListener("fullscreenchange", onFs); document.addEventListener("webkitfullscreenchange", onFs);
  const fs = ov.requestFullscreen || ov.webkitRequestFullscreen;  // iPhone Safari: none → the fixed overlay is it
  if (fs) { try { const r = fs.call(ov); ov.dataset.fs = "1"; r?.catch?.(() => { delete ov.dataset.fs; }); } catch { delete ov.dataset.fs; } }
  show();
  like.focus();
  return api;
}

/** „vor Ihnen: Anna – Küche Huber (2 Vorschläge)“ for a waiting job (§16.11), else "". */
export const aheadLine = j => j && j.position > 0 && (j.ahead || []).length
  ? "vor Ihnen: " + j.ahead.map(a => `${a.owner_name || "Jemand"} – ${a.project_title || "Auftrag"}${a.label ? ` (${a.label})` : ""}`).join(", ") : "";
