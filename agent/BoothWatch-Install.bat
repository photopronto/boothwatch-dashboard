@echo off
rem BoothWatch installer. Double-click this on the booth PC.
rem It asks for administrator permission, then installs everything.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://photopronto.github.io/boothwatch-dashboard/agent/install.ps1' | iex"
