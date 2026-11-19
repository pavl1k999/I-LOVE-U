# Устанавливает задачу планировщика "SecurityCam": запуск записи при включении ПК (ещё до входа в систему).
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $dir 'security_cam.py'

# Настоящий путь к python.exe (а не заглушка из Microsoft Store)
try { $py = (& python -c "import sys; print(sys.executable)").Trim() } catch { $py = $null }
if (-not $py -or -not (Test-Path $py)) {
    Write-Host 'Python не найден. Установите Python 3 с python.org и отметьте "Add to PATH".' -ForegroundColor Red
    exit 1
}
& $py -c "import cv2, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Устанавливаю opencv-python и requests...'
    & $py -m pip install opencv-python requests
}
Write-Host "Python: $py"

$action    = New-ScheduledTaskAction -Execute $py -Argument "`"$script`"" -WorkingDirectory $dir
# AtStartup — включение ПК; AtLogOn — подстраховка на случай "быстрого запуска" Windows
$triggers  = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
             -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable

# Разрешаем трансляцию в брандмауэре (только домашняя/частная сеть)
$port = 8080
$m = Select-String -Path (Join-Path $dir 'config.ini') -Pattern '^\s*port\s*=\s*(\d+)'
if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
Remove-NetFirewallRule -DisplayName 'SecurityCam stream' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'SecurityCam stream' -Direction Inbound -Protocol TCP `
    -LocalPort $port -Action Allow -Profile Private | Out-Null

Stop-ScheduledTask -TaskName 'SecurityCam' -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'SecurityCam' -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'SecurityCam'

Write-Host ''
Write-Host 'Готово! Запись уже идёт и будет начинаться при каждом включении ПК.' -ForegroundColor Green
Write-Host "Видео: $dir\recordings"
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in 'Dhcp','Manual' -and $_.IPAddress -notlike '169.*' } | Select-Object -First 1).IPAddress
if ($ip) { Write-Host "Трансляция (если задан пароль в config.ini): http://${ip}:$port" }
