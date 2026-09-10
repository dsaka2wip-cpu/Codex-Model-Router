@echo off
powershell.exe -NoProfile -File "%~dp0Restore-Codex.ps1"
if errorlevel 1 pause
