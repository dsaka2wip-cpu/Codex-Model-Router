@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-AdaptiveCodex.ps1" -WaitForExit %*
if errorlevel 1 pause
