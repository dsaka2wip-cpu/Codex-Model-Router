@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Router.ps1"
if errorlevel 1 pause
