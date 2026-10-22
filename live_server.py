"""Tiny HTTP server that serves a live webcam frame on demand.

Runs on the Pi alongside the timelapse. The camera is only open for
milliseconds per request, so it won't interfere with timelapse captures.

Usage:
    python3 live_server.py                  # default port 5555
    python3 live_server.py --port 5555
    python3 live_server.py --width 640 --height 480
"""

import argparse
import os
import socketserver
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler

frame_width = 640
frame_height = 480

USB_DEVICE = "/dev/video0"
USB_CONTROLS = "auto_exposure=1,exposure_time_absolute=5,brightness=30,contrast=5"


def apply_camera_controls():
    subprocess.run(
        ["v4l2-ctl", "-d", USB_DEVICE, "-c", USB_CONTROLS],
        capture_output=True,
        timeout=5,
    )


def grab_frame(width, height):
    width = min(width, 1280)
    height = min(height, 720)
    apply_camera_controls()
    with tempfile.TemporaryDirectory() as td:
        pattern = os.path.join(td, "img%03d.jpg")
        cmd = [
            "ffmpeg",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", f"{width}x{height}",
            "-i", USB_DEVICE,
            "-frames:v", "4",
            "-y",
            pattern,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            return None
        last = os.path.join(td, "img004.jpg")
        if not os.path.isfile(last):
            frames = sorted(f for f in os.listdir(td) if f.endswith(".jpg"))
            if not frames:
                return None
            last = os.path.join(td, frames[-1])
        with open(last, "rb") as f:
            return f.read()


class FrameHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/frame":
            self.send_response(404)
            self.end_headers()
            return

        data = grab_frame(frame_width, frame_height)
        if data is None:
            self.send_response(503)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def main():
    global frame_width, frame_height

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    args = parser.parse_args()

    frame_width = args.width
    frame_height = args.height

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", args.port), FrameHandler) as httpd:
        print(f"Live view server on port {args.port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
