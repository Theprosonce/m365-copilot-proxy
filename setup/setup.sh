#!/usr/bin/env bash
# Install uv if needed, install this project, then start the proxy.
# Usage:
#   ./setup/setup.sh            # start tray GUI
#   ./setup/setup.sh serve      # start headless API
#   ./setup/setup.sh serve --no-launch-edge
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing uv..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "Error: install curl or wget, then re-run this script." >&2
        exit 1
    fi
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

uv sync
exec uv run copilot-openai-proxy "$@"
