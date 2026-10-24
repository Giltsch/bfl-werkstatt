import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

import printing

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
FIX = ROOT / "tests" / "fixtures"
LEGACY = FIX / "legacy_v1_0db93df20e67.json"
REAL = FIX / "real_v2_bb505aeddc04.json"
ROOM = "uploads/20261016-5d0db410c1.png"
IMAGES = ["20261016-201402-1738609104.png", "20261016-183109-2032343468.png", "20261016-182745-1353941386.png"]


@pytest.fixture
def root(tmp_path):
    """A private copy of the few images the sheets use (outputs/ is never written)."""
    r = tmp_path / "root"
    (r / "uploads").mkdir(parents=True)
    for rel in [ROOM, *IMAGES]:
        if not (OUT / rel).exists():
            pytest.skip(f"{rel} missing")
        shutil.copy(OUT / rel, r / rel)
    rgba = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    rgba.paste((30, 30, 30, 255), (50, 50, 350, 60))
    rgba.save(r / "uploads" / "sketch.png")
    (tmp_path / "out").mkdir()
    return r


def pdf_text(pdf, first=None, last=None):
    cmd = ["pdftotext"] + (["-f", str(first), "-l", str(last)] if first else []) + [str(pdf), "-"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def build(doc, root, skipped=None, **kw):
    out = root.parent / "out"
    for f in out.iterdir():
        f.unlink()
    pdf = printing.build_sheet(doc, root, out, skipped, **kw)
    pages = int(next(l.split()[-1] for l in subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True,
                                                             check=True).stdout.splitlines() if l.startswith("Pages:")))
    printing.run_pdftoppm(pdf, out / "r", 40)
    assert len(list(out.glob("r-*.jpg"))) == pages
    text = pdf_text(pdf)
    for bad in ("Prüf", "score", "seed", "Interne", "anonymisiert"):
        assert bad not in text
    return pages, text


def piece(pid, label, mat, w, h, d, **kw):
    return {"id": pid, "type": None, "label": label, "material_label": mat, "handles_label": "Griffleiste",
            "w": w, "h": h, "d": d, **kw}


def variant(vid, image, kind, pieces, **kw):
    return {"id": vid, "image": image, "kind": kind, "fav": False, "note": "", "piece_ids": [p["id"] for p in pieces],
            "pieces": [{k: p[k] for k in ("id", "label", "material_label", "handles_label", "w", "h", "d")} for p in pieces],
            "boxes": {}, "seed": 123456, "judge": {"status": "ok", "overall": 8.1, "comment": "Prüfer sagt gut"}, **kw}


def two_piece_doc():
    a, b = piece("pa1234", "Einbauschrank", "Eiche natur geölt", 180, 250, 60), piece("pb1234", "Sideboard", "Nussbaum", 180, None, 45)
    return {"version": 3, "title": "Wohnzimmer", "customer": "Familie Test", "room": ROOM, "pieces": [a, b],
            "variants": [variant("v1aaaa", IMAGES[0], "room", [a, b], fav=True, note="Sockel 10 cm zurückgesetzt."),
                         variant("v2aaaa", IMAGES[2], "material", [a, b], fav=True, **{"from": "v1aaaa"})],
            "notes": "Kunde zahlt bar", "internal_notes": "Rabatt 10 % intern",
            "company": {"name": "Schreinerei Holz", "contact": "Tel. 0123", "color": "#7a4b2a"}}


def test_legacy_doc(root):
    import projects
    doc = projects.migrate_workshop(json.loads(LEGACY.read_text()))
    pages, text = build(doc, root)
    assert pages == 1  # the one favourite
    assert "Vorher" in text and "Nachher" in text and "Nussbaum" in text and "Einbauschrank" in text
    assert "180 × 250 × 60 cm" in text and "Maße B × H × T" in text
    # the raw v1 doc is still accepted
    pages, text = build(json.loads(LEGACY.read_text()), root)
    assert pages == 1 and "Nussbaum" in text and "180 × 250 × 60 cm" in text


def test_two_pieces(root):
    pages, text = build(two_piece_doc(), root)
    assert pages == 2
    assert "Nachher 1" in text and "Nachher 2" in text and "Vorher" in text
    assert "180 × 250 × 60 cm" in text and "180 × – × 45 cm" in text and "Sideboard" in text
    assert "Schreinerei Holz" in text
    # the internal edit history, doc notes and internal notes never reach the customer (the old sheet printed v.note)
    assert "Sockel" not in text and "bar" not in text and "Rabatt" not in text
    assert "Gewählter Entwurf" not in text and "Vorgang" not in text  # no version, no ref


def test_render_with_and_without_sketch(root):
    a = piece("pa1234", "Sideboard", "Nussbaum", 180, 75, 45, sketch="uploads/sketch.png")
    doc = {"version": 2, "title": "Projekt R", "room": ROOM, "pieces": [a], "selected": "v1aaaa",
           "variants": [variant("v1aaaa", IMAGES[1], "render", [a], sketch="uploads/sketch.png")]}
    pages, text = build(doc, root)
    assert pages == 1 and "Skizze" in text and "Entwurf" in text and "Vorher" not in text and "180 × 75 × 45 cm" in text
    doc["variants"][0]["sketch"] = None  # falls back to the piece's sketch
    assert "Skizze" in build(doc, root)[1]
    a["sketch"] = "uploads/gone.png"     # no sketch file: one large picture
    pages, text = build(doc, root)
    assert pages == 1 and "Entwurf" in text and "Skizze" not in text and "Vorher" not in text


def test_render_sketch_alpha_on_white(root):
    img = printing.SheetImages(root).open("uploads/sketch.png")
    assert img.mode == "RGB" and img.getpixel((5, 5)) == (255, 255, 255) and img.getpixel((100, 55)) == (30, 30, 30)


def test_free_variant(root):
    a = piece("pa1234", "Esstisch", "Esche", 180, 76, 90)
    doc = {"version": 2, "title": "Frei", "room": None, "pieces": [a],
           "variants": [variant("v1aaaa", IMAGES[1], "free", [a])]}
    pages, text = build(doc, root)
    assert pages == 1 and "Entwurf" in text and "Vorher" not in text and "Nachher" not in text and "180 × 76 × 90 cm" in text


def test_missing_images_skipped(root):
    a = piece("pa1234", "Einbauschrank", "Eiche natur geölt", 180, 250, 60)
    doc = {"version": 2, "title": "Fehlt", "room": ROOM, "pieces": [a],
           "variants": [variant("v1aaaa", "29990101-000000-1.png", "room", [a], fav=True),
                        variant("v2aaaa", IMAGES[0], "room", [a], fav=True)]}
    skipped = []
    pages, text = build(doc, root, skipped)
    assert pages == 1 and skipped == ["v1aaaa"] and "Nachher" in text
    only = copy.deepcopy(doc)
    only["variants"] = only["variants"][:1]
    with pytest.raises(printing.ExportError, match="Die Bilder der gewählten Varianten fehlen."):
        printing.build_sheet(only, root, root.parent / "out", [])


def test_fallback_rows_from_doc_pieces(root):
    a = piece("pa1234", "Küche", "", 240, 220, 60, material="custom", material_custom="Beton-Optik")
    v = variant("v1aaaa", IMAGES[0], "room", [a])
    del v["pieces"]
    rows = printing.sheet_rows({"pieces": [a]}, v)
    assert rows == [("Küche", "Beton-Optik", "Griffleiste", "240 × 220 × 60 cm")]
    assert printing.dims_text(None, None, None) == "–" and printing.dims_text("180", None, 60) == "180 × – × 60 cm"


def test_real_fixture_never_prints_internal_notes(tmp_path):
    """The research found the edit history („Mach die eckbank …“) on the customer sheet; the anonymized fixture has
    „Interne Änderungsnotiz N (anonymisiert)“ in every edited variant's note."""
    doc = json.loads(REAL.read_text())
    r = tmp_path / "root"
    (r / "uploads").mkdir(parents=True)
    for rel in [doc["room"], *(v["image"] for v in doc["variants"])]:
        if not (OUT / rel).exists():
            pytest.skip(f"{rel} missing")
        shutil.copy(OUT / rel, r / rel)
    (tmp_path / "out").mkdir()
    assert any("Änderungsnotiz" in v["note"] for v in doc["variants"][:3])
    doc.update(internal_notes="Interne Notiz (anonymisiert)", notes="Notiz (anonymisiert)", ref="2026-002")
    ids = [v["id"] for v in doc["variants"][:3]]
    version = {"n": 1, "created": "2026-10-18T10:00:00", "variant_ids": ids, "customer_note": "Guten Tag Familie Beispiel, hier die Entwürfe."}
    pages, text = build(doc, r, version=version)
    assert pages == 3 and "Guten Tag Familie Beispiel" in text
    assert "Änderungsnotiz" not in text and "Prüftext" not in text and "18.10.2026" in text
    assert "Vorher" in text and "Nachher 1" in text  # edits show the room photo as "before"


def test_header_ref_version_and_customer_note(root):
    doc = two_piece_doc()
    doc.update(ref="2026-002", ext_ref="A-123", contact={"street": "Hauptstr. 1", "zip": "91522", "city": "Ansbach",
                                                         "phone": "0981 99", "email": "kunde@example.org"})
    version = {"n": 2, "created": "2026-10-18T10:12:00", "variant_ids": ["v2aaaa", "v1aaaa"], "customer_note": "Guten Tag,\nanbei die Entwürfe."}
    pages, text = build(doc, root, version=version)
    assert "Vorgang 2026-002 · zum Angebot Nr. A-123 · Version 2" in text
    assert "Familie Test · Hauptstr. 1, 91522 Ansbach · 18.10.2026" in text
    assert "0981 99" not in text and "kunde@example.org" not in text
    assert "anbei die Entwürfe" in text and "Sockel" not in text
    first = pdf_text(root.parent / "out" / "kundenblatt.pdf", 1, 1)
    assert "anbei die Entwürfe" in first and "Nachher 1" in first  # version order: v2aaaa first, note on page 1
    assert "Maße laut Aufmaß; Preise und Ausführung siehe Angebot." in text and "Datenschutz: Fotos und Daten" in text


def test_approval_block_only_with_version(root):
    doc = two_piece_doc()
    assert "Gewählter Entwurf (keine Auftragserteilung)" not in build(doc, root)[1]
    version = {"n": 1, "created": "2026-10-18T10:00:00", "variant_ids": ["v1aaaa", "v2aaaa"], "customer_note": ""}
    pages, text = build(doc, root, version=version)
    assert "Gewählter Entwurf (keine Auftragserteilung)" in text and "gewählt am" in text and "Unterschrift" in text
    pdf = root.parent / "out" / "kundenblatt.pdf"
    assert "Gewählter Entwurf" in pdf_text(pdf, pages, pages) and "Gewählter Entwurf" not in pdf_text(pdf, 1, 1)
    assert "Online ansehen" not in text and "http" not in text and "/k/" not in text


def test_qr_only_with_public_link(root):
    version = {"n": 1, "created": "2026-10-18T10:00:00", "variant_ids": ["v1aaaa"], "customer_note": ""}
    link = "https://entwurf.schreinerei-holz.de/k/q8Zp1bW0yYbq2m1xN3k9vR6tL4sH7dFc"
    pages, text = build(two_piece_doc(), root, version=version, link=link)
    assert pages == 1 and "Online ansehen und freigeben" in text and link in text.replace("\n", "")


def test_approved_stamp_first_and_no_block(root):
    doc = two_piece_doc()
    doc["approved"] = {"version": 1, "variant_id": "v2aaaa", "at": "2026-10-19T09:00:00", "by": "kunde", "via": "link", "name": "X"}
    version = {"n": 1, "created": "2026-10-18T10:00:00", "variant_ids": ["v1aaaa", "v2aaaa"], "customer_note": ""}
    pages, text = build(doc, root, version=version)
    assert "Freigegebener Entwurf" in text and "Gewählter Entwurf" not in text
    pdf = root.parent / "out" / "kundenblatt.pdf"
    assert "Freigegebener Entwurf" not in pdf_text(pdf, 1, 1) and "Freigegebener Entwurf" in pdf_text(pdf, 2, 2)  # version order kept
    pages, text = build(doc, root)  # draft sheet: approved first
    assert "Freigegebener Entwurf" in pdf_text(pdf, 1, 1) and "Nachher 1" in pdf_text(pdf, 1, 1)
    # default choice without ids: approved first, then the favourites
    chosen, _ = printing.chosen_variants({**doc, "variants": [dict(v, fav=v["id"] == "v1aaaa") for v in doc["variants"]]})
    assert [v["id"] for v in chosen] == ["v2aaaa", "v1aaaa"]


def test_variant_ids_filename_and_nothing_deleted(root):
    out = root.parent / "out"
    (out / "keep.txt").write_text("x")
    skipped = []
    pdf = printing.build_sheet(two_piece_doc(), root, out, skipped, variant_ids=["v2aaaa", "zzzz99"], filename="Visualisierung_X_Entwurf.pdf")
    assert pdf.name == "Visualisierung_X_Entwurf.pdf" and (out / "keep.txt").exists() and skipped == ["zzzz99"]
    text = pdf_text(pdf)
    assert "Nachher" in text and "Nachher 1" not in text  # one page


def test_before_of_chain():
    doc = {"room": "uploads/new.png", "pieces": [{"id": "p1", "sketch": "uploads/s.png"}],
           "variants": [{"id": "a", "kind": "room", "room": "uploads/old.png", "image": "a.png"},
                        {"id": "b", "kind": "material", "from": "a", "image": "b.png"},
                        {"id": "c", "kind": "edit", "from": "b", "image": "c.png"},
                        {"id": "r", "kind": "render", "piece_ids": ["p1"], "image": "r.png"},
                        {"id": "e", "kind": "edit", "from": "r", "image": "e.png"},
                        {"id": "f", "kind": "free", "image": "f.png"},
                        {"id": "x", "kind": "edit", "from": "x", "image": "x.png"}]}
    v = {x["id"]: x for x in doc["variants"]}
    assert printing.before_of(doc, v["c"]) == ("uploads/old.png", "Vorher", "Nachher")
    assert printing.before_of(doc, v["e"]) == ("uploads/s.png", "Skizze", "Entwurf")
    assert printing.before_of(doc, v["f"]) == (None, "", "Entwurf")
    assert printing.before_of(doc, v["x"]) == ("uploads/new.png", "Vorher", "Nachher")  # self-loop terminates
    assert printing.company_line({"street": "Hauptstr. 1", "zip": "91522", "city": "Ansbach", "phone": "0981"}) == "Hauptstr. 1 · 91522 Ansbach · 0981"
    assert printing.company_line({"contact": "Tel. 1", "street": "x"}) == "Tel. 1"
