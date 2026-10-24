/* ③ Platzieren: pieces as 3D boxes on the room photo (W3D), 2D frames without a measurement, remove frames.
   Phones run immersive (photo full width, four actions + „Mehr …“ sheet); tablets/desktops get a side panel.
   Gestures hold the piece's placement in the store so a colleague's move never yanks it mid-drag (spec §12, §16.7). */
import { toast, sheet, toggleRow, img, COARSE, api } from "./ui.js";
import { el, iconEl, css, set, clone, placed, activePiece, pieceName, sceneFor, reprojected, COLORS, NONE, isPhone, footerH, fit, errText, tooWide, wideFixButtons } from "./editor.js";

export const WARN = { out: "⚠ ragt aus dem Bild", high: "⚠ höher als die Decke", wide: "⚠ breiter als die Wand" };
export const INFO = "Alles innerhalb des Kastens wird neu gezeichnet – z. B. ein Tisch darin verschwindet.";
export const HINT = "Tippen setzt das Möbel an diese Stelle · Ziehen verschiebt · Kreis ziehen dreht · nahe an der Wand rastet es ein.";
export const TIP = "Tippen: hierhin setzen · Ziehen: verschieben · Kreis: drehen · zwei Finger: zoomen";
const REMOVE_HINT = "Tippen Sie auf das, was verschwinden soll (alter Tisch, Stuhl, Karton …) – oder ziehen Sie einen Rahmen.";
const DEFAULT_BOX = [300, 350, 700, 650];

/** Warning codes drawn next to a piece on the photo. */
export function warningsFor(piece, scene, doc) {
  const pl = piece?.placement, out = [];
  if (scene && pl?.mode === "3d" && pl.pose) {
    let r = null;
    try { r = scene.relations(piece); } catch (e) { console.error(e); }
    if (r?.outOfImage || !pl.box) out.push("out");
    if (r?.tooHigh) out.push("high");
    if (r?.tooWide) out.push("wide");
  } else if (placed(piece)) {
    const ch = doc?.geometry?.ok && doc.geometry.ceiling?.height;
    if (ch && +piece.h > ch) out.push("high");
  }
  return out;
}
export const unplacedText = doc => {
  const n = (doc?.pieces || []).filter(p => !placed(p)).length;
  return n ? `${n} Möbel noch nicht platziert – es wird an einer passenden Stelle ergänzt.` : null;
};

/** Local undo of the last 20 placement (or remove-frame) states; each entry is re-applied with one PATCH. */
export class UndoStack {
  constructor(max = 20) { this.max = max; this.items = []; }
  push(snap) { this.items.push(snap); if (this.items.length > this.max) this.items.shift(); }
  pop() { return this.items.pop() || null; }
  discard() { this.items.pop(); }
  get size() { return this.items.length; }
}

/** Arrow-key move of a 3d pose (cm); a wall piece slides along its wall and leaves it when moved away from it. */
export function nudgePose(scene, piece, pose, dx, dz) {
  let next = { x: pose.x + dx, z: pose.z + dz, yaw: pose.yaw || 0, wall: pose.wall || null };
  if (pose.wall) {
    const s = scene.snapToWall(next, piece, pose.wall);
    next = s && Math.hypot(s.x - pose.x, s.z - pose.z) > 0.5 ? s : { ...next, wall: null };
  }
  return scene.projectBox(piece, next).visible ? next : null;
}
export function nudgeBox(box, dy, dx) {
  const y = Math.max(-box[0], Math.min(1000 - box[2], dy)), x = Math.max(-box[1], Math.min(1000 - box[3], dx));
  return [box[0] + y, box[1] + x, box[2] + y, box[3] + x];
}
const layoutOf = () => (isPhone() ? (innerWidth > innerHeight ? "land" : "port") : innerWidth >= 900 ? "wide" : "tab");

css("ws-place-css", `
.pl{display:flex;flex-direction:column;gap:10px;min-width:0;width:100%}
.pl-stage{position:relative;overflow:hidden;touch-action:none;user-select:none;-webkit-user-select:none;min-height:0}
.pl-canvas{position:relative;flex:none;transform-origin:0 0}
.pl-canvas>img{position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none;max-height:none;-webkit-user-drag:none}
.pl-canvas>canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.pl-boxes{position:absolute;inset:0;pointer-events:none}
.pl-boxes.editing{pointer-events:auto;cursor:crosshair}
.pl-bx{--c:#59a7ff;position:absolute;border:2px solid var(--c);background:color-mix(in srgb,var(--c) 14%,transparent);cursor:move}
.pl-bx.act{border-width:3px}.pl-bx.fixed{border-style:dashed;pointer-events:none}
.pl-bx.rm{--c:#ef5b4c;background:repeating-linear-gradient(45deg,rgba(239,91,76,.22) 0 6px,transparent 6px 12px)}
.pl-bx>span{position:absolute;left:-2px;bottom:100%;font-size:13px;color:#111;background:var(--c);padding:1px 6px;border-radius:3px 3px 0 0;white-space:nowrap;max-width:60vw;overflow:hidden;text-overflow:ellipsis}
.pl-bx.rm>span{color:#fff}
.pl-bx.rm.poly{border:0;background:none;cursor:default;pointer-events:none}.pl-bx.rm.poly>button{pointer-events:auto}.pl-bx.rm.poly>.pg{position:absolute;inset:0}.pl-bx.rm.poly svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none}
.pl-bx.rm.poly polygon{fill:rgba(239,91,76,.35);stroke:#ef5b4c;stroke-width:2;vector-effect:non-scaling-stroke}
.pl-stage.picking{cursor:progress}
.pl-bx>button{position:absolute;right:-22px;top:-22px;width:44px;height:44px;border:0;border-radius:50%;background:rgba(0,0,0,.72);color:#fff;font-size:20px;line-height:44px;padding:0;cursor:pointer}
.pl-bx>i{position:absolute;right:-10px;bottom:-10px;width:44px;height:44px;cursor:nwse-resize;touch-action:none}
.pl-bx>i::after{content:"";position:absolute;right:6px;bottom:6px;width:24px;height:24px;box-sizing:border-box;background:#fff;border:2px solid var(--c);border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.4)}
.pl-boxes:not(.editing) .pl-bx>button,.pl-boxes:not(.editing) .pl-bx>i{display:none}
.pl-warns{position:absolute;inset:0;pointer-events:none}
.pl-warn{position:absolute;background:rgba(161,92,7,.95);color:#fff;font-size:13px;font-weight:600;padding:2px 7px;border-radius:4px;white-space:nowrap;line-height:1.35}
.pl-tip{position:absolute;left:50%;bottom:10px;transform:translateX(-50%);max-width:calc(100% - 20px);width:max-content;background:rgba(0,0,0,.62);color:#fff;
 padding:6px 12px;border-radius:999px;font-size:14px;pointer-events:none;transition:opacity .4s}
.pl-tip.gone{opacity:0}
.pl-hint .wide-fix{margin:0;justify-content:center}.pl-hint .wide-fix .btn{flex:0 1 auto;color:#fff;border-color:rgba(255,255,255,.5);background:rgba(255,255,255,.12)}
.pl-tools .btn[aria-pressed=true]{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}
.pl-hint{position:absolute;left:50%;top:10px;transform:translateX(-50%);max-width:calc(100% - 20px);width:max-content;background:rgba(0,0,0,.72);color:#fff;
 padding:8px 12px;border-radius:12px;font-size:15px;line-height:1.35;pointer-events:none;text-align:center;display:flex;flex-direction:column;gap:6px;align-items:center}
.pl-hint .btn{pointer-events:auto}
.pl-hint:empty{display:none}
.pl .pl-chips{flex:none;margin:0;padding-inline:0}
.pl-glyph{font-size:24px;line-height:24px}
.pl-tools{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;flex:none}
.pl-tools .btn{flex-direction:column;gap:2px;min-height:58px;padding:6px 2px;font-size:14px;font-weight:560;line-height:1.15}
.pl-panel{display:flex;flex-direction:column;gap:10px;min-width:0}
.pl-panel .btn{justify-content:flex-start}
.pl-panel .toggle-row{margin:0}
.pl-note{font-size:14px;color:var(--muted);margin:0}
.pl-note.warn{color:var(--warn)}
.pl-sheet{display:flex;flex-direction:column;gap:10px}
.pl-sheet .btn{justify-content:flex-start}
.pl-dims{display:grid;grid-template-columns:repeat(auto-fit,minmax(76px,1fr));gap:8px}
.pl-dims label{display:flex;flex-direction:column;gap:4px;font-size:14px;color:var(--muted)}
.pl-dims input{width:100%;min-width:0;min-height:48px;font-size:18px;text-align:center;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text)}
/* phones: immersive, full width */
.pl.phone{gap:0;overflow:hidden}
.pl.phone .pl-chips{padding:6px 8px 0;scroll-padding-inline:8px}
.pl.phone .pl-stage{border-radius:0;flex:1 1 0}
.pl.phone .pl-tools{padding:6px 8px}
.pl-bar{flex:none;display:flex;flex-direction:column;gap:6px;padding:8px 10px;background:var(--warn-soft,#fdf1dc);color:var(--text);border-top:2px solid var(--warn,#a15c07)}
.pl-bar p{margin:0;font-weight:600;color:var(--warn,#a15c07)}
.pl-bar .wide-fix{display:flex;gap:6px;margin:0}.pl-bar .wide-fix .btn{flex:1;min-height:44px;padding:6px 8px;font-size:15px}
.pl.phone.land .pl-bar{grid-column:1;grid-row:3}
.pl.phone.land{display:grid;grid-template-columns:minmax(0,1fr) 168px;grid-template-rows:auto minmax(0,1fr) auto}
.pl.phone.land .pl-chips{grid-column:1;grid-row:1}
.pl.phone.land .pl-stage{grid-column:1;grid-row:2;height:auto}
.pl.phone.land .pl-tools{grid-column:2;grid-row:1/4;grid-template-columns:1fr;grid-auto-rows:minmax(44px,1fr);padding:6px}
.pl.phone.land .pl-tools .btn{flex-direction:row;min-height:44px;gap:6px;justify-content:flex-start;padding:4px 8px}
.pl.wide{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:20px}
.pl.wide .pl-left{display:flex;flex-direction:column;gap:10px;min-height:0}
.pl.wide .pl-stage{flex:1}
.pl.wide .pl-panel{overflow-y:auto;min-height:0;padding-bottom:8px}
.pl.tab .pl-left{display:flex;flex-direction:column;gap:10px}
`);

export default {
  id: "platzieren", title: "Platzieren",
  state(doc) {
    if (!doc.room) return { done: false, available: false, reason: "Nur mit Raumfoto – in Schritt 1 ein Foto aufnehmen." };
    return { done: (doc.pieces || []).length > 0 && doc.pieces.every(placed), available: true, reason: null };
  },
  mount(root, sctx) {
    const { store } = sctx;
    let alive = true, tool = "place", showRoom = false, ctrl = null, scene = null, mode = null;
    let layout = null, gesture = 0, domDrag = null, liveRemoves = null, pinch = null, pinchHold = null, more = null;
    let zoom = { s: 1, tx: 0, ty: 0 }, tap = { t: 0, x: 0, y: 0 }, roomSize = null, resized = 0, userZoom = false, autoCx = null;
    const live = new Map(), pointers = new Map(), undo = new UndoStack(20), unbinds = [];
    const N = {};    // nodes of the current layout
    const doc = () => store.doc, cat = () => store.catalog;
    const v = () => sctx.view;
    const piece = id => (doc().pieces || []).find(p => p.id === id) || null;
    const pieces = () => (doc().pieces || []).map(p => (live.has(p.id) ? { ...p, placement: live.get(p.id) } : p));
    const act = () => activePiece(doc(), v());
    const plPath = id => `pieces.${id}.placement`;
    const removes = () => liveRemoves || doc().removes || [];
    const nameOf = p => pieceName(p, (doc().pieces || []).indexOf(p), cat());
    const hold = path => { try { store.hold?.(path); } catch (e) { console.error(e); } };
    const release = path => { try { store.release?.(path); } catch (e) { console.error(e); } };
    const patch = ops => store.patch(ops, { now: true }).catch(e => toast(errText(e), { kind: "err" }));

    function select(id) {
      if (!piece(id) || id === act()?.id) return;
      const vv = v(); vv.active_piece = id; sctx.saveView();
      if (layout === "port" || layout === "land") sctx.immersive(true, { title: nameOf(piece(id)) });
      applyMode(); chrome(); redraw();
    }
    function snapshot(path, id) {
      undo.push({ path, id, value: path === "removes" ? clone(doc().removes || []) : clone(piece(id)?.placement || NONE()) });
    }
    function commit(id, pl, elev) {
      const p = piece(id), ops = [set(plPath(id), pl)];
      if (elev != null && elev !== (+p?.elev || 0)) ops.push(set(`pieces.${id}.elev`, elev));
      if (scene && pl.mode === "3d" && p) {
        const d = scene.dims(p);
        ["w", "h", "d"].forEach((k, i) => { if (!(+p[k] > 0)) ops.push(set(`pieces.${id}.${k}`, Math.round(d[i]))); });
      }
      return patch(ops);
    }
    function doUndo() {
      let s = undo.pop();
      while (s && s.id && !piece(s.id)) s = undo.pop();
      if (!s) return;
      if (s.id && s.id !== act()?.id) { const vv = v(); vv.active_piece = s.id; sctx.saveView(); }
      patch([set(s.path, s.value)]);
    }

    /* ---------- scene & W3D ---------- */
    function ctrlMode() {
      const p = act();
      return scene && tool === "place" && p?.placement?.mode !== "2d" && !pinch ? "place" : "off";
    }
    function applyMode() {
      const m = ctrlMode();
      if (ctrl && m !== mode) try { ctrl.setMode(m); } catch (e) { console.error(e); }
      mode = m;
      if (N.boxes) N.boxes.classList.toggle("editing", m === "off" && !pinch && (tool === "remove" || !scene || act()?.placement?.mode === "2d"));
    }
    function attach() {
      try { ctrl?.destroy(); } catch { /* gone */ }
      ctrl = null; mode = null;
      if (!scene || !N.ov) return applyMode();
      try {
        ctrl = globalThis.W3D.attach(N.ov, { scene, imgSize: scene.size, mode: "off", showRoom, touch: COARSE, getZoom: () => zoom.s,
          getPieces: pieces, getActiveId: () => act()?.id, onSelect: select,
          onBegin: (id, kind) => { gesture++; hold(plPath(id)); snapshot(plPath(id), id); N.tip?.classList.add("gone"); void kind; },
          onChange: (id, pose, o) => {
            const p = piece(id);
            if (!p) return;
            const { elev, ...bare } = pose;   // a tap on a wall also sets the hanging height
            const next = reprojected(scene, elev != null ? { ...p, elev } : p, { mode: "3d", pose: bare, box: null });
            if (!o.final) { live.set(id, next); warns(); return; }
            live.delete(id);
            if (!placed(p)) revealSoon(id);   // the first tap: the whole piece must be on screen
            const vv = v(); if (vv.active_piece !== id) { vv.active_piece = id; sctx.saveView(); }
            commit(id, next, elev);
          },
          onEnd: (id, kind, o) => {
            gesture = Math.max(0, gesture - 1);
            if (o.aborted) live.delete(id);
            if (!o.moved || o.aborted) undo.discard();
            release(plPath(id));
            settle(); redraw(); chrome();
          } });
      } catch (e) { console.error(e); ctrl = null; }
      applyMode();
    }
    function settle() { if (ensureScene()) size(); }   // a deferred measurement change after a gesture
    function ensureScene() {
      const s = sceneFor(doc(), cat());
      if (s === scene) return false;
      if (gesture || pinch) return false;   // a measurement change waits until the gesture is over
      scene = s;
      attach();
      return true;
    }

    /* ---------- drawing ---------- */
    const pct = r => ({ top: `${r[0] / 10}%`, left: `${r[1] / 10}%`, height: `${(r[2] - r[0]) / 10}%`, width: `${(r[3] - r[1]) / 10}%` });
    function boxes() {
      if (!N.boxes) return;
      const kids = [], a = act()?.id;
      pieces().forEach((p, i) => {
        const pl = p.placement, fixed = pl?.mode === "3d" && !scene;   // a 3d piece without a scene: its last box
        if (!pl?.box || !(pl.mode === "2d" || fixed)) return;
        kids.push(el("div.pl-bx" + (fixed ? ".fixed" : "") + (p.id === a ? ".act" : ""), { "data-b": `p:${p.id}`, style: { ...pct(pl.box), "--c": COLORS[i % COLORS.length] } },
          el("span", {}, nameOf(p)), fixed ? null : el("button", { type: "button", "data-x": `p:${p.id}`, "aria-label": "Rahmen entfernen" }, "×"), fixed ? null : el("i")));
      });
      removes().forEach((r, i) => {
        if (Array.isArray(r)) return kids.push(el("div.pl-bx.rm", { "data-b": `r:${i}`, style: pct(r) }, el("span", {}, "entfernen"),
          el("button", { type: "button", "data-x": `r:${i}`, "aria-label": "Entfernen-Rahmen löschen" }, "×"), el("i")));
        const b = r.box, w = Math.max(1, b[3] - b[1]), hh = Math.max(1, b[2] - b[0]);   // a tapped outline
        kids.push(el("div.pl-bx.rm.poly", { "data-b": `r:${i}`, style: pct(b) },
          el("div.pg", { html: `<svg viewBox="${b[1]} ${b[0]} ${w} ${hh}" preserveAspectRatio="none"><polygon points="${r.poly.map(p => p.join(",")).join(" ")}"/></svg>` }),
          el("button", { type: "button", "data-x": `r:${i}`, "aria-label": "Auswahl löschen" }, "×")));
      });
      N.boxes.replaceChildren(...kids);
    }
    function warns() {
      if (!N.warns) return;
      const d = doc(), kids = [], a = N.bar ? act()?.id : null;
      for (const p of pieces()) {
        if (p.id === a) continue;   // phones: the active piece's warnings sit in the bar under the photo
        const codes = warningsFor(p, scene, d);
        if (!codes.length) continue;
        const b = p.placement?.box;
        // inside the box below its top edge: the W3D dimension label sits above the edge (clamped to the canvas top)
        kids.push(el("div.pl-warn", { style: b ? { top: `max(calc(${b[0] / 10}% + 4px), 26px)`, left: `${Math.min(b[1] / 10, 70)}%` } : { top: "52px", left: "8px" } },
          codes.map((c, i) => [i ? el("br") : null, WARN[c]])));
      }
      N.warns.replaceChildren(...kids);
    }
    function redraw() {
      try { ctrl?.redraw(); } catch (e) { console.error(e); }
      if (!domDrag) boxes();
      warns();
    }
    function hintText() {
      const p = act();
      if (!p) return null;
      const name = nameOf(p);
      if (tool === "remove") return REMOVE_HINT;
      if (placed(p)) return null;
      if (!scene) return [doc().geometry && !doc().geometry.ok || !doc().geometry ? "Raum nicht vermessen – bitte von Hand einrahmen." : null, `Ziehen Sie einen Rahmen, wo „${name}“ stehen soll.`];
      if (p.placement?.mode === "2d") return `Ziehen Sie einen Rahmen, wo „${name}“ stehen soll.`;
      return `Tippen Sie mitten auf die Stelle, wo „${name}“ stehen soll – auch auf ein altes Möbel, das ersetzt wird.`;
    }

    /* ---------- actions ---------- */
    function can() {
      const p = act(), pl = p?.placement, is3d = !!(scene && pl?.mode === "3d" && pl.pose);
      return { wall: is3d && scene.walls.length > 0, rotate: is3d, undo: undo.size > 0, free: is3d && !!pl.pose.wall,
        to2d: !!scene && pl?.mode !== "2d", to3d: !!scene && pl?.mode === "2d" };
    }
    function toWall() {
      const p = act(), pose = p?.placement?.pose;
      if (!pose || !scene) return;
      let s = null;
      try { const nw = scene.nearestWall(pose.x, pose.z); s = nw && scene.snapToWall(pose, p, nw.wall.id); } catch (e) { console.error(e); }
      if (!s) return toast("Keine passende Wand in der Nähe.", { kind: "err" });
      snapshot(plPath(p.id), p.id);
      commit(p.id, reprojected(scene, p, { mode: "3d", pose: s, box: null })).then(() => revealSoon(p.id));
      toast(`„${nameOf(p)}“ steht jetzt an der Wand.`, { kind: "ok", ms: 2500 });
    }
    function wallToggle() { if (can().free) { freeFromWall(); toast("Von der Wand gelöst.", { ms: 2500 }); } else toWall(); }
    function rotate() {
      const p = act(), pose = p?.placement?.pose;
      if (!pose || !scene) return;
      snapshot(plPath(p.id), p.id);
      commit(p.id, reprojected(scene, p, { mode: "3d", pose: scene.turn(pose, p, 90), box: null })).then(() => revealSoon(p.id));
    }
    function freeFromWall() {
      const p = act(), pl = p?.placement;
      if (!pl?.pose?.wall) return;
      snapshot(plPath(p.id), p.id);
      commit(p.id, { ...pl, pose: { ...pl.pose, wall: null } });
    }
    function to2d() {
      const p = act(), pl = p.placement;
      snapshot(plPath(p.id), p.id);
      tool = "place";
      commit(p.id, { mode: "2d", pose: pl.pose || null, box: pl.box || DEFAULT_BOX.slice() });
    }
    function to3d() {
      const p = act(), pl = p.placement;
      snapshot(plPath(p.id), p.id);
      tool = "place";
      commit(p.id, pl.pose ? reprojected(scene, p, { mode: "3d", pose: pl.pose, box: null }) : NONE());
    }
    function clearPlacement() {
      const p = act();
      if (!p || p.placement?.mode === "none") return;
      snapshot(plPath(p.id), p.id);
      commit(p.id, NONE());
    }
    function setTool(t) { tool = t; applyMode(); chrome(); }
    function setWalls(on) { showRoom = !!on; try { ctrl?.setShowRoom(showRoom); } catch (e) { console.error(e); } }

    function dimsFields(p) {
      const parse = x => { const n = parseInt(x, 10); return n >= 1 ? Math.min(2000, n) : null; };
      const f = (k, lab) => {
        const inp = el("input", { type: "number", inputmode: "numeric", min: "1", max: "2000", "aria-label": `${lab} in cm` });
        unbinds.push(store.bind(inp, `pieces.${p.id}.${k}`, { parse, format: x => (x == null ? "" : String(x)) }));
        return el("label", {}, `${lab} (cm)`, inp);
      };
      const e = el("input", { type: "number", inputmode: "numeric", min: "0", max: "400", placeholder: "0", "aria-label": "Abstand vom Boden in cm" });
      unbinds.push(store.bind(e, `pieces.${p.id}.elev`, { parse: x => { const n = parseInt(x, 10); return n > 0 ? Math.min(400, n) : 0; }, format: x => (x ? String(x) : "") }));
      return el("div.pl-dims", {}, f("w", "Breite"), f("h", "Höhe"), f("d", "Tiefe"), el("label", {}, "Über Boden (cm)", e));
    }
    function toolButtons(list) {   // the extra tools of the „Mehr …“ sheet and the desktop panel
      const c = can(), b = (label, on, ok = true, icn) => el("button.btn.secondary.block", { type: "button", disabled: !ok, on: { click: on } }, icn ? iconEl(icn) : null, label);
      list.push(
        tool === "remove" ? b("Fertig", () => { setTool("place"); more?.close(); }, true, "check") : b("Etwas aus dem Bild entfernen", () => { setTool("remove"); more?.close(); }, true, "trash"),
        c.to2d ? b("Von Hand einrahmen", () => { to2d(); more?.close(); }) : null,
        c.to3d ? b("Zurück zur 3D-Platzierung", () => { to3d(); more?.close(); }) : null,
        scene ? toggleRow({ title: "Wände anzeigen", checked: showRoom, onChange: setWalls }) : null);
      return list;
    }
    function openMore() {
      const p = act();
      for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
      const dims = el("div", { hidden: true }, p ? dimsFields(p) : null);
      const body = el("div.pl-sheet", {},
        el("button.btn.secondary.block", { type: "button", disabled: !p, on: { click: () => { dims.hidden = !dims.hidden; } } }, iconEl("ruler"), "Maße ändern"), dims,
        toolButtons([]), el("p.pl-note", {}, HINT), el("p.pl-note", {}, INFO));
      more = sheet({ title: "Weitere Werkzeuge", body, actions: [{ label: "Fertig", kind: "secondary" }], onClose: () => { more = null; } });
    }
    function fourTools() {
      const c = can(), b = (icn, label, on, ok) => el("button.btn.secondary", { type: "button", disabled: !ok, on: { click: on } }, iconEl(icn), label);
      const turn = el("button.btn.secondary", { type: "button", disabled: !c.rotate, "aria-label": "↻ 90° drehen", on: { click: rotate } }, el("span.pl-glyph", { "aria-hidden": "true" }, "↻"), "90° drehen");
      const wall = el("button.btn.secondary", { type: "button", disabled: !c.wall, "aria-pressed": String(c.free), on: { click: wallToggle } },
        iconEl("wall"), c.free ? "An der Wand ✓" : "An die Wand");
      return [wall, turn,
        b("undo", "Rückgängig", doUndo, c.undo), b("more", "Mehr …", openMore, true)];
    }

    /* ---------- chrome (chips, tools, panel, hint) ---------- */
    let panelPiece = null;
    function chrome() {
      if (!alive || !N.root) return;
      const d = doc(), a = act();
      if (N.chips) {
        const list = d.pieces || [];
        N.chips.hidden = list.length < 2;
        N.chips.replaceChildren(...list.map((p, i) => el("button.chip" + (p.id === a?.id ? ".active" : ""), { type: "button", "aria-pressed": String(p.id === a?.id),
          on: { click: () => select(p.id) } }, el("i.dot", { style: { background: COLORS[i % COLORS.length] } }), nameOf(p), placed(p) ? "" : " ·  offen")));
      }
      if (N.tools) N.tools.replaceChildren(...fourTools());
      if (N.panel) {
        if (panelPiece !== a?.id || !N.dims) {
          for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
          panelPiece = a?.id;
          N.dims = a ? dimsFields(a) : el("div");
        }
        const notes = [unplacedText(d), ...(a ? warningsFor(a, scene, d).map(c => WARN[c]) : [])].filter(Boolean);
        const dist = a && scene && a.placement?.mode === "3d" && a.placement.pose && scene.distance ? scene.distance(a.placement.pose) : null;
        const keep = N.panel.contains(document.activeElement) && document.activeElement.matches("input");
        const kids = [el("h3", {}, a ? nameOf(a) : "Möbel"), ...fourTools().slice(0, 3).map(n => { n.classList.add("block"); return n; }),
          el("div", {}, el("p.pl-note", {}, "Maße ändern"), N.dims), ...toolButtons([]),
          ...notes.map(t => el("p.pl-note.warn", {}, t)), a ? wideFixButtons(a, scene, sctx) : null,
          dist ? el("p.pl-note", {}, `Steht ca. ${(dist / 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} m vom Standort des Fotos entfernt.`) : null,
          el("p.pl-note", {}, scene ? HINT : "Raum nicht vermessen – bitte von Hand einrahmen."), el("p.pl-note", {}, INFO)];
        if (!keep) N.panel.replaceChildren(...kids.filter(Boolean));
      }
      if (N.hint) {
        const t = hintText();
        N.hint.replaceChildren(...[].concat(t || []).filter(Boolean).map(x => el("span", {}, x)),
          ...(tool === "remove" ? [el("button.btn.secondary", { type: "button", on: { click: () => setTool("place") } }, "Fertig")] : []));
      }
      if (N.bar) {   // phones: warnings below the photo, never over the piece and its size label
        const wide = tool !== "remove" && a && tooWide(a, scene);
        const codes = a && tool !== "remove" ? warningsFor(a, scene, d).filter(c => c !== "wide" || !wide) : [];
        N.bar.replaceChildren(...(wide ? [el("p", {}, `⚠ Breiter als die Wand (${wide.wall} cm)`), wideFixButtons(a, scene, sctx)] : []),
          ...codes.map(c => el("p", {}, WARN[c])));
        N.bar.hidden = !N.bar.childNodes.length;
      }
      if (N.tip) N.tip.hidden = !(a && placed(a) && tool !== "remove" && scene);
      sctx.refresh();
    }

    /* ---------- layout ---------- */
    function aspect() {
      const g = doc().geometry, s = (g?.ok && g.size) || doc().room_size || roomSize;
      return s ? s[0] / s[1] : 4 / 3;
    }
    function build() {
      for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
      try { ctrl?.destroy(); } catch { /* gone */ }
      ctrl = null; mode = null; panelPiece = null; N.dims = null;
      const phone = layout === "port" || layout === "land";
      const imgEl = el("img", { src: img(doc().room), alt: "Raumfoto", draggable: "false" });
      if (!doc().room_size && !(doc().geometry?.ok)) imgEl.addEventListener("load", () => { roomSize = [imgEl.naturalWidth, imgEl.naturalHeight]; size(); }, { once: true });
      N.boxes = el("div.pl-boxes"); N.warns = el("div.pl-warns"); N.ov = el("canvas");
      N.canvas = el("div.pl-canvas", {}, imgEl, N.boxes, N.ov, N.warns);
      N.hint = el("div.pl-hint", { "aria-live": "polite" });
      N.tip = phone ? el("div.pl-tip", {}, TIP) : null;
      if (N.tip) setTimeout(() => N.tip?.classList.add("gone"), 9000);
      N.stage = el("div.stage.pl-stage", {}, N.canvas, N.hint, N.tip);
      N.chips = el("div.chips.scroll.pl-chips", { role: "group", "aria-label": "Möbel wählen" });
      N.tools = phone ? el("div.pl-tools", { role: "toolbar", "aria-label": "Werkzeuge" }) : null;
      N.bar = phone ? el("div.pl-bar", { role: "status", hidden: true }) : null;
      N.panel = phone ? null : el("aside.pl-panel", { "aria-label": "Werkzeuge" });
      N.room = doc().room;
      N.root = phone ? el("div.pl.phone" + (layout === "land" ? ".land" : ""), {}, N.chips, N.stage, N.bar, N.tools)
        : el("div.pl." + layout, {}, el("div.pl-left", {}, N.chips, N.stage), N.panel);
      root.replaceChildren(N.root);
      wireStage();
      scene = sceneFor(doc(), cat());
      attach();
      boxes(); chrome(); size(); warns();
    }
    function size() {
      if (!alive || !N.root) return;
      const top = Math.max(0, N.root.getBoundingClientRect().top), avail = Math.max(240, innerHeight - top - footerH());
      if (layout === "port") {
        N.root.style.height = `${avail}px`; N.stage.style.height = "";
      } else if (layout === "land") {
        N.root.style.height = `${avail}px`; N.stage.style.height = "";
      } else if (layout === "wide") {
        N.root.style.height = `${Math.max(420, avail - 8)}px`; N.stage.style.height = "";
      } else {
        N.root.style.height = "";
        N.stage.style.height = `${Math.round(Math.min(Math.max(N.stage.clientWidth / aspect(), 320), innerHeight * 0.6))}px`;
      }
      const [w, h] = fit(N.stage.clientWidth, N.stage.clientHeight, aspect());
      N.canvas.style.width = `${w}px`; N.canvas.style.height = `${h}px`;
      const sh = N.stage.clientHeight;
      if (layout === "port" && !userZoom && h < sh * 0.8) {
        // a landscape photo on a portrait phone: start enlarged (up to the stage height) around the active piece
        const s0 = Math.min(1.6, sh / h), b = act()?.placement?.box;
        const cx = autoCx ??= b ? (b[1] + b[3]) / 2000 : 0.5;  // fixed once: later resizes must not follow the piece
        setZoom(s0, w / 2 - cx * w * s0, h * (1 - s0) / 2);
      } else if (!userZoom && zoom.s !== 1) setZoom(1, 0, 0);  // an earlier auto zoom no longer fits (stage grew)
      else setZoom(zoom.s, zoom.tx, zoom.ty);
      try { ctrl?.resize(); } catch (e) { console.error(e); }
    }
    function relayout() {
      const l = layoutOf();
      if (l !== layout) {
        layout = l;
        sctx.immersive(l === "port" || l === "land", { title: act() ? nameOf(act()) : "Platzieren" });
        build();
        requestAnimationFrame(size);
      } else size();
    }

    /* ---------- zoom / pinch ---------- */
    function setZoom(s, tx, ty) {
      const w = N.canvas?.clientWidth || 0, h = N.canvas?.clientHeight || 0;
      s = Math.max(1, Math.min(3, s));
      tx = Math.min(0, Math.max(w * (1 - s), tx)); ty = Math.min(0, Math.max(h * (1 - s), ty));
      zoom = { s, tx, ty };
      if (N.canvas) N.canvas.style.transform = s === 1 ? "" : `translate(${tx}px, ${ty}px) scale(${s})`;
      try { ctrl?.redraw(); } catch (e) { console.error(e); }   // the rotate handle keeps its screen size
    }
    /** Pans (and zooms out if needed) so the piece with its size label is fully on screen; a no-op when it already is. */
    function reveal(id) {
      const b = (live.get(id) || piece(id)?.placement)?.box;
      if (!b || !N.canvas || !N.stage || gesture || pinch) return;
      const w = N.canvas.clientWidth, h = N.canvas.clientHeight, W = N.stage.clientWidth, H = N.stage.clientHeight;
      const ox = N.canvas.offsetLeft, oy = N.canvas.offsetTop, m = 16, lab = 34;
      const x0 = b[1] / 1000 * w, x1 = b[3] / 1000 * w, y0 = b[0] / 1000 * h, y1 = b[2] / 1000 * h;
      const inX = q => ox + zoom.tx + zoom.s * q, inY = q => oy + zoom.ty + zoom.s * q;
      if (inX(x0) >= m && inX(x1) <= W - m && inY(y0) >= lab && inY(y1) <= H - m - 60) return;
      const s = Math.max(1, Math.min(zoom.s, (W - 2 * m) / Math.max(1, x1 - x0), (H - lab - m - 60) / Math.max(1, y1 - y0)));
      userZoom = true;
      setZoom(s, W / 2 - ox - s * (x0 + x1) / 2, (H - 60 + lab - m) / 2 - oy - s * (y0 + y1) / 2);
    }
    const revealSoon = id => requestAnimationFrame(() => requestAnimationFrame(() => { if (alive) reveal(id); }));
    const pts = () => [...pointers.values()];
    const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    function wireStage() {
      const st = N.stage;
      st.addEventListener("pointerdown", e => {
        pointers.set(e.pointerId, [e.clientX, e.clientY, e.timeStamp]);
        if (pointers.size === 2) {
          e.stopPropagation(); e.preventDefault();
          if (domDrag) cancelDomDrag();
          const [a, b] = pts();
          pinch = { d0: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, s0: zoom.s, m0: mid(a, b), t0: [zoom.tx, zoom.ty], ox: N.canvas.getBoundingClientRect() };
          userZoom = true;
          const id = act()?.id;
          pinchHold = id ? plPath(id) : null;
          if (pinchHold) hold(pinchHold);
          applyMode();   // W3D off: aborts its drag, the piece snaps back to where it was
        } else if (pointers.size > 2) e.stopPropagation();
      }, true);
      st.addEventListener("pointermove", e => {
        if (!pointers.has(e.pointerId)) return;
        const prev = pointers.get(e.pointerId);
        pointers.set(e.pointerId, [e.clientX, e.clientY, prev[2]]);
        if (!pinch || pointers.size < 2) return;
        e.stopPropagation();
        const [a, b] = pts(), m = mid(a, b), s = Math.max(1, Math.min(3, pinch.s0 * Math.hypot(a[0] - b[0], a[1] - b[1]) / pinch.d0));
        // keep the content point under the first midpoint under the fingers (layout origin = rect minus current translate)
        const L = pinch.ox.left - pinch.t0[0], T = pinch.ox.top - pinch.t0[1];
        const cx = (pinch.m0[0] - L - pinch.t0[0]) / pinch.s0, cy = (pinch.m0[1] - T - pinch.t0[1]) / pinch.s0;
        setZoom(s, m[0] - L - s * cx, m[1] - T - s * cy);
      }, true);
      const up = e => {
        const p = pointers.get(e.pointerId);
        pointers.delete(e.pointerId);
        if (pinch && pointers.size < 2) {   // the finger still down is ignored until it lifts
          pinch = null; pointers.clear(); tap.t = 0;
          if (pinchHold) release(pinchHold);
          pinchHold = null;
          e.stopPropagation();
          applyMode(); settle();
          return;
        }
        if (p && !pointers.size && e.type === "pointerup" && Math.hypot(e.clientX - p[0], e.clientY - p[1]) < 12 && e.timeStamp - p[2] < 300) {
          if (zoom.s > 1 && e.timeStamp - tap.t < 320 && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) < 30) { userZoom = true; setZoom(1, 0, 0); tap.t = 0; }
          else tap = { t: e.timeStamp, x: e.clientX, y: e.clientY };
        }
      };
      st.addEventListener("pointerup", up, true);
      st.addEventListener("pointercancel", up, true);
      wireBoxes();
    }

    /* ---------- 2D frames and remove frames (DOM editor, ported from v2) ---------- */
    function cancelDomDrag() {
      if (!domDrag) return;
      if (domDrag.k[0] === "p") live.delete(domDrag.k.slice(2)); else liveRemoves = null;
      release(domDrag.path);
      undo.discard();
      domDrag = null; boxes(); warns();
    }
    function wireBoxes() {
      const c = N.boxes;
      const pt = e => { const r = c.getBoundingClientRect(); return [Math.max(0, Math.min(1000, (e.clientY - r.top) / r.height * 1000)), Math.max(0, Math.min(1000, (e.clientX - r.left) / r.width * 1000))]; };
      const norm = r => [Math.min(r[0], r[2]), Math.min(r[1], r[3]), Math.max(r[0], r[2]), Math.max(r[1], r[3])].map(Math.round);
      const get = k => (k[0] === "p" ? (live.get(k.slice(2)) || piece(k.slice(2))?.placement)?.box : removes()[+k.slice(2)]);
      const put = (k, r) => {
        if (k[0] === "p") { const p = piece(k.slice(2)); live.set(p.id, { ...(live.get(p.id) || p.placement), mode: "2d", box: r }); }
        else { liveRemoves = (liveRemoves || clone(doc().removes || [])); liveRemoves[+k.slice(2)] = r; }
      };
      c.addEventListener("pointerdown", e => {
        if ((e.button || 0) > 0 || !c.classList.contains("editing") || pointers.size > 1) return;
        const x = e.target.dataset?.x;
        if (x != null) {
          e.stopPropagation();
          if (x[0] === "p") { const id = x.slice(2); snapshot(plPath(id), id); commit(id, NONE()); }
          else { snapshot("removes"); patch([set("removes", (doc().removes || []).filter((_, i) => i !== +x.slice(2)))]); }
          return;
        }
        try { c.setPointerCapture(e.pointerId); } catch { /* synthetic */ }
        const p = pt(e), bx = e.target.closest(".pl-bx");
        if (bx && get(bx.dataset.b) && !bx.classList.contains("poly")) {
          const k = bx.dataset.b;
          if (k[0] === "p" && k.slice(2) !== act()?.id) select(k.slice(2));
          domDrag = { k, mode: e.target.tagName === "I" ? "size" : "move", p0: p, r0: get(k).slice() };
        } else if (tool === "remove") {
          liveRemoves = [...clone(doc().removes || []), [p[0], p[1], p[0], p[1]]];
          domDrag = { k: `r:${liveRemoves.length - 1}`, mode: "new", p0: p };
        } else {
          const a = act();
          if (!a) return;
          domDrag = { k: `p:${a.id}`, mode: "new", p0: p };
          live.set(a.id, { mode: "2d", pose: a.placement?.pose || null, box: [p[0], p[1], p[0], p[1]] });
        }
        domDrag.path = domDrag.k[0] === "p" ? plPath(domDrag.k.slice(2)) : "removes";
        domDrag.k[0] === "p" ? snapshot(domDrag.path, domDrag.k.slice(2)) : snapshot("removes");
        hold(domDrag.path);
        e.preventDefault();
      });
      c.addEventListener("pointermove", e => {
        if (!domDrag) return;
        const p = pt(e), r0 = domDrag.r0;
        domDrag.moved = true;
        if (domDrag.mode === "new") put(domDrag.k, norm([domDrag.p0[0], domDrag.p0[1], p[0], p[1]]));
        else if (domDrag.mode === "size") put(domDrag.k, norm([r0[0], r0[1], p[0], p[1]]));
        else put(domDrag.k, nudgeBox(r0, p[0] - domDrag.p0[0], p[1] - domDrag.p0[1]).map(Math.round));
        boxes(); warns();
      });
      const end = () => {
        if (!domDrag) return;
        const d = domDrag, r = get(d.k);
        domDrag = null;
        const tiny = !r || r[2] - r[0] < 20 || r[3] - r[1] < 20;
        if ((d.mode === "new" && tiny) || !d.moved) {
          if (d.k[0] === "p") live.delete(d.k.slice(2)); else liveRemoves = null;
          undo.discard();
          if (tool === "remove" && d.mode === "new" && !d.moved) pick(d.p0);   // a tap: select the object under it
        } else if (d.k[0] === "p") {
          const id = d.k.slice(2), pl = live.get(id);
          live.delete(id);
          commit(id, pl);
        } else {
          const list = liveRemoves; liveRemoves = null;
          patch([set("removes", list)]);
        }
        release(d.path);
        settle(); boxes(); warns(); chrome();
      };
      c.addEventListener("pointerup", end);
      c.addEventListener("pointercancel", end);
    }

    /* ---------- tap-to-select (SAM 2 on the server) ---------- */
    async function pick(p) {   // p: [top, left] on the 0-1000 grid
      const d = doc(), sz = d.room_size || roomSize;
      if (!d.room || !sz) return;
      N.stage?.classList.add("picking");
      try {
        const r = await api("POST", "/api/geometry/segment", { room: d.room, point: [p[1] / 1000 * sz[0], p[0] / 1000 * sz[1]] });
        if (doc().room !== d.room) return;
        snapshot("removes");
        const at = (doc().removes || []).length, parts = (r.parts || []).map(o => ({ poly: o.poly, box: o.box }));
        await patch([set("removes", [...clone(doc().removes || []), ...parts])]);
        offerBigger(r.options || [], r.index || 0, at, parts.length);
      } catch (e) { toast(errText(e), { kind: "err" }); }
      finally { N.stage?.classList.remove("picking"); boxes(); chrome(); }
    }

    function offerBigger(opts, i, at, n) {   // SAM's next larger candidate replaces this selection (a cup → the whole table)
      const more = i + 1 < opts.length;
      toast(more ? "Ausgewählt. Nur ein Teil erwischt?" : "Ausgewählt – wird beim Berechnen entfernt.", { ms: 9000,
        action: more ? { label: "Größer auswählen", run: async () => {
          const list = clone(doc().removes || []);
          if (list.length < at + n) return;
          const next = opts[i + 1].map(o => ({ poly: o.poly, box: o.box }));
          list.splice(at, n, ...next);
          snapshot("removes");
          await patch([set("removes", list.slice(0, 20))]); boxes(); chrome();
          offerBigger(opts, i + 1, at, next.length);
        } } : null });
    }

    /* ---------- keyboard ---------- */
    function onKey(e) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector(".backdrop") || gesture || domDrag || pinch || root.inert) return;
      const p = act(), pl = p?.placement;
      if (!p) return;
      const k = e.key, step = e.shiftKey ? 25 : 5;
      const dir = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[k];
      if (dir) {
        if (scene && pl?.mode === "3d" && pl.pose) {
          const next = nudgePose(scene, p, pl.pose, dir[0] * step, dir[1] * step);
          e.preventDefault();
          if (!next) return;
          snapshot(plPath(p.id), p.id);
          commit(p.id, reprojected(scene, p, { mode: "3d", pose: next, box: null }));
        } else if (pl?.mode === "2d" && pl.box) {
          e.preventDefault();
          const g = e.shiftKey ? 50 : 10;
          snapshot(plPath(p.id), p.id);
          commit(p.id, { ...pl, box: nudgeBox(pl.box, -dir[1] * g, dir[0] * g) });
        }
      } else if ((k === "r" || k === "R") && can().rotate) { e.preventDefault(); rotate(); }
      else if (k === "Delete" || k === "Backspace") { e.preventDefault(); clearPlacement(); }
    }

    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => { cancelAnimationFrame(resized); resized = requestAnimationFrame(size); }) : null;
    const footer = document.querySelector(".ws-footer") || document.getElementById("ws-actions");
    if (ro && footer) ro.observe(footer);
    const onResize = () => { cancelAnimationFrame(resized); resized = requestAnimationFrame(relayout); };
    addEventListener("resize", onResize);
    addEventListener("orientationchange", onResize);
    document.addEventListener("keydown", onKey);
    relayout();
    if (ro && N.stage) ro.observe(N.stage);

    return {
      onDoc(evt) {
        if (!alive) return;
        if (!doc().room) return;
        if (doc().room !== N.room && !gesture && !domDrag && !pinch) { undo.items.length = 0; live.clear(); setZoom(1, 0, 0); return build(); }
        const a = act()?.id;
        if (ensureScene()) { size(); if (a) revealSoon(a); }   // new measurement: the box changed size
        else if (a && evt?.paths?.some(p => p === `pieces.${a}.w` || p === `pieces.${a}.h` || p === `pieces.${a}.d`)) revealSoon(a);
        if ((layout === "port" || layout === "land") && act()) sctx.immersive(true, { title: nameOf(act()) });
        applyMode(); chrome(); redraw();
      },
      primary() {
        return { label: "Weiter: Berechnen", disabled: false, warning: unplacedText(doc()), run: () => sctx.next() };
      },
      unmount() {
        alive = false;
        cancelDomDrag();
        if (pinchHold) release(pinchHold);
        removeEventListener("resize", onResize); removeEventListener("orientationchange", onResize);
        document.removeEventListener("keydown", onKey);
        ro?.disconnect();
        more?.close();
        for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
        try { ctrl?.destroy(); } catch { /* gone */ }
        sctx.immersive(false);
      },
    };
  },
};
