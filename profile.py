"""The company profile: how this workshop usually builds, so the assistant fills sensible defaults instead of asking.

Layers (later wins): catalog (workshop.CATALOG dims/elev) → trade pack (packs/<id>.json: schreiner, kueche, treppe,
terrassendach, schiebetuer) → the company's own settings in projects/profile.json (mode 600). The profile also carries
vocabulary aliases (spoken words → catalog ids), wording for prompts and the judge, the preferred quality, auto-send rules
and what the team keeps correcting: learn() counts identical corrections per (type, field, value) and after `threshold`
(default 3) distinct pieces offers a suggestion; an accepted suggestion becomes the type's default.

Stored values of None mean "not set here, inherit from the pack / catalog". Handles "keine" means explicitly no handles
(stairs, terrace roofs), which resolves to None in the effective defaults."""

import importlib.util
import sysconfig
from pathlib import Path


def __getattr__(name):
    """This file shadows the stdlib `profile` module (the app dir is first on sys.path) and cProfile does
    `import profile as _pyprofile`; forward unknown attributes to the real one so profiling keeps working.
    Defined before the app imports below, so a cProfile import during them also resolves."""
    std = globals().get("_STD")
    if std is None:
        spec = importlib.util.spec_from_file_location("_stdlib_profile",
                                                      Path(sysconfig.get_paths()["stdlib"]) / "profile.py")
        std = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(std)
        globals()["_STD"] = std
    if name.startswith("__") or not hasattr(std, name):
        raise AttributeError(name)
    return getattr(std, name)


import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import projects
from workshop import CATALOG

HERE = Path(__file__).parent
PACKS = HERE / "packs"   # tests monkeypatch this
PATH = None              # tests may set an explicit file; default: projects.STORE / "profile.json"
router = APIRouter()

DEFAULT_TRADE = "schreiner"
QUALITY = ("entwurf", "fein")
NO_HANDLES = "keine"
TYPES = tuple(CATALOG["pieces"])
MATERIALS = tuple(k for k in CATALOG["materials"] if k != "custom")
HANDLES = tuple(CATALOG["handles"])
ALIAS_FIELDS = ("type", "material", "handles")
LEARN_FIELDS = ("material", "handles", "d", "h", "elev")   # w is the order's own measure, never a company habit
FIELD_LABELS = {"material": "Material", "handles": "Griffe", "w": "Breite", "h": "Höhe", "d": "Tiefe",
                "elev": "Abstand vom Boden"}
BASE_COMPANY = {"material": "eiche", "material_custom": "", "handles": "push", "plinth": 8, "board": 19}  # = DEFAULT_PIECE
WORDING = {"notes_de": 1000, "prompt_en": 600, "judge_en": 600}
MAX_ALIASES = 200        # per field
MAX_COUNTS = 400         # learning counters kept (oldest dropped)
MAX_SUGGESTIONS = 100
MAX_PIECE_IDS = 30       # per counter: identical corrections count once per piece
ID = r"[a-z][a-z0-9_]{1,30}"

_LOCK = threading.RLock()
_CACHE: dict[str, tuple] = {}   # path -> (stat key, parsed raw)


class Bad(ValueError):
    """A rejected profile value; the message is German and shown to the user."""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def path() -> Path:
    return Path(PATH) if PATH else Path(projects.STORE) / "profile.json"


# ---------------- text normalisation (aliases) ----------------

_UML = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def norm(text: str) -> str:
    """Lowercase, umlauts spelled out, separators collapsed: "Push-to-open" == "push to open", "Weißlack" == "weisslack"."""
    s = str(text or "").lower().translate(_UML)
    s = re.sub(r"[-_/.,;:!?()\"'„“”‚‘’]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------- validation ----------------

class _V:
    """strict: raise Bad (PUT from the user); lenient: drop the bad value (files on disk, packs)."""

    def __init__(self, strict: bool):
        self.strict = strict

    def fail(self, msg: str, default=None):
        if self.strict:
            raise Bad(msg)
        return default

    def dict(self, v, what: str) -> dict:
        if v is None:
            return {}
        return v if isinstance(v, dict) else self.fail(f"{what}: ungültiges Format.", {})

    def keys(self, d: dict, allowed, what: str) -> None:
        for k in list(d):
            if k not in allowed:
                self.fail(f"{what}: unbekanntes Feld „{k}“.")
                d.pop(k)

    def int(self, v, lo: int, hi: int, msg: str):
        if v is None:
            return None
        if isinstance(v, str) and re.fullmatch(r"\s*\d+([.,]\d+)?\s*", v):
            v = float(v.replace(",", "."))
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= round(v) <= hi:
            return self.fail(msg)
        return int(round(v))

    def num(self, v, lo: float, hi: float, msg: str):
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            return self.fail(msg)
        return round(float(v), 2)

    def bool(self, v, msg: str):
        if v is None or isinstance(v, bool):
            return v
        return self.fail(msg)

    def str(self, v, n: int, msg: str) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            return self.fail(msg, "")
        if len(v) > n:
            return self.fail(f"{msg} (höchstens {n} Zeichen).", v[:n])
        return v.strip()

    def choice(self, v, allowed, msg: str):
        if v is None:
            return None
        return v if isinstance(v, str) and v in allowed else self.fail(msg)


def _material(v: _V, m, what="Material"):
    """A catalog material id, or "custom:<free text>" (packs/aliases for woods outside the catalog, e.g. Lärche)."""
    if m is None:
        return None
    if isinstance(m, str) and m.startswith("custom:") and 1 <= len(m[7:].strip()) <= 200:
        return "custom:" + m[7:].strip()
    return v.choice(m, MATERIALS + ("custom",), f"{what}: unbekanntes Material „{m}“.")


def _handles(v: _V, h, what="Griffe"):
    return v.choice(h, HANDLES + (NO_HANDLES,), f"{what}: unbekannte Griffart „{h}“.")


def _doors(v: _V, d, what: str):
    if d is None:
        return None
    d = v.dict(d, f"{what} Türen")
    v.keys(d, ("per",), f"{what} Türen")
    per = v.int(d.get("per"), 20, 200, f"{what}: Türbreite muss zwischen 20 und 200 cm liegen.")
    return {"per": per} if per else None


def _type_defaults(v: _V, t: dict, what: str) -> dict:
    t = v.dict(t, what)
    v.keys(t, ("w", "h", "d", "elev", "full_height", "doors", "material", "material_custom", "handles"), what)
    dims = {k: v.int(t.get(k), 1, 2000, f"{what}: {FIELD_LABELS[k]} muss zwischen 1 und 2000 cm liegen.")
            for k in ("w", "h", "d")}
    out = {**dims, "elev": v.int(t.get("elev"), 0, 400, f"{what}: Abstand vom Boden muss zwischen 0 und 400 cm liegen."),
           "full_height": v.bool(t.get("full_height"), f"{what}: raumhoch muss ja/nein sein."),
           "doors": _doors(v, t.get("doors"), what), "material": _material(v, t.get("material"), what),
           "material_custom": v.str(t.get("material_custom"), 200, f"{what}: Materialangabe ungültig"),
           "handles": _handles(v, t.get("handles"), what)}
    return {k: x for k, x in out.items() if x not in (None, "")}


def _company(v: _V, c) -> dict:
    c = v.dict(c, "Firmenvorgaben")
    v.keys(c, ("material", "material_custom", "handles", "plinth", "board"), "Firmenvorgaben")
    return {"material": _material(v, c.get("material")),
            "material_custom": v.str(c.get("material_custom"), 200, "Eigene Materialangabe ungültig"),
            "handles": _handles(v, c.get("handles")),
            "plinth": v.int(c.get("plinth"), 0, 30, "Sockelhöhe muss zwischen 0 und 30 cm liegen."),
            "board": v.int(c.get("board"), 8, 200, "Material-/Plattenstärke muss zwischen 8 und 200 mm liegen.")}


def _aliases(v: _V, a, templates=()) -> dict:
    a = v.dict(a, "Begriffe")
    v.keys(a, ALIAS_FIELDS, "Begriffe")
    out = {}
    for f in ALIAS_FIELDS:
        rows = v.dict(a.get(f), "Begriffe")
        if len(rows) > MAX_ALIASES:
            v.fail(f"Zu viele Begriffe (höchstens {MAX_ALIASES} je Art).")
            rows = dict(list(rows.items())[:MAX_ALIASES])
        got = {}
        for phrase, val in rows.items():
            if not isinstance(phrase, str) or not 2 <= len(norm(phrase)) <= 60:
                v.fail(f"Begriff „{str(phrase)[:60]}“: 2 bis 60 Zeichen.")
                continue
            if f == "type":
                val = v.choice(val, TYPES + tuple(templates), f"Begriff „{phrase}“: unbekannter Möbeltyp „{val}“.")
            elif f == "material":
                val = _material(v, val, f"Begriff „{phrase}“")
            else:
                val = _handles(v, val, f"Begriff „{phrase}“")
            if val is not None:
                got[phrase.strip()] = val
        out[f] = got
    return out


def _wording(v: _V, w) -> dict:
    w = v.dict(w, "Formulierungen")
    v.keys(w, WORDING, "Formulierungen")
    return {k: v.str(w.get(k), n, "Formulierung ungültig") for k, n in WORDING.items()}


def _auto_send(v: _V, a) -> dict:
    a = v.dict(a, "Automatisch senden")
    v.keys(a, ("enabled", "min_overall", "require_pass", "max_images"), "Automatisch senden")
    mo = v.num(a.get("min_overall"), 0, 10, "Mindestnote muss zwischen 0 und 10 liegen.")
    mi = v.int(a.get("max_images"), 1, 4, "Automatisch senden: 1 bis 4 Bilder.")
    rp = v.bool(a.get("require_pass"), "„Nur bestandene Bilder“ muss ja/nein sein.")
    return {"enabled": bool(v.bool(a.get("enabled"), "„Automatisch senden“ muss ja/nein sein.")),
            "min_overall": 7.5 if mo is None else mo, "require_pass": True if rp is None else rp,
            "max_images": mi or 2}


def _learning(v: _V, lr) -> dict:
    lr = v.dict(lr, "Lernen")
    v.keys(lr, ("enabled", "threshold"), "Lernen")
    on = v.bool(lr.get("enabled"), "„Aus Korrekturen lernen“ muss ja/nein sein.")
    th = v.int(lr.get("threshold"), 2, 10, "Vorschlag nach 2 bis 10 gleichen Korrekturen.")
    return {"enabled": True if on is None else on, "threshold": th or 3}


def _learned(raw) -> dict:
    """Bookkeeping written only by learn()/accept(); never user input, so it is only shape-checked."""
    raw = raw if isinstance(raw, dict) else {}
    counts = {k: c for k, c in (raw.get("counts") or {}).items()
              if isinstance(k, str) and isinstance(c, dict) and isinstance(c.get("n"), int)} \
        if isinstance(raw.get("counts"), dict) else {}
    sugg = [s for s in raw.get("suggestions") or [] if isinstance(s, dict) and isinstance(s.get("id"), str)] \
        if isinstance(raw.get("suggestions"), list) else []
    return {"counts": counts, "suggestions": sugg[-MAX_SUGGESTIONS:]}


TOP = ("trade", "company", "types", "aliases", "wording", "quality", "auto_send", "learning")


def normalize(raw, strict: bool = False) -> dict:
    v = _V(strict)
    raw = raw if isinstance(raw, dict) else {}
    if strict:
        v.keys(dict(raw), TOP, "Firmenprofil")
    trade = raw.get("trade")
    if trade is not None and trade not in packs_by_id():
        trade = v.fail(f"Unbekanntes Fachpaket „{trade}“.")
    pack = packs_by_id().get(trade or DEFAULT_TRADE) or {}
    types = {}
    for t, d in v.dict(raw.get("types"), "Möbeltypen").items():
        if t not in TYPES and t not in pack.get("templates", {}):
            v.fail(f"Unbekannter Möbeltyp „{t}“.")
            continue
        td = _type_defaults(v, d, CATALOG["pieces"].get(t, {}).get("de") or t)
        if td:
            types[t] = td
    q = v.choice(raw.get("quality"), QUALITY, "Qualität muss „entwurf“ oder „fein“ sein.")
    return {"version": 1, "trade": trade or DEFAULT_TRADE, "company": _company(v, raw.get("company")), "types": types,
            "aliases": _aliases(v, raw.get("aliases"), pack.get("templates", {})), "wording": _wording(v, raw.get("wording")),
            "quality": q, "auto_send": _auto_send(v, raw.get("auto_send")), "learning": _learning(v, raw.get("learning")),
            "learned": _learned(raw.get("learned")),
            "updated": raw.get("updated") if isinstance(raw.get("updated"), str) else None,
            "updated_by": raw.get("updated_by") if isinstance(raw.get("updated_by"), str) else None}


# ---------------- trade packs ----------------

def _pack(raw, pid: str, strict: bool = False) -> dict:
    v = _V(strict)
    raw = v.dict(raw, f"Paket {pid}")
    if raw.get("id") != pid:
        v.fail(f"Paket {pid}: id passt nicht zum Dateinamen.")
    templates = {}
    for t in raw.get("templates") or []:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not re.fullmatch(ID, t["id"]) or t["id"] in TYPES:
            v.fail(f"Paket {pid}: ungültige Vorlage.")
            continue
        base = v.choice(t.get("type"), TYPES, f"Paket {pid}: Vorlage {t['id']} hat einen unbekannten Typ.")
        dims = _type_defaults(v, {k: t.get(k) for k in ("w", "h", "d", "elev", "full_height", "doors", "material",
                                                       "material_custom", "handles") if k in t}, f"Vorlage {t['id']}")
        templates[t["id"]] = {"id": t["id"], "type": base, "label": v.str(t.get("label"), 80, "Vorlage: Name") or t["id"],
                              "text": v.str(t.get("text"), 500, "Vorlage: Text"), **dims}
    types = [t for t in raw.get("types") or [] if t in TYPES]
    if len(types) != len(raw.get("types") or []):
        v.fail(f"Paket {pid}: unbekannter Möbeltyp in „types“.")
    td = {}
    for t, d in v.dict(raw.get("type_defaults"), f"Paket {pid}").items():
        if t not in TYPES and t not in templates:
            v.fail(f"Paket {pid}: unbekannter Möbeltyp „{t}“.")
            continue
        td[t] = _type_defaults(v, d, f"Paket {pid}/{t}")
    q = v.choice(raw.get("quality"), QUALITY, f"Paket {pid}: Qualität ungültig.")
    return {"id": pid, "label": v.str(raw.get("label"), 80, "Paketname") or pid,
            "description": v.str(raw.get("description"), 400, "Paketbeschreibung"), "types": types,
            "templates": templates, "company": _company(v, raw.get("company")), "type_defaults": td,
            "aliases": _aliases(v, raw.get("aliases"), templates), "wording": _wording(v, raw.get("wording")),
            "quality": q}


def _read_json(p: Path):
    """Parsed JSON, cached by (inode, mtime, size); None if missing. A corrupt file is moved aside, never overwritten."""
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    key = (st.st_ino, st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(str(p))
    if hit and hit[0] == key:
        return copy.deepcopy(hit[1])
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        traceback.print_exc()
        if p.parent != PACKS and p.exists():
            p.replace(p.with_name(f"{p.name}.corrupt-{int(time.time())}"))
        return None
    _CACHE[str(p)] = (key, raw)
    return copy.deepcopy(raw)


def packs_by_id() -> dict:
    out = {}
    for f in sorted(Path(PACKS).glob("*.json")):
        if re.fullmatch(ID, f.stem):
            raw = _read_json(f)
            if isinstance(raw, dict):
                out[f.stem] = _pack(raw, f.stem)
    return out


def pack_summary(p: dict) -> dict:
    return {"id": p["id"], "label": p["label"], "description": p["description"],
            "types": [{"id": t, "label": CATALOG["pieces"][t]["de"]} for t in p["types"]],
            "templates": [{"id": t["id"], "label": t["label"], "type": t["type"]} for t in p["templates"].values()],
            "quality": p["quality"]}


# ---------------- storage ----------------

def _write(obj: dict) -> None:
    """Atomic and private: mkstemp (mode 600) in the same dir, fsync, os.replace."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _CACHE.pop(str(p), None)


def get() -> dict:
    """The stored profile (normalized, missing file = pack defaults only); a deep copy."""
    with _LOCK:
        return normalize(_read_json(path()))


def _update(fn, who: str | None = None) -> dict:
    with _LOCK:
        prof = get()
        fn(prof)
        prof = normalize(prof)
        if who is not None:
            prof["updated"], prof["updated_by"] = now(), who
        _write(prof)
        return prof


def _merge(target, patch):
    """RFC 7386 JSON merge patch: dicts merge, None deletes (= inherit again), everything else replaces."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge(out.get(k), v)
    return out


def put(patch: dict, who: str = "anon") -> dict:
    """Merge patch from the settings page; raises Bad with a German message on the first invalid value."""
    if not isinstance(patch, dict):
        raise Bad("Ungültige Änderung.")
    reset = "learned" in patch and patch.get("learned") is None   # {"learned": null}: forget what was learned
    patch = {k: x for k, x in patch.items() if k != "learned" or not reset}
    with _LOCK:
        cur = get()
        user = {k: cur[k] for k in TOP}
        merged = _merge(user, patch)
        normalize(merged, strict=True)    # validates; raises Bad
        merged["learned"] = {"counts": {}, "suggestions": []} if reset else cur["learned"]
        prof = normalize(merged)
        prof["updated"], prof["updated_by"] = now(), who
        _write(prof)
        return prof


# ---------------- effective view ----------------

def _apply(dst: dict, src: dict, layer: str, src_map: dict) -> None:
    for k, x in src.items():
        if x is not None and x != "":
            dst[k] = x
            src_map[k] = layer


def _split_material(d: dict) -> None:
    m = d.get("material")
    if isinstance(m, str) and m.startswith("custom:"):
        d["material"], d["material_custom"] = "custom", m[7:]


def effective(prof: dict | None = None) -> dict:
    """Profile + pack + catalog, resolved: company defaults, per-type defaults (catalog types and pack templates),
    merged aliases, wording, quality, auto-send. Pure (no I/O beyond reading packs)."""
    prof = prof or get()
    pack = packs_by_id().get(prof["trade"]) or _pack({"id": prof["trade"]}, prof["trade"])
    company, csrc = dict(BASE_COMPANY), {k: "standard" for k in BASE_COMPANY}
    _apply(company, pack["company"], "paket", csrc)
    _apply(company, prof["company"], "firma", csrc)
    _split_material(company)
    types = {}
    for t in TYPES + tuple(pack["templates"]):
        tpl = pack["templates"].get(t)
        base = tpl["type"] if tpl else t
        cat = CATALOG["pieces"].get(base or "", {})
        dims = cat.get("dims") or [None, None, None]
        row = {"type": base, "template": t if tpl else None, "label": tpl["label"] if tpl else cat["de"],
               "text": tpl["text"] if tpl else "", "w": dims[0], "h": dims[1], "d": dims[2], "elev": cat.get("elev", 0),
               "full_height": False, "doors": None, **{k: company[k] for k in BASE_COMPANY}}
        src = {k: "katalog" for k in ("w", "h", "d", "elev")} | {k: csrc[k] for k in BASE_COMPANY}
        layers = [("paket", tpl or {}), ("paket", pack["type_defaults"].get(t, {}))]
        if base and base != t:
            # a template inherits its base type's pack defaults, and the company's material/handles choice for it
            own = {k: x for k, x in prof["types"].get(base, {}).items() if k in ("material", "material_custom", "handles")}
            layers = [("paket", pack["type_defaults"].get(base, {}))] + layers + [("firma", own)]
        for layer, d in layers + [("firma", prof["types"].get(t, {}))]:
            _apply(row, {k: x for k, x in d.items() if k not in ("id", "type", "label", "text")}, layer, src)
        _split_material(row)
        if row["handles"] == NO_HANDLES:
            row["handles"] = None
        row["source"] = src
        types[t] = row
    if company["handles"] == NO_HANDLES:
        company["handles"] = None
    aliases = {f: {**pack["aliases"].get(f, {}), **prof["aliases"].get(f, {})} for f in ALIAS_FIELDS}
    wording = {k: "\n".join(x for x in (pack["wording"][k], prof["wording"][k]) if x) for k in WORDING}
    return {"trade": prof["trade"], "pack": pack_summary(pack), "company": company, "types": types, "aliases": aliases,
            "wording": wording, "quality": prof["quality"] or pack["quality"] or "entwurf",
            "auto_send": prof["auto_send"], "learning": prof["learning"]}


def defaults_for(type_: str | None, prof: dict | None = None) -> dict:
    """Defaults for one piece type (catalog id or pack template id): {type, template, label, text, w, h, d, elev,
    full_height, doors, doors_n, material, material_custom, handles, plinth, board, source}. Unknown/None → company
    defaults with no dims."""
    eff = effective(prof)
    row = eff["types"].get(type_)
    if row is None:
        row = {"type": None, "template": None, "label": "Möbel", "text": "", "w": None, "h": None, "d": None, "elev": 0,
               "full_height": False, "doors": None, **eff["company"], "source": {}}
    row = copy.deepcopy(row)
    row["doors_n"] = doors_for(row, row.get("w"))
    return row


def doors_for(defaults: dict, w) -> int | None:
    """Door count from the type's rule (one leaf per `per` cm, rounded like mockup.parts does)."""
    per = ((defaults or {}).get("doors") or {}).get("per")
    if not per or not isinstance(w, (int, float)) or isinstance(w, bool) or w <= 0:
        return None
    return max(1, min(12, round(w / per)))


def piece_defaults(type_: str | None, prof: dict | None = None) -> dict:
    """Fields ready for an add_piece op (only projects.PIECE_FIELDS; w stays None — the order's measure)."""
    d = defaults_for(type_, prof)
    out = {"type": d["type"], "text": d["text"], "material": d["material"] or "eiche",
           "material_custom": d["material_custom"] if d["material"] == "custom" else "", "handles": d["handles"],
           "w": None, "h": None if d["full_height"] else d["h"], "d": d["d"]}
    if d.get("elev"):
        out["elev"] = d["elev"]
    return out


# ---------------- vocabulary ----------------

def _alias_rx(phrase: str) -> re.Pattern:
    # German endings so "Messinggriffe(n)", "Eichen…" still hit; word boundaries so "schrank" ≠ "haengeschrank"
    return re.compile(rf"(?<![a-z0-9]){re.escape(norm(phrase))}(?:e|en|n|s|er|es)?(?![a-z0-9])")


def resolve(text: str, prof: dict | None = None) -> dict:
    """Spoken words → ids: {"type", "template", "material", "material_custom", "handles", "hits": [...]}; only the
    fields found. Longest phrases claim their span first ("Eiche weiß geölt" beats "Eiche")."""
    eff = effective(prof)
    s = norm(text)
    rows = sorted(((phrase, f, val) for f in ALIAS_FIELDS for phrase, val in eff["aliases"][f].items()),
                  key=lambda r: -len(norm(r[0])))
    taken, hits = [], []
    for phrase, f, val in rows:
        for m in _alias_rx(phrase).finditer(s):
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append({"phrase": phrase, "field": f, "value": val, "at": m.start()})
    hits.sort(key=lambda h: h["at"])
    out = {}
    for h in hits:
        f, val = h["field"], h["value"]
        if f in out:
            continue
        if f == "type":
            tpl = eff["types"].get(val, {})
            out["type"], out["template"] = tpl.get("type", val), tpl.get("template")
        elif f == "material" and val.startswith("custom:"):
            out["material"], out["material_custom"] = "custom", val[7:]
        elif f == "handles":
            out["handles"] = None if val == NO_HANDLES else val
        else:
            out[f] = val
    out["hits"] = hits
    return out


def vocabulary_hint(prof: dict | None = None, limit: int = 1500) -> str:
    """Short German lines for LLM prompts: "Weißlack → material weiss"; capped so prompts stay small."""
    eff = effective(prof)
    lines, n = [], 0
    for f in ALIAS_FIELDS:
        by_val: dict[str, list] = {}
        for phrase, val in eff["aliases"][f].items():
            by_val.setdefault(val, []).append(phrase)
        for val, phrases in by_val.items():
            line = f"{', '.join(phrases[:6])} → {f} {val}"
            if n + len(line) + 1 > limit:
                return "\n".join(lines)
            lines.append(line)
            n += len(line) + 1
    return "\n".join(lines)


def voice_words(prof: dict | None = None, limit: int = 60) -> list[str]:
    """Distinct alias phrases (original spelling) for the speech recogniser's domain prompt."""
    seen, out = set(), []
    for f in ALIAS_FIELDS:
        for phrase in effective(prof)["aliases"][f]:
            if norm(phrase) not in seen:
                seen.add(norm(phrase))
                out.append(phrase)
    return out[:limit]


def prompt_notes(prof: dict | None = None) -> dict:
    """{"notes_de", "prompt_en", "judge_en"}: pack wording followed by the company's own."""
    return effective(prof)["wording"]


def auto_send_decision(verdicts: list[dict], prof: dict | None = None) -> dict:
    """Which results may go out without a human look: {"send": bool, "picks": [indices], "why": German}. Never sends
    when the rule is off (the default)."""
    rule = (prof or get())["auto_send"]
    if not rule["enabled"]:
        return {"send": False, "picks": [], "why": "Automatisch senden ist aus."}
    ok = []
    for i, v in enumerate(verdicts or []):
        o = v.get("overall") if isinstance(v, dict) else None
        if not isinstance(o, (int, float)) or isinstance(o, bool) or o < rule["min_overall"]:
            continue
        if rule["require_pass"] and v.get("pass") is not True:
            continue
        ok.append((o, i))
    picks = [i for _, i in sorted(ok, key=lambda x: (-x[0], x[1]))[:rule["max_images"]]]
    if not picks:
        return {"send": False, "picks": [], "why": f"Kein Bild erreicht die Mindestnote {rule['min_overall']:g}."}
    return {"send": True, "picks": picks, "why": f"{len(picks)} Bild(er) ab Note {rule['min_overall']:g}."}


# ---------------- learning ----------------

def _key(t: str, f: str, val) -> str:
    return f"{t}|{f}|{json.dumps(val, ensure_ascii=False)}"


def _sid(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def _value_label(f: str, val) -> str:
    if val is None:
        return "–"
    if f == "material":
        return CATALOG["materials"].get(val, {}).get("de", str(val))
    if f == "handles":
        return CATALOG["handles"].get(val, {}).get("de", str(val))
    return f"{val} cm"


def _learnable(f: str, val) -> bool:
    if f == "material":
        return val in MATERIALS
    if f == "handles":
        return val in HANDLES
    if f == "elev":
        return isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 400
    return isinstance(val, int) and not isinstance(val, bool) and 1 <= val <= 2000


def learn(before_piece: dict, after_piece: dict, who: str = "anon", fields=None) -> list[dict]:
    """Records the user's corrections of one piece. A correction counts once per piece (the same piece edited back and
    forth is one vote); after `threshold` pieces corrected to the same value a suggestion opens. Returns new suggestions.
    A type change is not a correction (the other fields follow the new type)."""
    if not isinstance(before_piece, dict) or not isinstance(after_piece, dict):
        return []
    t = after_piece.get("type")
    if t not in TYPES or before_piece.get("type") != t:
        return []
    pid = str(after_piece.get("id") or before_piece.get("id") or "")
    changed = [f for f in (fields or LEARN_FIELDS) if f in LEARN_FIELDS
               and after_piece.get(f) != before_piece.get(f) and _learnable(f, after_piece.get(f))]
    if not changed:
        return []
    new = []

    def fn(prof):
        if not prof["learning"]["enabled"]:
            return
        eff_row = effective(prof)["types"][t]
        counts, sugg = prof["learned"]["counts"], prof["learned"]["suggestions"]
        for f in changed:
            val = after_piece[f]
            if eff_row.get(f) == val:
                continue    # already the default: nothing to learn
            k = _key(t, f, val)
            c = counts.get(k) or {"type": t, "field": f, "value": val, "n": 0, "pieces": [], "who": [], "first": now()}
            if pid and pid in c["pieces"]:
                continue
            c["n"] += 1
            c["pieces"] = (c["pieces"] + ([pid] if pid else []))[-MAX_PIECE_IDS:]
            c["who"] = (c["who"] + [who])[-MAX_PIECE_IDS:] if who not in c["who"] else c["who"]
            c["last"] = now()
            counts[k] = c
            sid = _sid(k)
            if c["n"] >= prof["learning"]["threshold"] and not any(s["id"] == sid and s["status"] == "open" for s in sugg):
                s = {"id": sid, "type": t, "field": f, "value": val, "count": c["n"], "from": eff_row.get(f),
                     "who": list(c["who"]), "status": "open", "created": now(),
                     "text_de": f"{eff_row['label']}: {FIELD_LABELS[f]} standardmäßig {_value_label(f, val)} statt "
                                f"{_value_label(f, eff_row.get(f))}? ({c['n']}× so korrigiert)"}
                sugg[:] = [x for x in sugg if x["id"] != sid] + [s]
                new.append(s)
        if len(counts) > MAX_COUNTS:
            for k in sorted(counts, key=lambda k: counts[k].get("last") or "")[:len(counts) - MAX_COUNTS]:
                counts.pop(k)
        del sugg[:-MAX_SUGGESTIONS]

    _update(fn)
    return copy.deepcopy(new)


def learn_doc(before_doc: dict, after_doc: dict, who: str = "anon", ops: list | None = None) -> list[dict]:
    """learn() for every piece present in both docs. With ops, only fields the user set explicitly count (never side
    effects such as the elevation that follows a type change). TEST orders (Großtest, assistant checks) never teach
    the company profile: their corrections are scripted, not the carpenter's habits."""
    if any(isinstance(d, dict) and (d.get("test") or re.match(r"TEST\b", str(d.get("title") or "").strip()))
           for d in (before_doc, after_doc)):
        return []
    fields_by: dict[str, set] | None = None
    if ops is not None:
        fields_by = {}
        for op in ops if isinstance(ops, list) else []:
            p = op.get("path") if isinstance(op, dict) and op.get("op") == "set" else None
            s = p.split(".") if isinstance(p, str) else []
            if len(s) == 3 and s[0] == "pieces" and s[2] in LEARN_FIELDS:
                fields_by.setdefault(s[1], set()).add(s[2])
        if not fields_by:
            return []
    before = {p.get("id"): p for p in (before_doc or {}).get("pieces") or [] if isinstance(p, dict)}
    out = []
    for p in (after_doc or {}).get("pieces") or []:
        if isinstance(p, dict) and p.get("id") in before:
            fs = None if fields_by is None else fields_by.get(p["id"])
            if fields_by is not None and not fs:
                continue
            out += learn(before[p["id"]], p, who, tuple(fs) if fs else None)
    return out


def suggestions(status: str | None = "open") -> list[dict]:
    return [s for s in get()["learned"]["suggestions"] if status is None or s.get("status") == status]


def _decide(sid: str, who: str, accept_it: bool) -> dict:
    out = {}

    def fn(prof):
        s = next((x for x in prof["learned"]["suggestions"] if x["id"] == sid), None)
        if s is None:
            raise KeyError(sid)
        if s["status"] != "open":
            out["s"] = s
            return
        if accept_it:
            row = prof["types"].setdefault(s["type"], {})
            row[s["field"]] = s["value"]
            if s["field"] == "h":
                row["full_height"] = False    # an explicit default height replaces "raumhoch"
        s.update(status="accepted" if accept_it else "dismissed", decided=now(), decided_by=who)
        # start over: a dismissed habit may come back, an accepted one is now the default
        prof["learned"]["counts"].pop(_key(s["type"], s["field"], s["value"]), None)
        out["s"] = s

    prof = _update(fn, who)
    return {"suggestion": copy.deepcopy(out["s"]), "profile": prof}


def accept(sid: str, who: str = "anon") -> dict:
    """The suggested value becomes the type's default. Raises KeyError for an unknown id; idempotent otherwise."""
    return _decide(sid, who, True)


def dismiss(sid: str, who: str = "anon") -> dict:
    return _decide(sid, who, False)


# ---------------- router ----------------

def _who(request: Request) -> str:
    return getattr(getattr(request, "state", None), "owner", None) or "anon"


def _view(prof: dict) -> dict:
    return {"profile": prof, "effective": effective(prof)}


@router.get("/api/profile")
def api_get():
    return _view(get())


@router.put("/api/profile")
def api_put(body: dict, request: Request):
    try:
        prof = put(body, _who(request))
    except Bad as e:
        raise HTTPException(400, str(e))
    return _view(prof)


@router.get("/api/profile/packs")
def api_packs():
    return {"packs": [pack_summary(p) for p in packs_by_id().values()], "current": get()["trade"]}


@router.get("/api/profile/suggestions")
def api_suggestions():
    return {"suggestions": suggestions("open")}


@router.post("/api/profile/suggestions/{sid}/accept")
def api_accept(sid: str, request: Request):
    try:
        res = accept(sid, _who(request))
    except KeyError:
        raise HTTPException(404, "Vorschlag nicht gefunden.")
    return {"suggestion": res["suggestion"], **_view(res["profile"])}


@router.post("/api/profile/suggestions/{sid}/dismiss")
def api_dismiss(sid: str, request: Request):
    try:
        res = dismiss(sid, _who(request))
    except KeyError:
        raise HTTPException(404, "Vorschlag nicht gefunden.")
    return {"suggestion": res["suggestion"], **_view(res["profile"])}
