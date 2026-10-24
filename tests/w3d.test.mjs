import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const W3D = require("../static/werkstatt3d.js");
const FIX = JSON.parse(readFileSync(new URL("./fixtures/geometry_sample.json", import.meta.url)));
const fix = () => JSON.parse(JSON.stringify(FIX));

const A_CORNERS = [[367.19, 749.73], [865.97, 749.73], [410.08, 690.35], [838.68, 690.35],
  [331.10, 107.39], [888.93, 107.39], [383.87, 141.49], [855.36, 141.49]];
const B_CORNERS = [[-19.97, 1042.22], [226.83, 787.37], [-231.07, 1042.22], [91.02, 787.37],
  [-68.21, 684.38], [207.35, 551.58], [-294.03, 684.38], [65.60, 551.58]];
const piece = (id, w, h, d, pose, extra) => Object.assign({ id, type: null, w, h, d, placement: pose ? { mode: "3d", pose, box: null } : { mode: "none", pose: null, box: null } }, extra);
const A = () => piece("a", 180, 220, 60, { x: -20, z: 370, yaw: 0, wall: null });
const B = () => piece("b", 120, 80, 45, { x: -170, z: 250, yaw: 90, wall: null });

const near = (a, b, tol = 0.5) => assert.ok(Math.abs(a - b) <= tol, `${a} vs ${b}`);
const nearAll = (a, b, tol = 0.5) => { a.flat().forEach((v, i) => near(v, b.flat()[i], tol)); };
const boxNear = (a, b) => a.forEach((v, i) => assert.ok(Math.abs(v - b[i]) <= 1, `${a} vs ${b}`));

test("init", () => {
  assert.equal(W3D.version, 1);
  assert.equal(W3D.init(null), null);
  assert.equal(W3D.init({ ...fix(), ok: false }), null);
  const s = W3D.init(fix());
  assert.deepEqual(s.size, [1344, 1008]);
  for (const k of ["origin", "right", "up", "forward"]) nearAll(s.frame[k], FIX.frame[k], 1e-3);
});

test("project boxes A and B", () => {
  const s = W3D.init(fix());
  const a = s.projectBox(A());
  nearAll(a.corners, A_CORNERS); boxNear(a.box, [106, 246, 744, 662]); assert.equal(a.visible, true);
  const b = s.projectBox(B());
  nearAll(b.corners, B_CORNERS); boxNear(b.box, [547, 0, 1000, 169]); assert.equal(b.visible, true);
  const behind = s.projectBox(piece("c", 180, 220, 60, { x: -20, z: -100, yaw: 0 }));
  assert.equal(behind.visible, false);
  assert.ok(behind.corners.every(c => c === null));
});

test("floor point, toImage", () => {
  const s = W3D.init(fix());
  const p = s.floorPoint(672, 900); near(p.x, 0); near(p.z, 243.79);
  const q = s.floorPoint(300, 800); near(q.x, -119.96); near(q.z, 301.0);
  assert.equal(s.floorPoint(672, 300), null);
  const px = s.toImage([q.x, 0, q.z]); near(px[0], 300); near(px[1], 800);
  assert.equal(s.toImage([0, 0, -500]), null);
});

test("snapToWall and coverage", () => {
  const s = W3D.init(fix()), pc = piece("x", 100, 100, 60, null);
  const a = s.snapToWall({ x: -20, z: 300, yaw: 0 }, pc, "w0");
  near(a.x, -20); near(a.z, 370); near(a.yaw, 0); assert.equal(a.wall, "w0");
  const b = s.snapToWall({ x: -20, z: 300, yaw: 0 }, pc, "w1");
  near(b.x, -170); near(b.z, 300); near(b.yaw, 90); assert.equal(b.wall, "w1");
  assert.equal(s.snapToWall({ x: -20, z: 300, yaw: 0 }, pc), null);
  assert.equal(s.snapToWall({ x: -20, z: 120, yaw: 0 }, pc), null);
  assert.equal(s.snapToWall({ x: -175, z: 30, yaw: 0 }, pc), null);
  assert.equal(s.snapToWall({ x: -175, z: 200, yaw: 0 }, pc).wall, "w1");
  const nw = s.nearestWall(-175, 30);
  assert.ok(!nw || nw.wall.id !== "w1");
});

test("place near a wall snaps, open floor does not", () => {
  // a tap marks the middle of the box in the photo (H/2 above its footprint centre), not the floor contact
  const s = W3D.init(fix()), pc = piece("x", 180, 220, 60, null);
  const px = s.toImage([-20, 110, 395]);
  const p = s.place(px[0], px[1], pc);
  near(p.z, 370); assert.equal(p.wall, "w0"); near(p.yaw, 0);
  const base = s.toImage([-20, 110, 400]);  // tap on the middle of the wall right behind
  assert.equal(s.place(base[0], base[1], pc).wall, "w0");
  const open = s.toImage([0, 110, 250]);
  const o = s.place(open[0], open[1], pc);
  assert.equal(o.wall, null); near(o.z, 250, 1); assert.equal(o.yaw, 0);
  assert.equal(s.place(672, 20, pc), null);  // far above anything a standing piece can reach
});

test("measure", () => {
  const s = W3D.init(fix());
  const p1 = s.toImage([0, 0, 243.79]), p2 = s.toImage([-119.96, 0, 301.0]);
  near(s.measure(p1, p2, "floor"), 132.9);
  const bot = s.toImage([-110, 0, 340]), top = s.toImage([-110, 220, 340]);
  near(s.measure(bot, top, "vertical"), 220);
  near(s.measure(top, bot, "vertical"), s.measure(bot, top, "vertical"), 1e-9);
});

test("relations", () => {
  const s = W3D.init(fix());
  const r = s.relations(A());
  near(r.ceilingRatio, 0.846, 1e-3); near(r.ceilingHeight, 260);
  near(r.wallRatio, 0.4, 1e-6); near(r.wallLength, 450); assert.equal(r.wallLabel, "hinten");
  near(r.distance, 370.5, 0.1);
  assert.equal(r.tooHigh, false); assert.equal(r.tooWide, false); assert.equal(r.outOfImage, false);
  const big = s.relations(piece("w", 500, 300, 60, { x: 25, z: 370, yaw: 0, wall: "w0" }));
  assert.equal(big.tooHigh, true); assert.equal(big.tooWide, true);
  assert.equal(s.relations(piece("z", 100, 100, 60, { x: 0, z: -300, yaw: 0 })).outOfImage, true);
});

test("dims fall back to the catalog", () => {
  const s = W3D.init(fix(), { pieces: { schrank: { dims: [200, 240, 60] } } });
  assert.deepEqual(s.dims({ type: "schrank", w: null, h: 250, d: "" }), [200, 250, 60]);
  assert.deepEqual(s.dims({ type: null }), [100, 100, 60]);
});

test("toGrid and boxToGrid", () => {
  const g = W3D.toGrid([672.4, 504.6], [1344, 1008]);
  assert.ok(g.every(Number.isInteger)); assert.deepEqual(g, [501, 500]);
  assert.deepEqual(W3D.boxToGrid([[331.1, 107.39], [888.93, 107.39], [865.97, 749.73]], [1344, 1008]), [106, 246, 744, 662]);
  assert.deepEqual(W3D.boxToGrid([[-50, -10], [100, 2000]], [1344, 1008]), [0, 0, 1000, 75]);
  assert.equal(W3D.boxToGrid([], [1344, 1008]), null);
});

test("drag offset keeps the grab point under the finger (above the horizon)", () => {
  const s = W3D.init(fix()), pose = A().placement.pose;
  const bc = s.bottomCentre(pose), grab = [610, 200];
  const off = [grab[0] - bc[0], grab[1] - bc[1]];
  const fp = s.dragTarget(off, [grab[0] + 50, grab[1] + 20]);
  assert.ok(fp);
  const nb = s.toImage([fp.x, 0, fp.z]);
  near(nb[0] - bc[0], 50); near(nb[1] - bc[1], 20);
  assert.equal(s.floorPoint(grab[0], grab[1]), null);  // the raw pointer is above the horizon
});

test("scalePoses and calibration rescale", () => {
  const pieces = [A(), piece("p", 1, 1, 1, null, { placement: { mode: "2d", pose: null, box: [1, 2, 3, 4] } })];
  W3D.scalePoses(pieces, 1.25);
  near(pieces[0].placement.pose.x, -25); near(pieces[0].placement.pose.z, 462.5);
  assert.deepEqual(pieces[1].placement.box, [1, 2, 3, 4]);
  const g = fix(); g.floor.d *= 1.25;
  const s = W3D.init(g);
  const scaled = Object.assign(pieces[0], { w: 225, h: 275, d: 75 });
  nearAll(s.projectBox(scaled).corners, A_CORNERS);
  const same = s.projectBox(Object.assign({}, scaled, { w: 180, h: 220, d: 60 })).box;
  assert.ok(same[2] - same[0] < 744 - 106);
});

test("snapYaw", () => {
  const s = W3D.init(fix());
  assert.deepEqual(s.snapYaw(7), { yaw: 0, wall: "w0" });
  assert.equal(s.snapYaw(95).yaw, 90);
  assert.equal(s.snapYaw(45).wall, null); near(s.snapYaw(45).yaw, 45);
});

// ---------------- controller with a fake canvas ----------------

function fakeCanvas() {
  const handlers = {};
  let count = 0;
  const ctx = new Proxy({}, {
    get: (t, k) => (k in t ? t[k] : k === "measureText" ? () => ({ width: 40 }) : () => {}),
    set: (t, k, v) => { t[k] = v; return true; },
  });
  return {
    clientWidth: 672, clientHeight: 504, width: 0, height: 0, style: {},
    getContext: () => ctx,
    addEventListener(t, f) { count++; (handlers[t] = handlers[t] || []).push(f); },
    removeEventListener(t, f) { const l = handlers[t] || []; const i = l.indexOf(f); if (i >= 0) { l.splice(i, 1); count--; } },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 672, height: 504 }),
    setPointerCapture() {}, releasePointerCapture() {},
    fire(t, e) { for (const f of handlers[t] || []) f(Object.assign({ pointerId: 1, button: 0, preventDefault() {} }, e)); },
    get listeners() { return count; },
  };
}

test("attach: place, drag, measure, destroy", () => {
  const s = W3D.init(fix());
  const cv = fakeCanvas();
  const pieces = [A(), piece("c", 120, 80, 45, null)];
  let active = "c";
  const changes = [], selects = [], measures = [];
  const ctl = W3D.attach(cv, {
    scene: s, imgSize: [1344, 1008], getPieces: () => pieces, getActiveId: () => active,
    onChange: (id, pose, o) => { changes.push([id, pose, o.final]); const p = pieces.find(q => q.id === id); p.placement = { mode: "3d", pose, box: null }; },
    onSelect: id => { selects.push(id); active = id; }, onMeasure: (a, b) => measures.push([a, b]), mode: "place",
  });
  assert.equal(cv.listeners, 4);
  assert.equal(cv.style.pointerEvents, "auto"); assert.equal(cv.style.touchAction, "none");
  assert.equal(cv.width, 672);  // devicePixelRatio 1 in node

  // tap on empty space places the active piece with its middle there (H/2 = 40 cm up); client px = image px / 2
  const fl = s.toImage([100, 40, 250]);
  cv.fire("pointerdown", { clientX: fl[0] / 2, clientY: fl[1] / 2 });
  assert.equal(changes.length, 1); assert.equal(changes[0][0], "c"); assert.equal(changes[0][2], true);
  near(changes[0][1].x, 100, 1); near(changes[0][1].z, 250, 1);

  // grab piece A high up (above the horizon) and drag
  cv.fire("pointerdown", { clientX: 305, clientY: 100 });
  assert.deepEqual(selects, ["a"]);
  cv.fire("pointermove", { clientX: 330, clientY: 110 });
  cv.fire("pointerup", { clientX: 330, clientY: 110 });
  const last = changes[changes.length - 1];
  assert.equal(last[0], "a"); assert.equal(last[2], true); assert.ok(last[1]);

  // a tap above the horizon on empty space does nothing
  const n = changes.length;
  cv.fire("pointerdown", { clientX: 30, clientY: 20 });
  assert.equal(changes.length, n);

  // measure mode
  ctl.setMode("measure");
  cv.fire("pointerdown", { clientX: 100, clientY: 450 });
  cv.fire("pointermove", { clientX: 200, clientY: 460 });
  cv.fire("pointerup", { clientX: 200, clientY: 460 });
  assert.equal(measures.length, 1);
  near(measures[0][0][0], 200); near(measures[0][1][1], 920);

  ctl.setMode("off");
  assert.equal(cv.style.pointerEvents, "none"); assert.equal(cv.style.touchAction, "auto");
  ctl.setShowRoom(true); ctl.redraw(); ctl.resize();
  ctl.destroy();
  assert.equal(cv.listeners, 0);
});

test("drawOverlay draws without a DOM", () => {
  const s = W3D.init(fix()), calls = [];
  const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : k === "measureText" ? () => ({ width: 40 }) : (...a) => calls.push(k)), set: (t, k, v) => { t[k] = v; return true; } });
  W3D.drawOverlay(ctx, s, [A(), B(), piece("n", 1, 1, 1, null)], { pxScale: 0.5, activeId: "a", showRoom: true, measureLine: [[100, 900], [600, 950]], touch: true });
  assert.ok(calls.filter(c => c === "stroke").length >= 24);
  assert.ok(calls.includes("fillText") && calls.includes("arc"));
});

test("poses at the camera are not visible, drag stays on the photographed floor", () => {
  const s = W3D.init(fix());
  for (const z of [20, 40, 60]) assert.equal(s.projectBox(piece("w", 100, 210, 60, { x: 0, z, yaw: 0 })).visible, false);
  const fp = s.dragTarget([0, -700], [672, 1000]);  // tall piece grabbed at the top, pointer at the bottom edge
  const bottom = s.floorPoint(672, 1008);
  near(fp.z, bottom.z, 0.5);
});

// ---------------- v3 additions: turn, label clamping, touch handle, onBegin/onEnd ----------------

test("turn: yaw + deg, snaps only to a wall with the matching angle within 30 cm", () => {
  const s = W3D.init(fix()), pc = piece("x", 120, 80, 60, null);
  const free = s.turn({ x: -20, z: 370, yaw: 0, wall: "w0" }, pc, 90);
  assert.deepEqual(free, { x: -20, z: 370, yaw: 90, wall: null });
  const snapped = s.turn({ x: -160, z: 300, yaw: 0, wall: null }, pc, 90);
  near(snapped.x, -170); near(snapped.z, 300); near(snapped.yaw, 90); assert.equal(snapped.wall, "w1");
  const back = s.turn({ x: -20, z: 368, yaw: 90, wall: null }, pc, -90);
  near(back.z, 370); assert.equal(back.wall, "w0");
  assert.equal(s.turn({ x: 0, z: 200, yaw: 170, wall: null }, pc, 90).yaw, -100);
  assert.equal(s.turn({ x: 0, z: 200, yaw: 0, wall: null }, pc, 180).yaw, -180);
});

function recCtx() {
  const rects = [], texts = [], arcs = [];
  const ctx = new Proxy({}, {
    get: (t, k) => (k in t ? t[k] : k === "measureText" ? s => ({ width: 7 * String(s).length })
      : k === "fillRect" ? (...a) => rects.push(a) : k === "fillText" ? (...a) => texts.push(a)
      : k === "arc" ? (...a) => arcs.push(a) : () => {}),
    set: (t, k, v) => { t[k] = v; return true; },
  });
  return { ctx, rects, texts, arcs };
}

test("labels are clamped inside the canvas, short text when the long one does not fit", () => {
  const s = W3D.init(fix());
  // piece at the far right: its label would start beyond the right edge
  const edge = piece("e", 120, 200, 45, { x: 180, z: 330, yaw: 0, wall: null });
  const r = recCtx(), k = 0.25, view = [1344 * k, 1008 * k];
  W3D.drawOverlay(r.ctx, s, [edge, A()], { pxScale: k, activeId: "e", viewSize: view, touch: true });
  assert.ok(r.rects.length >= 2);
  for (const [x, y, w, h] of r.rects) {
    assert.ok(x >= 0 && x + w <= view[0] + 1e-6, `x ${x}+${w} outside ${view[0]}`);
    assert.ok(y >= 0 && y + h <= view[1] + 1e-6, `y ${y}+${h} outside ${view[1]}`);
  }
  const tiny = recCtx();
  W3D.drawOverlay(tiny.ctx, s, [A()], { pxScale: 0.1, activeId: "a", viewSize: [134, 101], touch: true });
  const dims = tiny.texts.map(t => t[0]).filter(t => /\d/.test(t));
  assert.ok(dims.includes("180×220×60"), dims.join("|"));
  const wide = recCtx();
  W3D.drawOverlay(wide.ctx, s, [A()], { pxScale: 0.5, activeId: "a", viewSize: [672, 504] });
  assert.ok(wide.texts.some(t => t[0] === "B 180 × H 220 × T 60"));
});

test("touch rotate handle: radius 22 px, ↻ glyph", () => {
  const s = W3D.init(fix()), r = recCtx();
  W3D.drawOverlay(r.ctx, s, [A()], { pxScale: 0.5, activeId: "a", touch: true });
  assert.ok(r.arcs.some(a => a[2] === 22));
  assert.ok(r.texts.some(t => t[0] === "↻"));
  const m = recCtx();
  W3D.drawOverlay(m.ctx, s, [A()], { pxScale: 0.5, activeId: "a" });
  assert.ok(m.arcs.some(a => a[2] === 9));
  assert.equal(W3D.HANDLE_R.touch, 22);
});

test("attach: onBegin/onEnd around move, rotate (touch hit radius +8) and place; abort on mode change", () => {
  const s = W3D.init(fix()), cv = fakeCanvas();
  const pieces = [A(), piece("c", 120, 80, 45, null)];
  let active = "a";
  const log = [];
  const ctl = W3D.attach(cv, {
    scene: s, imgSize: [1344, 1008], touch: true, getPieces: () => pieces, getActiveId: () => active, mode: "place",
    onChange: (id, pose, o) => { log.push(["change", id, o.final]); pieces.find(q => q.id === id).placement = { mode: "3d", pose, box: null }; },
    onSelect: id => { active = id; },
    onBegin: (id, kind) => log.push(["begin", id, kind]),
    onEnd: (id, kind, o) => log.push(["end", id, kind, o.moved]),
  });
  // rotate: grab 28 css px away from the handle centre (22 + 8 = 30 hit radius)
  const h = s.handlePoint(pieces[0], null, W3D.HANDLE_GAP.touch / 0.5);  // below the footprint (pxScale 0.5)
  const foot = s.projectBox(pieces[0]).corners.slice(0, 4).filter(Boolean);
  assert.ok(h[1] >= Math.max(...foot.map(p => p[1])) + 79, "rotate handle sits ~40 css px below the footprint, not on the box front");
  cv.fire("pointerdown", { clientX: h[0] / 2 + 28, clientY: h[1] / 2 });
  assert.deepEqual(log.at(-1), ["begin", "a", "rotate"]);
  cv.fire("pointermove", { clientX: h[0] / 2 + 120, clientY: h[1] / 2 + 40 });
  cv.fire("pointerup", { clientX: h[0] / 2 + 120, clientY: h[1] / 2 + 40 });
  assert.deepEqual(log.at(-1), ["end", "a", "rotate", true]);
  assert.deepEqual(log.at(-2), ["change", "a", true]);
  // a press on the piece without moving: begin + end(moved false), no change
  log.length = 0;
  cv.fire("pointerdown", { clientX: 305, clientY: 100 });
  cv.fire("pointerup", { clientX: 305, clientY: 100 });
  assert.deepEqual(log, [["begin", "a", "move"], ["end", "a", "move", false]]);
  // tap-place of the unplaced piece
  log.length = 0; active = "c";
  const fl = s.toImage([100, 0, 250]);
  cv.fire("pointerdown", { clientX: fl[0] / 2, clientY: fl[1] / 2 });
  assert.deepEqual(log.map(l => l[0]), ["begin", "change", "end"]);
  assert.equal(log[0][2], "place");
  // a drag cut short by a mode change still ends
  log.length = 0; active = "a";
  cv.fire("pointerdown", { clientX: 305, clientY: 100 });
  assert.equal(ctl.busy, true);
  ctl.setMode("off");
  assert.deepEqual(log.at(-1), ["end", "a", "move", false]);
  assert.equal(ctl.busy, false);
  ctl.destroy();
});
