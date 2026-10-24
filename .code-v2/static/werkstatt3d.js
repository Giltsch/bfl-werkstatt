/* Werkstatt 3D placement: floor/wall math (mirrors geometry.py, spec §2), wireframe overlay and pointer interaction.
   Image coords are room-image pixels; floor coords x right, y up, z forward (cm). No DOM access at load time. */
(function (root) {
  "use strict";
  const NEAR = 10, MIN_Z = 50, SNAP_CM = 30, SNAP_DEG = 10, COVER_CM = 50;  // MIN_Z: nearest usable corner (cm)
  const EDGES = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
  const DEF_DIMS = [100, 100, 60];

  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const unit = a => mul(a, 1 / Math.hypot(a[0], a[1], a[2]));
  const rad = d => d * Math.PI / 180, deg = r => r * 180 / Math.PI;
  const wrap = a => ((a + 180) % 360 + 360) % 360 - 180;
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

  function frameOf(normal, d) {
    const n = unit(normal);
    const fwd = unit(sub([0, 0, 1], mul(n, n[2])));
    return { origin: mul(n, -d), right: cross(fwd, n), up: n, forward: fwd };
  }

  function hullOf(points) {  // Andrew's monotone chain, counter-clockwise in (u, v), no repeated end point
    const seen = new Set(), pts = [];
    for (const p of points) { const k = p[0] + "," + p[1]; if (!seen.has(k)) { seen.add(k); pts.push([p[0], p[1]]); } }
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (pts.length <= 2) return pts;
    const cr = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [], upper = [];
    for (const p of pts) { while (lower.length >= 2 && cr(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
    for (let i = pts.length - 1; i >= 0; i--) { const p = pts[i]; while (upper.length >= 2 && cr(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }

  function boxToGrid(hull, size) {
    if (!hull || !hull.length) return null;
    const [W, H] = size, us = hull.map(p => p[0]), vs = hull.map(p => p[1]);
    const c = x => clamp(x, 0, 1000);
    const umin = clamp(Math.min(...us), 0, W), umax = clamp(Math.max(...us), 0, W);
    const vmin = clamp(Math.min(...vs), 0, H), vmax = clamp(Math.max(...vs), 0, H);
    return [c(Math.floor(1000 * vmin / H)), c(Math.floor(1000 * umin / W)), c(Math.ceil(1000 * vmax / H)), c(Math.ceil(1000 * umax / W))];
  }

  function toGrid(p, size) {
    return [clamp(Math.round(p[1] / size[1] * 1000), 0, 1000), clamp(Math.round(p[0] / size[0] * 1000), 0, 1000)];
  }

  function scalePoses(pieces, k) {
    for (const p of pieces || []) {
      const pose = p && p.placement && p.placement.mode === "3d" && p.placement.pose;
      if (pose) { pose.x *= k; pose.z *= k; }
    }
  }

  function pointInPoly(p, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const a = poly[i], b = poly[j];
      if ((a[1] > p[1]) !== (b[1] > p[1]) && p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside;
    }
    return inside;
  }

  function poseOf(piece) {
    const pl = piece && piece.placement;
    return pl && pl.mode === "3d" && pl.pose ? pl.pose : null;
  }

  function init(geometry, catalog) {
    if (!geometry || !geometry.ok || !geometry.floor || !geometry.intrinsics || !geometry.size) return null;
    const K = geometry.intrinsics, d = geometry.floor.d, fr = frameOf(geometry.floor.normal, d);
    const walls = geometry.walls || [], size = geometry.size.slice();
    const types = (catalog && catalog.pieces) || {};

    const cam = p => add(add(add(fr.origin, mul(fr.right, p[0])), mul(fr.up, p[1])), mul(fr.forward, p[2]));
    const proj = X => [K.fx * X[0] / X[2] + K.cx, K.fy * X[1] / X[2] + K.cy];
    const ray = (u, v) => [(u - K.cx) / K.fx, (v - K.cy) / K.fy, 1];
    const wallDist = (w, x, z) => w.normal[0] * x + w.normal[1] * z + w.offset;
    const wallYaw = w => deg(Math.atan2(w.normal[0], -w.normal[1]));
    const covers = (w, x, z) => {
      if (!w.from || !w.to) return true;
      const ax = w.to[0] - w.from[0], az = w.to[1] - w.from[1], L = Math.hypot(ax, az);
      if (L < 1e-6) return true;
      const s = ((x - w.from[0]) * ax + (z - w.from[1]) * az) / L;
      return s >= -COVER_CM && s <= L + COVER_CM;
    };

    const sc = { geometry, size, frame: fr, walls, ceiling: geometry.ceiling || null, cam, proj };

    sc.dims = piece => {
      const def = (piece && types[piece.type] && types[piece.type].dims) || DEF_DIMS;
      return ["w", "h", "d"].map((k, i) => (piece && +piece[k] > 0 ? +piece[k] : def[i]));
    };

    sc.floorPoint = (u, v) => {
      const r = ray(u, v), nr = dot(fr.up, r);
      if (nr >= 0) return null;
      const t = -d / nr;
      if (t <= 0) return null;
      const X = sub(mul(r, t), fr.origin);
      return { x: dot(X, fr.right), z: dot(X, fr.forward) };
    };

    sc.toImage = p => { const X = cam(p); return X[2] < NEAR ? null : proj(X); };

    sc.boxCorners = (piece, pose) => {
      pose = pose || poseOf(piece);
      const [W, H, D] = sc.dims(piece), th = rad(pose.yaw || 0);
      const ex = [Math.cos(th), Math.sin(th)], ez = [-Math.sin(th), Math.cos(th)], out = [];
      for (let i = 0; i < 8; i++) {
        const sx = i & 1 ? 1 : -1, sz = i & 2 ? 1 : -1, y = i & 4 ? H : 0;
        out.push([pose.x + sx * W / 2 * ex[0] + sz * D / 2 * ez[0], y, pose.z + sx * W / 2 * ex[1] + sz * D / 2 * ez[1]]);
      }
      return out;
    };

    // camera-space segment clipped to Z >= NEAR → [p, q] image points or null
    const clipSeg = (A, B) => {
      if (A[2] < NEAR && B[2] < NEAR) return null;
      if (A[2] < NEAR) A = add(A, mul(sub(B, A), (NEAR - A[2]) / (B[2] - A[2])));
      else if (B[2] < NEAR) B = add(B, mul(sub(A, B), (NEAR - B[2]) / (A[2] - B[2])));
      return [proj(A), proj(B)];
    };
    sc.segment = (p, q) => clipSeg(cam(p), cam(q));
    sc.clipCam = clipSeg;

    sc.projectBox = (piece, pose) => {
      const C = sc.boxCorners(piece, pose).map(cam);
      const corners = C.map(X => (X[2] >= NEAR ? proj(X) : null));
      const pts = corners.filter(Boolean);
      for (const [a, b] of EDGES) {
        const za = C[a][2], zb = C[b][2];
        if ((za - NEAR) * (zb - NEAR) < 0) pts.push(proj(add(C[a], mul(sub(C[b], C[a]), (NEAR - za) / (zb - za)))));
      }
      const hull = hullOf(pts), box = boxToGrid(hull, size);
      // at/behind the camera the clipped hull overflows the photo: not a usable placement (mirrors geometry.py)
      const nearest = Math.min(...C.map(X => X[2]));
      return { corners, hull, box, visible: !!(hull.length && box && box[2] > box[0] && box[3] > box[1]) && nearest >= MIN_Z };
    };

    sc.nearestWall = (x, z) => {
      let best = null;
      for (const w of walls) {
        if (!covers(w, x, z)) continue;
        const dist = wallDist(w, x, z);
        if (!best || Math.abs(dist) < Math.abs(best.dist)) best = { wall: w, dist };
      }
      return best;
    };

    sc.snapToWall = (pose, piece, wallId) => {
      const D = sc.dims(piece)[2];
      let w = null;
      if (wallId != null) w = walls.find(w => w.id === wallId) || null;
      else {
        let bestErr = SNAP_CM;
        for (const c of walls) {
          if (!covers(c, pose.x, pose.z)) continue;
          const err = Math.abs(wallDist(c, pose.x, pose.z) - D / 2);
          if (err < bestErr) { bestErr = err; w = c; }
        }
      }
      if (!w) return null;
      const k = D / 2 - wallDist(w, pose.x, pose.z);
      return { x: pose.x + k * w.normal[0], z: pose.z + k * w.normal[1], yaw: wallYaw(w), wall: w.id };
    };

    // tap-to-place snaps from the wall base up to D/2 + 30 cm in front of it (a tap on the base line snaps too)
    sc.place = (u, v, piece) => {
      const fp = sc.floorPoint(u, v);
      if (!fp) return null;
      const D = sc.dims(piece)[2], nw = sc.nearestWall(fp.x, fp.z);
      if (nw && nw.dist - D / 2 < SNAP_CM) return sc.snapToWall({ x: fp.x, z: fp.z, yaw: 0, wall: null }, piece, nw.wall.id);
      return { x: fp.x, z: fp.z, yaw: 0, wall: null };
    };

    sc.bottomCentre = pose => sc.toImage([pose.x, 0, pose.z]);
    // the target stays on the floor seen in the photo: a tall piece grabbed at the top must not slide under the camera
    sc.dragTarget = (offset, pointer) => sc.floorPoint(clamp(pointer[0] - offset[0], 0, size[0]), clamp(pointer[1] - offset[1], 0, size[1]));

    sc.handlePoint = (piece, pose) => {
      pose = pose || poseOf(piece);
      const D = sc.dims(piece)[2], th = rad(pose.yaw || 0), k = D / 2 + 25;
      return sc.toImage([pose.x + Math.sin(th) * k, 0, pose.z - Math.cos(th) * k]);
    };

    // yaw snapped to wall angles (+k·90°) within ±10°; {yaw, wall: id of the wall whose own angle matched, or null}
    sc.snapYaw = yaw => {
      const bases = walls.length ? walls.map(w => [wallYaw(w), w.id]) : [[0, null]];
      let best = null;
      for (const [b, id] of bases) for (let k = -2; k <= 2; k++) {
        const diff = Math.abs(wrap(yaw - (b + k * 90)));
        if (diff <= SNAP_DEG && (!best || diff < best.diff)) best = { yaw: wrap(b + k * 90), wall: k === 0 ? id : null, diff };
      }
      return best ? { yaw: best.yaw, wall: best.wall } : { yaw: wrap(yaw), wall: null };
    };

    sc.measure = (p1, p2, mode) => {
      if (mode === "floor") {
        const a = sc.floorPoint(p1[0], p1[1]), b = sc.floorPoint(p2[0], p2[1]);
        return a && b ? Math.hypot(a.x - b.x, a.z - b.z) : null;
      }
      if (mode === "vertical") {
        const [lo, hi] = p1[1] >= p2[1] ? [p1, p2] : [p2, p1];
        const P = sc.floorPoint(lo[0], lo[1]);
        if (!P) return null;
        const A = cam([P.x, 0, P.z]), d1 = fr.up, d2 = ray(hi[0], hi[1]);
        const a = dot(d1, d1), b = dot(d1, d2), c = dot(d2, d2), den = a * c - b * b;
        if (Math.abs(den) < 1e-9) return null;
        return (b * dot(d2, A) - c * dot(d1, A)) / den;
      }
      return null;
    };

    sc.relations = piece => {
      const pose = poseOf(piece), [W, H, D] = sc.dims(piece);
      const ch = sc.ceiling ? sc.ceiling.height : null;
      const out = { ceilingHeight: ch, ceilingRatio: ch ? H / ch : null, wallRatio: null, wallLength: null, wallLabel: null,
        distance: null, tooHigh: !!(ch && H > ch), tooWide: false, outOfImage: false };
      if (!pose) return out;
      let w = walls.find(w => w.id === pose.wall) || null;
      if (!w) {
        let bestErr = SNAP_CM;
        for (const c of walls) {
          if (!covers(c, pose.x, pose.z)) continue;
          const err = Math.abs(wallDist(c, pose.x, pose.z) - D / 2);
          if (err < bestErr) { bestErr = err; w = c; }
        }
      }
      if (w) {
        out.wallLabel = w.label;
        if (w.from && w.to) {
          out.wallLength = Math.hypot(w.to[0] - w.from[0], w.to[1] - w.from[1]);
          out.wallRatio = out.wallLength > 0 ? W / out.wallLength : null;
          out.tooWide = !!(out.wallRatio && out.wallRatio > 1);
        }
      }
      out.distance = Math.hypot(pose.x, pose.z);
      out.outOfImage = !sc.projectBox(piece).visible;
      return out;
    };

    // floor extent seen in the photo (for the 50 cm grid)
    sc.floorRange = () => {
      const [W, H] = size, xs = [], zs = [];
      for (let i = 0; i <= 8; i++) for (let j = 0; j <= 12; j++) {
        const fp = sc.floorPoint(W * i / 8, H * j / 12);
        if (fp && fp.z < 3000) { xs.push(fp.x); zs.push(fp.z); }
      }
      if (!xs.length) return null;
      return { x0: Math.max(-1500, Math.min(...xs)), x1: Math.min(1500, Math.max(...xs)),
        z0: Math.max(0, Math.min(...zs)), z1: Math.min(1500, Math.max(...zs)) };
    };

    // clip a floor segment (x, z) to the room side of every wall → [[x, z], [x, z]] or null
    sc.clipToRoom = (a, b) => {
      let t0 = 0, t1 = 1;
      for (const w of walls) {
        const fa = wallDist(w, a[0], a[1]), fb = wallDist(w, b[0], b[1]);
        if (fa < 0 && fb < 0) return null;
        if (fa < 0) t0 = Math.max(t0, fa / (fa - fb));
        else if (fb < 0) t1 = Math.min(t1, fa / (fa - fb));
      }
      if (t1 <= t0) return null;
      const at = t => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      return [at(t0), at(t1)];
    };
    return sc;
  }

  // ---------------- drawing ----------------

  const COLORS = ["#59a7ff", "#7cc4a0", "#f2b84b", "#e879f9"];
  const rgba = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
  };
  const fmt = v => String(Math.round(v));

  function drawOverlay(ctx, scene, pieces, opts) {
    opts = opts || {};
    if (!scene || !ctx) return;
    const k = opts.pxScale || 1, touch = !!opts.touch, S = p => [p[0] * k, p[1] * k];
    const line = (p, q) => { const a = S(p), b = S(q); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); };
    const label = (text, p, bg, fg) => {
      ctx.font = `${touch ? 14 : 12}px system-ui, sans-serif`;
      const w = ctx.measureText(text).width + 10, h = touch ? 20 : 18, a = S(p);
      ctx.fillStyle = bg; ctx.fillRect(a[0] - w / 2, a[1] - h, w, h);
      ctx.fillStyle = fg || "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(text, a[0], a[1] - h / 2);
    };
    ctx.save();
    ctx.lineCap = "round"; ctx.lineJoin = "round";

    if (opts.showRoom) {
      const r = scene.floorRange();
      if (r) {
        ctx.strokeStyle = "rgba(255,255,255,.25)"; ctx.lineWidth = 1;
        const st = 50, xa = Math.floor(r.x0 / st) * st, xb = Math.ceil(r.x1 / st) * st, za = Math.floor(r.z0 / st) * st, zb = Math.ceil(r.z1 / st) * st;
        const grid = (a, b) => { const c = scene.clipToRoom(a, b); const s = c && scene.segment([c[0][0], 0, c[0][1]], [c[1][0], 0, c[1][1]]); if (s) line(s[0], s[1]); };
        for (let x = xa; x <= xb; x += st) grid([x, za], [x, zb]);
        for (let z = za; z <= zb; z += st) grid([xa, z], [xb, z]);
      }
      ctx.setLineDash([8, 6]); ctx.lineWidth = 2; ctx.strokeStyle = "rgba(255,120,90,.9)";
      for (const w of scene.walls) {
        if (!w.from || !w.to) continue;
        const s = scene.segment([w.from[0], 0, w.from[1]], [w.to[0], 0, w.to[1]]);
        if (!s) continue;
        line(s[0], s[1]);
        ctx.setLineDash([]);
        label(w.label, [(s[0][0] + s[1][0]) / 2, (s[0][1] + s[1][1]) / 2 - 4 / k], "rgba(255,120,90,.85)");
        ctx.setLineDash([8, 6]);
      }
      ctx.setLineDash([]);
    }

    (pieces || []).forEach((piece, i) => {
      const pose = poseOf(piece);
      if (!pose) return;
      const col = COLORS[i % COLORS.length], active = piece.id === opts.activeId;
      const pb = scene.projectBox(piece), C = scene.boxCorners(piece).map(scene.cam);
      const fc = [0, 1, 5, 4].map(j => pb.corners[j]);
      if (fc.every(Boolean)) {
        ctx.fillStyle = rgba(col, 0.12); ctx.beginPath();
        fc.forEach((p, j) => { const a = S(p); j ? ctx.lineTo(a[0], a[1]) : ctx.moveTo(a[0], a[1]); });
        ctx.closePath(); ctx.fill();
      }
      ctx.strokeStyle = col; ctx.lineWidth = active ? 3 : 2;
      for (const [a, b] of EDGES) { const s = scene.clipCam(C[a], C[b]); if (s) line(s[0], s[1]); }
      const top = [4, 5, 6, 7].map(j => pb.corners[j]).filter(Boolean);
      if (top.length) {
        const [W, H, D] = scene.dims(piece);
        const u = top.reduce((s, p) => s + p[0], 0) / top.length, v = Math.min(...top.map(p => p[1]));
        label(`B ${fmt(W)} × H ${fmt(H)} × T ${fmt(D)}`, [u, v - 6 / k], rgba(col, active ? 0.95 : 0.75), "#0b0d10");
      }
      const h = scene.handlePoint(piece);
      if (h) {
        const a = S(h), r = touch ? 16 : 9;
        ctx.beginPath(); ctx.arc(a[0], a[1], r, 0, 2 * Math.PI);
        ctx.fillStyle = active ? col : "rgba(0,0,0,.55)"; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke();
        ctx.beginPath(); ctx.arc(a[0], a[1], r * 0.5, -0.4 * Math.PI, 0.9 * Math.PI); ctx.stroke();
      }
    });

    const m = opts.measureLine;
    if (m) {
      ctx.strokeStyle = "#ffd84a"; ctx.lineWidth = 2; ctx.setLineDash([7, 5]);
      line(m[0], m[1]); ctx.setLineDash([]);
      const a = S(m[0]), b = S(m[1]), L = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (L > 0) {
        const nx = -(b[1] - a[1]) / L * 7, ny = (b[0] - a[0]) / L * 7;
        for (const p of [a, b]) { ctx.beginPath(); ctx.moveTo(p[0] - nx, p[1] - ny); ctx.lineTo(p[0] + nx, p[1] + ny); ctx.stroke(); }
      }
      const steep = Math.abs(m[1][1] - m[0][1]) > 1.5 * Math.abs(m[1][0] - m[0][0]);  // same rule as the dialog (openCal)
      const cm = scene.measure(m[0], m[1], steep ? "vertical" : "floor");
      label(cm ? `≈ ${fmt(cm)} cm` : "–", [(m[0][0] + m[1][0]) / 2, (m[0][1] + m[1][1]) / 2 - 8 / k], "rgba(0,0,0,.75)", "#ffd84a");
    }
    ctx.restore();
  }

  // ---------------- interaction ----------------

  function attach(canvas, opts) {
    let scene = opts.scene || null, mode = opts.mode || "off", showRoom = !!opts.showRoom;
    const touch = !!opts.touch, imgSize = opts.imgSize || (scene && scene.size) || [1, 1];
    const raf = root.requestAnimationFrame ? root.requestAnimationFrame.bind(root) : fn => setTimeout(fn, 16);
    const caf = root.cancelAnimationFrame ? root.cancelAnimationFrame.bind(root) : clearTimeout;
    const ctx = canvas.getContext && canvas.getContext("2d");
    const listeners = [];
    let drag = null, measureLine = null, frame = 0, pending = null;
    const noop = () => {};
    const onChange = opts.onChange || noop, onSelect = opts.onSelect || noop, onMeasure = opts.onMeasure || noop;
    const pieces = () => (opts.getPieces ? opts.getPieces() : []) || [];
    const activeId = () => (opts.getActiveId ? opts.getActiveId() : null);
    const on = (type, fn) => { canvas.addEventListener(type, fn); listeners.push([type, fn]); };
    const pxScale = () => (canvas.clientWidth || imgSize[0]) / imgSize[0];

    function applyMode() {
      if (!canvas.style) return;
      const live = mode === "place" || mode === "measure";
      canvas.style.pointerEvents = live ? "auto" : "none";
      canvas.style.touchAction = live ? "none" : "auto";
      canvas.style.cursor = mode === "measure" ? "crosshair" : live ? "pointer" : "";
    }

    function redraw() {
      if (!ctx) return;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (!scene) return;
      const dpr = canvas.width / Math.max(1, canvas.clientWidth || canvas.width);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawOverlay(ctx, scene, pieces(), { pxScale: pxScale(), activeId: activeId(), showRoom, measureLine, touch });
    }

    function resize() {
      const dpr = root.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round((canvas.clientWidth || imgSize[0]) * dpr));
      canvas.height = Math.max(1, Math.round((canvas.clientHeight || imgSize[1]) * dpr));
      redraw();
    }

    function imgPt(e) {
      const r = canvas.getBoundingClientRect();
      return [(e.clientX - r.left) * imgSize[0] / (r.width || 1), (e.clientY - r.top) * imgSize[1] / (r.height || 1)];
    }

    function hit(p) {  // → {type: "rotate"|"move", piece} for the topmost piece under p (active first)
      const list = pieces().map((piece, i) => ({ piece, i })).filter(o => poseOf(o.piece));
      const act = activeId();
      list.sort((a, b) => (b.piece.id === act) - (a.piece.id === act) || b.i - a.i);
      const r = (touch ? 16 : 9) + 6;
      for (const { piece } of list) {
        const h = scene.handlePoint(piece);
        if (h && Math.hypot(h[0] - p[0], h[1] - p[1]) * pxScale() <= r) return { type: "rotate", piece };
      }
      for (const { piece } of list) {
        const pb = scene.projectBox(piece);
        if (pb.hull.length >= 3 && pointInPoly(p, pb.hull)) return { type: "move", piece };
      }
      return null;
    }

    function step(final) {
      frame = 0;
      if (!drag || !scene) return;
      const p = pending || drag.start, piece = drag.piece, p0 = drag.pose0;
      let pose = null;
      if (drag.type === "move") {
        const fp = scene.dragTarget(drag.offset, p);
        if (fp) {
          const free = { x: fp.x, z: fp.z, yaw: p0.yaw, wall: null };
          pose = scene.snapToWall(free, piece) || free;
          if (!scene.projectBox(piece, pose).visible) pose = null;  // keeps the last usable pose
        }
      } else {
        const fp = scene.floorPoint(p[0], p[1]);
        if (fp && Math.hypot(fp.x - p0.x, fp.z - p0.z) > 1) {
          const s = scene.snapYaw(deg(Math.atan2(fp.x - p0.x, -(fp.z - p0.z))));
          pose = { x: p0.x, z: p0.z, yaw: s.yaw, wall: s.wall && s.wall === p0.wall ? p0.wall : null };
        }
      }
      if (pose) drag.last = pose;
      if (pose || (final && drag.last)) {
        onChange(piece.id, pose || drag.last, { final });
        redraw();
      }
    }

    function down(e) {
      if (!scene || (e.button != null && e.button > 0)) return;
      const p = imgPt(e);
      if (mode === "measure") {
        measureLine = [p, p];
        drag = { type: "measure" };
      } else if (mode === "place") {
        const h = hit(p);
        if (h) {
          if (h.piece.id !== activeId()) onSelect(h.piece.id);
          const pose0 = Object.assign({}, poseOf(h.piece)), bc = scene.bottomCentre(pose0);
          drag = { type: h.type, piece: h.piece, pose0, start: p, last: null, moved: false,
            offset: bc ? [p[0] - bc[0], p[1] - bc[1]] : [0, 0] };
        } else {
          const piece = pieces().find(q => q.id === activeId());
          if (!piece || (poseOf(piece) && scene.projectBox(piece).visible)) return;
          const pose = scene.place(p[0], p[1], piece);  // null above the horizon: ignored
          if (pose) { onChange(piece.id, pose, { final: true }); redraw(); }
          return;
        }
      } else return;
      pending = null;
      if (canvas.setPointerCapture && e.pointerId != null) try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* already released */ }
      if (e.preventDefault) e.preventDefault();
      redraw();
    }

    function move(e) {
      if (!drag) return;
      const p = imgPt(e);
      if (drag.type === "measure") {
        measureLine = [measureLine[0], p];
        if (!frame) frame = raf(() => { frame = 0; redraw(); });
        return;
      }
      pending = p;
      drag.moved = true;
      if (!frame) frame = raf(() => step(false));
    }

    function up(e) {
      if (!drag) return;
      if (frame) { caf(frame); frame = 0; }
      if (drag.type === "measure") {
        const [a, b] = measureLine;
        if (Math.hypot(b[0] - a[0], b[1] - a[1]) * pxScale() >= 10) onMeasure(a, b);
        else measureLine = null;
        redraw();
      } else if (drag.moved) step(true);
      drag = null; pending = null;
      if (canvas.releasePointerCapture && e && e.pointerId != null) try { canvas.releasePointerCapture(e.pointerId); } catch (err) { /* not captured */ }
    }

    on("pointerdown", down);
    on("pointermove", move);
    on("pointerup", up);
    on("pointercancel", up);
    applyMode();
    resize();

    return {
      setMode(m) { mode = m; drag = null; if (m !== "measure") measureLine = null; applyMode(); redraw(); },
      setScene(s) { scene = s; drag = null; redraw(); },
      setShowRoom(b) { showRoom = !!b; redraw(); },
      redraw, resize,
      destroy() {
        if (frame) { caf(frame); frame = 0; }
        for (const [type, fn] of listeners) canvas.removeEventListener(type, fn);
        listeners.length = 0;
        drag = null; scene = null;
        mode = "off"; applyMode();
        if (ctx) { ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height); }
      },
    };
  }

  const W3D = { version: 1, COLORS, EDGES, NEAR, MIN_Z, init, toGrid, boxToGrid, scalePoses, frameOf, hullOf, drawOverlay, attach };
  root.W3D = W3D;
  if (typeof module !== "undefined" && module.exports) module.exports = W3D;
})(typeof window !== "undefined" ? window : globalThis);
