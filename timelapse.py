"""Timelapse capture for a Raspberry Pi camera module.

Reads settings from timelapse_config.json. Photos are saved during
light hours only (start_time to end_time) at the configured interval.

Usage:
    python3 timelapse.py                     # use default config
    python3 timelapse.py --config my.json    # custom config file
    python3 timelapse.py --once              # capture a single frame and exit

Stop with Ctrl+C — photos captured so far are kept.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time

DEFAULT_CONFIG = os.path.expanduser("~/plant-monitor/scripts/timelapse_config.json")
DEFAULT_DIR = os.path.expanduser("~/plant-monitor/photos/timelapse")

running = True
rpicam_binary = None


def handle_signal(sig, frame):
    global running
    print("\nStopping timelapse...")
    running = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def find_rpicam():
    global rpicam_binary
    for name in ("rpicam-still", "libcamera-still"):
        if shutil.which(name):
            rpicam_binary = name
            return
    print("Error: no camera tool found (rpicam-still or libcamera-still)", file=sys.stderr)
    sys.exit(1)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("interval_minutes", 20)
    cfg.setdefault("start_time", "06:00")
    cfg.setdefault("end_time", "22:00")
    cfg.setdefault("width", 1920)
    cfg.setdefault("height", 1080)
    cfg.setdefault("directory", DEFAULT_DIR)
    return cfg


def capture_frame(width, height, output_path):
    cmd = [
        rpicam_binary,
        "-o", output_path,
        "--width", str(width),
        "--height", str(height),
        "--nopreview",
        "--timeout", "5000",
        "--immediate",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=15)
    return result.returncode == 0 and os.path.isfile(output_path)


def is_within_hours(start_str, end_str):
    now = dt.datetime.now().time()
    sh, sm = (int(x) for x in start_str.split(":"))
    eh, em = (int(x) for x in end_str.split(":"))
    start = dt.time(sh, sm)
    end = dt.time(eh, em)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def seconds_until_next(interval_minutes):
    now = dt.datetime.now()
    next_time = now + dt.timedelta(minutes=interval_minutes)
    next_time = next_time.replace(second=0, microsecond=0)
    remaining = (next_time - now).total_seconds()
    return max(remaining, 1)


def run_once(cfg):
    base_dir = os.path.expanduser(cfg["directory"])
    now = dt.datetime.now()
    day_folder = now.strftime("%Y-%m-%d")
    out_dir = os.path.join(base_dir, day_folder)
    os.makedirs(out_dir, exist_ok=True)
    filename = now.strftime("tl-%Y%m%d-%H%M%S.jpg")
    path = os.path.join(out_dir, filename)
    ok = capture_frame(cfg["width"], cfg["height"], path)
    if not ok:
        print(f"[{now.strftime('%H:%M:%S')}] Failed to capture frame")
        return False
    print(f"[{now.strftime('%H:%M:%S')}] Saved {day_folder}/{filename}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to config JSON")
    parser.add_argument("--once", action="store_true", help="capture one frame and exit")
    args = parser.parse_args()

    find_rpicam()
    cfg = load_config(args.config)

    if args.once:
        run_once(cfg)
        return

    interval = cfg["interval_minutes"]
    print(f"Timelapse started — interval {interval}min, "
          f"active {cfg['start_time']} to {cfg['end_time']}")
    print(f"Camera: {rpicam_binary}")
    print(f"Photos saving to: {cfg['directory']}")
    print("Press Ctrl+C to stop\n")

    while running:
        if not is_within_hours(cfg["start_time"], cfg["end_time"]):
            print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] Outside active hours, sleeping...")
            time.sleep(60)
            continue

        run_once(cfg)
        wait = seconds_until_next(interval)

        deadline = dt.datetime.now() + dt.timedelta(seconds=wait)
        while running and dt.datetime.now() < deadline:
            time.sleep(1)

    print("Timelapse stopped.")


if __name__ == "__main__":
    main()
