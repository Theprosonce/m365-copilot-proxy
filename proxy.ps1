# Unified build + toggle script for the M365 Copilot proxy.
#
# Replaces proxy-toggle.bat. One command:
#   .\proxy.ps1               # toggle on/off; build-if-missing before the first start
#   .\proxy.ps1 -ForceBuild   # rebuild dist\m365-copilot-proxy.exe even if it exists, then start
#
# What it does (when NOT running):
#   1. ensure .venv exists (needed by PyInstaller in build-exe.ps1)
#   2. ensure dist\m365-copilot-proxy.exe exists (locally built -> no Mark-of-the-Web ->
#      no SmartScreen warning, unlike the binary downloaded from GitHub releases)
#   3. start that exe headless (`serve`) - windowless, listening on :8000
#
# What it does (when running):
#   detects listener on :8000 and stops every m365-copilot-openai-proxy process
#   (same kill-by-image-path used in the old proxy-toggle.bat).
#
# macOS / Linux counterpart: proxy.sh (runs from source, no PyInstaller step).
# Foreground source run (no exe at all, useful on dev): use run.ps1 / run.sh instead.

[CmdletBinding()]
param(
    [switch]$ForceBuild
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$port    = 8000
$exeRel  = "dist\m365-copilot-proxy.exe"
$exe     = Join-Path $PSScriptRoot $exeRel

# --- 1. detect listener on :PORT ---------------------------------------------
$active = $false
foreach ($line in (netstat -ano)) {
    if ($line -match ":$port\s+\S+\s+LISTENING") { $active = $true; break }
}

# --- 2. running -> STOP ------------------------------------------------------
if ($active) {
    Write-Host "[M365 Proxy] running on :$port - STOPPING..." -ForegroundColor Yellow
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like '*m365-copilot-openai-proxy*' } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Write-Host "[M365 Proxy] stopped." -ForegroundColor Green
    return
}

# --- 3. not running -> ENSURE + START ----------------------------------------
Write-Host "[M365 Proxy] not running - preparing to start..." -ForegroundColor Cyan

# 3a. ensure venv (build-exe.ps1 invokes .venv\Scripts\python.exe -m PyInstaller)
if (-not (Test-Path .venv)) {
    Write-Host "  .venv missing - creating + installing project (editable)..." -ForegroundColor Cyan
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install --quiet -e .
    # PyInstaller is needed only at build time
    & .\.venv\Scripts\python.exe -m pip install --quiet pyinstaller
}

# 3b. ensure exe (or force rebuild)
if ((-not (Test-Path $exe)) -or $ForceBuild) {
    if ($ForceBuild) {
        Write-Host "  -ForceBuild specified - rebuilding $exeRel ..." -ForegroundColor Cyan
    } else {
        Write-Host "  $exeRel missing - building (PyInstaller, ~1-2 min)..." -ForegroundColor Cyan
    }
    & ".\build-exe.ps1"
    if (-not (Test-Path $exe)) { throw "build failed: $exeRel not found" }
}

# 3c. start the locally-built signed exe headless, windowless (no MotW -> no SmartScreen prompt).
# The exe is --windowed (no console subsystem). Without -RedirectStandardOutput/Error any startup
# crash dies silently and we can't tell whether `serve` is actually listening. So we capture both
# streams to proxy.log / proxy.err.log and *poll netstat* until the listener binds (or time out).
$logOut = Join-Path $PSScriptRoot "proxy.log"
$logErr = Join-Path $PSScriptRoot "proxy.err.log"
# truncate previous run so the dump-on-fail only shows current attempt
"" | Set-Content -Path $logOut -Encoding utf8
"" | Set-Content -Path $logErr -Encoding utf8

Write-Host "[M365 Proxy] starting $exeRel serve (logs: proxy.log, proxy.err.log) ..." -ForegroundColor Cyan
$env:M365_TIME_ZONE      = "Europe/Rome"
$env:M365_WORK_GROUNDING = "false"
$env:M365_DEBUG          = "1"
$proc = Start-Process -FilePath $exe -ArgumentList "serve" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -PassThru

# Probe /healthz with a short timeout (max ~30s total). netstat alone is insufficient: uvicorn can
# bind :PORT but stay deadlocked, so a TCP-open check would falsely report "started". An HTTP 200
# from /healthz is the authoritative "FastAPI app is actually serving" signal.
$ready = $false
$timeoutSec = 30
$deadline = (Get-Date).AddSeconds($timeoutSec)
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) { break }
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # not up yet -> keep polling
    }
    Start-Sleep -Milliseconds 500
}

if ($ready) {
    Write-Host "[M365 Proxy] started (pid $($proc.Id)). http://127.0.0.1:$port" -ForegroundColor Green
    exit 0
}

# Fail path: dump the tail of both logs so the user sees the actual error.
Write-Host "[M365 Proxy] FAILED - /healthz did not respond within ${timeoutSec}s." -ForegroundColor Red
if ($proc.HasExited) {
    Write-Host "  process pid $($proc.Id) exited with code $($proc.ExitCode)" -ForegroundColor Red
} else {
    Write-Host "  process pid $($proc.Id) still running but no listener; leaving it for inspection" -ForegroundColor Red
}
foreach ($f in @($logOut, $logErr)) {
    if ((Test-Path $f) -and ((Get-Item $f).Length -gt 0)) {
        Write-Host "----- tail $((Split-Path $f -Leaf)) -----" -ForegroundColor Yellow
        Get-Content -Path $f -Tail 20 | ForEach-Object { Write-Host "  $_" }
    }
}
exit 1