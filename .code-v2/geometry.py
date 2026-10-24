"""Room geometry from one photo: metric depth (Apple DepthPro), floor plane, walls, ceiling, vanishing points,
and the box math the Werkstatt uses to place furniture in real centimetres. Conventions: see the spec §2 —
camera coords X right, Y down, Z forward (cm); floor coords x right, y up, z forward (cm)."""

import copy
import json
import math
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

DEPTH_REPO = "apple/DepthPro-hf"
GEO_DIR = Path(__file__).parent / "outputs" / "geometry"
NPY_SIDE = 512
NEAR = 10.0  # near plane (cm)
MIN_Z = 50.0  # a box corner nearer to the camera than this (or behind it) is not a usable placement
EDGES = ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7))
DEFAULT_DIMS = (100, 100, 60)
NO_FLOOR = "Kein Boden erkannt – bitte Rahmen von Hand zeichnen."
FAILED = "Raumerkennung fehlgeschlagen – Rahmen von Hand zeichnen."

_model = _processor = None
_load_lock = threading.Lock()


def rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    return img.convert("RGB")


# ---------------- depth model ----------------

def load_depth() -> None:
    global _model, _processor
    with _load_lock:
        if _model is not None:
            return
        import torch
        from transformers import AutoImageProcessor, DepthProForDepthEstimation

        _processor = AutoImageProcessor.from_pretrained(DEPTH_REPO)
        _model = DepthProForDepthEstimation.from_pretrained(DEPTH_REPO, dtype=torch.float16).to("cuda").eval()


def unload_depth() -> None:
    global _model, _processor
    with _load_lock:
        if _model is None:
            return
        _model = _processor = None
        import gc

        import torch

        gc.collect()
        torch.cuda.empty_cache()


def depth_loaded() -> bool:
    return _model is not None


def _estimate(img: Image.Image, max_side: int = 1536) -> tuple[np.ndarray, float, bool]:
    """(depth cm at the downscaled size, focal px for img's size, focal came from the model)."""
    import torch

    load_depth()
    W, H = img.size
    k = min(1.0, max_side / max(W, H))
    small = img if k == 1 else img.resize((max(1, round(W * k)), max(1, round(H * k))), Image.LANCZOS)
    w, h = small.size
    try:
        with torch.no_grad():
            inputs = _processor(images=small, return_tensors="pt").to("cuda", torch.float16)
            out = _model(**inputs)
            post = _processor.post_process_depth_estimation(out, target_sizes=[(h, w)])[0]
        depth = post["predicted_depth"].float().cpu().numpy().astype(np.float32) * 100.0
        f = post.get("focal_length")
        f = float(f) if f is not None else float("nan")
    finally:
        inputs = out = post = None
        torch.cuda.empty_cache()  # unified memory: give the activations back to FLUX, the judge and Ollama
    if math.isfinite(f) and f > 0:
        return depth, f * W / w, True
    return depth, 0.8 * max(W, H), False


def estimate_depth(img: Image.Image, max_side: int = 1536) -> tuple[np.ndarray, float]:
    depth, f, _ = _estimate(img, max_side)
    return depth, f


# ---------------- frame, projection, placement (mirrored in static/werkstatt3d.js) ----------------

def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def frame_of(normal, d) -> dict:
    n = _unit(normal)
    fwd = _unit(np.array([0.0, 0.0, 1.0]) - n[2] * n)
    right = np.cross(fwd, n)
    return {"origin": (-float(d) * n).tolist(), "right": right.tolist(), "up": n.tolist(), "forward": fwd.tolist()}


def _frame(geo: dict) -> dict:
    fl = geo["floor"]
    return {k: np.asarray(v, dtype=float) for k, v in frame_of(fl["normal"], fl["d"]).items()}


def _cam(fr: dict, p) -> np.ndarray:
    return fr["origin"] + p[0] * fr["right"] + p[1] * fr["up"] + p[2] * fr["forward"]


def _ray(geo: dict, u: float, v: float) -> np.ndarray:
    k = geo["intrinsics"]
    return np.array([(u - k["cx"]) / k["fx"], (v - k["cy"]) / k["fy"], 1.0])


def _proj(geo: dict, X) -> list[float]:
    k = geo["intrinsics"]
    return [float(k["fx"] * X[0] / X[2] + k["cx"]), float(k["fy"] * X[1] / X[2] + k["cy"])]


def floor_point(geo: dict, u: float, v: float) -> tuple[float, float] | None:
    fr = _frame(geo)
    ray = _ray(geo, u, v)
    nr = float(fr["up"] @ ray)
    if nr >= 0:
        return None
    t = -geo["floor"]["d"] / nr
    if t <= 0:
        return None
    X = t * ray - fr["origin"]
    return float(X @ fr["right"]), float(X @ fr["forward"])


def box_corners(dims, pose: dict) -> list[list[float]]:
    W, H, D = dims
    th = math.radians(pose.get("yaw") or 0.0)
    ex, ez = (math.cos(th), math.sin(th)), (-math.sin(th), math.cos(th))
    out = []
    for i in range(8):
        sx, sz, y = (1 if i & 1 else -1), (1 if i & 2 else -1), (H if i & 4 else 0)
        out.append([pose["x"] + sx * W / 2 * ex[0] + sz * D / 2 * ez[0], y,
                    pose["z"] + sx * W / 2 * ex[1] + sz * D / 2 * ez[1]])
    return out


def hull_of(pts) -> list[list[float]]:
    """Andrew's monotone chain; counter-clockwise in (u, v) coordinates, no repeated end point."""
    pts = sorted({(float(p[0]), float(p[1])) for p in pts})
    if len(pts) <= 2:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def grid_box(hull, size) -> list[int] | None:
    if not hull:
        return None
    W, H = size
    us, vs = [p[0] for p in hull], [p[1] for p in hull]
    umin, umax = min(max(min(us), 0), W), min(max(max(us), 0), W)
    vmin, vmax = min(max(min(vs), 0), H), min(max(max(vs), 0), H)
    c = lambda x: max(0, min(1000, int(x)))
    return [c(math.floor(1000 * vmin / H)), c(math.floor(1000 * umin / W)),
            c(math.ceil(1000 * vmax / H)), c(math.ceil(1000 * umax / W))]


def project_box(geo: dict, dims, pose: dict) -> dict:
    fr = _frame(geo)
    cam = [_cam(fr, p) for p in box_corners(dims, pose)]
    corners = [_proj(geo, X) if X[2] >= NEAR else None for X in cam]
    pts = [c for c in corners if c is not None]
    for a, b in EDGES:
        za, zb = cam[a][2], cam[b][2]
        if (za - NEAR) * (zb - NEAR) < 0:
            t = (NEAR - za) / (zb - za)
            pts.append(_proj(geo, cam[a] + t * (cam[b] - cam[a])))
    hull = hull_of(pts)
    box = grid_box(hull, geo["size"])
    # at/behind the camera the clipped hull overflows the image and would turn the whole photo into the edit box
    visible = bool(hull) and box is not None and box[2] > box[0] and box[3] > box[1] and float(min(X[2] for X in cam)) >= MIN_Z
    r = lambda p: [round(p[0], 2), round(p[1], 2)]
    return {"corners": [r(c) if c else None for c in corners], "hull": [r(p) for p in hull],
            "box": box, "visible": visible}


def _wall_yaw(w: dict) -> float:
    return math.degrees(math.atan2(w["normal"][0], -w["normal"][1]))


def _covers(w: dict, x: float, z: float, margin: float = 50.0) -> bool:
    if not w.get("from") or not w.get("to"):
        return True
    a, b = np.asarray(w["from"], float), np.asarray(w["to"], float)
    L = float(np.linalg.norm(b - a))
    if L < 1e-6:
        return True
    s = float((np.array([x, z]) - a) @ (b - a)) / L
    return -margin <= s <= L + margin


def _wall_dist(w: dict, x: float, z: float) -> float:
    return w["normal"][0] * x + w["normal"][1] * z + w["offset"]


def snap_to_wall(geo: dict, x: float, z: float, depth_cm: float, wall_id: str | None = None,
                 max_dist: float = 30.0) -> dict | None:
    walls = geo.get("walls") or []
    if wall_id is not None:
        w = next((w for w in walls if w["id"] == wall_id), None)
    else:
        cands = [(abs(_wall_dist(w, x, z) - depth_cm / 2), w) for w in walls if _covers(w, x, z)]
        cands = [c for c in cands if c[0] < max_dist]
        w = min(cands, key=lambda c: c[0])[1] if cands else None
    if w is None:
        return None
    nx, nz = w["normal"]
    k = depth_cm / 2 - _wall_dist(w, x, z)
    return {"x": x + k * nx, "z": z + k * nz, "yaw": _wall_yaw(w), "wall": w["id"]}


def measure(geo: dict, p1, p2, mode: str, depth: np.ndarray | None = None) -> float | None:
    if mode == "floor":
        a, b = floor_point(geo, *p1), floor_point(geo, *p2)
        return None if a is None or b is None else math.dist(a, b)
    if mode == "vertical":
        lo, hi = (p1, p2) if p1[1] >= p2[1] else (p2, p1)
        P = floor_point(geo, *lo)
        if P is None:
            return None
        fr = _frame(geo)
        A, d1, d2 = _cam(fr, (P[0], 0.0, P[1])), fr["up"], _ray(geo, *hi)
        a, b, c = d1 @ d1, d1 @ d2, d2 @ d2
        den = a * c - b * b
        if abs(den) < 1e-9:
            return None
        return float((b * (d2 @ A) - c * (d1 @ A)) / den)
    if mode == "depth":
        if depth is None:
            return None
        pts = []
        for p in (p1, p2):
            z = depth_at(depth, geo["size"], *p)
            if z is None:
                return None
            pts.append(z * _ray(geo, *p))
        return float(np.linalg.norm(pts[0] - pts[1]))
    return None


def rescale(geo: dict, s: float) -> dict:
    g = copy.deepcopy(geo)
    if g.get("floor"):
        g["floor"]["d"] = round(g["floor"]["d"] * s, 3)
        g["frame"] = frame_of(g["floor"]["normal"], g["floor"]["d"])
    for w in g.get("walls") or []:
        w["offset"] = round(w["offset"] * s, 3)
        for k in ("from", "to"):
            if w.get(k):
                w[k] = [round(c * s, 2) for c in w[k]]
    c = g.get("ceiling")
    if c and not c.get("manual"):
        c["height"] = round(c["height"] * s, 1)
    if g.get("depth_range"):
        g["depth_range"] = [round(v * s, 1) for v in g["depth_range"]]
    g["scale"] = round((g.get("scale") or 1.0) * s, 6)
    return g


def _check_scale(s: float) -> None:
    if not math.isfinite(s) or not 0.5 <= s <= 2.0:
        raise ValueError(f"Maßstab unplausibel (×{s:.2f}).".replace(".", ",", 1))


def calibrate(geo: dict, p1, p2, length_cm: float, mode: str, depth=None) -> tuple[dict, float, float]:
    measured = measure(geo, p1, p2, mode, depth)
    if not measured or measured <= 0:
        raise ValueError("Messung nicht möglich: Punkte liegen nicht auf dem Boden.")
    s = float(length_cm) / measured
    _check_scale(s)
    g = rescale(geo, s)
    return g, round(measured, 1), g["scale"]


def set_room_height(geo: dict, height_cm: float) -> tuple[dict, float | None, float]:
    c = geo.get("ceiling")
    if c and not c.get("manual"):
        detected = c["height"]
        s = float(height_cm) / detected
        _check_scale(s)
        g = rescale(geo, s)
        return g, detected, g["scale"]
    g = copy.deepcopy(geo)
    g["ceiling"] = {"height": float(height_cm), "manual": True}
    return g, None, g.get("scale") or 1.0


def _pose_wall(geo: dict, dims, pose: dict) -> dict | None:
    walls = geo.get("walls") or []
    w = next((w for w in walls if w["id"] == pose.get("wall")), None)
    if w is None:
        cands = [(abs(_wall_dist(w, pose["x"], pose["z"]) - dims[2] / 2), w) for w in walls
                 if _covers(w, pose["x"], pose["z"])]
        cands = [c for c in cands if c[0] < 30]
        w = min(cands, key=lambda c: c[0])[1] if cands else None
    return w


def relations(geo: dict, dims, pose: dict) -> dict:
    W, H, D = dims
    c = geo.get("ceiling")
    ch = c["height"] if c else None
    w = _pose_wall(geo, dims, pose)
    wl = math.dist(w["from"], w["to"]) if w and w.get("from") and w.get("to") else None
    return {"ceiling_height": ch, "ceiling_ratio": round(H / ch, 3) if ch else None,
            "wall_id": w["id"] if w else None, "wall_label": w["label"] if w else None,
            "wall_length": round(wl, 1) if wl else None, "wall_ratio": round(W / wl, 3) if wl else None,
            "distance": round(math.hypot(pose["x"], pose["z"]), 1)}


# ---------------- cache ----------------

def _dir(geo_dir):
    return Path(geo_dir) if geo_dir is not None else GEO_DIR


def _key(room_rel: str) -> str:
    return Path(room_rel).stem


def load_cached(room_rel: str, geo_dir: Path | None = None) -> dict | None:
    p = _dir(geo_dir) / f"{_key(room_rel)}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_cached(geo: dict, geo_dir: Path | None = None) -> None:
    d = _dir(geo_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{_key(geo['room'])}.json.tmp"
    tmp.write_text(json.dumps(geo, ensure_ascii=False, indent=1))
    tmp.replace(d / f"{_key(geo['room'])}.json")


def depth_map(room_rel: str, geo_dir: Path | None = None) -> np.ndarray | None:
    p = _dir(geo_dir) / f"{_key(room_rel)}-depth.npy"
    try:
        depth = np.load(p).astype(np.float32)
    except (OSError, ValueError):
        return None
    geo = load_cached(room_rel, geo_dir)
    return depth * float((geo or {}).get("scale") or 1.0)


def depth_at(depth: np.ndarray, size, u: float, v: float) -> float | None:
    W, H = size
    if not (0 <= u <= W and 0 <= v <= H):
        return None
    h, w = depth.shape
    k = NPY_SIDE / max(W, H)
    x = min(max(u * k - 0.5, 0.0), w - 1.0)
    y = min(max(v * k - 0.5, 0.0), h - 1.0)
    x0, y0 = int(x), int(y)
    x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
    fx, fy = x - x0, y - y0
    val = (depth[y0, x0] * (1 - fx) * (1 - fy) + depth[y0, x1] * fx * (1 - fy)
           + depth[y1, x0] * (1 - fx) * fy + depth[y1, x1] * fx * fy)
    return float(val) if math.isfinite(val) and val > 0 else None


# ---------------- fitting ----------------

def _backproject(depth: np.ndarray, f: float, samples: int = 200):
    """Regular sample grid of the depth map → camera points (cm), their v as a share of the height, Z."""
    h, w = depth.shape
    step = max(1, round(max(w, h) / samples))
    vs, us = np.mgrid[step // 2:h:step, step // 2:w:step]
    z = depth[vs, us].astype(np.float64)
    ok = np.isfinite(z) & (z > 1) & (z < 2000)
    us, vs, z = us[ok] + 0.5, vs[ok] + 0.5, z[ok]
    pts = np.stack([z * (us - w / 2) / f, z * (vs - h / 2) / f, z], axis=1)
    return pts, vs / h


def _plane_svd(P: np.ndarray):
    c = P.mean(axis=0)
    n = np.linalg.svd(P - c, full_matrices=False)[2][-1]
    return n, -float(n @ c)


def _fit_floor(pts: np.ndarray, vrel: np.ndarray, up_hint, iters: int = 400, seed: int = 0):
    """RANSAC floor plane (n up, d > 0) → (n, d, inlier share) or None."""
    up = _unit(up_hint)
    cand = pts[vrel > 0.4]
    if len(cand) < 50:
        return None
    rng = np.random.default_rng(seed)
    thr_c = 3 + 0.01 * cand[:, 2]
    best, best_n = None, 0
    for chunk in range(0, iters, 100):
        idx = rng.integers(0, len(cand), (100, 3))
        a, b, c = cand[idx[:, 0]], cand[idx[:, 1]], cand[idx[:, 2]]
        n = np.cross(b - a, c - a)
        norm = np.linalg.norm(n, axis=1)
        good = norm > 1e-6
        n, a = n[good] / norm[good, None], a[good]
        d = -np.einsum("ij,ij->i", n, a)
        flip = d < 0
        n[flip], d[flip] = -n[flip], -d[flip]
        good = (n @ up >= math.cos(math.radians(45))) & (d >= 20) & (d <= 300)
        n, d = n[good], d[good]
        if not len(n):
            continue
        counts = (np.abs(cand @ n.T + d) < thr_c[:, None]).sum(axis=0)
        i = int(counts.argmax())
        if counts[i] > best_n:
            best, best_n = (n[i], d[i]), int(counts[i])
    if best is None:
        return None
    n, d = best
    thr = 3 + 0.01 * pts[:, 2]
    for _ in range(3):
        inl = np.abs(pts @ n + d) < thr
        if inl.sum() < 3:
            return None
        n, d = _plane_svd(pts[inl])
        if d < 0:
            n, d = -n, -d
    inl = np.abs(pts @ n + d) < thr
    if not (20 <= d <= 300) or n @ up < math.cos(math.radians(45)):
        return None
    return n, float(d), float(inl.mean())


def _floor_coords(pts: np.ndarray, fr: dict) -> np.ndarray:
    Q = pts - fr["origin"]
    return np.stack([Q @ fr["right"], Q @ fr["up"], Q @ fr["forward"]], axis=1)


def _wall_dirs_ransac(xz: np.ndarray, iters: int = 300, seed: int = 1):
    """Dominant vertical plane among non-floor points → its 2D normal (fallback when no vanishing point)."""
    if len(xz) < 30:
        return None
    rng = np.random.default_rng(seed)
    dist = np.linalg.norm(xz, axis=1)
    thr = 3 + 0.01 * dist
    idx = rng.integers(0, len(xz), (iters, 2))
    t = xz[idx[:, 1]] - xz[idx[:, 0]]
    L = np.linalg.norm(t, axis=1)
    good = L > 20
    if not good.any():
        return None
    t, a = t[good] / L[good, None], xz[idx[good, 0]]
    n = np.stack([-t[:, 1], t[:, 0]], axis=1)
    c = -np.einsum("ij,ij->i", n, a)
    counts = (np.abs(xz @ n.T + c) < thr[:, None]).sum(axis=0)
    return n[int(counts.argmax())]


def _find_walls(xz: np.ndarray, dirs, min_share: float = 0.03):
    """Walls along each candidate normal direction: the farthest significant histogram peak per side."""
    walls, total = [], len(xz)
    if total < 30:
        return walls, 0
    dist = np.linalg.norm(xz, axis=1)
    for dvec in dirs:
        dvec = _unit(dvec)
        p = xz @ dvec
        lo, hi = float(p.min()), float(p.max())
        if hi - lo < 1:
            continue
        bins = np.arange(lo - 4, hi + 8, 4.0)
        hist = np.histogram(p, bins)[0].astype(float)
        win = np.convolve(hist, np.ones(5), "same")
        centers = (bins[:-1] + bins[1:]) / 2
        peaks = [i for i in range(len(win)) if win[i] >= min_share * total
                 and win[i] == win[max(0, i - 3):i + 4].max()]
        for side in (1, -1):
            sp = [i for i in peaks if side * centers[i] > 50]
            if not sp:
                continue
            i = max(sp, key=lambda i: side * centers[i])
            c0 = centers[i]
            inl = np.abs(p - c0) < 3 + 0.01 * dist
            c0 = float(np.median(p[inl]))
            inl = np.abs(p - c0) < 3 + 0.01 * dist
            n2 = -side * dvec  # into the room
            off = side * c0
            t = np.array([-n2[1], n2[0]])
            s = xz[inl] @ t
            foot = -off * n2
            a, b = np.percentile(s, 2), np.percentile(s, 98)
            if off < 50 or b - a < 40:  # clutter, not a wall
                continue
            walls.append({"normal": n2, "offset": off, "from": foot + a * t, "to": foot + b * t,
                          "support": int(inl.sum())})
    return walls, sum(w["support"] for w in walls)


def _wall_label(n) -> str:
    if n[1] < -0.7:
        return "hinten"
    if n[0] > 0.7:
        return "links"
    if n[0] < -0.7:
        return "rechts"
    return "wand"


def _fit_ceiling(pts_f: np.ndarray, vrel: np.ndarray, n: np.ndarray, pts: np.ndarray):
    up_sel = vrel < 0.35
    hgt = pts_f[up_sel, 1]
    cand = (hgt > 200) & (hgt < 450)
    if cand.sum() < 50 or cand.sum() < 0.08 * max(1, up_sel.sum()):
        return None
    hist, bins = np.histogram(hgt[cand], np.arange(200, 454, 4.0))
    win = np.convolve(hist, np.ones(3), "same")
    h0 = (bins[int(win.argmax())] + 2)
    P, Z = pts[up_sel], pts[up_sel, 2]
    inl = np.abs(hgt - h0) < 3 + 0.01 * Z
    if inl.sum() < max(50, 0.08 * up_sel.sum()):
        return None
    cn, _ = _plane_svd(P[inl])
    if abs(float(cn @ n)) < math.cos(math.radians(10)):
        return None
    h = float(np.median(hgt[inl]))
    return h if 200 <= h <= 450 else None


# ---------------- lines & vanishing points ----------------

def _segments(img: Image.Image) -> np.ndarray:
    """Line segments (x1, y1, x2, y2) in image pixels, ≥ 3 % of the diagonal."""
    import cv2

    W, H = img.size
    k = min(1.0, 1280 / max(W, H))
    g = np.asarray(img.convert("L").resize((max(1, round(W * k)), max(1, round(H * k))), Image.LANCZOS))
    segs = None
    try:
        lines = cv2.createLineSegmentDetector().detect(g)[0]
        segs = lines.reshape(-1, 4) if lines is not None else np.zeros((0, 4))
    except Exception:
        edges = cv2.Canny(g, 60, 160)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 360, 60, minLineLength=int(0.03 * math.hypot(*g.shape)), maxLineGap=6)
        segs = lines.reshape(-1, 4) if lines is not None else np.zeros((0, 4))
    segs = segs.astype(np.float64) / k
    L = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    return segs[L >= 0.03 * math.hypot(W, H)]


def _vp_ransac(segs: np.ndarray, iters: int = 500, tol_deg: float = 2.0, seed: int = 2):
    """→ (homogeneous VP, inlier mask) or (None, None)."""
    if len(segs) < 3:
        return None, None
    p1 = np.c_[segs[:, :2], np.ones(len(segs))]
    p2 = np.c_[segs[:, 2:], np.ones(len(segs))]
    lines = np.cross(p1, p2)
    lines /= np.linalg.norm(lines[:, :2], axis=1, keepdims=True)
    mid = (segs[:, :2] + segs[:, 2:]) / 2
    sdir = segs[:, 2:] - segs[:, :2]
    length = np.linalg.norm(sdir, axis=1)
    sdir /= length[:, None]
    cos_tol = math.cos(math.radians(tol_deg))

    def inliers(v):
        to = v[:2] - mid * v[2]
        nrm = np.linalg.norm(to, axis=1)
        return (nrm > 1e-9) & (np.abs(np.einsum("ij,ij->i", to, sdir)) >= cos_tol * np.maximum(nrm, 1e-9))

    rng = np.random.default_rng(seed)
    best, best_score = None, 0.0
    for _ in range(iters):
        i, j = rng.choice(len(segs), 2, replace=False)
        v = np.cross(lines[i], lines[j])
        if np.linalg.norm(v) < 1e-12:
            continue
        v = v / np.linalg.norm(v)
        score = length[inliers(v)].sum()
        if score > best_score:
            best, best_score = v, score
    if best is None:
        return None, None
    m = inliers(best)
    if m.sum() < 3:
        return None, None
    A = lines[m] * length[m, None]
    v = np.linalg.svd(A)[2][-1]
    m2 = inliers(v)
    return (v, m2) if m2.sum() >= m.sum() else (best, m)


def _vp_dir(v, K) -> np.ndarray:
    d = np.array([(v[0] - K["cx"] * v[2]) / K["fx"], (v[1] - K["cy"] * v[2]) / K["fy"], v[2]])
    return _unit(d)


def _vp_point(v, size):
    if abs(v[2]) < 1e-9:
        return None
    u, w = v[0] / v[2], v[1] / v[2]
    if abs(u) > 100 * size[0] or abs(w) > 100 * size[1]:
        return None
    return [round(float(u), 1), round(float(w), 1)]


def _vanishing(segs: np.ndarray, K: dict, size):
    """{"vertical","h1","h2"}: (homogeneous VP, 3D direction) or None."""
    out = {"vertical": None, "h1": None, "h2": None}
    if len(segs) < 3:
        return out
    ang = np.degrees(np.arctan2(np.abs(segs[:, 2] - segs[:, 0]), np.abs(segs[:, 3] - segs[:, 1])))
    vert = ang < 25
    rest = np.ones(len(segs), bool)
    if vert.sum() >= 3:
        v, m = _vp_ransac(segs[vert])
        if v is not None:
            d = _vp_dir(v, K)
            if d[1] > 0:
                d = -d
            if d @ np.array([0, -1.0, 0]) >= math.cos(math.radians(45)):
                out["vertical"] = (v, d)
                idx = np.flatnonzero(vert)[m]
                rest[idx] = False
    rest &= ~vert
    for key in ("h1", "h2"):
        if rest.sum() < 4:
            break
        v, m = _vp_ransac(segs[rest], seed=3 if key == "h1" else 4)
        if v is None or m.sum() < 4:
            break
        out[key] = (v, _vp_dir(v, K))
        rest[np.flatnonzero(rest)[m]] = False
    return out


# ---------------- analysis ----------------

def fit_scene(depth: np.ndarray, f_room: float, size, segs: np.ndarray | None = None) -> dict:
    """Floor, walls, ceiling, horizon and VPs from a depth map (cm, any resolution with the room image's aspect)
    and the room image's focal length. Returns the metric part of a Geometry (no IO, no model)."""
    W, H = size
    h, w = depth.shape
    K = {"fx": float(f_room), "fy": float(f_room), "cx": W / 2, "cy": H / 2}
    pts, vrel = _backproject(depth, f_room * w / W)
    vps = _vanishing(segs if segs is not None else np.zeros((0, 4)), K, size)
    res = {"intrinsics": K, "floor": None, "frame": None, "walls": [], "ceiling": None, "horizon": None,
           "vps": {k: (_vp_point(v[0], size) if v else None) for k, v in vps.items()}, "error": None}
    if len(pts):
        res["depth_range"] = [round(float(np.percentile(pts[:, 2], 5)), 1), round(float(np.percentile(pts[:, 2], 95)), 1)]
    hints = ([vps["vertical"][1]] if vps["vertical"] else []) + [np.array([0.0, -1.0, 0.0])]
    fl = None
    for hint in hints:
        fl = _fit_floor(pts, vrel, hint) if len(pts) else None
        if fl and fl[2] >= 0.05:
            break
    if not fl or fl[2] < 0.05:
        res["error"] = NO_FLOOR
        return res
    n, d, share = fl
    fr = {k: np.asarray(v) for k, v in frame_of(n, d).items()}
    res["floor"] = {"normal": [round(float(c), 6) for c in n], "d": round(d, 2), "inliers": round(share, 3)}
    res["frame"] = frame_of(res["floor"]["normal"], res["floor"]["d"])
    pf = _floor_coords(pts, fr)
    thr = 3 + 0.01 * pts[:, 2]
    nonfloor = np.abs(pf[:, 1]) > thr
    mid = nonfloor & (pf[:, 1] > 30) & (pf[:, 1] < 180)
    xz = pf[mid][:, [0, 2]]
    cands = []
    for key in ("h1", "h2"):
        if vps[key]:
            dv = vps[key][1]
            d2 = np.array([dv @ fr["right"], dv @ fr["forward"]])
            if np.linalg.norm(d2) > 0.3:
                d2 = _unit(d2)
                cands.append((d2, np.array([-d2[1], d2[0]])))
    rd = _wall_dirs_ransac(xz)
    if rd is not None:
        rd = _unit(rd)
        cands.append((rd, np.array([-rd[1], rd[0]])))
    best = ([], 0)
    for pair in cands:
        found = _find_walls(xz, pair)
        if found[1] > best[1]:
            best = found
    walls = sorted(best[0], key=lambda w: -w["support"])[:4]
    res["walls"] = [{"id": f"w{i}", "label": _wall_label(w["normal"]),
                     "normal": [round(float(c), 6) for c in w["normal"]], "offset": round(float(w["offset"]), 2),
                     "from": [round(float(c), 1) for c in w["from"]], "to": [round(float(c), 1) for c in w["to"]]}
                    for i, w in enumerate(walls)]
    ch = _fit_ceiling(pf, vrel, n, pts)
    res["ceiling"] = {"height": round(ch, 1)} if ch else None
    if abs(n[1]) > 1e-6:
        hv = lambda u: K["cy"] - K["fy"] * (n[2] + n[0] * (u - K["cx"]) / K["fx"]) / n[1]
        res["horizon"] = [[0.0, round(float(hv(0)), 2)], [float(W), round(float(hv(W)), 2)]]
    return res


def _confidence(geo: dict, focal_ok: bool) -> str:
    fl = geo.get("floor")
    if not fl:
        return "low"
    c = "high" if fl["inliers"] >= 0.2 and geo["walls"] and 80 <= fl["d"] <= 220 else "medium"
    return "medium" if c == "high" and not focal_ok else c


def _preview(depth: np.ndarray, path: Path) -> None:
    import cv2

    lo, hi = np.percentile(depth, 2), np.percentile(depth, 98)
    g = (255 * (1 - np.clip((depth - lo) / max(hi - lo, 1e-6), 0, 1))).astype(np.uint8)  # near = warm
    col = cv2.applyColorMap(g, cv2.COLORMAP_TURBO)
    h, w = g.shape
    k = 640 / max(h, w)
    col = cv2.resize(col, (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), col, [cv2.IMWRITE_JPEG_QUALITY, 85])


def _empty(room_rel: str, size, error: str) -> dict:
    W, H = size
    f = 0.8 * max(W, H)
    return {"version": 1, "ok": False, "error": error, "room": room_rel, "size": [W, H],
            "intrinsics": {"fx": f, "fy": f, "cx": W / 2, "cy": H / 2}, "floor": None, "frame": None, "walls": [],
            "ceiling": None, "horizon": None, "vps": {"vertical": None, "h1": None, "h2": None}, "scale": 1.0,
            "depth_range": None, "depth_preview": None, "confidence": "low", "seconds": 0.0}


def analyze(img: Image.Image, room_rel: str, force: bool = False, geo_dir: Path | None = None) -> dict:
    t0 = time.time()
    old = load_cached(room_rel, geo_dir)
    if old and not force:
        return old
    size = img.size
    try:
        img = rgb(img)
        size = img.size
        depth, f, focal_ok = _estimate(img)
    except Exception as e:  # model/GPU trouble: report, do not cache
        geo = _empty(room_rel, size, FAILED)
        geo["seconds"] = round(time.time() - t0, 2)
        print(f"geometry: depth failed: {e!r}")
        return geo
    geo = _empty(room_rel, size, None)
    try:
        d = _dir(geo_dir)
        d.mkdir(parents=True, exist_ok=True)
        key = _key(room_rel)
        h, w = depth.shape
        k = NPY_SIDE / max(h, w)
        small = np.asarray(Image.fromarray(depth).resize((max(1, round(w * k)), max(1, round(h * k))), Image.BILINEAR))
        np.save(d / f"{key}-depth.npy", small.astype(np.float16))
        _preview(depth, d / f"{key}-depth.jpg")
        geo["depth_preview"] = f"geometry/{key}-depth.jpg"
        try:
            segs = _segments(img)
        except Exception as e:
            print(f"geometry: line detection failed: {e!r}")
            segs = None
        fit = fit_scene(depth, f, size, segs)
        geo.update({k: v for k, v in fit.items() if k != "error"})
        geo["ok"] = fit["floor"] is not None
        geo["error"] = fit["error"]
    except Exception as e:
        print(f"geometry: fitting failed: {e!r}")
        geo.update(_empty(room_rel, size, FAILED), depth_preview=geo.get("depth_preview"))
    geo["confidence"] = _confidence(geo, focal_ok)
    if old and geo["ok"]:  # re-analysis keeps the stored calibration and a manual ceiling
        if (old.get("scale") or 1.0) != 1.0:
            geo = rescale(geo, old["scale"])
        if (old.get("ceiling") or {}).get("manual"):
            geo["ceiling"] = old["ceiling"]
    geo["seconds"] = round(time.time() - t0, 2)
    if geo["ok"] or not (old and old.get("ok")):  # a failed re-analysis never replaces a good measurement + calibration
        save_cached(geo, geo_dir)
    return geo
