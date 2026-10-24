import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import export3d


def test_carcass_becomes_panels_with_exact_sizes():
    rows = export3d.cut_list({"type": "schrank", "text": "3 Türen"}, (180, 240, 60)).splitlines()
    by = {r.split(";")[0]: r.split(";") for r in rows[1:]}
    assert by["Front"][1] == "3" and by["Seite links"][2:] == ["2320", "582", "19"]   # 240 cm minus 8 cm plinth
    assert by["Rückwand"][4] == "8" and int(by["Fachboden"][1]) >= 3


def test_glb_is_valid_metres_with_front_to_plus_z():
    g = export3d.glb({"type": "sideboard", "material": "eiche"}, (180, 75, 45))
    magic, ver, total = struct.unpack("<III", g[:12])
    assert magic == 0x46546C67 and ver == 2 and total == len(g)
    n, kind = struct.unpack("<II", g[12:20])
    doc = json.loads(g[20:20 + n])
    lo = [min(a["min"][i] for a in doc["accessors"] if "min" in a) for i in range(3)]
    hi = [max(a["max"][i] for a in doc["accessors"] if "max" in a) for i in range(3)]
    assert abs(hi[0] - lo[0] - 1.82) < 0.011 and abs(hi[1] - 0.75) < 1e-4 and lo[1] == 0.0    # 180 cm + top overhang
    assert hi[2] > 0.2                                                                          # fronts on the +Z side


def test_stl_scale_and_triangle_count():
    p = {"type": "tisch"}
    full, mini = export3d.stl(p, (200, 75, 100)), export3d.stl(p, (200, 75, 100), 10)
    n = struct.unpack("<I", full[80:84])[0]
    assert n == 5 * 12 and len(full) == 84 + 50 * n                                            # top + 4 legs
    zs = [struct.unpack("<f", full[84 + 12 + 50 * i + 8 + 12 * k: 84 + 12 + 50 * i + 12 + 12 * k])[0] for i in range(n) for k in range(3)]
    assert abs(max(zs) - 750) < 0.01
    zs10 = [struct.unpack("<f", mini[84 + 12 + 50 * i + 8 + 12 * k: 84 + 12 + 50 * i + 12 + 12 * k])[0] for i in range(n) for k in range(3)]
    assert abs(max(zs10) - 75) < 0.01
