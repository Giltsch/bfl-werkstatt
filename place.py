"""Server-side placement: an exact Python port of the W3D math in static/werkstatt3d.js that a tap in step ③ uses, so
the agent can place a piece from an image point and store the same `placement` the UI would.

Image coords are room-image pixels; floor coords x right, y up, z forward (cm); grid boxes are [top, left, bottom, right]
on 0–1000. The arithmetic follows the JS expression by expression (same operation order, multiply-by-reciprocal in
`_unit`, JS rounding), so both sides give the same floats; tests/test_place.py cross-checks against node.
The geometry dict already carries the calibration (geometry.rescale bakes the scale into floor.d and the walls), so a
scene needs no extra scale; `scale_poses` mirrors W3D.scalePoses for adopting a new calibration."""
import copy
import math

NEAR, MIN_Z, SNAP_CM, SNAP_DEG, COVER_CM = 10, 50, 30, 10, 50  # same constants as werkstatt3d.js
EDGES = ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7))
DEF_DIMS = (100, 100, 60)
INF = float("inf")


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _mul(a, k):
    return [a[0] * k, a[1] * k, a[2] * k]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _unit(a):
    return _mul(a, 1 / math.hypot(a[0], a[1], a[2]))


def _rad(d):
    return d * math.pi / 180


def _deg(r):
    return r * 180 / math.pi


def _wrap(a):  # JS ((a + 180) % 360 + 360) % 360 - 180; Python's % already gives the non-negative remainder
    return math.fmod(math.fmod(a + 180, 360) + 360, 360) - 180


def _clamp(x, a, b):
    return max(a, min(b, x))


def js_round(x: float) -> int:
    """Math.round: halves go up (Python's round() goes to even)."""
    return math.floor(x + 0.5)


def _num(v) -> float:
    """JS unary plus for the values a piece carries (numbers, numeric strings, null) — NaN when not a number."""
    if v is None or v is False:
        return 0.0
    if v is True:
        return 1.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return math.nan
    return math.nan


def frame_of(normal, d) -> dict:
    n = _unit(normal)
    fwd = _unit(_sub([0, 0, 1], _mul(n, n[2])))
    return {"origin": _mul(n, -d), "right": _cross(fwd, n), "up": n, "forward": fwd}


def hull_of(points) -> list:
    """Andrew's monotone chain, counter-clockwise in (u, v), no repeated end point."""
    seen, pts = set(), []
    for p in points:
        k = (p[0], p[1])
        if k not in seen:
            seen.add(k)
            pts.append([p[0], p[1]])
    pts.sort(key=lambda p: (p[0], p[1]))
    if len(pts) <= 2:
        return pts

    def cr(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cr(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cr(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def box_to_grid(hull, size) -> list | None:
    if not hull:
        return None
    W, H = size
    us, vs = [p[0] for p in hull], [p[1] for p in hull]
    umin, umax = _clamp(min(us), 0, W), _clamp(max(us), 0, W)
    vmin, vmax = _clamp(min(vs), 0, H), _clamp(max(vs), 0, H)
    c = lambda x: _clamp(x, 0, 1000)  # noqa: E731
    return [c(math.floor(1000 * vmin / H)), c(math.floor(1000 * umin / W)),
            c(math.ceil(1000 * vmax / H)), c(math.ceil(1000 * umax / W))]


def to_grid(p, size) -> list:
    """Image point (u, v) → grid [top, left]."""
    return [_clamp(js_round(p[1] / size[1] * 1000), 0, 1000), _clamp(js_round(p[0] / size[0] * 1000), 0, 1000)]


def from_grid(point_grid, size) -> list:
    """Grid [top, left] (0–1000) → image point (u, v) — the inverse step-platzieren.js uses for its segment tap."""
    return [point_grid[1] / 1000 * size[0], point_grid[0] / 1000 * size[1]]


def scale_poses(pieces, k: float) -> None:
    """In place, like W3D.scalePoses: a new calibration scales every 3d pose's floor position by k."""
    for p in pieces or []:
        pl = p.get("placement") if isinstance(p, dict) else None
        pose = pl and pl.get("mode") == "3d" and pl.get("pose")
        if pose:
            pose["x"] *= k
            pose["z"] *= k


def pose_of(piece) -> dict | None:
    pl = piece.get("placement") if isinstance(piece, dict) else None
    return pl["pose"] if pl and pl.get("mode") == "3d" and pl.get("pose") else None


def default_catalog() -> dict:
    import workshop  # lazy: workshop pulls in the web stack
    return workshop.CATALOG


class Scene:
    """W3D.init(geometry, catalog). Build with `init()` (None without a usable measurement)."""

    def __init__(self, geometry: dict, catalog: dict | None = None):
        K = geometry["intrinsics"]
        self.geometry = geometry
        self.K = {k: float(K[k]) for k in ("fx", "fy", "cx", "cy")}
        self.d = geometry["floor"]["d"]
        self.frame = fr = frame_of(geometry["floor"]["normal"], self.d)
        self.walls = list(geometry.get("walls") or [])
        self.size = list(geometry["size"])
        self.ceiling = geometry.get("ceiling") or None
        self.types = (catalog or {}).get("pieces") or {}
        self._camF = [-_dot(fr["origin"], fr["right"]), -_dot(fr["origin"], fr["up"]), -_dot(fr["origin"], fr["forward"])]

    # ---- camera ----
    def cam(self, p):
        fr = self.frame
        return _add(_add(_add(fr["origin"], _mul(fr["right"], p[0])), _mul(fr["up"], p[1])), _mul(fr["forward"], p[2]))

    def proj(self, X):
        K = self.K
        return [K["fx"] * X[0] / X[2] + K["cx"], K["fy"] * X[1] / X[2] + K["cy"]]

    def ray(self, u, v):
        K = self.K
        return [(u - K["cx"]) / K["fx"], (v - K["cy"]) / K["fy"], 1]

    # ---- walls ----
    @staticmethod
    def wall_dist(w, x, z):
        return w["normal"][0] * x + w["normal"][1] * z + w["offset"]

    @staticmethod
    def wall_yaw(w):
        return _deg(math.atan2(w["normal"][0], -w["normal"][1]))

    @staticmethod
    def covers(w, x, z):
        if not w.get("from") or not w.get("to"):
            return True
        ax, az = w["to"][0] - w["from"][0], w["to"][1] - w["from"][1]
        L = math.hypot(ax, az)
        if L < 1e-6:
            return True
        s = ((x - w["from"][0]) * ax + (z - w["from"][1]) * az) / L
        return -COVER_CM <= s <= L + COVER_CM

    def wall_by_id(self, wall_id):
        return next((w for w in self.walls if w.get("id") == wall_id), None)

    # ---- pieces ----
    def dims(self, piece) -> list:
        t = self.types.get((piece or {}).get("type")) if piece else None
        default = (t and t.get("dims")) or DEF_DIMS
        out = []
        for i, k in enumerate(("w", "h", "d")):
            v = _num(piece.get(k)) if piece else math.nan
            out.append(v if v > 0 else default[i])
        return out

    @staticmethod
    def elev(piece) -> float:
        """cm between floor and the underside (wall-hung pieces)."""
        v = _num((piece or {}).get("elev"))
        return max(0, v if v == v and v else 0)  # +x || 0 maps NaN and 0 to 0

    def floor_point(self, u, v) -> dict | None:
        fr = self.frame
        r = self.ray(u, v)
        nr = _dot(fr["up"], r)
        if nr >= 0:
            return None
        t = -self.d / nr
        if t <= 0:
            return None
        X = _sub(_mul(r, t), fr["origin"])
        return {"x": _dot(X, fr["right"]), "z": _dot(X, fr["forward"])}

    def to_image(self, p):
        X = self.cam(p)
        return None if X[2] < NEAR else self.proj(X)

    def box_corners(self, piece, pose=None) -> list:
        pose = pose or pose_of(piece)
        W, H, D = self.dims(piece)
        th = _rad(pose.get("yaw") or 0)
        y0 = self.elev(piece)
        ex, ez, out = (math.cos(th), math.sin(th)), (-math.sin(th), math.cos(th)), []
        for i in range(8):
            sx, sz = (1 if i & 1 else -1), (1 if i & 2 else -1)
            y = y0 + (H if i & 4 else 0)
            out.append([pose["x"] + sx * W / 2 * ex[0] + sz * D / 2 * ez[0], y,
                        pose["z"] + sx * W / 2 * ex[1] + sz * D / 2 * ez[1]])
        return out

    def project_box(self, piece, pose=None) -> dict:
        C = [self.cam(p) for p in self.box_corners(piece, pose)]
        corners = [self.proj(X) if X[2] >= NEAR else None for X in C]
        pts = [c for c in corners if c is not None]
        for a, b in EDGES:
            za, zb = C[a][2], C[b][2]
            if (za - NEAR) * (zb - NEAR) < 0:
                pts.append(self.proj(_add(C[a], _mul(_sub(C[b], C[a]), (NEAR - za) / (zb - za)))))
        hull = hull_of(pts)
        box = box_to_grid(hull, self.size)
        # at/behind the camera the clipped hull overflows the photo: not a usable placement (mirrors geometry.py)
        nearest = min(X[2] for X in C)
        visible = bool(hull and box and box[2] > box[0] and box[3] > box[1]) and nearest >= MIN_Z
        return {"corners": corners, "hull": hull, "box": box, "visible": visible}

    def nearest_wall(self, x, z) -> dict | None:
        best = None
        for w in self.walls:
            if not self.covers(w, x, z):
                continue
            dist = self.wall_dist(w, x, z)
            if best is None or abs(dist) < abs(best["dist"]):
                best = {"wall": w, "dist": dist}
        return best

    def snap_to_wall(self, pose, piece, wall_id=None) -> dict | None:
        D = self.dims(piece)[2]
        w = None
        if wall_id is not None:
            w = self.wall_by_id(wall_id)
        else:
            best_err = SNAP_CM
            for c in self.walls:
                if not self.covers(c, pose["x"], pose["z"]):
                    continue
                err = abs(self.wall_dist(c, pose["x"], pose["z"]) - D / 2)
                if err < best_err:
                    best_err, w = err, c
        if w is None:
            return None
        k = D / 2 - self.wall_dist(w, pose["x"], pose["z"])
        return {"x": pose["x"] + k * w["normal"][0], "z": pose["z"] + k * w["normal"][1], "yaw": self.wall_yaw(w),
                "wall": w["id"]}

    def centre_at(self, u, v, piece, yaw=0) -> dict | None:
        """Floor point whose box centre (at elev + H/2) projects onto the tap (u, v) — people tap the middle of the spot,
        often onto old furniture where no floor is visible. Newton on the floor-image point, numeric Jacobian."""
        H, y0, size = self.dims(piece)[1], self.elev(piece), self.size

        def centre(q):
            f = self.floor_point(q[0], q[1])
            c = f and self.to_image([f["x"], y0 + H / 2, f["z"]])
            return {"f": f, "c": c} if c else None

        q, best = [u, v], None
        # a wall-hung or tall piece is tapped above the horizon: start the search on the visible floor below the tap
        while not self.floor_point(q[0], q[1]) and q[1] < size[1] * 2:
            q = [q[0], q[1] + size[1] * 0.02]
        err = INF
        for _ in range(30):
            r = centre(q)
            if not r:
                q = [q[0], q[1] + size[1] * 0.02]
                continue
            best = r["f"]
            e = [u - r["c"][0], v - r["c"][1]]
            err = math.hypot(e[0], e[1])
            if abs(e[0]) + abs(e[1]) < 0.25:
                break
            ru, rv = centre([q[0] + 1, q[1]]), centre([q[0], q[1] + 1])
            if not ru or not rv:
                q = [q[0] + e[0], q[1] + e[1]]
                continue
            a, b = ru["c"][0] - r["c"][0], rv["c"][0] - r["c"][0]
            c, d = ru["c"][1] - r["c"][1], rv["c"][1] - r["c"][1]
            det = a * d - b * c
            if abs(det) < 1e-9:
                break
            q = [q[0] + (d * e[0] - b * e[1]) / det, q[1] + (a * e[1] - c * e[0]) / det]
        # no floor spot puts the middle there (e.g. far above the horizon for a low piece): not a placement
        return best if best and err <= 0.03 * max(size[0], size[1]) else None

    def wall_hit(self, u, v) -> dict | None:
        """The nearest wall the tap ray meets: {t, wall, x, y, z} in floor coordinates (y above the floor)."""
        fr, camF = self.frame, self._camF
        r = self.ray(u, v)
        dr = [_dot(r, fr["right"]), _dot(r, fr["up"]), _dot(r, fr["forward"])]
        best = None
        for w in self.walls:
            den = w["normal"][0] * dr[0] + w["normal"][1] * dr[2]
            if abs(den) < 1e-9:
                continue
            t = -(w["normal"][0] * camF[0] + w["normal"][1] * camF[2] + w["offset"]) / den
            if t <= 0:
                continue
            P = [camF[0] + t * dr[0], camF[1] + t * dr[1], camF[2] + t * dr[2]]
            if P[1] < 0 or not self.covers(w, P[0], P[2]) or (best and best["t"] <= t):
                continue
            best = {"t": t, "wall": w, "x": P[0], "y": P[1], "z": P[2]}
        return best

    def hang(self, u, v, piece) -> dict | None:
        """Wall-hung pieces: a tap on a wall hangs the piece there, its middle at the tapped height (elev = new elevation)."""
        hit = self.wall_hit(u, v)
        if not hit:
            return None
        _, H, D = self.dims(piece)
        w = hit["wall"]
        return {"x": hit["x"] + w["normal"][0] * D / 2, "z": hit["z"] + w["normal"][1] * D / 2, "yaw": self.wall_yaw(w),
                "wall": w["id"], "elev": max(0, js_round(hit["y"] - H / 2))}

    def place(self, u, v, piece, yaw=0) -> dict | None:
        if self.elev(piece) > 0:
            hp = self.hang(u, v, piece)
            if hp:
                return hp
        fp = self.centre_at(u, v, piece, yaw)
        if not fp:
            return None
        D, nw = self.dims(piece)[2], self.nearest_wall(fp["x"], fp["z"])
        if nw and nw["dist"] - D / 2 < SNAP_CM:
            return self.snap_to_wall({"x": fp["x"], "z": fp["z"], "yaw": yaw, "wall": None}, piece, nw["wall"]["id"])
        return {"x": fp["x"], "z": fp["z"], "yaw": yaw, "wall": None}

    def distance(self, pose) -> float:
        """Camera → piece distance on the floor in cm."""
        X = self.cam([pose["x"], 0, pose["z"]])
        return math.hypot(X[0], X[2])

    def relations(self, piece) -> dict:
        pose, (W, H, D) = pose_of(piece), self.dims(piece)
        ch = self.ceiling.get("height") if self.ceiling else None
        top = self.elev(piece) + H
        out = {"ceilingHeight": ch, "ceilingRatio": top / ch if ch else None, "wallRatio": None, "wallLength": None,
               "wallLabel": None, "distance": None, "tooHigh": bool(ch and top > ch), "tooWide": False, "outOfImage": False}
        if not pose:
            return out
        w = self.wall_by_id(pose.get("wall"))
        if w is None:
            best_err = SNAP_CM
            for c in self.walls:
                if not self.covers(c, pose["x"], pose["z"]):
                    continue
                err = abs(self.wall_dist(c, pose["x"], pose["z"]) - D / 2)
                if err < best_err:
                    best_err, w = err, c
        if w is not None:
            out["wallLabel"] = w.get("label")
            if w.get("from") and w.get("to"):
                out["wallLength"] = math.hypot(w["to"][0] - w["from"][0], w["to"][1] - w["from"][1])
                out["wallRatio"] = W / out["wallLength"] if out["wallLength"] > 0 else None
                out["tooWide"] = bool(out["wallRatio"] and out["wallRatio"] > 1)
        out["distance"] = math.hypot(pose["x"], pose["z"])
        out["outOfImage"] = not self.project_box(piece)["visible"]
        return out


def init(geometry: dict | None, catalog: dict | None = None) -> Scene | None:
    """W3D.init: None without a usable measurement."""
    g = geometry
    if not g or not g.get("ok") or not g.get("floor") or not g.get("intrinsics") or not g.get("size"):
        return None
    return Scene(g, catalog)


def scene_for(doc: dict, catalog: dict | None = None) -> Scene | None:
    """editor.js sceneFor: a scene only for an order with a room photo and a usable measurement."""
    if not doc or not doc.get("room"):
        return None
    return init(doc.get("geometry"), default_catalog() if catalog is None else catalog)


def reprojected(scene: Scene, piece: dict, pl: dict) -> dict:
    """editor.js reprojected: wall poses re-snap (depth may differ), the box comes from the projection (None = not visible)."""
    if not scene or not pl or pl.get("mode") != "3d" or not pl.get("pose"):
        return pl
    pose = pl["pose"]
    if pose.get("wall"):
        pose = scene.snap_to_wall(pose, piece, pose["wall"]) or pose
    pr = scene.project_box(piece, pose)
    return {"mode": "3d", "pose": pose, "box": pr["box"] if pr["visible"] else None}


def _wall_hung(piece: dict, catalog: dict) -> float:
    """Catalog elevation of a wall-hung type (Wandregal, Hängeschrank), 0 for floor-standing ones."""
    t = ((catalog or {}).get("pieces") or {}).get(piece.get("type")) or {}
    return t.get("elev") or 0


def placement_for(doc: dict, piece: dict | str, point_grid, against_wall: bool | None = None, yaw=None,
                  catalog: dict | None = None) -> dict | None:
    """The `placement` step ③ stores after a tap at point_grid ([top, left] on 0–1000) for this piece, or None when the UI
    would ignore the tap (no scene, above the horizon, box not visible).

    against_wall None = exactly the tap (snaps when the back is within 30 cm of a wall; wall-hung pieces hang on the wall
    hit by the tap); True = additionally put it against the nearest wall like „An die Wand“ (None if no wall fits);
    False = never snap (free pose; a wall-hung piece floats at its elevation). yaw None keeps the piece's current turn.
    A wall-hung piece without an elev (e.g. fresh from intent.py) gets its catalog elevation, as step ② would set.
    Returns {"mode": "3d", "pose": {x, z, yaw, wall}, "box": [t, l, b, r]} plus "elev" (int) when the tap hung the piece
    at a new height — that one goes to pieces.<id>.elev, not into the placement (use `tap_ops`)."""
    catalog = default_catalog() if catalog is None else catalog
    if isinstance(piece, str):
        piece = next((p for p in doc.get("pieces") or [] if p.get("id") == piece), None)
    scene = scene_for(doc, catalog)
    if not scene or not piece or not point_grid or len(point_grid) != 2:
        return None
    piece, filled = dict(piece), None
    if piece.get("elev") is None and _wall_hung(piece, catalog):
        piece["elev"] = filled = _wall_hung(piece, catalog)
    u, v = from_grid(point_grid, scene.size)
    old = pose_of(piece)
    y = (old.get("yaw") or 0) if yaw is None and old else (yaw or 0)
    if against_wall is False:
        fp = scene.centre_at(u, v, piece, y)
        pose = {"x": fp["x"], "z": fp["z"], "yaw": y, "wall": None} if fp else None
    else:
        pose = scene.place(u, v, piece, y)
    if not pose:
        return None
    elev = pose.pop("elev", None)
    shown = {**piece, "elev": elev} if elev is not None else piece
    if not scene.project_box(shown, pose)["visible"]:  # the UI ignores such a tap
        return None
    out = reprojected(scene, shown, {"mode": "3d", "pose": pose, "box": None})
    if against_wall:  # then „An die Wand“ (step-platzieren.js toWall) on the committed pose
        nw = scene.nearest_wall(out["pose"]["x"], out["pose"]["z"])
        s = nw and scene.snap_to_wall(out["pose"], shown, nw["wall"]["id"])
        out = s and reprojected(scene, shown, {"mode": "3d", "pose": s, "box": None})
        if not out or not out["box"]:
            return None
    out = {"mode": "3d", "pose": dict(out["pose"]), "box": out["box"]}
    if elev is None and filled:
        elev = filled  # the catalog elevation must be stored too, or the generated box would stand on the floor
    if elev is not None:
        out["elev"] = int(elev)
    return out


def tap_ops(doc: dict, piece: dict | str, placement: dict, catalog: dict | None = None) -> list:
    """The ops step-platzieren.js commit() sends for a tap: the placement, the elevation when it changed, and the
    catalog dims for every dimension the piece does not have yet. Feed them to projects.apply_ops."""
    catalog = default_catalog() if catalog is None else catalog
    if isinstance(piece, str):
        piece = next((p for p in doc.get("pieces") or [] if p.get("id") == piece), None)
    if not piece or not placement:
        return []
    pid, pl = piece["id"], {k: copy.deepcopy(placement[k]) for k in ("mode", "pose", "box")}
    ops = [{"op": "set", "path": f"pieces.{pid}.placement", "value": pl}]
    elev = placement.get("elev")
    if elev is not None and elev != (_num(piece.get("elev")) or 0):
        ops.append({"op": "set", "path": f"pieces.{pid}.elev", "value": elev})
    scene = scene_for(doc, catalog)
    if scene and pl["mode"] == "3d":
        d = scene.dims(piece)
        for i, k in enumerate(("w", "h", "d")):
            if not _num(piece.get(k)) > 0:
                ops.append({"op": "set", "path": f"pieces.{pid}.{k}", "value": js_round(d[i])})
    return ops
