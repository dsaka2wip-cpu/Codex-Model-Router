@echo off
powershell.exe -NoProfile -File "%~dp0Start-AdaptiveCodex.ps1"
if errorlevel 1 pause
