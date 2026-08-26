"""Grow diary daemon for the Raspberry Pi.

Schedules photos according to your light schedule (read from Firebase Settings),
captures them with a USB webcam, stores them in Firestore as small JPEGs, and
periodically pushes live temperature/humidity/VPD/fan readings from the AC
Infinity Controller 69 over Bluetooth.

Run as a systemd service so it restarts on boot and never exits.

Requirements:
    pip install opencv-python-headless firebase-admin bleak

Environment:
    FIREBASE_SERVICE_ACCOUNT   path to the Firebase service-account JSON (required)
    FIREBASE_PROJECT_ID        your Firebase project id, e.g. black-betty-diary

Usage:
    python3 pi_grow.py                    # foreground (for testing)
    python3 pi_grow.py --once             # take one photo now, push one sensor reading, exit
    python3 pi_grow.py --sensor-interval 15
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import struct
import sys
import time

import cv2
import firebase_admin
from firebase_admin import credentials, firestore

from ac_infinity_scan import parse_advertisement

SETTINGS_REF = "settings/app"
STATUS_REF = "status/pi"
DEFAULT_SETTINGS = {
    "lightsOn": "18:00",
    "lightsOff": "12:00",
    "photosPerDay": 4,
    "plants": ["Plant A"],
}

TICK_SECONDS = 30
SETTINGS_CACHE_SECONDS = 300
PHOTO_MAX_WIDTH = 1280
JPEG_QUALITY = 80
MANUFACTURER_ID = 2306

db = None
_state_path = None


def log(msg):
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)
    try:
        with open(_state_path + ".log", "a") as fh:
            fh.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass


def minutes_of(hhmm):
    hour, minute = (int(p) for p in hhmm.split(":"))
    return hour * 60 + minute


def format_minutes(value):
    value %= 1440
    return f"{value // 60:02d}:{value % 60:02d}"


def schedule_times(lights_on, lights_off, per_day):
    start = minutes_of(lights_on)
    end = minutes_of(lights_off)
    if end <= start:
        end += 1440
    length = end - start
    step = length / (per_day + 1)
    return [format_minutes(start + round(step * i)) for i in range(1, per_day + 1)]


def load_state():
    try:
        with open(_state_path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"shot_slots": {}}


def save_state(state):
    with open(_state_path, "w") as fh:
        json.dump(state, fh)


def fetch_settings(force=False):
    cached = getattr(fetch_settings, "cache", None)
    cached_at = getattr(fetch_settings, "at", 0)
    if not force and cached is not None and time.time() - cached_at < SETTINGS_CACHE_SECONDS:
        return cached
    settings = dict(DEFAULT_SETTINGS)
    try:
        doc = db.collection("settings").document("app").get()
        if doc.exists:
            settings.update(doc.to_dict())
    except Exception as exc:
        log(f"WARNING could not read settings: {exc}")
    fetch_settings.cache = settings
    fetch_settings.at = time.time()
    return settings


def report_status(extra=None):
    status = {"heartbeat": dt.datetime.now(dt.timezone.utc).isoformat()}
    if extra:
        status.update(extra)
    db.collection("status").document("pi").set(status)


def capture_photo(camera_index):
    cam = cv2.VideoCapture(camera_index)
    if not cam.isOpened():
        raise RuntimeError(f"camera /dev/video{camera_index} not detected")
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    for _ in range(60):
        cam.read()
    ok, frame = cam.read()
    cam.release()
    if not ok or frame is None:
        raise RuntimeError("camera returned no frame")
    if frame.shape[1] > PHOTO_MAX_WIDTH:
        scale = PHOTO_MAX_WIDTH / frame.shape[1]
        frame = cv2.resize(frame, (PHOTO_MAX_WIDTH, int(frame.shape[0] * scale)))
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise RuntimeError("failed to encode jpeg")
    return encoded.tobytes()


def store_photo(data, plant):
    import base64

    now = dt.datetime.now(dt.timezone.utc)
    db.collection("photos").add(
        {
            "timestamp": now.isoformat(),
            "plant": plant,
            "base64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data),
        }
    )
    log(f"photo stored ({len(data)} bytes) for {plant}")


def do_photo(settings, state, camera_index):
    now_min = dt.datetime.now().hour * 60 + dt.datetime.now().minute
    today = dt.date.today().isoformat()
    times = schedule_times(settings["lightsOn"], settings["lightsOff"], settings["photosPerDay"])
    plants = settings["plants"] or ["Plant A"]
    due = None
    for i, slot in enumerate(times):
        key = f"{today}:{i}:{slot}"
        if key in state["shot_slots"]:
            continue
        if now_min >= minutes_of(slot):
            due = (key, slot)
            break
    if due is None:
        return
    key, slot = due
    try:
        data = capture_photo(camera_index)
        store_photo(data, plants[0])
        state["shot_slots"][key] = dt.datetime.now().isoformat()
        save_state(state)
        report_status({"last_photo": dt.datetime.now(dt.timezone.utc).isoformat()})
    except Exception as exc:
        report_status({"last_photo_error": str(exc), "last_error_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        log(f"ERROR photo: {exc}")


async def do_sensor():
    from bleak import BleakScanner

    reading = None
    stop = asyncio.Event()

    def callback(device, advertisement_data):
        nonlocal reading
        parsed = parse_advertisement(advertisement_data.manufacturer_data)
        if parsed is None:
            return
        reading = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "plant": "tent",
            **{k: v for k, v in parsed.items() if k in ("temperature", "humidity", "vpd", "fan_speed", "unit")},
        }
        stop.set()

    async def scan():
        scanner = BleakScanner(callback)
        await scanner.start()
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        finally:
            await scanner.stop()

    await asyncio.wait_for(scan(), timeout=15)

    if reading:
        db.collection("readings").add(reading)
        log(f"sensor: {reading['temperature']}{reading['unit']} {reading['humidity']}% RH VPD {reading.get('vpd')}")
    else:
        log("sensor: no AC Infinity controller advertising")


async def sensor_loop(sensor_interval):
    while True:
        try:
            await do_sensor()
        except Exception as exc:
            report_status({"last_sensor_error": str(exc), "last_error_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            log(f"ERROR sensor: {exc}")
        await asyncio.sleep(sensor_interval * 60)


async def run_forever(args):
    tasks = [
        asyncio.create_task(_photo_loop(args)),
        asyncio.create_task(sensor_loop(args.sensor_interval)),
    ]
    await asyncio.gather(*tasks)


async def _photo_loop(args):
    while True:
        settings = fetch_settings()
        state = load_state()
        try:
            do_photo(settings, state, args.camera)
        except Exception as exc:
            report_status({"last_photo_error": str(exc), "last_error_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            log(f"ERROR photo loop: {exc}")
        await asyncio.sleep(TICK_SECONDS)


def main():
    global db, _state_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0, help="/dev/videoN index")
    parser.add_argument("--sensor-interval", type=int, default=15, help="minutes between sensor scans")
    parser.add_argument("--state", default="/var/lib/growcam/growcam.json", help="state file path")
    parser.add_argument("--once", action="store_true", help="one photo + one sensor read, then exit")
    args = parser.parse_args()

    _state_path = args.state
    os.makedirs(os.path.dirname(_state_path), exist_ok=True)

    if not firebase_admin._apps:
        cred = credentials.Certificate(os.environ["FIREBASE_SERVICE_ACCOUNT"])
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    if args.once:
        asyncio.run(_once(args))
        return

    log("grow diary daemon started")
    asyncio.run(run_forever(args))


async def _once(args):
    settings = fetch_settings(force=True)
    do_photo(settings, load_state(), args.camera)
    await do_sensor()
    report_status()


if __name__ == "__main__":
    main()
