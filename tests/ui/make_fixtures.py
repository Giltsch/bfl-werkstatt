"""Writes the synthetic UI fixtures (tests/ui/fixtures/) used by the Playwright mock (spec §10.7).

    .venv/bin/python tests/ui/make_fixtures.py

Everything is drawn or invented here: no real customer photo or name ends up in the fixtures. The room is projected
with the camera of tests/fixtures/geometry_sample.json, so 3D placements in the fixture docs line up with the walls.
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
OUT = HERE / "fixtures"
GEO = json.loads((ROOT / "tests/fixtures/geometry_sample.json").read_text())
XSS_NAME, XSS_COMMENT = "<b>Herr X</b>", "<img src=x onerror=alert(1)>"


def catalog() -> dict:
    os.environ.setdefault("FLUX_DATA", tempfile.mkdtemp(prefix="ws-fixtures-"))   # never touch real data dirs
    sys.path.insert(0, str(ROOT))
    import workshop
    cat = copy.deepcopy(workshop.catalog())
    cat.setdefault("quality", {"entwurf": {"label": "Entwurf (schneller)", "steps": 8},
                               "fein": {"label": "Fein (für den Kunden)", "steps": 16}})
    cat.setdefault("common_materials", ["eiche", "eicheweiss", "nuss", "weiss"])
    return cat


def geometry(size) -> dict:
    g = copy.deepcopy(GEO)
    w, h = size
    dy = g["intrinsics"]["cy"] - g["horizon"][0][1]
    g["size"] = [w, h]
    g["intrinsics"].update(cx=w / 2, cy=h / 2)
    g["horizon"] = [[0.0, round(h / 2 - dy, 2)], [float(w), round(h / 2 - dy, 2)]]
    return g


class Cam:
    def __init__(self, g):
        f = g["frame"]
        self.o, self.r, self.u, self.f = f["origin"], f["right"], f["up"], f["forward"]
        self.k = g["intrinsics"]

    def __call__(self, x, y, z):   # floor frame (x right, y up, z forward, cm) -> pixel
        p = [self.o[i] + x * self.r[i] + y * self.u[i] + z * self.f[i] for i in range(3)]
        return (self.k["fx"] * p[0] / p[2] + self.k["cx"], self.k["fy"] * p[1] / p[2] + self.k["cy"])


def quad(cam, pts):
    return [cam(*p) for p in pts]


def draw_room(size) -> tuple[Image.Image, Cam]:
    g = geometry(size)
    cam = Cam(g)
    im = Image.new("RGB", size, "#d8d2c8")
    d = ImageDraw.Draw(im)
    L, R, B, N, H = -200, 250, 400, 60, 260
    d.polygon(quad(cam, [(L, H, N), (R, H, N), (R, H, B), (L, H, B)]), fill="#efece6")              # ceiling
    d.polygon(quad(cam, [(L, 0, N), (L, 0, B), (L, H, B), (L, H, N)]), fill="#d9d1c3")              # left wall
    d.polygon(quad(cam, [(R, 0, N), (R, 0, B), (R, H, B), (R, H, N)]), fill="#cfc6b6")              # right wall
    d.polygon(quad(cam, [(L, 0, B), (R, 0, B), (R, H, B), (L, H, B)]), fill="#e6e0d5")              # back wall
    d.polygon(quad(cam, [(L, 0, N), (R, 0, N), (R, 0, B), (L, 0, B)]), fill="#a77d55")              # floor
    for x in range(L, R, 18):                                                                         # floor boards
        d.line(quad(cam, [(x, 0, N), (x, 0, B)]), fill="#8f6744", width=2)
    for x0, x1 in ((L, R),):
        d.line(quad(cam, [(x0, 0, B), (x1, 0, B)]), fill="#f6f3ee", width=6)                          # skirting
    d.polygon(quad(cam, [(0, 95, B), (120, 95, B), (120, 215, B), (0, 215, B)]), fill="#bcd6ea")     # window
    d.line(quad(cam, [(60, 95, B), (60, 215, B)]), fill="#ffffff", width=6)
    d.line(quad(cam, [(0, 155, B), (120, 155, B)]), fill="#ffffff", width=6)
    d.polygon(quad(cam, [(0, 95, B), (120, 95, B), (120, 215, B), (0, 215, B)]), outline="#ffffff", width=8)
    d.polygon(quad(cam, [(R, 0, 150), (R, 0, 240), (R, 205, 240), (R, 205, 150)]), fill="#f3efe8", outline="#bdb3a3", width=3)  # door
    d.polygon(quad(cam, [(L, 15, 220), (L, 15, 330), (L, 75, 330), (L, 75, 220)]), fill="#f1f1f1", outline="#c9c9c9", width=3)  # radiator
    return im.filter(ImageFilter.GaussianBlur(0.6)), cam


SIDEBOARD = dict(x0=-60, x1=120, z0=355, z1=400, h=75)


def draw_sideboard(im, cam, wood, dark):
    d = ImageDraw.Draw(im)
    s = SIDEBOARD
    d.polygon(quad(cam, [(s["x0"], s["h"], s["z0"]), (s["x1"], s["h"], s["z0"]), (s["x1"], s["h"], s["z1"]), (s["x0"], s["h"], s["z1"])]), fill=dark)
    d.polygon(quad(cam, [(s["x0"], 8, s["z0"]), (s["x1"], 8, s["z0"]), (s["x1"], s["h"], s["z0"]), (s["x0"], s["h"], s["z0"])]), fill=wood)
    d.polygon(quad(cam, [(s["x0"] + 6, 0, s["z0"] + 4), (s["x1"] - 6, 0, s["z0"] + 4), (s["x1"] - 6, 8, s["z0"] + 4), (s["x0"] + 6, 8, s["z0"] + 4)]), fill="#3b332b")
    for k in (1, 2):
        x = s["x0"] + k * (s["x1"] - s["x0"]) / 3
        d.line(quad(cam, [(x, 10, s["z0"]), (x, s["h"] - 2, s["z0"])]), fill=dark, width=3)
    return im


def bbox_grid(cam, size, pts):
    px = [cam(*p) for p in pts]
    w, h = size
    xs, ys = [p[0] for p in px], [p[1] for p in px]
    c = lambda v: max(0, min(1000, round(v)))
    return [c(min(ys) / h * 1000), c(min(xs) / w * 1000), c(max(ys) / h * 1000), c(max(xs) / w * 1000)]


def draw_sketch() -> Image.Image:
    im = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([120, 180, 780, 450], outline="black", width=5)
    d.rectangle([150, 450, 750, 480], outline="black", width=4)
    for x in (340, 560):
        d.line([x, 180, x, 450], fill="black", width=4)
    d.line([120, 120, 780, 120], fill="#555", width=2)
    d.text((420, 92), "180 cm", fill="black")
    d.line([830, 180, 830, 480], fill="#555", width=2)
    d.text((840, 320), "75", fill="black")
    return im


def piece(pid, type_, label, material="eiche", mat_label="Eiche natur geölt", handles="push", h_label="grifflos (Push)",
          dims=(None, None, None), placement=None, text=""):
    return {"id": pid, "type": type_, "text": text, "material": material, "material_custom": "", "handles": handles,
            "label": label, "material_label": mat_label, "handles_label": h_label, "w": dims[0], "h": dims[1], "d": dims[2],
            "sketch": None, "render": None, "sample": None, "use_render": False, "where": "",
            "placement": placement or {"mode": "none", "pose": None, "box": None}}


def verdict(overall, ok, scores):
    keys = ("room_preserved", "matches_request", "proportions", "realism", "artifacts")
    return {"status": "ok", "model": "fake-judge", "scores": dict(zip(keys, scores)), "overall": overall, "pass": ok,
            "comment": "Synthetischer Prüftext.", "fix_hint": "", "seconds": 18.0, "graded": "2026-10-17T15:19:08"}


def variant(vid, image, pieces, judge, *, fav=False, backend="vllm", steps=16, kind="room", seed=101):
    return {"id": vid, "kind": kind, "image": image, "room": "uploads/room.png", "size": [1344, 1008], "seed": seed,
            "job": "f00dfeed01", "fav": fav, "note": "", "from": None, "sketch": None, "upscaled": False, "retry_of": None,
            "piece_ids": [p["id"] for p in pieces], "piece": " + ".join(p["label"] for p in pieces),
            "material": pieces[0]["material_label"], "handles": pieces[0]["handles_label"],
            "pieces": [{"id": p["id"], "label": p["label"], "material_label": p["material_label"],
                        "handles_label": p["handles_label"], "w": p["w"], "h": p["h"], "d": p["d"]} for p in pieces],
            "boxes": {}, "judge": judge, "by": "peter", "caption_backend": backend, "steps": steps,
            "active_piece": pieces[0]["id"], "locked": False}


def base_doc(pid, **kw):
    doc = {"id": pid, "version": 3, "rev": 1, "frev_floor": 0, "frev": {}, "created": "2026-10-15T09:12:00",
           "created_by": "peter", "updated": "2026-10-17T16:40:00", "updated_by": "peter", "title": "", "customer": "",
           "test": False, "status": "anfrage", "ref": "", "ext_ref": "",
           "contact": {"phone": "", "email": "", "street": "", "zip": "", "city": ""}, "appointment": None,
           "internal_notes": "", "archived_at": None, "quality": "entwurf", "res": 1536, "count": 2,
           "judge": {"enabled": True, "auto_retry": False}, "versions": [], "approved": None, "trash": [],
           "activity": [{"at": "2026-10-15T09:12:00", "by": "peter", "what": "created", "text": "Auftrag angelegt"}],
           "thumb": None, "company": {}, "setting": "", "room_changes": "", "room": None, "sample": None,
           "room_size": None, "tidy": True, "protect": True, "guide": False, "removes": [], "geometry": None,
           "calibration": None, "pieces": [], "deleted_variants": [], "pending_jobs": [], "variants": []}
    doc.update(kw)
    return doc


def summary(doc, **kw):
    c = doc["contact"]
    q = " ".join(str(x) for x in (doc["title"], doc["customer"], doc["ref"], doc["ext_ref"], *c.values(),
                                  doc["internal_notes"][:500]) if x).lower()
    s = {"id": doc["id"], "title": doc["title"] or "Ohne Titel", "customer": doc["customer"], "status": doc["status"],
         "ref": doc["ref"], "ext_ref": doc["ext_ref"], "city": c["city"], "appointment": doc["appointment"],
         "archived_at": doc["archived_at"], "test": doc["test"], "retention_due": False, "created_by": doc["created_by"],
         "thumb": doc["thumb"], "updated": doc["updated"], "updated_by": doc["updated_by"],
         "updated_by_name": {"peter": "Peter", "ben": "Ben"}.get(doc["updated_by"], doc["updated_by"]),
         "variants": len(doc["variants"]), "versions": len(doc["versions"]), "approved": bool(doc["approved"]),
         "attention": None, "pending": len(doc["pending_jobs"]), "rev": doc["rev"], "q": q}
    s.update(kw)
    return s


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    land, cam = draw_room((1344, 1008))
    land.save(OUT / "room.png")
    port, pcam = draw_room((1008, 1344))
    port.save(OUT / "room-portrait.png")
    for name, wood, dark in (("variant-1.jpg", "#c9a273", "#9c7a50"), ("variant-2.jpg", "#6e4b33", "#4a311f"),
                             ("variant-3.jpg", "#f2f0ec", "#cfcac2")):
        draw_sideboard(land.copy(), cam, wood, dark).save(OUT / name, quality=88)
    draw_sketch().save(OUT / "sketch.png")
    cat = catalog()
    (OUT / "catalog.json").write_text(json.dumps(cat, ensure_ascii=False, indent=1))
    g_land, g_port = geometry((1344, 1008)), geometry((1008, 1344))
    g_land["room"], g_port["room"] = "uploads/room.png", "uploads/room-portrait.png"
    (OUT / "geometry.json").write_text(json.dumps(g_land, indent=1))
    (OUT / "geometry-portrait.json").write_text(json.dumps(g_port, indent=1))

    s = SIDEBOARD
    sb_box = bbox_grid(cam, (1344, 1008), [(x, y, z) for x in (s["x0"], s["x1"]) for y in (0, s["h"]) for z in (s["z0"], s["z1"])])
    p1 = piece("p1aaaa", "sideboard", "Sideboard", dims=(180, 75, 45),
               placement={"mode": "3d", "pose": {"x": 30.0, "z": 377.5, "yaw": 0.0, "wall": "w0"}, "box": sb_box})
    p2 = piece("p2bbbb", "regal", "Regalwand", "nuss", "Nussbaum", "leiste", "Griffleiste", dims=(120, 220, 35),
               placement={"mode": "2d", "pose": None, "box": [150, 40, 820, 230]})
    va = variant("va111111", "variant-1.jpg", [p1, p2], verdict(8.5, True, [9, 8, 9, 8, 9]), fav=True, seed=111)
    vb = variant("vb222222", "variant-2.jpg", [p1, p2], verdict(5.7, False, [8, 5, 6, 6, 5]), backend="template", seed=222)
    vc = variant("vc333333", "variant-3.jpg", [p1, p2], verdict(3.2, False, [4, 3, 3, 4, 2]), steps=8, seed=333)
    snap_rows = [{"piece": p["label"], "material": p["material_label"], "handles": p["handles_label"],
                  "dims": f'{p["w"]} × {p["h"]} × {p["d"]} cm'} for p in (p1, p2)]
    version = {"n": 1, "created": "2026-10-16T10:12:00", "by": "peter", "variant_ids": ["va111111", "vb222222"],
               "customer_note": "Guten Tag Familie Beispiel,\nanbei unsere Entwürfe.", "status": "aenderung",
               "token": "tokFixture0000000000000000000001", "expires": "2027-01-14", "viewed_at": "2026-10-16T19:40:11",
               "response": {"variant_id": "va111111", "action": "aenderung", "name": XSS_NAME, "comment": XSS_COMMENT,
                            "at": "2026-10-16T19:44:00", "via": "link"},
               "dir": "exports/workshop-a1b2c3d4e5f6/v1",
               "files": {"pdf": "Visualisierung_2026-002_Beispiel_V1.pdf", "zip": "Visualisierung_2026-002_Beispiel_V1.zip",
                         "logo": None, "images": ["01_Vorher.jpg", "02_Entwurf-1_Eiche.jpg", "03_Entwurf-2_Nussbaum.jpg"]},
               "snapshot": {"title": "Esszimmer Sideboard", "customer": "Familie Beispiel", "ref": "2026-002", "ext_ref": "A-1043",
                            "date": "2026-10-16", "company": {"name": "Schreinerei Muster", "contact": "", "color": "#2f6b4f"},
                            "variants": [{"n": 1, "id": "va111111", "before": "01_Vorher.jpg", "after": "02_Entwurf-1_Eiche.jpg",
                                          "before_label": "Vorher", "after_label": "Nachher", "rows": snap_rows},
                                         {"n": 2, "id": "vb222222", "before": "01_Vorher.jpg", "after": "03_Entwurf-2_Nussbaum.jpg",
                                          "before_label": "Vorher", "after_label": "Nachher", "rows": snap_rows}]}}
    va["locked"] = vb["locked"] = True
    full = base_doc("a1b2c3d4e5f6", rev=12, title="Esszimmer Sideboard", customer="Familie Beispiel", status="kunde",
                    ref="2026-002", ext_ref="A-1043", updated_by="ben",
                    contact={"phone": "0981 123456", "email": "beispiel@example.org", "street": "Musterweg 3", "zip": "91522", "city": "Ansbach"},
                    appointment={"at": "2026-10-20T09:00", "what": "Präsentation"},
                    internal_notes="Kunde möchte eher dunkles Holz. Parkplatz im Hof.", quality="fein",
                    judge={"enabled": True, "auto_retry": True}, versions=[version], thumb="variant-1.jpg",
                    room="uploads/room.png", room_size=[1344, 1008], geometry=g_land, pieces=[p1, p2], variants=[va, vb, vc])
    full["activity"] += [
        {"at": "2026-10-15T09:30:00", "by": "peter", "what": "photo", "text": "Raumfoto hinzugefügt"},
        {"at": "2026-10-15T09:31:00", "by": "system", "what": "status", "text": "Status: Anfrage → Vor-Ort-Termin"},
        {"at": "2026-10-15T10:02:00", "by": "peter", "what": "generated", "text": "3 Vorschläge berechnet"},
        {"at": "2026-10-16T10:12:00", "by": "peter", "what": "version_sent", "text": "Version 1 an den Kunden gegeben (2 Entwürfe)"},
        {"at": "2026-10-16T19:40:11", "by": "kunde", "what": "viewed", "text": "Kunde hat Version 1 angesehen"},
        {"at": "2026-10-16T19:44:00", "by": "kunde", "what": "change_request",
         "text": f"Änderungswunsch zu Version 1 ({XSS_NAME}): „{XSS_COMMENT}“"}]
    new = base_doc("0e1e2e3e4e5e", title="", customer="Familie Neu", ref="2026-004", created="2026-10-17T16:00:00",
                   updated="2026-10-17T16:00:00", contact={"phone": "0981 7654", "email": "", "street": "", "zip": "", "city": "Feuchtwangen"},
                   pieces=[piece("n1cccc", None, "", handles="push")])
    portrait = base_doc("9f8e7d6c5b4a", rev=4, title="Flur Garderobe", customer="Herr Hoch", status="termin", ref="2026-003",
                        room="uploads/room-portrait.png", room_size=[1008, 1344], geometry=g_port,
                        contact={"phone": "", "email": "", "street": "", "zip": "", "city": "Dinkelsbühl"},
                        pieces=[piece("h1dddd", "sideboard", "Sideboard", dims=(180, 75, 45))])
    for name, d in (("doc-full.json", full), ("doc-new.json", new), ("doc-portrait.json", portrait)):
        (OUT / name).write_text(json.dumps(d, ensure_ascii=False, indent=1))

    today = "2026-10-17T14:00"
    extra = [
        summary(base_doc("b0b0b0b0b0b1", title="Küche Huber", customer="Huber", status="entwurf", ref="2026-001",
                         updated_by="ben", thumb="variant-2.jpg", updated="2026-10-17T11:02:00"), variants=4),
        summary(base_doc("c0c0c0c0c0c2", title="Einbauschrank Flur", customer="Familie Müller", status="anfrage", ref="2026-005",
                         appointment={"at": today, "what": "Aufmaß"}), attention="termin_heute"),
        summary(base_doc("d0d0d0d0d0d3", title="Badmöbel", customer="Schmidt", status="freigegeben", ref="2025-031",
                         thumb="variant-3.jpg", updated="2026-10-12T08:00:00"), attention="freigegeben", versions=1, approved=True),
        summary(base_doc("e0e0e0e0e0e4", title="Treppe", customer="Weber", status="abgesagt", ref="2025-019",
                         updated="2026-03-01T08:00:00"), retention_due=True),
        summary(base_doc("f0f0f0f0f0f5", title="TEST Kunde", customer="Test", test=True)),
        summary(base_doc("a0a0a0a0a0a6", title="Garderobe alt", customer="Klein", status="fertig", ref="2025-002",
                         archived_at="2026-01-10T10:00:00", updated="2026-01-10T10:00:00")),
    ]
    lst = [summary(full, attention="aenderung"), summary(portrait), summary(new)] + extra
    (OUT / "list.json").write_text(json.dumps(lst, ensure_ascii=False, indent=1))
    team = [{"id": "peter", "name": "Peter", "color": "#59a7ff"}, {"id": "ben", "name": "Ben", "color": "#7cc4a0"}]
    settings = {"company": {"name": "Schreinerei Muster", "contact": "", "street": "Hauptstr. 1", "zip": "91522", "city": "Ansbach",
                            "phone": "0981 123456", "email": "info@example.org", "web": "", "color": "#2f6b4f", "logo": None},
                "team": team, "access": {"password_set": False}, "retention": {"abgesagt_months": 6, "fertig_months": 12},
                "public_base_url": "", "trusted_proxy": False,
                "backup": {"target": "/home/werkstatt/bfl-backups", "keep": 14}}
    (OUT / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=1))
    me = {"user": team[0], "device": "8f3a1c2e4b5d6f70", "auth": {"required": False, "ok": True}, "team": team}
    (OUT / "me.json").write_text(json.dumps(me, ensure_ascii=False, indent=1))
    print("fixtures written to", OUT)


if __name__ == "__main__":
    main()
