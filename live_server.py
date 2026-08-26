"""Tiny HTTP server that serves a live webcam frame on demand.

Runs on the Pi alongside the timelapse. The camera is only open for
milliseconds per request, so it won't interfere with timelapse captures.

Usage:
    python3 live_server.py                  # default port 5555
    python3 live_server.py --port 5555
    python3 live_server.py --width 640 --height 480
"""

import argparse
import shutil
import socketserver
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler

frame_width = 640
frame_height = 480


class FrameHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/frame":
            self.send_response(404)
            self.end_headers()
            return

        rpicam = shutil.which("rpicam-still") or shutil.which("libcamera-still")
        if not rpicam:
            self.send_response(503)
            self.end_headers()
            return

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            cmd = [
                rpicam,
                "-o", tmp.name,
                "--width", str(frame_width),
                "--height", str(frame_height),
                "--nopreview",
                "--timeout", "3000",
                "--immediate",
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode != 0:
                self.send_response(503)
                self.end_headers()
                return

            data = tmp.read()

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

    with socketserver.TCPServer(("", args.port), FrameHandler) as httpd:
        print(f"Live view server on port {args.port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
