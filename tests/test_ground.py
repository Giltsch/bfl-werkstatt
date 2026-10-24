import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import ground
import judge
import projects

# §2.1 geometry fixture (= tests/test_workshop.py): camera 150 cm above the floor, back wall w0 at z = 400 cm,
# left wall w1 at x = -200, right wall w2 at x = 250, ceiling 260 cm
GEO = {"version": 1, "ok": True, "error": None, "room": "uploads/test.png", "size": [1344, 1008],
       "intrinsics": {"fx": 1000.0, "fy": 1000.0, "cx": 672.0, "cy": 504.0},
       "floor": {"normal": [0.0, -0.984808, -0.173648], "d": 150.0, "inliers": 0.4},
       "frame": {"origin": [0.0, 147.7212, 26.0472], "right": [1.0, 0.0, 0.0], "up": [0.0, -0.984808, -0.173648],
                 "forward": [0.0, -0.173648, 0.984808]},
       "walls": [{"id": "w0", "label": "hinten", "normal": [0.0, -1.0], "offset": 400.0, "from": [-200.0, 400.0], "to": [250.0, 400.0]},
                 {"id": "w1", "label": "links", "normal": [1.0, 0.0], "offset": 200.0, "from": [-200.0, 400.0], "to": [-200.0, 100.0]},
                 {"id": "w2", "label": "rechts", "normal": [-1.0, 0.0], "offset": 250.0, "from": [250.0, 400.0], "to": [250.0, 100.0]}],
       "ceiling": {"height": 260.0}, "horizon": [[0.0, 327.67], [1344.0, 327.67]],
       "vps": {"vertical": None, "h1": None, "h2": None}, "scale": 1.0, "depth_range": [150.0, 600.0],
       "depth_preview": None, "confidence": "high", "seconds": 0.0}

PHOTO = Image.new("RGB", (1344, 1008), (200, 190, 180))


@pytest.fixture
def vlm(monkeypatch):
    """Fake VLM: answers are queued JSON dicts (or exceptions); every request is recorded."""
    state = {"answers": [], "calls": []}

    def ask(system, content, schema, name, timeout, max_tokens=700, temperature=0):
        state["calls"].append({"system": system, "content": content, "schema": schema, "name": name})
        a = state["answers"].pop(0)
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a)

    monkeypatch.setattr(judge, "ask", ask)
    monkeypatch.setattr(judge, "status", lambda force=False: {"available": True})
    monkeypatch.setattr(judge, "wait_memory", lambda limit=120, step=5: True)
    monkeypatch.setattr(judge, "mark_down", lambda: state.setdefault("down", True))
    return state


# ---------------- helpers

def test_box_point_and_iou_helpers():
    assert ground._box_xyxy([700, 500, 300, 100]) == [100, 300, 500, 700]      # unsorted frame → [t, l, b, r]
    assert ground._box_xyxy([10, 10, 10, 50]) is None                          # empty
    assert ground._box_xyxy([-5, 20, 1200, 30]) == [20, 0, 30, 1000]           # clamped
    assert ground._box_xyxy("nope") is None
    assert ground._point([1200, -3]) == [1000.0, 0.0] and ground._point([1]) is None
    assert ground._point([float("nan"), 3]) is None
    assert ground.iou([0, 0, 100, 100], [0, 0, 100, 100]) == 1.0
    assert ground.iou([0, 0, 100, 100], [0, 50, 100, 150]) == pytest.approx(1 / 3)
    assert ground.iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0 and ground.iou(None, [0, 0, 1, 1]) == 0.0
    assert ground.union_box([{"box": [10, 20, 30, 40]}, {"box": [5, 30, 50, 35]}]) == [5, 20, 50, 40]
    assert ground.union_box([]) is None
    assert ground.inside([50, 50], [0, 0, 100, 100]) and not ground.inside([120, 50], [0, 0, 100, 100])
    assert ground.inside([120, 50], [0, 0, 100, 100], margin=25)


def test_image_accepts_bytes_paths_and_rgba(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGBA", (40, 30), (0, 0, 0, 0)).save(p)
    for src in (p, str(p), p.read_bytes(), Image.open(p)):
        im = ground._image(src)
        assert im.mode == "RGB" and im.size == (40, 30) and im.getpixel((0, 0)) == (255, 255, 255)


def test_grid_cells_and_overlay():
    assert ground.cell_label(0, 0) == "A1" and ground.cell_label(11, 7) == "L8"
    assert ground.parse_cell("C4", 12, 8) == (2, 3)
    assert ground.parse_cell("cell: d 7", 12, 8) == (3, 6)
    assert ground.parse_cell("Z1", 12, 8) is None and ground.parse_cell("C9", 12, 8) is None
    assert ground.parse_cell("", 12, 8) is None
    big = Image.new("RGB", (3000, 2000), (128, 128, 128))
    ov = ground.grid_overlay(big)
    assert ov.size == (1024, 683)                                              # downscaled like judge.data_url
    a = np.asarray(ov).astype(int)
    assert (np.abs(a - 128).sum(axis=2) > 60).mean() > 0.01                    # lines + labels drawn


# ---------------- geometry

def test_surface_at_floor_wall_ceiling():
    mid = ground.surface_at(GEO, [500, 500])          # optical axis: meets the back wall at ~80 cm before the floor
    assert mid["surface"] == "wall" and mid["wall"] == "w0"
    assert mid["z"] == pytest.approx(400, abs=0.5) and mid["y"] == pytest.approx(79.5, abs=1)
    low = ground.surface_at(GEO, [500, 1000])
    assert low["surface"] == "floor" and low["wall"] is None and low["z"] == pytest.approx(200.9, abs=1)
    assert low["y"] == pytest.approx(0, abs=0.01)
    top = ground.surface_at(GEO, [500, 0])
    assert top["surface"] == "ceiling"
    left = ground.surface_at(GEO, [10, 450])
    assert left["surface"] == "wall" and left["wall"] == "w1"
    assert ground.surface_at(None, [500, 500])["surface"] is None
    assert ground.surface_at({**GEO, "ok": False}, [500, 500])["surface"] is None


def test_surface_at_ignores_walls_outside_their_extent():
    short = copy.deepcopy(GEO)
    short["walls"][0]["from"], short["walls"][0]["to"] = [150.0, 400.0], [250.0, 400.0]   # back wall only on the right
    s = ground.surface_at(short, [500, 500])   # x = 0 is 150 cm left of the segment (> 50 cm margin): no wall there
    assert s["surface"] in ("floor", "ceiling", None) and s["wall"] is None


def test_nearest_wall_and_check_place():
    assert ground.nearest_wall(GEO, 0, 380) == ("w0", 20.0)
    assert ground.nearest_wall(GEO, -190, 200)[0] == "w1"
    # a floor point 20 cm in front of the back wall belongs to it
    p = None
    for v in range(600, 1000, 2):
        s = ground.surface_at(GEO, [500, v])
        if s["surface"] == "floor" and s["z"] <= 385:
            p = [500, v]
            break
    chk = ground.check_place(GEO, p, "floor")
    assert chk["surface"] == "floor" and chk["wall"] == "w0" and chk["penalty"] == 0
    # free floor in the middle of the room: no wall
    chk = ground.check_place(GEO, [500, 1000])
    assert chk["surface"] == "floor" and chk["wall"] is None
    # the VLM said "wall" for a floor point: a mild penalty only
    assert ground.check_place(GEO, [500, 1000], "wall")["penalty"] == pytest.approx(0.1)
    # on the ceiling: slides down onto the wall below, loses confidence
    assert ground.surface_at(GEO, [500, 10])["surface"] == "ceiling"
    chk = ground.check_place(GEO, [500, 10])
    assert chk["surface"] == "wall" and chk["wall"] == "w0" and chk["point"][1] > 10 and chk["penalty"] >= 0.2
    assert ground.surface_at(GEO, chk["point"])["surface"] == "wall"
    # no geometry: passes through untouched
    assert ground.check_place(None, [10, 20]) == {"point": [10, 20], "surface": None, "wall": None, "penalty": 0.0,
                                                  "notes": []}


def test_snap_to_remove_only_for_replacement_phrases():
    doc = {"removes": [[400, 300, 600, 500], {"poly": [[600, 600], [700, 600], [700, 700]], "box": [600, 600, 700, 700]}]}
    assert ground.snap_to_remove(doc, "wo die alte Kommode steht", [380, 450]) == [400.0, 500.0]
    assert ground.snap_to_remove(doc, "anstelle des Regals", [650, 650]) == [650.0, 650.0]
    assert ground.snap_to_remove(doc, "links neben dem Fenster", [380, 450]) is None
    assert ground.snap_to_remove(doc, "wo der Tisch steht", [100, 100]) is None
    assert ground.snap_to_remove(None, "wo der Tisch steht", [400, 500]) is None


# ---------------- locate

def test_locate_native_with_geometry(vlm):
    vlm["answers"] = [{"reference_de": "rechts neben dem Fenster, Rückwand", "found": True, "surface": "wall",
                       "point_2d": [500, 500]}]
    r = ground.locate(PHOTO, "rechts neben dem Fenster", GEO)
    assert r["status"] == "ok" and r["point"] == [500.0, 500.0] and r["wall"] == "w0" and r["surface"] == "wall"
    assert r["confidence"] == ground.CONF["native"] and "Wand hinten" in r["why"] and r["method"] == "native"
    call = vlm["calls"][0]
    assert call["schema"] is ground.PLACE_SCHEMA and "rechts neben dem Fenster" in call["content"][-1]["text"]
    assert call["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_locate_repairs_and_retries_bad_output(vlm):
    vlm["answers"] = ["kaputt", '```json\n{"reference_de": "Mitte", "found": True, "surface": "floor", "point_2d": [500, 1000],}\n```']
    r = ground.locate(PHOTO, "Mitte des Raums", GEO)
    assert r["status"] == "ok" and r["surface"] == "floor" and len(vlm["calls"]) == 2


def test_locate_ceiling_point_is_moved_and_penalised(vlm):
    vlm["answers"] = [{"reference_de": "oben", "found": True, "surface": "wall", "point_2d": [500, 20]}]
    r = ground.locate(PHOTO, "über dem Sofa", GEO)
    assert r["status"] == "ok" and r["point"][1] > 20 and r["surface"] == "wall"
    assert r["confidence"] < ground.CONF["native"] and "Decke" in r["why"]


def test_locate_not_found_unavailable_error(vlm, monkeypatch):
    vlm["answers"] = [{"reference_de": "Kamin", "found": False, "surface": "unclear", "point_2d": [0, 0]}]
    r = ground.locate(PHOTO, "neben dem Kamin", GEO)
    assert r["status"] == "not_found" and r["point"] is None and "Kamin" in r["why"]
    vlm["answers"] = [httpx.ConnectError("refused")]
    r = ground.locate(PHOTO, "neben dem Kamin", GEO)
    assert r["status"] == "unavailable" and vlm.get("down")
    vlm["answers"] = ["no json", "still none"]
    assert ground.locate(PHOTO, "neben dem Kamin")["status"] == "error"
    assert ground.locate(PHOTO, "   ")["status"] == "error"
    with pytest.raises(ValueError):
        ground.locate(PHOTO, "x", method="magic")
    monkeypatch.setattr(judge, "status", lambda force=False: {"available": False})
    assert ground.locate(PHOTO, "neben dem Kamin")["status"] == "unavailable"
    assert ground.find_object(PHOTO, "Kamin")["status"] == "unavailable"


def test_locate_without_geometry_and_with_removes(vlm):
    vlm["answers"] = [{"reference_de": "alte Kommode", "found": True, "surface": "floor", "point_2d": [420, 480]}]
    doc = {"removes": [[400, 300, 600, 500]]}
    r = ground.locate(PHOTO, "wo die alte Kommode steht", None, doc)
    assert r["status"] == "ok" and r["point"] == [400.0, 500.0] and r["wall"] is None and r["surface"] is None


def test_locate_grid_and_grid2(vlm):
    vlm["answers"] = [{"reference_de": "Rückwand", "found": True, "cell": "F4"}]
    r = ground.locate(PHOTO, "an der Rückwand", GEO, method="grid")
    cw, ch = 1000 / ground.GRID_COLS, 1000 / ground.GRID_ROWS
    assert r["status"] == "ok" and r["point"] == [round(5.5 * cw, 1), round(3.5 * ch, 1)]
    assert vlm["calls"][0]["schema"] is ground.GRID_SCHEMA and "A-L" in vlm["calls"][0]["content"][-1]["text"]
    vlm["calls"].clear()
    # grid2: the second answer picks a cell of a 6 x 6 grid over the 3 x 3 cells around the first one
    vlm["answers"] = [{"reference_de": "Rückwand", "found": True, "cell": "F4"},
                      {"reference_de": "Rückwand", "found": True, "cell": "A1"}]
    r = ground.locate(PHOTO, "an der Rückwand", GEO, method="grid2")
    x0, y0 = 5.5 * cw - 1.5 * cw, 3.5 * ch - 1.5 * ch
    assert r["point"] == pytest.approx([x0 + 0.5 * 3 * cw / 6, y0 + 0.5 * 3 * ch / 6], abs=0.15)
    assert len(vlm["calls"]) == 2 and "A-F" in vlm["calls"][1]["content"][-1]["text"]
    vlm["answers"] = [{"reference_de": "?", "found": True, "cell": "Q99"}] * 2
    assert ground.locate(PHOTO, "an der Rückwand", GEO, method="grid")["status"] == "error"


# ---------------- find_object

def test_find_object_uses_sam_when_it_matches(vlm, monkeypatch):
    seen = {}
    sam_parts = [{"poly": [[310, 420], [690, 420], [690, 640], [310, 640]], "box": [420, 310, 640, 690]}]

    def fake_refine(img, box, point, prompt="box"):
        seen.update(box=box, point=point, prompt=prompt)
        return sam_parts, 0.9

    monkeypatch.setattr(ground, "refine", fake_refine)
    vlm["answers"] = [{"label_de": "alte Kommode", "found": True, "bbox_2d": [300, 400, 700, 650], "point_2d": [500, 520]}]
    r = ground.find_object(PHOTO, "die alte Kommode")
    assert r["status"] == "ok" and r["box"] == [400, 300, 650, 700] and r["point"] == [500.0, 520.0]
    assert r["parts"] == sam_parts and r["refined"] and r["iou"] == 0.9 and r["label"] == "alte Kommode"
    assert seen == {"box": [400, 300, 650, 700], "point": [500.0, 520.0], "prompt": "box"}
    assert set(r["timing"]) == {"vlm", "sam"}


def test_find_object_falls_back_to_the_frame(vlm, monkeypatch):
    monkeypatch.setattr(ground, "refine", lambda img, box, point, prompt="box": ([{"poly": [], "box": [0, 0, 5, 5]}], 0.1))
    # the point lies outside the frame: replaced by the frame's centre
    vlm["answers"] = [{"label_de": "Tisch", "found": True, "bbox_2d": [300, 400, 700, 650], "point_2d": [10, 10]}]
    r = ground.find_object(PHOTO, "der Tisch")
    assert r["point"] == [500.0, 525.0] and not r["refined"] and r["iou"] == 0.1
    assert r["parts"] == [{"poly": [[300, 400], [700, 400], [700, 650], [300, 650]], "box": [400, 300, 650, 700]}]
    monkeypatch.setattr(ground, "refine", lambda *a, **k: None)              # SAM not available
    vlm["answers"] = [{"label_de": "Tisch", "found": True, "bbox_2d": [300, 400, 700, 650], "point_2d": [500, 500]}]
    r = ground.find_object(PHOTO, "der Tisch")
    assert r["status"] == "ok" and not r["refined"] and r["iou"] is None and len(r["parts"]) == 1
    vlm["answers"] = [{"label_de": "", "found": False, "bbox_2d": [0, 0, 0, 0], "point_2d": [0, 0]}]
    r = ground.find_object(PHOTO, "der Kamin")
    assert r["status"] == "not_found" and r["parts"] == [] and r["point"] is None
    vlm["answers"] = [{"label_de": "Tisch", "found": True, "bbox_2d": [300, 400, 700, 650], "point_2d": [500, 500]}]
    r = ground.find_object(PHOTO, "der Tisch", refine_with_sam=False)
    assert not r["refined"] and r["iou"] is None


def test_refine_picks_the_mask_matching_the_frame(monkeypatch):
    import segment
    h, w = 100, 200
    small = np.zeros((h, w), bool)
    small[40:60, 90:110] = True                         # a handle on the cabinet
    whole = np.zeros((h, w), bool)
    whole[30:80, 50:150] = True                         # the cabinet: frame [300, 250, 800, 750]
    monkeypatch.setattr(segment, "loaded", lambda: True)
    monkeypatch.setattr(ground, "_sam_candidates", lambda img, box, point, prompt: [small, whole])
    parts, score = ground.refine(Image.new("RGB", (w, h)), [300, 250, 800, 750], [500, 500])
    assert score == pytest.approx(1.0) and parts[0]["box"][1] < 250 and parts[0]["box"][3] >= 750
    monkeypatch.setattr(ground, "_sam_candidates", lambda *a: (_ for _ in ()).throw(RuntimeError("cuda")))
    assert ground.refine(Image.new("RGB", (w, h)), [300, 250, 800, 750], [500, 500]) is None
    monkeypatch.setattr(segment, "loaded", lambda: False)
    monkeypatch.setattr(judge, "mem_available_gb", lambda: 5.0)
    assert ground.refine(Image.new("RGB", (w, h)), [300, 250, 800, 750], [500, 500]) is None   # memory guard


# ---------------- router

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "STORE", tmp_path / "projects")
    monkeypatch.setattr(ground, "OUT", tmp_path / "outputs")
    (tmp_path / "projects/workshop").mkdir(parents=True)
    (tmp_path / "outputs/uploads").mkdir(parents=True)
    PHOTO.save(tmp_path / "outputs/uploads/20261018-aaaaaaaaaa.png")
    base = {"id": "abcdef123456", "version": 2, "title": "TEST Grounding", "room": "uploads/20261018-aaaaaaaaaa.png",
            "room_size": [1344, 1008], "geometry": GEO, "removes": [], "pieces": [], "variants": []}
    (tmp_path / "projects/workshop/abcdef123456.json").write_text(json.dumps(base))
    (tmp_path / "projects/workshop/bbbbbb123456.json").write_text(json.dumps({**base, "id": "bbbbbb123456", "room": None}))
    calls = []
    monkeypatch.setattr(ground, "locate", lambda photo, phrase, geo=None, doc=None: calls.append(
        ("place", photo.size, phrase, bool(geo), doc["id"])) or {"status": "ok", "point": [1, 2]})
    monkeypatch.setattr(ground, "find_object", lambda photo, phrase: calls.append(("object", photo.size, phrase)) or
                        {"status": "ok", "point": [3, 4], "parts": []})
    app = FastAPI()
    app.include_router(ground.router)
    return TestClient(app), calls


def test_router_locate_place_and_object(api):
    c, calls = api
    r = c.post("/api/workshop/abcdef123456/locate", json={"phrase": "links neben dem Fenster"})
    assert r.status_code == 200 and r.json()["point"] == [1, 2]
    assert calls[-1] == ("place", (1344, 1008), "links neben dem Fenster", True, "abcdef123456")
    r = c.post("/api/workshop/abcdef123456/locate", json={"phrase": "die alte Kommode", "kind": "object"})
    assert r.status_code == 200 and r.json()["point"] == [3, 4] and calls[-1] == ("object", (1344, 1008), "die alte Kommode")


def test_router_rejects_bad_requests(api):
    c, calls = api
    assert c.post("/api/workshop/abcdef123456/locate", json={"phrase": " "}).status_code == 400
    assert c.post("/api/workshop/abcdef123456/locate", json={"phrase": 5}).status_code == 400
    r = c.post("/api/workshop/abcdef123456/locate", json={"phrase": "x", "kind": "ceiling"})
    assert r.status_code == 400 and "place" in r.json()["detail"]
    r = c.post("/api/workshop/bbbbbb123456/locate", json={"phrase": "x"})
    assert r.status_code == 400 and "Raumfoto" in r.json()["detail"]
    assert c.post("/api/workshop/ffffff999999/locate", json={"phrase": "x"}).status_code == 404
    assert calls == []


def test_evalset_is_well_formed():
    """evalset/ground.json: ≥ 30 cases on FLUX rooms that exist, with sane grid boxes."""
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "evalset/ground.json").read_text())
    assert len(data["cases"]) >= 30
    assert {c["kind"] for c in data["cases"]} == {"place", "object"}
    for c in data["cases"]:
        t, l, b, r = c["expect"]
        assert 0 <= t < b <= 1000 and 0 <= l < r <= 1000, c["id"]
        room = data["rooms"][c["room"]]
        assert (root / room["image"]).is_file() and room["prompt"]
    assert len({c["id"] for c in data["cases"]}) == len(data["cases"])


def test_locate_native_short_asks_for_less(vlm):
    vlm["answers"] = [{"found": True, "surface": "wall", "point_2d": [500, 500]}]
    r = ground.locate(PHOTO, "an der Rückwand", GEO, method="native_short")
    call = vlm["calls"][0]
    assert call["schema"] is ground.SHORT_SCHEMA and '"reference_de"' not in call["system"]
    assert r["status"] == "ok" and r["wall"] == "w0" and r["why"].startswith("an der Rückwand")
