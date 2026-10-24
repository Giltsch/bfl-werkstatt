/* Guided project editor (#/p/<pid>[/<step>]): stepper, the mounted step module and the action bar [Zurück] [primary].
   Steps ①–③ live in step-raum/-moebel/-platzieren.js, ④–⑤ in step-berechnen/-ergebnis.js (spec §10.6, §12).
   Shared helpers for the step modules are exported from here (pure ones are unit-tested in tests/ws_editor.test.mjs). */
import { toast, sheet, icon, confirmDialog } from "./ui.js";

export const STEPS = [
  { id: "raum", name: "Raum" }, { id: "moebel", name: "Möbel" }, { id: "platzieren", name: "Platzieren" },
  { id: "berechnen", name: "Berechnen" }, { id: "ergebnis", name: "Ergebnis & Kunde" },
];
const FILES = { raum: "./step-raum.js", moebel: "./step-moebel.js", platzieren: "./step-platzieren.js",
  berechnen: "./step-berechnen.js", ergebnis: "./step-ergebnis.js" };
export const COLORS = (globalThis.W3D && globalThis.W3D.COLORS) || ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9"];
export const MISSING = "Dieser Schritt ist noch nicht verfügbar.";
const DAY = 24 * 3600e3;

/* ---------------- pure helpers (no DOM) ---------------- */
export const NONE = () => ({ mode: "none", pose: null, box: null });
export const clone = o => (o == null ? o : JSON.parse(JSON.stringify(o)));
export const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
export const rid = (n = 6) => Array.from({ length: n }, () => "abcdefghijklmnopqrstuvwxyz0123456789"[Math.floor(Math.random() * 36)]).join("");
export const placed = p => ["2d", "3d"].includes(p?.placement?.mode) && !!p.placement.box;
export const pieceOk = p => !!(p && (p.type || (p.text || "").trim()));
export const liveVariants = doc => {
  const del = new Set(doc?.deleted_variants || []);
  return (doc?.variants || []).filter(v => !del.has(v.id));
};
export const activeJobs = jobs => (jobs || []).filter(j => j.state === "queued" || j.state === "running");
// "ohne Raumfoto" is a per-device choice; a colleague's free project (Umgebung or variants without a room) counts too
export const noRoom = (doc, view) => !doc?.room && !!(view?.noRoom || (doc?.setting || "").trim() || liveVariants(doc).length);
export const emptyPiece = (src, id = rid(6)) => ({ id, type: null, material: src?.material || "eiche",
  handles: src ? (src.handles ?? null) : "push", placement: NONE() });
export const activePiece = (doc, view) => (doc?.pieces || []).find(p => p.id === view?.active_piece) || (doc?.pieces || [])[0] || null;
export const pieceName = (p, i, catalog) => (catalog?.pieces?.[p?.type]?.de) || (p?.text || "").trim().slice(0, 30) || `Möbel ${i + 1}`;
export const set = (path, value) => ({ op: "set", path, value });
/** Wall length in whole cm, rounded down once so every place shows the same number and „auf Wandbreite“ fits. */
export const wallCm = r => (r?.wallLength > 0 ? Math.floor(r.wallLength) : null);
/** {wall} when a placed 3d piece is wider than its wall, else null. */
export function tooWide(piece, scene) {
  const pl = piece?.placement;
  if (!scene || pl?.mode !== "3d" || !pl.pose) return null;
  let r = null;
  try { r = scene.relations(piece); } catch (e) { console.error(e); }
  return r?.tooWide && wallCm(r) ? { wall: wallCm(r) } : null;
}
/** The two fixes offered next to „breiter als die Wand“: set the width to the wall, or measure the wall again. */
export function wideFixButtons(piece, scene, sctx, { onDone } = {}) {
  const w = tooWide(piece, scene);
  if (!w) return null;
  return el("div.wide-fix", {},
    el("button.btn.secondary", { type: "button", on: { click: () => {
      sctx.store.patch([set(`pieces.${piece.id}.w`, w.wall)], { now: true }).then(() => toast(`Breite auf ${w.wall} cm gesetzt.`, { kind: "ok" }),
        e => toast(errText(e), { kind: "err" }));
      onDone?.();
    } } }, `Auf Wandbreite setzen (${w.wall} cm)`),
    el("button.btn.ghost", { type: "button", on: { click: () => { onDone?.(); measureWall(sctx.store, piece); } } },
      "Wand genauer nachmessen"));
}
/** The tape measure over the current step (no step change), prompted for the wall the piece stands at. */
export function measureWall(store, piece) {
  const i = (store.doc?.pieces || []).findIndex(p => p.id === piece.id), name = pieceName(piece, Math.max(0, i), store.catalog);
  return import("./step-raum.js").then(m => m.openTape(store, { label: "Wand", focus: piece.placement?.box || null,
    prompt: `Linie unten am Boden über die Wand ziehen, an der „${name}“ steht – von Ecke zu Ecke.` }));
}
/** Pieces wider than their wall, as „Einbauschrank (180 cm, Wand 137 cm)“ lines (step ④ asks before computing). */
export function wideList(doc, catalog) {
  const scene = sceneFor(doc, catalog);
  return (doc?.pieces || []).map((p, i) => { const w = tooWide(p, scene); return w ? `${pieceName(p, i, catalog)} (${p.w} cm, Wand ${w.wall} cm)` : null; }).filter(Boolean);
}

const DESIGN_STEPS = new Set(["raum", "moebel", "platzieren"]);
/** "locked" (approved: the design steps are read-only until confirmed), "outdated" (changed after the approval), or null. */
export function approvalState(doc) {
  const a = doc?.approved;
  if (!a || !["freigegeben", "auftrag", "fertig"].includes(doc.status)) return null;
  return a.outdated ? "outdated" : "locked";
}
const UNLOCKED = new Set();  // projects whose approved design this device chose to change (this visit)

/** First step that is not done (spec §12); a remembered step younger than 24 h wins if it is available. */
export function defaultStep(doc, view, { jobs = [], now = Date.now(), available = () => true } = {}) {
  if (view?.step && STEPS.some(s => s.id === view.step) && now - (view.stepAt || 0) < DAY && available(view.step)) return view.step;
  const pieces = doc?.pieces || [];
  if (!doc?.room && !noRoom(doc, view)) return "raum";
  if (pieces.some(p => !pieceOk(p))) return "moebel";
  if (doc?.room && pieces.some(p => !placed(p))) return "platzieren";
  if (!liveVariants(doc).length && !activeJobs(jobs).length) return "berechnen";
  return "ergebnis";
}

/** Built-in state for steps whose module is missing (④/⑤ belong to ui_results and carry their own state()). */
export function fallbackState(id, doc, jobs) {
  if (id === "ergebnis") {
    const ok = liveVariants(doc).length > 0 || activeJobs(jobs).length > 0;
    return { done: false, available: ok, reason: ok ? null : "Noch keine Vorschläge – erst in Schritt 4 berechnen." };
  }
  return { done: id === "berechnen" && liveVariants(doc).length > 0, available: true, reason: null };
}

/** W3D scene for the doc's geometry (cached by its JSON), null without a usable measurement. */
const SCENES = { key: null, scene: null };
export function sceneFor(doc, catalog) {
  const W3D = globalThis.W3D, g = doc?.room && doc.geometry;
  if (!W3D || !g || !g.ok) return null;
  const key = JSON.stringify(g) + "|" + Object.keys(catalog?.pieces || {}).length;
  if (SCENES.key !== key) {
    SCENES.key = key;
    try { SCENES.scene = W3D.init(g, catalog); } catch (e) { console.error(e); SCENES.scene = null; }
  }
  return SCENES.scene;
}

/** Placement of a 3d piece recomputed from its pose: wall pieces re-snap (depth changed), box from the projection. */
export function reprojected(scene, piece, pl = piece.placement) {
  if (!scene || pl?.mode !== "3d" || !pl.pose) return pl;
  let pose = pl.pose;
  if (pose.wall) try { pose = scene.snapToWall(pose, piece, pose.wall) || pose; } catch (e) { console.error(e); }
  let box = null;
  try { const pr = scene.projectBox(piece, pose); box = pr.visible ? pr.box : null; } catch (e) { console.error(e); }
  return { mode: "3d", pose, box };
}

/** Ops that bring every 3d box in line with its pose and dims (after dims or geometry changes). */
export function syncBoxOps(doc, scene) {
  if (!scene) return [];
  const ops = [];
  for (const p of doc?.pieces || []) {
    const pl = p.placement;
    if (pl?.mode !== "3d" || !pl.pose) continue;
    const next = reprojected(scene, p);
    const round = q => ({ ...q, pose: { ...q.pose, x: +q.pose.x.toFixed(2), z: +q.pose.z.toFixed(2) } });
    if (!same(round(next), round(pl))) ops.push(set(`pieces.${p.id}.placement`, next));
  }
  return ops;
}

/* ---------------- tiny DOM helpers (shared by the step modules) ---------------- */
/** el("button.btn.primary", {on: {click}}, "text", child…): text children become text nodes, falsy ones are skipped. */
export function el(sel, attrs = {}, ...kids) {
  const [tag, ...cls] = sel.split(".");
  const n = document.createElement(tag || "div");
  if (cls.length) n.className = cls.join(" ");
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (k === "class") n.className += (n.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") for (const [sk, sv] of Object.entries(v)) sk.startsWith("--") ? n.style.setProperty(sk, sv) : (n.style[sk] = sv);
    else if (k === "html") n.innerHTML = v;  // trusted markup only (icons)
    else if (k in n && !k.startsWith("aria-") && !k.startsWith("data-") && k !== "list") n[k] = v === true ? true : v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false && c !== "") n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
export const iconEl = (name, cls = "ed-ico") => el("span." + cls, { "aria-hidden": "true", html: icon(name) });
export function css(id, text) {
  if (typeof document === "undefined" || document.getElementById(id)) return;
  document.head.append(el("style", { id }, text));
}
export const isPhone = () => Math.min(innerWidth, innerHeight) < 700;
export const footerH = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--footer-h")) || 0;
export function fit(boxW, boxH, aspect) {
  let w = boxW, h = boxW / aspect;
  if (h > boxH) { h = boxH; w = boxH * aspect; }
  return [Math.max(40, Math.floor(w)), Math.max(30, Math.floor(h))];
}
export function errText(e) {
  const d = e?.data || e?.detail;
  return (d && typeof d === "object" ? d.message : null) || (typeof d === "string" ? d : null) || e?.message || "Unerwarteter Fehler – bitte die Seite neu laden.";
}

css("ws-editor-css", `
.ed{display:flex;flex-direction:column;min-width:0}
.ed-stepper .step{justify-content:flex-start}
.ed-stepper .step .t{overflow:hidden;text-overflow:ellipsis}
.ed-stepper-phone{display:none!important}
@media (max-width:899px){.ed-stepper .step{display:none}.ed-stepper-phone{display:flex!important;width:100%;justify-content:space-between}}
.ed-steplist{display:flex;flex-direction:column;gap:8px}
.ed-steplist .btn{justify-content:flex-start;text-align:left;gap:12px}
.ed-steplist .num{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:var(--panel2);font-weight:700;flex:none}
.ed-steplist small{display:block;color:var(--muted);font-size:14px;font-weight:450}
.ed-ico{display:inline-flex;width:24px;height:24px;flex:none}.ed-ico svg{width:100%;height:100%}
.ed-missing{padding:24px 0}
.ed-loading{padding:32px 0;color:var(--muted)}
.wide-fix{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}.wide-fix .btn{flex:1 1 200px}
.ed-lock{display:flex;flex-wrap:wrap;align-items:center;gap:10px;border-left:4px solid var(--ok);margin:0 0 12px}
.ed-lock.warn{border-left-color:var(--warn)}.ed-lock p{margin:0;flex:1 1 260px}
.ed-inert{opacity:.6;filter:saturate(.7)}
`);

/* ---------------- the editor view ---------------- */
export default {
  mount(root, ctx) {
    const { store } = ctx;
    let pid = ctx.route.pid, current = null, inst = null, mods = {}, alive = true, immersive = false;
    let actKey = null, runPrimary = null, busy = false;
    const offs = [];
    const view = () => store.view(pid);
    const saveView = () => { try { store.saveView(); } catch (e) { console.error(e); } };
    const jobs = () => { try { return store.projectJobs(pid) || []; } catch { return []; } };
    const stepper = el("nav.stepper.ed-stepper", { "aria-label": "Schritte" });
    const stepEl = el("div.ed-step");
    const lockBar = el("div.card.ed-lock", { hidden: true, role: "status" });
    root.replaceChildren(el("div.ed", {}, stepper, lockBar, stepEl));
    stepEl.append(el("p.ed-loading", {}, "Wird geladen …"));

    const stateOf = id => {
      const doc = store.doc, m = mods[id];
      if (!doc) return { done: false, available: false, reason: null };
      if (!m || !m.state) return fallbackState(id, doc, jobs());
      try { return m.state(doc, view(), store) || fallbackState(id, doc, jobs()); } catch (e) { console.error(e); return fallbackState(id, doc, jobs()); }
    };
    const idx = id => STEPS.findIndex(s => s.id === id);
    const go = id => {
      const st = stateOf(id);
      if (!st.available) { if (st.reason) toast(st.reason); return; }
      if (id === current) return;
      ctx.nav(`#/p/${pid}/${id}`);
    };
    const step = dir => {
      for (let i = idx(current) + dir; i >= 0 && i < STEPS.length; i += dir) if (stateOf(STEPS[i].id).available) return go(STEPS[i].id);
    };

    function setImmersive(on, opts = {}) {   // opts: {title} — the 44 px row shows „‹ {title} · Schritt n/5 ⋯“
      const was = immersive;
      immersive = !!on;
      try { ctx.immersive?.(immersive, immersive ? { step: idx(current) + 1, total: STEPS.length, back: `#/p/${pid}/${STEPS[Math.max(0, idx(current) - 1)].id}`, ...opts } : {}); } catch (e) { console.error(e); }
      stepper.hidden = immersive;
      if (was !== immersive) { actKey = null; renderActions(); }
    }

    const sctx = { ...ctx, pid, get view() { return view(); }, saveView, next: () => step(1), back: () => step(-1), go,
      refresh: () => { renderActions(); renderStepper(); }, immersive: setImmersive, go };

    /** After the customer approved: banner + read-only design steps until „Trotzdem ändern“ is confirmed (spec: a changed
        design needs a new version). Once changed, the server flags the approval as outdated and the banner says so. */
    function applyLock() {
      const st = approvalState(store.doc), design = DESIGN_STEPS.has(current);
      const locked = st === "locked" && design && !UNLOCKED.has(pid);
      stepEl.inert = locked;
      stepEl.classList.toggle("ed-inert", locked);
      lockBar.hidden = !(design && st);
      if (lockBar.hidden) return;
      lockBar.classList.toggle("warn", st === "outdated" || UNLOCKED.has(pid));
      const unlock = async () => {
        if (!await confirmDialog({ title: "Änderung nach Freigabe – neue Version nötig",
          text: "Der Kunde hat diesen Stand freigegeben. Wenn Sie jetzt Raum, Möbel oder Maße ändern, passt die Freigabe nicht mehr – danach bitte eine neue Version an den Kunden senden.",
          ok: "Ja, ändern" })) return;
        UNLOCKED.add(pid); applyLock();
      };
      lockBar.replaceChildren(...(st === "outdated"
        ? [el("p", {}, "Freigabe veraltet: Seit der Freigabe wurden Möbel oder Maße geändert. Bitte eine neue Version an den Kunden senden."),
           el("button.btn.secondary", { type: "button", on: { click: () => go("ergebnis") } }, "Zum Versand")]
        : UNLOCKED.has(pid)
          ? [el("p", {}, "Änderung nach Freigabe – bitte danach eine neue Version an den Kunden senden.")]
          : [el("p", {}, "Der Kunde hat diesen Entwurf freigegeben. Dieser Schritt ist deshalb gesperrt."),
             el("button.btn.secondary", { type: "button", on: { click: unlock } }, "Trotzdem ändern …")]));
    }

    function renderStepper() {
      if (!store.doc) return;
      const items = STEPS.map((s, i) => ({ ...s, i, st: stateOf(s.id) }));
      const cur = items.find(s => s.id === current) || items[0];
      const btn = s => el("button.step" + (s.id === current ? ".active" : s.st.done ? ".done" : ""), { type: "button",
        "aria-current": s.id === current ? "step" : null, "aria-disabled": s.st.available ? null : "true", title: s.st.available ? null : s.st.reason,
        on: { click: () => go(s.id) } }, el("span.num", {}, s.st.done && s.id !== current ? "✓" : String(s.i + 1)), el("span.t", {}, s.name));
      stepper.replaceChildren(...items.map(btn),
        el("button.btn.secondary.ed-stepper-phone", { type: "button", "aria-haspopup": "dialog", on: { click: openList } },
          el("span", {}, `Schritt ${cur.i + 1} von ${STEPS.length} · ${cur.name}`), el("span", { "aria-hidden": "true" }, "▾")));
      function openList() {
        let sh = null;
        const list = el("div.ed-steplist", {}, items.map(s => el("button.btn.block" + (s.id === current ? ".secondary" : ".ghost"), {
          type: "button", "aria-disabled": s.st.available ? null : "true",
          on: { click: () => { if (!s.st.available) return toast(s.st.reason || MISSING); sh?.close(); go(s.id); } } },
          el("span.num", {}, s.st.done ? "✓" : String(s.i + 1)),
          el("span", {}, s.name, !s.st.available && s.st.reason ? el("small", {}, s.st.reason) : null))));
        sh = sheet({ title: "Schritte", body: list, actions: [] });
      }
    }

    function renderActions() {
      if (!alive || !store.doc) return;
      let p = null;
      try { p = inst?.primary?.() || null; } catch (e) { console.error(e); }
      const back = !immersive && idx(current) > 0;
      const line = p ? (p.disabled ? p.reason : p.warning) || "" : "";
      const key = JSON.stringify([p?.label, !!p?.disabled, line, back, busy]);
      runPrimary = p?.run || null;
      if (key === actKey) return;
      actKey = key;
      // #ws-actions lays these out (reason line on top, [Zurück] [primary]); immersive mode shows only the primary
      ctx.actions([
        line ? el("p.reason" + (p.disabled ? "" : ".warn"), { role: "status" }, line) : null,
        back ? el("button.btn.secondary", { type: "button", on: { click: () => step(-1) } }, "Zurück") : null,
        p ? el("button.btn.primary", { type: "button", disabled: !!p.disabled || busy, "data-primary": "1", on: { click: onPrimary } }, p.label) : null,
      ].filter(Boolean));
    }
    async function onPrimary() {
      if (busy || !runPrimary) return;
      busy = true; renderActions();
      try { await runPrimary(); } catch (e) { console.error(e); toast(errText(e), { kind: "err" }); }
      finally { busy = false; actKey = null; if (alive) renderActions(); }
    }

    function show(id) {
      if (!alive) return;
      if (inst) { try { inst.unmount?.(); } catch (e) { console.error(e); } inst = null; }
      if (immersive) setImmersive(false);
      current = id;
      const v = view();
      v.step = id; v.stepAt = Date.now(); saveView();
      try { store.setView?.(id); } catch { /* presence only */ }
      const want = `#/p/${pid}/${id}`;
      if (location.hash !== want) try { history.replaceState(null, "", want); } catch { /* sandboxed */ }
      stepEl.replaceChildren();
      const m = mods[id];
      try { inst = m ? m.mount(stepEl, sctx) : null; } catch (e) { console.error(e); inst = null; }
      if (!inst) stepEl.replaceChildren(el("div.empty.ed-missing", {}, el("p", {}, MISSING)));
      actKey = null;
      applyLock(); renderStepper(); renderActions();
      try { scrollTo(0, 0); } catch { /* jsdom */ }
    }

    function pick(routeStep) {
      const doc = store.doc;
      if (routeStep && STEPS.some(s => s.id === routeStep)) {
        const st = stateOf(routeStep);
        if (st.available) return routeStep;
        if (st.reason) toast(st.reason);
      }
      return defaultStep(doc, view(), { jobs: jobs(), available: id => stateOf(id).available });
    }

    function title() { const d = store.doc; if (d && !immersive) try { ctx.title?.(d.title || "Ohne Titel", d.customer || ""); } catch { /* frame */ } }

    function syncBoxes() {
      const ops = syncBoxOps(store.doc, sceneFor(store.doc, store.catalog));
      if (ops.length) store.patch(ops, { now: true }).catch(e => console.error(e));
    }

    (async () => {
      const loaded = await Promise.allSettled(STEPS.map(s => import(FILES[s.id])));
      loaded.forEach((r, i) => { if (r.status === "fulfilled" && r.value?.default?.mount) mods[STEPS[i].id] = r.value.default; else if (r.status === "rejected") console.warn(STEPS[i].id, r.reason); });
      for (;;) {   // offline (cellar): retry by button, when the connection returns, or every 15 s
        try { await store.open(pid); break; } catch (e) {
          if (!alive) return;
          const gone = e?.status === 404, home = el("button.btn.secondary", { type: "button", on: { click: () => ctx.nav("#/") } }, "Zu den Aufträgen");
          ctx.actions(null);
          if (gone) { stepEl.replaceChildren(el("div.empty", {}, el("p", {}, "Auftrag nicht gefunden."), home)); return; }
          await new Promise(res => {
            const done = () => { clearTimeout(t); offOnline(); removeEventListener("online", done); res(); };
            const offOnline = store.on("online", done), t = setTimeout(done, 15000);
            addEventListener("online", done);
            offs.push(done);
            stepEl.replaceChildren(el("div.empty", {}, el("p", {}, errText(e)),
              el("p.muted", {}, "Sobald die Verbindung wieder da ist, wird der Auftrag automatisch geöffnet."),
              el("button.btn.primary", { type: "button", on: { click: done } }, "Nochmal versuchen"), home));
          });
          if (!alive) return;
        }
      }
      if (!alive) return;
      offs.push(store.on("doc", evt => {
        if (!store.doc) return;
        try { inst?.onDoc?.(evt || {}); } catch (e) { console.error(e); }
        if (!evt || evt.source !== "remote") syncBoxes();
        title(); applyLock(); renderStepper(); renderActions();
      }));
      for (const ev of ["queue", "status"]) offs.push(store.on(ev, () => {
        try { (ev === "queue" ? inst?.onQueue : inst?.onStatus)?.(); } catch (e) { console.error(e); }
        renderStepper(); renderActions();
      }));
      const raum = mods.raum;
      if (raum?.onAnalysis) offs.push(raum.onAnalysis(() => { renderStepper(); renderActions(); }));
      title();
      show(pick(ctx.route.step));
      try { raum?.ensureAnalysis?.(store); } catch (e) { console.error(e); }
    })();

    return {
      onRoute(route) {
        if (!alive || route.pid !== pid || !store.doc) return;
        const id = pick(route.step);
        if (id !== current) show(id);
      },
      unmount() {
        alive = false;
        for (const off of offs) try { off?.(); } catch { /* already gone */ }
        if (inst) try { inst.unmount?.(); } catch (e) { console.error(e); }
        inst = null;
        if (immersive) try { ctx.immersive?.(false); } catch { /* frame gone */ }
        try { ctx.actions(null); } catch { /* frame gone */ }
        store.flush?.().catch?.(() => {});
      },
    };
  },
};
