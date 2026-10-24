#!/usr/bin/env bash
# The studio's judge VLM (port 8002). Unified memory: FLUX + upsampler use ~37 GB, the judge ~40 GB (29 GB FP8 weights +
# fixed 5 GiB KV + encoder/activations); --max-num-seqs stays small (hybrid Mamba model: one cache block per sequence).
set -euo pipefail
if [ "${1:-}" = "--wait-studio" ]; then   # ExecStartPre: never load 29 GB while the studio is still loading FLUX
  for _ in $(seq 120); do
    systemctl --user is-active -q flux-studio || exit 0
    curl -sf 127.0.0.1:7860/api/status | /usr/bin/python3 -c \
      'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s["loaded"]["distilled"] and (s.get("pup") or {}).get("state") != "starting" else 1)' 2>/dev/null && exit 0
    sleep 5
  done
  exit 0                                  # 600 s: continue anyway, the memory check below still guards
fi
export TRITON_PTXAS_PATH="${TRITON_PTXAS_PATH:-/usr/local/cuda/bin/ptxas}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"      # cached in ~/.cache/huggingface/hub
need=55; avail=$(free -g | awk '/^Spei|^Mem/ {print $7}')
if [ -n "$avail" ] && [ "$avail" -lt "$need" ]; then echo "serve_judge: only ${avail} GB available, need ${need} GB" >&2; exit 1; fi
MODEL="${JUDGE_MODEL:-Qwen/Qwen3.8-27B-FP8}"
VLLM="${VLLM_BIN:-$HOME/venvs/vllm-judge/bin/vllm}"
export PATH="$(dirname "$VLLM"):$PATH"            # flashinfer JIT-compiles its sampler with the venv's ninja
# --kv-cache-memory-bytes skips vLLM's memory profiling (which would subtract FLUX's concurrent use from the KV budget);
# --gpu-memory-utilization is then only the start-up free-memory check (psutil "available" on the Spark).
# max_pixels caps images at 1 MP: the preprocessor allows 16.7 MP, and vLLM would profile the encoder at that size.
exec "$VLLM" serve "$MODEL" \
  --host 127.0.0.1 --port "${JUDGE_PORT:-8002}" \
  --gpu-memory-utilization "${JUDGE_MEM:-0.33}" --kv-cache-memory-bytes 5G \
  --max-model-len 16384 --max-num-seqs "${JUDGE_MAX_SEQS:-8}" \
  --mm-processor-kwargs '{"max_pixels": 1048576}' \
  --limit-mm-per-prompt '{"image": {"count": 4, "width": 1024, "height": 1024}, "video": 0}'
