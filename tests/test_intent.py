import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import intent
import judge
import projects

# ---------------------------------------------------------------- normalize_de

CASES = [
    # metres + centimetres spoken as two numbers
    ("zwei zwanzig breit", "220 cm breit"),
    ("zwo zwanzig breit", "220 cm breit"),
    ("zwo-zwanzig breit", "220 cm breit"),
    ("zwei-fünfzig hoch", "250 cm hoch"),
    ("eins achtzig", "180 cm"),
    ("drei sechzig lang", "360 cm lang"),
    ("eins fünfundachtzig", "185 cm"),
    ("zwei null fünf hoch", "205 cm hoch"),
    # run together into one word
    ("zweizwanzig", "220 cm"),
    ("einsachtzig hoch", "180 cm hoch"),
    ("einsfünfundachtzig breit", "185 cm breit"),
    ("zwozwanzig", "220 cm"),
    ("einszehn breit", "110 cm breit"),
    ("zweizehn", "210 cm"),
    # with "Meter"
    ("ein Meter achtzig", "180 cm"),
    ("einen Meter zwanzig breit", "120 cm breit"),
    ("zwei Meter zwanzig", "220 cm"),
    ("zwei Meter zehn hoch", "210 cm hoch"),
    ("1 Meter 80", "180 cm"),
    ("1 m 20", "120 cm"),
    ("1m20", "120 cm"),
    ("2 m", "200 cm"),
    ("drei Meter", "300 cm"),
    ("zwei Meter", "200 cm"),
    ("zweieinhalb Meter", "250 cm"),
    ("anderthalb Meter", "150 cm"),
    ("eineinhalb Meter breit", "150 cm breit"),
    ("einen halben Meter", "50 cm"),
    ("ein halber Meter", "50 cm"),
    # decimals
    ("2,20", "220 cm"),
    ("2,20 breit", "220 cm breit"),
    ("2.20 m", "220 cm"),
    ("2,2 m", "220 cm"),
    ("1,8 m", "180 cm"),
    ("2,20m hoch", "220 cm hoch"),
    ("0,6 tief", "60 cm tief"),
    ("2,5 cm stark", "2,5 cm stark"),
    # centimetres and millimetres
    ("sechzig Zentimeter", "60 cm"),
    ("60 Zentimeter tief", "60 cm tief"),
    ("60cm", "60 cm"),
    ("fünfundvierzig Zenti", "45 cm"),
    ("600 mm tief", "60 cm tief"),
    ("2400 Millimeter", "240 cm"),
    ("19 mm Platte", "19 mm Platte"),
    # "er" forms
    ("220er", "220 cm"),
    ("60er Tiefe", "60 cm tief"),
    ("80er Breite", "80 cm breit"),
    ("sechziger Tiefe", "60 cm tief"),
    ("ein 60er Korpus", "ein 60 cm Korpus"),
    # bare numbers next to a dimension word
    ("achtzig tief", "80 cm tief"),
    ("220 breit", "220 cm breit"),
    ("Tiefe 60", "Tiefe 60 cm"),
    ("Tiefe sechzig", "Tiefe 60 cm"),
    ("Höhe: 75", "Höhe: 75 cm"),
    ("Breite von 180", "Breite von 180 cm"),
    ("zwanzig breiter", "20 cm breiter"),
    ("zehn Zentimeter schmaler", "10 cm schmaler"),
    ("Unterkante hundertvierzig", "Unterkante 140 cm"),
    ("zweihundertvierzig hoch", "240 cm hoch"),
    ("hundertachtzig breit", "180 cm breit"),
    ("fünfundvierzig tief", "45 cm tief"),
    # chains
    ("220 mal 60 mal 240", "220 × 60 × 240 cm"),
    ("160 x 80 x 45", "160 × 80 × 45 cm"),
    ("160 × 80 cm", "160 × 80 cm"),
    # room height phrases
    ("raumhoch", "raumhoch"),
    ("deckenhoch", "raumhoch"),
    ("deckenhohe Türen", "raumhohe Türen"),
    ("zimmerhoch", "raumhoch"),
    ("bis unter die Decke", "raumhoch"),
    ("bis unter d'Decke", "raumhoch"),
    ("bis zur Decke", "raumhoch"),
    ("bis an die Decke", "raumhoch"),
    ("vom Boden bis zur Decke", "raumhoch"),
    ("Schrank bis ganz oben unter die Decke", "Schrank raumhoch"),
    ("halbe Höhe", "halbe Raumhöhe"),
    ("auf halber Höhe", "auf halber Raumhöhe"),
    ("halbhoch", "halbe Raumhöhe"),
    # Bavarian / Franconian
    ("zwoa zwanzg broad", "220 cm breit"),
    ("zwoa Meter zwanzg", "220 cm"),
    ("oans achtzg", "180 cm"),
    ("fuchzg tief", "50 cm tief"),
    ("sechzg dief", "60 cm tief"),
    ("zwoarazwanzg Schubladen", "22 Schubladen"),
    ("oanazwanzg", "21"),
    ("vier Meter zwanzich lang", "420 cm lang"),
    ("achtzich hoach", "80 cm hoch"),
    ("zwoahundat", "200"),
    ("zwa Meter breit, fuchzich tief", "200 cm breit, 50 cm tief"),
    ("Raum hoch", "raumhoch"),
    ("Decken hohe Türen", "raumhohe Türen"),
    ("fünfe", "5"),
    # counts stay counts
    ("drei Türen", "3 Türen"),
    ("dreizehn Fächer", "13 Fächer"),
    ("zwei sechzig breite Schränke", "2 × 60 cm breite Schränke"),
    ("eine zwei sechzig breite Kommode", "eine 260 cm breite Kommode"),
    ("zwei Meter, drei Türen", "200 cm, 3 Türen"),
    # things that are not lengths
    ("ein Einbauschrank", "ein Einbauschrank"),
    ("einer der Schränke", "einer der Schränke"),
    ("nein, achtzig tief", "nein, 80 cm tief"),
    ("12 m² Raum", "12 m² Raum"),
    ("Kosten 2,50 Euro", "Kosten 2,50 Euro"),
    ("1.000 Stück", "1.000 Stück"),
    ("Weißlack, grifflos", "Weißlack, grifflos"),
    ("", ""),
]


@pytest.mark.parametrize("src,want", CASES)
def test_normalize_de(src, want):
    assert intent.normalize_de(src) == want


def test_normalize_full_sentence():
    s = ("links neben dem Fenster ein Einbauschrank, zwei-zwanzig breit, raumhoch, Weißlack, drei Türen grifflos, "
         "der alte Schrank kommt weg")
    assert intent.normalize_de(s) == ("links neben dem Fenster ein Einbauschrank, 220 cm breit, raumhoch, Weißlack, "
                                      "3 Türen grifflos, der alte Schrank kommt weg")
    assert intent.normalize_de("Hochschrank zwei vierzig, sechzig tief und Oberschränke fünfunddreißig tief") == \
        "Hochschrank 240 cm, 60 cm tief und Oberschränke 35 cm tief"


def test_normalize_is_idempotent():
    for src, _ in CASES:
        once = intent.normalize_de(src)
        assert intent.normalize_de(once) == once, src


@pytest.mark.parametrize("word,value", [
    ("zwanzig", 20), ("einundzwanzig", 21), ("neunundneunzig", 99), ("hundert", 100), ("hundertzwanzig", 120),
    ("einhundertachtzig", 180), ("zweihundertvierzig", 240), ("hundertundzehn", 110), ("tausend", 1000),
    ("zweitausendvierhundert", 2400), ("elf", 11), ("zwölf", 12), ("zwoarazwanzg", 22), ("dreiazwanzg", 23),
    ("fuffzig", 50), ("sechzich", 60), ("dreißig", 30), ("dreissig", 30), ("zweieinhalb", 2.5), ("anderthalb", 1.5),
])
def test_parse_number(word, value):
    assert intent.parse_number(word) == value


@pytest.mark.parametrize("word", ["Einbauschrank", "eine", "zweitürig", "hundertachtzigx", "Nische", "und"])
def test_parse_number_rejects(word):
    assert intent.parse_number(word) is None


# ---------------------------------------------------------------- plan() without a model

def reading(*pieces, drop=(), removes=(), confidence=0.9):
    full = []
    for p in pieces:
        full.append({"ref": None, "type": None, "w": None, "h": None, "d": None, "elev": None, "full_height": False,
                     "full_width": False, "material": None, "material_custom": "", "handles": None, "text": "",
                     "where": "", **p})
    return {"pieces": full, "drop": list(drop), "removes": list(removes), "confidence": confidence}


def mkdoc(*pieces, room_h=255, active=None):
    doc = {"id": "a1b2c3d4e5f6", "title": "TEST intent", "rev": 1, "frev": {}, "frev_floor": 0,
           "pieces": [{"material": "eiche", "handles": None, **p} for p in pieces]}
    if room_h:
        doc["geometry"] = {"ok": True, "ceiling": {"height": room_h}, "floor": {}}
    if active:
        doc["active_piece"] = active
    return projects.normalize_workshop(doc)


def test_plan_fills_the_empty_default_piece_with_defaults():
    doc = mkdoc()   # a fresh order has one empty piece
    empty_id = doc["pieces"][0]["id"]
    r = intent.plan(reading({"type": "schrank", "w": 220, "full_height": True, "material": "weiss", "handles": "push",
                             "text": "3 Türen", "where": "links neben dem Fenster"}, removes=["der alte Schrank"]), doc)
    p = r["pieces"][0]
    assert (p["ref"], p["id"], p["type"], p["w"], p["h"], p["d"]) == (None, empty_id, "schrank", 220, 255, 60)
    assert p["full_height"] is True and p["material"] == "weiss" and p["handles"] == "push"
    assert r["questions"] == [] and r["removes"] == [{"what": "der alte Schrank"}]
    assert all(o["op"] == "set" and o["path"].startswith(f"pieces.{empty_id}.") for o in r["ops"])
    assert "Einbauschrank 220 × raumhoch × 60 cm" in r["summary_de"] and "der alte Schrank" in r["summary_de"]


def test_plan_asks_for_width_only():
    r = intent.plan(reading({"type": "sideboard", "material": "kirsch", "text": "3 Schubladen"}), mkdoc())
    p = r["pieces"][0]
    assert p["w"] is None and (p["h"], p["d"]) == (75, 45) and p["handles"] == "push"
    assert r["questions"] == [{"piece": 0, "field": "w", "question_de": "Wie breit soll das Sideboard werden?",
                               "default": 180}]
    assert r["confidence"] < 0.9


def test_plan_full_width_needs_no_width_question():
    r = intent.plan(reading({"type": "kueche", "full_width": True}), mkdoc())
    assert r["questions"] == [] and r["pieces"][0]["w"] is None


def test_plan_wall_hung_types_get_their_elevation_and_no_handles_for_shelves():
    r = intent.plan(reading({"type": "wandregal", "w": 120}), mkdoc())
    p = r["pieces"][0]
    assert p["elev"] == 150 and p["handles"] is None and p["d"] == 25


def test_plan_wall_hung_up_to_the_ceiling():
    r = intent.plan(reading({"type": "haengeschrank", "w": 120, "h": 60, "full_height": True}), mkdoc(room_h=250))
    p = r["pieces"][0]
    assert (p["h"], p["elev"], p["full_height"]) == (60, 190, False)


def test_plan_raumhoch_in_text_means_full_height():
    r = intent.plan(reading({"type": "regal", "d": 35, "text": "raumhoch"}), mkdoc(room_h=262), text="Bücherregal raumhoch")
    p = r["pieces"][0]
    assert (p["h"], p["full_height"], p["text"]) == (262, True, "")
    r = intent.plan(reading({"type": "haengeschrank", "w": 120, "h": 60, "text": "raumhoch"}), mkdoc(room_h=250),
                    text="Hängeschrank raumhoch, 60 cm hoch, 120 cm breit")
    assert (r["pieces"][0]["elev"], r["pieces"][0]["h"]) == (190, 60)
    r = intent.plan(reading({"type": "schrank", "w": 200, "h": 210}), mkdoc(), text="Schrank 210 cm hoch, raumhohe Türen")
    assert r["pieces"][0]["h"] == 210


def test_plan_unmeasured_room_uses_250_and_warns():
    r = intent.plan(reading({"type": "schrank", "w": 200}), mkdoc(room_h=None))
    assert r["pieces"][0]["h"] == 250 and r["pieces"][0]["full_height"] is True
    assert any("250" in w for w in r["warnings"])


def test_plan_explicit_height_beats_full_height_default():
    r = intent.plan(reading({"type": "schrank", "w": 200, "h": 210}), mkdoc())
    assert r["pieces"][0]["h"] == 210 and r["pieces"][0]["full_height"] is False


def test_plan_profile_defaults_duck_typed():
    class Prof:
        def defaults_for(self, t):
            return {"d": 58, "h": None, "elev": 0, "material": "nuss", "handles": "leiste", "full_height": True}
    r = intent.plan(reading({"type": "schrank", "w": 200}), mkdoc(), Prof())
    p = r["pieces"][0]
    assert (p["d"], p["material"], p["handles"], p["h"]) == (58, "nuss", "leiste", 255)
    r = intent.plan(reading({"type": "sideboard", "w": 160}), mkdoc(), {"types": {"sideboard": {"d": 50, "h": 80}}})
    assert (r["pieces"][0]["d"], r["pieces"][0]["h"]) == (50, 80)
    r = intent.plan(reading({"type": "sideboard"}), mkdoc(), {"sideboard": {"w": 200}})
    assert r["questions"][0]["default"] == 200


def test_plan_custom_material():
    r = intent.plan(reading({"type": "sideboard", "w": 200, "material": "custom", "material_custom": "Lack schwarz"}), mkdoc())
    p = r["pieces"][0]
    assert p["material"] == "custom" and p["material_custom"] == "Lack schwarz"
    assert "Lack schwarz" in r["summary_de"]
    r = intent.plan(reading({"type": "sideboard", "w": 200, "material": "custom", "material_custom": ""}), mkdoc())
    assert r["pieces"][0]["material"] == "eiche"   # "custom" without a name falls back to the default


@pytest.mark.parametrize("text,field,want", [
    ("Bücherregal raumhoch, Esche", "material", "esche"), ("Eiche weiß geölt", "material", "eicheweiss"),
    ("aus Eichenholz", "material", "eiche"), ("Weißlack", "material", "weiss"), ("Nuss oder Eiche", "material", None),
    ("Arbeitsplatte", "material", None), ("mit Holzknöpfen", "handles", "knopf"), ("grifflos", "handles", "push"),
    ("Griffleiste", "handles", "leiste"), ("nichts", "handles", None),
])
def test_spoken(text, field, want):
    assert intent.spoken(text, field) == want


def test_plan_fills_material_the_model_left_out():
    r = intent.plan(reading({"type": "regal", "full_height": True, "d": 35}), mkdoc(), text="Bücherregal raumhoch, Esche")
    assert r["pieces"][0]["material"] == "esche"
    r = intent.plan(reading({"type": "regal"}, {"type": "sideboard"}), mkdoc(), text="Regal und Sideboard in Esche")
    assert r["pieces"][0]["material"] == "eiche"   # two pieces: no guessing which one is meant


def test_plan_correction_updates_only_changed_fields():
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220, "h": 255, "d": 60, "material": "weiss", "handles": "push"})
    r = intent.plan(reading({"ref": "p1schr", "d": 80, "w": 220}), doc)
    assert r["ops"] == [{"op": "set", "path": "pieces.p1schr.d", "value": 80}]
    assert r["pieces"][0]["ref"] == "p1schr" and r["pieces"][0]["d"] == 80 and r["pieces"][0]["changes"] == ["Tiefe 80 cm"]
    assert r["summary_de"] == "Einbauschrank: Tiefe 80 cm"


def test_plan_correction_to_custom_material():
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220, "material": "weiss"})
    r = intent.plan(reading({"ref": "p1schr", "material": "custom", "material_custom": "Lack schwarz"}), doc)
    assert r["ops"] == [{"op": "set", "path": "pieces.p1schr.material", "value": "custom"},
                        {"op": "set", "path": "pieces.p1schr.material_custom", "value": "Lack schwarz"}]
    assert r["summary_de"] == "Einbauschrank: Lack schwarz"


def test_plan_unattached_correction_goes_to_the_active_piece():
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220}, {"id": "p2side", "type": "sideboard", "w": 180},
                active="p2side")
    r = intent.plan(reading({"d": 40}), doc)
    assert r["ops"] == [{"op": "set", "path": "pieces.p2side.d", "value": 40}]


def test_plan_type_change_moves_untouched_defaults():
    doc = mkdoc({"id": "p1side", "type": "sideboard", "w": 120, "h": 75, "d": 45, "handles": "push"})
    r = intent.plan(reading({"ref": "p1side", "type": "haengeschrank"}), doc)
    paths = {o["path"]: o["value"] for o in r["ops"]}
    assert paths == {"pieces.p1side.type": "haengeschrank", "pieces.p1side.h": 60, "pieces.p1side.d": 35,
                     "pieces.p1side.elev": 140}


def test_plan_ignores_unknown_ids_and_bad_values():
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220})
    r = intent.plan({"pieces": [{"ref": "nope", "type": "rakete", "w": 99999, "material": "gold", "handles": "x",
                                 "text": "", "where": ""}], "drop": ["nope"], "removes": [5, " "], "confidence": "hoch"}, doc)
    assert r["ops"] == [] and r["removes"] == [] and r["drops"] == []


def test_plan_drop_and_last_piece_replacement():
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220}, {"id": "p2side", "type": "sideboard", "w": 180})
    r = intent.plan(reading(drop=["p2side"]), doc)
    assert r["ops"] == [{"op": "remove_piece", "id": "p2side"}] and r["questions"] == []
    assert "Sideboard wird gestrichen" in r["summary_de"]
    # dropping the only piece: the new piece becomes the replacement (apply_ops always keeps one piece)
    doc = mkdoc({"id": "p1wreg", "type": "wandregal", "w": 120})
    r = intent.plan(reading({"type": "haengeschrank", "w": 100}, drop=["p1wreg"]), doc)
    assert len(r["ops"]) == 1 and r["ops"][0]["op"] == "remove_piece"
    assert r["ops"][0]["replacement"]["type"] == "haengeschrank"
    assert r["pieces"][0]["id"] == r["ops"][0]["replacement"]["id"]
    # dropping the only piece without a new one: an empty replacement
    r = intent.plan(reading(drop=["p1wreg"]), doc)
    assert r["ops"][0]["replacement"]["type"] is None


def test_plan_respects_max_pieces():
    doc = mkdoc(*({"id": f"p{i}xxxx", "type": "sideboard", "w": 100} for i in range(4)))
    r = intent.plan(reading({"type": "wandregal", "w": 80}), doc)
    assert r["ops"] == [] and r["pieces"] == [] and r["questions"] == []
    assert any("Höchstens 4" in w for w in r["warnings"])
    doc = mkdoc(*({"id": f"p{i}xxxx", "type": "sideboard", "w": 100} for i in range(3)))
    r = intent.plan(reading({"type": "wandregal", "w": 80}, {"type": "haengeschrank", "w": 80}), doc)
    assert len(r["pieces"]) == 1 and len(r["warnings"]) == 1


def test_plan_nothing_understood_asks():
    r = intent.plan(reading(confidence=0.1), mkdoc())
    assert r["ops"] == [] and r["questions"][0]["field"] == "type" and r["confidence"] < 0.2


def test_room_height_sources():
    assert intent.room_height({"geometry": {"ok": True, "ceiling": {"height": 262.4}}}) == 262
    assert intent.room_height({"geometry": {"ok": False, "ceiling": {"height": 262}}, "calibration": {"room_height_cm": 248}}) == 248
    assert intent.room_height({"calibration": {"room_height_cm": 20}}) is None
    assert intent.room_height({}) is None


# ---------------------------------------------------------------- ops are accepted by apply_ops

@pytest.fixture
def store(tmp_path, monkeypatch):
    s = tmp_path / "projects"
    for k in projects.KINDS:
        (s / k).mkdir(parents=True)
    (tmp_path / "outputs" / "uploads").mkdir(parents=True)
    monkeypatch.setattr(projects, "STORE", s)
    monkeypatch.setattr(projects, "OUT", tmp_path / "outputs")
    monkeypatch.setattr(projects, "UPLOADS", tmp_path / "outputs" / "uploads")
    monkeypatch.setattr(projects, "EXPORTS", tmp_path / "outputs" / "exports")
    monkeypatch.setattr(projects, "job_running", None)
    return s


def put(store, doc):
    projects.write_json(store / "workshop" / f"{doc['id']}.json", doc)
    return doc["id"]


def test_ops_apply_new_piece_into_fresh_order(store):
    doc = projects.new_workshop({"title": "TEST intent"}, "anna")
    doc["rev"] = 1
    pid = put(store, doc)
    r = intent.plan(reading({"type": "schrank", "w": 220, "full_height": True, "material": "weiss", "handles": "push",
                             "text": "3 Türen", "where": "links"}, {"type": "wandregal", "w": 80, "material": "custom",
                                                                   "material_custom": "Lack schwarz"}), doc)
    new = projects.apply_ops(pid, 1, r["ops"], "anna")
    assert len(new["pieces"]) == 2
    a, b = new["pieces"]
    assert (a["type"], a["w"], a["h"], a["d"], a["material"], a["text"], a["where"]) == ("schrank", 220, 250, 60, "weiss", "3 Türen", "links")
    assert (b["type"], b["elev"], b["material"], b["material_custom"], b["label"]) == ("wandregal", 150, "custom", "Lack schwarz", "Wandregal")
    assert a["id"] == r["pieces"][0]["id"] and b["id"] == r["pieces"][1]["id"]


def test_ops_apply_corrections_drops_and_replacement(store):
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220, "h": 255, "d": 60, "material": "weiss", "handles": "push"},
                {"id": "p2side", "type": "sideboard", "w": 180, "h": 75, "d": 45})
    pid = put(store, doc)
    r = intent.plan(reading({"ref": "p1schr", "d": 80, "material": "eiche"}, drop=["p2side"]), doc)
    new = projects.apply_ops(pid, doc["rev"], r["ops"], "anna")
    assert [p["id"] for p in new["pieces"]] == ["p1schr"]
    assert (new["pieces"][0]["d"], new["pieces"][0]["material"]) == (80, "eiche")
    r = intent.plan(reading({"type": "haengeschrank", "w": 100}, drop=["p1schr"]), new)
    new2 = projects.apply_ops(pid, new["rev"], r["ops"], "anna")
    assert [p["type"] for p in new2["pieces"]] == ["haengeschrank"] and new2["pieces"][0]["elev"] == 140
    r = intent.plan(reading({"ref": new2["pieces"][0]["id"], "type": "sideboard"}), new2)
    new3 = projects.apply_ops(pid, new2["rev"], r["ops"], "anna")
    p = new3["pieces"][0]
    assert (p["type"], p["h"], p["d"], p.get("elev")) == ("sideboard", 75, 45, None)


# ---------------------------------------------------------------- understand() with a fake model

class Fake:
    def __init__(self, answers, health=200):
        self.answers, self.health, self.bodies = list(answers), health, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(self.health)
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": judge.JUDGE_MODEL}]})
        self.bodies.append(json.loads(req.content))
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    judge.mark_down()
    monkeypatch.setattr(judge, "unit_state", lambda: "active")
    monkeypatch.setattr(judge, "mem_available_gb", lambda: 80.0)
    monkeypatch.setattr(judge, "_sleep", lambda s: None)
    yield
    judge.mark_down()


def use(monkeypatch, fake):
    monkeypatch.setattr(judge, "_transport", httpx.MockTransport(fake))
    return fake


def test_understand_sends_normalized_text_and_order_context(monkeypatch):
    fake = use(monkeypatch, Fake([json.dumps(reading({"ref": "p1schr", "d": 80}))]))
    doc = mkdoc({"id": "p1schr", "type": "schrank", "w": 220, "h": 255, "d": 60, "material": "weiss", "text": "3 Türen"})
    r = intent.understand("nein, achtzig tief", doc)
    assert r["status"] == "ok" and r["text"] == "nein, 80 cm tief"
    assert r["ops"] == [{"op": "set", "path": "pieces.p1schr.d", "value": 80}]
    body = fake.bodies[0]
    user = body["messages"][1]["content"][-1]["text"]
    assert 'TEXT: "nein, 80 cm tief"' in user and "p1schr: schrank" in user and "ceiling height 255 cm" in user
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["pieces"]["items"]["properties"]["ref"]["anyOf"][0]["enum"] == ["p1schr"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_understand_unavailable_and_error(monkeypatch):
    use(monkeypatch, Fake(["{}"], health=503))
    r = intent.understand("Sideboard zwei Meter", mkdoc())
    assert r["status"] == "unavailable" and r["ops"] == [] and r["text"] == "Sideboard 200 cm"
    judge.mark_down()
    fake = use(monkeypatch, Fake(["kaputt", "immer noch kaputt"]))
    r = intent.understand("Sideboard zwei Meter", mkdoc())
    assert r["status"] == "error" and len(fake.bodies) == 2


def test_understand_repairs_sloppy_json(monkeypatch):
    use(monkeypatch, Fake(["```json\n{'pieces': [{'ref': None, 'type': 'sideboard', 'w': 200, 'h': None, 'd': None, "
                           "'elev': None, 'full_height': False, 'full_width': False, 'material': 'nuss', "
                           "'material_custom': '', 'handles': None, 'text': '', 'where': ''},], 'drop': [], "
                           "'removes': [], 'confidence': 0.8}\n```"]))
    r = intent.understand("Sideboard zwei Meter Nuss", mkdoc())
    assert r["status"] == "ok" and r["pieces"][0]["w"] == 200 and r["pieces"][0]["material"] == "nuss"


def test_understand_empty_text_asks():
    r = intent.understand("   ", mkdoc())
    assert r["status"] == "ok" and r["questions"][0]["field"] == "type"


# ---------------------------------------------------------------- router

@pytest.fixture
def client(store):
    app = FastAPI()

    @app.middleware("http")
    async def who(request: Request, call_next):
        request.state.owner = "anna"
        return await call_next(request)
    app.include_router(intent.router)
    monkey_doc = projects.new_workshop({"title": "TEST intent"}, "anna")
    monkey_doc["rev"] = 1
    put(store, monkey_doc)
    return TestClient(app), monkey_doc


def test_route_validation(client, monkeypatch):
    c, doc = client
    assert c.post(f"/api/workshop/{doc['id']}/understand", json={"text": " "}).status_code == 400
    assert c.post(f"/api/workshop/{doc['id']}/understand", json={"text": "x" * 2001}).status_code == 400
    r = c.post(f"/api/workshop/{doc['id']}/understand", json={"text": "Sideboard", "apply": True})
    assert r.status_code == 400 and "base_rev" in r.json()["detail"]
    assert c.post("/api/workshop/000000000000/understand", json={"text": "Sideboard"}).status_code == 404


def test_route_preview_and_apply(client, monkeypatch):
    c, doc = client
    monkeypatch.setattr(intent, "_profile", lambda: None)
    use(monkeypatch, Fake([json.dumps(reading({"type": "sideboard", "w": 180, "material": "nuss"}))]))
    r = c.post(f"/api/workshop/{doc['id']}/understand", json={"text": "Sideboard eins achtzig in Nuss"})
    assert r.status_code == 200 and "doc" not in r.json() and r.json()["ops"]
    assert projects.load("workshop", doc["id"])["pieces"][0]["type"] is None   # preview only
    r = c.post(f"/api/workshop/{doc['id']}/understand", json={"text": "Sideboard eins achtzig in Nuss", "apply": True,
                                                             "base_rev": doc["rev"]})
    body = r.json()
    assert r.status_code == 200 and body["rev"] == doc["rev"] + 1
    p = body["doc"]["pieces"][0]
    assert (p["type"], p["w"], p["h"], p["d"], p["material"]) == ("sideboard", 180, 75, 45, "nuss")
    assert projects.load("workshop", doc["id"])["pieces"][0]["type"] == "sideboard"


def test_route_apply_conflict_is_409(client, monkeypatch):
    c, doc = client
    monkeypatch.setattr(intent, "_profile", lambda: None)
    pid0 = doc["pieces"][0]["id"]
    # a colleague picked a wood meanwhile; filling the piece would overwrite it with the default
    projects.apply_ops(doc["id"], doc["rev"], [{"op": "set", "path": f"pieces.{pid0}.material", "value": "nuss"}], "ben")
    use(monkeypatch, Fake([json.dumps(reading({"type": "sideboard", "w": 180}))]))
    r = c.post(f"/api/workshop/{doc['id']}/understand", json={"text": "Sideboard eins achtzig", "apply": True,
                                                             "base_rev": doc["rev"]})
    assert r.status_code == 409


def test_route_unavailable_does_not_apply(client, monkeypatch):
    c, doc = client
    monkeypatch.setattr(intent, "_profile", lambda: None)
    use(monkeypatch, Fake(["{}"], health=503))
    r = c.post(f"/api/workshop/{doc['id']}/understand", json={"text": "Sideboard", "apply": True, "base_rev": doc["rev"]})
    assert r.status_code == 200 and r.json()["status"] == "unavailable" and "doc" not in r.json()


def test_route_bare_correction_goes_to_the_open_piece(client, store, monkeypatch):
    """The Möbel card sends the open piece: „nein, nur dreißig tief“ after adding a shelf must not hit the cabinet."""
    c, doc = client
    monkeypatch.setattr(intent, "_profile", lambda: None)
    doc = {**doc, "pieces": [{"id": "p1schr", "type": "schrank", "w": 220, "h": 255, "d": 60, "material": "weiss"},
                             {"id": "p2regal", "type": "wandregal", "w": 120, "h": 30, "d": 25, "material": "eiche"}]}
    put(store, projects.normalize_workshop(doc))
    fake = use(monkeypatch, Fake([json.dumps(reading({"d": 30}))]))
    url = f"/api/workshop/{doc['id']}/understand"
    r = c.post(url, json={"text": "nein, nur dreißig tief", "active_piece": "p1schr"}).json()
    assert r["ops"] == [{"op": "set", "path": "pieces.p1schr.d", "value": 30}]
    r = c.post(url, json={"text": "nein, nur dreißig tief", "active_piece": "p2regal"}).json()
    assert [o["path"] for o in r["ops"]] == ["pieces.p2regal.d"]
    plain = c.post(url, json={"text": "nein, nur dreißig tief"}).json()["ops"]
    r = c.post(url, json={"text": "nein, nur dreißig tief", "active_piece": "gibtsnicht"}).json()
    assert r["ops"] == plain and len(fake.bodies) == 4                   # an unknown id is ignored
