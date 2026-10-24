"""Keep a photo unchanged outside the regions an edit was allowed to touch.

FLUX edits re-render the whole frame: walls drift, niches get filled in, the framing shifts by a percent or two. For the
workshop page the room shell must stay exactly as photographed, so after generation the result is aligned back onto the
original photo, colour-matched, and only the allowed regions (plus a feathered margin) are taken from it."""

import numpy as np
from PIL import Image, ImageFilter


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


def keep_outside(out: Image.Image, orig: Image.Image, regions, margin: float = 0.04) -> Image.Image:
    """`regions` are [top, left, bottom, right] boxes on a 0-1000 grid. Returns `out` with everything outside them
    replaced by `orig` (resized to the output size)."""
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

    soft = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(pad / 4))
    return Image.composite(out, orig, soft)


def _boxes(regions, w, h, pad) -> np.ndarray:
    mask = np.zeros((h, w), bool)
    for t, l, b, r in regions:
        y0, x0 = max(0, int(t / 1000 * h - pad)), max(0, int(l / 1000 * w - pad))
        y1, x1 = min(h, int(b / 1000 * h + pad)), min(w, int(r / 1000 * w + pad))
        mask[y0:y1, x0:x1] = True
    return mask
