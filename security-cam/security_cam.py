"""
Security Cam — запись видео с веб-камеры при запуске ПК.

Пишет видео кусками (по умолчанию по 10 минут) в папку recordings/,
накладывает на кадр дату и время, автоматически удаляет записи старше
RETENTION_DAYS дней. Остановить — Ctrl+C или закрыть окно консоли.

Требования:  pip install opencv-python
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import cv2

# ---------------- НАСТРОЙКИ ----------------
CAMERA_INDEX = 0            # 0 — встроенная/первая камера
WIDTH, HEIGHT = 1280, 720   # разрешение
FPS = 20                    # кадров в секунду
SEGMENT_MINUTES = 10        # длина одного файла
RETENTION_DAYS = 7          # сколько дней хранить записи
START_DELAY_SEC = 0         # пауза перед стартом (камера и так ждётся в цикле ниже)
OUTPUT_DIR = Path(__file__).resolve().parent / "recordings"
# -------------------------------------------

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=OUTPUT_DIR / "security_cam.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cam")
log.addHandler(logging.StreamHandler(sys.stdout))


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
    for f in OUTPUT_DIR.glob("cam_*.mp4"):
        if datetime.fromtimestamp(f.stat().st_mtime) < limit:
            f.unlink(missing_ok=True)
            log.info("Удалена старая запись %s", f.name)


def new_writer(w, h):
    name = OUTPUT_DIR / f"cam_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
    writer = cv2.VideoWriter(str(name), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    log.info("Новый файл: %s", name.name)
    return writer


def main():
    time.sleep(START_DELAY_SEC)
    cap = open_camera()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or WIDTH
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or HEIGHT

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
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, stamp, (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(frame, stamp, (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (255, 255, 255), 2, cv2.LINE_AA)
            writer.write(frame)

            if time.time() >= segment_end:
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


if __name__ == "__main__":
    main()
