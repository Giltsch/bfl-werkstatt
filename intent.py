"""Understanding a carpenter's spoken order: German dimension normalisation (deterministic) and the structured reading of
the order by the local Qwen (judge.ask with a strict JSON schema), turned into projects ops that apply_ops accepts.

normalize_de() turns colloquial dimensions into "NNN cm" ("zwo-zwanzig" → "220 cm", "einsachtzig" → "180 cm",
"60er Tiefe" → "60 cm tief", "deckenhoch" → "raumhoch") so the model never has to guess what "zwei zwanzig" means.
understand() asks the model only for what was said; defaults (company profile or built-ins), questions, the summary and
the ops are computed here, so the model's output stays short (≈ 10 tokens/s while FLUX shares the GPU)."""

import io
import random
import re
import string
import time
import traceback

import httpx
from fastapi import APIRouter, HTTPException, Request
from PIL import Image

import judge
import projects
from workshop import CATALOG

router = APIRouter()
EXCLUSIVE = lambda fn: fn()  # the integrator may set the scheduler's exclusive() (keeps the call away from a FLUX image)

MAX_TEXT = 2000
TIMEOUT = 90           # the judge shares the GPU with FLUX: a short answer can take 30 s under load
MAX_TOKENS = 600
DEFAULT_ROOM_H = 250   # typical German ceiling when the room was not measured
TYPES = tuple(CATALOG["pieces"])
MATERIALS = tuple(CATALOG["materials"])            # incl. "custom"
HANDLES = tuple(CATALOG["handles"])
# types whose width the carpenter must say (everything else has a usable catalog width)
ASK_W = {"schrank", "kueche", "regal", "sideboard", "wandregal", "haengeschrank", "garderobe", "bad", "sitzbank", "wand",
         "bett", "tisch"}
# types with fronts: only these get default handles
HANDLE_TYPES = {"schrank", "kueche", "sideboard", "haengeschrank", "garderobe", "bad", "sitzbank"}
FULL_HEIGHT_TYPES = {"schrank"}   # an Einbauschrank goes up to the ceiling unless a height is given
W_QUESTION = {"schrank": "Wie breit soll der Einbauschrank werden?", "kueche": "Wie lang soll die Küche werden?",
              "regal": "Wie breit soll die Regalwand werden?", "sideboard": "Wie breit soll das Sideboard werden?",
              "wandregal": "Wie breit soll das Wandregal werden?", "haengeschrank": "Wie breit soll der Hängeschrank werden?",
              "garderobe": "Wie breit soll die Garderobe werden?", "bad": "Wie breit soll das Badmöbel werden?",
              "sitzbank": "Wie lang soll die Sitzbank werden?", "wand": "Wie breit soll die Wandverkleidung werden?",
              "bett": "Wie breit soll das Bett werden (Liegefläche)?", "tisch": "Wie lang soll der Tisch werden?"}
# extra spoken words per id for the prompt (the profile's vocabulary is added on top)
TYPE_WORDS = {"schrank": "Kleiderschrank, Schrank, Schrankwand, Dachschrägenschrank, Abstellschrank, Nischenschrank",
              "kueche": "Küchenzeile, Kochinsel, Unter- und Oberschränke",
              "regal": "Bücherregal, Bücherwand, Regal raumhoch",
              "sideboard": "Kommode, Lowboard, TV-Möbel, Anrichte", "wandregal": "Wandboard, schwebende Bretter",
              "haengeschrank": "Oberschrank, Wandschrank hängend", "tisch": "Tisch, Schreibtisch",
              "bett": "Doppelbett, Einzelbett", "treppe": "Holztreppe", "garderobe": "Flurgarderobe, Flurschrank",
              "bad": "Waschtisch, Waschtischunterschrank, Badschrank", "sitzbank": "Eckbank, Fensterbank zum Sitzen",
              "wand": "Paneele, Holzverkleidung, Akustikpaneele", "tuer": "Zimmertür, Schiebetür"}
MATERIAL_WORDS = {"eiche": "Eiche, Eiche geölt", "eicheraeuchert": "Räuchereiche, dunkle Eiche",
                  "eicheweiss": "Eiche weiß geölt, helle Eiche", "nuss": "Nuss, Walnuss", "esche": "Esche",
                  "kirsch": "Kirsche", "fichte": "Fichte", "multiplex": "Multiplex, Birke Multiplex, Sperrholz",
                  "altholz": "Altholz", "weiss": "Weißlack, weiß lackiert, weiß", "anthrazit": "anthrazit, dunkelgrau lackiert"}
HANDLE_WORDS = {"push": "grifflos, Push-to-open, Tip-on, ohne Griffe", "leiste": "Griffleiste, Griffmulde",
                "messing": "Messinggriffe", "schwarz": "schwarze Griffe", "leder": "Lederlaschen, Ledergriffe",
                "knopf": "Knöpfe, Holzknöpfe, Knauf"}

# deterministic safety net when the model leaves out a material / handles that the text names (longest phrase first)
SPOKEN = {"material": {"eiche weiß geölt": "eicheweiss", "eiche weiss geölt": "eicheweiss", "weiß geölte eiche": "eicheweiss",
                       "helle eiche": "eicheweiss", "eiche geräuchert": "eicheraeuchert", "räuchereiche": "eicheraeuchert",
                       "geräucherte eiche": "eicheraeuchert", "dunkle eiche": "eicheraeuchert", "eiche": "eiche",
                       "nussbaum": "nuss", "walnuss": "nuss", "nuss": "nuss", "esche": "esche", "kirschbaum": "kirsch",
                       "kirsche": "kirsch", "fichte": "fichte", "multiplex": "multiplex", "sperrholz": "multiplex",
                       "altholz": "altholz", "weißlack": "weiss", "weiß lackiert": "weiss", "lack weiß": "weiss",
                       "weiß": "weiss", "weiss": "weiss", "anthrazit": "anthrazit"},
          "handles": {"grifflos": "push", "push to open": "push", "push-to-open": "push", "tip-on": "push",
                      "ohne griffe": "push", "griffleiste": "leiste", "griffmulde": "leiste", "messing": "messing",
                      "schwarze griffe": "schwarz", "lederlasche": "leder", "ledergriff": "leder", "holzknöpf": "knopf",
                      "knöpf": "knopf", "knopf": "knopf", "knauf": "knopf"}}


def spoken(text: str, field: str) -> str | None:
    """The one catalog id the text names for material / handles (None when none or several different ones)."""
    s, taken, found = (text or "").lower(), [], set()
    for phrase in sorted(SPOKEN[field], key=len, reverse=True):
        for m in re.finditer(rf"(?<![^\W\d_]){re.escape(phrase)}(?:e|en|n|s|er|es)?(?:holz)?(?![^\W\d_])", s):
            if not any(m.start() < b and a < m.end() for a, b in taken):
                taken.append((m.start(), m.end()))
                found.add(SPOKEN[field][phrase])
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------- normalize_de

_UNITS = {"null": 0, "ein": 1, "eins": 1, "oans": 1, "oan": 1, "zwei": 2, "zwo": 2, "zwoa": 2, "zwa": 2, "drei": 3, "vier": 4,
          "via": 4, "fünf": 5, "fuenf": 5, "fümf": 5, "fimf": 5, "sechs": 6, "sieben": 7, "siebn": 7, "acht": 8,
          "neun": 9, "nein": 9}
_TEENS = {"zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15,
          "fuenfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_TENS = {}
for _v, _forms in {20: "zwanzig zwanzg zwanzich", 30: "dreißig dreissig dreißg dreissg dreißich dreissich",
                   40: "vierzig vierzg viazg vierzich", 50: "fünfzig fuenfzig fünfzg fünfzich fuchzg fuchzig fuchzich fuffzig fuffzich fuffzg",
                   60: "sechzig sechzg sechzich", 70: "siebzig siebzg siebzich siebenzig siemzg",
                   80: "achtzig achtzg achzg ochzg achtzich", 90: "neunzig neunzg neinzg neunzich"}.items():
    _TENS.update(dict.fromkeys(_forms.split(), _v))
_CONNECT = ("und", "ara", "ana", "ra", "na", "a")   # Bavarian "zwoarazwanzg" = zwei-und-zwanzig
_HUNDRED = ("hundert", "hundat", "hunderd")
# whole-word dialect / colloquial forms (before the number parser sees them)
_WORD_FORMS = {"fünfe": "fünf", "fümfe": "fünf", "fimfe": "fünf", "sechse": "sechs", "siebene": "sieben",
               "simme": "sieben", "neune": "neun", "neine": "neun", "zehne": "zehn", "elfe": "elf", "zwölfe": "zwölf",
               "viere": "vier", "dreie": "drei", "oane": "eins", "oans": "eins"}
_METER = {"m", "meter", "metern", "meta", "meda", "metà"}
_CM = {"cm", "zentimeter", "zentimetern", "zenti", "zentimeta", "zentimetä", "centimeter"}
_MM = {"mm", "millimeter", "millimetern"}
_DIM_ADJ = re.compile(r"(broad|broat|hoach|dief|diaf|breit|hoch|hohe?[nrms]?|tief|lang|lange?[nrms]?|breite?[nrms]?|tiefe?[nrms]?|höher|breiter|tiefer|länger|"
                      r"niedriger|schmaler|kürzer|flacher|dick|stark)$", re.I)
_DIM_NOUN = {"breite", "höhe", "tiefe", "länge", "korpustiefe", "raumhöhe", "deckenhöhe", "gesamtbreite",
             "gesamthöhe", "nischenbreite", "nischenhöhe", "sockelhöhe", "unterkante", "oberkante", "kniestock"}
_DIALECT_ADJ = {"broad": "breit", "broat": "breit", "hoach": "hoch", "dief": "tief", "diaf": "tief"}
_ER_NOUN = {"tiefe": "tief", "breite": "breit", "höhe": "hoch", "länge": "lang"}
_ARTICLES = {"ein", "eine", "einen", "einem", "einer", "der", "die", "das", "den", "dem"}
_MAL = {"mal", "x", "×", "*"}
_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+|\s+|.", re.S)


def _tail(s: str, table: dict) -> tuple[int, str] | None:
    for k in sorted(table, key=len, reverse=True):
        if s.endswith(k):
            return table[k], s[:-len(k)]
    return None


def _below100(s: str) -> int | None:
    """German 0-99 in one word incl. dialect forms: "fünfundvierzig", "zwoarazwanzg", "sechzg", "elf"."""
    if s in _UNITS and s not in ("ein", "oan", "nein"):
        return _UNITS[s]
    if s in _TEENS:
        return _TEENS[s]
    if s in _TENS:
        return _TENS[s]
    t = _tail(s, _TENS)
    if t:
        tens, head = t
        for c in _CONNECT:
            if head.endswith(c) and head[:-len(c)] in _UNITS and _UNITS[head[:-len(c)]] > 0:
                return tens + _UNITS[head[:-len(c)]]
    return None


def parse_number(word: str) -> float | None:
    """One German number word → value (0 … 9999), incl. "zweihundertvierzig", "anderthalb", "zweieinhalb"; else None."""
    s = _WORD_FORMS.get(word.lower(), word.lower())
    if not s.isalpha():
        return None
    if s in ("anderthalb", "eineinhalb", "einhalb"):
        return 1.5 if s != "einhalb" else 0.5
    if s.endswith("einhalb") and len(s) > 7:
        base = parse_number(s[:-7])
        return base + 0.5 if base is not None and base == int(base) and base < 1000 else None
    total = 0
    m = re.match(r"(.*?)tausend(.*)$", s)
    if m:
        k = 1 if m.group(1) in ("", "ein") else _below100(m.group(1))
        if not k:
            return None
        total, s = k * 1000, m.group(2)
        if not s:
            return float(total)
    for h in _HUNDRED:
        i = s.find(h)
        if i >= 0:
            head, s = s[:i], s[i + len(h):]
            k = 1 if head in ("", "ein", "oa", "oan") else _UNITS.get(head)
            if not k:
                return None
            total += k * 100
            if s.startswith("und"):
                s = s[3:]
            if not s:
                return float(total)
            break
    v = _below100(s)
    if v is None:
        return None
    return float(total + v)


def _metric_compound(word: str) -> int | None:
    """"einsachtzig" / "zweizwanzig" / "zwoazwanzg" / "einsfünfundachtzig": metres and centimetres run together → cm.
    Real number words never look like that ("dreizehn" is 13, so unit + "zehn" is left alone for 3-9)."""
    s = word.lower()
    for head in sorted((u for u in _UNITS if u not in ("null", "ein", "oan", "nein")), key=len, reverse=True):
        if s.startswith(head) and len(s) > len(head):
            rest = s[len(head):]
            if rest in _UNITS or (rest == "zehn" and _UNITS[head] >= 3):
                continue
            if rest.startswith(("und", "hundert", "tausend", "einhalb")):
                continue
            v = _below100(rest)
            if v is not None and v >= 10:
                return _UNITS[head] * 100 + v
    return None


def _tokens(text: str) -> list[dict]:
    out = []
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        tok = {"t": t, "k": "sp" if t.isspace() else "w" if t[0].isalpha() else "n" if t[0].isdigit() else "p"}
        if tok["k"] == "n":
            tok["v"] = float(t.replace(",", "."))
            tok["dec"] = "," in t or "." in t
            tok["word"] = False
            if tok["dec"] and len(re.split(r"[.,]", t)[1]) == 3:   # "1.000" is a thousands separator
                tok["v"], tok["dec"] = float(t.replace(".", "").replace(",", "")), False
        elif tok["k"] == "w":
            v = parse_number(t)
            mc = _metric_compound(t) if v is None else None
            if v is not None and t.lower() not in ("ein", "eine", "einen", "oan", "nein"):
                tok.update(k="n", v=v, dec=v != int(v), word=True)
            elif mc is not None:
                tok.update(k="cm", v=mc)
            elif t.lower().endswith("er") and (ev := parse_number(t[:-2])) is not None and ev >= 10:
                tok.update(k="n", v=ev, dec=False, word=True, er=True)   # "sechziger (Tiefe)" = 60er
        out.append(tok)
    # "zwo-zwanzig", "zwei-fünfzig": a hyphen between two number words is a pause, not a minus
    for i in range(1, len(out) - 1):
        if out[i]["t"] in ("-", "–") and out[i - 1].get("word") and out[i + 1].get("word"):
            out[i] = {"t": " ", "k": "sp"}
    return out


def _fmt(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:g}".replace(".", ",")


def normalize_de(text: str) -> str:
    """Deterministic German dimension normalisation: every length becomes "N cm" (whole cm), number words become digits,
    "deckenhoch / bis unter die Decke" become "raumhoch", "halbe Höhe" becomes "halbe Raumhöhe"."""
    if not isinstance(text, str) or not text.strip():
        return ""
    toks = _tokens(text)
    idx = [i for i, t in enumerate(toks) if t["k"] != "sp"]   # positions of non-space tokens
    out: list[str] = []
    pos = 0   # next raw token index to copy

    def low(j):
        return toks[idx[j]]["t"].lower() if 0 <= j < len(idx) else ""

    def kind(j):
        return toks[idx[j]]["k"] if 0 <= j < len(idx) else ""

    def tok(j):
        return toks[idx[j]] if 0 <= j < len(idx) else {}

    def glued(j):  # token j+1 follows token j without a space ("60er", "1m20")
        return j + 1 < len(idx) and idx[j + 1] == idx[j] + 1

    def emit(j0, j1, s):  # replace non-space tokens j0..j1 (inclusive) by s
        nonlocal pos
        out.extend(t["t"] for t in toks[pos:idx[j0]])
        out.append(s)
        pos = idx[j1] + 1

    def dim_after(j):  # a dimension word right after token j?
        w = tok(j + 1).get("t", "")
        return bool(w) and (bool(_DIM_ADJ.fullmatch(w)) or w.lower() in _DIM_NOUN)

    def dim_before(j):
        k = j - 1
        if low(k) in (":", "von", "auf", "ca", "etwa", "circa", "ungefähr", "rund", "knapp", "gut", "."):
            k -= 1
            if low(k) in ("von", "auf", "ca", "etwa", "circa", ":"):
                k -= 1
        return low(k) in _DIM_NOUN

    def count_use(j_first, j_next):
        """"zwei sechzig breite Schränke" = two cabinets of 60, not 2.60 m: an inflected adjective after the pair
        (attributive, before a noun) without an article in front."""
        w = low(j_next)
        return bool(re.fullmatch(r"(breite|tiefe|hohe|lange)n?", w)) and low(j_first - 1) not in _ARTICLES

    j = 0
    while j < len(idx):
        t, lw = tok(j), low(j)
        # "ein Meter achtzig", "einen halben Meter", "ein halber Meter"
        if lw in ("ein", "einen", "eine") and low(j + 1) in _METER:
            t = {"k": "n", "v": 1.0, "dec": False, "word": True}
        elif lw in ("ein", "einen", "halben", "halber") and low(j + 1) in ("halben", "halber") and low(j + 2) in _METER:
            emit(j, j + 2, "50 cm")
            j += 3
            continue
        elif lw in ("halben", "halber") and low(j + 1) in _METER:
            emit(j, j + 1, "50 cm")
            j += 2
            continue
        if t.get("k") == "cm":   # "einsachtzig" as one word
            emit(j, j, f"{t['v']} cm")
            j += 1
            continue
        if t.get("k") != "n":
            j += 1
            continue
        v = t["v"]
        nxt = low(j + 1)
        # metres: "2 m", "zwei Meter zwanzig", "1 m 20", "2,20 m", "zweieinhalb Meter"
        if nxt in _METER and not (nxt == "m" and tok(j + 2).get("t") in ("²", "³", "2", "3") and glued(j + 1)):
            b = tok(j + 2)
            if (v == int(v) and v < 10 and b.get("k") == "n" and not b.get("dec") and b["v"] < 100
                    and not (b["v"] < 10 and (tok(j + 3).get("t", "")[:1].isupper() and low(j + 3) not in _DIM_NOUN))):
                end = j + 2
                if low(end + 1) in _CM:
                    end += 1
                emit(j, end, f"{int(v * 100 + b['v'])} cm")
                j = end + 1
            else:
                emit(j, j + 1, f"{_fmt(v * 100)} cm")
                j += 2
            continue
        if nxt in _CM:
            emit(j, j + 1, f"{_fmt(v)} cm")
            j += 2
            continue
        if nxt in _MM:
            if v >= 100:
                emit(j, j + 1, f"{_fmt(round(v / 10, 1))} cm")
                j += 2
            else:
                j += 2
            continue
        # "zwei zwanzig", "eins achtzig", "zwo-zwanzig", "zwei null fünf"
        b = tok(j + 1)
        if t.get("word") and v == int(v) and 1 <= v <= 9 and lw not in ("ein",) and b.get("word") and b.get("k") == "n":
            cm, end = None, j + 1
            if b["v"] == 0 and tok(j + 2).get("word") and tok(j + 2).get("k") == "n" and tok(j + 2)["v"] < 10:
                cm, end = int(v * 100 + tok(j + 2)["v"]), j + 2
            elif 10 <= b["v"] < 100 and b["v"] == int(b["v"]):
                cm = int(v * 100 + b["v"])
            if cm is not None and not count_use(j, end + 1):
                emit(j, end, f"{cm} cm")
                j = end + 1
                continue
            if cm is not None and end == j + 1:   # "zwei sechzig breite Schränke": 2 × 60
                emit(j, end, f"{_fmt(v)} × {_fmt(b['v'])} cm")
                j = end + 1
                continue
        # decimals without a unit: "2,20" / "1.80" / "0,6" are metres when the integer part is one digit
        if t.get("dec") and not t.get("word") and v < 10 and nxt not in ("€", "euro", "%", "prozent", "uhr", "eur"):
            emit(j, j, f"{_fmt(v * 100)} cm")
            j += 1
            continue
        # "60er", "220er Breite", "sechziger Tiefe"
        if (glued(j) and nxt == "er") or t.get("er"):
            end = j + 1 if glued(j) and nxt == "er" else j
            noun = low(end + 1)
            if noun in _ER_NOUN:
                emit(j, end + 1, f"{_fmt(v)} cm {_ER_NOUN[noun]}")
                j = end + 2
            else:
                emit(j, end, f"{_fmt(v)} cm")
                j = end + 1
            continue
        # chains "220 mal 60 mal 240"
        if 10 <= v <= 2000 and nxt in _MAL and tok(j + 2).get("k") == "n" and 10 <= tok(j + 2)["v"] <= 2000:
            vals, end = [v, tok(j + 2)["v"]], j + 2
            if low(end + 1) in _MAL and tok(end + 2).get("k") == "n" and 10 <= tok(end + 2)["v"] <= 2000:
                vals.append(tok(end + 2)["v"])
                end += 2
            if low(end + 1) in _CM:
                end += 1
            emit(j, end, " × ".join(_fmt(x) for x in vals) + " cm")
            j = end + 1
            continue
        # a bare number next to a dimension word: "achtzig tief", "Tiefe 60", "220 breit"
        if 10 <= v <= 2000 and (dim_after(j) or dim_before(j)):
            emit(j, j, f"{_fmt(v)} cm")
            j += 1
            continue
        if t.get("word"):
            emit(j, j, _fmt(v))
        j += 1
    out.extend(t["t"] for t in toks[pos:])
    s = "".join(out)
    s = re.sub(r"\b(?:vom\s+boden\s+)?bis\s+(?:ganz\s+)?(?:oben\s+)?(?:unter\s+(?:die|d['’]\s?)|unters|an\s+(?:die|d['’]\s?)|zur|zu\s+der)\s*"
               r"(?:zimmer)?decke\b", "raumhoch", s, flags=re.I)
    s = re.sub(r"\bvom\s+boden\s+bis\s+(?:zur\s+)?decke\b", "raumhoch", s, flags=re.I)
    s = re.sub(r"\b(?:decken|raum|zimmer)\s?(hoch|hohe[nrms]?|hohe)\b", lambda m: "raum" + m.group(1).lower(), s, flags=re.I)
    s = re.sub(r"\bhalbe(?:r)?\s+(?:raum)?höhe\b", lambda m: "halber Raumhöhe" if m.group(0).lower().startswith("halber")
               else "halbe Raumhöhe", s, flags=re.I)
    s = re.sub(r"\bhalb\s*hoch\b", "halbe Raumhöhe", s, flags=re.I)
    s = re.sub(r"\b(broad|broat|hoach|dief|diaf)\b", lambda m: _DIALECT_ADJ[m.group(1).lower()], s, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


# ---------------------------------------------------------------- profile (duck-typed)

def _defaults(profile, type_: str | None) -> dict:
    """{d, h, w?, elev, material, material_custom, handles, full_height} for a type: the profile when it has defaults_for
    (object/module) or is a dict ({"types": {type: {...}}} or {type: {...}}), else the catalog."""
    cat = CATALOG["pieces"].get(type_, {})
    dims = cat.get("dims") or [None, None, None]
    base = {"w": None if type_ in ASK_W else dims[0], "h": dims[1], "d": dims[2], "elev": cat.get("elev", 0),
            "material": None, "material_custom": "", "handles": "push" if type_ in HANDLE_TYPES else None,
            "full_height": type_ in FULL_HEIGHT_TYPES}
    row = None
    try:
        if hasattr(profile, "defaults_for"):
            row = profile.defaults_for(type_)
        elif isinstance(profile, dict):
            types = profile.get("types") if isinstance(profile.get("types"), dict) else profile
            row = types.get(type_) if isinstance(types, dict) else None
    except Exception:
        traceback.print_exc()
    if isinstance(row, dict):
        for k in ("h", "d", "elev", "material", "material_custom", "full_height"):
            if row.get(k) is not None:
                base[k] = row[k]
        if "handles" in row:   # None from a profile = explicitly no handles
            base["handles"] = row["handles"] if row["handles"] in HANDLES else None
        if row.get("w") is not None and type_ not in ASK_W:
            base["w"] = row["w"]
        if row.get("w") is not None:
            base["w_default"] = row["w"]
    if base["material"] not in MATERIALS:
        base["material"], base["material_custom"] = None, ""
    return base


def _vocabulary(profile) -> str:
    try:
        if hasattr(profile, "vocabulary_hint"):
            return str(profile.vocabulary_hint() or "")[:1500]
        if isinstance(profile, dict) and isinstance(profile.get("aliases"), dict):
            lines = []
            for f, table in profile["aliases"].items():
                if isinstance(table, dict):
                    lines += [f"{k} → {f} {v}" for k, v in list(table.items())[:40]]
            return "\n".join(lines)[:1500]
    except Exception:
        traceback.print_exc()
    return ""


# ---------------------------------------------------------------- doc helpers

def room_height(doc: dict) -> int | None:
    """Measured ceiling height in cm: the analysed geometry's ceiling, else the calibration's room height."""
    g = doc.get("geometry") if isinstance(doc.get("geometry"), dict) else {}
    h = (g.get("ceiling") or {}).get("height") if g.get("ok") and isinstance(g.get("ceiling"), dict) else None
    if not h:
        c = doc.get("calibration") if isinstance(doc.get("calibration"), dict) else {}
        h = c.get("room_height_cm")
    try:
        h = float(h)
    except (TypeError, ValueError):
        return None
    return int(round(h)) if 150 <= h <= 600 else None


def _empty(p: dict) -> bool:
    return not p.get("type") and not (p.get("text") or "").strip() and not any(p.get(k) for k in ("w", "h", "d", "sketch", "render"))


def _pieces(doc: dict) -> list[dict]:
    return [p for p in (doc.get("pieces") or []) if isinstance(p, dict) and isinstance(p.get("id"), str)]


def _rid(taken) -> str:
    while True:
        r = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if r not in taken:
            return r


def _label(type_) -> str:
    return CATALOG["pieces"].get(type_, {}).get("de", "Möbel")


def _dim(v, lo=1, hi=2000) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).replace(",", ".")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f)) if lo <= round(f) <= hi else None


# ---------------------------------------------------------------- the model call

def _schema(ref_ids: list[str]) -> dict:
    dim = {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 2000}, {"type": "null"}]}  # bounded: else runaway digits
    ref = {"anyOf": [{"type": "string", "enum": ref_ids}, {"type": "null"}]} if ref_ids else {"type": "null"}
    piece = {"type": "object", "properties": {
        "ref": ref,
        "type": {"anyOf": [{"type": "string", "enum": list(TYPES)}, {"type": "null"}]},
        "w": dim, "h": dim, "d": dim,
        "elev": {"anyOf": [{"type": "integer", "minimum": 0, "maximum": 400}, {"type": "null"}]},
        "full_height": {"type": "boolean"}, "full_width": {"type": "boolean"},
        "material": {"anyOf": [{"type": "string", "enum": list(MATERIALS)}, {"type": "null"}]},
        "material_custom": {"type": "string", "maxLength": 80},
        "handles": {"anyOf": [{"type": "string", "enum": list(HANDLES)}, {"type": "null"}]},
        "text": {"type": "string", "maxLength": 300}, "where": {"type": "string", "maxLength": 200}},
        # nothing required: the model leaves out empty fields (≈ 35 instead of ≈ 150 tokens per piece at ~7 tokens/s)
        "required": [], "additionalProperties": False}
    return {"type": "object", "properties": {
        "pieces": {"type": "array", "items": piece, "maxItems": 6},
        "drop": {"type": "array", "items": {"type": "string", "enum": ref_ids} if ref_ids else {"type": "string"},
                 "maxItems": 4},
        "removes": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 6},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["pieces"], "additionalProperties": False}


SYSTEM = """You turn a German carpenter's (Schreiner) spoken order into structured data for a furniture visualisation \
app. The text is a speech transcript; lengths are already normalised to "N cm". Reply with compact JSON on one line \
(no spaces, no line breaks) and leave out every field that would be null, "", false or an empty list.

pieces: one entry per furniture piece the text asks to build, or per existing order piece it changes.
- New piece: no ref. Change of a piece listed under ORDER: ref = its id plus ONLY the fields that change. A \
correction without a furniture name ("nein, 80 cm tief", "breiter", "nimm Eiche", "doch grifflos") changes the \
ACTIVE piece; mentioning a listed piece by name ("das Sideboard 20 cm schmaler") changes \
that piece. Never repeat unchanged pieces.
- type: catalog id of the closest type (see the words after each id). Pieces of one kitchen are ONE kueche.
- w = width (Breite/Länge), h = height, d = depth (Tiefe), elev = underside above the floor for wall-hung pieces \
("Unterkante 140" → elev 140; "Oberkante 200 cm" with h 60 → elev 140). Whole cm. Only numbers that are said or follow \
directly from what is said (relative changes like "20 cm breiter" → current w + 20; "doppelt so breit"). Never guess \
sizes: leave them out when not said. Under a Dachschräge h = the highest point (the low side goes into text).
- full_height = true for raumhoch (no h). full_width = true for "über die ganze Wand", "auf Wandbreite", \
"von Wand zu Wand", "die ganze Nische". "halbe Raumhöhe" → h = half the room height. A wall-hung piece "raumhoch" \
with its own height (Hängeschrank bis unter die Decke, 60 cm hoch) touches the ceiling: h as said, elev = ceiling − h.
- material / handles: catalog ids, null when not mentioned. A material that is not in the list → "custom" and the \
material in material_custom (e.g. "Lack schwarz", "Kiefer"). Handles not in the list → no handles, name them in text.
- text: other construction details in short German (number of doors / drawers / shelves, open compartments, \
Dachschräge with heights, lighting, Sockel). For a change: the full new detail text, only if it changes.
- where: the spoken location phrase ("links neben dem Fenster", "in der Nische").
drop: ids of ORDER pieces the customer no longer wants ("das Sideboard brauchen wir nicht", "lösch die Garderobe"). \
"X streichen, stattdessen Y" = drop X and a new piece Y.
removes: things standing in the PHOTO that must disappear ("der alte Schrank kommt weg", "die Kommode raus"), as short \
German phrases ("der alte Schrank"). Never order pieces.
confidence: 0-1, how sure you are that you understood the request. If the text asks for no furniture and no change, \
return {"pieces":[],"confidence":0.1}."""


def _catalog_text(profile) -> str:
    lines = ["CATALOG types: " + "; ".join(f"{k} = {v['de']} ({TYPE_WORDS.get(k, '')})" for k, v in CATALOG["pieces"].items()),
             "materials: " + "; ".join(f"{k} = {v['de']} ({MATERIAL_WORDS[k]})" for k, v in CATALOG["materials"].items()
                                       if k in MATERIAL_WORDS),
             "handles: " + "; ".join(f"{k} = {v['de']} ({HANDLE_WORDS[k]})" for k, v in CATALOG["handles"].items())]
    vocab = _vocabulary(profile)
    if vocab:
        lines.append("Company vocabulary:\n" + vocab)
    return "\n".join(lines)


def _order_text(doc: dict, pieces: list[dict], active: str | None) -> str:
    live = [p for p in pieces if not _empty(p)]
    if not live:
        return "ORDER: no pieces yet."
    rows = []
    for p in live:
        dims = " × ".join(str(p.get(k) or "?") for k in ("w", "h", "d"))
        mat = (p.get("material_custom") or "custom") if p.get("material") == "custom" else p.get("material")
        s = f"- {p['id']}: {p.get('type') or '?'} ({_label(p.get('type'))}) {dims} cm, material {mat}, handles {p.get('handles')}"
        if p.get("elev"):
            s += f", elev {p['elev']}"
        if (p.get("text") or "").strip():
            s += f', text "{p["text"].strip()[:160]}"'
        if (p.get("where") or "").strip():
            s += f', where "{p["where"].strip()[:80]}"'
        rows.append(s)
    return f"ORDER (ACTIVE piece: {active}):\n" + "\n".join(rows)


def _parse(text: str) -> dict | None:
    d = judge.raw_fields(text)
    if not isinstance(d, dict) or not isinstance(d.get("pieces"), list):
        return None
    return d


def _photo(photo) -> Image.Image | None:
    if photo is None:
        return None
    try:
        if isinstance(photo, Image.Image):
            return photo
        if isinstance(photo, (bytes, bytearray)):
            with Image.open(io.BytesIO(photo)) as im:
                im.load()
                return judge.rgb(im)
    except Exception:
        traceback.print_exc()
    return None


def _ask(normalized: str, doc: dict, pieces: list[dict], active, room_h, profile, photo) -> tuple[dict | None, str]:
    """→ (model reading | None, status)."""
    live = [p["id"] for p in pieces if not _empty(p)]
    ctx = [_catalog_text(profile),
           f"ROOM: ceiling height {room_h} cm (measured)." if room_h else f"ROOM: ceiling height not measured (about {DEFAULT_ROOM_H} cm).",
           _order_text(doc, pieces, active), f'TEXT: "{normalized}"']
    img = _photo(photo)
    content = judge.content_of([("the customer's room photo", img)] if img else [], "\n\n".join(ctx))
    if not judge.status()["available"]:
        return None, "unavailable"
    if not judge.wait_memory(limit=30):
        return None, "unavailable"
    try:
        for attempt in range(2):
            raw = EXCLUSIVE(lambda: judge.ask(SYSTEM, content, _schema(live), "order", TIMEOUT, MAX_TOKENS,
                                              temperature=0.3 * attempt))
            d = _parse(raw)
            if d is not None:
                return d, "ok"
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        judge.mark_down()
        print(f"intent: model unavailable ({type(e).__name__}: {e})", flush=True)
        return None, "unavailable"
    return None, "error"


# ---------------------------------------------------------------- post-processing

def _clean(raw: dict, ids: set) -> dict:
    """The model's piece, validated field by field (never trusts enums or ranges)."""
    ref = raw.get("ref") if raw.get("ref") in ids else None
    mat = raw.get("material") if raw.get("material") in MATERIALS else None
    custom = str(raw.get("material_custom") or "").strip()[:80]
    if mat == "custom" and not custom:
        mat = None
    if mat is None and custom:
        mat = "custom"
    return {"ref": ref, "type": raw.get("type") if raw.get("type") in TYPES else None,
            "w": _dim(raw.get("w")), "h": _dim(raw.get("h")), "d": _dim(raw.get("d")),
            "elev": _dim(raw.get("elev"), 0, 400), "full_height": raw.get("full_height") is True,
            "full_width": raw.get("full_width") is True, "material": mat, "material_custom": custom if mat == "custom" else "",
            "handles": raw.get("handles") if raw.get("handles") in HANDLES else None,
            "text": str(raw.get("text") or "").strip()[:2000], "where": str(raw.get("where") or "").strip()[:500]}


def _summary(rows: list[dict], removes: list[dict], dropped: list[dict]) -> str:
    parts = []
    for r in rows:
        name = _label(r["type"]) if r["type"] else "Möbel"
        if r["ref"] and r.get("changes"):
            parts.append(f"{name}: " + ", ".join(r["changes"]))
            continue
        h = "raumhoch" if r["full_height"] else (r["h"] or "?")
        s = f"{name} {r['w'] or '?'} × {h} × {r['d'] or '?'} cm"
        if r.get("elev"):
            s += f", hängt {r['elev']} cm hoch"
        mat = r["material_custom"] if r["material"] == "custom" else CATALOG["materials"].get(r["material"], {}).get("de")
        extras = [x for x in (mat, CATALOG["handles"].get(r["handles"], {}).get("de"), r["text"]) if x]
        if extras:
            s += ", " + ", ".join(extras)
        if r["where"]:
            s += f" ({r['where']})"
        parts.append(s)
    parts += [f"{_label(p.get('type'))} wird gestrichen" for p in dropped]
    if removes:
        parts.append("aus dem Foto entfernen: " + ", ".join(r["what"] for r in removes))
    return "; ".join(parts)


_FIELD_DE = {"w": "Breite", "h": "Höhe", "d": "Tiefe", "elev": "Höhe über Boden", "material": "Material",
             "handles": "Griffe", "text": "Details", "where": "Ort", "type": "Art"}


def _change_text(f: str, v) -> str:
    if f in ("w", "h", "d", "elev"):
        return f"{_FIELD_DE[f]} {v} cm"
    if f == "material":
        return CATALOG["materials"].get(v, {}).get("de", str(v))
    if f == "material_custom":
        return str(v)
    if f == "handles":
        return CATALOG["handles"].get(v, {}).get("de", "keine Griffe")
    if f == "type":
        return f"jetzt {_label(v)}"
    return f"{_FIELD_DE.get(f, f)}: {v}"


def plan(reading: dict, doc: dict, profile=None, text: str = "") -> dict:
    """The model's reading + the order → pieces (with defaults), questions, ops, summary. Pure (no model, no disk).
    text (the normalised order) fills a material / handles the model left out for a single new piece."""
    pieces = _pieces(doc)
    by_id = {p["id"]: p for p in pieces}
    live = [p for p in pieces if not _empty(p)]
    live_ids = {p["id"] for p in live}
    active = doc.get("active_piece") if doc.get("active_piece") in live_ids else (live[-1]["id"] if live else None)
    room_h = room_height(doc)
    warnings: list[str] = []
    taken = set(by_id)

    raw_rows = [_clean(r, live_ids) for r in (reading.get("pieces") or []) if isinstance(r, dict)]
    if text and len(raw_rows) == 1 and raw_rows[0]["ref"] is None and raw_rows[0]["type"]:
        for f in ("material", "handles"):
            if raw_rows[0][f] is None and (v := spoken(text, f)):
                raw_rows[0][f] = v
    if text and len(raw_rows) == 1 and not raw_rows[0]["full_height"] and re.search(r"\braumhoch\b", text, re.I):
        r0 = raw_rows[0]   # the model sometimes files "raumhoch" under text instead of full_height
        hung = bool(CATALOG["pieces"].get(r0["type"], {}).get("elev"))
        if r0["h"] is None or (hung and r0["elev"] is None):
            r0["full_height"] = True
            r0["text"] = re.sub(r"^\s*raumhoch\s*[,;]?\s*|\s*[,;]?\s*raumhoch\s*$", "", r0["text"], flags=re.I)
    drops = list(dict.fromkeys(x for x in (reading.get("drop") or []) if x in live_ids))
    removes = [{"what": str(x).strip()[:120]} for x in (reading.get("removes") or []) if isinstance(x, str) and x.strip()][:6]

    rows: list[dict] = []
    for r in raw_rows:
        # a bare correction ("nein, achtzig tief") the model did not attach: it means the active piece
        if r["ref"] is None and r["type"] is None and active and (r["full_height"] or (not r["text"] and any(
                r[k] is not None for k in ("w", "h", "d", "elev", "material", "handles")))):
            r["ref"] = active
        if r["ref"] in drops:
            continue
        if r["ref"] and any(x["ref"] == r["ref"] for x in rows):   # two entries for one piece: merge
            prev = next(x for x in rows if x["ref"] == r["ref"])
            for k, v in r.items():
                if v not in (None, "", False):
                    prev[k] = v
            continue
        rows.append(r)

    out_pieces, questions, ops = [], [], []
    empties = [p for p in pieces if _empty(p)]
    allowed_new = projects.MAX_PIECES - (len(pieces) - len(drops)) + len(empties)   # empty pieces get filled first
    new_count = 0
    for r in rows:
        if r["ref"]:
            p = by_id[r["ref"]]
            changes, set_ops = [], []
            t_new = r["type"] if r["type"] and r["type"] != p.get("type") else None
            target = {"type": t_new or p.get("type")}
            if t_new:   # like the type tiles: untouched defaults of the old type follow the new type
                old_d, new_d = _defaults(profile, p.get("type")), _defaults(profile, t_new)
                for k in ("h", "d", "elev"):
                    if r[k] is None and (p.get(k) in (None, old_d.get(k)) or (k == "elev" and not p.get(k))):
                        if new_d.get(k) is not None and new_d.get(k) != (p.get(k) or (0 if k == "elev" else None)):
                            r[k] = new_d[k]
            if r["full_height"]:
                r["h"] = r["h"] or room_h or DEFAULT_ROOM_H
                if not room_h:
                    warnings.append(f"Raumhöhe nicht gemessen – {DEFAULT_ROOM_H} cm angenommen.")
            for f in ("type", "w", "h", "d", "elev", "material", "handles", "text", "where"):
                v = target["type"] if f == "type" else r[f]
                if f == "type" and not t_new:
                    continue
                if v in (None, "") or v == p.get(f):
                    continue
                set_ops.append({"op": "set", "path": f"pieces.{p['id']}.{f}", "value": v})
                if not (f == "material" and v == "custom"):   # the custom name is named below
                    changes.append(_change_text(f, v))
            if r["material"] == "custom" and r["material_custom"] != (p.get("material_custom") or ""):
                set_ops.append({"op": "set", "path": f"pieces.{p['id']}.material_custom", "value": r["material_custom"]})
                changes.append(r["material_custom"])
            merged = {**{k: p.get(k) for k in ("type", "w", "h", "d", "material", "material_custom", "handles", "text", "where")},
                      "elev": p.get("elev") or 0}
            for o in set_ops:
                merged[o["path"].rsplit(".", 1)[1]] = o["value"]
            row = {"ref": p["id"], "id": p["id"], **merged, "full_height": r["full_height"], "changes": changes}
            ops += set_ops
            out_pieces.append(row)
            continue
        # a new piece
        if r["type"] is None and not r["text"]:
            continue   # nothing to build
        if new_count >= allowed_new:
            warnings.append(f"Höchstens {projects.MAX_PIECES} Möbel pro Auftrag – {_label(r['type'])} nicht übernommen.")
            continue
        new_count += 1
        dfl = _defaults(profile, r["type"])
        if r["full_height"] and r["h"] and r["elev"] is None and dfl.get("elev"):
            # a wall-hung piece "bis unter die Decke" with its own height: top edge at the ceiling
            r["elev"], r["full_height"] = max(0, (room_h or DEFAULT_ROOM_H) - r["h"]), False
        full_h = r["full_height"] or (r["h"] is None and dfl["full_height"])
        if r["h"] is None and full_h:
            r["h"] = room_h or DEFAULT_ROOM_H
            if not room_h:
                warnings.append(f"Raumhöhe nicht gemessen – {DEFAULT_ROOM_H} cm angenommen.")
        for k in ("h", "d", "w"):
            if r[k] is None and dfl.get(k) is not None:
                r[k] = dfl[k]
        if r["elev"] is None:
            r["elev"] = dfl.get("elev") or 0
        if r["material"] is None:
            r["material"], r["material_custom"] = dfl["material"] or "eiche", dfl.get("material_custom") or ""
            if r["material"] != "custom":
                r["material_custom"] = ""
        if r["handles"] is None:
            r["handles"] = dfl["handles"]
        row = {**r, "ref": None, "full_height": bool(full_h)}
        idx = len(out_pieces)
        if r["type"] is None:
            questions.append({"piece": idx, "field": "type", "question_de": "Was für ein Möbel soll es werden?", "default": None})
        elif r["w"] is None and not r["full_width"] and r["type"] in ASK_W:
            dw = dfl.get("w_default") or (CATALOG["pieces"][r["type"]].get("dims") or [None])[0]
            questions.append({"piece": idx, "field": "w", "question_de": W_QUESTION.get(r["type"], "Wie breit soll es werden?"),
                              "default": dw})
        value = {"type": r["type"], "text": r["text"], "material": r["material"], "material_custom": r["material_custom"],
                 "handles": r["handles"], "w": r["w"], "h": r["h"], "d": r["d"], "where": r["where"]}
        if r["elev"]:
            value["elev"] = r["elev"]
        if empties:
            p = empties.pop(0)
            row["id"] = p["id"]
            ops += [{"op": "set", "path": f"pieces.{p['id']}.{k}", "value": v} for k, v in value.items()
                    if v != p.get(k) and not (v in (None, "") and p.get(k) in (None, ""))]
        else:
            pid = _rid(taken)
            taken.add(pid)
            row["id"] = pid
            ops.append({"op": "add_piece", "value": {"id": pid, **value,
                                                     "placement": {"mode": "none", "pose": None, "box": None}}})
        out_pieces.append(row)

    # drops: removing the last piece needs a replacement (apply_ops keeps one piece); an add can be that replacement
    remaining = len(pieces) - len(drops)
    drop_ops = []
    for i, pid in enumerate(drops):
        op = {"op": "remove_piece", "id": pid}
        if remaining == 0 and i == len(drops) - 1:
            add = next((o for o in ops if o["op"] == "add_piece"), None)
            if add:
                ops.remove(add)
                op["replacement"] = add["value"]
            else:
                rid = _rid(taken)
                op["replacement"] = {"id": rid, "type": None, "material": by_id[pid].get("material") or "eiche",
                                     "handles": by_id[pid].get("handles"),
                                     "placement": {"mode": "none", "pose": None, "box": None}}
        drop_ops.append(op)
    ops = drop_ops + ops

    pieces_out = [{k: row.get(k) for k in ("ref", "id", "type", "w", "h", "d", "elev", "material", "material_custom",
                                           "handles", "text", "where", "full_height")} for row in out_pieces]
    for row, po in zip(out_pieces, pieces_out):
        po["elev"] = po["elev"] or None
        if row.get("ref"):
            po["changes"] = row.get("changes") or []
    dropped = [by_id[x] for x in drops]
    summary = _summary(out_pieces, removes, dropped)
    if not out_pieces and not drops and not removes and not warnings:
        questions.append({"piece": None, "field": "type", "question_de": "Was soll gebaut oder geändert werden?", "default": None})
    conf = reading.get("confidence")
    conf = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.5
    conf = max(0.0, min(1.0, conf)) * (0.8 if questions else 1.0)
    return {"summary_de": summary, "pieces": pieces_out, "removes": removes,
            "drops": [{"id": p["id"], "label": _label(p.get("type"))} for p in dropped], "questions": questions,
            "ops": ops, "warnings": list(dict.fromkeys(warnings)), "confidence": round(conf, 2)}


def _result(status: str, normalized: str, t0: float, **kw) -> dict:
    base = {"status": status, "text": normalized, "summary_de": "", "pieces": [], "removes": [], "drops": [],
            "questions": [], "ops": [], "warnings": [], "confidence": 0.0}
    return {**base, **kw, "seconds": round(time.time() - t0, 1)}


def understand(text: str, doc: dict, profile=None, photo=None) -> dict:
    """German order text (+ optional room photo) → structured pieces, questions and apply_ops-ready ops. Never raises:
    a stopped model gives status "unavailable", an unreadable answer status "error"."""
    t0 = time.time()
    normalized = normalize_de(text or "")
    if not normalized:
        return _result("ok", "", t0, questions=[{"piece": None, "field": "type", "default": None,
                                                 "question_de": "Was soll gebaut oder geändert werden?"}])
    doc = doc if isinstance(doc, dict) else {}
    try:
        pieces = _pieces(doc)
        live = [p["id"] for p in pieces if not _empty(p)]
        active = doc.get("active_piece") if doc.get("active_piece") in live else (live[-1] if live else None)
        reading, status = _ask(normalized, doc, pieces, active, room_height(doc), profile, photo)
        if reading is None:
            return _result(status, normalized, t0)
        return _result("ok", normalized, t0, **plan(reading, doc, profile, normalized))
    except Exception:
        traceback.print_exc()
        return _result("error", normalized, t0)


# ---------------------------------------------------------------- HTTP

def _profile():
    """The company profile module when it is installed (profile.py, Phase 5); None → built-in defaults."""
    try:
        import profile as prof
        return prof if hasattr(prof, "defaults_for") else None
    except Exception:
        return None


@router.post("/api/workshop/{pid}/understand")
def understand_route(pid: str, body: dict, request: Request):
    """{text, apply?: bool, base_rev?: int} → understand() result; with apply the ops go through projects.apply_ops
    (base_rev required, conflicts come back as its 409) and the saved doc is returned as "doc" (+ "rev")."""
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "Bitte beschreiben Sie, was gebaut werden soll.")
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"Text zu lang (höchstens {MAX_TEXT} Zeichen).")
    apply = body.get("apply") is True
    base_rev = body.get("base_rev")
    if apply and (not isinstance(base_rev, int) or isinstance(base_rev, bool)):
        raise HTTPException(400, "Ungültige Änderung: base_rev.")
    doc = projects.load("workshop", pid)
    ap = body.get("active_piece")
    if isinstance(ap, str) and any(p.get("id") == ap for p in doc.get("pieces") or []):
        doc = {**doc, "active_piece": ap}   # the piece open in the Möbel step gets bare corrections
    res = understand(text, doc, _profile())
    if apply and res["status"] == "ok" and res["ops"]:
        new = projects.apply_ops(pid, base_rev, res["ops"], projects._who(request))
        res["doc"], res["rev"] = new, new["rev"]
    return res
