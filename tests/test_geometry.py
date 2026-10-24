import copy
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from PIL import Image

import geometry as g

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "geometry_sample.json").read_text())
A_DIMS, A_POSE = (180, 220, 60), {"x": -20, "z": 370, "yaw": 0}
B_DIMS, B_POSE = (120, 80, 45), {"x": -170, "z": 250, "yaw": 90}
A_CORNERS = [[367.19, 749.73], [865.97, 749.73], [410.08, 690.35], [838.68, 690.35],
             [331.10, 107.39], [888.93, 107.39], [383.87, 141.49], [855.36, 141.49]]
B_CORNERS = [[-19.97, 1042.22], [226.83, 787.37], [-231.07, 1042.22], [91.02, 787.37],
             [-68.21, 684.38], [207.35, 551.58], [-294.03, 684.38], [65.60, 551.58]]


def geo():
    return copy.deepcopy(FIXTURE)


def close(a, b, tol=0.5):
    return all(abs(x - y) <= tol for x, y in zip(np.ravel(a), np.ravel(b)))


def box_close(a, b):
    return all(abs(x - y) <= 1 for x, y in zip(a, b))


def pixel_of(gg, x, z, y=0.0):
    return g._proj(gg, g._cam(g._frame(gg), (x, y, z)))


# ---------------- §2.1 vectors ----------------

def test_frame():
    fr = g.frame_of(FIXTURE["floor"]["normal"], FIXTURE["floor"]["d"])
    for k, v in FIXTURE["frame"].items():
        assert close(fr[k], v, 1e-3), k


def test_project_boxes():
    a = g.project_box(geo(), A_DIMS, A_POSE)
    assert close(a["corners"], A_CORNERS) and box_close(a["box"], [106, 246, 744, 662]) and a["visible"]
    b = g.project_box(geo(), B_DIMS, B_POSE)
    assert close(b["corners"], B_CORNERS) and box_close(b["box"], [547, 0, 1000, 169]) and b["visible"]
    json.dumps(a)  # plain floats


def test_project_behind_camera():
    r = g.project_box(geo(), A_DIMS, {"x": -20, "z": -100, "yaw": 0})
    assert r["visible"] is False and all(c is None for c in r["corners"])


def test_near_plane_clipping():
    r = g.project_box(geo(), (100, 100, 100), {"x": 0, "z": 0, "yaw": 0})  # straddles the camera
    assert any(c is None for c in r["corners"]) and any(c is not None for c in r["corners"])
    assert r["hull"] and r["box"] is not None


def test_floor_point():
    assert close(g.floor_point(geo(), 672, 900), (0.0, 243.79))
    assert close(g.floor_point(geo(), 300, 800), (-119.96, 301.00))
    assert g.floor_point(geo(), 672, 300) is None


def test_snap_to_wall():
    assert close([*g.snap_to_wall(geo(), -20, 300, 60, "w0").values()][:3], [-20, 370, 0])
    p = g.snap_to_wall(geo(), -20, 300, 60, "w1")
    assert close([p["x"], p["z"], p["yaw"]], [-170, 300, 90]) and p["wall"] == "w1"
    assert g.snap_to_wall(geo(), -20, 300, 60) is None
    assert g.snap_to_wall(geo(), -20, 120, 60) is None
    assert g.snap_to_wall(geo(), -175, 30, 60) is None  # outside w1's covered z-range
    p = g.snap_to_wall(geo(), -175, 200, 60)  # inside it: snaps
    assert p["wall"] == "w1" and close([p["x"]], [-170])


def test_measure():
    p1, p2 = pixel_of(geo(), 0, 243.79), pixel_of(geo(), -119.96, 301.00)
    assert abs(g.measure(geo(), p1, p2, "floor") - 132.9) < 0.5
    bottom, top = pixel_of(geo(), -110, 340), pixel_of(geo(), -110, 340, 220)
    assert abs(g.measure(geo(), bottom, top, "vertical") - 220) < 0.5
    assert g.measure(geo(), top, bottom, "vertical") == pytest.approx(g.measure(geo(), bottom, top, "vertical"))
    assert g.measure(geo(), (672, 300), (672, 900), "floor") is None


def test_relations():
    r = g.relations(geo(), A_DIMS, {**A_POSE, "wall": "w0"})
    assert r["ceiling_ratio"] == pytest.approx(0.846, abs=1e-3)
    assert r["wall_id"] == "w0" and r["wall_label"] == "hinten" and r["wall_length"] == 450 and r["wall_ratio"] == 0.4
    assert r["distance"] == pytest.approx(370.5, abs=0.1)
    r = g.relations(geo(), A_DIMS, A_POSE)  # no wall id: found by distance
    assert r["wall_id"] == "w0"
    r = g.relations({**geo(), "ceiling": None}, A_DIMS, {"x": 0, "z": 200, "yaw": 0})
    assert r["ceiling_ratio"] is None and r["wall_id"] is None


# ---------------- calibration ----------------

def test_calibrate_doubles():
    p1, p2 = pixel_of(geo(), 0, 243.79), pixel_of(geo(), -119.96, 301.00)
    gg, measured, s = g.calibrate(geo(), p1, p2, 2 * 132.9, "floor")
    assert abs(measured - 132.9) < 0.5 and s == pytest.approx(2.0, abs=0.01)
    assert gg["floor"]["d"] == pytest.approx(300, abs=1) and gg["ceiling"]["height"] == pytest.approx(520, abs=2)
    assert gg["walls"][0]["offset"] == pytest.approx(800, abs=3) and gg["walls"][0]["from"][0] == pytest.approx(-400, abs=2)
    assert FIXTURE["floor"]["d"] == 150  # input untouched
    k = gg["scale"]
    a = g.project_box(gg, [v * k for v in A_DIMS], {"x": -20 * k, "z": 370 * k, "yaw": 0})
    assert close(a["corners"], A_CORNERS)
    with pytest.raises(ValueError, match="unplausibel"):
        g.calibrate(geo(), p1, p2, 5 * 132.9, "floor")
    with pytest.raises(ValueError, match="nicht auf dem Boden"):
        g.calibrate(geo(), (672, 300), (672, 900), 100, "floor")


def test_confirmed_ceiling_survives_a_line():
    p1, p2 = pixel_of(geo(), 0, 243.79), pixel_of(geo(), -119.96, 301.00)
    gg, detected, _ = g.set_room_height(geo(), 260)            # detected 260 cm confirmed unchanged: a reference
    assert gg["refs"] == [{"kind": "height", "cm": 260.0, "raw": 260}] and gg["scale"] == 1.0
    with pytest.raises(g.RefConflict) as e:                      # line says ×1.44 → ceiling 3,75 m: ask, change nothing
        g.calibrate(gg, p1, p2, 1.44 * 132.9, "floor", label="Wand")
    assert e.value.info["kind"] == "height" and e.value.info["ceiling_cm"] == pytest.approx(374.4, abs=1)
    assert e.value.info["new"] == {"kind": "line", "label": "Wand", "cm": pytest.approx(191.4, abs=0.1)}
    g2, _, s2 = g.calibrate(gg, p1, p2, 1.04 * 132.9, "floor")     # within 10 %: both kept, fitted in between
    assert 1.0 < s2 < 1.04 and len(g2["refs"]) == 2
    g3, _, s3 = g.calibrate(gg, p1, p2, 1.44 * 132.9, "floor", replace=True)
    assert s3 == pytest.approx(1.44, abs=0.01) and [r["kind"] for r in g3["refs"]] == ["line"]
    g4, _, s4 = g.set_room_height(g2, 255)                      # a new ceiling replaces the old one, the line stays
    assert [r["kind"] for r in g4["refs"]] == ["line", "height"] and g4["refs"][1]["cm"] == 255


def test_pose_rescale():
    gg = g.rescale(geo(), 1.25)
    pose = {"x": -20 * 1.25, "z": 370 * 1.25, "yaw": 0}
    assert close([pose["x"], pose["z"]], [-25, 462.5])
    a = g.project_box(gg, [v * 1.25 for v in A_DIMS], pose)
    assert close(a["corners"], A_CORNERS)
    small = g.project_box(gg, A_DIMS, pose)["box"]
    assert small[2] - small[0] < 744 - 106 and small[3] - small[1] < 662 - 246


def test_room_height():
    gg, detected, s = g.set_room_height(geo(), 252)
    assert detected == 260 and s == pytest.approx(0.969, abs=1e-3) and gg["ceiling"]["height"] == pytest.approx(252, abs=0.2)
    nc = {**geo(), "ceiling": None}
    gg, detected, s = g.set_room_height(nc, 252)
    assert detected is None and s == 1.0 and gg["ceiling"] == {"height": 252.0, "manual": True}
    p1, p2 = pixel_of(gg, 0, 243.79), pixel_of(gg, -119.96, 301.00)
    gg2, _, s2 = g.calibrate(gg, p1, p2, 1.5 * 132.9, "floor")
    assert gg2["ceiling"]["height"] == 252.0 and s2 == pytest.approx(1.5, abs=0.01)
    with pytest.raises(ValueError):
        g.set_room_height(geo(), 600)


def test_depth_at():
    size = (1344, 1008)
    k = g.NPY_SIDE / 1344
    h, w = round(1008 * k), g.NPY_SIDE
    ys, xs = np.mgrid[0:h, 0:w]
    depth = (100 + xs + 1000 * ys).astype(np.float32)
    assert g.depth_at(depth, size, 672, 504) == pytest.approx(100 + (672 * k - 0.5) + 1000 * (504 * k - 0.5), abs=1e-2)
    assert g.depth_at(depth, size, 0, 0) == pytest.approx(100, abs=1e-3)
    assert g.depth_at(depth, size, -1, 10) is None and g.depth_at(depth, size, 10, 2000) is None
    gg = geo()
    flat = np.full((h, w), 300.0, np.float32)  # fronto-parallel wall at Z = 300 cm
    d = g.measure(gg, (672 - 100, 504), (672 + 100, 504), "depth", flat)
    assert d == pytest.approx(60, abs=0.5)  # 200 px at f = 1000 and Z = 300


def test_rgb_alpha_on_white():
    im = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    assert g.rgb(im).getpixel((0, 0)) == (255, 255, 255)
    p = Image.new("P", (4, 4), 0)
    p.info["transparency"] = 0
    assert g.rgb(p).getpixel((0, 0)) == (255, 255, 255)


# ---------------- fitting on synthetic depth ----------------

def synth(pitch_deg, W=672, H=504, f=500.0, cam_h=150.0, ceiling=260.0, noise=0.01, seed=0):
    """Analytic depth of the fixture room (floor, 3 walls, ceiling) seen from a camera pitched down."""
    p = math.radians(pitch_deg)
    n = np.array([0.0, -math.cos(p), -math.sin(p)])
    fr = {k: np.asarray(v) for k, v in g.frame_of(n, cam_h).items()}
    planes = [(n, cam_h), (-n, ceiling - cam_h)]
    planes += [(w["normal"][0] * fr["right"] + w["normal"][1] * fr["forward"], w["offset"]) for w in FIXTURE["walls"]]
    v, u = np.mgrid[0:H, 0:W] + 0.5
    rays = np.stack([(u - W / 2) / f, (v - H / 2) / f, np.ones_like(u)], -1)
    t = np.full((H, W), np.inf)
    for a, b in planes:
        ar = rays @ a
        hit = ar < -1e-9
        t = np.minimum(t, np.where(hit, -b / np.where(hit, ar, -1.0), np.inf))
    t *= 1 + noise * np.random.default_rng(seed).standard_normal(t.shape)
    return t.astype(np.float32), n


@pytest.mark.parametrize("pitch", [10, 40])
def test_fit_synthetic(pitch):
    depth, n = synth(pitch)
    r = g.fit_scene(depth, 500.0, (672, 504))
    assert r["error"] is None and r["floor"]
    ang = math.degrees(math.acos(min(1.0, float(np.dot(r["floor"]["normal"], n)))))
    assert ang < 2 and abs(r["floor"]["d"] - 150) < 3
    back = [w for w in r["walls"] if w["label"] == "hinten"]
    assert back and abs(back[0]["offset"] - 400) < 5
    assert r["horizon"] is not None
    if pitch == 10:
        assert r["ceiling"] and abs(r["ceiling"]["height"] - 260) < 8
        assert any(w["label"] == "links" and abs(w["offset"] - 200) < 5 for w in r["walls"])


def test_fit_no_floor():
    depth = np.full((300, 400), 500.0, np.float32)  # a flat wall only
    depth *= 1 + 0.01 * np.random.default_rng(3).standard_normal(depth.shape).astype(np.float32)
    r = g.fit_scene(depth, 400.0, (400, 300))
    assert r["floor"] is None and r["error"] == g.NO_FLOOR


def test_vanishing_points_synthetic():
    K = {"fx": 1000.0, "fy": 1000.0, "cx": 672.0, "cy": 504.0}
    vp_h = np.array([1700.0, 400.0])
    rng = np.random.default_rng(5)
    segs = []
    for _ in range(12):  # lines through the VP
        a = np.array([rng.uniform(0, 600), rng.uniform(100, 900)])
        d = (vp_h - a) / np.linalg.norm(vp_h - a)
        segs.append([*a, *(a + d * 200)])
    for x in np.linspace(100, 1200, 8):  # verticals (VP at infinity)
        segs.append([x, 200, x, 800])
    vps = g._vanishing(np.array(segs), K, (1344, 1008))
    assert vps["vertical"] is not None and vps["vertical"][1][1] < -0.99
    pt = g._vp_point(vps["h1"][0], (1344, 1008))
    assert pt and abs(pt[0] - 1700) < 5 and abs(pt[1] - 400) < 5


# ---------------- analyze (no model: depth estimation stubbed) ----------------

@pytest.fixture
def stub_depth(monkeypatch):
    depth, _ = synth(10)
    monkeypatch.setattr(g, "_estimate", lambda img, max_side=1536: (depth, 500.0 * img.width / 672, True))
    return depth


def test_analyze_pipeline(tmp_path, stub_depth):
    img = Image.new("RGB", (672, 504), (200, 200, 200))
    r = g.analyze(img, "uploads/room.png", geo_dir=tmp_path)
    assert r["ok"] and r["confidence"] in ("high", "medium") and r["depth_preview"] == "geometry/room-depth.jpg"
    assert (tmp_path / "room.json").exists() and (tmp_path / "room-depth.jpg").exists()
    npy = np.load(tmp_path / "room-depth.npy")
    assert npy.dtype == np.float16 and max(npy.shape) == g.NPY_SIDE
    assert g.load_cached("uploads/room.png", tmp_path)["floor"] == r["floor"]
    # calibrated re-analysis keeps scale and manual ceiling
    cal = g.rescale(r, 1.1)
    cal["ceiling"] = {"height": 255.0, "manual": True}
    g.save_cached(cal, tmp_path)
    assert g.analyze(img, "uploads/room.png", geo_dir=tmp_path) == cal  # cached
    r2 = g.analyze(img, "uploads/room.png", force=True, geo_dir=tmp_path)
    assert r2["scale"] == pytest.approx(1.1) and r2["ceiling"] == {"height": 255.0, "manual": True}
    assert r2["floor"]["d"] == pytest.approx(r["floor"]["d"] * 1.1, abs=0.1)
    dm = g.depth_map("uploads/room.png", tmp_path)
    assert dm is not None and float(np.median(dm)) == pytest.approx(float(np.median(npy)) * 1.1, rel=1e-2)


def test_analyze_never_raises(tmp_path, monkeypatch):
    def boom(img, max_side=1536):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(g, "_estimate", boom)
    r = g.analyze(Image.new("RGB", (64, 48)), "uploads/x.png", geo_dir=tmp_path)
    assert r["ok"] is False and r["error"] and r["floor"] is None
    assert not (tmp_path / "x.json").exists()  # transient failures are not cached


def test_analyze_no_floor_cached(tmp_path, monkeypatch):
    depth = np.full((120, 160), 500.0, np.float32)
    monkeypatch.setattr(g, "_estimate", lambda img, max_side=1536: (depth, 200.0, False))
    r = g.analyze(Image.new("RGB", (160, 120)), "uploads/sky.png", geo_dir=tmp_path)
    assert r["ok"] is False and r["error"] == g.NO_FLOOR and r["confidence"] == "low"
    assert g.load_cached("uploads/sky.png", tmp_path)["error"] == g.NO_FLOOR  # deterministic: cached, "Neu vermessen" forces


# ---------------- router ----------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import geometry_api

    (tmp_path / "uploads").mkdir()
    Image.new("RGB", (1344, 1008), (180, 180, 180)).save(tmp_path / "uploads" / "test.png")
    monkeypatch.setattr(geometry_api, "OUT", tmp_path)
    monkeypatch.setattr(geometry_api, "GEO_DIR", tmp_path / "geometry")
    app = FastAPI()
    app.include_router(geometry_api.router)
    return TestClient(app), tmp_path


def test_router_paths(client):
    c, root = client
    assert c.post("/api/geometry/analyze", json={"room": "../server.py"}).status_code == 400
    assert c.post("/api/geometry/analyze", json={"room": "uploads/missing.png"}).status_code == 400
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "room_height_cm": 250})
    assert r.status_code == 400 and r.json()["detail"] == "Raum noch nicht vermessen."


def test_router_cached_and_calibrate(client):
    c, root = client
    g.save_cached(geo(), root / "geometry")
    r = c.post("/api/geometry/analyze", json={"room": "uploads/test.png"})
    assert r.status_code == 200 and r.json() == FIXTURE
    p1, p2 = pixel_of(geo(), 0, 243.79), pixel_of(geo(), -119.96, 301.00)
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "p1": p1, "p2": p2, "length_cm": 150, "mode": "floor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["measured_cm"] - 132.9) < 0.5 and body["scale"] == pytest.approx(150 / 132.9, abs=0.01)
    assert g.load_cached("uploads/test.png", root / "geometry")["scale"] == body["scale"]
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "p1": p1, "p2": p2, "length_cm": 900, "mode": "floor"})
    assert r.status_code == 400 and "unplausibel" in r.json()["detail"]
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "room_height_cm": 270}).json()
    # both confirmed references stay: the scale fits the 150 cm line and the 270 cm ceiling (weighted 1:2)
    assert r["geometry"]["ceiling"]["height"] == pytest.approx(277.6, abs=0.3) and r["measured_cm"] is not None
    assert [x["kind"] for x in r["geometry"]["refs"]] == ["line", "height"]
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "room_height_cm": 360}).json()
    assert r["conflict"]["kind"] == "line" and r["conflict"]["implied_cm"] == pytest.approx(184, abs=1) and r["scaled"] is False
    assert g.load_cached("uploads/test.png", root / "geometry")["ceiling"]["height"] == pytest.approx(277.6, abs=0.3)
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "room_height_cm": 360, "on_conflict": "replace"}).json()
    assert r["geometry"]["ceiling"]["height"] == pytest.approx(360, abs=0.2) and [x["kind"] for x in r["geometry"]["refs"]] == ["height"]
    r = c.post("/api/geometry/calibrate", json={"room": "uploads/test.png", "reset": True}).json()
    assert r["scale"] == 1.0 and r["geometry"]["floor"]["d"] == pytest.approx(150, abs=0.05)
    assert g.load_cached("uploads/test.png", root / "geometry")["scale"] == 1.0


def test_router_analyze_stubbed(client, stub_depth):
    c, root = client
    r = c.post("/api/geometry/analyze", json={"room": "uploads/test.png", "force": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (root / "geometry" / "test.json").exists()


def test_router_low_memory(client, monkeypatch):
    import geometry_api

    c, root = client
    monkeypatch.setattr(geometry_api, "_mem_available_gb", lambda: 2.0)
    monkeypatch.setattr(g, "_model", None)
    r = c.post("/api/geometry/analyze", json={"room": "uploads/test.png"}).json()
    assert r["ok"] is False and r["error"] == "Zu wenig freier Speicher – später erneut versuchen."


# ---------------- real model (opt-in: BFL_REAL_DEPTH=1, run under the GPU lock) ----------------

@pytest.mark.skipif(os.environ.get("BFL_REAL_DEPTH") != "1", reason="real DepthPro run is opt-in")
def test_real_room(tmp_path):
    room = Path(__file__).resolve().parents[1] / "outputs" / "uploads" / "20261016-5d0db410c1.png"
    if not room.exists():
        pytest.skip("legacy room photo missing")
    try:
        r = g.analyze(Image.open(room), "uploads/20261016-5d0db410c1.png", force=True, geo_dir=tmp_path)
    finally:
        g.unload_depth()
    assert r["ok"] and 800 < r["intrinsics"]["fx"] < 960 and 80 <= r["floor"]["d"] <= 220 and r["walls"]


def test_pose_at_camera_not_visible():
    for z in (20, 40, 60):  # a wardrobe dragged under the camera would make the whole photo the edit box
        assert g.project_box(geo(), (100, 210, 60), {"x": 0, "z": z, "yaw": 0})["visible"] is False
    assert g.project_box(geo(), A_DIMS, A_POSE)["visible"] is True


def test_failed_reanalysis_keeps_good_cache(tmp_path, monkeypatch):
    good = {**geo(), "room": "uploads/x.png", "scale": 0.9}
    g.save_cached(good, tmp_path)
    monkeypatch.setattr(g, "_estimate", lambda img: (np.full((48, 64), 300, np.float32), 50.0, True))
    monkeypatch.setattr(g, "fit_scene", lambda *a: {"floor": None, "error": g.NO_FLOOR})
    r = g.analyze(Image.new("RGB", (64, 48)), "uploads/x.png", force=True, geo_dir=tmp_path)
    assert r["ok"] is False and g.load_cached("uploads/x.png", tmp_path)["scale"] == 0.9
