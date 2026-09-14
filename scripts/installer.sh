#!/usr/bin/env bash
# Install or uninstall project-managed runtime resources.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

info() { printf '[installer] %s\n' "$*"; }
die() { printf '[installer] Error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
Usage: ./scripts/installer.sh --install | --uninstall

  --install    Verify prerequisites, install project dependencies, build the
               Chromium/noVNC image, and validate VNC plus Chrome DevTools.
  --uninstall  Remove project containers, images, volumes, virtual environment,
               generated configuration, sessions, caches, and logs.
EOF
}

compose() {
    docker compose -f "$ROOT/compose.server.yml" "$@"
}

install_uv() {
    if have uv; then
        return
    fi
    info "uv not found; installing it for the current user"
    if have curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif have wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        die "curl or wget is required to install uv"
    fi
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    have uv || die "uv was installed but is unavailable in PATH"
}

check_docker() {
    have docker || die "Docker is required. Install Docker Engine or Docker Desktop, then rerun --install"
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
    docker info >/dev/null 2>&1 || die "Docker is installed, but its daemon is unavailable or inaccessible"
}

wait_http() {
    local name="$1" url="$2" attempts="${3:-60}"
    local i
    for ((i=1; i<=attempts; i++)); do
        if curl --fail --silent "$url" >/dev/null 2>&1; then
            info "$name is ready: $url"
            return
        fi
        sleep 1
    done
    compose logs --tail=80 chromium >&2 || true
    die "$name did not become ready at $url"
}

install_project() {
    install_uv
    check_docker
    have curl || die "curl is required for endpoint validation"

    info "Installing locked Python dependencies"
    uv sync --frozen

    info "Building and starting Chromium, noVNC, and Chrome DevTools"
    compose up -d --build chromium
    wait_http "noVNC browser" "http://127.0.0.1:6080/vnc.html"
    wait_http "Chrome DevTools" "http://127.0.0.1:9222/json/version"

    local host
    host="$(hostname -f 2>/dev/null || hostname)"
    cat <<EOF

Installation complete.

Run the proxy:
  uv run copilot-proxy-server

Open noVNC locally:
  http://127.0.0.1:6080/vnc.html

Open noVNC remotely, after allowing TCP 6080 in the server firewall:
  http://${host}:6080/vnc.html

The proxy API listens on all interfaces at port 8000.
Chrome DevTools stays restricted to localhost on port 9222.
Only the proxy API and noVNC are intended for remote access.
The first Microsoft 365 sign-in is the only interactive setup.
EOF
}

uninstall_project() {
    check_docker
    info "Removing project containers, local images, and volumes"
    compose down --volumes --remove-orphans --rmi local

    info "Removing generated local project state"
    rm -rf -- .venv .sessions .pytest_cache
    rm -f -- .env config.ini debug.log proxy.log proxy.err.log last_request.json

    cat <<'EOF'
Uninstall complete.
Docker and uv remain installed because other projects may use them.
EOF
}

case "${1:-}" in
    --install)
        shift
        (($# == 0)) || die "--install does not accept additional arguments"
        install_project
        ;;
    --uninstall)
        shift
        (($# == 0)) || die "--uninstall does not accept additional arguments"
        uninstall_project
        ;;
    -h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
