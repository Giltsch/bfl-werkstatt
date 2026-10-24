import copy
import json
import os
import shutil
import subprocess
import sys
from glob import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import geometry
import place
import workshop

HERE = Path(__file__).parent
SAMPLE = json.loads((HERE / "fixtures" / "geometry_sample.json").read_text())
GEOS = json.loads((HERE / "fixtures" / "place_geometries.json").read_text())  # demo order + 8 analysed FLUX rooms
CAT = workshop.CATALOG
A_CORNERS = [[367.19, 749.73], [865.97, 749.73], [410.08, 690.35], [838.68, 690.35],
             [331.10, 107.39], [888.93, 107.39], [383.87, 141.49], [855.36, 141.49]]
NONE = {"mode": "none", "pose": None, "box": None}


def pc(pid, w=None, h=None, d=None, pose=None, **extra):
    pl = {"mode": "3d", "pose": pose, "box": None} if pose else dict(NONE)
    return {"id": pid, "type": None, "w": w, "h": h, "d": d, "placement": pl, **extra}


def doc_of(geo, *pieces):
    return {"room": "uploads/x.png", "geometry": copy.deepcopy(geo), "pieces": list(pieces)}


def near(a, b, tol=0.5):
    assert abs(a - b) <= tol, f"{a} vs {b}"


# ---------------- unit tests (pure Python) ----------------

def test_init_needs_a_usable_measurement():
    assert place.init(None) is None
    assert place.init({**SAMPLE, "ok": False}) is None
    assert place.init({k: v for k, v in SAMPLE.items() if k != "floor"}) is None
    s = place.init(SAMPLE)
    assert s.size == [1344, 1008]
    for k in ("origin", "right", "up", "forward"):
        assert all(abs(a - b) <= 1e-3 for a, b in zip(s.frame[k], SAMPLE["frame"][k]))
    assert place.scene_for({"room": None, "geometry": SAMPLE}) is None
    assert place.scene_for({"room": "uploads/x.png", "geometry": None}) is None


def test_project_box_matches_the_js_fixture_numbers():
    s = place.init(SAMPLE)
    a = s.project_box(pc("a", 180, 220, 60, {"x": -20, "z": 370, "yaw": 0, "wall": None}))
    for c, e in zip(a["corners"], A_CORNERS):
        near(c[0], e[0]); near(c[1], e[1])
    assert a["box"] == [106, 246, 744, 662] and a["visible"]
    behind = s.project_box(pc("c", 180, 220, 60, {"x": -20, "z": -100, "yaw": 0}))
    assert behind["visible"] is False and all(c is None for c in behind["corners"])


def test_floor_point_and_to_image_agree_with_geometry_py():
    s = place.init(SAMPLE)
    for u, v in [(672, 900), (300, 800), (1200, 990), (50, 700)]:
        p, q = s.floor_point(u, v), geometry.floor_point(SAMPLE, u, v)
        near(p["x"], q[0], 1e-6); near(p["z"], q[1], 1e-6)
        back = s.to_image([p["x"], 0, p["z"]])
        near(back[0], u, 1e-6); near(back[1], v, 1e-6)
    assert s.floor_point(672, 300) is None
    assert s.to_image([0, 0, -500]) is None


def test_js_helpers():
    assert [place.js_round(x) for x in (0.5, 1.5, 2.5, -0.5, -1.5, -2.6)] == [1, 2, 3, 0, -1, -3]
    assert place._wrap(190) == -170 and place._wrap(-190) == 170 and place._wrap(180) == -180
    assert place.to_grid([672.4, 504.6], [1344, 1008]) == [501, 500]
    assert place.box_to_grid([[331.1, 107.39], [888.93, 107.39], [865.97, 749.73]], [1344, 1008]) == [106, 246, 744, 662]
    assert place.box_to_grid([[-50, -10], [100, 2000]], [1344, 1008]) == [0, 0, 1000, 75]
    assert place.box_to_grid([], [1344, 1008]) is None
    assert place.hull_of([[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5], [0, 0]]) == [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert place.from_grid([500, 250], [1000, 800]) == [250.0, 400.0]


def test_dims_and_elev_like_js():
    s = place.init(SAMPLE, {"pieces": {"schrank": {"dims": [200, 240, 60]}}})
    assert s.dims({"type": "schrank", "w": None, "h": 250, "d": ""}) == [200, 250, 60]
    assert s.dims({"type": "schrank", "w": "120", "h": 0, "d": "x"}) == [120, 240, 60]
    assert s.dims({"type": None}) == [100, 100, 60]
    assert s.elev({"elev": 150}) == 150 and s.elev({"elev": None}) == 0 and s.elev({"elev": -5}) == 0
    assert s.elev({}) == 0 and s.elev({"elev": "abc"}) == 0


def test_snap_and_place_near_a_wall():
    s = place.init(SAMPLE)
    p = pc("x", 100, 100, 60)
    a = s.snap_to_wall({"x": -20, "z": 300, "yaw": 0}, p, "w0")
    near(a["x"], -20); near(a["z"], 370); near(a["yaw"], 0); assert a["wall"] == "w0"
    assert s.snap_to_wall({"x": -20, "z": 300, "yaw": 0}, p) is None
    # the middle of a box standing 10 cm in front of the back wall → snaps to it
    c = s.to_image([0, 50, 360])
    r = s.place(c[0], c[1], p)
    assert r["wall"] == "w0"
    near(r["z"], 370, 1)
    assert s.place(672, 20, p) is None  # far above anything a standing piece can reach


def test_placement_for_floor_piece_on_the_demo_room():
    g = GEOS["demo_wohnzimmer"]
    s = place.init(g, CAT)
    sb = pc("sb", 200, 50, 45, type="sideboard")
    # tap the middle of where a sideboard at the back wall would appear
    w0 = s.wall_by_id("w0")
    fx, fz = 0.0, -w0["offset"] / w0["normal"][1] - 40  # a little in front of the back wall
    c = s.to_image([fx, 25, fz])
    grid = [c[1] / g["size"][1] * 1000, c[0] / g["size"][0] * 1000]
    pl = place.placement_for(doc_of(g, sb), sb, grid, catalog=CAT)
    assert pl["mode"] == "3d" and pl["pose"]["wall"] == "w0" and "elev" not in pl
    assert set(pl["pose"]) == {"x", "z", "yaw", "wall"}
    assert pl["box"] and pl["box"][2] > pl["box"][0]
    near(s.wall_dist(w0, pl["pose"]["x"], pl["pose"]["z"]), 22.5, 1e-6)  # back flush with the wall
    free = place.placement_for(doc_of(g, sb), sb, grid, against_wall=False, catalog=CAT)
    assert free["pose"]["wall"] is None and free["pose"]["yaw"] == 0
    # by id, and with an explicit yaw
    turned = place.placement_for(doc_of(g, sb), "sb", grid, against_wall=False, yaw=30, catalog=CAT)
    assert turned["pose"]["yaw"] == 30


def test_placement_for_keeps_the_current_turn_and_forces_the_wall():
    g = GEOS["demo_wohnzimmer"]
    s = place.init(g, CAT)
    t = pc("t", 180, 76, 90, {"x": 0, "z": 300, "yaw": 15, "wall": None}, type="tisch")
    c = s.to_image([-60, 38, 280])
    grid = [c[1] / 1024 * 1000, c[0] / 1536 * 1000]
    pl = place.placement_for(doc_of(g, t), t, grid, catalog=CAT)
    assert pl["pose"]["wall"] is None and pl["pose"]["yaw"] == 15
    forced = place.placement_for(doc_of(g, t), t, grid, against_wall=True, catalog=CAT)
    assert forced["pose"]["wall"] in ("w0", "w1")
    w = s.wall_by_id(forced["pose"]["wall"])
    near(s.wall_dist(w, forced["pose"]["x"], forced["pose"]["z"]), 45, 1e-6)
    near(forced["pose"]["yaw"], s.wall_yaw(w), 1e-9)


def test_placement_for_wall_hung_piece_hangs_and_returns_elev():
    g = GEOS["demo_wohnzimmer"]
    s = place.init(g, CAT)
    wr = pc("wr", 160, 30, 25, type="wandregal", elev=119)
    hit = None
    for v in range(150, 600, 25):  # find a tap that meets a wall above the floor
        hit = s.wall_hit(768, v)
        if hit and 80 < hit["y"] < 220:
            break
    assert hit
    pl = place.placement_for(doc_of(g, wr), wr, [v / 1024 * 1000, 500], catalog=CAT)
    assert pl["pose"]["wall"] == hit["wall"]["id"]
    assert pl["elev"] == max(0, place.js_round(hit["y"] - 15))
    assert "elev" not in pl["pose"]
    ops = place.tap_ops(doc_of(g, wr), wr, pl, catalog=CAT)
    assert ops[0] == {"op": "set", "path": "pieces.wr.placement", "value": {k: pl[k] for k in ("mode", "pose", "box")}}
    assert {"op": "set", "path": "pieces.wr.elev", "value": pl["elev"]} in ops
    # a wall-hung type without an elev (fresh from intent.py) gets its catalog elevation and hangs too
    bare = pc("hs", type="haengeschrank")
    pl2 = place.placement_for(doc_of(g, bare), bare, [v / 1024 * 1000, 500], catalog=CAT)
    assert pl2["pose"]["wall"] == hit["wall"]["id"] and isinstance(pl2["elev"], int)
    ops2 = place.tap_ops(doc_of(g, bare), bare, pl2, catalog=CAT)
    paths = [o["path"] for o in ops2]
    assert paths == ["pieces.hs.placement", "pieces.hs.elev", "pieces.hs.w", "pieces.hs.h", "pieces.hs.d"]
    assert [o["value"] for o in ops2[2:]] == CAT["pieces"]["haengeschrank"]["dims"]


def test_placement_survives_projects_normalisation():
    import projects
    g = GEOS["demo_wohnzimmer"]
    wr = pc("wr", 160, 30, 25, type="wandregal", elev=119)
    s = place.init(g, CAT)
    v = next(v for v in range(150, 600, 25) if (h := s.wall_hit(768, v)) and 80 < h["y"] < 220)
    pl = place.placement_for(doc_of(g, wr), wr, [v / 1024 * 1000, 500], catalog=CAT)
    piece = projects._piece({**wr, "elev": pl["elev"], "placement": {k: pl[k] for k in ("mode", "pose", "box")}}, "wr")
    assert piece["placement"]["pose"] == pl["pose"] and piece["placement"]["box"] == pl["box"]
    assert piece["elev"] == pl["elev"]


def test_untappable_points_return_none():
    g = GEOS["demo_wohnzimmer"]
    sb = pc("sb", 200, 50, 45, type="sideboard")
    assert place.placement_for(doc_of(g, sb), sb, [5, 500], catalog=CAT) is None  # sky above the horizon
    assert place.placement_for({"room": None, "geometry": g, "pieces": [sb]}, sb, [800, 500], catalog=CAT) is None
    assert place.placement_for(doc_of(g, sb), "missing", [800, 500], catalog=CAT) is None
    assert place.placement_for(doc_of(g, sb), sb, None, catalog=CAT) is None


def test_calibration_scale_keeps_the_picture():
    # a new calibration scales the room and every pose by k; scaled dims then project onto the same pixels (W3D test)
    g = GEOS["demo_wohnzimmer"]
    k = GEOS["demo_calibrated_x1.12"]["scale"] / (g["scale"] or 1)
    pieces = [pc("a", 180, 220, 60, {"x": -20, "z": 370, "yaw": 10, "wall": None}),
              {"id": "b", "placement": {"mode": "2d", "pose": None, "box": [1, 2, 3, 4]}}]
    before = place.init(g).project_box(pieces[0])["corners"]
    place.scale_poses(pieces, k)
    near(pieces[0]["placement"]["pose"]["x"], -20 * k, 1e-9)
    assert pieces[1]["placement"]["box"] == [1, 2, 3, 4]
    scaled = {**pieces[0], "w": 180 * k, "h": 220 * k, "d": 60 * k}
    after = place.init(GEOS["demo_calibrated_x1.12"]).project_box(scaled)["corners"]
    for a, b in zip(before, after):
        near(a[0], b[0], 0.5); near(a[1], b[1], 0.5)


def test_relations_and_distance():
    s = place.init(SAMPLE)
    a = pc("a", 180, 220, 60, {"x": -20, "z": 370, "yaw": 0, "wall": None})
    r = s.relations(a)
    near(r["ceilingRatio"], 0.846, 1e-3); near(r["ceilingHeight"], 260)
    near(r["wallRatio"], 0.4, 1e-6); near(r["wallLength"], 450); assert r["wallLabel"] == "hinten"
    near(r["distance"], 370.5, 0.1)
    assert not r["tooHigh"] and not r["tooWide"] and not r["outOfImage"]
    assert s.distance({"x": 0, "z": 300}) > 0


# ---------------- cross-check against static/werkstatt3d.js in node ----------------

def _node() -> str | None:
    n = shutil.which("node")
    if n:
        return n
    found = sorted(glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    return found[-1] if found else None


NODE = _node()
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

PIECES = [  # floor-standing, tall, free-standing, typeless, wall-hung (explicit elev), wall-hung low, turned
    pc("sb", 200, 50, 45, type="sideboard"),
    pc("sr", None, None, None, type="schrank"),
    pc("ti", 180, 76, 90, type="tisch"),
    pc("x", None, None, None),
    pc("wr", 160, 30, 25, type="wandregal", elev=119),
    pc("hs", 120, 60, 35, type="haengeschrank", elev=40),
    pc("bt", 180, 95, 210, {"x": 50, "z": 4000, "yaw": 33, "wall": None}, type="bett"),  # far away: taps never hit it
]
POINTS = [[t, l] for t in range(40, 1000, 80) for l in range(30, 1000, 85)]  # 12 × 12 = 144 grid points


def _run_node(payload, tmp_path):
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(payload))
    r = subprocess.run([NODE, str(HERE / "place_xcheck.mjs"), str(f)], capture_output=True, text=True, timeout=300,
                       cwd=str(HERE.parent))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def _cmp(py, js, path="", stats=None):
    """Recursive comparison: floats within 1e-6 relative (1e-6 absolute near 0), grid boxes within 1 unit."""
    if js is None or py is None:
        assert py is None and js is None, f"{path}: py={py!r} js={js!r}"
        return
    if isinstance(js, dict):
        assert set(py) == set(js), f"{path}: keys {sorted(py)} vs {sorted(js)}"
        for k in js:
            _cmp(py[k], js[k], f"{path}.{k}", stats)
        return
    if isinstance(js, list):
        assert len(py) == len(js), f"{path}: len {len(py)} vs {len(js)}"
        box = path.endswith(".box") or path.endswith(".value") and len(js) == 4 and all(isinstance(x, int) for x in js)
        for i, (a, b) in enumerate(zip(py, js)):
            if box and isinstance(b, (int, float)):
                assert abs(a - b) <= 1, f"{path}[{i}]: {a} vs {b}"
                if stats is not None:
                    stats["box_exact"] += a == b
                    stats["box_n"] += 1
            else:
                _cmp(a, b, f"{path}[{i}]", stats)
        return
    if isinstance(js, bool) or isinstance(js, str):
        assert py == js, f"{path}: {py!r} vs {js!r}"
        return
    a, b = float(py), float(js)
    err = abs(a - b) / max(1.0, abs(b))
    assert err <= 1e-6, f"{path}: {a} vs {b}"
    if stats is not None:
        stats["max_rel"] = max(stats["max_rel"], err)
        stats["n"] += 1


def _py_prim(c):
    s = place.init(GEOS[c["geo"]], CAT)
    u, v = c["uv"]
    p, pose = c["piece"], c["pose"]
    wh = s.wall_hit(u, v)
    nw = s.nearest_wall(pose["x"], pose["z"])
    return {
        "floorPoint": s.floor_point(u, v), "centreAt": s.centre_at(u, v, p, c.get("yaw") or 0),
        "wallHit": wh and {"t": wh["t"], "wall": wh["wall"]["id"], "x": wh["x"], "y": wh["y"], "z": wh["z"]},
        "hang": s.hang(u, v, p), "place": s.place(u, v, p, c.get("yaw") or 0),
        "toImage": s.to_image([pose["x"], 50, pose["z"]]), "projectBox": s.project_box(p, pose),
        "nearestWall": nw and {"wall": nw["wall"]["id"], "dist": nw["dist"]},
        "snapAuto": s.snap_to_wall(pose, p), "snapW0": s.snap_to_wall(pose, p, "w0"),
        "distance": s.distance(pose), "relations": s.relations({**p, "placement": {"mode": "3d", "pose": pose, "box": None}}),
        "dims": s.dims(p), "elev": s.elev(p), "toGrid": place.to_grid([u, v], s.size),
    }


@needs_node
def test_primitives_match_werkstatt3d_js(tmp_path):
    prims = []
    for name, g in GEOS.items():
        W, H = g["size"]
        for i, p in enumerate(PIECES[:6]):
            for t, l in POINTS[i::6]:
                u, v = l / 1000 * W, t / 1000 * H
                # a pose somewhere on the floor near the tap (or a fixed one when the tap misses the floor)
                fp = place.init(g, CAT).floor_point(u, v) or {"x": 10.0, "z": 300.0}
                prims.append({"geo": name, "uv": [u, v], "piece": p, "yaw": (t * 7 + l) % 90 - 45,
                              "pose": {"x": fp["x"], "z": fp["z"], "yaw": (t + l * 3) % 360 - 180, "wall": None}})
    js = _run_node({"geometries": GEOS, "catalog": CAT, "prims": prims}, tmp_path)["prims"]
    stats = {"max_rel": 0.0, "n": 0, "box_exact": 0, "box_n": 0}
    for c, j in zip(prims, js):
        _cmp(_py_prim(c), j, f"{c['geo']}:{c['piece']['id']}@{c['uv']}", stats)
    assert len(js) == len(prims) > 1000
    print(f"\nprimitives: {len(prims)} cases, {stats['n']} floats, max rel err {stats['max_rel']:.2e}, "
          f"boxes exact {stats['box_exact']}/{stats['box_n']}")


@needs_node
def test_taps_match_step_platzieren(tmp_path):
    cases = [{"geo": name, "piece": p, "point": pt, "wall": False}
             for name in GEOS for p in PIECES for pt in POINTS]
    cases += [{"geo": name, "piece": p, "point": pt, "wall": True}
              for name in GEOS for p in PIECES[:5] for pt in POINTS[::3]]
    out = _run_node({"geometries": GEOS, "catalog": CAT, "cases": cases}, tmp_path)["taps"]
    stats = {"max_rel": 0.0, "n": 0, "box_exact": 0, "box_n": 0}
    placed = hung = snapped = hits = 0
    for c, j in zip(cases, out):
        if j.get("hit"):
            hits += 1
            continue
        d = doc_of(GEOS[c["geo"]], c["piece"])
        py = place.placement_for(d, c["piece"], c["point"], against_wall=True if c["wall"] else None, catalog=CAT)
        where = f"{c['geo']}:{c['piece']['id']}@{c['point']} wall={c['wall']}"
        _cmp(py, j["result"], where, stats)
        if py is None:
            continue
        placed += 1
        hung += "elev" in py
        snapped += py["pose"]["wall"] is not None
        if not c["wall"]:
            _cmp(place.tap_ops(d, c["piece"], py, catalog=CAT), j["ops"], where + " ops", stats)
    n = len(cases)
    print(f"\ntaps: {n} cases ({hits} hit a piece), {placed} placed ({snapped} at a wall, {hung} hung), "
          f"{stats['n']} floats, max rel err {stats['max_rel']:.2e}, boxes exact {stats['box_exact']}/{stats['box_n']}")
    assert placed > 2000 and hung > 100 and snapped > 300
    assert stats["box_exact"] == stats["box_n"]
