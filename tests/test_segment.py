import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import projects
import segment
import workshop as ws


def test_outline_keeps_every_large_part_and_grows_it():
    m = np.zeros((400, 600), bool)
    m[100:300, 50:250] = True          # the table left of a chair …
    m[120:280, 330:420] = True         # … and its right part behind it
    m[10:12, 590:592] = True           # a speck: dropped
    parts = segment.outline(m)
    assert len(parts) == 2
    t, l, b, r = parts[0]["box"]
    assert t < 250 and l < 84 and b > 750 and r > 416                        # grown a little beyond the mask
    assert all(3 <= len(p["poly"]) <= segment.MAX_POINTS for p in parts)
    assert segment.outline(np.zeros((10, 10), bool)) == []


def test_removes_accept_frames_and_outlines():
    poly = {"poly": [[100, 100], [300, 120], [200, 400]], "box": [100, 100, 400, 300]}
    assert projects._remove([10, 20, 300, 400]) == [10, 20, 300, 400]
    assert projects._remove(poly) == {"poly": [[100.0, 100.0], [300.0, 120.0], [200.0, 400.0]], "box": [100, 100, 400, 300]}
    assert projects._remove({"poly": [[1, 2]], "box": [1, 2, 3, 4]}) is None
    doc = {"removes": [[10, 20, 300, 400], poly]}
    regs = ws._removes(doc)
    assert regs == [[10, 20, 300, 400], {"poly": poly["poly"]}] and ws._rbox(regs[1]) == [100, 100, 400, 300]
