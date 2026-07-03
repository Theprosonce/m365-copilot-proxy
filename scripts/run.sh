#!/usr/bin/env bash
# Run the proxy FROM SOURCE — no .exe needed (Linux, or any box without the signed binary).
#
# Usage:
#   ./scripts/run.sh            # tray GUI (bare invocation; needs a desktop + python3-tk)
#   ./scripts/run.sh serve      # headless OpenAI/Anthropic API
#   ./scripts/run.sh serve --no-launch-edge
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
    uv sync
    exec uv run copilot-openai-proxy "$@"
else
    [ -d .venv ] || python3 -m venv .venv
    ./.venv/bin/python -m pip install --quiet -e .
    exec ./.venv/bin/python -m m365_copilot_openai_proxy "$@"
fi