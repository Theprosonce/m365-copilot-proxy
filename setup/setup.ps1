<#
.SYNOPSIS
    Install uv if needed, install this project, then start the proxy.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup\setup.ps1 serve
    powershell -ExecutionPolicy Bypass -File .\setup\setup.ps1 serve --no-launch-edge
#>
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found; installing uv..."
    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"

    $candidatePaths = @(
        Join-Path $env:USERPROFILE ".local\bin"
        Join-Path $env:USERPROFILE ".cargo\bin"
    )
    foreach ($path in $candidatePaths) {
        if ((Test-Path $path) -and ($env:Path -notlike "*$path*")) {
            $env:Path = "$path;$env:Path"
        }
    }
}

uv sync
uv run copilot-openai-proxy @args
