@echo off
powershell.exe -NoProfile -File "%~dp0Start-AdaptiveCodex.ps1" -WaitForExit %*
if errorlevel 1 pause
