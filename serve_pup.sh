#!/usr/bin/env bash
# The studio's prompt upsampler (klein-pup, block-FP8) behind vLLM's OpenAI API on 127.0.0.1:8003, ~16 GB unified
# memory (11.2 GiB weights + 1 GiB KV + encoder/activations). Measured: layout caption 57 s, edit 13 s (bf16 in-process: 159/27 s).
# Never add speculative decoding: n-gram drafting corrupts the captions of this hybrid model.
set -euo pipefail
if [ "${1:-}" = "--wait-studio" ]; then   # ExecStartPre: load after FLUX, never at the same time
  for _ in $(seq 120); do
    systemctl --user is-active -q flux-studio || exit 0
    curl -sf 127.0.0.1:7860/api/status | /usr/bin/python3 -c \
      'import json,sys; sys.exit(0 if json.load(sys.stdin)["loaded"]["distilled"] else 1)' 2>/dev/null && exit 0
    sleep 5
  done
  exit 0
fi
export TRITON_PTXAS_PATH="${TRITON_PTXAS_PATH:-/usr/local/cuda/bin/ptxas}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
# need: pup ~16 GB + a FLUX render that runs meanwhile (the scheduler does that on purpose) ~12 GB + the 15 GB reserve
need=43; avail=$(free -g | awk '/^Spei|^Mem/ {print $7}')
if [ -n "$avail" ] && [ "$avail" -lt "$need" ]; then echo "serve_pup: only ${avail} GB available, need ${need} GB" >&2; exit 1; fi
MODEL="${PUP_MODEL:-$HOME/models/flux-3-image-klein-pup-fp8}"
VLLM="${VLLM_BIN:-$HOME/venvs/vllm-judge/bin/vllm}"
export PATH="$(dirname "$VLLM"):$PATH"
exec "$VLLM" serve "$MODEL" --served-model-name pup \
  --host 127.0.0.1 --port "${PUP_PORT:-8003}" \
  --gpu-memory-utilization "${PUP_MEM:-0.16}" --kv-cache-memory-bytes 1G \
  --max-model-len 8192 --max-num-seqs "${PUP_SEQS:-3}" \
  --mm-processor-kwargs '{"max_pixels": 1048576}' \
  --limit-mm-per-prompt '{"image": {"count": 10, "width": 1024, "height": 1024}, "video": 0}'
