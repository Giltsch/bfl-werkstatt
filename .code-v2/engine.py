"""FLUX 3 Image Klein on the local GPU: the two transformers (distilled / base), the shared text encoder and VAE,
and the prompt upsampler (klein-pup). Everything loads lazily and stays resident."""

import gc
import math
import threading

import torch
from PIL import Image, ImageOps

from diffusers import ModularPipeline
from diffusers.modular_pipelines import Flux3AutoBlocks, Flux3DistilledAutoBlocks
from diffusers.modular_pipelines.flux3 import denoise as flux3_denoise
from diffusers.modular_pipelines.flux3.prompt_upsampler import prepare_references, upsample_prompt

DISTILLED_REPO = "black-forest-labs/flux-3-image-klein"
BASE_REPO = "black-forest-labs/flux-3-image-klein-base"
PUP_REPO = "black-forest-labs/flux-3-image-klein-pup"

DTYPE = torch.bfloat16
MAX_REFERENCES = 10
MAX_REF_PIXELS = 1024 * 1024
MAX_SIDE = 4096

# unified memory (CPU+GPU share 121 GB, running out freezes the machine): a load needs about its bf16 weights
# (distilled carries text encoder + VAE, base reuses them) plus a reserve for activations and the OS
NEED_GB = {"distilled": 19, "base": 11, "pup": 18}
RESERVE_GB = 6

MODES = {
    "distilled": {"repo": DISTILLED_REPO, "steps": 16, "guidance": None, "blocks": Flux3DistilledAutoBlocks},
    "base": {"repo": BASE_REPO, "steps": 50, "guidance": 3.5, "blocks": Flux3AutoBlocks},
}


class Progress:
    """Step reporting hooked into the denoise loop's tqdm."""

    callback = None  # set per job: callback(done, total)


class _ProgressBar:
    def __init__(self, total):
        self.total, self.n = total, 0

    def update(self, n=1):
        self.n += n
        if Progress.callback:
            Progress.callback(self.n, self.total)

    def __enter__(self):
        if Progress.callback:
            Progress.callback(0, self.total)
        return self

    def __exit__(self, *exc):
        return False


def _progress_bar(self, iterable=None, total=None):
    return _ProgressBar(total if total is not None else len(iterable))


flux3_denoise.Flux3DenoiseLoopWrapper.progress_bar = _progress_bar


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


def check_memory(what: str) -> None:
    need, free = NEED_GB[what] + RESERVE_GB, mem_available_gb()
    if free < need:
        raise MemoryError(f"Zu wenig freier Speicher zum Laden von „{what}“ ({free:.0f} GB frei, etwa {need} GB nötig) – "
                          "Prüfer oder andere Modelle entladen und erneut versuchen.")


class Engine:
    def __init__(self):
        self.lock = threading.Lock()  # one GPU job at a time
        self.pipes: dict[str, ModularPipeline] = {}
        self.pup = None
        self.pup_processor = None
        self.status = "idle"

    # ---------- loading ----------

    def pipe(self, mode: str) -> ModularPipeline:
        if mode in self.pipes:
            return self.pipes[mode]
        check_memory(mode)
        self.status = f"loading {mode} model…"
        # the pipeline's own upsampler step goes: captions are written beforehand by load_pup/upsample,
        # so the user can review them, and one upsampler serves both modes
        blocks = MODES[mode]["blocks"]()
        blocks.sub_blocks.pop("prompt_upsampler")
        pipe = blocks.init_pipeline(MODES[mode]["repo"])
        names = [n for n in pipe.pretrained_component_names if not n.startswith("prompt_upsampler")]
        shared = next(iter(self.pipes.values()), None)
        if shared is not None:
            # text encoder, tokenizer and VAE are the same in both repos: load only what differs
            names = [n for n in names if n not in ("text_encoder", "tokenizer", "vae")]
            pipe.load_components(names=names, dtype=DTYPE)
            pipe.update_components(text_encoder=shared.text_encoder, tokenizer=shared.tokenizer, vae=shared.vae)
        else:
            pipe.load_components(names=names, dtype=DTYPE)
        pipe.to("cuda")
        self.pipes[mode] = pipe
        self.status = "idle"
        return pipe

    def load_pup(self):
        if self.pup is not None:
            return
        from transformers import AutoProcessor, Qwen3_5ForConditionalGeneration

        check_memory("pup")
        self.status = "loading prompt upsampler…"
        self.pup_processor = AutoProcessor.from_pretrained(PUP_REPO)
        self.pup = Qwen3_5ForConditionalGeneration.from_pretrained(PUP_REPO, dtype=DTYPE, device_map="cuda").eval()
        self.status = "idle"

    def unload(self, what: str):
        with self.lock:
            if what == "pup":
                self.pup = self.pup_processor = None
            elif what in self.pipes:
                del self.pipes[what]
            gc.collect()
            torch.cuda.empty_cache()

    def loaded(self):
        return {"distilled": "distilled" in self.pipes, "base": "base" in self.pipes, "pup": self.pup is not None}

    # ---------- work ----------

    @torch.no_grad()
    def upsample(self, request: str, images: list[Image.Image], boxes: list[dict] | None, width: int, height: int) -> str:
        """The full caption for a short request. `boxes` use 0-1 coordinates [top, left, bottom, right]."""
        self.load_pup()
        refs = prepare_references(images) if images else None
        if not images and width != height:
            request = f"TARGET CANVAS: {canvas_ratio(width, height)}\n{request}"
        return upsample_prompt(self.pup, self.pup_processor, request, images=refs, boxes=boxes or None)

    @torch.no_grad()
    def generate(self, prompt: str, images: list[Image.Image], mode: str, width: int, height: int,
                 steps: int, guidance: float | None, seed: int) -> Image.Image:
        pipe = self.pipe(mode)
        if mode == "base" and guidance is not None:
            pipe.guider = pipe.guider.new(guidance_scale=float(guidance))
        kwargs = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_inference_steps": steps,
            "generator": torch.Generator().manual_seed(seed),
            "output": "images",
        }
        if images:
            kwargs["image"] = [fit_reference(i) for i in images]
        return pipe(**kwargs)[0]


def snap(v: int) -> int:
    return max(256, min(MAX_SIDE, round(int(v) / 16) * 16))


def dims_for_aspect(aspect: float, side: int) -> tuple[int, int]:
    h = (side * side / aspect) ** 0.5
    return snap(h * aspect), snap(h)
