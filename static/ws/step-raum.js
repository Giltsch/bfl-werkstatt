/* ① Raum: room photo (camera first), room analysis (DepthPro, geometry_api), ceiling-height check and the tape-measure
   calibration as two separate actions, optional wood sample, or "ohne Raumfoto" with the Umgebung text (spec §12, §16.5). */
import { api, toast, confirmDialog, choose, sheet, pickImage, upload, img, fmt, COARSE } from "./ui.js";
import { el, iconEl, css, set, clone, same, placed, noRoom, reprojected, fit, errText, NONE } from "./editor.js";

export const T = {
  noPhoto: "Bitte zuerst ein Raumfoto aufnehmen – oder ohne Raumfoto weiter.", measuring: "Raum wird vermessen …",
  uploading: "Foto wird hochgeladen …", busy: "Der Rechner ist gerade beschäftigt – bitte gleich nochmal versuchen.",
  failed: "Der Raum konnte nicht vermessen werden. Sie können die Möbel trotzdem von Hand einrahmen.",
};
const CONF = { high: "Messung zuverlässig", medium: "Messung ungefähr", low: "Messung unsicher – bitte nachmessen" };
export const TAPE_PROMPT = "Ziehen Sie eine Linie über etwas, dessen Länge Sie kennen (Türbreite, Fliese, Tisch).";
const ACC = { high: "hoch", medium: "mittel", low: "niedrig" };
const transient = msg => /beschäftigt|Speicher|busy/i.test(msg || "");

/* ---------------- analysis state (per project, survives step changes) ---------------- */
const AN = new Map();            // pid → {room, phase: "upload"|"analyzing"|"failed", error, busy, pct}
const listeners = new Set();
const notify = () => { for (const fn of listeners) try { fn(); } catch (e) { console.error(e); } };
export function onAnalysis(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export const analysisOf = doc => { const a = AN.get(doc?.id); return a && (a.phase === "upload" || a.room === doc.room) ? a : null; };
export function _setAnalysis(pid, a) { if (a) AN.set(pid, a); else AN.delete(pid); notify(); }  // tests and internal use

/** Gate of „Weiter: Möbel“ (§10.6): photo uploaded and analysis finished (success or failure), or no-room chosen. */
export function raumGate(doc, view, a = analysisOf(doc)) {
  if (a?.phase === "upload") return { disabled: true, reason: T.uploading };
  if (!doc?.room) return noRoom(doc, view) ? { disabled: false, reason: null } : { disabled: true, reason: T.noPhoto };
  if (a?.phase === "failed" || (doc.geometry && a?.phase !== "analyzing")) return { disabled: false, reason: null };
  return { disabled: true, reason: T.measuring };
}

/** One PATCH for a new room photo: the old analysis, calibration, removes and every placement are void. */
export function newRoomOps(doc, up) {
  return [set("room", up.path), set("room_size", [up.width, up.height]), set("geometry", null), set("calibration", null),
    set("removes", []), ...(doc.pieces || []).map(p => set(`pieces.${p.id}.placement`, NONE()))];
}

/** Ops after an analysis: geometry, plus 3d boxes from the new scene (ok) or 3d pieces turned into frames (failed). */
export function analysisOps(doc, g, catalog, W3D = globalThis.W3D) {
  const ops = [set("geometry", g)];
  let scene = null;
  if (g.ok && W3D) try { scene = W3D.init(g, catalog); } catch (e) { console.error(e); }
  for (const p of doc.pieces || []) {
    const pl = p.placement;
    if (pl?.mode !== "3d") continue;
    const next = scene ? reprojected(scene, p) : pl.box ? { ...pl, mode: "2d" } : NONE();
    if (!same(next, pl)) ops.push(set(`pieces.${p.id}.placement`, next));
  }
  return ops;
}

/** Adopts a new calibration like v2's adoptGeo: poses scale with the factor, wall pieces re-snap, boxes reproject.
    Returns the ops for one PATCH (geometry, calibration and every changed placement). */
export function calibrationOps(doc, geo, oldScale, cal, catalog, W3D = globalThis.W3D) {
  const k = (geo.scale || 1) / (oldScale || 1), pieces = clone(doc.pieces || []);
  if (W3D && Math.abs(k - 1) > 1e-9) W3D.scalePoses(pieces, k);
  let scene = null;
  if (W3D && geo.ok) try { scene = W3D.init(geo, catalog); } catch (e) { console.error(e); }
  const ops = [set("geometry", geo), set("calibration", cal)];
  pieces.forEach((p, i) => {
    const next = scene ? reprojected(scene, p) : p.placement;
    if (!same(next, doc.pieces[i].placement)) ops.push(set(`pieces.${p.id}.placement`, next));
  });
  return ops;
}

export const steepMode = (p1, p2) => (Math.abs(p2[1] - p1[1]) > 1.5 * Math.abs(p2[0] - p1[0]) ? "vertical" : "floor");

/** The confirmed references as one line: „Maße nachgemessen (Decke 2,76 m · Wand 137 cm)“; null when nothing was measured. */
export function measuredText(g) {
  const refs = g?.refs || [];
  if (!refs.length) return Math.abs((g?.scale || 1) - 1) > 0.001 ? "Maße nachgemessen" : null;
  return `Maße nachgemessen (${refs.map(r => (r.kind === "height" ? `Decke ${fmt.m(r.cm)}` : `${r.label || "Strecke"} ${fmt.num(r.cm)} cm`)).join(" · ")})`;
}

/** The question when a new measurement contradicts a confirmed one by more than 10 % (server: geometry.add_reference). */
export function conflictQuestion(c) {
  const old = c.kind === "height" ? `Decke ${fmt.m(c.cm)}` : `${c.label || "Strecke"} ${fmt.num(c.cm)} cm`;
  if (c.kind === "height" && c.ceiling_cm) return { title: "Passt nicht zur Deckenhöhe",
    text: `Dann wäre die Decke ${fmt.m(c.ceiling_cm)} hoch – stimmt das?`, yes: `Ja, Decke ${fmt.m(c.ceiling_cm)}`, no: `Nein, ${old} stimmt` };
  const what = c.kind === "height" ? "die Decke" : c.label ? `die ${c.label}` : "die vorher gemessene Strecke";
  const val = c.kind === "height" ? fmt.m(c.implied_cm) : `${fmt.num(c.implied_cm)} cm`;
  return { title: "Passt nicht zur ersten Messung", text: `Dann wäre ${what} ${val} ${c.kind === "height" ? "hoch" : "lang"} statt ${c.kind === "height" ? fmt.m(c.cm) : `${fmt.num(c.cm)} cm`} – stimmt das?`,
    yes: "Ja, neue Messung stimmt", no: `Nein, ${old} stimmt` };
}

/** A warning when a measurement makes the room implausible (typo, wrong line, cm vs m); null when it looks normal. */
export function plausibility(g) {
  const ch = g?.ceiling?.height, s = g?.scale || 1;
  if (ch && (ch < 200 || ch > 420)) return `Bitte prüfen: Damit wäre die Decke ${fmt.m(ch)} hoch. Stimmt die eingegebene Länge?`;
  if (s < 0.6 || s > 1.6) return `Bitte prüfen: Die Messung ändert alle Größen um ${Math.round(Math.abs(s - 1) * 100)} %. Stimmt die eingegebene Länge?`;
  return null;
}

/** Room measured with a real length (ceiling confirmed or tape)? Without it every size in cm is only DepthPro's estimate. */
export const isMeasured = d => !d?.geometry?.ok || !!measuredText(d.geometry);

/** POSTs one calibration; on a contradiction asks which value is right. → "saved" | "kept" (new value dropped) | null. */
async function calibrate(store, body, cal) {
  const d = store.doc;
  let r = await api("POST", "/api/geometry/calibrate", { room: d.room, ...body });
  if (r.conflict) {
    const q = conflictQuestion(r.conflict);
    const v = await choose({ title: q.title, text: q.text, dismiss: false, buttons: [
      { label: q.no, value: "keep", kind: "secondary" }, { label: q.yes, value: "replace", kind: "primary" }] });
    if (v !== "replace") return "kept";
    r = await api("POST", "/api/geometry/calibrate", { room: d.room, ...body, on_conflict: "replace" });
  }
  const now = store.doc; if (now.room !== d.room) return null;
  await store.patch(calibrationOps(now, r.geometry, now.geometry?.scale || 1, cal(now.calibration || {}, r), store.catalog), { now: true });
  const odd = plausibility(r.geometry);
  if (odd) setTimeout(() => toast(odd, { kind: "err", ms: 9000 }), 1600);   // after the „bestätigt“ toast
  return r;
}

export async function saveHeight(store, h) {
  const g = store.doc.geometry, cur = g?.ceiling?.height;
  if (!(h >= 100 && h <= 500)) return toast("Raumhöhe muss zwischen 100 und 500 cm liegen.", { kind: "err" });
  if (cur && Math.abs(h - cur) < 1 && (g.refs || []).some(r => r.kind === "height" && Math.abs(r.cm - h) < 1)) return toast("Deckenhöhe ist bestätigt.", { kind: "ok" });
  try {
    const r = await calibrate(store, { room_height_cm: h }, (c, r) => ({ p1: c.p1 ?? null, p2: c.p2 ?? null, length_cm: c.length_cm ?? null,
      mode: c.mode || "room_height", measured_cm: c.measured_cm ?? null, scale: r.geometry.scale, room_height_cm: h }));
    if (r === "kept") return toast("Die bisherigen Maße bleiben.");
    if (!r) return;
    const ch = r.geometry.ceiling?.height;
    toast(r.scaled === false ? "Raumhöhe gespeichert. Für genaue Maße eine bekannte Länge nachmessen."
      : Math.abs((ch || h) - h) < 1 ? `Deckenhöhe ${fmt.m(h)} bestätigt.` : `Maße an alle Messungen angepasst – Decke jetzt ${fmt.m(ch)}.`, { kind: "ok" });
  } catch (e) { toast(e?.status === 503 ? T.busy : errText(e), { kind: "err" }); }
}

async function saveTape(store, p1, p2, len, mode, label) {
  const r = await calibrate(store, { p1, p2, length_cm: len, mode, ...(label ? { label } : {}) }, (c, r) =>
    ({ p1, p2, length_cm: len, mode, measured_cm: r.measured_cm ?? null, scale: r.geometry.scale, room_height_cm: c.room_height_cm ?? null }));
  if (r && r !== "kept") {
    const ch = r.geometry.ceiling?.height;
    toast(ch ? `Maße angepasst – Deckenhöhe jetzt ${fmt.m(ch)}.` : "Maße angepasst.", { kind: "ok" });
  }
  return r;
}

/** The tape measure as a full-screen overlay over the current step: the photo fills the height (two fingers or the mouse
    wheel zoom and pan), one finger draws the line, the length form takes at most ~40 % at the bottom so the line stays
    visible above it. Resolves true once the measurement is saved, false when closed. */
export function openTape(store, { prompt = TAPE_PROMPT, label = null, focus = null } = {}) {
  return new Promise(resolve => {
    const W3D = globalThis.W3D, d = store.doc, geo = d?.geometry;
    const scene = W3D && geo?.ok ? W3D.init(geo, store.catalog) : null;
    if (!scene) { toast("Raum noch nicht vermessen.", { kind: "err" }); return resolve(false); }
    const size = geo.size || d.room_size, aspect = size[0] / size[1];
    const canvas = el("canvas"), cv = el("div.tp-cv", {}, el("img", { src: img(d.room), alt: "", draggable: "false" }), canvas);
    const stage = el("div.tp-stage", {}, cv), form = el("div.tp-form");
    let z = { s: 1, tx: 0, ty: 0 }, base = [0, 0], line = null, saved = false, fitted = false;
    const pointers = new Map();
    let pinch = null;
    const close = () => { try { ctl?.destroy(); } catch { /* gone */ } ro?.disconnect(); removeEventListener("keydown", esc); ov.remove(); resolve(saved); };
    const esc = e => { if (e.key === "Escape" && !document.querySelector(".backdrop")) close(); };
    const ov = el("div.ed-tape", { role: "dialog", "aria-modal": "true", "aria-label": "Genauer nachmessen" },
      el("div.tp-top", {}, el("p", {}, prompt), el("button.btn.ghost.tp-x", { type: "button", on: { click: close } }, "Abbrechen")),
      stage, form);
    document.body.append(ov);
    addEventListener("keydown", esc);

    function apply(s, tx, ty) {
      const W = stage.clientWidth, H = stage.clientHeight, [w, h] = base;
      s = Math.max(1, Math.min(4, s));
      const ox = (W - w) / 2, oy = (H - h) / 2;
      tx = w * s <= W ? (W - w * s) / 2 - ox : Math.min(-ox, Math.max(W - ox - w * s, tx));
      ty = h * s <= H ? (H - h * s) / 2 - oy : Math.min(-oy, Math.max(H - oy - h * s, ty));
      z = { s, tx, ty };
      cv.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
      ctl?.redraw();
    }
    // centre a photo point (fractions) at the middle of the stage
    const centre = (fx, fy, s = z.s) => { const W = stage.clientWidth, H = stage.clientHeight, [w, h] = base;
      apply(s, W / 2 - (W - w) / 2 - fx * w * s, H / 2 - (H - h) / 2 - fy * h * s); };
    function layout() {
      const [w, h] = fit(stage.clientWidth, stage.clientHeight, aspect);
      base = [w, h]; cv.style.width = `${w}px`; cv.style.height = `${h}px`;
      cv.style.left = `${(stage.clientWidth - w) / 2}px`; cv.style.top = `${(stage.clientHeight - h) / 2}px`;
      ctl?.resize();
      if (line) centre((line[0][0] + line[1][0]) / 2 / size[0], (line[0][1] + line[1][1]) / 2 / size[1]);
      else if (!fitted) {   // start with the photo filling the stage height, around the piece the measurement is for
        fitted = true;
        const fx = focus ? (focus[1] + focus[3]) / 2000 : 0.5, fy = focus ? (focus[0] + focus[2]) / 2000 : 0.5;
        centre(fx, fy, Math.min(3, Math.max(stage.clientHeight / h, stage.clientWidth / w)));
      } else apply(z.s, z.tx, z.ty);
    }
    const ctl = W3D.attach(canvas, { scene, imgSize: scene.size, mode: "measure", touch: COARSE, getZoom: () => z.s,
      onMeasure: (a, b) => { line = [a, b]; askLength(a, b); } });
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => requestAnimationFrame(layout)) : null;
    ro?.observe(stage); layout();

    // two fingers: zoom + pan (the measure gesture of the first finger is dropped); wheel zoom on desktop
    const pts = () => [...pointers.values()], mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    stage.addEventListener("pointerdown", e => {
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      if (pointers.size === 2) {
        e.stopPropagation(); e.preventDefault();
        const [a, b] = pts();
        pinch = { d0: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, m0: mid(a, b), z0: { ...z } };
        ctl.setMode("off");
      }
    }, true);
    stage.addEventListener("pointermove", e => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      if (!pinch || pointers.size < 2) return;
      e.stopPropagation();
      const [a, b] = pts(), m = mid(a, b), s = Math.max(1, Math.min(4, pinch.z0.s * Math.hypot(a[0] - b[0], a[1] - b[1]) / pinch.d0));
      const r = stage.getBoundingClientRect(), k = s / pinch.z0.s;
      const px = pinch.m0[0] - r.left, py = pinch.m0[1] - r.top, ox = (stage.clientWidth - base[0]) / 2, oy = (stage.clientHeight - base[1]) / 2;
      apply(s, m[0] - r.left - ox - (px - ox - pinch.z0.tx) * k, m[1] - r.top - oy - (py - oy - pinch.z0.ty) * k);
    }, true);
    const up = e => {
      pointers.delete(e.pointerId);
      if (pinch && pointers.size < 2) { pinch = null; pointers.clear(); e.stopPropagation(); ctl.setMode("measure"); ctl.setLine(line); }
    };
    stage.addEventListener("pointerup", up, true);
    stage.addEventListener("pointercancel", up, true);
    stage.addEventListener("wheel", e => {
      e.preventDefault();
      const r = stage.getBoundingClientRect(), s = Math.max(1, Math.min(4, z.s * (e.deltaY < 0 ? 1.15 : 1 / 1.15))), k = s / z.s;
      const ox = (stage.clientWidth - base[0]) / 2, oy = (stage.clientHeight - base[1]) / 2, px = e.clientX - r.left - ox, py = e.clientY - r.top - oy;
      apply(s, px - (px - z.tx) * k, py - (py - z.ty) * k);
    }, { passive: false });

    function idle() {
      line = null;
      form.replaceChildren(el("p.tp-hint", {}, COARSE ? "Mit einem Finger ziehen · zwei Finger: zoomen und verschieben" : "Mit der Maus ziehen · Mausrad: zoomen"));
    }
    function askLength(p1, p2) {
      let mode = steepMode(p1, p2);
      const meas = el("p.tp-meas");
      const input = el("input", { type: "number", inputmode: "numeric", min: "1", max: "5000", "aria-label": "Echte Länge in cm", placeholder: "z. B. 250" });
      const seg = el("div.seg", { role: "group", "aria-label": "Lage der Linie" });
      const upd = () => {
        let m = null; try { m = scene.measure(p1, p2, mode); } catch { /* off floor */ }
        meas.textContent = m ? `Im Foto gemessen: ca. ${Math.round(m)} cm` : "Messung nicht möglich: Punkte liegen nicht auf dem Boden.";
        seg.replaceChildren(...[["floor", "Liegt am Boden"], ["vertical", "Ist senkrecht"]].map(([k, t]) =>
          el("button.btn" + (k === mode ? ".active" : ""), { type: "button", "aria-pressed": String(k === mode), on: { click: () => { mode = k; upd(); } } }, t)));
      };
      upd();
      const ok = el("button.btn.primary", { type: "button", on: { click: async () => {
        const len = parseFloat(input.value);
        if (!(len >= 1 && len <= 5000)) { toast("Bitte die echte Länge in cm eingeben.", { kind: "err" }); input.focus(); return; }
        ok.disabled = true;
        try {
          const r = await saveTape(store, p1, p2, len, mode, label);
          if (r === "kept") { toast("Die bisherigen Maße bleiben – Linie bei Bedarf genauer ziehen."); ctl.setLine(null); idle(); }
          else if (r) { saved = true; close(); }
        } catch (e) { toast(errText(e), { kind: "err" }); }
        ok.disabled = false;
      } } }, "Maße übernehmen");
      input.addEventListener("keydown", e => { if (e.key === "Enter") ok.click(); });
      form.replaceChildren(meas, seg,
        el("label.field", {}, el("span.label", {}, "Wie lang ist das in echt?"), el("span.unit", {}, input, el("span", {}, "cm"))),
        el("div.tp-btns", {}, el("button.btn.secondary", { type: "button", on: { click: () => { ctl.setLine(null); idle(); } } }, "Neu ziehen"), ok));
      if (!COARSE) setTimeout(() => input.focus(), 60);
    }
    idle();
  });
}

async function analyze(store, force) {
  const doc = store.doc;
  if (!doc?.room) return;
  const pid = doc.id, room = doc.room;
  _setAnalysis(pid, { room, phase: "analyzing" });
  let a = null;
  try {
    const g = await api("POST", "/api/geometry/analyze", { room, force: !!force });
    if (store.pid !== pid || store.doc?.room !== room) return;
    if (!g.ok && store.doc.geometry?.ok) toast(`${g.error || "Raumerkennung fehlgeschlagen."} Die bisherige Vermessung bleibt.`, { kind: "err" });
    else if (!g.ok && transient(g.error)) a = { room, phase: "failed", busy: true, error: g.error };
    else {
      await store.patch(analysisOps(store.doc, g, store.catalog), { now: true });
      if (!g.ok) a = { room, phase: "failed", error: g.error };
    }
  } catch (e) {
    a = { room, phase: "failed", busy: e?.status === 503, error: errText(e) };
  } finally {
    if (AN.get(pid)?.room === room) _setAnalysis(pid, a);
  }
}

/** Starts the analysis when a room photo has no measurement yet (opened from the dashboard, or a colleague's photo). */
export function ensureAnalysis(store) {
  const doc = store.doc, a = analysisOf(doc);
  if (doc?.room && doc.geometry == null && !a) analyze(store, false);
}

css("ws-raum-css", `
.rm{display:flex;flex-direction:column;gap:16px;max-width:760px;margin:0 auto;width:100%}
.rm h2{margin:0;font-size:22px}
.rm .rm-big{min-height:72px;font-size:18px;display:flex;gap:10px;align-items:center;justify-content:center;border:2px solid var(--accent);color:var(--accent);font-weight:600}
.rm .rm-big .ed-ico{width:28px;height:28px}
.rm .rm-hint{margin:0;color:var(--muted);font-size:15px}
.rm .rm-photo{position:relative;background:var(--stage,#1b1c1f);border-radius:var(--r,12px);overflow:hidden;display:flex;justify-content:center}
.rm .rm-photo img{display:block;max-width:100%;max-height:min(52vh,560px);object-fit:contain}
.rm .rm-photo canvas{position:absolute;pointer-events:none}
.rm .rm-status{display:flex;flex-direction:column;gap:4px}
.rm .rm-status p{margin:0}
.rm .rm-ok{color:var(--ok);font-weight:600}
.rm .rm-need{margin:0 0 8px;padding:10px 12px;border-radius:10px;background:var(--warn-soft);color:var(--text);font-size:15px}
.rm .rm-row{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}
.rm .rm-row .field{flex:1;min-width:150px;margin:0}
.rm .unit input{font-size:18px}
.rm .rm-btns{display:flex;gap:8px;flex-wrap:wrap}
.rm .rm-btns .btn{flex:1;min-width:150px}
.rm .rm-sample{display:flex;gap:12px;align-items:center}
.rm .rm-sample img{width:96px;height:72px;object-fit:cover;border-radius:8px}
.rm dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0}.rm dt{color:var(--muted)}.rm dd{margin:0}
.ed-tape{position:fixed;inset:0;z-index:75;background:#0d0e10;color:#fff;display:flex;flex-direction:column}
.ed-tape .tp-top{display:flex;gap:8px;align-items:center;padding:8px 8px 8px 16px;padding-top:max(8px,env(safe-area-inset-top));font-size:16px;line-height:1.35}
.ed-tape .tp-top p{margin:0;flex:1;font-weight:600}
.ed-tape .tp-x{color:#fff;flex:none;min-height:44px}
.ed-tape .tp-stage{flex:1;min-height:0;position:relative;overflow:hidden;touch-action:none}
.ed-tape .tp-cv{position:absolute;transform-origin:0 0}.ed-tape .tp-cv img,.ed-tape .tp-cv canvas{position:absolute;inset:0;width:100%;height:100%}
.ed-tape .tp-form{flex:none;max-height:42svh;overflow-y:auto;background:var(--panel);color:var(--text);border-radius:16px 16px 0 0;
 padding:12px 16px;padding-bottom:max(12px,env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:10px}
.ed-tape .tp-form p{margin:0}.ed-tape .tp-meas{font-weight:600}.ed-tape .tp-hint{color:var(--muted)}
.ed-tape .tp-form .field{margin:0}.ed-tape .tp-form .unit input{font-size:18px}
.ed-tape .tp-form .seg{display:flex;gap:6px}.ed-tape .tp-form .seg .btn{flex:1}
.ed-tape .tp-btns{display:flex;gap:8px}.ed-tape .tp-btns .btn{flex:1}
`);

export default {
  id: "raum", title: "Raum", onAnalysis, ensureAnalysis,
  state(doc, view) {
    const g = raumGate(doc, view);
    return { done: !g.disabled, available: true, reason: null };
  },
  mount(root, sctx) {
    const { store } = sctx;
    let alive = true, showWalls = false, ctrl = null;
    const unbinds = [];
    const doc = () => store.doc;
    const off = onAnalysis(() => { if (alive) { render(); sctx.refresh(); } });

    async function choosePhoto(camera) {
      const file = await pickImage({ camera });   // first await: keeps the user gesture
      if (file && alive) useFile(file);
    }
    async function useFile(file) {
      const d = doc();
      if ((d.pieces || []).some(placed) || (d.removes || []).length) {
        const ok = await confirmDialog({ title: "Anderes Foto", text: "Neues Raumfoto? Die Platzierung der Möbel wird dabei zurückgesetzt.", ok: "Neues Foto verwenden" });
        if (!ok) return;
      }
      const pid = d.id;
      _setAnalysis(pid, { phase: "upload", pct: 0 });
      let up;
      try {
        up = await upload(file, { maxSide: 2560, onProgress: (x, total) => {
          const cur = AN.get(pid); if (!cur || cur.phase !== "upload") return;
          cur.pct = total ? x / total : x > 1 ? x / 100 : x;
          const bar = root.querySelector(".rm-up > i"); if (bar) bar.style.width = `${Math.round(cur.pct * 100)}%`;
        } });
      } catch (e) { _setAnalysis(pid, null); toast(errText(e), { kind: "err" }); return; }
      if (store.pid !== pid) return keepElsewhere(pid, up);
      const v = sctx.view; v.noRoom = false; sctx.saveView();
      _setAnalysis(pid, { room: up.path, phase: "analyzing" });
      try { await store.patch(newRoomOps(store.doc, up), { now: true }); } catch (e) { _setAnalysis(pid, null); toast(errText(e), { kind: "err" }); return; }
      AN.delete(pid);
      analyze(store, false);
    }
    // the order was left while the photo uploaded: save it there anyway (one PATCH on a fresh doc), never drop it
    async function keepElsewhere(pid, up) {
      _setAnalysis(pid, null);
      try {
        const fresh = await api("GET", `/api/projects/workshop/${pid}`);
        await api("PATCH", `/api/projects/workshop/${pid}`, { base_rev: fresh.rev, ops: newRoomOps(fresh, up) });
        toast(`Raumfoto für „${fresh.title || "Auftrag"}“ gespeichert.`, { kind: "ok" });
      } catch (e) { toast(`Das Raumfoto konnte nicht gespeichert werden: ${errText(e)}`, { kind: "err" }); }
    }
    function photoButtons(kind = "room") {
      const label = kind === "room" ? ["📷 Raum fotografieren", "Foto auswählen"] : ["📷 Muster fotografieren", "Foto auswählen"];
      const act = kind === "room" ? choosePhoto : chooseSample;
      return [
        el("button.btn.secondary.block.rm-big", { type: "button", on: { click: () => act(true) } }, label[0]),
        el("button.btn.secondary.block", { type: "button", on: { click: () => act(false) } }, label[1]),
      ];
    }
    async function chooseSample(camera) {
      const file = await pickImage({ camera });
      if (!file || !alive) return;
      try { const up = await upload(file, { maxSide: 2048 }); await store.patch([set("sample", up.path)], { now: true }); }
      catch (e) { toast(errText(e), { kind: "err" }); }
    }
    function otherPhoto() {
      if (!COARSE) return choosePhoto(false);
      let sh = null;
      const body = el("div.rm-btns", {}, el("button.btn.secondary.block", { type: "button", on: { click: () => { sh.close(); choosePhoto(true); } } }, "📷 Raum fotografieren"),
        el("button.btn.secondary.block", { type: "button", on: { click: () => { sh.close(); choosePhoto(false); } } }, "Foto auswählen"));
      sh = sheet({ title: "Anderes Foto", body, actions: [{ label: "Abbrechen", kind: "secondary" }] });
    }

    /* ---- calibration ---- */
    async function resetCal() {
      const d = doc();
      try {
        const r = await api("POST", "/api/geometry/calibrate", { room: d.room, reset: true });
        await store.patch(calibrationOps(doc(), r.geometry, doc().geometry?.scale || 1, null, store.catalog), { now: true });
        toast("Messung zurückgesetzt.");
      } catch (e) { toast(errText(e), { kind: "err" }); }
    }
    const tape = () => openTape(store);

    /* ---- rendering ---- */
    function photo(d) {
      const im = el("img", { src: img(d.room), alt: "Raumfoto", draggable: "false" });
      const box = el("div.rm-photo", {}, im);
      if (showWalls && d.geometry?.ok && globalThis.W3D) {
        const c = el("canvas");
        box.append(c);
        const draw = () => {
          const sc = globalThis.W3D.init(d.geometry, store.catalog);
          c.style.left = `${im.offsetLeft}px`; c.style.top = `${im.offsetTop}px`; c.style.width = `${im.clientWidth}px`; c.style.height = `${im.clientHeight}px`;
          ctrl?.destroy();
          ctrl = globalThis.W3D.attach(c, { scene: sc, imgSize: sc.size, mode: "off", showRoom: true, touch: COARSE, getPieces: () => doc().pieces, getActiveId: () => null });
        };
        im.complete ? requestAnimationFrame(draw) : im.addEventListener("load", draw, { once: true });
      }
      return box;
    }
    function status(d) {
      const a = analysisOf(d), g = d.geometry;
      if (a?.phase === "upload") return el("div.rm-status", {}, el("p", {}, T.uploading), el("div.bar.rm-up", {}, el("i", { style: { width: `${Math.round((a.pct || 0) * 100)}%` } })));
      if (a?.phase === "analyzing" || (d.room && !g && !a)) return el("div.rm-status", { role: "status" }, el("p", {}, "Raum wird vermessen … (ca. 10 Sek.)"), el("div.bar.indeterminate", {}, el("i")));
      if (a?.phase === "failed" || (g && !g.ok)) return el("div.rm-status.card", { role: "status" },
        el("p", {}, a?.busy ? T.busy : T.failed),
        el("div.rm-btns", {}, el("button.btn.secondary", { type: "button", on: { click: () => analyze(store, true) } }, "Nochmal versuchen")));
      if (!g?.ok) return null;
      const ch = g.ceiling?.height, done = measuredText(g);
      return el("div.rm-status", { role: "status" }, el("p.rm-ok", {}, "✓ Raum erkannt"),
        ch ? el("p", {}, `Deckenhöhe ${done ? "" : "ca. "}${fmt.m(ch)}`) : null, done ? el("p.rm-ok", {}, `✓ ${done}`) : el("p", {}, CONF[g.confidence] || CONF.low));
    }
    function heightCard(d) {
      const g = d.geometry;
      if (!g?.ok) return null;
      const ch = g.ceiling?.height;
      const input = el("input", { type: "number", inputmode: "numeric", min: "100", max: "500", value: ch ? String(Math.round(ch)) : String(d.calibration?.room_height_cm || ""),
        placeholder: "z. B. 250", "aria-label": "Raumhöhe in cm" });
      const go = () => { input.blur(); saveHeight(store, parseFloat(input.value)); };  // blur: the status line re-renders
      input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
      return el("div.card.rm-cal", {},
        isMeasured(d) ? null : el("p.rm-need", {}, "Wichtig für echte Größen: Bitte einmal nachmessen – Deckenhöhe bestätigen oder eine bekannte Länge einzeichnen (z. B. Türbreite)."),
        el("label.field", {}, el("span.label", {}, ch ? "Ist die Decke so hoch?" : "Decke nicht im Bild – Raumhöhe eingeben:"),
          el("span.rm-row", {}, el("span.unit", { style: { flex: "1" } }, input, el("span", {}, "cm")),
            el("button.btn.secondary", { type: "button", on: { click: go } }, "Übernehmen"))),
        el("div.rm-btns", {}, el("button.btn.secondary", { type: "button", on: { click: tape } }, iconEl("ruler"), " Genauer nachmessen")));
    }
    function expert(d) {
      const g = d.geometry;
      if (!g?.ok) return el("details.details", {}, el("summary", {}, "Expertenansicht"),
        el("div.rm-btns", {}, el("button.btn.ghost", { type: "button", on: { click: () => analyze(store, true) } }, "Raum neu erkennen")));
      const det = el("details.details", { open: showWalls || null }, el("summary", {}, "Expertenansicht"),
        el("dl", {},
          g.floor?.d ? [el("dt", {}, "Kamerahöhe"), el("dd", {}, `ca. ${Math.round(g.floor.d)} cm`)] : null,
          el("dt", {}, "Maßstab-Faktor"), el("dd", {}, fmt.num(g.scale || 1, 2)),
          el("dt", {}, "Genauigkeit"), el("dd", {}, ACC[g.confidence] || ACC.low)),
        el("div.rm-btns", {},
          g.depth_preview ? el("button.btn.ghost", { type: "button", on: { click: () => {
            sheet({ title: "Tiefenkarte", body: el("img", { src: img(g.depth_preview), alt: "Tiefenkarte", style: { maxWidth: "100%" } }), actions: [{ label: "Schließen", kind: "secondary" }] });
          } } }, "Tiefenkarte ansehen") : null,
          el("button.btn.ghost", { type: "button", "aria-pressed": String(showWalls), on: { click: () => { showWalls = !showWalls; render(); } } }, showWalls ? "Wände ausblenden" : "Wände anzeigen"),
          el("button.btn.ghost", { type: "button", on: { click: () => analyze(store, true) } }, "Raum neu erkennen"),
          Math.abs((g.scale || 1) - 1) > 0.001 ? el("button.btn.ghost", { type: "button", on: { click: resetCal } }, "Messung zurücksetzen") : null));
      return det;
    }
    function sampleCard(d) {
      return el("div.card", {}, el("h3", {}, "Holzmuster (optional)"),
        el("p.rm-hint", {}, "Foto einer Musterplatte – das Holz im Bild wird daran angepasst."),
        d.sample ? el("div.rm-sample", {}, el("img", { src: img(d.sample), alt: "Holzmuster" }),
          el("button.btn.ghost", { type: "button", on: { click: () => store.patch([set("sample", null)], { now: true }) } }, "Entfernen"))
          : el("div.rm-btns", {}, el("button.btn.secondary", { type: "button", on: { click: () => chooseSample(true) } }, "📷 Muster fotografieren"),
            el("button.btn.secondary", { type: "button", on: { click: () => chooseSample(false) } }, "Foto auswählen")));
    }
    function freeCard() {
      const ta = el("textarea", { rows: 3, placeholder: "z. B. heller Altbau-Flur mit Dielenboden", "aria-label": "Umgebung (optional)" });
      unbinds.push(store.bind(ta, "setting", { debounce: 400 }));
      return el("div.card", {}, el("h3", {}, "Freie Visualisierung"),
        el("label.field", {}, el("span.label", {}, "Umgebung (optional)"), ta));
    }

    let deferred = false;
    const focusedField = () => root.contains(document.activeElement) && document.activeElement.matches("input,textarea");
    const onFocusOut = () => setTimeout(() => { if (deferred && alive && !focusedField()) render(); });
    root.addEventListener("focusout", onFocusOut);
    function render() {
      if (!alive || !doc()) return;
      if (focusedField()) { deferred = true; return; }  // never steal a field; re-rendered when it loses focus
      deferred = false;
      for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
      ctrl?.destroy(); ctrl = null;
      const d = doc(), v = sctx.view, a = analysisOf(d), kids = [];
      if (!d.room && a?.phase !== "upload") {
        kids.push(el("h2", {}, "Raum fotografieren"), ...photoButtons(),
          el("p.rm-hint", {}, "Am besten Hoch- oder Querformat, aus einer Ecke, Boden und Decke sichtbar. Bitte ohne Personen fotografieren."));
        if (noRoom(d, v)) kids.push(freeCard());
        else kids.push(el("button.btn.ghost.block", { type: "button", on: { click: () => { v.noRoom = true; sctx.saveView(); render(); sctx.refresh(); } } },
          "Ohne Raumfoto weiter (freie Visualisierung)"));
      } else if (!d.room) {
        kids.push(el("h2", {}, "Raum fotografieren"), status(d));
      } else {
        kids.push(photo(d), status(d), heightCard(d),
          el("div.rm-btns", {}, el("button.btn.secondary", { type: "button", on: { click: otherPhoto } }, "Anderes Foto")),
          expert(d), sampleCard(d));
      }
      root.replaceChildren(el("div.rm", {}, kids));
    }

    // drop a photo on the page (desktop)
    const onDrop = e => {
      const f = e.dataTransfer?.files?.[0];
      if (!f || !f.type.startsWith("image/")) return;
      e.preventDefault();
      useFile(f);
    };
    const onOver = e => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); };
    root.addEventListener("dragover", onOver);
    root.addEventListener("drop", onDrop);

    render();
    ensureAnalysis(store);
    if (sctx.view?.tape) { const v = sctx.view; delete v.tape; sctx.saveView(); }  // older links: the tape now opens in place
    return {
      onDoc(evt) {
        if (!evt?.paths || evt.paths.some(p => /^(room|room_size|geometry|calibration|sample|setting|pieces|removes)/.test(p)) || evt.source === "local") render();
      },
      primary() {
        const g = raumGate(doc(), sctx.view);
        return { label: "Weiter: Möbel", disabled: g.disabled, reason: g.reason, run: async () => {
          const d = doc();
          if (!d.room || isMeasured(d)) return sctx.next();
          const ch = d.geometry.ceiling?.height;
          const v = await choose({ title: "Maße sind noch geschätzt", text: "Ohne eine echte Messung können die Möbel im Bild zu groß oder zu klein wirken. Einmal nachmessen dauert 10 Sekunden.",
            buttons: [...(ch ? [{ label: `Decke ist ${fmt.m(ch)} hoch – stimmt`, value: "ceil", kind: "primary" }] : []),
              { label: "Bekannte Länge einzeichnen", value: "tape", kind: ch ? "secondary" : "primary" },
              { label: "Trotzdem weiter (Größen geschätzt)", value: "go", kind: "secondary" }] });
          if (v === "ceil") { await saveHeight(store, Math.round(ch)); if (isMeasured(doc())) sctx.next(); }
          else if (v === "tape") openTape(store);
          else if (v === "go") sctx.next();
        } };
      },
      unmount() {
        alive = false; off();
        root.removeEventListener("dragover", onOver); root.removeEventListener("drop", onDrop); root.removeEventListener("focusout", onFocusOut);
        for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
        ctrl?.destroy();
      },
    };
  },
};
