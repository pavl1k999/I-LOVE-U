@echo off
rem Двойной клик -> установка автозапуска (попросит права администратора)
net session >nul 2>&1 || (powershell -Command "Start-Process '%~f0' -Verb RunAs" & exit /b)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
