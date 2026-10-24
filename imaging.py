"""Pure image/size helpers shared by the engine, the scheduler and the upsampler client (PIL + stdlib only: no torch,
no diffusers, so jobs.py and the tests import it without loading the GPU stack)."""

import math

from PIL import Image, ImageOps

MAX_REFERENCES = 10
MAX_REF_PIXELS = 1024 * 1024
MAX_SIDE = 4096
MAX_UPLOAD = 40 * 2**20  # bytes per uploaded image (read into RAM whole; phone photos are 3-12 MB)

# steps/guidance per FLUX mode; engine.MODES adds repo + block classes (those need diffusers)
MODE_DEFAULTS = {"distilled": {"steps": 16, "guidance": None}, "base": {"steps": 50, "guidance": 3.5}}


def fit_reference(img: Image.Image, max_pixels: int = MAX_REF_PIXELS) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    img = img.convert("RGB")
    if img.width * img.height <= max_pixels:
        return img
    scale = (max_pixels / (img.width * img.height)) ** 0.5
    return img.resize((max(16, round(img.width * scale)), max(16, round(img.height * scale))), Image.LANCZOS)


def canvas_ratio(width: int, height: int) -> str:
    g = math.gcd(width, height)
    w, h = width // g, height // g
    if max(w, h) > 32:  # not a tidy ratio: round to two decimals
        return f"{width / height:.2f}:1"
    return f"{w}:{h}"


def mem_available_gb() -> float:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except OSError:
        pass
    return float("inf")


def snap(v: int) -> int:
    return max(256, min(MAX_SIDE, round(int(v) / 16) * 16))


def dims_for_aspect(aspect: float, side: int) -> tuple[int, int]:
    h = (side * side / aspect) ** 0.5
    return snap(h * aspect), snap(h)
