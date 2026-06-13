# EXPERIMENT: Nuitka --standalone (compiled C, in-process -> clears WDAC) wrapped in an Inno Setup
# per-user installer. Separate from build-exe.ps1 (PyInstaller onefile) and build-exe-nuitka.ps1
# (onefile, WDAC dead-end) -- those are left untouched.
#
# Flow:  Nuitka --standalone  ->  sign main exe  ->  ISCC installer.iss  ->  sign Setup.exe
# Usage: powershell -ExecutionPolicy Bypass -File .\installer_nuitka.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Single source of truth: read the version from the package (__version__).
$version = (& "$root\.venv\Scripts\python.exe" -c "import m365_copilot_openai_proxy as m; print(m.__version__)").Trim()
if (-not $version) { throw "Could not read __version__ from m365_copilot_openai_proxy" }
$out     = Join-Path $root "dist-nuitka"
$distDir = Join-Path $out "build_entry.dist"
$exe     = Join-Path $distDir "m365-copilot-proxy.exe"
$instOut = Join-Path $root "dist-installer"
$setup   = Join-Path $instOut "M365CopilotProxy-Setup-$version.exe"

# Locate ISCC (Inno Setup compiler) -- winget --scope user lands it under %LOCALAPPDATA%\Programs.
$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe not found. Install Inno Setup: winget install JRSoftware.InnoSetup" }

Write-Host "[0/4] Stopping any running instance..." -ForegroundColor Cyan
Get-Process -Name "m365-copilot-proxy" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "[1/4] Nuitka --standalone compile (slow: minutes)..." -ForegroundColor Cyan
& "$root\.venv\Scripts\python.exe" -m nuitka `
    --standalone `
    --windows-console-mode=disable `
    --enable-plugin=tk-inter `
    --include-package=m365_copilot_openai_proxy `
    --include-package-data=m365_copilot_openai_proxy `
    --include-package=uvicorn `
    --include-package=customtkinter `
    --include-package-data=customtkinter `
    --include-package=pystray `
    --windows-icon-from-ico="assets\icon.ico" `
    --product-name="M365 Copilot Proxy" `
    --product-version=$version `
    --company-name="MassimilianoPili" `
    --output-dir="$out" `
    --output-filename="m365-copilot-proxy.exe" `
    --assume-yes-for-downloads `
    --remove-output `
    build_entry.py
if (-not (Test-Path $exe)) { throw "Nuitka standalone build failed: $exe not found" }

Write-Host "[2/4] Signing the standalone main exe..." -ForegroundColor Cyan
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -like "*MassimilianoPili*" } | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=MassimilianoPili Code Signing" `
        -CertStoreLocation Cert:\CurrentUser\My -KeyUsage DigitalSignature -KeyExportPolicy Exportable
}
$sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert -HashAlgorithm SHA256
Write-Host "  signed main exe: $($sig.Status)"

Write-Host "[3/4] Building installer with Inno Setup..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $instOut | Out-Null
& "$iscc" "/DAppVersion=$version" "/DPayloadDir=$distDir" "installer.iss"
if (-not (Test-Path $setup)) { throw "Inno build failed: $setup not found" }

Write-Host "[4/4] Signing the installer..." -ForegroundColor Cyan
$sig2 = Set-AuthenticodeSignature -FilePath $setup -Certificate $cert -HashAlgorithm SHA256
Write-Host "  signed installer: $($sig2.Status)"
$mb = [math]::Round((Get-Item $setup).Length/1MB,1)
Write-Host "Done -> $setup ($mb MB)" -ForegroundColor Green