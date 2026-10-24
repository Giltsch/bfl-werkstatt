"""Sloped ceilings (Dachschrägen): detection from depth, free height, the slope_fit piece option, the assistant's
answers, and the keep rules for removed pieces that came with the same fix."""
import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import geometry as g
import jobs
import keep
import projects
import workshop as ws

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "geometry_sample.json").read_text())
TILT, KNEE = 45.0, 100.0        # the synthetic roof: 45°, meeting the left wall (x = -200) at 100 cm


def slope_plane(tilt=TILT, knee=KNEE, wall_x=-200.0):
    """Floor-frame plane n·X + off = 0 of a roof rising from the left wall: y = knee + (x - wall_x)·tan(tilt)."""
    t = math.tan(math.radians(tilt))
    n = np.array([t, -1.0, 0.0])
    k = np.linalg.norm(n)
    return n / k, (knee - wall_x * t) / k


def synth(pitch_deg=10, slope=True, W=672, H=504, f=500.0, cam_h=150.0, ceiling=260.0, noise=0.005, seed=0):
    """Analytic depth of the fixture room (floor, 3 walls, flat ceiling, optionally a roof slope on the left)."""
    p = math.radians(pitch_deg)
    n = np.array([0.0, -math.cos(p), -math.sin(p)])
    fr = {k: np.asarray(v) for k, v in g.frame_of(n, cam_h).items()}
    planes = [(n, cam_h), (-n, ceiling - cam_h)]
    planes += [(w["normal"][0] * fr["right"] + w["normal"][1] * fr["forward"], w["offset"]) for w in FIXTURE["walls"]]
    if slope:
        sn, off = slope_plane()
        a = sn[0] * fr["right"] + sn[1] * fr["up"] + sn[2] * fr["forward"]
        planes.append((a, off - float(a @ fr["origin"])))
    v, u = np.mgrid[0:H, 0:W] + 0.5
    rays = np.stack([(u - W / 2) / f, (v - H / 2) / f, np.ones_like(u)], -1)
    t = np.full((H, W), np.inf)
    for a, b in planes:
        ar = rays @ a
        hit = ar < -1e-9
        t = np.minimum(t, np.where(hit, -b / np.where(hit, ar, -1.0), np.inf))
    t *= 1 + noise * np.random.default_rng(seed).standard_normal(t.shape)
    return t.astype(np.float32), n


def slope_geo():
    """The workshop test fixture room with the synthetic roof slope added."""
    geo = copy.deepcopy(FIXTURE)
    sn, off = slope_plane()
    geo["slopes"] = [{"normal": [float(c) for c in sn], "offset": float(off), "tilt": TILT}]
    return geo


# ---------------- detection ----------------

def test_slope_found_in_an_attic_and_its_free_height():
    depth, _ = synth()
    r = g.fit_scene(depth, 500.0, (672, 504))
    assert len(r["slopes"]) == 1
    sl = r["slopes"][0]
    assert abs(sl["tilt"] - TILT) < 4
    geo = {**r, "slopes": r["slopes"]}
    assert abs(g.headroom(geo, -190, 300) - (KNEE + 10)) < 12          # near the knee wall
    assert g.headroom(geo, 200, 300) == pytest.approx(r["ceiling"]["height"], abs=0.5)   # far side: the flat ceiling


@pytest.mark.parametrize("pitch", [10, 40])
def test_no_slope_in_a_plain_room(pitch):
    depth, _ = synth(pitch, slope=False)
    assert g.fit_scene(depth, 500.0, (672, 504))["slopes"] == []


def test_old_measurement_gets_its_slopes_from_the_stored_depth(tmp_path):
    depth, _ = synth()
    r = g.fit_scene(depth, 500.0, (672, 504))
    geo = {**g._empty("uploads/attic.png", (672, 504), None), **{k: v for k, v in r.items() if k != "error"}, "ok": True}
    del geo["slopes"]
    small = np.asarray(Image.fromarray(depth).resize((512, 384), Image.BILINEAR))
    np.save(tmp_path / "attic-depth.npy", small.astype(np.float16))
    back = g.with_slopes(geo, tmp_path)
    assert len(back["slopes"]) == 1 and abs(back["slopes"][0]["tilt"] - TILT) < 4
    assert "slopes" not in geo                                          # the stored dict is not changed
    assert g.with_slopes({**geo, "room": "uploads/none.png"}, tmp_path)["slopes"] == []   # no depth map: none


def test_clearance_and_rescale():
    geo = slope_geo()
    pose = {"x": -170, "z": 300, "yaw": 90, "wall": "w1"}               # 60 deep against the left wall
    sc = g.slope_clearance(geo, (160, 220, 60), pose)
    assert sc["lowest"] == pytest.approx(KNEE, abs=0.5) and sc["over"] == pytest.approx(220 - KNEE, abs=0.5)
    assert g.slope_clearance(geo, (160, 80, 60), pose)["over"] == 0
    assert g.relations(geo, (160, 220, 60), pose)["slope"]["over"] > 100
    assert g.slope_clearance(FIXTURE, (160, 220, 60), pose) is None      # no slopes: no answer
    big = g.rescale(geo, 1.2)
    assert g.headroom(big, -240, 360) == pytest.approx(1.2 * g.headroom(geo, -200, 300), abs=0.5)


# ---------------- the piece option ----------------

def test_slope_fit_is_a_piece_field():
    p = projects._piece({"type": "schrank", "slope_fit": True}, "p1aaaa")
    assert p["slope_fit"] is True
    assert "slope_fit" not in projects._piece({"type": "schrank", "slope_fit": False}, "p1aaaa")   # old docs unchanged
    assert projects._piece_value("slope_fit", True) is True
    with pytest.raises(Exception):
        projects._piece_value("slope_fit", "ja")


def attic_doc(**piece_kw):
    p = {"id": "p1aaaa", "type": "schrank", "text": "", "material": "eiche", "material_custom": "", "handles": "push",
         "w": 160, "h": 220, "d": 60, "sketch": None, "render": None, "use_render": False, "sample": None, "where": "",
         "placement": {"mode": "3d", "pose": {"x": -170, "z": 300, "yaw": 90, "wall": "w1"}, "box": None}, **piece_kw}
    return {"id": "a77c00000000", "title": "TEST Dach", "room": "uploads/room.png", "room_size": [1344, 1008],
            "removes": [], "variants": [], "pieces": [p], "active_piece": "p1aaaa", "geometry": slope_geo(),
            "tidy": True, "protect": True, "count": 2, "res": 1536, "judge": {"enabled": False}, "guide": False,
            "mockup": False, "setting": "", "room_changes": "", "sample": None}


def test_room_job_warns_about_the_slope_and_describes_the_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "OUT", tmp_path)
    (tmp_path / "uploads").mkdir()
    Image.new("RGB", (1344, 1008), (200, 200, 200)).save(tmp_path / "uploads/room.png")
    plan = ws.build_room_job(attic_doc())
    assert any("ragt" in w and "Dachschräge" in w for w in plan["warnings"])
    assert not any("Höher als die Decke" in w for w in plan["warnings"])
    fit = ws.build_room_job(attic_doc(slope_fit=True))
    assert not any("Dachschräge" in w or "Höher als die Decke" in w for w in fit["warnings"])
    assert "roof slope" in fit["params"]["prompt"] and "cut at an angle along the slope" in fit["params"]["prompt"]


def test_fitted_keep_region_ends_at_the_slope():
    doc = attic_doc(slope_fit=True)
    boxes, _ = ws.resolve_boxes(doc)
    full = ws.keep_regions_for(attic_doc(), attic_doc()["pieces"][0], boxes["p1aaaa"])[0]["poly"]
    cut = ws.keep_regions_for(doc, doc["pieces"][0], boxes["p1aaaa"])[0]["poly"]
    assert min(y for _, y in cut) > min(y for _, y in full) + 20      # the top no longer reaches into the slope


def test_offer_names_the_slope_fit():
    import offer
    assert "Dachschräge" in offer.position(1, {"type": "schrank", "w": 160, "h": 220, "d": 60, "slope_fit": True})
    assert "Dachschräge" not in offer.position(1, {"type": "schrank", "w": 160, "h": 220, "d": 60})


# ---------------- the assistant ----------------

def test_assistant_slope_rows_and_words(monkeypatch):
    import agent
    rows = agent._slope_rows(attic_doc())
    assert len(rows) == 1 and rows[0]["lowest"] == pytest.approx(KNEE, abs=1) and rows[0]["fit_h"] == 95
    assert agent._slope_rows(attic_doc(slope_fit=True)) == []
    for said in ("an die schräge anpassen", "bitte an die dachschräge anpassen", "oben schräg"):
        assert agent._SLOPE_FIT.search(said)
    for said in ("niedriger", "mach ihn niedriger", "so hoch wie es geht"):
        assert agent._SLOPE_LOWER.fullmatch(said)
    for said in ("anderer platz", "einen anderen platz suchen", "wo passt er hin"):
        assert agent._SLOPE_MOVE.fullmatch(said)
    assert not agent._SLOPE_FIT.search("stell den schrank links neben das fenster")


# ---------------- keep: removed pieces ----------------

def test_remove_regions_pass_validation_with_their_flag():
    regs = jobs.keep_regions([{"box": [100, 100, 300, 300], "remove": True},
                              {"poly": [[1, 1], [50, 1], [50, 50]], "remove": True}, [400, 400, 500, 500]])
    assert regs[0] == {"box": [100.0, 100.0, 300.0, 300.0], "remove": True} and regs[1]["remove"] is True
    assert regs[2] == [400.0, 400.0, 500.0, 500.0]
    m = keep._boxes([{"box": [100, 100, 300, 300]}], 1000, 1000, 0)
    assert m[150, 150] and not m[350, 350]


def test_the_shadow_beside_a_removed_piece_goes_too():
    rng = np.random.default_rng(1)
    o = np.full((600, 800, 3), 200, np.float32) + rng.normal(0, 3, (600, 800, 3))
    o[300:500, 300:500] = (110, 100, 90)                       # the old chest …
    o[300:500, 500:530] = (150, 150, 150)                      # … and its soft side shadow just outside the outline
    out = np.full((600, 800, 3), 200, np.float32) + rng.normal(0, 3, (600, 800, 3))   # the edit: wall and floor only
    orig = Image.fromarray(np.clip(o, 0, 255).astype(np.uint8))
    edit = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    chest = [500, 375, 834, 625]                               # [t, l, b, r] on the 0-1000 grid
    plain = np.asarray(keep.keep_outside(edit, orig, [chest]), np.float32)
    marked = np.asarray(keep.keep_outside(edit, orig, [{"box": chest, "remove": True}]), np.float32)
    assert plain[400, 520].mean() < 175                        # unmarked: the shadow strip comes back from the photo
    assert abs(marked[400, 520].mean() - 200) < 12             # marked as removed: gone with the chest
    assert (marked[:150] == np.asarray(orig)[:150]).all()      # far away the photo stays exact


def test_relit_wall_next_to_a_piece_leaves_no_grey_cloud():
    rng = np.random.default_rng(2)
    o = np.full((600, 800, 3), 190, np.float32) + rng.normal(0, 2, (600, 800, 3))
    out = o.copy()
    out[150:550, 300:500] = (150, 110, 70)                     # the new piece in its box
    out[150:550, 500:560] *= 0.78                              # FLUX's soft shadow beside it, outside the box
    res = np.asarray(keep.keep_outside(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)),
                                       Image.fromarray(np.clip(o, 0, 255).astype(np.uint8)), [[250, 375, 917, 625]]), np.float32)
    assert abs(res[350, 590].mean() - 190) < 6                 # no grey blob grown from the shadow
