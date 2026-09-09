# Run the proxy FROM SOURCE — no .exe needed (use this where Application Control / Smart App
# Control blocks the downloaded binaries; pulled source has no Mark-of-the-Web).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1            # headless OpenAI/Anthropic API (default)
#   powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 serve      # headless OpenAI/Anthropic API
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv sync
    uv run copilot-proxy-server @args
} else {
    if (-not (Test-Path .venv)) { python -m venv .venv }
    & .\.venv\Scripts\python.exe -m pip install --quiet -e .
    & .\.venv\Scripts\python.exe -m copilot_proxy_server @args
}