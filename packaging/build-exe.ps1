# Build the single-file proxy .exe with PyInstaller, then self-sign it.
# Corporate Application Control (WDAC) blocks UNSIGNED executables; an Authenticode
# signature (even self-signed by you) is enough for it to run on this machine.
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\packaging\build-exe.ps1
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $root
$exe = Join-Path $root "dist\m365-copilot-proxy.exe"

# [0/3] Kill any running instance, otherwise the locked .exe can't be overwritten/signed
#       (a stray instance silently leaves the OLD binary in place -> "it didn't update").
Write-Host "[0/3] Stopping any running instance..." -ForegroundColor Cyan
# Match by NAME too: a onefile exe runs from a temp _MEI path, so matching only $_.Path misses it
# and the file stays locked at sign time.
Get-Process -Name "m365-copilot-proxy" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $exe } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "[1/3] Clean PyInstaller build..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "$root\build", "$root\dist", "$root\m365-copilot-proxy.spec" -ErrorAction SilentlyContinue
& "$root\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name m365-copilot-proxy --icon "assets\icon.ico" `
    --collect-data m365_copilot_openai_proxy `
    --collect-submodules m365_copilot_openai_proxy `
    --collect-all uvicorn `
    --collect-all customtkinter `
    --collect-all pystray `
    packaging\build_entry.py
if (-not (Test-Path $exe)) { throw "build failed: $exe not found" }

Write-Host "[2/3] Code signing..." -ForegroundColor Cyan
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -like "*MassimilianoPili*" } | Select-Object -First 1
if (-not $cert) {
    Write-Host "  creating self-signed code-signing cert (CurrentUser, no admin needed)..."
    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject "CN=MassimilianoPili Code Signing" `
        -CertStoreLocation Cert:\CurrentUser\My `
        -KeyUsage DigitalSignature -KeyExportPolicy Exportable
}
$sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert -HashAlgorithm SHA256
Write-Host "  signed by: $($cert.Subject)  (status: $($sig.Status))"
Write-Host "[3/3] Done -> $exe" -ForegroundColor Green