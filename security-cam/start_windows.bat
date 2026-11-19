@echo off
rem Запуск записи с камеры. Положите ярлык на этот файл в папку автозагрузки (Win+R -> shell:startup)
cd /d "%~dp0"
start "Security Cam" /min python security_cam.py
