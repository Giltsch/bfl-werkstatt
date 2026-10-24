import numpy as np
from PIL import Image

import keep


def test_alpha_is_hard_inside_and_fades_only_outward():
    w, h = 1000, 800
    a = keep._alpha([[250, 300, 750, 600]], w, h)
    grow = int(keep.GROW * w)
    assert a[200:600, 300:600].min() == 1.0                       # the whole piece is opaque, edges included
    assert a[200 - grow + 1, 450] == 1.0 and a[400, 300 - grow + 1] == 1.0
    fade = a[200 - grow - 12:200 - grow, 450]
    assert (np.diff(fade) >= 0).all() and fade[0] == 0.0          # short monotone fade outside the grown box
    assert a[:100].max() == 0.0 and a[:, :200].max() == 0.0


def test_keep_outside_restores_the_room_pixel_exact():
    rng = np.random.default_rng(1)
    orig = Image.fromarray(rng.integers(0, 255, (400, 500, 3), np.uint8))
    out = Image.new("RGB", (500, 400), (120, 80, 40))
    res = np.asarray(keep.keep_outside(out, orig, [[300, 300, 700, 700]]))
    o = np.asarray(orig)
    assert (res[:80] == o[:80]).all() and (res[:, :100] == o[:, :100]).all()
    inside = res[130:270, 160:340].reshape(-1, 3)
    assert inside.std(axis=0).max() < 1.0                          # piece fully from the generated image


def _scene(w=600, h=800):
    rng = np.random.default_rng(3)
    base = np.full((h, w, 3), 180, np.float32) + rng.normal(0, 6, (h, w, 3))
    base[500:700, 200:400] = (90, 60, 40)                          # an old table leg block below the box
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def test_changes_touching_the_box_come_from_the_edit():
    orig = _scene()
    out = np.asarray(orig, np.float32).copy()
    out[300:500, 150:450] = (150, 100, 60)                         # the new piece inside its box
    out[500:700, 200:400] = 180                                    # the old legs are gone in the edit
    res = np.asarray(keep.keep_outside(Image.fromarray(out.astype(np.uint8)), orig, [[375, 250, 625, 750]]))
    assert abs(res[600, 300].astype(int) - 180).max() < 20         # no old leg pasted back under the new piece
    assert (res[:150] == np.asarray(orig)[:150]).all()             # far away the photo stays exact


def test_global_drift_does_not_spread_the_edit():
    orig = _scene()
    out = np.clip(np.asarray(orig, np.float32) * 1.15 + 8, 0, 255)
    out[300:500, 150:450] = (150, 100, 60)
    res = np.asarray(keep.keep_outside(Image.fromarray(out.astype(np.uint8)), orig, [[375, 250, 625, 750]]))
    assert (res[650:700, 220:380] == np.asarray(orig)[650:700, 220:380]).all()
