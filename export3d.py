"""3D files of a planned piece, built from its measured dimensions (no AI): the parts of mockup.parts with every carcass
block opened up into real panels (sides, top, bottom, back, shelves, 19 mm board, 8 mm back), exact to the millimetre.

    glb(piece, dims)  → bytes   metres, Y up, front facing +Z (model-viewer, AR on the customer's phone)
    stl(piece, dims, scale) → bytes   millimetres, 1:scale (3D print miniature or CAD import)
    cut_list(piece, dims) → str   CSV of the panels (Zuschnittliste)"""

import io
import json
import struct

import numpy as np

import mockup

BOARD, BACK = 1.9, 0.8   # cm
NAMES = {"front": "Front", "top": "Deckplatte", "board": "Boden/Fachboden", "dark": "Sockel/Rückwand", "soft": "Polster",
         "body": "Korpus"}


def panels(piece: dict, dims) -> list[tuple]:
    """(x0, x1, y0, y1, z0, z1, tone, name) in cm, piece frame of mockup.parts (front at z = -D/2)."""
    out = []
    shelves_in = piece.get("type") in ("schrank", "garderobe", "haengeschrank", "sideboard", "kueche")
    for b in mockup.parts(piece, dims):
        x0, x1, y0, y1, z0, z1, tone = b
        if tone != "body" or piece.get("type") in ("tisch", "treppe", "bett", "tuer", "wand") or min(x1 - x0, y1 - y0, z1 - z0) < 3 * BOARD:
            out.append((*b, NAMES.get(tone, "Teil")))
            continue
        t = BOARD   # a carcass block → five panels and shelves behind the fronts
        out += [(x0, x0 + t, y0, y1, z0, z1, "body", "Seite links"), (x1 - t, x1, y0, y1, z0, z1, "body", "Seite rechts"),
                (x0 + t, x1 - t, y1 - t, y1, z0, z1 - BACK, "body", "Deckel"), (x0 + t, x1 - t, y0, y0 + t, z0, z1 - BACK, "body", "Boden"),
                (x0, x1, y0, y1, z1 - BACK, z1, "dark", "Rückwand")]
        h = y1 - y0
        if shelves_in and h > 60:
            n = max(1, round(h / 40) - 1)
            for i in range(1, n + 1):
                y = y0 + h * i / (n + 1)
                out.append((x0 + t, x1 - t, y - t / 2, y + t / 2, z0 + 2, z1 - BACK, "board", "Fachboden"))
    return out


def _box_mesh(b, flip_z=True):
    """24 vertices with flat normals and 12 triangles of one block (cm)."""
    x0, x1, y0, y1, z0, z1 = b[:6]
    if flip_z:   # glTF looks from +Z: the front (-z in the piece frame) must face +Z
        z0, z1 = -z1, -z0
    faces = [((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1)),
             ((x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (0, 0, -1)),
             ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (-1, 0, 0)),
             ((x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (1, 0, 0)),
             ((x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0), (0, 1, 0)),
             ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0))]
    pos, nor, idx = [], [], []
    for f in faces:
        k = len(pos)
        pos += f[:4]
        nor += [f[4]] * 4
        idx += [k, k + 1, k + 2, k, k + 2, k + 3]
    return np.array(pos, np.float32), np.array(nor, np.float32), np.array(idx, np.uint32)


def _rgb(piece):
    return mockup.WOOD.get(piece.get("material"), (200, 170, 130))


def glb(piece: dict, dims) -> bytes:
    """Binary glTF, metres, one primitive per tone (wood shades from the chosen material)."""
    base = np.array(_rgb(piece), np.float32) / 255.0
    groups = {}
    for p in panels(piece, dims):
        groups.setdefault(p[6], []).append(p)
    bin_, views, accs, prims, mats = bytearray(), [], [], [], []

    def add(arr, target, comp, typ, minmax=False):
        while len(bin_) % 4:
            bin_.append(0)
        views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": arr.nbytes, "target": target})
        bin_.extend(arr.tobytes())
        a = {"bufferView": len(views) - 1, "componentType": comp, "count": len(arr), "type": typ}
        if minmax:
            a.update(min=arr.min(0).tolist(), max=arr.max(0).tolist())
        accs.append(a)
        return len(accs) - 1

    for tone, blocks in groups.items():
        P, N, I = [], [], []
        for b in blocks:
            p, n, i = _box_mesh(b)
            I.append(i + sum(len(x) for x in P))
            P.append(p / 100.0)   # cm → m
            N.append(n)
        P, N, I = np.concatenate(P), np.concatenate(N), np.concatenate(I)
        c = np.clip(base * mockup.TONES.get(tone, 1.0), 0, 1)
        mats.append({"name": NAMES.get(tone, tone), "pbrMetallicRoughness": {
            "baseColorFactor": [float(c[0] ** 2.2), float(c[1] ** 2.2), float(c[2] ** 2.2), 1.0],   # sRGB → linear
            "metallicFactor": 0.0, "roughnessFactor": 0.7}})
        prims.append({"attributes": {"POSITION": add(P, 34962, 5126, "VEC3", True), "NORMAL": add(N, 34962, 5126, "VEC3")},
                      "indices": add(I, 34963, 5125, "SCALAR"), "material": len(mats) - 1})
    doc = {"asset": {"version": "2.0", "generator": "bfl Werkstatt"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0, "name": piece.get("label") or "Möbel"}], "meshes": [{"primitives": prims}],
           "materials": mats, "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(bin_)}]}
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    bin_ += b"\0" * (-len(bin_) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(bin_), 0x004E4942) + bytes(bin_))


def stl(piece: dict, dims, scale: float = 1.0) -> bytes:
    """Binary STL in millimetres at 1:scale; Z up (3D printers and CAD), the piece standing on z = 0."""
    tris = []
    for b in panels(piece, dims):
        p, n, i = _box_mesh(b, flip_z=False)
        for t in i.reshape(-1, 3):
            tris.append((n[t[0]], p[t]))
    out = io.BytesIO()
    out.write(b"bfl Werkstatt STL (mm)".ljust(80, b" "))
    out.write(struct.pack("<I", len(tris)))
    k = 10.0 / max(scale, 1e-6)   # cm → mm, then the print scale
    for nrm, v in tris:
        nx, ny, nz = nrm
        out.write(struct.pack("<3f", nx, -nz, ny))                       # y up → z up
        for x, y, z in v:
            out.write(struct.pack("<3f", x * k, -z * k, y * k))
        out.write(b"\0\0")
    return out.getvalue()


def cut_list(piece: dict, dims) -> str:
    """Zuschnittliste: one line per panel, the three edge lengths sorted (Länge ≥ Breite ≥ Stärke) in mm, grouped."""
    rows = {}
    for b in panels(piece, dims):
        if b[6] in ("soft",):
            continue
        L, W, T = sorted((round((b[1] - b[0]) * 10), round((b[3] - b[2]) * 10), round((b[5] - b[4]) * 10)), reverse=True)
        key = (b[7], L, W, T)
        rows[key] = rows.get(key, 0) + 1
    lines = ["Teil;Anzahl;Länge mm;Breite mm;Stärke mm"]
    for (name, L, W, T), n in sorted(rows.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        lines.append(f"{name};{n};{L};{W};{T}")
    return "\n".join(lines) + "\n"
