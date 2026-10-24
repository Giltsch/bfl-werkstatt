"""Keep a photo unchanged outside the regions an edit was allowed to touch.

FLUX edits re-render the whole frame: walls drift, niches get filled in, the framing shifts by a percent or two. For the
workshop page the room shell must stay exactly as photographed, so after generation the result is aligned back onto the
original photo, colour-matched, and only the allowed regions (plus a margin feathered outward) are taken from it."""

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_closing, binary_dilation, binary_fill_holes, binary_opening, distance_transform_edt, label, uniform_filter

GROW, FEATHER = 0.015, 0.006   # hard margin around each box, then a short outward fade (fractions of the long side)
HARMONIZE = 0.08               # how far into the edited area the photo's brightness is carried (share of the long side)
CORE = 65.0                    # a changed blob next to a box counts as an object only with a core this different
REMOVE_GROW, CORE_REMOVE = 0.03, 40.0   # removed pieces: wider margin (side, shadow) and a lower bar for their rests


def _gray(img: Image.Image, size) -> np.ndarray:
    a = np.asarray(img.convert("L").resize(size, Image.BILINEAR), np.float32)
    return (a - a.mean()) / (a.std() + 1e-6)


def _warp(img: Image.Image, s: float, dx: float, dy: float, size=None) -> Image.Image:
    """Zoom by 1/s around the centre and shift by (dx, dy) source pixels: output (x, y) samples (s*x + c, s*y + f)."""
    w, h = img.size
    cx, cy = w / 2, h / 2
    return img.transform(size or img.size, Image.AFFINE, (s, 0, cx - s * cx + dx, 0, s, cy - s * cy + dy), Image.BICUBIC)


def _align(out: Image.Image, orig: Image.Image, outside: np.ndarray):
    """The (scale, dx, dy) that best maps `out` onto `orig`, judged only where the photo must stay unchanged."""
    w, h = out.size
    k = 4
    sw, sh = w // k, h // k
    small_out = out.convert("L").resize((sw, sh), Image.BILINEAR)
    ref = _gray(orig, (sw, sh))
    m = np.asarray(Image.fromarray(outside.astype(np.uint8) * 255).resize((sw, sh))) > 127
    if m.sum() < 0.1 * m.size:
        return 1.0, 0.0, 0.0

    def err(s, dx, dy):
        a = np.asarray(_warp(small_out, s, dx, dy), np.float32)
        a = (a - a.mean()) / (a.std() + 1e-6)
        return float(np.abs(a - ref)[m].mean())

    best = (err(1.0, 0, 0), 1.0, 0.0, 0.0)
    identity = best[0]
    for s in np.arange(0.96, 1.0401, 0.01):
        for dx in range(-8, 9, 2):
            for dy in range(-8, 9, 2):
                best = min(best, (err(s, dx, dy), s, dx, dy))
    _, s0, x0, y0 = best
    for s in np.arange(s0 - 0.006, s0 + 0.0061, 0.002):
        for dx in np.arange(x0 - 1.5, x0 + 1.51, 0.5):
            for dy in np.arange(y0 - 1.5, y0 + 1.51, 0.5):
                best = min(best, (err(s, dx, dy), s, dx, dy))
    e, s, dx, dy = best
    if e > identity * 0.97:  # not clearly better: trust the frame as it is
        return 1.0, 0.0, 0.0
    return float(s), float(dx * k), float(dy * k)


def keep_outside(out: Image.Image, orig: Image.Image, regions, margin: float = 0.04, grow: bool = True) -> Image.Image:
    """`regions` are [top, left, bottom, right] boxes on a 0-1000 grid. Returns `out` with everything outside them
    replaced by `orig` (resized to the output size). The pieces keep hard edges: alpha is 1 inside each box grown by
    GROW and only fades outward over FEATHER, so a piece never turns transparent at its border. With `grow`, clearly
    changed areas touching a box are taken from the edit too (see _changed_next_to)."""
    out = out.convert("RGB")
    w, h = out.size
    orig = orig.convert("RGB").resize((w, h), Image.LANCZOS)
    pad = margin * max(w, h)
    mask, ring = _boxes(regions, w, h, pad), _boxes(regions, w, h, pad * 3)
    if not mask.any():
        return out

    s, dx, dy = _align(out, orig, ~mask)
    if (s, dx, dy) != (1.0, 0.0, 0.0):
        out = _warp(out, s, dx, dy)

    # Match the edit's exposure and colour to the photo, measured in a ring just outside the edited area.
    ring &= ~mask
    if ring.sum() < 500:
        ring = ~mask
    a, o = np.asarray(out, np.float32), np.asarray(orig, np.float32)
    for c in range(3):
        ma, sa = a[..., c][ring].mean(), a[..., c][ring].std() + 1e-6
        mo, so = o[..., c][ring].mean(), o[..., c][ring].std() + 1e-6
        gain = float(np.clip(so / sa, 0.8, 1.25))
        a[..., c] = (a[..., c] - ma) * gain + mo
    out = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))

    hard = _boxes(regions, w, h, GROW * max(w, h))
    removes = [r for r in regions if isinstance(r, dict) and r.get("remove")]
    gone = _gone(removes, w, h) if removes else None
    if gone is not None:
        hard |= gone
    a = np.asarray(out, np.float32)
    if grow:
        hard |= _changed_next_to(a, o, hard, gone)
    return Image.fromarray(blend(harmonize(a, o, hard), o, hard))


def harmonize(a: np.ndarray, o: np.ndarray, hard: np.ndarray) -> np.ndarray:
    """FLUX re-lights the whole frame a little: the wall next to a new piece comes out a few percent lighter or greyer
    than in the photo. Pasting that next to the original leaves a soft grey seam. So the edit's low-frequency brightness
    is pulled to the photo's: the ratio photo/edit, measured outside the edited area (blurred, so texture and small
    misalignment do not count), is spread into it and applied fully at its border, fading out over HARMONIZE of the long
    side. Deep inside, the new piece keeps its own light."""
    import cv2
    h, w = hard.shape
    k = 4
    sw, sh = max(1, w // k), max(1, h // k)
    shrink = lambda x: cv2.resize(x, (sw, sh), interpolation=cv2.INTER_AREA)
    outside = shrink((~hard).astype(np.float32)) > 0.99
    if outside.sum() < 0.05 * outside.size or not (~outside).any():
        return a
    # objects that differ (a leg the edit dropped, a misaligned edge) are no light: only similar pixels are measured
    similar = np.abs(cv2.blur(shrink(a), (3, 3)) - cv2.blur(shrink(o), (3, 3))).mean(-1) < 40
    valid = outside & ~binary_dilation(~similar, iterations=2)
    if valid.sum() < 0.03 * valid.size:
        return a
    sig = max(2.0, 0.01 * max(sw, sh))
    vf = valid.astype(np.float32)
    wv0 = np.maximum(cv2.GaussianBlur(vf, (0, 0), sig), 1e-3)[..., None]
    blur = lambda x: cv2.GaussianBlur(x * vf[..., None], (0, 0), sig) / wv0   # only what stays from the photo counts
    sa, so = blur(shrink(a)), blur(shrink(o))
    ratio = np.clip((so + 4) / (sa + 4), 0.8, 1.25)
    # spread the measured ratio into the edited area: normalized convolution over a wide kernel
    big = max(4.0, HARMONIZE * max(sw, sh) / 2)
    wv = cv2.GaussianBlur(valid.astype(np.float32), (0, 0), big)
    filled = cv2.GaussianBlur(ratio * valid[..., None], (0, 0), big) / np.maximum(wv, 1e-3)[..., None]
    filled = np.where(wv[..., None] > 1e-3, filled, 1.0)
    ratio = np.where(valid[..., None], ratio, filled)
    d = distance_transform_edt(~outside) / max(1.0, HARMONIZE * max(sw, sh))
    t = np.clip(1 - d, 0, 1)
    t = t * t * (3 - 2 * t)
    gain = 1 + (ratio - 1) * t[..., None]
    gain = cv2.resize(gain.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(a * gain, 0, 255)


def blend(a: np.ndarray, o: np.ndarray, hard: np.ndarray, levels: int | None = None) -> np.ndarray:
    """Seamless paste of the edit `a` into the photo `o` (float arrays): inside `hard` exactly the edit, far outside exactly
    the photo, and in a band around it a Laplacian-pyramid blend — coarse brightness/colour differences fade over a wide
    band, fine texture (tile joints, rug, grain) switches over a narrow one, so no straight cut is visible."""
    import cv2
    h, w = hard.shape
    if levels is None:   # the blend band ≈ 3 % of the long side (≈ 48 px at 1536)
        levels = int(np.clip(np.floor(np.log2(max(8.0, 0.03 * max(w, h)))), 3, 6))
    m = hard.astype(np.float32)
    ga, go, gm = [a], [o], [m]
    for _ in range(levels):
        ga.append(cv2.pyrDown(ga[-1]))
        go.append(cv2.pyrDown(go[-1]))
        gm.append(cv2.pyrDown(gm[-1]))
    up = lambda x, ref: cv2.pyrUp(x, dstsize=(ref.shape[1], ref.shape[0]))
    res = ga[-1] * gm[-1][..., None] + go[-1] * (1 - gm[-1][..., None])
    for i in range(levels - 1, -1, -1):
        la, lo = ga[i] - up(ga[i + 1], ga[i]), go[i] - up(go[i + 1], go[i])
        k = gm[i][..., None]
        res = up(res, ga[i]) + la * k + lo * (1 - k)
    # exact edit inside (eroded a little, smooth step), exact photo beyond the band the coarsest level can reach
    inner = np.clip(distance_transform_edt(hard) / max(2.0, FEATHER * max(w, h)), 0, 1)
    band = 1.5 * 2 ** levels
    outer = np.clip(distance_transform_edt(~hard) / band, 0, 1)
    inner, outer = inner * inner * (3 - 2 * inner), outer * outer * (3 - 2 * outer)
    res = res * (1 - inner[..., None]) + a * inner[..., None]
    res = res * (1 - outer[..., None]) + o * outer[..., None]
    return np.clip(res, 0, 255).astype(np.uint8)


def _changed_next_to(a: np.ndarray, o: np.ndarray, hard: np.ndarray, gone: np.ndarray | None = None) -> np.ndarray:
    """Areas where the edit clearly differs from the photo AND that touch the edit box: the new piece reaching past its
    box, or the remains of an old piece the box only partly covered (legs, a chair, a tablecloth edge). Restoring those
    from the photo would leave a chimera of old and new. Limited to a window around the boxes, so drift elsewhere in the
    frame (lighting, plants) never spreads the edit over the room."""
    h, w = hard.shape
    k = 4
    sh, sw = h // k, w // k
    small = lambda x: np.asarray(Image.fromarray(x).resize((sw, sh), Image.BILINEAR), np.float32)
    sa = uniform_filter(small(a.astype(np.uint8)), (5, 5, 1))
    diff = np.abs(sa - uniform_filter(small(o.astype(np.uint8)), (5, 5, 1))).mean(-1)
    box = np.asarray(Image.fromarray(hard.astype(np.uint8) * 255).resize((sw, sh))) > 127
    ys, xs = np.nonzero(box)
    if not len(ys):
        return np.zeros_like(hard)
    far = ~box
    far[max(0, ys.min() - sh // 8):ys.max() + sh // 8, max(0, xs.min() - sw // 8):xs.max() + sw // 8] = False
    noise = diff[far] if far.sum() > 1000 else diff[~box]
    thr = max(32.0, float(np.median(noise) + 7 * np.median(np.abs(noise - np.median(noise)))))   # objects, not re-drawn texture
    changed = binary_closing(binary_opening(diff > thr, iterations=2), iterations=4)
    bh, bw = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
    win = np.zeros_like(box)
    win[max(0, ys.min() - bh // 2):ys.max() + bh // 2 + 1, max(0, xs.min() - bw // 2):xs.max() + bw // 2 + 1] = True
    # Each changed blob touching the box is judged on its own. It is taken from the edit only when it has a clearly
    # different core (an object; a shadow or a re-lit patch of wall stays below CORE and would leave a grey cloud) and
    # sits in the lower part of the box beside it (legs, a tablecloth edge, the rest of a removed cabinet). Above that
    # it is usually the room itself – a beam or a slope the edit drew over – and the photo wins.
    # Beside or below a removed piece anything clearly changed is its shadow or rest: taken from the edit at a lower bar.
    lab, n = label(changed & win & ~box)
    touch = binary_dilation(box, iterations=2)
    near_gone = binary_dilation(np.asarray(Image.fromarray(gone.astype(np.uint8) * 255).resize((sw, sh))) > 127,
                                iterations=2) if gone is not None else None
    keep_ids = []
    for i in range(1, n + 1):
        m = lab == i
        if not (m & touch).any():
            continue
        if near_gone is not None and (m & near_gone).any() and (diff[m] >= CORE_REMOVE).sum() >= 20:
            cols = np.nonzero(m.any(axis=0))[0]
            rows = np.nonzero(near_gone[:, max(0, cols.min() - 4):cols.max() + 5].any(axis=1))[0]
            if len(rows) and np.nonzero(m)[0].mean() >= rows.min():   # beside or below it: a window above stays
                keep_ids.append(i)
                continue
        if (diff[m] >= CORE).sum() < 20:
            continue
        cols = np.nonzero(m.any(axis=0))[0]
        near = box[:, max(0, cols.min() - 4):cols.max() + 5]
        rows = np.nonzero(near.any(axis=1))[0]
        if len(rows) and np.nonzero(m)[0].mean() < rows.min() + 0.4 * (rows.max() - rows.min()) and not _continues(sa, m, box):
            continue
        keep_ids.append(i)
    # the kept blobs, holes filled and edges smoothed; the seamless blend hides their outline
    grown = binary_fill_holes(binary_closing(np.isin(lab, keep_ids) | box, iterations=3)) & ~box
    if grown.sum() > 0.8 * box.sum():  # most of the window changed: no clear object, trust the box alone
        return np.zeros_like(hard)
    return np.asarray(Image.fromarray(grown.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 127


def _gone(removes, w, h) -> np.ndarray:
    """The removed pieces grown by REMOVE_GROW to the sides and downwards (their side faces, shadow and floor contact).
    Upwards only GROW, and beside a piece the wider margin starts a little below its top edge: there is usually wall or
    a window reveal next to the top corner, where the edit draws edges a few pixels off and a seam shows as a kink."""
    from scipy.ndimage import maximum_filter1d, minimum_filter1d
    core = _boxes(removes, w, h, 0)
    big = _boxes(removes, w, h, REMOVE_GROW * max(w, h))
    g, n = int(GROW * max(w, h)), 2 * int(REMOVE_GROW * max(w, h)) + 1
    has = core.any(axis=0)
    top = np.where(has, core.argmax(axis=0), h).astype(np.int64)
    bot = np.where(has, h - 1 - core[::-1].argmax(axis=0), -1).astype(np.int64)
    top_n, bot_n = minimum_filter1d(top, size=n), maximum_filter1d(bot, size=n)
    side = np.where(has, top - g, top_n + 0.15 * np.maximum(0, bot_n - top_n))
    ys = np.arange(h)[:, None]
    return (big & (ys >= side[None, :])) | _boxes(removes, w, h, GROW * max(w, h))


def _continues(sa: np.ndarray, m: np.ndarray, box: np.ndarray) -> bool:
    """Does a changed blob above a box look like the new piece reaching past its box (in the edit it resembles the box
    next to it) rather than the room drawn over something (it resembles the wall around it)?"""
    ring = binary_dilation(m, iterations=3) & ~m
    inner, outer = ring & box, ring & ~box
    if inner.sum() < 5 or outer.sum() < 5:
        return False
    c = sa[m].mean(0)
    return float(np.abs(c - sa[inner].mean(0)).mean()) < float(np.abs(c - sa[outer].mean(0)).mean())


def _feather(hard: np.ndarray, w, h) -> np.ndarray:
    t = np.clip(1 - distance_transform_edt(~hard) / max(3.0, FEATHER * max(w, h)), 0, 1)
    return t * t * (3 - 2 * t)


def _alpha(regions, w, h) -> np.ndarray:
    return _feather(_boxes(regions, w, h, GROW * max(w, h)), w, h)


def _boxes(regions, w, h, pad) -> np.ndarray:
    """Regions as a mask, grown by `pad` px: [top, left, bottom, right] boxes (also as {"box": ...}) and {"poly": [[x, y], ...]}
    outlines (0-1000)."""
    mask = np.zeros((h, w), bool)
    polys = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(polys)
    for r in regions:
        if isinstance(r, dict) and "box" in r:
            r = r["box"]
        elif isinstance(r, dict):
            draw.polygon([(x / 1000 * w, y / 1000 * h) for x, y in r["poly"]], fill=255)
            continue
        t, l, b, rr = r
        y0, x0 = max(0, int(t / 1000 * h - pad)), max(0, int(l / 1000 * w - pad))
        y1, x1 = min(h, int(b / 1000 * h + pad)), min(w, int(rr / 1000 * w + pad))
        mask[y0:y1, x0:x1] = True
    p = np.asarray(polys) > 0
    if p.any():
        mask |= distance_transform_edt(~p) <= pad if pad > 0 else p
    return mask
