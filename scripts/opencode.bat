@echo off
REM Launcher for opencode.ps1 - passes all arguments through
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0opencode.ps1" %*