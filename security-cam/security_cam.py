"""
Security Cam — один exe-файл.

Двойной щелчок по SecurityCam.exe:
  * не установлено  -> устанавливает автозапуск при включении ПК и сразу начинает запись;
  * уже установлено -> предлагает удалить.

Запись видео идёт в папку recordings рядом с exe, живая трансляция — в браузере.
Если в config.ini указан Telegram-бот, при старте трансляции придёт одно
сообщение со ссылкой.
"""

import os
import sys
import time
import json
import base64
import socket
import secrets
import logging
import threading
import subprocess
import configparser
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import cv2

APP = "SecurityCam"
FROZEN = getattr(sys, "frozen", False)
EXE = Path(sys.executable if FROZEN else __file__).resolve()
BASE = EXE.parent
CONFIG = BASE / "config.ini"
OUTPUT_DIR = BASE / "recordings"

DEFAULT_CONFIG = """\
; ===== Настройки Security Cam =====
; После изменения перезагрузите ПК (или удалите и снова установите программу)

[camera]
index = 0
width = 1280
height = 720
fps = 20

[recording]
segment_minutes = 10
retention_days = 7

[stream]
port = 8080
user = admin
password = {password}

[telegram]
; Токен бота от @BotFather и ваш chat_id. Пусто — Telegram не используется.
token =
chat_id =
"""


# ================= КОНФИГ =================
def load_config():
    if not CONFIG.exists():
        CONFIG.write_text(DEFAULT_CONFIG.format(password=secrets.token_urlsafe(8)),
                          encoding="utf-8")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.get("stream", "password", fallback="").strip():
        cfg.set("stream", "password", secrets.token_urlsafe(8))
        with open(CONFIG, "w", encoding="utf-8") as f:
            cfg.write(f)
    return cfg


cfg = load_config()
CAMERA_INDEX    = cfg.getint("camera", "index", fallback=0)
WIDTH           = cfg.getint("camera", "width", fallback=1280)
HEIGHT          = cfg.getint("camera", "height", fallback=720)
FPS             = cfg.getint("camera", "fps", fallback=20)
SEGMENT_MINUTES = cfg.getint("recording", "segment_minutes", fallback=10)
RETENTION_DAYS  = cfg.getint("recording", "retention_days", fallback=7)
STREAM_PORT     = cfg.getint("stream", "port", fallback=8080)
STREAM_USER     = cfg.get("stream", "user", fallback="admin").strip()
STREAM_PASS     = cfg.get("stream", "password", fallback="").strip()
TG_TOKEN        = cfg.get("telegram", "token", fallback="").strip()
TG_CHAT_ID      = cfg.get("telegram", "chat_id", fallback="").strip()

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=OUTPUT_DIR / "security_cam.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cam")
if sys.stdout:
    log.addHandler(logging.StreamHandler(sys.stdout))


# ================= УСТАНОВКА (Windows) =================
def msgbox(text, yes_no=False):
    import ctypes
    flags = 0x40 | (0x04 if yes_no else 0)  # info icon (+ Yes/No)
    return ctypes.windll.user32.MessageBoxW(None, text, APP, flags) == 6  # 6 = Yes


def is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate(arg):
    import ctypes
    params = arg if FROZEN else f'"{EXE}" {arg}'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(BASE), 1)


def powershell(script):
    flags = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", script], capture_output=True, text=True,
                          creationflags=flags)


def is_installed():
    return powershell(f"Get-ScheduledTask -TaskName {APP} -ErrorAction Stop").returncode == 0


def install():
    exe, args = (str(EXE), "--run") if FROZEN else (sys.executable, f'"{EXE}" --run')
    ps = f"""
$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}' -WorkingDirectory '{BASE}'
$t = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
     -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
     -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName '{APP}' -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null
Remove-NetFirewallRule -DisplayName '{APP} stream' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName '{APP} stream' -Direction Inbound -Protocol TCP `
    -LocalPort {STREAM_PORT} -Action Allow -Profile Private,Domain | Out-Null
Start-ScheduledTask -TaskName '{APP}'
"""
    r = powershell(ps)
    if r.returncode != 0:
        msgbox("Не удалось установить:\n\n" + (r.stderr or r.stdout)[-800:])
        return
    msgbox("✅ Установлено! Запись уже идёт и будет включаться при каждом запуске ПК.\n\n"
           f"Видео: {OUTPUT_DIR}\n\n"
           f"Трансляция: {stream_url(local_ip())}\n"
           f"Логин: {STREAM_USER}\nПароль: {STREAM_PASS}\n\n"
           "(всё это есть в config.ini)")


def uninstall():
    powershell(f"""
Stop-ScheduledTask -TaskName '{APP}' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName '{APP}' -Confirm:$false -ErrorAction SilentlyContinue
Get-Process -Name '{APP}' -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Id -notin @({os.getpid()}, {os.getppid()}) }} | Stop-Process -Force
Remove-NetFirewallRule -DisplayName '{APP} stream' -ErrorAction SilentlyContinue
""")
    msgbox("Программа удалена из автозапуска, запись остановлена.\n"
           "Записанные видео остались в папке recordings.")


# ================= СЕТЬ / TELEGRAM =================
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def tailscale_ip():
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            a, b = map(int, ip.split(".")[:2])
            if a == 100 and 64 <= b <= 127:
                return ip
    except OSError:
        pass
    return None


def stream_url(ip):
    return f"http://{ip}:{STREAM_PORT}"


def notify_stream_started():
    """Одно сообщение в Telegram: трансляция началась + ссылка. Повторяет, пока нет интернета."""
    if not (TG_TOKEN and TG_CHAT_ID):
        return

    def worker():
        delay = 5
        while True:
            lines = [f"🔴 Трансляция началась ({datetime.now():%d.%m.%Y %H:%M})",
                     stream_url(local_ip())]
            ts = tailscale_ip()
            if ts:
                lines.append(stream_url(ts) + "  (через Tailscale)")
            data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID,
                                           "text": "\n".join(lines)}).encode()
            try:
                with urllib.request.urlopen(
                        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                        data=data, timeout=30) as r:
                    if json.load(r).get("ok"):
                        log.info("Telegram: сообщение отправлено")
                        return
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 404):
                    log.error("Telegram: проверьте token/chat_id (%s)", e.code)
                    return
            except Exception as e:
                log.warning("Telegram недоступен (%s), повтор через %d сек",
                            e.__class__.__name__, delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)

    threading.Thread(target=worker, daemon=True).start()


# ================= ТРАНСЛЯЦИЯ =================
latest_frame = None
frame_lock = threading.Lock()


def get_frame():
    with frame_lock:
        return None if latest_frame is None else latest_frame.copy()


class StreamHandler(BaseHTTPRequestHandler):
    auth = "Basic " + base64.b64encode(f"{STREAM_USER}:{STREAM_PASS}".encode()).decode()

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != self.auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Security Cam"')
            self.end_headers()
            return
        if self.path == "/":
            html = (b"<html><head><meta name=viewport content='width=device-width'>"
                    b"<title>Security Cam</title></head><body style='margin:0;background:#000'>"
                    b"<img src='/video' style='width:100%'></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)
        elif self.path == "/video":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            log.info("Трансляция: подключился %s", self.client_address[0])
            try:
                while True:
                    f = get_frame()
                    if f is not None:
                        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 70])
                        data = buf.tobytes()
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    time.sleep(0.1)
            except (ConnectionError, OSError):
                log.info("Трансляция: отключился %s", self.client_address[0])
        else:
            self.send_response(404)
            self.end_headers()


def start_stream():
    while True:
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", STREAM_PORT), StreamHandler)
            break
        except OSError as e:
            log.warning("Порт %d занят (%s), повтор через 10 сек", STREAM_PORT, e)
            time.sleep(10)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Трансляция: %s", stream_url(local_ip()))


# ================= ЗАПИСЬ =================
def open_camera():
    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    attempt = 0
    while True:
        attempt += 1
        cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, FPS)
            log.info("Камера открыта (попытка %d)", attempt)
            return cap
        cap.release()
        # при загрузке Windows драйвер камеры может появиться не сразу — ждём, сколько нужно
        if attempt <= 3 or attempt % 30 == 0:
            log.warning("Камера недоступна, жду... (попытка %d)", attempt)
        time.sleep(2)


def cleanup_old():
    limit = datetime.now() - timedelta(days=RETENTION_DAYS)
    for f in OUTPUT_DIR.glob("*.mp4"):
        if datetime.fromtimestamp(f.stat().st_mtime) < limit:
            f.unlink(missing_ok=True)
            log.info("Удалена старая запись %s", f.name)


def new_writer(w, h):
    name = OUTPUT_DIR / f"cam_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
    writer = cv2.VideoWriter(str(name), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    log.info("Новый файл: %s", name.name)
    return writer


def stamp(frame, h):
    text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for color, thick in (((0, 0, 0), 4), ((255, 255, 255), 2)):
        cv2.putText(frame, text, (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, color, thick, cv2.LINE_AA)


def run():
    global latest_frame
    log.info("=== Запуск ===")
    cap = open_camera()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or WIDTH
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or HEIGHT

    start_stream()
    notify_stream_started()

    cleanup_old()
    writer = new_writer(w, h)
    segment_end = time.time() + SEGMENT_MINUTES * 60
    frame_interval = 1.0 / FPS
    fails = 0
    try:
        while True:
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                fails += 1
                if fails > 50:
                    log.error("Камера пропала, переподключение...")
                    cap.release()
                    cap = open_camera()
                    fails = 0
                time.sleep(0.1)
                continue
            fails = 0

            if frame.shape[1] != w or frame.shape[0] != h:
                frame = cv2.resize(frame, (w, h))
            stamp(frame, h)
            writer.write(frame)
            with frame_lock:
                latest_frame = frame

            if t0 >= segment_end:
                writer.release()
                cleanup_old()
                writer = new_writer(w, h)
                segment_end = time.time() + SEGMENT_MINUTES * 60

            dt = time.time() - t0
            if dt < frame_interval:
                time.sleep(frame_interval - dt)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    finally:
        writer.release()
        cap.release()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--run" or os.name != "nt":
        run()
    elif arg == "--install":
        install()
    elif arg == "--uninstall":
        uninstall()
    elif is_installed():
        if msgbox("Security Cam уже установлена и работает.\n\nУдалить её?", yes_no=True):
            uninstall() if is_admin() else elevate("--uninstall")
    else:
        if msgbox("Установить Security Cam?\n\nКамера будет записывать видео при каждом "
                  "включении ПК. Понадобятся права администратора.", yes_no=True):
            install() if is_admin() else elevate("--install")


if __name__ == "__main__":
    main()
