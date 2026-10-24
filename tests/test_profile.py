import json
import os
import stat
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import profile
import projects

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    s = tmp_path / "projects"
    s.mkdir()
    monkeypatch.setattr(projects, "STORE", s)
    monkeypatch.setattr(profile, "PATH", None)
    monkeypatch.setattr(profile, "PACKS", ROOT / "packs")
    monkeypatch.setattr(profile, "_CACHE", {})
    return s


def _piece(pid, t="schrank", **kw):
    return {"id": pid, "type": t, "material": "eiche", "handles": "push", "w": 200, "h": 240, "d": 60, **kw}


# ---------------- packs ----------------

def test_shipped_packs_are_complete_and_strictly_valid():
    packs = profile.packs_by_id()
    assert set(packs) == {"schreiner", "kueche", "treppe", "terrassendach", "schiebetuer"}
    for pid in packs:
        raw = json.loads((ROOT / "packs" / f"{pid}.json").read_text(encoding="utf-8"))
        p = profile._pack(raw, pid, strict=True)           # raises on any unknown id
        assert p["label"] and p["description"] and p["wording"]["prompt_en"] and p["wording"]["judge_en"]
        assert len(p["templates"]) + len(p["types"]) >= 3


def test_profile_ids_match_the_app():
    assert set(profile.TYPES) == set(projects.PIECE_LABELS)
    assert set(profile.MATERIALS) == set(projects.MATERIAL_LABELS)
    assert set(profile.HANDLES) == set(projects.HANDLE_LABELS)
    # every default a pack produces passes the PATCH validator of projects.py
    for pid in profile.packs_by_id():
        profile.put({"trade": pid})
        eff = profile.effective()
        for t in eff["types"]:
            pd = profile.piece_defaults(t)
            assert set(pd) <= set(projects.PIECE_FIELDS)
            for f, v in pd.items():
                projects._piece_value(f, v)
            assert pd["type"] is not None or pd["text"], (pid, t)   # free-text templates carry a description


# ---------------- get / put ----------------

def test_missing_file_gives_pack_defaults():
    prof = profile.get()
    assert prof["trade"] == "schreiner" and prof["types"] == {} and prof["auto_send"]["enabled"] is False
    d = profile.defaults_for("schrank")
    assert (d["d"], d["full_height"], d["material"], d["handles"], d["plinth"]) == (60, True, "eiche", "push", 8)
    assert d["source"]["d"] == "paket" and d["source"]["w"] == "katalog"
    assert d["doors"] == {"per": 50} and d["doors_n"] == 4                     # 200 cm / 50
    assert profile.defaults_for("haengeschrank")["elev"] == 140
    assert not profile.path().exists()                                         # reading never writes


def test_put_merges_persists_private_and_atomic(store):
    profile.put({"company": {"material": "weiss", "plinth": 10}, "types": {"schrank": {"d": 65}}}, "anna")
    profile.put({"company": {"handles": "leiste"}, "quality": "fein"}, "ben")
    p = profile.path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [f.name for f in store.iterdir()] == ["profile.json"]               # no temp files left behind
    raw = json.loads(p.read_text())
    assert raw["company"]["material"] == "weiss" and raw["company"]["handles"] == "leiste" and raw["company"]["plinth"] == 10
    assert raw["updated_by"] == "ben" and raw["quality"] == "fein"
    d = profile.defaults_for("schrank")
    assert (d["d"], d["material"], d["handles"]) == (65, "weiss", "leiste")
    assert d["source"]["d"] == "firma" and d["source"]["material"] == "firma"
    profile.put({"types": {"schrank": None}, "company": {"material": None}})   # null = inherit again
    d = profile.defaults_for("schrank")
    assert (d["d"], d["material"]) == (60, "eiche")


@pytest.mark.parametrize("patch, msg", [
    ({"farbe": "rot"}, "unbekanntes Feld „farbe“"),
    ({"trade": "baecker"}, "Unbekanntes Fachpaket"),
    ({"company": {"material": "gold"}}, "unbekanntes Material „gold“"),
    ({"company": {"handles": "kordel"}}, "unbekannte Griffart"),
    ({"company": {"plinth": 50}}, "Sockelhöhe"),
    ({"company": {"board": True}}, "Plattenstärke"),
    ({"types": {"rakete": {"d": 3}}}, "Unbekannter Möbeltyp"),
    ({"types": {"schrank": {"d": 0}}}, "Tiefe"),
    ({"types": {"schrank": {"doors": {"per": 5}}}}, "Türbreite"),
    ({"types": {"schrank": {"elev": 999}}}, "Abstand vom Boden"),
    ({"quality": "super"}, "Qualität"),
    ({"aliases": {"material": {"Gold": "gold"}}}, "unbekanntes Material"),
    ({"aliases": {"type": {"x": "schrank"}}}, "2 bis 60 Zeichen"),
    ({"auto_send": {"min_overall": 11}}, "Mindestnote"),
    ({"wording": {"notes_de": "x" * 1001}}, "höchstens 1000 Zeichen"),
    ({"learning": {"threshold": 1}}, "2 bis 10"),
])
def test_put_rejects_with_german_message_and_keeps_the_file(patch, msg):
    profile.put({"company": {"material": "nuss"}})
    before = profile.path().read_bytes()
    with pytest.raises(profile.Bad, match=msg):
        profile.put(patch)
    assert profile.path().read_bytes() == before


def test_trade_switch_changes_defaults_and_templates():
    profile.put({"trade": "kueche"})
    assert profile.defaults_for("kueche")["handles"] == "leiste" and profile.defaults_for("kueche")["plinth"] == 10
    k = profile.defaults_for("kochinsel")
    assert (k["type"], k["template"], k["w"], k["d"]) == ("kueche", "kochinsel", 200, 100)
    profile.put({"trade": "treppe"})
    assert profile.defaults_for("treppe")["handles"] is None                    # "keine" → no handles
    assert profile.piece_defaults("treppe")["handles"] is None
    profile.put({"trade": "terrassendach"})
    t = profile.defaults_for("terrassendach")
    assert t["type"] is None and t["material"] == "custom" and t["material_custom"] == "Douglasie natur"
    assert "Terrassenüberdachung" in profile.piece_defaults("terrassendach")["text"]


def test_template_inherits_base_type_and_company_material():
    profile.put({"trade": "schiebetuer", "types": {"schrank": {"material": "eicheweiss", "d": 70}}})
    s = profile.defaults_for("schiebetuerschrank")
    assert s["type"] == "schrank" and s["d"] == 65                             # template dims stay
    assert s["material"] == "eicheweiss"                                       # company choice for the base type
    assert s["doors"] == {"per": 100} and profile.doors_for(s, 230) == 2
    assert profile.piece_defaults("schiebetuerschrank")["h"] is None           # raumhoch: height comes from the room


def test_unknown_type_falls_back_to_company():
    d = profile.defaults_for(None)
    assert d["type"] is None and d["w"] is None and d["material"] == "eiche" and d["doors_n"] is None


# ---------------- vocabulary ----------------

def test_resolve_aliases_longest_first_and_word_boundaries():
    r = profile.resolve("Links ein Einbauschrank, zwei-zwanzig breit, Weißlack, drei Türen grifflos")
    assert (r["type"], r["material"], r["handles"]) == ("schrank", "weiss", "push")
    r = profile.resolve("Kleiderschrank in Eiche weiß geölt mit Messinggriffen")
    assert (r["material"], r["handles"]) == ("eicheweiss", "messing")
    assert profile.resolve("ein Hängeschrank über der Spüle")["type"] == "haengeschrank"   # not "schrank"
    assert profile.resolve("PUSH TO OPEN bitte")["handles"] == "push"
    r = profile.resolve("in Buche")
    assert (r["material"], r["material_custom"]) == ("custom", "Buche massiv geölt")
    assert profile.resolve("nichts davon")["hits"] == []


def test_profile_aliases_override_pack_and_templates_resolve():
    profile.put({"aliases": {"material": {"Weißlack": "anthrazit", "Hausfarbe": "nuss"}}})
    assert profile.resolve("Weißlack")["material"] == "anthrazit"
    assert profile.resolve("in unserer Hausfarbe")["material"] == "nuss"
    profile.put({"trade": "schiebetuer"})
    r = profile.resolve("ein Schwebetürenschrank mit Spiegel")
    assert (r["type"], r["template"], r["material"], r["material_custom"]) == ("schrank", "schiebetuerschrank", "custom", "Spiegelglas")
    assert "Hausfarbe" in profile.voice_words()
    assert "→ material nuss" in profile.vocabulary_hint()
    assert len(profile.vocabulary_hint(limit=100)) <= 100


def test_prompt_notes_append_company_wording():
    profile.put({"wording": {"prompt_en": "Always oiled solid wood."}})
    n = profile.prompt_notes()
    assert n["prompt_en"].startswith("Bespoke joinery") and n["prompt_en"].endswith("Always oiled solid wood.")
    assert profile.effective()["quality"] == "entwurf"
    profile.put({"trade": "treppe"})
    assert profile.effective()["quality"] == "fein"                            # pack preference
    profile.put({"quality": "entwurf"})
    assert profile.effective()["quality"] == "entwurf"                         # company beats pack


# ---------------- learning ----------------

def test_three_identical_corrections_make_a_suggestion_and_accept_sets_default():
    for i in range(2):
        assert profile.learn(_piece(f"p{i}"), _piece(f"p{i}", d=65), "anna") == []
    assert profile.learn(_piece("p0"), _piece("p0", d=65)) == []               # same piece again: one vote
    new = profile.learn(_piece("p2"), _piece("p2", d=65), "ben")
    assert len(new) == 1 and new[0]["field"] == "d" and new[0]["value"] == 65 and new[0]["count"] == 3
    assert new[0]["text_de"] == "Einbauschrank: Tiefe standardmäßig 65 cm statt 60 cm? (3× so korrigiert)"
    assert [s["id"] for s in profile.suggestions()] == [new[0]["id"]]
    assert profile.learn(_piece("p3"), _piece("p3", d=65)) == []               # already open: no duplicate
    res = profile.accept(new[0]["id"], "chef")
    assert res["suggestion"]["status"] == "accepted" and res["suggestion"]["decided_by"] == "chef"
    assert profile.defaults_for("schrank")["d"] == 65 and profile.suggestions() == []
    assert profile.learn(_piece("p9"), _piece("p9", d=65)) == []               # now the default: nothing to learn
    assert profile.accept(new[0]["id"])["suggestion"]["status"] == "accepted"  # idempotent
    with pytest.raises(KeyError):
        profile.accept("nope")


def test_learn_ignores_type_changes_defaults_and_custom_material():
    assert profile.learn(_piece("a", t="regal"), _piece("a", t="schrank", d=80)) == []
    for i in range(5):
        profile.learn(_piece(f"b{i}", d=50), _piece(f"b{i}", d=60))           # 60 is already the default
        profile.learn(_piece(f"c{i}"), _piece(f"c{i}", material="custom"))
        profile.learn(_piece(f"e{i}"), _piece(f"e{i}", w=230))                # width is the order's measure
    assert profile.get()["learned"]["counts"] == {} and profile.suggestions() == []


def test_material_suggestion_and_dismiss_restarts_counting():
    for i in range(3):
        new = profile.learn(_piece(f"m{i}"), _piece(f"m{i}", material="weiss"))
    sid = new[0]["id"]
    assert "Lack weiß matt statt Eiche natur geölt" in new[0]["text_de"]
    assert profile.dismiss(sid, "chef")["suggestion"]["status"] == "dismissed"
    assert profile.defaults_for("schrank")["material"] == "eiche"
    for i in range(2):
        assert profile.learn(_piece(f"n{i}"), _piece(f"n{i}", material="weiss")) == []
    assert profile.learn(_piece("n2"), _piece("n2", material="weiss"))[0]["status"] == "open"


def test_accepting_a_height_replaces_raumhoch():
    for i in range(3):
        new = profile.learn(_piece(f"h{i}"), _piece(f"h{i}", h=230))
    profile.accept(new[0]["id"])
    d = profile.defaults_for("schrank")
    assert (d["h"], d["full_height"]) == (230, False) and profile.piece_defaults("schrank")["h"] == 230


def test_learning_threshold_and_switch():
    profile.put({"learning": {"threshold": 2}})
    profile.learn(_piece("a"), _piece("a", handles="leiste"))
    assert len(profile.learn(_piece("b"), _piece("b", handles="leiste"))) == 1
    profile.put({"learning": {"enabled": False}})
    profile.learn(_piece("c"), _piece("c", d=80))
    assert not any("|d|" in k for k in profile.get()["learned"]["counts"])
    profile.put({"learned": None})                                             # forget everything learned
    assert profile.get()["learned"] == {"counts": {}, "suggestions": []}


def test_learn_doc_counts_only_explicitly_set_fields():
    before = {"pieces": [_piece("x1"), _piece("x2", t="regal", d=35)]}
    after = {"pieces": [_piece("x1", d=70, handles="leiste"), _piece("x2", t="regal", d=40)]}
    profile.learn_doc(before, after, "anna", ops=[{"op": "set", "path": "pieces.x1.d", "value": 70}])
    keys = set(profile.get()["learned"]["counts"])
    assert keys == {'schrank|d|70'}                                            # handles changed as a side effect: ignored
    assert profile.learn_doc(before, after, "anna", ops=[{"op": "add_piece", "piece": {}}]) == []
    profile.learn_doc(before, after, "anna")                                   # without ops: every diff
    assert {'regal|d|40', 'schrank|handles|"leiste"'} <= set(profile.get()["learned"]["counts"])


def test_learn_doc_ignores_test_orders():
    before = {"title": "TEST Großtest A", "test": True, "pieces": [_piece("x1")]}
    after = {"title": "TEST Großtest A", "test": True, "pieces": [_piece("x1", d=70)]}
    assert profile.learn_doc(before, after, "anna", ops=[{"op": "set", "path": "pieces.x1.d", "value": 70}]) == []
    assert profile.learn_doc({"title": "TEST x", "pieces": before["pieces"]}, {"title": "TEST x", "pieces": after["pieces"]},
                             "anna") == []
    assert not profile.get()["learned"]["counts"]


def test_concurrent_learning_loses_no_update():
    def worker(k):
        for i in range(5):
            profile.learn(_piece(f"t{k}x{i}"), _piece(f"t{k}x{i}", d=72), f"w{k}")
    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert profile.get()["learned"]["counts"]["schrank|d|72"]["n"] == 40
    assert len(profile.suggestions()) == 1


def test_counters_are_bounded(monkeypatch):
    monkeypatch.setattr(profile, "MAX_COUNTS", 5)
    for v in range(61, 70):
        profile.learn(_piece(f"q{v}"), _piece(f"q{v}", d=v))
    assert len(profile.get()["learned"]["counts"]) == 5


# ---------------- auto-send, robustness ----------------

def test_auto_send_is_off_by_default_and_picks_best_passing():
    vs = [{"overall": 8.1, "pass": True}, {"overall": 9.0, "pass": False}, {"overall": 7.9, "pass": True},
          {"overall": 6.0, "pass": True}, {"overall": None}]
    assert profile.auto_send_decision(vs)["send"] is False
    profile.put({"auto_send": {"enabled": True, "min_overall": 7.5}})
    d = profile.auto_send_decision(vs)
    assert d == {"send": True, "picks": [0, 2], "why": "2 Bild(er) ab Note 7.5."}
    profile.put({"auto_send": {"require_pass": False, "max_images": 1}})
    assert profile.auto_send_decision(vs)["picks"] == [1]
    assert profile.auto_send_decision([{"overall": 3, "pass": True}])["send"] is False


def test_corrupt_file_is_moved_aside_not_lost(store):
    profile.path().write_text("{kaputt")
    assert profile.get()["trade"] == "schreiner"
    assert [f.name.startswith("profile.json.corrupt-") for f in store.iterdir()] == [True]
    profile.put({"quality": "fein"})
    assert profile.get()["quality"] == "fein"


def test_lenient_load_drops_bad_values_from_disk():
    profile.path().write_text(json.dumps({"trade": "weg", "company": {"material": "gold", "plinth": 12},
                                          "types": {"schrank": {"d": "65"}, "ufo": {"d": 1}}, "quality": 3}))
    prof = profile.get()
    assert prof["trade"] == "schreiner" and prof["company"]["material"] is None and prof["company"]["plinth"] == 12
    assert prof["types"] == {"schrank": {"d": 65}} and prof["quality"] is None


def test_stdlib_profile_shim_keeps_cprofile_working():
    import cProfile
    assert callable(profile.run) and cProfile.run.__doc__


# ---------------- router ----------------

@pytest.fixture
def client():
    a = FastAPI()

    @a.middleware("http")
    async def who(request: Request, call_next):
        request.state.owner = "anna"
        return await call_next(request)

    a.include_router(profile.router)
    return TestClient(a)


def test_router_roundtrip(client):
    r = client.get("/api/profile")
    assert r.status_code == 200 and r.json()["effective"]["types"]["schrank"]["d"] == 60
    r = client.put("/api/profile", json={"trade": "kueche", "company": {"board": 16}})
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["updated_by"] == "anna" and body["effective"]["company"]["board"] == 16
    assert body["effective"]["pack"]["id"] == "kueche"
    r = client.put("/api/profile", json={"company": {"plinth": -1}})
    assert r.status_code == 400 and r.json()["detail"] == "Sockelhöhe muss zwischen 0 und 30 cm liegen."
    r = client.get("/api/profile/packs")
    assert r.json()["current"] == "kueche" and len(r.json()["packs"]) == 5
    sch = next(p for p in r.json()["packs"] if p["id"] == "schiebetuer")
    assert {"id": "schiebetuerschrank", "label": "Schiebetürenschrank", "type": "schrank"} in sch["templates"]


def test_router_suggestions_accept_dismiss(client):
    profile.put({"trade": "kueche"})
    for i in range(3):
        profile.learn(_piece(f"k{i}", t="kueche", d=60), _piece(f"k{i}", t="kueche", d=65))
    sug = client.get("/api/profile/suggestions").json()["suggestions"]
    assert len(sug) == 1 and sug[0]["type"] == "kueche"
    r = client.post(f"/api/profile/suggestions/{sug[0]['id']}/accept")
    assert r.status_code == 200 and r.json()["effective"]["types"]["kueche"]["d"] == 65
    assert r.json()["suggestion"]["decided_by"] == "anna"
    assert client.get("/api/profile/suggestions").json()["suggestions"] == []
    assert client.post("/api/profile/suggestions/abc/accept").status_code == 404
    r = client.post("/api/profile/suggestions/abc/dismiss")
    assert r.status_code == 404 and r.json()["detail"] == "Vorschlag nicht gefunden."
