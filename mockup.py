"""A geometric mock-up of the planned furniture, drawn into the room photo: every piece as shaded blocks in its measured
3d pose (z-buffered, so pieces hide each other and things standing in front of them), divided into the parts a carpenter
builds (carcass, plinth, doors, drawers, shelves, worktop, legs). FLUX gets it as a reference image, so size, position,
perspective and division come from the geometry instead of being guessed."""

import math
import re

import numpy as np
from PIL import Image, ImageDraw

import geometry

NEAR = 10.0
FACES = ((0, 2, 6, 4), (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6), (0, 1, 3, 2), (4, 6, 7, 5))  # -x, +x, -z, +z, bottom, top
TONES = {"body": 1.0, "front": 1.12, "top": 1.05, "board": 1.08, "dark": 0.45, "soft": 1.25}
WOOD = {"eiche": (196, 160, 112), "eicheraeuchert": (120, 88, 60), "eicheweiss": (222, 208, 186), "nuss": (118, 82, 56),
        "esche": (214, 190, 150), "kirsch": (170, 100, 70), "buche": (210, 168, 120), "multiplex": (220, 196, 150),
        "altholz": (140, 110, 80), "weiss": (236, 234, 228), "anthrazit": (70, 72, 76)}
NUM = {"ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5, "sechs": 6}
LIGHT = np.array([-0.35, -0.75, -0.55]) / np.linalg.norm([-0.35, -0.75, -0.55])


def _count(text: str, words: str):
    m = re.search(rf"(\d+|{'|'.join(NUM)})\s*-?\s*(?:{words})", text or "", re.I)
    if not m:
        return None
    v = m.group(1).lower()
    return max(1, min(12, int(v) if v.isdigit() else NUM[v]))


def _fronts(x0, x1, y0, y1, zf, n, horizontal=False, gap=0.5, t=1.8):
    """n door/drawer leaves on the front plane z = zf (local front is -z), separated by gaps."""
    out = []
    for i in range(n):
        if horizontal:
            a, b = y0 + (y1 - y0) * i / n, y0 + (y1 - y0) * (i + 1) / n
            out.append((x0 + gap, x1 - gap, a + gap, b - gap, zf - t, zf, "front"))
        else:
            a, b = x0 + (x1 - x0) * i / n, x0 + (x1 - x0) * (i + 1) / n
            out.append((a + gap, b - gap, y0 + gap, y1 - gap, zf - t, zf, "front"))
    return out


def parts(piece: dict, dims) -> list[tuple]:
    """Blocks (x0, x1, y0, y1, z0, z1, tone) in the piece frame: x across the width, y up from the floor (elevation
    included), z through the depth with the FRONT at z = -D/2 (the back faces the wall)."""
    W, H, D = (float(v) for v in dims)
    e = float(piece.get("elev") or 0)
    t, text = piece.get("type"), piece.get("text") or ""
    x0, x1, zf, zb, y0, y1 = -W / 2, W / 2, -D / 2, D / 2, e, e + H
    doors = _count(text, r"t(?:ü|ue)r(?:en|ig)?|fl(?:ü|ue)gel")
    drawers = _count(text, r"schubl\w*|schubk\w*|ausz(?:ü|ue)g\w*")
    out = []
    if t in ("schrank", "garderobe", "haengeschrank", "sideboard", "bad"):
        plinth = 8.0 if t in ("schrank", "sideboard", "garderobe") and e == 0 else 0.0
        if plinth:
            out.append((x0 + 1, x1 - 1, y0, y0 + plinth, zf + 5, zb, "dark"))
        top = 2.0 if t == "sideboard" else 0.0
        out.append((x0, x1, y0 + plinth, y1 - top, zf + 1.8, zb, "body"))
        if top:
            out.append((x0 - 1, x1 + 1, y1 - top, y1, zf - 1, zb, "top"))
        fy0, fy1 = y0 + plinth, y1 - top
        if t == "bad" or (drawers and not doors):
            out += _fronts(x0, x1, fy0, fy1, zf + 1.8, drawers or 2, horizontal=True)
        else:
            per = 50.0 if t in ("schrank", "garderobe") else 45.0
            out += _fronts(x0, x1, fy0, fy1, zf + 1.8, doors or max(1, round(W / per)))
    elif t == "kueche":
        base_h = min(H, 90.0)
        out.append((x0 + 1, x1 - 1, y0, y0 + 10, zf + 6, zb, "dark"))
        out.append((x0, x1, y0 + 10, y0 + base_h - 4, zf + 1.8, zb, "body"))
        out += _fronts(x0, x1, y0 + 10, y0 + base_h - 4, zf + 1.8, max(1, round(W / 60)))
        out.append((x0, x1, y0 + base_h - 4, y0 + base_h, zf - 2, zb, "top"))
        if H > 160:
            wy0, wy1 = y0 + 145, min(y1, y0 + 220)
            out.append((x0, x1, wy0, wy1, zb - 35, zb, "body"))
            out += _fronts(x0, x1, wy0, wy1, zb - 35, max(1, round(W / 60)))
    elif t in ("regal", "wandregal"):
        if t == "regal":
            out.append((x0, x1, y0, y1, zb - 1.5, zb, "dark"))                      # back panel
            out += [(x0, x0 + 2, y0, y1, zf, zb, "board"), (x1 - 2, x1, y0, y1, zf, zb, "board")]
            n = max(2, round(H / 38) + 1)
        else:
            n = max(1, round(H / 30)) + (1 if H > 30 else 0)
            if H >= 30:  # a shelf with cheeks reads as one unit, not as loose boards
                out += [(x0, x0 + 2, y0, y1, zf, zb, "board"), (x1 - 2, x1, y0, y1, zf, zb, "board")]
        for i in range(n):
            y = y0 + (H - 2.5) * (i / (n - 1) if n > 1 else 0)
            out.append((x0, x1, y, y + 2.5, zf, zb, "board"))
    elif t == "tisch":
        out.append((x0, x1, y1 - 4, y1, zf, zb, "top"))
        for sx in (x0 + 8, x1 - 15):
            for sz in (zf + 8, zb - 15):
                out.append((sx, sx + 7, y0, y1 - 4, sz, sz + 7, "body"))
    elif t == "bett":
        out.append((x0, x1, y0 + 8, y0 + min(H, 40), zf, zb - 6, "body"))
        out.append((x0 + 4, x1 - 4, y0 + min(H, 40), y0 + min(H, 40) + 18, zf + 4, zb - 10, "soft"))
        out.append((x0, x1, y0, y1, zb - 6, zb, "board"))                            # headboard against the wall
    elif t == "sitzbank":
        out.append((x0, x1, y0, y1 - 8, zf, zb, "body"))
        out += _fronts(x0, x1, y0 + 4, y1 - 12, zf, drawers or max(1, round(W / 60)))
        out.append((x0 + 2, x1 - 2, y1 - 8, y1, zf + 2, zb - 2, "soft"))
    elif t == "treppe":
        n = max(3, round(H / 18))
        for i in range(n):
            out.append((x0, x1, y0, y0 + H * (i + 1) / n, zf + D * i / n, zf + D * (i + 1) / n, "board" if i % 2 else "body"))
    elif t == "wand":
        n = max(2, round(W / 15))
        for i in range(n):
            a = x0 + W * i / n
            out.append((a + 0.4, a + W / n - 0.4, y0, y1, zf, zb, "board"))
    elif t == "tuer":
        out += [(x0, x0 + 6, y0, y1, zf, zb, "body"), (x1 - 6, x1, y0, y1, zf, zb, "body"), (x0, x1, y1 - 6, y1, zf, zb, "body")]
        out.append((x0 + 6.5, x1 - 6.5, y0 + 1, y1 - 6.5, zf + 2, zb - 2, "front"))
    else:
        out.append((x0, x1, y0, y1, zf, zb, "body"))
    return out


def _corners(b, pose):
    x0, x1, y0, y1, z0, z1 = b[:6]
    th = math.radians(pose.get("yaw") or 0.0)
    ex, ez = (math.cos(th), math.sin(th)), (-math.sin(th), math.cos(th))
    out = []
    for i in range(8):
        x, z, y = (x1 if i & 1 else x0), (z1 if i & 2 else z0), (y1 if i & 4 else y0)
        out.append((pose["x"] + x * ex[0] + z * ez[0], y, pose["z"] + x * ex[1] + z * ez[1]))
    return out


def render(photo: Image.Image, geo: dict, items: list, room_depth: np.ndarray | None = None) -> Image.Image:
    """`items`: (piece, dims, pose, rgb) per placed 3d piece (pose in floor cm). Returns the photo with the mock-up drawn in.
    Pixels where the room photo is clearly nearer than a piece (a chair in front of the table) keep the photo, unless that
    would hide most of the piece (it replaces what stands there now)."""
    img = photo.convert("RGB")
    W, H = img.size
    gw, gh = geo["size"]
    k = W / gw
    K = geo["intrinsics"]
    fx, fy, cx, cy = K["fx"] * k, K["fy"] * k, K["cx"] * k, K["cy"] * (H / gh)
    fr = geometry._frame(geo)
    zbuf = np.full((H, W), np.inf, np.float32)
    col = np.zeros((H, W, 3), np.float32)
    fid = np.full((H, W), -1, np.int32)
    owner = np.full((H, W), -1, np.int32)
    vv, uu = np.mgrid[0:H, 0:W].astype(np.float32)
    face_no = 0
    for pi, (piece, dims, pose, rgb) in enumerate(items):
        base = np.asarray(rgb, np.float32)
        for b in parts(piece, dims):
            C = np.array([geometry._cam(fr, p) for p in _corners(b, pose)])
            tone = TONES.get(b[6], 1.0)
            for f in FACES:
                P = C[list(f)]
                if (P[:, 2] < NEAR).any():
                    continue
                n = np.cross(P[1] - P[0], P[3] - P[0])
                if np.linalg.norm(n) < 1e-9:
                    continue
                n /= np.linalg.norm(n)
                centre = P.mean(0)
                if np.dot(n, centre - C.mean(0)) < 0:     # orient outward
                    n = -n
                if np.dot(n, centre) >= 0:                  # facing away from the camera
                    continue
                pts = [(fx * X[0] / X[2] + cx, fy * X[1] / X[2] + cy) for X in P]
                us, vs = [p[0] for p in pts], [p[1] for p in pts]
                u0, u1 = max(0, int(min(us))), min(W, int(math.ceil(max(us))) + 1)
                v0, v1 = max(0, int(min(vs))), min(H, int(math.ceil(max(vs))) + 1)
                if u1 <= u0 or v1 <= v0:
                    continue
                m = Image.new("L", (u1 - u0, v1 - v0), 0)
                ImageDraw.Draw(m).polygon([(u - u0, v - v0) for u, v in pts], fill=255)
                mask = np.asarray(m) > 0
                if not mask.any():
                    continue
                ru, rv = (uu[v0:v1, u0:u1] - cx) / fx, (vv[v0:v1, u0:u1] - cy) / fy
                den = n[0] * ru + n[1] * rv + n[2]
                with np.errstate(divide="ignore", invalid="ignore"):
                    z = np.where(np.abs(den) > 1e-9, np.dot(n, P[0]) / den, np.inf)
                zb = zbuf[v0:v1, u0:u1]
                win = mask & (z > 0) & (z < zb)
                if not win.any():
                    continue
                shade = (0.38 + 0.62 * max(0.0, float(np.dot(n, LIGHT)))) * tone
                zb[win] = z[win]
                col[v0:v1, u0:u1][win] = np.clip(base * shade, 0, 255)
                fid[v0:v1, u0:u1][win] = face_no
                owner[v0:v1, u0:u1][win] = pi
                face_no += 1
    drawn = owner >= 0
    fitted = [pi for pi, it in enumerate(items) if it[0].get("slope_fit")]
    if fitted and geo.get("slopes") and drawn.any():   # a piece fitted to a Dachschräge ends at the slope
        sel = np.isin(owner, fitted) & np.isfinite(zbuf)
        z = zbuf[sel].astype(np.float64)
        Xc = np.stack([(uu[sel] - cx) / fx * z, (vv[sel] - cy) / fy * z, z], -1) - fr["origin"]
        x, y, zz = Xc @ fr["right"], Xc @ fr["up"], Xc @ fr["forward"]
        free = np.full(len(z), np.inf)
        ch = (geo.get("ceiling") or {}).get("height")
        if ch:
            free[:] = ch
        for sl in geo["slopes"]:
            nn = sl["normal"]
            if nn[1] < -1e-6:
                free = np.minimum(free, -(nn[0] * x + nn[2] * zz + sl["offset"]) / nn[1])
        cut = np.zeros_like(drawn)
        cut[sel] = y > free + 1.0
        drawn &= ~cut
        fid[cut] = -1
    if room_depth is not None and drawn.any():
        rd = np.asarray(Image.fromarray(room_depth.astype(np.float32)).resize((W, H), Image.BILINEAR))
        front = drawn & (rd > 0) & (rd < zbuf - 40)
        for pi in range(len(items)):
            mine = owner == pi
            if mine.any() and front[mine].mean() < 0.6:
                drawn &= ~(front & mine)
    # outlines where the face changes: doors, drawers and shelves read as divisions
    edge = np.zeros_like(drawn)
    edge[:, 1:] |= fid[:, 1:] != fid[:, :-1]
    edge[1:, :] |= fid[1:, :] != fid[:-1, :]
    edge &= (fid >= 0) | np.roll(fid >= 0, 1, 0) | np.roll(fid >= 0, 1, 1)
    out = np.asarray(img, np.float32).copy()
    out[drawn] = col[drawn]
    line = edge & (drawn | np.roll(drawn, 1, 0) | np.roll(drawn, 1, 1))
    out[line] *= 0.35
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def depth_span(geo: dict, size, piece: dict, dims, pose: dict):
    """Per pixel of the piece's silhouette (at `size`): nearest and farthest camera depth of its outer box.
    → (mask, near, far) arrays; mask False where the piece is not seen."""
    W, H = size
    gw, gh = geo["size"]
    K = geo["intrinsics"]
    fx, fy, cx, cy = K["fx"] * W / gw, K["fy"] * H / gh, K["cx"] * W / gw, K["cy"] * H / gh
    fr = geometry._frame(geo)
    w, h, d = (float(v) for v in dims)
    e = float(piece.get("elev") or 0)
    C = np.array([geometry._cam(fr, p) for p in _corners((-w / 2, w / 2, e, e + h, -d / 2, d / 2), pose)])
    near = np.full((H, W), np.inf, np.float32)
    far = np.full((H, W), -np.inf, np.float32)
    vv, uu = np.mgrid[0:H, 0:W].astype(np.float32)
    for f in FACES:
        P = C[list(f)]
        if (P[:, 2] < NEAR).any():
            continue
        n = np.cross(P[1] - P[0], P[3] - P[0])
        if np.linalg.norm(n) < 1e-9:
            continue
        n /= np.linalg.norm(n)
        pts = [(fx * X[0] / X[2] + cx, fy * X[1] / X[2] + cy) for X in P]
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).polygon(pts, fill=255)
        mask = np.asarray(m) > 0
        if not mask.any():
            continue
        den = n[0] * (uu - cx) / fx + n[1] * (vv - cy) / fy + n[2]
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(np.abs(den) > 1e-9, np.dot(n, P[0]) / den, np.nan)
        ok = mask & np.isfinite(z) & (z > 0)
        near[ok] = np.minimum(near[ok], z[ok])
        far[ok] = np.maximum(far[ok], z[ok])
    return np.isfinite(near) & np.isfinite(far), near, far


def obstacles(geo: dict, piece: dict, dims, pose: dict, room_depth: np.ndarray, skip=None) -> dict:
    """What the room photo shows where the piece would stand (DepthPro depth, so roughly):
    inside — share of the silhouette where something of the room lies within the piece's volume (radiator, old furniture);
    behind — share where the room is far behind the piece's back (a window or an opening behind a wall piece);
    front — share where something stands clearly in front of it; front_low the same in the lower half of the silhouette
    (a bed or table that would hide it – a beam or lamp in front of the top matters less).
    `skip`: bool mask (depth resolution) of areas that will be removed anyway."""
    dh, dw = room_depth.shape
    mask, near, far = depth_span(geo, (dw, dh), piece, dims, pose)
    if skip is not None:
        mask &= ~skip
    n = int(mask.sum())
    if n < 30:
        return {"inside": 0.0, "behind": 0.0, "front": 0.0, "front_low": 0.0, "pixels": n}
    rd, zn, zf = room_depth[mask], near[mask], far[mask]
    tol = np.maximum(10.0, 0.05 * zn)          # DepthPro is good to a few per cent
    inside = (rd > zn + tol) & (rd < zf - tol)
    behind = rd > zf + np.maximum(60.0, 0.15 * zf)
    front = rd < zn - np.maximum(30.0, 0.08 * zn)
    rows = np.nonzero(mask)[0]
    low = rows >= (rows.min() + rows.max()) / 2
    return {"inside": round(float(inside.mean()), 3), "behind": round(float(behind.mean()), 3),
            "front": round(float(front.mean()), 3), "front_low": round(float(front[low].mean()), 3) if low.any() else 0.0,
            "pixels": n}
