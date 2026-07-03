# Run the proxy FROM SOURCE — no .exe needed (use this where Application Control / Smart App
# Control blocks the downloaded binaries; pulled source has no Mark-of-the-Web).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1            # tray GUI (bare invocation)
#   powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 serve      # headless OpenAI/Anthropic API
#   powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 serve --no-launch-edge
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv sync
    uv run copilot-openai-proxy @args
} else {
    if (-not (Test-Path .venv)) { python -m venv .venv }
    & .\.venv\Scripts\python.exe -m pip install --quiet -e .
    & .\.venv\Scripts\python.exe -m m365_copilot_openai_proxy @args
}