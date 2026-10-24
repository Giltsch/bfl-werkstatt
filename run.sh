#!/bin/bash
# Start FLUX 3 Klein Studio on http://<this-host>:7860
# Frees the GPU first if the LawAI vLLM services are running (they are only stopped, not disabled).
cd "$(dirname "$0")"
# prefer: systemctl --user start flux-studio  (stops the LawAI services itself)
systemctl --user stop flux-studio lawai-judge.service lawai-ocr.service 2>/dev/null
exec .venv/bin/python server.py "$@"
