import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import projects
import workshop as ws

ROOT = Path(__file__).resolve().parents[1]

# §2.1 fixture (inline copy: must not depend on the GEOMETRY implementer's file)
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

PIECE = {"id": "p1aaaa", "type": "kueche", "text": "Eine schlichte Einbauküche", "label": "Küche",
         "material_label": "Eiche natur geölt", "handles_label": "grifflos (Push)", "w": 180, "h": 250, "d": 60,
         "material": "eiche", "material_custom": "", "handles": "push", "sketch": None, "render": None,
         "use_render": False, "sample": None, "where": "",
         "placement": {"mode": "2d", "pose": None, "box": [150, 245, 780, 545]}}

# v2 version of the legacy project (inline copy: must not depend on projects.migrate_workshop)
LEGACY = {"id": "0db93df20e67", "version": 2, "title": "Einbauschrank Wohnzimmer", "customer": "Familie Beispiel",
          "room": "uploads/20261016-5d0db410c1.png", "room_size": [1344, 1008], "sample": None, "setting": "",
          "room_changes": "", "tidy": True, "protect": True, "count": 2, "res": 1536, "removes": [[800, 600, 920, 800]],
          "guide": False, "judge": {"enabled": True, "auto_retry": True}, "geometry": None, "calibration": None,
          "pieces": [PIECE], "active_piece": "p1aaaa",
          "variants": [{"id": "oty2dff1", "kind": "room", "image": "20261016-201402-1738609104.png", "size": [1776, 1328],
                        "fav": False, "piece_ids": ["p1aaaa"], "piece": "Küche", "material": "Eiche natur geölt",
                        "handles": "grifflos (Push)", "note": "",
                        "pieces": [{"id": "p1aaaa", "label": "Küche", "material_label": "Eiche natur geölt",
                                    "handles_label": "grifflos (Push)", "w": 180, "h": 250, "d": 60}],
                        "boxes": {"p1aaaa": [150, 245, 780, 545]}, "from": None,
                        "keep": [[150, 245, 795, 545], [800, 600, 920, 800]], "upscaled": False, "judge": None}],
          "selected": "oty2dff1", "deleted_variants": [], "pending_jobs": []}


def piece(pid, **kw):
    return {**copy.deepcopy(PIECE), "id": pid, "text": "", **kw}


def png(path: Path, size, mode="RGB", color=(120, 90, 60)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path)


@pytest.fixture
def out(tmp_path, monkeypatch):
    """A throwaway outputs/ with a room photo and a few sketches."""
    monkeypatch.setattr(ws, "OUT", tmp_path)
    png(tmp_path / "uploads/room.png", (400, 300))
    for i in range(1, 5):
        png(tmp_path / f"uploads/sk{i}.png", (300, 400), color=(250, 250, 250))
    png(tmp_path / "uploads/sample.png", (200, 200))
    return tmp_path


def room_doc(pieces, **kw):
    return {**copy.deepcopy(LEGACY), "room": "uploads/room.png", "room_size": [400, 300], "removes": [], "variants": [],
            "pieces": pieces, "active_piece": pieces[0]["id"], **kw}


# ---------------- catalog & helpers ----------------

def test_catalog_and_helpers():
    assert len(ws.CATALOG["pieces"]) == 14 and len(ws.CATALOG["materials"]) == 12 and len(ws.CATALOG["handles"]) == 6
    assert ws.CATALOG["pieces"]["schrank"]["dims"] == [200, 240, 60] and ws.CATALOG["materials"]["custom"] == {"de": "Eigene Angabe…", "en": ""}
    assert ws.dims_for(1344 / 1008, 1536) == (1776, 1328)
    assert ws.piece_dims({"type": "sideboard", "w": 150, "h": None, "d": ""}) == (150, 75, 45)
    assert ws.piece_dims({"type": None}) == (100, 100, 60)
    assert ws.labels({"type": "schrank", "material": "custom", "material_custom": "Räuchereiche", "handles": "knopf",
                      "label": "UI-Label"}) == {"label": "Einbauschrank", "material_label": "Räuchereiche", "handles_label": "Holzknöpfe"}
    room = {"width": 1776, "height": 1328, "count": 1, "steps": 16, "upsample": True, "boxes": [{"id": "moebel_1"}]}
    # 1536 + room ref → 3.3 s/step (research); layout caption 61 s (bench)
    assert ws.estimate_seconds(room, [(1344, 1008)]) == round(61 + 2 + 16 * (0.27e-3 * (1776 * 1328 + 1024 * 1024) / 256 - 0.3) + 6)
    assert ws.estimate_seconds({**room, "upsample": False}, [(1344, 1008)]) == ws.estimate_seconds(room, [(1344, 1008)]) - 61
    step = 0.27e-3 * (1776 * 1328 + 1024 * 1024) / 256 - 0.3
    assert abs(step - 3.3) < 0.05
    assert ws.estimate_seconds({**room, "boxes": None, "count": 2, "steps": 8}, [(1344, 1008)]) == round(13 + 2 * (8 + 8 * step))
    assert ws.estimate_seconds({**room, "boxes": None, "width": 1024, "height": 1024}, []) == \
        round(20 + 8 + 16 * (0.27e-3 * 1024 * 1024 / 256 - 0.3))


# ---------------- room jobs ----------------

def test_legacy_room_job():
    plan = ws.build_room_job(copy.deepcopy(LEGACY))
    p = plan["params"]
    assert p["task"] == "layout" and plan["refs"] == ["uploads/20261016-5d0db410c1.png"] and plan["roles"] == ["room"]
    assert [r["id"] for r in p["boxes"]] == ["moebel_1", "weg_1"]
    assert p["boxes"][0]["tgt_bbox"] == [150, 245, 780, 545] and p["boxes"][1]["src_bbox"] == [800, 600, 920, 800]
    assert p["keep"] == [[150, 245, 795, 545], {"box": [800, 600, 920, 800], "remove": True}]
    assert ws.SHELL in p["prompt"] and ws.TIDY in p["prompt"]
    assert "Size exactly 180 cm wide, 250 cm high, 60 cm deep" in p["prompt"]
    assert p["prompt"].startswith("In <ref_image_0>, add a custom-made fitted kitchen with base cabinets")
    assert "in <moebel_1>, precisely fitted to the walls" in p["prompt"] and "Remove <weg_1> and show" in p["prompt"]
    assert (p["width"], p["height"], p["count"], p["seed"], p["source"]) == (1776, 1328, 2, -1, "workshop:0db93df20e67")
    v = p["meta"]["variant"]
    assert v["pieces"][0] == {"id": "p1aaaa", "label": "Küche", "material_label": "Eiche natur geölt",
                              "handles_label": "grifflos (Push)", "w": 180, "h": 250, "d": 60}
    assert v["kind"] == "room" and v["boxes"] == {"p1aaaa": [150, 245, 780, 545]} and v["keep"] == p["keep"]
    assert p["judge"]["room_ref"] == 0 and p["judge"]["sketch_ref"] is None and p["retry"] == 1
    req = p["judge"]["request"]
    assert req["kind"] == "room" and req["pieces"][0]["w"] == 180 and req["removes"] == [[800, 600, 920, 800]]
    assert plan["warnings"] == []


def test_real_legacy_project_migrated():
    src = ROOT / "tests/fixtures/legacy_v1_0db93df20e67.json"  # frozen, anonymized copy of the real v1 doc
    if not src.exists() or not hasattr(projects, "migrate_workshop"):
        pytest.skip("legacy fixture or projects.migrate_workshop not available")
    doc = projects.migrate_workshop(json.loads(src.read_text()))  # read only, never written
    p = ws.build_room_job(doc)["params"]
    assert p["task"] == "layout" and p["keep"] == [[150, 245, 795, 545], {"box": [800, 600, 920, 800], "remove": True}]
    assert "Size exactly 180 cm wide, 250 cm high, 60 cm deep" in p["prompt"]


def test_two_pieces_sketch_sample_and_guide(out):
    doc = room_doc([piece("p1aaaa", type="schrank", placement={"mode": "2d", "pose": None, "box": [100, 50, 900, 400]}),
                    piece("p2bbbb", type="sideboard", sketch="uploads/sk2.png", w=180, h=75, d=45,
                          placement={"mode": "2d", "pose": None, "box": [600, 500, 900, 900]})],
                   sample="uploads/sample.png")
    plan = ws.build_room_job(doc)
    pr = plan["params"]["prompt"]
    assert plan["refs"] == ["uploads/room.png", "uploads/sk2.png", "uploads/sample.png"]
    assert plan["roles"] == ["room", "sketch:p2bbbb", "sample"]
    assert "in <moebel_1>, " in pr and "in <moebel_2>, each precisely fitted" in pr
    assert "The design of <moebel_2> follows the carpenter's sketch in <ref_image_1>." in pr
    assert "The wood color and grain match the material sample in <ref_image_2>." in pr
    assert plan["params"]["judge"]["sketch_ref"] == 1
    assert plan["params"]["keep"] == [[100, 50, 915, 400], [600, 500, 915, 900]]
    assert plan["params"]["meta"]["variant"]["piece"] == "Einbauschrank + Sideboard"

    doc["guide"] = True
    plan = ws.build_room_job(doc)
    pr = plan["params"]["prompt"]
    assert plan["roles"] == ["room", "guide", "sketch:p2bbbb", "sample"] and plan["refs"][1].startswith("guides/0db93df20e67-")
    assert "<ref_image_1> shows the same photo with the planned furniture outlines" in pr
    assert "sketch in <ref_image_2>." in pr and "material sample in <ref_image_3>." in pr
    g = Image.open(out / plan["refs"][1])
    assert g.size == (400, 300) and g.getpixel((20, 150)) == (89, 167, 255)  # left edge of piece 1 (u = 50/1000 * 400) in W3D.COLORS[0]
    assert ws.build_room_job(doc)["refs"][1] == plan["refs"][1]  # same layout → same cached file


def test_four_pieces_cap(out):
    box = lambda i: {"mode": "2d", "pose": None, "box": [100, 200 * i, 900, 200 * i + 180]}
    pieces = [piece(f"p{i}aaaa", type="regal", sketch=f"uploads/sk{i + 1}.png", placement=box(i)) for i in range(4)]
    doc = room_doc(pieces, sample="uploads/sample.png", guide=True, active_piece="p2aaaa")
    plan = ws.build_room_job(doc)
    pr = plan["params"]["prompt"]
    assert len(plan["refs"]) == 5
    assert plan["roles"] == ["room", "guide", "sketch:p2aaaa", "sketch:p0aaaa", "sketch:p1aaaa"]
    assert "The design of <moebel_3> follows the carpenter's sketch in <ref_image_2>." in pr
    assert "The design of <moebel_1> follows the carpenter's sketch in <ref_image_3>." in pr
    assert "The design of <moebel_2> follows the carpenter's sketch in <ref_image_4>." in pr
    assert "<moebel_4> follows" not in pr and "material sample" not in pr and "<ref_image_5>" not in pr
    assert "Zu viele Vorlagen – 1 Skizze(n) nicht mitgegeben (höchstens 5 Bilder)." in plan["warnings"]
    assert any("Holzmuster nicht mitgegeben" in w for w in plan["warnings"])
    assert plan["params"]["judge"]["sketch_ref"] == 2
    assert not any(w.startswith("Dauer ca.") for w in plan["warnings"])  # the UI shows times itself


def test_render_as_design_ref_and_shared_sample(out):
    png(out / "render1.png", (300, 300))
    doc = room_doc([piece("p1aaaa", sketch="uploads/sk1.png", render="render1.png", use_render=True,
                          sample="uploads/sample.png")], sample="uploads/sample.png")
    plan = ws.build_room_job(doc)
    assert plan["refs"] == ["uploads/room.png", "render1.png", "uploads/sample.png"]
    pr = plan["params"]["prompt"]
    assert "The design of <moebel_1> follows the product rendering in <ref_image_1>." in pr
    assert pr.count("material sample") == 1 and "Its wood color and grain match the material sample in <ref_image_2>." in pr


def test_unplaced_piece_and_validation(out):
    doc = room_doc([piece("p1aaaa", type="schrank"), piece("p2bbbb", type="tisch", where="vor dem Fenster",
                                                          placement={"mode": "none", "pose": None, "box": None})])
    plan = ws.build_room_job(doc)
    assert plan["params"]["keep"] is None and "Raum schützen wirkt erst, wenn alle Möbel platziert sind." in plan["warnings"]
    assert "Also build a custom-made solid wood dining table" in plan["params"]["prompt"]
    assert "here: vor dem Fenster." in plan["params"]["prompt"]
    assert [r["id"] for r in plan["params"]["boxes"]] == ["moebel_1"]

    bad = room_doc([piece("p1aaaa"), piece("p2bbbb", type=None, text="  ")])
    with pytest.raises(HTTPException) as e:
        ws.build_room_job(bad)
    assert e.value.status_code == 400 and e.value.detail == "Möbel 2: bitte auswählen oder beschreiben."
    with pytest.raises(HTTPException) as e:
        ws.build_room_job(room_doc([piece("p1aaaa", type=None)]))
    assert e.value.detail == "Bitte Möbel auswählen oder beschreiben."
    with pytest.raises(HTTPException) as e:
        ws.build_room_job(room_doc([piece("p1aaaa", material="custom", material_custom="")]))
    assert e.value.detail == "Bitte die eigene Oberfläche beschreiben."
    with pytest.raises(HTTPException) as e:
        ws.build_room_job(room_doc([piece(f"p{i}aaaa") for i in range(5)]))
    assert e.value.detail == "Höchstens 4 Möbel pro Projekt."


def test_room_edit_task_without_boxes(out):
    doc = room_doc([piece("p1aaaa", type="schrank", sketch="uploads/sk1.png", where="in die Nische",
                          placement={"mode": "none", "pose": None, "box": None})], room_changes="Wand weiß streichen")
    p = ws.build_room_job(doc)["params"]
    assert p["task"] == "edit" and "boxes" not in p and p["keep"] is None
    assert p["prompt"].startswith("In the first image, build a custom-made fitted floor-to-ceiling built-in wardrobe")
    assert ". Location: in die Nische." in p["prompt"]
    assert "Its design follows the carpenter's sketch in the second image." in p["prompt"]
    assert "Also change the room itself as requested: Wand weiß streichen. Apart from that, keep" in p["prompt"]


def test_free_jobs(out):
    doc = room_doc([piece("p1aaaa", type="sideboard")], room=None, setting="ein Loft")
    p = ws.build_room_job(doc)["params"]
    assert p["task"] == "create" and (p["width"], p["height"]) == ws.dims_for(4 / 3, 1536)
    assert p["prompt"].startswith("Professional interior photograph of a custom-made low sideboard")
    assert p["prompt"].endswith("in ein Loft. Realistic proportions and craftsmanship details, eye-level camera, soft natural light.")
    assert p["meta"]["variant"]["kind"] == "free" and p["judge"]["room_ref"] is None

    doc["pieces"][0]["sketch"] = "uploads/sk1.png"
    plan = ws.build_room_job(doc)
    assert plan["params"]["task"] == "edit" and plan["refs"] == ["uploads/sk1.png"]
    assert plan["params"]["prompt"].startswith("Photorealistic photograph of the finished furniture, built exactly as drawn "
                                               "in the carpenter's sketch in the first image: a custom-made low sideboard")
    assert plan["params"]["judge"]["sketch_ref"] == 0


def test_render_job(out):
    png(out / "uploads/tall.png", (300, 1000), mode="RGBA", color=(0, 0, 0, 0))
    doc = room_doc([piece("p1aaaa", type="schrank", sketch="uploads/tall.png")])
    plan = ws.build_render_job(doc, "p1aaaa")
    p = plan["params"]
    assert plan["refs"] == ["uploads/tall.png"] and p["task"] == "edit" and p["count"] == 2
    assert (p["width"], p["height"]) == ws.dims_for(0.75, 1536)
    assert p["judge"]["room_ref"] is None and p["judge"]["sketch_ref"] == 0 and p["judge"]["request"]["kind"] == "render"
    assert p["prompt"].startswith("Photorealistic studio product photograph of the finished piece of furniture drawn in the "
                                  "carpenter's sketch in the first image: a custom-made fitted floor-to-ceiling")
    assert "seamless light grey studio background" in p["prompt"] and p["keep"] is None
    v = p["meta"]["variant"]
    assert v["kind"] == "render" and v["sketch"] == "uploads/tall.png" and v["piece_ids"] == ["p1aaaa"] and v["boxes"] == {}
    doc["sample"] = "uploads/sample.png"
    assert ws.build_render_job(doc, "p1aaaa")["refs"] == ["uploads/tall.png", "uploads/sample.png"]
    with pytest.raises(HTTPException) as e:
        ws.build_render_job(room_doc([piece("p1aaaa")]), "p1aaaa")
    assert e.value.detail == "Bitte zuerst eine Skizze hochladen."
    with pytest.raises(HTTPException) as e:
        ws.build_render_job(doc, "nope00")
    assert e.value.status_code == 404


# ---------------- edit ops ----------------

def two_piece_variant_doc():
    doc = copy.deepcopy(LEGACY)
    doc["pieces"].append(piece("p2bbbb", type="sideboard", material="weiss"))
    v = doc["variants"][0]
    v["piece_ids"] = ["p1aaaa", "p2bbbb"]
    v["pieces"].append({"id": "p2bbbb", "label": "Sideboard", "material_label": "Lack weiß matt", "handles_label": "grifflos (Push)",
                        "w": 180, "h": 75, "d": 45})
    v["boxes"]["p2bbbb"] = [600, 600, 990, 900]
    return doc


def test_edit_ops():
    doc = copy.deepcopy(LEGACY)
    p = ws.build_edit_job(doc, "oty2dff1", "material", material="nuss")["params"]
    assert p["prompt"] == ("In the first image, change only the material of the fitted kitchen to American walnut, rich dark "
                           "brown with lively grain. Keep its shape and design, handles, the room, lighting and camera exactly unchanged.")
    assert p["keep"] == LEGACY["variants"][0]["keep"] and (p["width"], p["height"], p["count"]) == (1776, 1328, 1)
    assert p["meta"]["variant"]["kind"] == "material" and p["meta"]["variant"]["material"] == "Nussbaum"
    assert p["meta"]["variant"]["from"] == "oty2dff1" and p["judge"]["request"]["kind"] == "edit" and p["judge"]["room_ref"] == 0
    assert p["retry"] == 0

    doc = two_piece_variant_doc()
    plan = ws.build_edit_job(doc, "oty2dff1", "material", material="nuss", piece_id="p2bbbb")
    p = plan["params"]
    assert plan["refs"] == ["20261016-201402-1738609104.png"] and plan["roles"] == ["source"]
    assert "change only the material of the low sideboard on a recessed plinth to American walnut" in p["prompt"]
    assert p["keep"] == [[600, 600, 1000, 900], {"box": [800, 600, 920, 800], "remove": True}]
    snap = {r["id"]: r["material_label"] for r in p["meta"]["variant"]["pieces"]}
    assert snap == {"p1aaaa": "Eiche natur geölt", "p2bbbb": "Nussbaum"}
    assert doc["variants"][0]["pieces"][1]["material_label"] == "Lack weiß matt"  # the stored snapshot is untouched
    p = ws.build_edit_job(doc, "oty2dff1", "material", material="nuss")["params"]
    assert "material of the fitted kitchen and low sideboard on a recessed plinth to" in p["prompt"]

    p = ws.build_edit_job(doc, "oty2dff1", "edit", text="Türen in Salbeigrün lackieren")["params"]
    assert p["prompt"] == ("In the first image, Türen in Salbeigrün lackieren. Keep everything else exactly unchanged, especially "
                           "the walls, niches, windows, doors and floor.")
    assert p["keep"] == LEGACY["variants"][0]["keep"] and p["meta"]["variant"]["note"] == "Türen in Salbeigrün lackieren"
    assert p["meta"]["variant"]["pieces"] == doc["variants"][0]["pieces"]
    assert ws.build_edit_job(doc, "oty2dff1", "edit", text="x", keep_only=False)["params"]["keep"] is None

    p = ws.build_edit_job(doc, "oty2dff1", "upscale")["params"]
    assert p["prompt"] == ws.UPSCALE_PROMPT and p["upsample"] is False and p["seed"] == 7 and "judge" not in p
    assert (p["width"], p["height"]) == ws.dims_for(1776 / 1328, 2304) and p["meta"]["variant"]["upscaled"] is True
    doc["variants"][0]["size"] = [2400, 1800]
    with pytest.raises(HTTPException) as e:
        ws.build_edit_job(doc, "oty2dff1", "upscale")
    assert e.value.detail == "Das Bild ist schon groß genug."
    with pytest.raises(HTTPException) as e:
        ws.build_edit_job(doc, "missing1", "edit", text="x")
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        ws.build_edit_job(doc, "oty2dff1", "material", material="nuss", piece_id="zzzz99")
    assert e.value.status_code == 404


# ---------------- load_refs ----------------

def test_load_refs(out):
    png(out / "uploads/big.png", (2000, 1500))
    png(out / "uploads/sk12.png", (4000, 3000))
    png(out / "uploads/alpha.png", (100, 100), mode="RGBA", color=(0, 0, 0, 0))
    imgs = ws.load_refs(["uploads/big.png", "uploads/sk12.png", "uploads/alpha.png"], ["room", "sketch:a", "sketch:b"])
    assert imgs[0].size == (2000, 1500)
    assert imgs[1].width * imgs[1].height <= ws.SMALL_REF_PIXELS and imgs[1].mode == "RGB"
    assert imgs[2].getpixel((50, 50)) == (255, 255, 255)
    with pytest.raises(HTTPException):
        ws.load_refs(["../../etc/passwd"], ["sketch:a"])
    with pytest.raises(HTTPException):
        ws.load_refs(["uploads/missing.png"], ["room"])


# ---------------- 3d (needs geometry.py) ----------------

def test_3d_pieces(out):
    geometry = pytest.importorskip("geometry")
    if ws.geometry is None:
        pytest.skip("workshop imported without geometry")
    pa = piece("p1aaaa", type="schrank", w=180, h=220, d=60,
               placement={"mode": "3d", "pose": {"x": -20, "z": 370, "yaw": 0, "wall": "w0"}, "box": None})
    doc = room_doc([pa], room_size=[1344, 1008], geometry=copy.deepcopy(GEO))
    boxes, warns = ws.resolve_boxes(doc)
    assert all(abs(a - b) <= 1 for a, b in zip(boxes["p1aaaa"], [106, 246, 744, 662])) and warns == []
    plan = ws.build_room_job(doc)
    assert "reaches 85% of the 260 cm ceiling height" in plan["params"]["prompt"]
    assert "of the width of the back wall" in plan["params"]["prompt"]
    assert plan["params"]["boxes"][0]["tgt_bbox"] == boxes["p1aaaa"]
    assert plan["params"]["judge"]["request"]["pieces"][0]["relation"].startswith("it reaches 85% of the 260 cm ceiling height")

    behind = piece("p2bbbb", type="sideboard", placement={"mode": "3d", "pose": {"x": 0, "z": -100, "yaw": 0, "wall": None},
                                                          "box": [1, 1, 500, 500]})
    doc = room_doc([pa, behind], room_size=[1344, 1008], geometry=copy.deepcopy(GEO))
    boxes, warns = ws.resolve_boxes(doc)
    assert boxes["p2bbbb"] is None and "Möbel 2: Das Möbel ragt aus dem Bild." in warns
    plan = ws.build_room_job(doc)
    assert [r["id"] for r in plan["params"]["boxes"]] == ["moebel_1"] and plan["params"]["keep"] is None
    assert "Möbel 2: Das Möbel ragt aus dem Bild." in plan["warnings"]

    nod = piece("p3cccc", type="sideboard", w=None, h=None, d=None,
                placement={"mode": "3d", "pose": {"x": 50, "z": 300, "yaw": 0, "wall": None}, "box": None})
    doc = room_doc([nod], room_size=[1344, 1008], geometry=copy.deepcopy(GEO))
    want = geometry.project_box(GEO, (180, 75, 45), nod["placement"]["pose"])["box"]
    assert ws.resolve_boxes(doc)[0]["p3cccc"] == want
    plan = ws.build_room_job(doc)
    assert "Size exactly 180 cm wide, 75 cm high, 45 cm deep" in plan["params"]["prompt"]
    assert plan["params"]["meta"]["variant"]["pieces"][0]["w"] == 180

    too_high = piece("p4dddd", type="schrank", w=180, h=300, d=60,
                     placement={"mode": "3d", "pose": {"x": -20, "z": 370, "yaw": 0, "wall": "w0"}, "box": None})
    plan = ws.build_room_job(room_doc([too_high], room_size=[1344, 1008], geometry=copy.deepcopy(GEO)))
    assert "Möbel 1: Höher als die Decke (260 cm)!" in plan["warnings"]

    doc = room_doc([pa], room_size=[400, 300], geometry=copy.deepcopy(GEO), guide=True)
    plan = ws.build_room_job(doc)
    assert plan["roles"][:2] == ["room", "guide"] and (out / plan["refs"][1]).exists()


def test_3d_fallback_without_geometry(monkeypatch):
    monkeypatch.setattr(ws, "geometry", None)
    pa = piece("p1aaaa", placement={"mode": "3d", "pose": {"x": -20, "z": 370, "yaw": 0, "wall": "w0"}, "box": [106, 246, 744, 662]})
    doc = room_doc([pa], geometry=copy.deepcopy(GEO))
    assert ws.resolve_boxes(doc) == ({"p1aaaa": [106, 246, 744, 662]}, [])


# ---------------- record ----------------

Gone = getattr(projects, "Gone", None)
if Gone is None:
    class Gone(HTTPException):
        def __init__(self):
            super().__init__(404, "Auftrag nicht gefunden.")


class FakeProjects:
    """The projects.py v3 contract (§4.1) in memory: diff-based mutate (no save when deep-equal), log, advance."""

    def __init__(self, *docs):
        self.docs = {d["id"]: copy.deepcopy(d) for d in docs}
        self.logs, self.advances, self.saves, self.whos, self.changes = [], [], 0, [], []

    def load(self, kind, pid):
        if pid not in self.docs:
            raise Gone()
        return copy.deepcopy(self.docs[pid])

    def mutate(self, kind, pid, fn, who=None, auto=True):
        doc = self.load(kind, pid)
        before = copy.deepcopy(doc)
        fn(doc)
        if doc != before:
            doc["rev"] = int(doc.get("rev") or 0) + 1
            self.docs[pid] = doc
            self.saves += 1
            self.whos.append((who, auto))
        return copy.deepcopy(doc)

    def log(self, doc, what, text, who):
        doc.setdefault("activity", []).append({"what": what, "text": text, "by": who})
        self.logs.append((what, text, who))

    def advance(self, doc, target, who, text=None):
        self.advances.append((target, who))
        doc["status"] = target
        return True

    def changed_since(self, doc, base_rev, prefixes):
        return [c for c in self.changes if c["rev"] > base_rev and c["path"].startswith(prefixes)]


@pytest.fixture
def fake(monkeypatch):
    doc = copy.deepcopy(LEGACY)
    doc.update(pending_jobs=["a1b2c3d4e5"], rev=5, status="termin")
    fp = FakeProjects(doc)
    for name in ("load", "mutate", "log", "advance", "changed_since"):
        monkeypatch.setattr(projects, name, getattr(fp, name), raising=False)
    monkeypatch.setattr(projects, "Gone", Gone, raising=False)
    return fp


@pytest.fixture
def store(fake):
    return lambda: fake.load("workshop", "0db93df20e67")


def job(state="done", judge_state="off", results=2, source="workshop:0db93df20e67", **kw):
    meta = {"variant": {"kind": "room", "piece_ids": ["p1aaaa"], "piece": "Küche", "material": "Eiche natur geölt",
                        "handles": "grifflos (Push)", "note": "", "keep": None, "from": None, "id": "evil", "seed": 0,
                        "by": "evil", "active_piece": "p1aaaa",
                        "pieces": LEGACY["variants"][0]["pieces"], "boxes": {"p1aaaa": [150, 245, 780, 545]}}}
    res = [{"id": f"20261017-12000{i}-{100 + i}", "width": 1776, "height": 1328, "seed": 100 + i, "judge": {"status": "pending"}}
           for i in range(results)]
    return {"id": "a1b2c3d4e5", "state": state, "judge_state": judge_state, "retry_job": None, "owner": "anna",
            "owner_name": "Anna", "caption_backend": "vllm", "cancelled_by": None,
            "params": {"source": source, "meta": meta, "steps": 8}, "results": res, **kw}


def test_record(store, fake):
    j = job(judge_state="pending")
    ws.record(j)
    doc = store()
    new = [v for v in doc["variants"] if v.get("job") == "a1b2c3d4e5"]
    assert len(new) == 2 and doc["variants"][0]["image"] == "20261017-120001-101.png"  # sorted newest first
    assert all(v["id"] != "evil" and len(v["id"]) == 8 and v["seed"] in (100, 101) and v["size"] == [1776, 1328] for v in new)
    assert new[0]["kind"] == "room" and new[0]["fav"] is False and new[0]["judge"] == {"status": "pending"}
    assert all(v["by"] == "anna" and v["caption_backend"] == "vllm" and v["steps"] == 8 and v["active_piece"] == "p1aaaa" for v in new)
    assert doc["pending_jobs"] == ["a1b2c3d4e5"] and fake.logs == []  # still judging
    saves = fake.saves
    ws.record(j)
    assert len(store()["variants"]) == 3 and fake.saves == saves  # deduplicated by image; nothing changed → no save

    gone = new[1]["id"]
    projects.mutate("workshop", "0db93df20e67", lambda d: d.update(
        variants=[v for v in d["variants"] if v["id"] != gone], deleted_variants=[gone]))
    for r in j["results"]:
        r["judge"] = {"status": "ok", "overall": 8.1}
    j.update(judge_state="done", retry_job="ffee001122")
    ws.record(j, judge_only=True)
    doc = store()
    assert len(doc["variants"]) == 2 and all(v["id"] != gone for v in doc["variants"])
    assert [v["judge"] for v in doc["variants"] if v.get("job") == "a1b2c3d4e5"] == [{"status": "ok", "overall": 8.1}]
    assert doc["pending_jobs"] == ["ffee001122"]
    assert [w for w, _, _ in fake.logs] == ["generated", "retry"] and fake.logs[0][1:] == ("2 Vorschläge berechnet", "anna")
    assert fake.advances == [("entwurf", "anna")] and all(who == "anna" and auto for who, auto in fake.whos if who)


def test_record_partial_per_image(store, fake):
    """The scheduler records after every image: running → running → done gives no duplicates and one log/advance."""
    j = job(state="running", results=1)
    ws.record(j)
    ws.record(j)
    assert len([v for v in store()["variants"] if v.get("job") == "a1b2c3d4e5"]) == 1
    j["results"] += job(results=2)["results"][1:]
    ws.record(j)
    j["state"] = "done"
    ws.record(j)
    ws.record(j)
    doc = store()
    new = [v for v in doc["variants"] if v.get("job") == "a1b2c3d4e5"]
    assert len(new) == 2 and {v["steps"] for v in new} == {8} and {v["by"] for v in new} == {"anna"}
    assert fake.logs == [("generated", "2 Vorschläge berechnet", "anna")] and fake.advances == [("entwurf", "anna")]
    assert doc["pending_jobs"] == [] and doc["status"] == "entwurf"


def test_record_deleted_while_running_stays_deleted(store, fake):
    j = job(state="running", results=1)
    ws.record(j)
    v = next(v for v in store()["variants"] if v.get("job") == "a1b2c3d4e5")
    projects.mutate("workshop", "0db93df20e67", lambda d: d.update(
        variants=[x for x in d["variants"] if x["id"] != v["id"]], deleted_variants=[v["id"]], trash=[v]))
    j["results"] += job(results=2)["results"][1:]
    ws.record(j)
    assert [x["image"] for x in store()["variants"] if x.get("job") == "a1b2c3d4e5"] == ["20261017-120001-101.png"]


def test_record_cancel_error_texts(store, fake):
    ws.record(job(state="cancelled", results=1, cancelled_by="Ben"))
    assert fake.logs == [("cancelled", "Berechnung abgebrochen (Ben)", "anna")] and fake.advances == [("entwurf", "anna")]
    fake.docs["0db93df20e67"]["pending_jobs"] = ["a1b2c3d4e5"]
    fake.logs.clear()
    ws.record(job(state="error", results=0, error="MemoryError: zu wenig Speicher", id="a1b2c3d4e5"))
    assert fake.logs == [("failed", "Berechnung fehlgeschlagen: MemoryError: zu wenig Speicher", "anna")]
    for kind, log, want in (("render", None, "Skizze gerendert"), ("upscale", None, "Für Druck vergrößert"),
                            ("material", "Holz getauscht: Nussbaum", "Holz getauscht: Nussbaum"),
                            ("free", None, "1 Vorschlag berechnet")):
        fake.docs["0db93df20e67"]["pending_jobs"] = ["a1b2c3d4e5"]
        fake.logs.clear()
        j = job(results=1)
        j["params"]["meta"]["variant"]["kind"] = kind
        if log:
            j["params"]["meta"]["log"] = log
        ws.record(j)
        assert fake.logs == [("generated", want, "anna")]


def test_record_ignores_foreign_and_errors(store, fake):
    before = store()
    ws.record(job(source="studio"))
    with pytest.raises(HTTPException) as e:  # purged project: the scheduler deletes the job's files
        ws.record(job(source="workshop:aaaaaaaaaaaa"))
    assert e.value.status_code == 404
    ws.record({"params": None})
    j = job()
    j["params"]["meta"] = None
    ws.record(j)
    assert store()["variants"] == before["variants"]
    ws.record(job(state="error", results=0))
    assert store()["pending_jobs"] == []


def test_record_against_real_projects(tmp_path, monkeypatch):
    """With the real projects.py v3 (when present): diff-based mutate, Gone, status move."""
    if not hasattr(projects, "changed_since"):
        pytest.skip("projects.py v3 not available yet")
    monkeypatch.setattr(projects, "STORE", tmp_path / "projects")
    (tmp_path / "projects/workshop").mkdir(parents=True)
    doc = copy.deepcopy(LEGACY)
    doc["pending_jobs"] = ["a1b2c3d4e5"]
    (tmp_path / "projects/workshop/0db93df20e67.json").write_text(json.dumps(doc))
    j = job(state="running", results=1)
    ws.record(j)
    rev = projects.load("workshop", "0db93df20e67")["rev"]
    ws.record(j)
    assert projects.load("workshop", "0db93df20e67")["rev"] == rev  # nothing changed → no rev bump
    j["state"] = "done"
    ws.record(j)
    d = projects.load("workshop", "0db93df20e67")
    assert d["status"] == "entwurf" and d["pending_jobs"] == [] and any(a["what"] == "generated" for a in d["activity"])
    with pytest.raises(HTTPException):
        ws.record(job(source="workshop:aaaaaaaaaaaa"))


# ---------------- router ----------------

class Duplicate(Exception):  # = jobs.Duplicate (workshop recognises it by name, never imports jobs)
    def __init__(self, view):
        super().__init__("duplicate")
        self.view = view


class FakeUps:
    def __init__(self, state="up", cached=False):
        self.state, self.cached, self.items = state, cached, []

    def status(self, fresh=False):
        return {"backend": "vllm" if self.state == "up" else "template", "state": self.state, "model": None}

    def cache_has(self, item):
        self.items.append(item)
        return self.cached


@pytest.fixture
def client(store, out, monkeypatch):
    png(out / "uploads/20261016-5d0db410c1.png", (1344, 1008))
    png(out / "20261016-201402-1738609104.png", (1776, 1328))
    calls, active = [], {}

    def submit(params, images, owner="anon", owner_name="", project=None, label="", client_key=None, dedupe=None, force=False,
               on_accept=None):
        if (project, dedupe) in active and not force:
            raise Duplicate(active[(project, dedupe)])
        view = {"id": f"job{len(calls):06d}", "state": "queued", "owner": owner, "owner_name": owner_name, "position": len(active),
                "label": label, "results": [], "caption": "secret", "dedupe": dedupe}
        if on_accept:
            on_accept(view["id"])
        calls.append((params, images, {"owner": owner, "owner_name": owner_name, "project": project, "label": label,
                                       "client_key": client_key, "dedupe": dedupe, "force": force}))
        active[(project, dedupe)] = view
        return view

    ups = FakeUps()
    monkeypatch.setattr(ws, "SUBMIT", submit)
    monkeypatch.setattr(ws, "_ups", lambda: ups)
    app = FastAPI()

    @app.middleware("http")
    async def ident(request, call_next):
        request.state.owner, request.state.owner_name = "anna", "Anna"
        return await call_next(request)

    app.include_router(ws.router)
    c = TestClient(app)
    c.calls, c.active, c.ups = calls, active, ups
    return c, calls


def test_router(client, store, monkeypatch):
    c, calls = client
    r = c.get("/api/workshop/catalog").json()
    assert r["max_pieces"] == 4 and r["max_refs"] == 5 and r["pieces"]["schrank"]["de"] == "Einbauschrank"
    assert r["handles"]["push"]["en"] == "handleless push-to-open fronts"
    assert r["quality"] == {"entwurf": {"label": "Entwurf (schneller)", "steps": ws.STEPS_DRAFT},
                            "fein": {"label": "Fein (für den Kunden)", "steps": 16}}
    assert r["common_materials"] == ["eiche", "eicheweiss", "nuss", "weiss"]

    r = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True})
    assert r.status_code == 200 and r.json()["job"] is None and r.json()["plan"]["params"]["task"] == "layout" and not calls
    plan = r.json()["plan"]
    assert plan["label"] == "2 Vorschläge" and plan["wait_s"] == 0 and plan["caption"] == {"backend": "vllm", "state": "up"}
    assert plan["estimate_s"] > 0 and not any(w.startswith("Dauer") for w in plan["warnings"])

    r = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "client_key": "c-1"}).json()
    assert r["job"]["id"] == "job000000" and len(calls) == 1
    params, images, kw = calls[0]
    assert params["task"] == "layout" and len(images) == 1 and images[0].size == (1344, 1008)
    assert kw == {"owner": "anna", "owner_name": "Anna", "project": "0db93df20e67", "label": "2 Vorschläge",
                  "client_key": "c-1", "dedupe": "design", "force": False}
    assert "job000000" in store()["pending_jobs"]

    r = c.post("/api/workshop/0db93df20e67/edit", json={"op": "edit", "variant_id": "oty2dff1", "text": "Sockel schwarz"}).json()
    assert calls[-1][0]["meta"]["variant"]["from"] == "oty2dff1" and calls[-1][1][0].size == (1776, 1328)
    assert calls[-1][2]["dedupe"] == "edit:oty2dff1" and calls[-1][2]["label"] == "Änderung" and r["plan"]["label"] == "Änderung"
    assert c.post("/api/workshop/0db93df20e67/edit", json={"op": "upscale", "variant_id": "nope1234"}).status_code == 404
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "render", "piece_id": "p1aaaa"}).json()["detail"] \
        == "Bitte zuerst eine Skizze hochladen."

    monkeypatch.setattr(ws, "SUBMIT", None)
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "force": True}).status_code == 503


def test_dedupe_running_and_force(client, store):
    c, calls = client
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room"}).status_code == 200
    r = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room"})
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "running" and len(calls) == 1
    assert d["message"] == "Für diesen Auftrag läuft schon eine Berechnung (Anna, läuft gerade)."
    assert d["job"]["id"] == "job000000" and "caption" not in d["job"] and "results" not in d["job"]
    r = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "force": True})
    assert r.status_code == 200 and len(calls) == 2 and calls[-1][2]["force"] is True
    c.active[("0db93df20e67", "material:oty2dff1")] = {"id": "x", "owner_name": "Ben", "position": 1}
    d = c.post("/api/workshop/0db93df20e67/edit", json={"op": "material", "variant_id": "oty2dff1", "material": "nuss"}).json()["detail"]
    assert d["message"] == "Für diesen Auftrag läuft schon eine Berechnung (Ben, Platz 2)."
    # other dedupe keys do not collide
    assert c.post("/api/workshop/0db93df20e67/edit", json={"op": "edit", "variant_id": "oty2dff1", "text": "x"}).status_code == 200


def test_base_rev_conflict(client, fake):
    c, calls = client
    fake.changes = [{"rev": 7, "path": "pieces.p1aaaa.w", "by": "ben", "by_name": "Ben", "at": "…"},
                    {"rev": 8, "path": "title", "by": "ben", "by_name": "Ben", "at": "…"}]
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "base_rev": 8}).status_code == 200
    r = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "base_rev": 6, "force": True})
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "changed" and d["changes"] == [{"path": "pieces.p1aaaa.w", "by_name": "Ben"}]
    assert d["message"] == "Der Auftrag wurde gerade von Ben geändert. Bitte kurz prüfen und dann erneut starten."
    assert d["rev"] == 6 and d["doc"]["id"] == "0db93df20e67" and len(calls) == 1
    # a dry run (estimate) is never refused
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "base_rev": 6, "dry_run": True}).status_code == 200


def test_quality_presets_and_steps(client, fake, out):
    c, _ = client
    gen = lambda **kw: c.post("/api/workshop/0db93df20e67/generate", json={"dry_run": True, **kw}).json()["plan"]
    p = gen(kind="room", quality="entwurf")["params"]
    assert p["steps"] == ws.STEPS_DRAFT and (p["width"], p["height"]) == (1776, 1328)
    p = gen(kind="room", quality="fein")["params"]
    assert p["steps"] == 16 and (p["width"], p["height"]) == (1776, 1328)
    fake.docs["0db93df20e67"].update(quality="entwurf", res=1024)  # quality wins over the old res
    assert gen(kind="room")["params"]["steps"] == ws.STEPS_DRAFT and gen(kind="room")["params"]["width"] == 1776
    del fake.docs["0db93df20e67"]["quality"]
    assert gen(kind="room")["params"]["steps"] == ws.STEPS_DRAFT  # old doc: res 1024 → entwurf
    assert gen(kind="free", quality="entwurf")["params"]["steps"] == 16  # the A/B covers room jobs only
    fake.docs["0db93df20e67"]["pieces"][0]["sketch"] = "uploads/sk1.png"
    plan = gen(kind="render", piece_id="p1aaaa", quality="entwurf")
    assert plan["params"]["steps"] == 16 and plan["label"] == "Skizze: Küche"
    for body, label in (({"op": "material", "material": "nuss"}, "Holz: Nussbaum"), ({"op": "edit", "text": "x"}, "Änderung"),
                        ({"op": "upscale"}, "Für Druck vergrößern")):
        r = c.post("/api/workshop/0db93df20e67/edit", json={"variant_id": "oty2dff1", "dry_run": True, **body}).json()["plan"]
        assert r["params"]["steps"] == 16 and r["label"] == label
    plan = gen(kind="room", count=1)
    assert plan["params"]["count"] == 1 and plan["label"] == "1 Vorschlag"
    assert gen(kind="room", count=9)["params"]["count"] == 4


def test_estimate_wait_hooks_and_caption_plan(client, fake, monkeypatch):
    c, _ = client
    seen = {}
    monkeypatch.setattr(ws, "ESTIMATE", lambda params, sizes: seen.update(params=params, sizes=sizes) or 123.4)
    monkeypatch.setattr(ws, "WAIT", lambda owner: seen.update(owner=owner) or 95)
    plan = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True}).json()["plan"]
    assert plan["estimate_s"] == 123 and plan["wait_s"] == 95 and seen["owner"] == "anna" and seen["sizes"] == [(1344, 1008)]
    assert plan["judge_s"] == 0 and plan["estimate_text"] == "Bilder in ca. 2 Min."   # no judge hook: images only
    monkeypatch.setattr(ws, "JUDGE_EST", lambda n: 30.0)
    plan = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True}).json()["plan"]
    assert isinstance(plan["params"].get("judge"), dict)
    assert plan["judge_s"] == 30 and plan["estimate_text"] == "Bilder in ca. 2 Min., Bewertung ca. 30 Sek. danach"
    item = c.ups.items[-1]
    assert item["boxes"] == plan["params"]["boxes"] and item["images"][0].size == (1344, 1008) and item["width"] == 1776
    c.ups.cached = True
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True}).json()["plan"]["caption"] \
        == {"backend": "cache", "state": "up"}
    c.ups.state, c.ups.cached = "starting", False
    assert c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True}).json()["plan"]["caption"] \
        == {"backend": "template", "state": "starting"}
    r = c.post("/api/workshop/0db93df20e67/edit", json={"op": "upscale", "variant_id": "oty2dff1", "dry_run": True}).json()
    assert r["plan"]["caption"]["backend"] == "template"  # upsample: false
    monkeypatch.setattr(ws, "SKIP_PUP_FOR", {"material"})
    r = c.post("/api/workshop/0db93df20e67/edit", json={"op": "material", "material": "nuss", "variant_id": "oty2dff1",
                                                        "dry_run": True}).json()
    assert r["plan"]["params"]["upsample"] is False and r["plan"]["caption"]["backend"] == "template"


def test_active_piece_from_body(client, fake, out):
    c, calls = client
    d = fake.docs["0db93df20e67"]
    d["pieces"] = [piece("p1aaaa", sketch="uploads/sk1.png", placement={"mode": "2d", "pose": None, "box": [100, 50, 900, 400]}),
                   piece("p2bbbb", type="sideboard", sketch="uploads/sk2.png",
                         placement={"mode": "2d", "pose": None, "box": [600, 500, 900, 900]})]
    d["active_piece"] = "p1aaaa"
    plan = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True, "active_piece": "p2bbbb"}).json()["plan"]
    assert plan["roles"] == ["room", "sketch:p2bbbb", "sketch:p1aaaa"] and plan["params"]["judge"]["sketch_ref"] == 1
    assert plan["params"]["meta"]["variant"]["active_piece"] == "p2bbbb"
    # same as doc.active_piece did before
    assert ws.build_room_job({**d, "room": "uploads/room.png", "active_piece": "p2bbbb"})["roles"] == plan["roles"]
    plan = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "room", "dry_run": True, "active_piece": "nope"}).json()["plan"]
    assert plan["roles"][1] == "sketch:p1aaaa" and plan["params"]["meta"]["variant"]["active_piece"] == "p1aaaa"
    plan = c.post("/api/workshop/0db93df20e67/generate", json={"kind": "render", "dry_run": True, "active_piece": "p2bbbb"}).json()["plan"]
    assert plan["refs"] == ["uploads/sk2.png"] and plan["params"]["meta"]["variant"]["active_piece"] == "p2bbbb"


def test_regrade(client, store, fake, monkeypatch):
    c, _ = client
    monkeypatch.setattr(ws, "judge", None)
    r = c.post("/api/workshop/0db93df20e67/judge/oty2dff1").json()
    assert r["status"] == "unavailable" and r["comment"] == "Prüfer nicht verfügbar"

    seen = {}

    class FakeJudge:
        @staticmethod
        def grade(images, request):
            seen.update(images=images, request=request)
            return {"status": "ok", "overall": 7.5, "pass": True, "comment": "Gut."}

    monkeypatch.setattr(ws, "judge", FakeJudge)
    r = c.post("/api/workshop/0db93df20e67/judge/oty2dff1").json()
    assert r["overall"] == 7.5 and seen["request"]["kind"] == "room" and seen["images"]["room"].size == (1344, 1008)
    assert seen["request"]["pieces"][0]["box"] == [150, 245, 780, 545]
    assert store()["variants"][0]["judge"]["overall"] == 7.5 and fake.whos[-1] == ("anna", True)

    FakeJudge.grade = staticmethod(lambda images, request: seen.update(images=images) or {"status": "unavailable", "comment": "Prüfer nicht verfügbar"})
    c.post("/api/workshop/0db93df20e67/judge/oty2dff1")
    assert store()["variants"][0]["judge"]["overall"] == 7.5  # an outage never overwrites a real verdict

    # the sketch comes from the variant's active_piece, not doc.active_piece (v3 never updates it)
    png(out_dir(c) / "uploads/skA.png", (300, 400), color=(10, 10, 10))
    png(out_dir(c) / "uploads/skB.png", (300, 400), color=(200, 20, 20))
    d = fake.docs["0db93df20e67"]
    d["pieces"] = [piece("p1aaaa", sketch="uploads/skA.png"), piece("p2bbbb", sketch="uploads/skB.png")]
    d["active_piece"] = "p1aaaa"
    d["variants"][0].update(piece_ids=["p1aaaa", "p2bbbb"], active_piece="p2bbbb")
    c.post("/api/workshop/0db93df20e67/judge/oty2dff1")
    assert seen["images"]["sketch"].getpixel((5, 5)) == (200, 20, 20)
    d["variants"][0].pop("active_piece")
    c.post("/api/workshop/0db93df20e67/judge/oty2dff1")
    assert seen["images"]["sketch"].getpixel((5, 5)) == (10, 10, 10)  # old variants: piece order


def out_dir(_c):
    return ws.OUT


def test_record_partial_results_of_failed_job(store):
    ws.record(job(state="error", judge_state="pending"))  # image 2 of 2 raised: image 1 is still recorded and graded
    new = [v for v in store()["variants"] if v.get("job") == "a1b2c3d4e5"]
    assert len(new) == 2 and store()["pending_jobs"] == ["a1b2c3d4e5"]


def test_stale_pending_verdict_after_restart(tmp_path, monkeypatch):
    if not hasattr(projects, "changed_since"):
        pytest.skip("projects.py v3 not available yet")
    monkeypatch.setattr(projects, "STORE", tmp_path / "projects")
    (tmp_path / "projects/workshop").mkdir(parents=True)
    doc = copy.deepcopy(LEGACY)
    doc["pending_jobs"] = ["a1b2c3d4e5"]
    (tmp_path / "projects/workshop/0db93df20e67.json").write_text(json.dumps(doc))
    load = lambda: projects.load("workshop", "0db93df20e67")
    ws.record(job(judge_state="pending"))
    monkeypatch.setattr(projects, "job_running", lambda jid: jid == "a1b2c3d4e5")
    assert {v["judge"]["status"] for v in load()["variants"] if v.get("job") == "a1b2c3d4e5"} == {"pending"}
    monkeypatch.setattr(projects, "job_running", lambda jid: False)  # server restarted mid-grading
    js = [v["judge"] for v in load()["variants"] if v.get("job") == "a1b2c3d4e5"]
    assert {j["status"] for j in js} == {"error"} and "neu bewerten" in js[0]["comment"]


def test_room_variant_keeps_its_photo(store):
    doc = store()
    assert ws._variant_meta(doc, "room", ws._pieces(doc), {}, None)["room"] == doc["room"]
    assert ws._variant_meta(doc, "free", ws._pieces(doc), {}, None)["room"] is None


def _based_doc(out, fav=True):
    png(out / "v1.png", (400, 300))
    table = piece("p1aaaa", type="tisch", w=200, h=80, d=100, placement={"mode": "2d", "pose": None, "box": [300, 200, 800, 700]})
    shelf = piece("p2bbbb", type="wandregal", w=100, h=40, d=25, elev=150,
                  placement={"mode": "2d", "pose": None, "box": [100, 100, 250, 300]})
    doc = room_doc([table, shelf])
    snap = ws.snapshot(doc, ["p1aaaa"], {"p1aaaa": [300, 200, 800, 700]})
    doc["variants"] = [{"id": "v1", "kind": "room", "image": "v1.png", "room": doc["room"], "fav": fav,
                        "piece_ids": ["p1aaaa"], "keep": [[300, 200, 815, 700]], "judge": None, **snap}]
    return doc


def test_new_piece_is_added_to_the_starred_variant(out):
    plan = ws.build_room_job(_based_doc(out))
    p = plan["params"]
    assert plan["refs"][0] == "v1.png" and plan["base"]["kept"] == ["Esstisch"] and plan["base"]["new"] == ["Wandregal"]
    assert p["keep"] == [[100, 100, 265, 300]]                          # only the new piece's box may change
    assert "The solid wood dining table already in <ref_image_0> stays exactly as it is." in p["prompt"]
    assert "its underside hangs 150 cm above the floor" in p["prompt"]
    v = p["meta"]["variant"]
    assert v["piece_ids"] == ["p1aaaa", "p2bbbb"] and v["base"] == "v1" and v["room"] == "uploads/room.png"
    assert v["keep"] == [[300, 200, 815, 700], [100, 100, 265, 300]]


def test_no_base_when_off_changed_or_unstarred(out):
    doc = _based_doc(out)
    assert "base" not in ws.build_room_job({**doc, "base": False})
    moved = copy.deepcopy(doc)
    moved["pieces"][0]["placement"]["box"] = [300, 260, 800, 760]       # the table moved: everything is drawn again
    assert "base" not in ws.build_room_job(moved)
    assert "base" not in ws.build_room_job(_based_doc(out, fav=False))   # neither starred nor graded as good


def test_elevation_lifts_the_projected_box():
    import geometry
    pose = {"x": 0, "z": 300, "yaw": 0}
    floor = geometry.project_box(GEO, (100, 40, 25), pose)["box"]
    hung = geometry.project_box(GEO, (100, 40, 25), {**pose, "y": 150})["box"]
    assert hung[2] < floor[0]                                           # the hung shelf sits entirely above the floor one
    rel = geometry.relations(GEO, (100, 40, 25), {**pose, "y": 230})
    assert rel["ceiling_ratio"] == round(270 / 260, 3)                  # its top is above the 260 cm ceiling


def test_3d_piece_keeps_its_silhouette_plus_floor_shadow(out):
    p = piece("p1aaaa", type="sideboard", w=180, h=75, d=45,
              placement={"mode": "3d", "pose": {"x": 0, "z": 300, "yaw": 0, "wall": None}, "box": None})
    doc = room_doc([p], room_size=[1344, 1008], geometry=copy.deepcopy(GEO))
    boxes, _ = ws.resolve_boxes(doc)
    regs = ws.keep_regions_for(doc, doc["pieces"][0], boxes["p1aaaa"])
    assert len(regs) == 2 and all("poly" in r for r in regs)
    body = regs[0]["poly"]
    b = boxes["p1aaaa"]
    assert min(x for x, _ in body) >= b[1] - 1 and max(y for _, y in body) <= b[2] + 1   # inside the frame, not the frame
    hung = piece("p2bbbb", type="wandregal", w=100, h=40, d=25, elev=150,
                 placement={"mode": "3d", "pose": {"x": 0, "z": 380, "yaw": 0, "wall": "w0"}, "box": None})
    doc2 = room_doc([hung], room_size=[1344, 1008], geometry=copy.deepcopy(GEO))
    assert len(ws.keep_regions_for(doc2, doc2["pieces"][0], ws.resolve_boxes(doc2)[0]["p2bbbb"])) == 1   # no floor shadow
    plan = ws.build_room_job(doc)
    assert plan["roles"][:2] == ["room", "guide"] and "plain geometric mock-up" in plan["params"]["prompt"]
    assert "guide" not in ws.build_room_job({**doc, "mockup": False})["roles"]
