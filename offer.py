"""Positionstext for an offer: one position per planned piece (type, dimensions, material, handles, construction from
the export3d panels, mounting, the carpenter's notes), as plain German text the carpenter pastes into the offer of
his shop software. No prices: they come from his own calculation.

    offer_text(doc, variant_id=None) -> str   variant_id: the pieces as that variant shows them, else the current plan"""

import textwrap

import export3d
import projects
import workshop

WIDTH = 92
INDENT = " " * 9
PLINTH_MAX = 12   # cm: a dark panel this low at the bottom is the plinth (Sockel), anything else is the back panel
CARCASS = ("schrank", "garderobe", "haengeschrank", "sideboard", "bad", "kueche")


def _variant(doc: dict, vid):
    v = next((x for x in doc.get("variants") or [] if isinstance(x, dict) and x.get("id") == vid), None)
    if v is None:
        raise KeyError(vid)
    return v


def pieces_of(doc: dict, variant_id=None) -> list[dict]:
    """The pieces to quote: the current plan, or the variant's pieces with the dimensions and labels it was made with
    (its snapshot) on top of the current piece (type, notes, elevation are not in the snapshot)."""
    current = [p for p in doc.get("pieces") or [] if isinstance(p, dict)]
    if variant_id is None:
        return [p for p in current if p.get("type")]
    v = _variant(doc, variant_id)
    by_id = {p.get("id"): p for p in current}
    snap = [s for s in v.get("pieces") or [] if isinstance(s, dict)]
    if not snap:
        ids = v.get("piece_ids") or []
        return [by_id[i] for i in ids if i in by_id and by_id[i].get("type")] or [p for p in current if p.get("type")]
    out = []
    for s in snap:
        p = dict(by_id.get(s.get("id")) or {})
        p.update({k: s[k] for k in ("w", "h", "d") if s.get(k)})
        for k in ("label", "material_label", "handles_label"):
            if s.get(k):
                p[f"_{k}"] = s[k]
        if p.get("type") or p.get("_label"):
            out.append(p)
    return out


def _dims(p: dict) -> str:
    vals = [p.get(k) if isinstance(p.get(k), int) and p.get(k) > 0 else None for k in ("w", "h", "d")]
    s = " × ".join(str(v) if v else "–" for v in vals) + " cm"
    return s + (" (fehlende Maße nach Aufmaß)" if None in vals else "")


def _construction(p: dict) -> str:
    """„Korpus 19 mm, Rückwand 8 mm, 3 Fronten, 5 Fachböden, Sockel“ — counted from the panels export3d builds."""
    if p.get("type") not in CARCASS or not all(isinstance(p.get(k), int) and p[k] > 0 for k in ("w", "h", "d")):
        return ""
    panels = export3d.panels(p, (p["w"], p["h"], p["d"]))
    elev = float(p.get("elev") or 0)
    fronts = sum(1 for x in panels if x[6] == "front")
    shelves = sum(1 for x in panels if x[7] == "Fachboden")
    plinth = any(x[6] == "dark" and x[2] <= elev + 0.01 and x[3] - x[2] <= PLINTH_MAX for x in panels)
    parts = [f"Korpus aus {round(export3d.BOARD * 10)}-mm-Platten",
             f"Rückwand {round(export3d.BACK * 10)} mm" if any(x[7] == "Rückwand" for x in panels) else ""]
    if fronts:
        parts.append(f"{fronts} {'Front' if fronts == 1 else 'Fronten'}")
    if shelves:
        parts.append(f"{shelves} {'Fachboden' if shelves == 1 else 'Fachböden'}")
    if plinth:
        parts.append("Sockel zurückgesetzt")
    if p.get("type") == "kueche":
        parts.append("Arbeitsplatte")
    return ", ".join(x for x in parts if x)


def _material(p: dict) -> str:
    if p.get("_material_label"):
        return p["_material_label"]
    if p.get("material") == "custom":
        return (p.get("material_custom") or "").strip() or "nach Absprache"
    return projects.MATERIAL_LABELS.get(p.get("material"), "") or "nach Absprache"


def _row(label: str, value: str) -> list[str]:
    value = " ".join(str(value).split())
    if not value:
        return []
    return textwrap.wrap(f"{label}: {value}", WIDTH, initial_indent=INDENT, subsequent_indent=INDENT + "  ")


def position(n: int, p: dict) -> str:
    label = p.get("_label") or projects.PIECE_LABELS.get(p.get("type"), "") or "Möbel"
    lines = [f"Pos. {n:<3} 1 Stück {label} nach Maß, Schreinerarbeit"]
    lines += _row("Abmessungen (B × H × T)", _dims(p))
    lines += _row("Material / Oberfläche", _material(p))
    handles = p.get("_handles_label") or projects.HANDLE_LABELS.get(p.get("handles"), "")
    if p.get("type") in CARCASS + ("sitzbank",):   # tables, beds, stairs have no handles (the default is "push")
        lines += _row("Griffe", handles or "nach Absprache")
    lines += _row("Aufbau", _construction(p))
    elev = p.get("elev")
    if isinstance(elev, int) and elev > 0:
        lines += _row("Montage", f"wandhängend, Unterkante {elev} cm über Fertigfußboden")
    if p.get("slope_fit"):
        lines += _row("Anpassung", "an die Dachschräge angepasst (Oberseite schräg, Maß vor Ort aufnehmen)")
    lines += _row("Ausführung", p.get("text") or "")
    # no "Lage": `where` is the carpenter's spoken phrase („rechts neben den Fernseher an die Wand“) – planning
    # information, not offer German; the pictures show where the piece goes
    return "\n".join(lines)


def offer_text(doc: dict, variant_id=None) -> str:
    """German Positionstext of the order's pieces (variant_id: as that variant shows them). KeyError: no such variant."""
    pieces = pieces_of(doc, variant_id)
    head = "Angebotspositionen"
    title = (doc.get("title") or "").strip()
    if title:
        head += f" – {title}"
    if (doc.get("ref") or "").strip():
        head += f" (Nr. {doc['ref'].strip()})"
    out = [head]
    if variant_id is not None:
        out.append(f"Entwurf {variant_id}")
    out.append("")
    if not pieces:
        out.append("Noch keine Möbel geplant.")
        return "\n".join(out) + "\n"
    n = 0
    for p in pieces:
        n += 1
        out += [position(n, p), ""]
    if doc.get("removes"):
        n += 1
        out += [f"Pos. {n:<3} Demontage und Entsorgung der vorhandenen Einbauten bzw. Möbel (im Foto markiert)", ""]
    n += 1
    out += [f"Pos. {n:<3} Lieferung und fachgerechte Montage vor Ort", ""]
    out.append("Alle Maße sind Planungsmaße und werden vor der Fertigung durch ein Aufmaß vor Ort bestätigt.")
    return "\n".join(out) + "\n"


def piece_dims(p: dict) -> tuple[int, int, int]:
    """Full dimensions for drawings (catalogue defaults where a value is missing) — workshop's rule."""
    return workshop.piece_dims(p)
