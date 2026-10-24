"""2D workshop drawing of planned pieces as DXF R12 (AC1009, ASCII), written by hand: front view and top view of the
export3d panels with overall dimensions, millimetres 1:1, first-angle layout (Draufsicht below the Vorderansicht).

    piece_dxf(piece, dims, title="", lines=()) -> bytes      one piece
    drawing([(piece, dims, caption), ...], title, lines) -> bytes   several pieces side by side

Only R12 entities (LINE, TEXT) and tables (LTYPE, LAYER, STYLE) are written, so every CAD program reads the file
(AutoCAD, LibreCAD, DraftSight, QCAD, the CNC software of the shop). The file is pure ASCII: umlauts and "×" are written
as \\U+XXXX escapes, because readers disagree about the code page of R12 files (LibreOffice ignores $DWGCODEPAGE and
shows cp1252 bytes as DOS characters; the escapes render right in LibreOffice, AutoCAD, LibreCAD and ezdxf)."""

import re

import export3d

MM = 10.0  # export3d works in cm
LAYERS = {   # name → (ACI colour, linetype)
    "KONTUR": (7, "CONTINUOUS"),
    "KORPUS": (7, "CONTINUOUS"),
    "FRONTEN": (3, "CONTINUOUS"),
    "FACHBOEDEN": (5, "CONTINUOUS"),
    "SOCKEL_RUECKWAND": (8, "CONTINUOUS"),
    "DECKPLATTE": (30, "CONTINUOUS"),
    "POLSTER": (6, "CONTINUOUS"),
    "VERDECKT": (8, "DASHED"),
    "BEMASSUNG": (4, "CONTINUOUS"),
    "TEXT": (7, "CONTINUOUS"),
}
TONE_LAYER = {"body": "KORPUS", "front": "FRONTEN", "board": "FACHBOEDEN", "dark": "SOCKEL_RUECKWAND",
              "top": "DECKPLATTE", "soft": "POLSTER"}
SAMPLES = 6  # coverage test grid per panel (hidden-line approximation)


def escape(s: str) -> str:
    """Non-ASCII → \\U+XXXX (DXF unicode escape); characters beyond the BMP become "?"."""
    return "".join(c if 32 <= ord(c) < 127 else f"\\U+{ord(c):04X}" if 127 <= ord(c) <= 0xFFFF else "?" for c in s)


def unescape(s: str) -> str:
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)


def _num(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class _Writer:
    def __init__(self):
        self.ents: list[str] = []
        self.lo = [float("inf")] * 2
        self.hi = [float("-inf")] * 2
        self._seen: set = set()

    def _ext(self, x, y):
        self.lo = [min(self.lo[0], x), min(self.lo[1], y)]
        self.hi = [max(self.hi[0], x), max(self.hi[1], y)]

    def line(self, layer, x1, y1, x2, y2):
        a, b = (round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))
        key = (layer, min(a, b), max(a, b))
        if a == b or key in self._seen:   # coinciding panel edges are written once
            return
        self._seen.add(key)
        self._ext(x1, y1)
        self._ext(x2, y2)
        self.ents.append(f"0\nLINE\n8\n{layer}\n10\n{_num(x1)}\n20\n{_num(y1)}\n30\n0\n11\n{_num(x2)}\n21\n{_num(y2)}\n31\n0")

    def rect(self, layer, x0, y0, x1, y1):
        self.line(layer, x0, y0, x1, y0)
        self.line(layer, x1, y0, x1, y1)
        self.line(layer, x1, y1, x0, y1)
        self.line(layer, x0, y1, x0, y0)

    def text(self, layer, x, y, h, s, align="left", angle=0.0):
        s = escape(" ".join(str(s).split()))   # no newlines or control characters inside a group value
        if not s:
            return
        self._ext(x, y)
        self._ext(x + (len(s) * h * 0.7 if align == "left" and not angle else 0), y + h)
        g = f"0\nTEXT\n8\n{layer}\n10\n{_num(x)}\n20\n{_num(y)}\n30\n0\n40\n{_num(h)}\n1\n{s}"
        if angle:
            g += f"\n50\n{_num(angle)}"
        if align == "center":   # 72 = 1 centred on the alignment point 11/21 (R12 needs both points)
            g += f"\n72\n1\n11\n{_num(x)}\n21\n{_num(y)}\n31\n0"
        self.ents.append(g)

    def document(self) -> bytes:
        lo = [0.0, 0.0] if self.lo[0] == float("inf") else self.lo
        hi = [0.0, 0.0] if self.hi[0] == float("-inf") else self.hi
        out = ["0\nSECTION\n2\nHEADER",
               "9\n$ACADVER\n1\nAC1009", "9\n$DWGCODEPAGE\n3\nANSI_1252",
               "9\n$INSBASE\n10\n0\n20\n0\n30\n0",
               f"9\n$EXTMIN\n10\n{_num(lo[0])}\n20\n{_num(lo[1])}\n30\n0",
               f"9\n$EXTMAX\n10\n{_num(hi[0])}\n20\n{_num(hi[1])}\n30\n0",
               f"9\n$LIMMIN\n10\n{_num(lo[0])}\n20\n{_num(lo[1])}",
               f"9\n$LIMMAX\n10\n{_num(hi[0])}\n20\n{_num(hi[1])}",
               "9\n$LUNITS\n70\n2", "9\n$LUPREC\n70\n0", "9\n$TEXTSTYLE\n7\nSTANDARD", "9\n$CLAYER\n8\n0",
               "0\nENDSEC",
               "0\nSECTION\n2\nTABLES",
               "0\nTABLE\n2\nLTYPE\n70\n2",
               "0\nLTYPE\n2\nCONTINUOUS\n70\n0\n3\nSolid line\n72\n65\n73\n0\n40\n0",
               "0\nLTYPE\n2\nDASHED\n70\n0\n3\n__ __ __ __\n72\n65\n73\n2\n40\n15\n49\n10\n49\n-5",
               "0\nENDTAB",
               f"0\nTABLE\n2\nLAYER\n70\n{len(LAYERS) + 1}",
               "0\nLAYER\n2\n0\n70\n0\n62\n7\n6\nCONTINUOUS"]
        out += [f"0\nLAYER\n2\n{n}\n70\n0\n62\n{c}\n6\n{lt}" for n, (c, lt) in LAYERS.items()]
        out += ["0\nENDTAB",
                "0\nTABLE\n2\nSTYLE\n70\n1",
                "0\nSTYLE\n2\nSTANDARD\n70\n0\n40\n0\n41\n1\n50\n0\n71\n0\n42\n2.5\n3\ntxt\n4\n",
                "0\nENDTAB",
                "0\nENDSEC",
                "0\nSECTION\n2\nBLOCKS\n0\nENDSEC",
                "0\nSECTION\n2\nENTITIES", *self.ents, "0\nENDSEC", "0\nEOF"]
        return ("\r\n".join("\r\n".join(g.split("\n")) for g in out) + "\r\n").encode("ascii")


def _covered(r, nearer) -> bool:
    """Whether the rectangle r = (x0, y0, x1, y1) lies behind the union of the nearer rectangles (sampled on a grid)."""
    x0, y0, x1, y1 = r
    for i in range(SAMPLES):
        for j in range(SAMPLES):
            x = x0 + (x1 - x0) * (i + 0.5) / SAMPLES
            y = y0 + (y1 - y0) * (j + 0.5) / SAMPLES
            if not any(a[0] - 1e-6 <= x <= a[2] + 1e-6 and a[1] - 1e-6 <= y <= a[3] + 1e-6 for a in nearer):
                return False
    return True


def _view(w: _Writer, rects):
    """rects: (x0, y0, x1, y1, depth, layer), depth = distance from the viewer. Visible panels on their tone layer,
    panels completely behind nearer ones dashed on VERDECKT (switch that layer off for a clean view)."""
    rects = sorted(rects, key=lambda r: r[4])
    for k, r in enumerate(rects):
        nearer = [q[:4] for q in rects[:k] if q[4] < r[4] - 1e-6]
        w.rect("VERDECKT" if nearer and _covered(r[:4], nearer) else r[5], *r[:4])


def _hdim(w, x0, x1, y_obj, y_line, h, label=None):
    """Horizontal dimension below an object edge at y_obj: extension lines, line with oblique ticks, centred value."""
    t = h * 0.5
    for x in (x0, x1):
        w.line("BEMASSUNG", x, y_obj - h * 0.3, x, y_line - h * 0.4)
        w.line("BEMASSUNG", x - t, y_line - t, x + t, y_line + t)
    w.line("BEMASSUNG", x0 - h * 0.4, y_line, x1 + h * 0.4, y_line)
    w.text("BEMASSUNG", (x0 + x1) / 2, y_line + h * 0.3, h, label or _num(x1 - x0), "center")


def _vdim(w, y0, y1, x_obj, x_line, h, label=None):
    """Vertical dimension left of an object edge at x_obj; the value reads from the right (rotated 90°)."""
    t = h * 0.5
    for y in (y0, y1):
        w.line("BEMASSUNG", x_obj - h * 0.3, y, x_line - h * 0.4, y)
        w.line("BEMASSUNG", x_line - t, y - t, x_line + t, y + t)
    w.line("BEMASSUNG", x_line, y0 - h * 0.4, x_line, y1 + h * 0.4)
    w.text("BEMASSUNG", x_line - h * 0.3, (y0 + y1) / 2, h, label or _num(y1 - y0), "center", 90.0)


def _piece(w: _Writer, piece: dict, dims, ox: float, caption: str, h: float) -> float:
    """Front view with its baseline on y = 0 and the top view below it, the piece's left edge at x = ox (mm).
    Returns the right edge of everything drawn (for the next piece)."""
    W, H, D = (float(v) * MM for v in dims)
    elev = float(piece.get("elev") or 0) * MM
    panels = export3d.panels(piece, dims)
    shift = ox + W / 2   # piece frame x = 0 is the middle of the width
    xs = [shift + p[0] * MM for p in panels] + [shift + p[1] * MM for p in panels]
    lo, hi = min(xs + [ox]), max(xs + [ox + W])
    # Vorderansicht: x across, y up; nearer = smaller z (the front is at z = -D/2)
    _view(w, [(shift + p[0] * MM, p[2] * MM, shift + p[1] * MM, p[3] * MM, p[4] * MM, TONE_LAYER.get(p[6], "KORPUS"))
              for p in panels])
    top = max([p[3] * MM for p in panels] + [elev + H])
    if elev > 0:   # wall-hung: the floor line and the mounting height
        w.line("KONTUR", lo - 2 * h, 0, hi + 2 * h, 0)
        w.text("TEXT", hi + 2.4 * h, -h * 0.4, h * 0.8, "OKFF")
        _vdim(w, 0, elev, ox, ox - 5 * h, h)
    _vdim(w, elev, elev + H, ox, ox - 2.5 * h, h)
    _hdim(w, ox, ox + W, min(0.0, elev), min(0.0, elev) - 3 * h, h)
    w.text("TEXT", ox, top + 1.5 * h, h * 1.2, f"{caption} – Vorderansicht" if caption else "Vorderansicht")
    # Draufsicht (first angle: below, front edge at the bottom): y = base + z + D/2, nearer = higher top surface
    base = min(0.0, elev) - 8 * h - D
    _view(w, [(shift + p[0] * MM, base + p[4] * MM + D / 2, shift + p[1] * MM, base + p[5] * MM + D / 2, -p[3] * MM,
               TONE_LAYER.get(p[6], "KORPUS")) for p in panels])
    _vdim(w, base, base + D, ox, ox - 2.5 * h, h)
    _hdim(w, ox, ox + W, base, base - 3 * h, h)
    w.text("TEXT", ox, base + D + 1.2 * h, h * 1.2, "Draufsicht")
    return hi


def drawing(items, title: str = "", lines=()) -> bytes:
    """items: [(piece, (w, h, d) in cm, caption)]; pieces stand side by side with their own views and dimensions.
    title and lines (e.g. order, material) go into a text block under the drawing."""
    items = [it for it in items if it and it[0] and it[0].get("type")]
    w = _Writer()
    size = max([max(float(v) for v in it[1]) * MM for it in items] + [500.0])
    h = max(20.0, min(80.0, round(size / 50 / 5) * 5))   # text height: ~2 % of the biggest piece
    x = 0.0
    lowest = 0.0
    for piece, dims, caption in items:
        right = _piece(w, piece, dims, x, caption, h)
        lowest = min(lowest, min(0.0, float(piece.get("elev") or 0) * MM) - 11 * h - float(dims[2]) * MM)
        x = right + 10 * h
    y = lowest - 2 * h
    for i, s in enumerate([title, *lines] if title else list(lines)):
        if s:
            w.text("TEXT", 0, y, h * (1.3 if i == 0 and title else 0.9), s)
            y -= h * 1.8
    return w.document()


def piece_dxf(piece: dict, dims, title: str = "", lines=()) -> bytes:
    return drawing([(piece, dims, piece.get("label") or "")], title, lines)


def parse(data: bytes) -> list[tuple[int, str]]:
    """(group code, value) pairs of a DXF file — for tests and sanity checks of what we write."""
    rows = data.decode("ascii").splitlines()
    if len(rows) % 2:
        raise ValueError("odd number of lines")
    return [(int(rows[i].strip()), rows[i + 1]) for i in range(0, len(rows), 2)]
