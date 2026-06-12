@echo off
setlocal enabledelayedexpansion

rem === Toggle on/off del proxy M365 Copilot ===
set "PROXY_DIR=c:\NoCloud\Progetti\Varie\m365-copilot-openai-proxy"
set "PORT=8000"

rem Detection semplice: c'e' un listener su :PORT?
set "PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set "PID=%%p"

if defined PID (
    echo [M365 Proxy] attivo su :%PORT% - SPENGO tutti i processi proxy...
    rem Kill robusto: TUTTI i processi (exe + python della venv) per path immagine, non solo il
    rem PID del listener. Killare solo il listener lasciava orfani -> restart "che non prendono".
    powershell -NoProfile -Command "Get-Process | Where-Object { $_.Path -like '*m365-copilot-openai-proxy*' } | Stop-Process -Force"
    echo [M365 Proxy] spento.
) else (
    echo [M365 Proxy] non attivo - AVVIO...
    cd /d "%PROXY_DIR%"
    set "M365_TIME_ZONE=Europe/Rome"
    set "M365_WORK_GROUNDING=false"
    set "M365_DEBUG=1"
    start "M365 Copilot Proxy" /min "%PROXY_DIR%\.venv\Scripts\copilot-openai-proxy.exe" serve
    echo [M365 Proxy] avviato ^(finestra minimizzata^). Porta http://127.0.0.1:%PORT%
)

echo.
timeout /t 2 >nul
