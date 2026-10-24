"""Rebuild the FP8 checkpoint of the prompt upsampler (flux-3-image-klein-pup) that serve_pup.sh serves with vLLM.

Streams the bf16 checkpoint from the HF cache into Qwen's block-FP8 format — 128x128 blocks in float8_e4m3fn plus a
bf16 `weight_scale_inv` per block, the format the judge model uses — one tensor at a time on the CPU (≈ 2 min, a few GB
RAM). Only the language model's attention / linear-attention / MLP projections are quantized; vision tower, merger,
embeddings, lm_head and the small linear-attention gates stay bf16 (`modules_to_not_convert`).

    .venv/bin/python tools/quant_pup_fp8.py ~/models/flux-3-image-klein-pup-fp8 [--src <snapshot dir>]

Afterwards: `cd <dst> && sha256sum * > SHA256SUMS`.
"""

import argparse
import glob
import json
import os
import re
import shutil

import torch
from safetensors import safe_open
from safetensors.torch import save_file

QUANT = re.compile(r"model\.language_model\.layers\.\d+\.(self_attn\.(q|k|v|o)_proj|linear_attn\.(in_proj_qkv|in_proj_z|out_proj)"
                   r"|mlp\.(gate|up|down)_proj)\.weight$")
B, FP8_MAX = 128, torch.finfo(torch.float8_e4m3fn).max
SHARD_BYTES = 2e9


def quant(w):
    o, i = w.shape
    po, pi = -o % B, -i % B
    x = torch.nn.functional.pad(w.float(), (0, pi, 0, po)).view((o + po) // B, B, (i + pi) // B, B)
    scale = (x.abs().amax(dim=(1, 3)) / FP8_MAX).clamp(min=1e-12).to(torch.bfloat16)
    q = (x / scale.float()[:, None, :, None]).clamp(-FP8_MAX, FP8_MAX).view(o + po, i + pi)[:o, :i].to(torch.float8_e4m3fn)
    return q.contiguous(), scale.contiguous()


def skip_modules(cfg):
    skip = []
    for b in range(cfg["vision_config"]["depth"]):
        for pre in ("model.visual", "visual"):
            skip += [f"{pre}.blocks.{b}.attn.proj", f"{pre}.blocks.{b}.attn.qkv", f"{pre}.blocks.{b}.attn.qkv_proj",
                     f"{pre}.blocks.{b}.mlp.linear_fc1", f"{pre}.blocks.{b}.mlp.linear_fc2"]
    for pre in ("model.visual", "visual"):
        skip += [f"{pre}.merger.linear_fc1", f"{pre}.merger.linear_fc2", f"{pre}.patch_embed.proj", pre]
    for layer, kind in enumerate(cfg["text_config"]["layer_types"]):
        if kind == "linear_attention":
            skip += [f"model.language_model.layers.{layer}.linear_attn.{m}" for m in ("in_proj_a", "in_proj_b", "in_proj_ba", "conv1d")]
    return skip + ["lm_head", "model.language_model.embed_tokens"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dst")
    ap.add_argument("--src", help="bf16 snapshot dir (default: the HF cache of black-forest-labs/flux-3-image-klein-pup)")
    a = ap.parse_args()
    src = a.src or glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--black-forest-labs--flux-3-image-klein-pup/snapshots/*"))[0]
    dst = os.path.expanduser(a.dst)
    os.makedirs(dst, exist_ok=True)

    shard, size, n, index = {}, 0, 0, {}

    def flush():
        nonlocal shard, size, n
        if not shard:
            return
        name = f"model-{n:05d}.safetensors"
        save_file(shard, f"{dst}/{name}", metadata={"format": "pt"})
        index.update({k: name for k in shard})
        shard, size, n = {}, 0, n + 1

    with safe_open(f"{src}/model.safetensors", "pt", device="cpu") as f:
        for k in f.keys():
            t = f.get_tensor(k)
            if QUANT.match(k):
                q, s = quant(t)
                shard[k], shard[k + "_scale_inv"] = q, s
                size += q.numel() + s.numel() * 2
            else:
                shard[k] = t.contiguous()
                size += t.numel() * t.element_size()
            if size > SHARD_BYTES:
                flush()
    flush()
    json.dump({"metadata": {}, "weight_map": index}, open(f"{dst}/model.safetensors.index.json", "w"))
    cfg = json.load(open(f"{src}/config.json"))
    cfg["quantization_config"] = {"activation_scheme": "dynamic", "fmt": "e4m3", "quant_method": "fp8",
                                  "weight_block_size": [B, B], "modules_to_not_convert": skip_modules(cfg)}
    json.dump(cfg, open(f"{dst}/config.json", "w"), indent=2)
    for fn in os.listdir(src):
        if fn not in ("config.json", "model.safetensors") and not fn.startswith("."):
            shutil.copy(f"{src}/{fn}", f"{dst}/{fn}")
    print("done", n, "shards ->", dst)


if __name__ == "__main__":
    main()
