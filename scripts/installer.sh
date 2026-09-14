#!/usr/bin/env bash
# Install, repair, or remove project-managed runtime configuration.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

info() { printf '[installer] %s\n' "$*"; }
warn() { printf '[installer] Warning: %s\n' "$*" >&2; }
die() { printf '[installer] Error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
Usage: ./scripts/installer.sh --install | --reinstall | --uninstall

  --install    Verify prerequisites, install project dependencies, build the
               Chromium/noVNC image, and validate VNC plus Chrome DevTools.
  --reinstall  Repair the project by reinstalling locked dependencies and
               rebuilding its Chromium/noVNC image without cached layers.
  --uninstall  Remove project containers and generated local configuration.
               Docker, uv, Python packages, and the virtual environment remain.
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

ensure_docker() {
    have docker || die "Docker is required. Install Docker Engine or Docker Desktop, then retry"
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"

    if docker info >/dev/null 2>&1; then
        return
    fi

    if [[ "$(uname -s)" == "Linux" ]] && have systemctl; then
        info "Docker daemon is unavailable; attempting to start it"
        if systemctl --user start docker.service >/dev/null 2>&1 || \
           sudo -n systemctl start docker.service >/dev/null 2>&1; then
            for _ in $(seq 1 15); do
                docker info >/dev/null 2>&1 && return
                sleep 1
            done
        fi
    fi

    die "Docker daemon is unavailable or inaccessible. Start Docker, or grant this user Docker access, then retry"
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

print_success() {
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

install_project() {
    local reinstall="${1:-false}"
    install_uv
    ensure_docker
    have curl || die "curl is required for endpoint validation"

    if [[ "$reinstall" == "true" ]]; then
        info "Reinstalling locked Python dependencies"
        uv sync --frozen --reinstall
        info "Rebuilding Chromium and noVNC without cached layers"
        compose build --no-cache chromium
    else
        info "Installing locked Python dependencies"
        uv sync --frozen
        info "Building Chromium and noVNC"
        compose build chromium
    fi

    info "Starting Chromium, noVNC, and Chrome DevTools"
    compose up -d chromium
    wait_http "noVNC browser" "http://127.0.0.1:6080/vnc.html"
    wait_http "Chrome DevTools" "http://127.0.0.1:9222/json/version"
    print_success
}

uninstall_project() {
    info "Removing generated client and project configuration"
    if have uv && [[ -x .venv/bin/python ]]; then
        uv run copilot-proxy-server configure --undo >/dev/null 2>&1 || \
            warn "Client configuration could not be automatically reverted"
    fi

    if have docker && docker compose version >/dev/null 2>&1; then
        if docker info >/dev/null 2>&1; then
            info "Stopping and removing project containers"
            compose down --remove-orphans || warn "Project containers could not be removed"
        else
            warn "Docker daemon is unavailable; skipping container cleanup"
        fi
    else
        warn "Docker Compose is unavailable; skipping container cleanup"
    fi

    rm -rf -- .sessions
    rm -f -- .env config.ini debug.log proxy.log proxy.err.log last_request.json

    cat <<'EOF'
Uninstall complete.
Docker, uv, Python packages, images, volumes, and the virtual environment were preserved.
EOF
}

case "${1:-}" in
    --install)
        shift
        (($# == 0)) || die "--install does not accept additional arguments"
        install_project false
        ;;
    --reinstall)
        shift
        (($# == 0)) || die "--reinstall does not accept additional arguments"
        install_project true
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
