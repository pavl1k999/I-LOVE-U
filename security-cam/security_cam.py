"""
Security Cam — запись видео с веб-камеры при запуске ПК
+ уведомления в Telegram + живая трансляция в браузер.

Все настройки — в config.ini рядом со скриптом.
Требования:  pip install opencv-python requests
"""

import sys
import time
import queue
import base64
import logging
import threading
import configparser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import cv2

BASE = Path(__file__).resolve().parent

# ---------------- НАСТРОЙКИ ----------------
cfg = configparser.ConfigParser()
cfg.read(BASE / "config.ini", encoding="utf-8")

CAMERA_INDEX    = cfg.getint("camera", "index", fallback=0)
WIDTH           = cfg.getint("camera", "width", fallback=1280)
HEIGHT          = cfg.getint("camera", "height", fallback=720)
FPS             = cfg.getint("camera", "fps", fallback=20)
SEGMENT_MINUTES = cfg.getint("recording", "segment_minutes", fallback=10)
RETENTION_DAYS  = cfg.getint("recording", "retention_days", fallback=7)

TG_TOKEN        = cfg.get("telegram", "token", fallback="").strip()
TG_CHAT_ID      = cfg.get("telegram", "chat_id", fallback="").strip()
TG_CLIP_SECONDS = cfg.getint("telegram", "start_clip_seconds", fallback=20)
TG_SNAP_MINUTES = cfg.getint("telegram", "snapshot_every_minutes", fallback=0)

STREAM_ON       = cfg.getboolean("stream", "enabled", fallback=True)
STREAM_PORT     = cfg.getint("stream", "port", fallback=8080)
STREAM_USER     = cfg.get("stream", "user", fallback="admin")
STREAM_PASS     = cfg.get("stream", "password", fallback="").strip()

OUTPUT_DIR = BASE / "recordings"
# -------------------------------------------

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=OUTPUT_DIR / "security_cam.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cam")
log.addHandler(logging.StreamHandler(sys.stdout))

latest_frame = None
frame_lock = threading.Lock()


def get_frame():
    with frame_lock:
        return None if latest_frame is None else latest_frame.copy()


def jpeg(frame, quality=80):
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


# ================= TELEGRAM =================
class Telegram:
    def __init__(self, token, chat_id):
        import requests
        self.rq = requests
        self.api = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.q = queue.Queue()
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._commands, daemon=True).start()

    # --- очередь отправки: при загрузке ПК интернета может ещё не быть, поэтому повторяем ---
    def message(self, text):
        self.q.put(("sendMessage", {"text": text}, None))

    def photo(self, data, caption=""):
        self.q.put(("sendPhoto", {"caption": caption}, ("photo", "snap.jpg", data)))

    def video(self, path, caption=""):
        self.q.put(("sendVideo", {"caption": caption}, ("video", path)))

    def _sender(self):
        while True:
            method, data, file = self.q.get()
            data = dict(data, chat_id=self.chat_id)
            delay = 5
            while True:
                try:
                    files = None
                    if file and len(file) == 3:
                        files = {file[0]: (file[1], file[2])}
                    elif file:
                        files = {file[0]: open(file[1], "rb")}
                    r = self.rq.post(self.api + method, data=data, files=files, timeout=120)
                    if files and len(file) == 2:
                        files[file[0]].close()
                    if r.ok:
                        break
                    log.warning("Telegram %s: %s", method, r.text[:200])
                    if r.status_code in (400, 401, 403, 404):
                        break  # ошибка настроек — повтор не поможет
                except Exception as e:
                    log.warning("Telegram недоступен (%s), повтор через %d сек", e.__class__.__name__, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)

    # --- команды из чата: /photo, /status ---
    def _commands(self):
        offset = None
        while True:
            try:
                r = self.rq.get(self.api + "getUpdates",
                                params={"timeout": 50, "offset": offset}, timeout=60).json()
                for upd in r.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue  # чужие сообщения игнорируем
                    text = (msg.get("text") or "").strip().lower()
                    if text.startswith(("/photo", "фото")):
                        f = get_frame()
                        if f is not None:
                            self.photo(jpeg(f), datetime.now().strftime("%H:%M:%S"))
                        else:
                            self.message("Камера ещё не готова")
                    elif text.startswith(("/status", "статус")):
                        self.message("✅ Запись идёт. " + datetime.now().strftime("%d.%m %H:%M:%S"))
                    elif text.startswith("/start"):
                        self.message("Команды: /photo — снимок сейчас, /status — проверка")
            except Exception:
                time.sleep(10)


# ================= ТРАНСЛЯЦИЯ =================
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
                        data = jpeg(f, 70)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    time.sleep(0.1)
            except (ConnectionError, OSError):
                log.info("Трансляция: отключился %s", self.client_address[0])
        else:
            self.send_response(404)
            self.end_headers()


def start_stream():
    if not STREAM_ON:
        return
    if not STREAM_PASS:
        log.warning("Трансляция выключена: задайте password в config.ini [stream]")
        return
    srv = ThreadingHTTPServer(("0.0.0.0", STREAM_PORT), StreamHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Трансляция: http://<IP этого ПК>:%d", STREAM_PORT)


# ================= ЗАПИСЬ =================
def open_camera():
    attempt = 0
    while True:
        attempt += 1
        cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
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


def new_writer(w, h, prefix="cam"):
    name = OUTPUT_DIR / f"{prefix}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
    writer = cv2.VideoWriter(str(name), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    log.info("Новый файл: %s", name.name)
    return writer, name


def stamp(frame, h):
    text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for color, thick in (((0, 0, 0), 4), ((255, 255, 255), 2)):
        cv2.putText(frame, text, (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, color, thick, cv2.LINE_AA)


def main():
    global latest_frame
    boot_time = datetime.now()

    tg = None
    if TG_TOKEN and TG_CHAT_ID:
        tg = Telegram(TG_TOKEN, TG_CHAT_ID)
        tg.message(f"⚠️ ПК включён: {boot_time:%d.%m.%Y %H:%M:%S}")
    start_stream()

    cap = open_camera()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or WIDTH
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or HEIGHT

    cleanup_old()
    writer, _ = new_writer(w, h)
    segment_end = time.time() + SEGMENT_MINUTES * 60

    # короткий ролик в маленьком разрешении — для отправки в Telegram
    clip_size = (640, int(640 * h / w))
    clip_writer, clip_path = (new_writer(*clip_size, prefix="tgclip")
                              if tg and TG_CLIP_SECONDS > 0 else (None, None))
    clip_end = time.time() + TG_CLIP_SECONDS
    first_photo_sent = False
    next_snap = time.time() + TG_SNAP_MINUTES * 60 if TG_SNAP_MINUTES > 0 else None

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

            if tg:
                if not first_photo_sent:
                    tg.photo(jpeg(frame), "Первый кадр после включения")
                    first_photo_sent = True
                if clip_writer:
                    clip_writer.write(cv2.resize(frame, clip_size))
                    if t0 >= clip_end:
                        clip_writer.release()
                        clip_writer = None
                        tg.video(str(clip_path), f"Первые {TG_CLIP_SECONDS} сек после включения")
                if next_snap and t0 >= next_snap:
                    tg.photo(jpeg(frame), datetime.now().strftime("%H:%M:%S"))
                    next_snap = t0 + TG_SNAP_MINUTES * 60

            if t0 >= segment_end:
                writer.release()
                cleanup_old()
                writer, _ = new_writer(w, h)
                segment_end = time.time() + SEGMENT_MINUTES * 60

            dt = time.time() - t0
            if dt < frame_interval:
                time.sleep(frame_interval - dt)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    finally:
        writer.release()
        if clip_writer:
            clip_writer.release()
        cap.release()


if __name__ == "__main__":
    main()
