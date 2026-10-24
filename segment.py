"""Tap-to-select for the Werkstatt: SAM 2.1 (Apache-2.0) cuts out the object under a tap in the room photo, so an old
table or a chair can be removed precisely instead of with a rectangle that cuts through the bench next to it. The model
(~0.4 GB) loads on first use and stays resident; one tap takes ~0.2 s on the Spark."""

import threading

import numpy as np
from PIL import Image

REPO = "facebook/sam2.1-hiera-base-plus"
GROW = 0.012        # the outline is grown by this share of the long side: edges, shadows and legs' contact stay inside
MAX_POINTS = 32

_lock = threading.Lock()
_model = _proc = None


def loaded() -> bool:
    return _model is not None


def _load():
    global _model, _proc
    if _model is None:
        import torch
        from transformers import Sam2Model, Sam2Processor
        _proc = Sam2Processor.from_pretrained(REPO)
        _model = Sam2Model.from_pretrained(REPO, dtype=torch.float32).to("cuda").eval()
    return _model, _proc


def unload() -> None:
    global _model, _proc
    with _lock:
        _model = _proc = None
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def masks_at(img: Image.Image, points: list, negative: list | None = None) -> tuple[list[np.ndarray], int]:
    """SAM's candidate masks under the tap, smallest to largest (duplicates and slivers dropped), and the index of the
    one to show first. `negative` points must stay outside."""
    import torch
    with _lock:
        model, proc = _load()
        pts = [list(map(float, p)) for p in points] + [list(map(float, p)) for p in negative or []]
        labels = [1] * len(points) + [0] * len(negative or [])
        inp = proc(images=img.convert("RGB"), input_points=[[pts]], input_labels=[[labels]], return_tensors="pt").to("cuda")
        with torch.no_grad():
            out = model(**inp, multimask_output=True)
        masks = proc.post_process_masks(out.pred_masks.cpu(), inp["original_sizes"])[0][0].numpy()   # (3, H, W)
        scores = out.iou_scores[0, 0].cpu().numpy()
    # the best-scored mask, but never a sliver: prefer a larger one when the scores are close (a cup on the table → the table)
    order = sorted(range(len(scores)), key=lambda k: -scores[k])
    best = order[0]
    for k in order[1:]:
        if scores[k] > scores[best] - 0.08 and masks[k].sum() > 3 * masks[best].sum():
            best = k
    cands, first = [], 0
    for k in sorted(range(len(scores)), key=lambda k: masks[k].sum()):
        m = masks[k].astype(bool)
        if m.sum() < 50 or any((m ^ c).sum() < 0.1 * m.sum() for c in cands):
            if k == best and cands:
                first = len(cands) - 1
            continue
        if k == best:
            first = len(cands)
        cands.append(m)
    return cands, first


def outline(mask: np.ndarray) -> list[dict]:
    """The mask as outlines {"poly": [[x, y], ...], "box": [top, left, bottom, right]} on the 0-1000 grid: every part of
    at least 5 % of the largest (an object cut in two by a chair in front stays whole), grown by GROW, each simplified
    to at most MAX_POINTS points. Empty list for an empty mask."""
    import cv2
    h, w = mask.shape
    if mask.sum() < 50:
        return []
    k = max(3, int(GROW * max(w, h)) | 1)
    grown = cv2.dilate(mask.astype(np.uint8), np.ones((k, k), np.uint8))
    cs, _ = cv2.findContours(grown, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return []
    top = max(cv2.contourArea(c) for c in cs)
    out = []
    for c in sorted(cs, key=cv2.contourArea, reverse=True)[:4]:
        if cv2.contourArea(c) < 0.05 * top:
            continue
        eps, poly = 0.002 * cv2.arcLength(c, True), None
        for _ in range(12):
            poly = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
            if len(poly) <= MAX_POINTS:
                break
            eps *= 1.5
        if poly is None or len(poly) < 3:
            continue
        pts = [[round(1000 * x / w, 1), round(1000 * y / h, 1)] for x, y in poly]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        out.append({"poly": pts, "box": [int(min(ys)), int(min(xs)), int(np.ceil(max(ys))), int(np.ceil(max(xs)))]})
    return out
