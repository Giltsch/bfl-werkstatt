"""Print PDFs: a book interior with bleed plus a wrap-around cover for print-on-demand (Amazon KDP's paperback
spec), and the one-page customer sheet for the workshop page.

KDP paperback geometry: interior pages are trim + 0.125" wide (bleed on the outside edge) and trim + 0.25" high;
the cover is back + spine + front with 0.125" bleed all round; the spine width is page count x paper thickness.
"""

import io
import subprocess
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps, ImageStat
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

FONTS = Path(__file__).parent / "fonts"
for name in ("Garamond-Regular", "Garamond-Italic", "Garamond-SemiBold", "Nunito-Regular", "Nunito-Bold", "Fraunces-Display"):
    pdfmetrics.registerFont(TTFont(name, str(FONTS / f"{name}.ttf")))

IN = 72  # points per inch
BLEED = 0.125
DPI = 300
TRIMS = {  # KDP paperback trim sizes that suit picture books, in inches (width, height)
    "8.5x8.5": (8.5, 8.5), "8x8": (8, 8), "8x10": (8, 10), "8.5x11": (8.5, 11), "7x10": (7, 10), "6x9": (6, 9),
}
PAPER = {  # inches per page, from KDP's spine-width table
    "premium-color": ("Premium color (white)", 0.002347),
    "standard-color": ("Standard color (white)", 0.002252),
    "white": ("Black & white, white paper", 0.002252),
    "cream": ("Black & white, cream paper", 0.0025),
}
LAYOUTS = {
    "band": "Full-bleed picture, text on a soft band",
    "below": "Picture on top, text below",
    "facing": "Text left, full picture right",
    "image": "Photo book: full pictures, small captions",
}
TYPE = {  # body, emphasis, title
    "classic": ("Garamond-Regular", "Garamond-Italic", "Fraunces-Display"),
    "friendly": ("Nunito-Regular", "Nunito-Bold", "Fraunces-Display"),
}
BELOW_SHARE = 0.64  # "below" layout: the picture takes this share of the trim height (plus top bleed)
MIN_PAGES = 24
INK = HexColor("#2b2622")


class ExportError(Exception):
    pass


def options():
    return {
        "trims": {k: {"w": w, "h": h} for k, (w, h) in TRIMS.items()},
        "paper": {k: v[0] for k, v in PAPER.items()},
        "layouts": LAYOUTS, "fonts": list(TYPE), "bleed": BLEED, "below_share": BELOW_SHARE, "dpi": DPI,
    }


# ---------------- helpers ----------------

class Images:
    """Loads, crops and resamples pictures to 300 dpi for a box, and notes when the source is too small."""

    def __init__(self, root: Path):
        self.root, self.warnings = root, []

    def open(self, rel: str) -> Image.Image:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents or not p.exists():
            raise ExportError(f"Missing image: {rel}")
        return ImageOps.exif_transpose(Image.open(p)).convert("RGB")

    def fit(self, rel: str, w_pt: float, h_pt: float, label: str) -> ImageReader:
        img = self.open(rel)
        target = w_pt / h_pt
        if img.width / img.height > target:  # crop the sides
            cw = round(img.height * target)
            img = img.crop(((img.width - cw) // 2, 0, (img.width + cw) // 2, img.height))
        else:
            ch = round(img.width / target)
            img = img.crop((0, (img.height - ch) // 2, img.width, (img.height + ch) // 2))
        dpi = img.width / (w_pt / IN)
        if dpi < 180:
            self.warnings.append(f"{label}: only {dpi:.0f} dpi at print size; use “Upscale for print” on it.")
        px = (round(w_pt / IN * DPI), round(h_pt / IN * DPI))
        img = img.resize(px, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92, subsampling=0, dpi=(DPI, DPI))
        buf.seek(0)
        return ImageReader(buf)


def para(text: str, font: str, size: float, color=INK, align=TA_CENTER, leading=1.32) -> Paragraph:
    style = ParagraphStyle("p", fontName=font, fontSize=size, leading=size * leading, textColor=color, alignment=align)
    html = "<br/>".join(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for line in text.strip().split("\n"))
    return Paragraph(html, style)


def draw_para(c: Canvas, p: Paragraph, x: float, y_top: float, w: float) -> float:
    """Draws p with its top at y_top; returns the height used."""
    _, h = p.wrap(w, 10_000)
    p.drawOn(c, x, y_top - h)
    return h


def fit_para(text, font, size, w, max_h, **kw) -> tuple[Paragraph, float]:
    """The largest size up to `size` at which the text fits in max_h."""
    while True:
        p = para(text, font, size, **kw)
        _, h = p.wrap(w, 10_000)
        if h <= max_h or size <= 7:
            return p, h
        size *= 0.93


def run_pdftoppm(pdf: Path, prefix: Path, dpi: int, first=None, last=None):
    cmd = ["pdftoppm", "-r", str(dpi), "-jpeg", "-jpegopt", "quality=80"]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    subprocess.run(cmd + [str(pdf), str(prefix)], check=True, capture_output=True)


# ---------------- book ----------------

class Interior:
    def __init__(self, book: dict, imgs: Images, path: Path):
        tw, th = TRIMS.get(book.get("trim"), TRIMS["8.5x8.5"])
        self.tw, self.th = tw * IN, th * IN
        self.pw, self.ph = (tw + BLEED) * IN, (th + 2 * BLEED) * IN
        self.b = BLEED * IN
        self.margin = 0.5 * IN  # text keeps this far from the trim (KDP asks for at least 0.375")
        self.body, self.em, self.title = TYPE.get(book.get("font"), TYPE["classic"])
        self.size = 15 if tw >= 8 else 13
        self.imgs = imgs
        self.c = Canvas(str(path), pagesize=(self.pw, self.ph))
        self.c.setTitle(book.get("title") or "Untitled")
        self.c.setAuthor(book.get("author") or "")
        self.n = 0

    def trim_x(self) -> float:
        """x of the trim's left edge on the current page (recto pages have their bleed on the right)."""
        return 0 if self.n % 2 == 1 else self.b

    def next(self):
        if self.n:
            self.c.showPage()
        self.n += 1

    def full_picture(self, rel, label):
        self.c.drawImage(self.imgs.fit(rel, self.pw, self.ph, label), 0, 0, self.pw, self.ph)

    def text_block(self, text, y_center=None, size=None, font=None):
        x = self.trim_x() + self.margin
        w = self.tw - 2 * self.margin
        p, h = fit_para(text, font or self.body, size or self.size * 1.15, w, self.th - 2 * self.margin)
        yc = y_center if y_center is not None else self.b + self.th / 2
        draw_para(self.c, p, x, yc + h / 2, w)

    def band(self, text, top=False):
        """Text on a rounded, semi-opaque band inside the safe area."""
        if not text.strip():
            return
        pad = 0.18 * IN
        x = self.trim_x() + self.margin
        w = self.tw - 2 * self.margin
        p, h = fit_para(text, self.body, self.size, w - 2 * pad, self.th * 0.3)
        bh = h + 2 * pad
        y = self.b + self.th - self.margin - bh if top else self.b + self.margin
        c = self.c
        c.saveState()
        c.setFillColor(Color(1, 0.99, 0.97, alpha=0.86))
        c.roundRect(x, y, w, bh, 0.14 * IN, stroke=0, fill=1)
        c.restoreState()
        draw_para(c, p, x + pad, y + bh - pad, w - 2 * pad)

    def caption(self, text):
        if not text.strip():
            return
        x = self.trim_x() + self.margin
        w = self.tw - 2 * self.margin
        p, h = fit_para(text, self.em, self.size * 0.75, w - 0.24 * IN, self.th * 0.15, color=HexColor("#ffffff"))
        c = self.c
        c.saveState()
        c.setFillColor(Color(0, 0, 0, alpha=0.45))
        c.roundRect(x, self.b + self.margin, w, h + 0.2 * IN, 0.08 * IN, stroke=0, fill=1)
        c.restoreState()
        draw_para(c, p, x + 0.12 * IN, self.b + self.margin + h + 0.1 * IN, w - 0.24 * IN)


def build_book(book: dict, root: Path, out: Path) -> dict:
    pages = [p for p in book.get("pages", []) if p.get("image") or (p.get("text") or "").strip()]
    if not pages:
        raise ExportError("The book has no pages yet.")
    missing = [i + 1 for i, p in enumerate(book.get("pages", [])) if not p.get("image")]
    imgs = Images(root)
    layout = book.get("layout") if book.get("layout") in LAYOUTS else "band"
    title = book.get("title") or "Untitled"
    author = book.get("author") or ""
    german = (book.get("language") or "").lower().startswith(("de", "ger"))

    interior_pdf = out / "interior.pdf"
    it = Interior(book, imgs, interior_pdf)
    c = it.c

    # title page (1) and copyright page (2)
    it.next()
    yc = it.b + it.th * 0.62
    tp, th_ = fit_para(title, it.title, 34, it.tw - 2 * it.margin, it.th * 0.3)
    draw_para(c, tp, it.trim_x() + it.margin, yc + th_, it.tw - 2 * it.margin)
    if book.get("subtitle"):
        draw_para(c, para(book["subtitle"], it.em, it.size * 1.1), it.trim_x() + it.margin, yc - 0.25 * IN, it.tw - 2 * it.margin)
    if author:
        draw_para(c, para(author, it.body, it.size), it.trim_x() + it.margin, it.b + it.th * 0.2, it.tw - 2 * it.margin)
    it.next()
    year = date.today().year
    ai_note = ("Die Illustrationen wurden mit Hilfe von KI-Bildgenerierung erstellt." if german
               else "The illustrations were created with the help of AI image generation.")
    rights = "Alle Rechte vorbehalten." if german else "All rights reserved."
    imprint = book.get("imprint") or f"© {year} {author}\n{rights}\n{ai_note}"
    p = para(imprint, it.body, 9, align=TA_LEFT, leading=1.45)
    _, h = p.wrap(it.tw - 2 * it.margin, 10_000)
    p.drawOn(c, it.trim_x() + it.margin, it.b + it.margin)

    # facing layout: text must land on left (even) pages, so page 3 is a dedication or blank
    if layout == "facing" or book.get("dedication"):
        it.next()
        if book.get("dedication"):
            it.text_block(book["dedication"], y_center=it.b + it.th * 0.6, font=it.em)

    for i, pg in enumerate(pages, 1):
        text, rel, label = pg.get("text") or "", pg.get("image"), f"Page {i}"
        if layout == "facing":
            if it.n % 2 == 0:  # the next page would be a right page: leave one blank so text sits on the left
                it.next()
            it.next()
            it.text_block(text)
            it.next()
            if rel:
                it.full_picture(rel, label)
        elif layout == "below":
            it.next()
            ih = it.th * BELOW_SHARE + it.b
            if rel:
                c.drawImage(imgs.fit(rel, it.pw, ih, label), 0, it.ph - ih, it.pw, ih)
            it.text_block(text, y_center=it.b + it.th * (1 - BELOW_SHARE) / 2 + 0.05 * IN, size=it.size)
        else:
            it.next()
            if rel:
                it.full_picture(rel, label)
            if layout == "image":
                it.caption(text)
            else:
                it.band(text, top=pg.get("text_pos") == "top")

    it.next()
    end = book.get("end_text") or ("Ende" if german else "The End")
    it.text_block(end, size=it.size * 1.8, font=it.title)
    story_pages = it.n
    padded = 0
    while it.n < MIN_PAGES or it.n % 2:
        it.next()
        padded += 1
    c.save()
    total = it.n

    cover_pdf = out / "cover.pdf"
    spine = build_cover(book, imgs, cover_pdf, total)

    previews = []
    run_pdftoppm(interior_pdf, out / "p", 28, 1, min(total, 40))
    previews = sorted(out.glob("p-*.jpg"))
    run_pdftoppm(cover_pdf, out / "cover", 40)
    cover_preview = sorted(out.glob("cover*.jpg"))

    warnings = list(dict.fromkeys(imgs.warnings))
    if missing:
        warnings.insert(0, f"Pages without a picture: {', '.join(map(str, missing))}.")
    if padded > 2:
        warnings.append(f"{padded} blank pages were added at the end to reach {MIN_PAGES} pages (KDP's minimum). "
                        "More story pages look better.")
    if not book.get("cover", {}).get("image"):
        warnings.append("The cover has no picture yet.")
    tw, th = TRIMS.get(book.get("trim"), TRIMS["8.5x8.5"])
    return {
        "pages": total, "story_pages": story_pages, "padded": padded, "spine_in": round(spine, 4),
        "trim": f"{tw} x {th} in", "interior_size": f"{tw + BLEED} x {th + 2 * BLEED} in",
        "cover_size": f"{2 * tw + spine + 2 * BLEED:.3f} x {th + 2 * BLEED} in", "spine_text": total >= 79,
        "warnings": warnings,
        "files": {"interior": interior_pdf, "cover": cover_pdf, **({"cover_preview": cover_preview[0]} if cover_preview else {})},
        "previews": previews,
    }


def build_cover(book: dict, imgs: Images, path: Path, pages: int) -> float:
    tw, th = TRIMS.get(book.get("trim"), TRIMS["8.5x8.5"])
    spine = pages * PAPER.get(book.get("paper"), PAPER["premium-color"])[1]
    b = BLEED * IN
    W, H = (2 * tw + spine + 2 * BLEED) * IN, (th + 2 * BLEED) * IN
    front_x = b + (tw + spine) * IN
    body, em, title_font = TYPE.get(book.get("font"), TYPE["classic"])
    cover = book.get("cover") or {}
    c = Canvas(str(path), pagesize=(W, H))
    c.setTitle(f"{book.get('title') or 'Untitled'} (cover)")

    # back and spine colour: from the cover picture's left edge, darkened, unless one is set
    bg = HexColor(cover["color"]) if cover.get("color") else None
    if cover.get("image"):
        if bg is None:
            img = imgs.open(cover["image"])
            r, g, bl = ImageStat.Stat(img.crop((0, 0, max(1, img.width // 12), img.height))).mean
            bg = Color(r / 255 * 0.7, g / 255 * 0.7, bl / 255 * 0.7)
        c.setFillColor(bg)
        c.rect(0, 0, front_x, H, stroke=0, fill=1)
        c.drawImage(imgs.fit(cover["image"], W - front_x, H, "Cover"), front_x, 0, W - front_x, H)
    else:
        bg = bg or HexColor("#33414d")
        c.setFillColor(bg)
        c.rect(0, 0, W, H, stroke=0, fill=1)
    light = (0.299 * bg.red + 0.587 * bg.green + 0.114 * bg.blue) < 0.55
    fg = HexColor("#fbf7f0") if light else INK

    margin = 0.5 * IN
    title = book.get("title") or "Untitled"
    # front: title in the upper third, author at the bottom, with a soft shadow for legibility on pictures
    if not cover.get("title_in_art"):
        fw = tw * IN - 2 * margin
        for dx, dy, col in ((1.2, -1.2, Color(0, 0, 0, alpha=0.45)), (0, 0, HexColor(cover.get("title_color") or "#fffaf0"))):
            tp, h = fit_para(title, title_font, 44, fw, th * IN * 0.3, color=col, leading=1.12)
            draw_para(c, tp, front_x + margin + dx, H - b - margin + dy, fw)
            if book.get("subtitle"):
                draw_para(c, para(book["subtitle"], em, 16, color=col), front_x + margin + dx, H - b - margin - h - 6 + dy, fw)
            if book.get("author"):
                draw_para(c, para(book["author"], body, 16, color=col), front_x + margin + dx, b + margin + 22 + dy, fw)

    # back: blurb above the barcode area (KDP prints the barcode bottom right, ~2" x 1.2")
    blurb = cover.get("blurb") or book.get("blurb") or ""
    if blurb:
        bw = tw * IN - 2 * margin
        p, h = fit_para(blurb, body, 15, bw, th * IN - 2 * margin - 1.6 * IN, color=fg, align=TA_LEFT, leading=1.45)
        draw_para(c, p, b + margin, H - b - margin - 0.4 * IN, bw)

    # spine text needs at least 79 pages at KDP
    if pages >= 79:
        c.saveState()
        c.translate(b + tw * IN + spine * IN / 2, H / 2)
        c.rotate(-90)
        size = min(14, spine * IN * 0.45)
        c.setFont(title_font, size)
        c.setFillColor(fg)
        label = title + (f"  ·  {book['author']}" if book.get("author") else "")
        c.drawCentredString(0, -size * 0.35, label)
        c.restoreState()
    c.save()
    return spine


# ---------------- workshop sheet ----------------
# Kundenblatt v2: customer-facing, so the only free text on it is the customer note of a version; internal notes,
# edit instructions, verdicts, captions, prompts and seeds never reach the page. A LAN link is never printed either:
# callers pass `link` only for a public address.

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing

AI_NOTE = ("Unverbindliche Visualisierung, mit KI erstellt. Farben, Holzmaserung, Proportionen und Details können vom "
           "fertigen Möbelstück abweichen; maßgeblich sind Angebot und Werkplanung.")
MEASURES_NOTE = "Maße laut Aufmaß; Preise und Ausführung siehe Angebot."
PRIVACY_NOTE = "Datenschutz: Fotos und Daten bleiben bei uns in der Werkstatt und dienen nur Ihrem Angebot."
APPROVAL_HEAD = "Gewählter Entwurf (keine Auftragserteilung)"
APPROVED_STAMP = "Freigegebener Entwurf"
QR_CAPTION = "Online ansehen und freigeben"


def rgb(img: Image.Image) -> Image.Image:
    """EXIF-transposed RGB; transparency goes on white (a plain convert turns transparent CAD screenshots black)."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    return img.convert("RGB")


class SheetImages(Images):
    def open(self, rel: str) -> Image.Image:
        p = (self.root / rel).resolve() if isinstance(rel, str) else None
        if p is None or self.root.resolve() not in p.parents or not p.exists():
            raise ExportError(f"Missing image: {rel}")
        return rgb(Image.open(p))

    def exists(self, rel) -> bool:
        if not isinstance(rel, str):
            return False
        p = (self.root / rel).resolve()
        return self.root.resolve() in p.parents and p.is_file()


def dims_text(w, h, d) -> str:
    """"180 × 250 × 60 cm"; missing values as "–", nothing known → "–"."""
    vals = [str(int(x)) if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 else
            (x.strip() if isinstance(x, str) and x.strip().isdigit() else None) for x in (w, h, d)]
    if not any(vals):
        return "–"
    return " × ".join(v or "–" for v in vals) + " cm"


def sheet_rows(doc: dict, v: dict) -> list[tuple[str, str, str, str]]:
    """Per-piece rows: the variant's snapshot, else the doc's pieces, else (v1 docs) the variant's summary."""
    if isinstance(v.get("pieces"), list) and v["pieces"]:
        src = v["pieces"]
    elif isinstance(doc.get("pieces"), list) and doc["pieces"]:
        ids = v.get("piece_ids") or []
        src = [p for p in doc["pieces"] if p.get("id") in ids] or doc["pieces"]
    else:
        dims = dims_text(doc.get("w"), doc.get("h"), doc.get("d"))
        if dims == "–" and doc.get("dims"):
            dims = str(doc["dims"])
        return [(v.get("piece") or doc.get("piece_label") or "", v.get("material") or "", v.get("handles") or "", dims)]
    rows = []
    for p in src[:4]:
        mat = p.get("material_label") or (p.get("material_custom") if p.get("material") == "custom" else "")
        rows.append((p.get("label") or "", mat or "", p.get("handles_label") or "", dims_text(p.get("w"), p.get("h"), p.get("d"))))
    return rows


def clip(text: str, font: str, size: float, w: float) -> str:
    text = str(text)
    if pdfmetrics.stringWidth(text, font, size) <= w:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > w:
        text = text[:-1]
    return text.rstrip() + "…"


def company_line(company: dict) -> str:
    """The contact line: `contact` if set, else street · zip city · phone · email · web (same rule as projects)."""
    company = company or {}
    if str(company.get("contact") or "").strip():
        return str(company["contact"]).strip()
    city = " ".join(str(company.get(k) or "").strip() for k in ("zip", "city")).strip()
    parts = [str(company.get("street") or "").strip(), city, *(str(company.get(k) or "").strip() for k in ("phone", "email", "web"))]
    return " · ".join(p for p in parts if p)


def before_of(doc: dict, v: dict) -> tuple[str | None, str, str]:
    """(before image, before label, after label). The customer compares with what they know: the room photo for room
    designs and everything edited from them (wood swaps, changes, upscales), the sketch for renders, nothing for free
    designs. A frozen variant (version snapshot) carries its own `before`/labels."""
    if "before" in v:
        return v.get("before"), v.get("before_label") or "Vorher", v.get("after_label") or "Nachher"
    by_id = {x.get("id"): x for x in (doc.get("variants") or []) + (doc.get("trash") or []) if isinstance(x, dict)}
    root, seen = v, {v.get("id")}
    while root.get("from") in by_id and root["from"] not in seen:
        seen.add(root["from"])
        root = by_id[root["from"]]
    kind = root.get("kind") or ("room" if doc.get("room") else "free")
    if kind == "render" or (kind in ("edit", "material", "upscale") and root.get("sketch")):
        pieces = {p.get("id"): p for p in doc.get("pieces") or [] if isinstance(p, dict)}
        sketch = root.get("sketch") or (pieces.get((root.get("piece_ids") or [None])[0]) or {}).get("sketch")
        return sketch, "Skizze", "Entwurf"
    if kind == "free":
        return None, "", "Entwurf"
    return root.get("room") or doc.get("room"), "Vorher", "Nachher"


def chosen_variants(doc: dict, version: dict | None = None, variant_ids: list | None = None) -> tuple[list[dict], list]:
    """(variants in print order, requested ids that do not exist). Order: the version's ids → variant_ids → approved,
    then favourites → selected (old docs) → newest. The approved variant goes first, except in a version: there the
    numbers must stay those of the customer link and the approval block („Entwurf 1“ is the version's first id)."""
    variants = sorted((v for v in doc.get("variants") or [] if isinstance(v, dict)), key=lambda v: str(v.get("image")), reverse=True)
    by_id = {v.get("id"): v for v in variants}
    approved = (doc.get("approved") or {}).get("variant_id") if isinstance(doc.get("approved"), dict) else None
    ids = (version or {}).get("variant_ids") or variant_ids
    if ids:
        chosen, missing = [by_id[i] for i in dict.fromkeys(ids) if i in by_id], [i for i in ids if i not in by_id]
    else:
        missing = []
        chosen = ([by_id[approved]] if approved in by_id else []) + [v for v in variants if v.get("fav") and v.get("id") != approved]
        chosen = chosen or [v for v in variants if v.get("id") == doc.get("selected")] or variants[:1]
    if not (version or {}).get("variant_ids"):
        chosen.sort(key=lambda v: v.get("id") != approved)  # stable: approved first, the rest keeps its order
    return chosen, missing


def qr_drawing(link: str, size: float) -> Drawing:
    w = QrCodeWidget(link)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    return d


def fmt_date(iso: str | None) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return date.today().strftime("%d.%m.%Y")


def build_sheet(doc: dict, root: Path, out: Path, skipped: list | None = None, *, company: dict | None = None,
                version: dict | None = None, link: str | None = None, variant_ids: list | None = None,
                filename: str | None = None) -> Path:
    """Customer sheet: one A4 landscape page per chosen variant (see chosen_variants). Variants whose image is gone
    are skipped (ids → skipped). With a version: ref/version header, the version's customer note and, until a variant
    is approved, the approval block (QR code only for a public `link`). Never deletes anything in `out`."""
    imgs = SheetImages(root)
    skipped = [] if skipped is None else skipped
    chosen, missing = chosen_variants(doc, version, variant_ids)
    skipped.extend(missing)
    if not chosen:
        raise ExportError("Noch keine Visualisierung vorhanden.")
    for v in chosen:
        if not imgs.exists(v.get("image")):
            skipped.append(v.get("id"))
    chosen = [v for v in chosen if v.get("id") not in skipped]
    if not chosen:
        raise ExportError("Die Bilder der gewählten Varianten fehlen.")
    company = company or doc.get("company") or {}
    approved = doc.get("approved") if isinstance(doc.get("approved"), dict) else None
    approved_id = approved and approved.get("variant_id")
    note = str((version or {}).get("customer_note") or "").strip()
    block = bool(version) and not any(v.get("id") == approved_id for v in chosen)
    path = out / (filename or "kundenblatt.pdf")
    W, H = landscape(A4)
    c = Canvas(str(path), pagesize=(W, H))
    c.setTitle(doc.get("title") or "Visualisierung")
    c.setAuthor(company.get("name") or "")
    m, bottom = 36, 48
    green = HexColor(company.get("color") or "#3d5a4a")
    contact = doc.get("contact") if isinstance(doc.get("contact"), dict) else {}
    place = ", ".join(x for x in (str(contact.get("street") or "").strip(),
                                  " ".join(str(contact.get(k) or "").strip() for k in ("zip", "city")).strip()) if x)
    day = fmt_date(version.get("created")) if version else date.today().strftime("%d.%m.%Y")
    sub = " · ".join(x for x in (doc.get("customer"), place, day) if x)
    refs = " · ".join(x for x in (f"Vorgang {doc['ref']}" if doc.get("ref") else "",
                                  f"zum Angebot Nr. {doc['ext_ref']}" if doc.get("ext_ref") else "",
                                  f"Version {version['n']}" if version and version.get("n") else "") if x)

    def header():
        c.setFillColor(green)
        c.rect(0, H - 6, W, 6, stroke=0, fill=1)
        x = m
        if company.get("logo"):
            try:
                logo = imgs.open(company["logo"])
                lh = 34
                lw = lh * logo.width / logo.height
                c.drawImage(ImageReader(logo), m, H - m - lh + 6, lw, lh, mask="auto")
                x = m + lw + 12
            except ExportError:
                pass
        c.setFillColor(INK)
        c.setFont("Garamond-SemiBold", 18)
        c.drawString(x, H - m - 10, clip(company.get("name") or "", "Garamond-SemiBold", 18, W * 0.45 - x))
        c.setFont("Garamond-Regular", 10)
        c.drawString(x, H - m - 24, clip(company_line(company), "Garamond-Regular", 10, W * 0.5 - x))
        c.setFont("Fraunces-Display", 18)
        c.drawRightString(W - m, H - m - 10, clip(doc.get("title") or "Visualisierung", "Fraunces-Display", 18, W * 0.45))
        c.setFont("Garamond-Regular", 10.5)
        c.drawRightString(W - m, H - m - 24, clip(sub, "Garamond-Regular", 10.5, W * 0.45))
        if refs:
            c.drawRightString(W - m, H - m - 37, clip(refs, "Garamond-Regular", 10.5, W * 0.45))

    sender = " · ".join(x for x in (str(company.get("name") or "").strip(), company_line(company)) if x)

    def footer():
        if sender:  # who sent it, on every page (the header may be cut off when printed)
            c.setFont("Garamond-SemiBold", 8.5)
            c.setFillColor(green)
            c.drawString(m, 35, clip(sender, "Garamond-SemiBold", 8.5, W - 2 * m))
        c.setFont("Garamond-Italic", 8.5)
        c.setFillColor(HexColor("#6b645c"))
        c.drawString(m, 24, AI_NOTE)
        c.drawString(m, 13, f"{MEASURES_NOTE}  {PRIVACY_NOTE}")

    def picture(rel, x, y, w, h, label) -> tuple[float, float, float, float]:
        im = imgs.open(rel)
        s = min(w / im.width, h / im.height)
        dw, dh = im.width * s, im.height * s
        px, py = x + (w - dw) / 2, y + (h - dh) / 2
        c.drawImage(imgs.fit(rel, dw, dh, label), px, py, dw, dh)
        c.setFont("Garamond-SemiBold", 10)
        tw_ = pdfmetrics.stringWidth(label, "Garamond-SemiBold", 10) + 12
        c.setFillColor(Color(0, 0, 0, alpha=0.55))
        c.roundRect(px + 6, py + dh - 22, tw_, 16, 3, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.drawString(px + 12, py + dh - 17.5, label)
        return px, py, dw, dh

    def stamp(px, py, dw, dh):
        """„Freigegebener Entwurf“: a rounded box in the company colour, top right on the approved picture."""
        c.setFont("Garamond-SemiBold", 12)
        tw_ = pdfmetrics.stringWidth(APPROVED_STAMP, "Garamond-SemiBold", 12) + 20
        x, y = px + dw - tw_ - 8, py + dh - 32
        c.setFillColor(green)
        c.setStrokeColor(HexColor("#ffffff"))
        c.setLineWidth(1.5)
        c.roundRect(x, y, tw_, 24, 6, stroke=1, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.drawString(x + 10, y + 8, APPROVED_STAMP)

    def table(rows, x, y_top, w) -> float:
        """Möbel | Holz | Griffe | Maße with thin rules in the company colour; returns the height used."""
        heads = ("Möbel", "Holz / Oberfläche", "Griffe / Beschläge", "Maße B × H × T")
        cols = [w * f for f in (0.27, 0.28, 0.25, 0.20)]
        rh, fs = 16, 10.5
        y = y_top
        for cells, font in [(heads, "Garamond-SemiBold")] + [(r, "Garamond-Regular") for r in rows]:
            c.setFillColor(INK)
            c.setFont(font, fs)
            cx = x
            for text, cw in zip(cells, cols):
                c.drawString(cx, y - 11.5, clip(text, font, fs, cw - 8))
                cx += cw
            y -= rh
            c.setStrokeColor(green)
            c.setLineWidth(0.8 if font == "Garamond-SemiBold" else 0.35)
            c.line(x, y + 1, x + w, y + 1)
        return y_top - y

    def approval_block(y, h):
        """„Entwurf Nr. ☐ 1 ☐ 2 gewählt am ___ Unterschrift ___“ (boxes drawn: the fonts have no ☐), QR on the right."""
        c.setStrokeColor(green)
        c.setLineWidth(0.8)
        c.line(m, y + h - 2, W - m, y + h - 2)
        c.setFillColor(INK)
        c.setFont("Garamond-SemiBold", 11.5)
        c.drawString(m, y + h - 18, APPROVAL_HEAD)
        c.setFont("Garamond-Regular", 11)
        x, ly = m, y + h - 44
        c.drawString(x, ly, "Entwurf Nr.")
        x += pdfmetrics.stringWidth("Entwurf Nr.", "Garamond-Regular", 11) + 10
        c.setLineWidth(0.9)
        c.setStrokeColor(INK)
        for k in range(1, len(chosen) + 1):
            c.rect(x, ly - 1, 9, 9, stroke=1, fill=0)
            c.drawString(x + 13, ly, str(k))
            x += 13 + pdfmetrics.stringWidth(str(k), "Garamond-Regular", 11) + 12
        rest = "   gewählt am ______________   Unterschrift ________________________"
        c.drawString(x, ly, rest)
        if link:
            q = 62  # ~22 mm
            renderPDF.draw(qr_drawing(link, q), c, W - m - q, y + h - q - 4)
            c.setFont("Garamond-SemiBold", 10)
            c.drawRightString(W - m - q - 8, y + h - 18, QR_CAPTION)
            c.setFont("Garamond-Regular", 8)
            c.drawRightString(W - m - q - 8, y + h - 30, clip(link, "Garamond-Regular", 8, W * 0.4))

    top = H - m - (58 if refs else 46)
    multi = len(chosen) > 1
    full_w = W - 2 * m
    for n, v in enumerate(chosen):
        if n:
            c.showPage()
        header()
        num = f" {n + 1}" if multi else ""
        rows = sheet_rows(doc, v)
        page_note = note if n == 0 else ""
        tab_w = full_w * 0.62 if page_note else full_w
        info_h = 8 + 16 * (len(rows) + 1) + 10
        note_p = None
        if page_note:
            nw = full_w - tab_w - 18
            note_p, nh = fit_para(page_note, "Garamond-Regular", 10.5, nw, 150, align=TA_LEFT, leading=1.35)
            info_h = max(info_h, 8 + nh + 10)
        block_h = (78 if link else 50) if block and n == len(chosen) - 1 else 0
        base = bottom + block_h
        img_h = top - base - info_h - 10
        before, b_label, a_label = before_of(doc, v)
        if before == v.get("image") or not imgs.exists(before):
            before = None
        if before:
            gw = (full_w - 14) / 2
            picture(before, m, base + info_h, gw, img_h, b_label)
            box = picture(v["image"], m + gw + 14, base + info_h, gw, img_h, a_label + num)
        else:
            box = picture(v["image"], m, base + info_h, full_w, img_h, (a_label if a_label != "Nachher" else "Entwurf") + num)
        if approved_id and v.get("id") == approved_id:
            stamp(*box)
        table(rows, m, base + info_h - 8, tab_w)
        if note_p:
            draw_para(c, note_p, m + tab_w + 18, base + info_h - 8, full_w - tab_w - 18)
        if block_h:
            approval_block(bottom, block_h)
        footer()
    c.save()
    return path
