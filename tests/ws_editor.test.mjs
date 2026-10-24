// Unit tests for the guided editor (static/ws/editor.js, step-raum/-moebel/-platzieren.js): gating, default step,
// undo stack, warning mapping, PATCH payloads. No DOM: the modules only touch the DOM inside mount().
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const W3D = require("../static/werkstatt3d.js");   // sets globalThis.W3D like the classic script tag
const ed = await import("../static/ws/editor.js");
const raum = await import("../static/ws/step-raum.js");
const moebel = await import("../static/ws/step-moebel.js");
const place = await import("../static/ws/step-platzieren.js");

const GEO = JSON.parse(readFileSync(new URL("./fixtures/geometry_sample.json", import.meta.url)));
const geo = (extra = {}) => ({ ...JSON.parse(JSON.stringify(GEO)), room: "uploads/r.png", ...extra });
const CAT = { pieces: { sideboard: { de: "Sideboard", dims: [180, 75, 45] }, schrank: { de: "Einbauschrank", dims: [200, 240, 60] },
  tisch: { de: "Esstisch", dims: [180, 76, 90] } }, materials: { eiche: { de: "Eiche natur geölt" } }, handles: { push: { de: "grifflos (Push)" } } };
const NONE = { mode: "none", pose: null, box: null };
const pc = (id, extra = {}) => ({ id, type: "sideboard", text: "", material: "eiche", handles: "push", w: 180, h: 75, d: 45, placement: { ...NONE }, ...extra });
const p3 = (id, pose, extra = {}) => pc(id, { placement: { mode: "3d", pose, box: [1, 2, 3, 4] }, ...extra });
const docOf = (extra = {}) => ({ id: "pid1", room: "uploads/r.png", room_size: [1344, 1008], geometry: geo(), pieces: [pc("a1b2")], variants: [], removes: [], ...extra });
const near = (a, b, tol = 0.5) => assert.ok(Math.abs(a - b) <= tol, `${a} vs ${b}`);

// ---------------- editor: default step, fallback states, boxes ----------------

test("defaultStep follows the first unfinished step", () => {
  const d = ed.defaultStep;
  assert.equal(d({ room: null, pieces: [pc("a")], variants: [] }, {}), "raum");
  assert.equal(d({ room: null, pieces: [pc("a")], variants: [] }, { noRoom: true }), "berechnen");   // free: no placing
  assert.equal(d({ room: null, setting: "Altbau-Flur", pieces: [pc("a", { type: null })] }, {}), "moebel");
  assert.equal(d(docOf({ pieces: [pc("a", { type: null, text: "  " })] }), {}), "moebel");
  assert.equal(d(docOf({ pieces: [pc("a", { type: null, text: "Regal mit Schiebetür" })] }), {}), "platzieren");
  const placedDoc = docOf({ pieces: [p3("a", { x: 0, z: 300, yaw: 0, wall: null })] });
  assert.equal(d(placedDoc, {}), "berechnen");
  assert.equal(d(placedDoc, {}, { jobs: [{ state: "running" }] }), "ergebnis");
  assert.equal(d({ ...placedDoc, variants: [{ id: "v1" }] }, {}), "ergebnis");
  assert.equal(d({ ...placedDoc, variants: [{ id: "v1" }], deleted_variants: ["v1"] }, {}), "berechnen");
  // a remembered step younger than 24 h wins, an old one or an unavailable one does not
  const now = Date.now();
  assert.equal(d(placedDoc, { step: "moebel", stepAt: now - 3600e3 }, { now }), "moebel");
  assert.equal(d(placedDoc, { step: "moebel", stepAt: now - 25 * 3600e3 }, { now }), "berechnen");
  assert.equal(d(placedDoc, { step: "ergebnis", stepAt: now }, { now, available: id => id !== "ergebnis" }), "berechnen");
  assert.equal(d(placedDoc, { step: "bogus", stepAt: now }, { now }), "berechnen");
});

test("fallback state for steps without a module: Ergebnis needs variants or a running job", () => {
  assert.equal(ed.fallbackState("ergebnis", docOf(), []).available, false);
  assert.ok(ed.fallbackState("ergebnis", docOf(), []).reason);
  assert.equal(ed.fallbackState("ergebnis", docOf(), [{ state: "queued" }]).available, true);
  assert.equal(ed.fallbackState("ergebnis", docOf({ variants: [{ id: "x" }] }), []).available, true);
  assert.equal(ed.fallbackState("berechnen", docOf(), []).available, true);
});

test("emptyPiece / noRoom / placed helpers", () => {
  const e = ed.emptyPiece({ material: "nuss", handles: null }, "q7c2xw");
  assert.deepEqual(e, { id: "q7c2xw", type: null, material: "nuss", handles: null, placement: NONE });
  assert.deepEqual(ed.emptyPiece(null, "k3m9qa"), { id: "k3m9qa", type: null, material: "eiche", handles: "push", placement: NONE });
  assert.match(ed.rid(6), /^[a-z0-9]{6}$/);
  assert.equal(ed.noRoom({ room: null }, { noRoom: true }), true);
  assert.equal(ed.noRoom({ room: "uploads/x.png" }, { noRoom: true }), false);
  assert.equal(ed.noRoom({ room: null, variants: [{ id: "v" }] }, {}), true);
  assert.equal(ed.placed(p3("a", { x: 0, z: 1, yaw: 0 })), true);
  assert.equal(ed.placed(pc("a", { placement: { mode: "3d", pose: { x: 0, z: 1 }, box: null } })), false);
});

test("syncBoxOps: a changed width re-projects the box, a wall piece re-snaps its depth", () => {
  const scene = W3D.init(geo(), CAT);
  const onWall = scene.snapToWall({ x: -20, z: 300, yaw: 0 }, pc("w"), "w0");
  const pl = ed.reprojected(scene, pc("w", { placement: { mode: "3d", pose: onWall, box: null } }));
  const doc = docOf({ pieces: [pc("w", { placement: pl })] });
  assert.deepEqual(ed.syncBoxOps(doc, scene), []);
  doc.pieces[0].w = 240; doc.pieces[0].d = 60;     // deeper: the back must stay at the wall
  const ops = ed.syncBoxOps(doc, scene);
  assert.equal(ops.length, 1); assert.equal(ops[0].path, "pieces.w.placement");
  near(ops[0].value.pose.z, 370); assert.equal(ops[0].value.pose.wall, "w0");
  assert.ok(ops[0].value.box[3] - ops[0].value.box[1] > pl.box[3] - pl.box[1]);
  assert.deepEqual(ed.syncBoxOps(doc, null), []);
});

// ---------------- ① Raum ----------------

test("Raum gate: disabled until the analysis ends (success or failure) or no-room is chosen", () => {
  const g = raum.raumGate, noroom = { room: null, pieces: [] };
  assert.deepEqual(g(noroom, {}, null), { disabled: true, reason: raum.T.noPhoto });
  assert.deepEqual(g(noroom, { noRoom: true }, null), { disabled: false, reason: null });
  assert.deepEqual(g(noroom, {}, { phase: "upload" }), { disabled: true, reason: raum.T.uploading });
  const fresh = docOf({ geometry: null });
  assert.deepEqual(g(fresh, {}, null), { disabled: true, reason: raum.T.measuring });
  assert.deepEqual(g(fresh, {}, { room: fresh.room, phase: "analyzing" }), { disabled: true, reason: raum.T.measuring });
  assert.equal(g(fresh, {}, { room: fresh.room, phase: "failed", busy: true }).disabled, false);   // failure allows 2D frames
  assert.equal(g(docOf(), {}, null).disabled, false);
  assert.equal(g(docOf({ geometry: { ok: false, error: "kein Boden" } }), {}, null).disabled, false);
  assert.equal(g(docOf(), {}, { room: "uploads/r.png", phase: "analyzing" }).disabled, true);   // re-analysis running
});

test("Raum state uses the per-project analysis state", () => {
  const d = docOf({ id: "pidA", geometry: null });
  assert.equal(raum.default.state(d, {}).done, false);
  raum._setAnalysis("pidA", { room: d.room, phase: "failed", error: "x" });
  assert.equal(raum.default.state(d, {}).done, true);
  assert.equal(raum.analysisOf({ ...d, room: "uploads/other.png" }), null);  // other photo: stale state ignored
  raum._setAnalysis("pidA", null);
  assert.equal(raum.default.state(d, {}).available, true);
});

test("new room photo: one PATCH resets analysis, calibration, removes and every placement", () => {
  const d = docOf({ pieces: [p3("a", { x: 0, z: 300, yaw: 0 }), pc("b")], removes: [[1, 2, 3, 4]] });
  assert.deepEqual(raum.newRoomOps(d, { path: "uploads/new.png", width: 1920, height: 2560 }), [
    { op: "set", path: "room", value: "uploads/new.png" }, { op: "set", path: "room_size", value: [1920, 2560] },
    { op: "set", path: "geometry", value: null }, { op: "set", path: "calibration", value: null },
    { op: "set", path: "removes", value: [] },
    { op: "set", path: "pieces.a.placement", value: NONE }, { op: "set", path: "pieces.b.placement", value: NONE }]);
});

test("analysis ops: ok → geometry + reprojected 3d boxes; failed → 3d pieces become frames", () => {
  const d = docOf({ geometry: null, pieces: [p3("a", { x: -20, z: 370, yaw: 0, wall: null }, { w: 180, h: 220, d: 60 }), pc("b")] });
  const ok = raum.analysisOps(d, geo(), CAT);
  assert.equal(ok[0].path, "geometry"); assert.equal(ok[0].value.ok, true);
  assert.equal(ok.length, 2); assert.deepEqual(ok[1].value.box.map(Math.round), [106, 246, 744, 662]);
  const bad = raum.analysisOps(d, { ok: false, error: "kein Boden" }, CAT);
  assert.deepEqual(bad[1], { op: "set", path: "pieces.a.placement", value: { mode: "2d", pose: { x: -20, z: 370, yaw: 0, wall: null }, box: [1, 2, 3, 4] } });
  const nobox = raum.analysisOps({ ...d, pieces: [pc("c", { placement: { mode: "3d", pose: { x: 0, z: 1, yaw: 0 }, box: null } })] }, { ok: false }, CAT);
  assert.deepEqual(nobox[1].value, NONE);
});

test("calibration adoption: poses scale, wall pieces re-snap, one PATCH with geometry + calibration + placements", () => {
  const g0 = geo(), scene0 = W3D.init(g0, CAT);
  const wallPose = scene0.snapToWall({ x: -20, z: 300, yaw: 0 }, pc("a"), "w0");
  const a = pc("a", { placement: ed.reprojected(scene0, pc("a", { placement: { mode: "3d", pose: wallPose, box: null } })) });
  const b = pc("b", { placement: { mode: "2d", pose: null, box: [5, 5, 50, 50] } });
  const d = docOf({ geometry: g0, pieces: [a, b] });
  const g1 = geo(); g1.floor.d *= 1.25; g1.scale = 1.25;
  g1.walls = g1.walls.map(w => ({ ...w, offset: w.offset * 1.25, from: w.from && w.from.map(x => x * 1.25), to: w.to && w.to.map(x => x * 1.25) }));
  const cal = { p1: [1, 2], p2: [3, 4], length_cm: 90, mode: "floor", measured_cm: 72, scale: 1.25, room_height_cm: null };
  assert.equal(raum.measuredText({ scale: 1, refs: [] }), null);
  assert.equal(raum.measuredText({ scale: 1.1, refs: [{ kind: "height", cm: 276 }, { kind: "line", cm: 137, label: "Wand" }] }),
    "Maße nachgemessen (Decke 2,76 m · Wand 137 cm)");
  const q = raum.conflictQuestion({ kind: "height", cm: 276, implied_cm: 375, ceiling_cm: 375, new: { kind: "line", cm: 180, label: "Wand" } });
  assert.equal(q.text, "Dann wäre die Decke 3,75 m hoch – stimmt das?");
  assert.equal(q.yes, "Ja, Decke 3,75 m"); assert.equal(q.no, "Nein, Decke 2,76 m stimmt");
  assert.equal(raum.conflictQuestion({ kind: "line", label: "Wand", cm: 137, implied_cm: 180, new: { kind: "height", cm: 360 } }).text,
    "Dann wäre die Wand 180 cm lang statt 137 cm – stimmt das?");
  const ops = raum.calibrationOps(d, g1, 1, cal, CAT);
  assert.deepEqual(ops.slice(0, 2), [{ op: "set", path: "geometry", value: g1 }, { op: "set", path: "calibration", value: cal }]);
  assert.equal(ops.length, 3);   // the 2d frame is untouched
  const pose = ops[2].value.pose;
  near(pose.x, -25); assert.equal(pose.wall, "w0");
  near(pose.z, 500 - 22.5);       // back wall moved to z = 500, the 45 cm deep piece stands right in front of it
  assert.ok(ops[2].value.box);
  assert.equal(raum.steepMode([0, 0], [10, 100]), "vertical"); assert.equal(raum.steepMode([0, 0], [100, 30]), "floor");
});

// ---------------- ② Möbel ----------------

test("type choice fills real dimensions, replaces untouched defaults, keeps typed ones", () => {
  const empty = pc("a", { type: null, w: null, h: null, d: null });
  assert.deepEqual(moebel.typeOps(empty, "schrank", CAT), [
    { op: "set", path: "pieces.a.type", value: "schrank" }, { op: "set", path: "pieces.a.w", value: 200 },
    { op: "set", path: "pieces.a.h", value: 240 }, { op: "set", path: "pieces.a.d", value: 60 }]);
  const defaults = pc("a", { type: "schrank", w: 200, h: 240, d: 60 });
  assert.deepEqual(moebel.typeOps(defaults, "tisch", CAT).map(o => o.value), ["tisch", 180, 76, 90]);
  const typed = pc("a", { type: "schrank", w: 245, h: 240, d: 62 });
  assert.deepEqual(moebel.typeOps(typed, "tisch", CAT).map(o => o.path), ["pieces.a.type", "pieces.a.h"]);
  assert.match(moebel.typeIcon("schrank"), /^<svg/);
  assert.equal(Object.keys(moebel.TYPE_ICONS).length, 14);
});

test("delete: last piece gets a client replacement, undo re-adds the removed object", () => {
  const one = docOf({ pieces: [pc("a", { text: "mit Nische" })] });
  const r = moebel.deleteOps(one, "a", "q7c2xw");
  assert.deepEqual(r.ops, [{ op: "remove_piece", id: "a", replacement: { id: "q7c2xw", type: null, material: "eiche", handles: "push", placement: NONE } }]);
  const after = { ...one, pieces: [r.replacement] };
  assert.deepEqual(r.undo(after), [{ op: "add_piece", value: one.pieces[0] }, { op: "remove_piece", id: "q7c2xw" }]);
  const edited = { ...one, pieces: [{ ...r.replacement, type: "tisch" }] };   // replacement already used: keep it
  assert.deepEqual(r.undo(edited), [{ op: "add_piece", value: one.pieces[0] }]);
  const two = docOf({ pieces: [pc("a"), pc("b")] });
  const r2 = moebel.deleteOps(two, "b");
  assert.deepEqual(r2.ops, [{ op: "remove_piece", id: "b" }]);
  assert.equal(r2.replacement, null);
  assert.equal(moebel.deleteOps(two, "zz"), null);
});

test("piece warnings and auto render", () => {
  const scene = W3D.init(geo(), CAT);
  const big = p3("w", { x: 25, z: 370, yaw: 0, wall: "w0" }, { w: 500, h: 300, d: 60 });
  assert.deepEqual(moebel.pieceWarnings(big, docOf(), scene), ["Höher als die Decke (260 cm)!", "Breiter als die Wand (450 cm)!"]);
  assert.deepEqual(moebel.pieceWarnings(pc("x", { h: 270 }), docOf(), null), ["Höher als die Decke (260 cm)!"]);
  assert.deepEqual(moebel.pieceWarnings(pc("x", { h: 200 }), docOf(), null), []);
  const d = docOf({ pieces: [pc("a", { sketch: "uploads/s.png", render: null })], variants: [
    { id: "r1", kind: "render", piece_ids: ["a"], image: "r1.png", judge: { status: "ok", overall: 6 } },
    { id: "r2", kind: "render", piece_ids: ["a"], image: "r2.png", judge: { status: "ok", overall: 8 } }] });
  assert.deepEqual(moebel.autoRenderOps(d), [{ op: "set", path: "pieces.a.render", value: "r2.png" }]);
  d.variants.push({ id: "r3", kind: "render", piece_ids: ["a"], image: "r3.png", judge: { status: "pending" } });
  assert.deepEqual(moebel.autoRenderOps(d), []);
  assert.equal(moebel.default.state(docOf({ pieces: [pc("a", { type: null })] })).done, false);
  assert.equal(moebel.default.state(docOf()).done, true);
});

// ---------------- ③ Platzieren ----------------

test("Platzieren is unavailable without a room photo", () => {
  const st = place.default.state({ room: null, pieces: [pc("a")] });
  assert.deepEqual(st, { done: false, available: false, reason: "Nur mit Raumfoto – in Schritt 1 ein Foto aufnehmen." });
  assert.equal(place.default.state(docOf()).done, false);
  assert.equal(place.default.state(docOf({ pieces: [p3("a", { x: 0, z: 300, yaw: 0 })] })).done, true);
});

test("warning mapping on the photo", () => {
  const scene = W3D.init(geo(), CAT);
  assert.deepEqual(place.warningsFor(p3("w", { x: 25, z: 370, yaw: 0, wall: "w0" }, { w: 500, h: 300, d: 60 }), scene, docOf()), ["high", "wide"]);
  assert.deepEqual(place.warningsFor(p3("z", { x: 0, z: -300, yaw: 0 }, { w: 100, h: 100, d: 60 }), scene, docOf()), ["out"]);
  assert.deepEqual(place.warningsFor(p3("ok", { x: 0, z: 300, yaw: 0, wall: null }), scene, docOf()), []);
  assert.deepEqual(place.warningsFor(pc("f", { h: 280, placement: { mode: "2d", pose: null, box: [1, 1, 50, 50] } }), null, docOf()), ["high"]);
  assert.deepEqual(Object.values(place.WARN), ["⚠ ragt aus dem Bild", "⚠ höher als die Decke", "⚠ breiter als die Wand"]);
  assert.equal(place.unplacedText(docOf({ pieces: [pc("a"), pc("b"), p3("c", { x: 0, z: 300, yaw: 0 })] })),
    "2 Möbel noch nicht platziert – es wird an einer passenden Stelle ergänzt.");
  assert.equal(place.unplacedText(docOf({ pieces: [p3("c", { x: 0, z: 300, yaw: 0 })] })), null);
});

test("undo stack keeps the last 20 states", () => {
  const u = new place.UndoStack(20);
  for (let i = 0; i < 25; i++) u.push({ path: "pieces.a.placement", value: i });
  assert.equal(u.size, 20);
  assert.equal(u.pop().value, 24);
  u.discard();
  assert.equal(u.pop().value, 22);
  const e = new place.UndoStack();
  assert.equal(e.pop(), null);
});

test("arrow keys: wall pieces slide along the wall and leave it when moved away", () => {
  const scene = W3D.init(geo(), CAT), piece = pc("a", { w: 100, h: 100, d: 60 });
  const onWall = scene.snapToWall({ x: -20, z: 300, yaw: 0 }, piece, "w0");
  const slid = place.nudgePose(scene, piece, onWall, 5, 0);
  near(slid.x, -15); near(slid.z, onWall.z); assert.equal(slid.wall, "w0");
  const away = place.nudgePose(scene, piece, onWall, 0, -25);
  near(away.z, onWall.z - 25); assert.equal(away.wall, null);
  assert.equal(place.nudgePose(scene, piece, { x: 0, z: 60, yaw: 0, wall: null }, 0, -25), null);   // at the camera: refused
  assert.deepEqual(place.nudgeBox([100, 100, 200, 300], -150, 800), [0, 800, 100, 1000]);
});
