"""GrowLab Live — phone-friendly live camera view.

Serves a single page that shows the live feed from the Raspberry Pi's
live_server.py. The Pi must be reachable at GROWLAB_PI_HOST:GROWLAB_PI_PORT
(defaults to raspberrypi.local:5555).

Run: python3 web_app.py
Open: http://<this-machine-ip>:<port> on your phone (same WiFi).
"""

import os
import socket

import requests
from flask import Flask, Response, render_template

app = Flask(__name__)

PI_HOST = os.environ.get("GROWLAB_PI_HOST", "127.0.0.1")
PI_PORT = os.environ.get("GROWLAB_PI_PORT", "5555")
FRAME_URL = f"http://{PI_HOST}:{PI_PORT}/frame"


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
def index():
    return render_template("live.html")


@app.route("/frame")
def frame():
    try:
        r = requests.get(FRAME_URL, timeout=10)
        if r.status_code != 200 or not r.content:
            return Response(b"", status=503, mimetype="text/plain")
        return Response(r.content, mimetype="image/jpeg")
    except Exception:
        return Response(b"", status=503, mimetype="text/plain")


if __name__ == "__main__":
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "localhost"

    port = int(os.environ.get("GROWLAB_WEB_PORT", "5000"))
    print("\n  GrowLab Live running:")
    print(f"    Phone:  http://{local_ip}:{port}")
    print(f"    Local:  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
