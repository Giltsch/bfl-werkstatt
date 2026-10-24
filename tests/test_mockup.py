import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

import mockup
from test_workshop import GEO


def test_parts_follow_type_and_details():
    doors = [b for b in mockup.parts({"type": "schrank", "text": "3 Türen, links offen"}, (180, 240, 60)) if b[6] == "front"]
    assert len(doors) == 3 and min(b[0] for b in doors) >= -90 and max(b[1] for b in doors) <= 90
    assert all(b[5] <= -30 + 1.8 + 1e-9 for b in doors)                     # fronts sit on the front face (z = -D/2)
    drawers = [b for b in mockup.parts({"type": "sideboard", "text": "drei Schubladen"}, (180, 75, 45)) if b[6] == "front"]
    assert len(drawers) == 3 and len({b[2] for b in drawers}) == 3          # stacked: horizontal fronts
    shelf = mockup.parts({"type": "wandregal", "elev": 150}, (100, 40, 25))
    assert min(b[2] for b in shelf) == 150 and max(b[3] for b in shelf) == 190
    legs = [b for b in mockup.parts({"type": "tisch"}, (200, 75, 100)) if b[6] == "body"]
    assert len(legs) == 4


def test_render_draws_into_the_photo_and_keeps_the_rest():
    photo = Image.new("RGB", (1344, 1008), (90, 120, 150))
    piece = {"type": "sideboard", "text": ""}
    out = np.asarray(mockup.render(photo, GEO, [(piece, (180, 75, 45), {"x": 0, "z": 300, "yaw": 0}, (200, 160, 110))]))
    changed = (out != (90, 120, 150)).any(-1)
    assert 0.01 < changed.mean() < 0.3                                       # the piece is drawn, most of the photo is kept
    assert not changed[:200].any()                                           # a sideboard on the floor stays low in the frame


def test_nearer_room_hides_the_piece_unless_it_would_hide_most_of_it():
    photo = Image.new("RGB", (1344, 1008), (90, 120, 150))
    item = [({"type": "sideboard"}, (180, 75, 45), {"x": 0, "z": 300, "yaw": 0}, (200, 160, 110))]
    near = np.full((252, 336), 100.0, np.float32)                            # everything 1 m away: a wall in front
    base = (np.asarray(mockup.render(photo, GEO, item)) != (90, 120, 150)).any(-1).sum()
    hidden = (np.asarray(mockup.render(photo, GEO, item, near)) != (90, 120, 150)).any(-1).sum()
    assert hidden == base                                                    # fully covered = it replaces what is there
    part = np.full((252, 336), 1000.0, np.float32)
    part[:, :120] = 100.0                                                    # something near on the left third only
    some = (np.asarray(mockup.render(photo, GEO, item, part)) != (90, 120, 150)).any(-1).sum()
    assert some < base
