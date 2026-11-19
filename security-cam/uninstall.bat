@echo off
rem Двойной клик -> остановить запись и убрать автозапуск
net session >nul 2>&1 || (powershell -Command "Start-Process '%~f0' -Verb RunAs" & exit /b)
powershell -NoProfile -Command "Stop-ScheduledTask -TaskName SecurityCam -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName SecurityCam -Confirm:$false -ErrorAction SilentlyContinue"
echo Автозапуск удалён, запись остановлена.
pause
