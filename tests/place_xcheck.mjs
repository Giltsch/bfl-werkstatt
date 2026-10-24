// Cross-check harness for place.py (run by tests/test_place.py): computes the same things with the real
// static/werkstatt3d.js and static/ws/editor.js. A tap goes through W3D.attach on a fake canvas exactly like a finger in
// step ③, and the committed placement/ops repeat step-platzieren.js onChange + commit (those live inside mount()).
// usage: node tests/place_xcheck.mjs <cases.json>   → JSON on stdout
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const W3D = require("../static/werkstatt3d.js");   // sets globalThis.W3D like the classic script tag
const ed = await import("../static/ws/editor.js");
const { geometries, catalog, cases, prims } = JSON.parse(readFileSync(process.argv[2], "utf8"));

function fakeCanvas(W, H) {
  const handlers = {};
  const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : k === "measureText" ? () => ({ width: 40 }) : () => {}),
    set: (t, k, v) => { t[k] = v; return true; } });
  return {
    clientWidth: W, clientHeight: H, width: 0, height: 0, style: {}, getContext: () => ctx,
    addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); }, removeEventListener() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: W, height: H }),
    setPointerCapture() {}, releasePointerCapture() {},
    fire(t, e) { for (const f of handlers[t] || []) f(Object.assign({ pointerId: 1, button: 0, preventDefault() {} }, e)); },
  };
}

// step-platzieren.js commit(): placement, elevation when changed, catalog dims for missing ones
function commitOps(scene, p, pl, elev) {
  const set = ed.set, ops = [set(`pieces.${p.id}.placement`, pl)];
  if (elev != null && elev !== (+p.elev || 0)) ops.push(set(`pieces.${p.id}.elev`, elev));
  if (scene && pl.mode === "3d") {
    const d = scene.dims(p);
    ["w", "h", "d"].forEach((k, i) => { if (!(+p[k] > 0)) ops.push(set(`pieces.${p.id}.${k}`, Math.round(d[i]))); });
  }
  return ops;
}

function tap(c) {
  const g = geometries[c.geo], doc = { room: "uploads/x.png", geometry: g, pieces: [c.piece] };
  const scene = ed.sceneFor(doc, catalog);
  if (!scene) return { result: null, why: "no scene" };
  const [W, H] = scene.size, u = c.point[1] / 1000 * W, v = c.point[0] / 1000 * H;
  const cv = fakeCanvas(W, H), out = { result: null, ops: null, hit: false };
  const pieces = [JSON.parse(JSON.stringify(c.piece))];
  const ctl = W3D.attach(cv, { scene, imgSize: scene.size, mode: "place", getPieces: () => pieces, getActiveId: () => c.piece.id,
    onBegin: (id, kind) => { if (kind !== "place") out.hit = true; },
    onChange: (id, pose, o) => {
      if (!o.final) return;
      const p = pieces[0], { elev, ...bare } = pose;
      const next = ed.reprojected(scene, elev != null ? { ...p, elev } : p, { mode: "3d", pose: bare, box: null });
      out.result = elev != null ? { ...next, elev } : next;
      out.ops = commitOps(scene, p, next, elev);
    } });
  cv.fire("pointerdown", { clientX: u, clientY: v });
  cv.fire("pointerup", { clientX: u, clientY: v });
  ctl.destroy();
  if (out.result && c.wall) {   // „An die Wand“ (toWall) applied to the tapped pose
    const elev = out.result.elev, p = elev != null ? { ...pieces[0], elev } : pieces[0];   // the store has the new elev by then
    const pose = out.result.pose, nw = scene.nearestWall(pose.x, pose.z), s = nw && scene.snapToWall(pose, p, nw.wall.id);
    const next = s && ed.reprojected(scene, p, { mode: "3d", pose: s, box: null });
    out.result = next && next.box ? { ...next, ...(elev != null ? { elev } : {}) } : null;
  }
  return out;
}

function prim(c) {
  const scene = W3D.init(geometries[c.geo], catalog), [u, v] = c.uv, p = c.piece;
  const fp = scene.floorPoint(u, v), ca = scene.centreAt(u, v, p, c.yaw || 0), wh = scene.wallHit(u, v);
  const pose = c.pose;
  return {
    floorPoint: fp, centreAt: ca, wallHit: wh && { t: wh.t, wall: wh.wall.id, x: wh.x, y: wh.y, z: wh.z },
    hang: scene.hang(u, v, p), place: scene.place(u, v, p, c.yaw || 0),
    toImage: scene.toImage([pose.x, 50, pose.z]), projectBox: scene.projectBox(p, pose),
    nearestWall: (nw => nw && { wall: nw.wall.id, dist: nw.dist })(scene.nearestWall(pose.x, pose.z)),
    snapAuto: scene.snapToWall(pose, p), snapW0: scene.snapToWall(pose, p, "w0"),
    distance: scene.distance(pose), relations: scene.relations({ ...p, placement: { mode: "3d", pose, box: null } }),
    dims: scene.dims(p), elev: scene.elev(p), toGrid: W3D.toGrid([u, v], scene.size),
  };
}

process.stdout.write(JSON.stringify({ taps: (cases || []).map(tap), prims: (prims || []).map(prim) }));
