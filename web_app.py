"""GrowLab Web — phone-friendly interface for GrowLab.

Run: python3 web_app.py
Open: http://localhost:5000 on your phone (same WiFi).
"""

import csv
import io
import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from flask import (Flask, jsonify, render_template, request, send_file,
                   send_from_directory, url_for)
from PIL import Image

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

DATA_DIR = os.path.expanduser("~/plant-monitor/data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
PLANT_NAMES_FILE = os.path.join(DATA_DIR, "plant_names.json")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
TIMELAPSE_DIR = os.path.join(DATA_DIR, "photos", "timelapse")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def load_history():
    if os.path.isfile(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"analyses": []}


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def load_plant_names():
    try:
        if os.path.isfile(PLANT_NAMES_FILE):
            with open(PLANT_NAMES_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def get_grows():
    history = load_history()
    entries = history.get("analyses", [])
    grows = {}
    for i, entry in enumerate(entries):
        raw = entry.get("plant", "")
        if not raw:
            continue
        if " #" in raw:
            name = raw.rsplit(" #", 1)[0].strip()
        else:
            name = raw.strip()
        gid = ""
        if " #" in raw:
            gid = raw.rsplit(" #", 1)[1].strip()
        key = name
        if key not in grows:
            grows[key] = {"name": name, "grows": {}}
        if gid not in grows[key]["grows"]:
            grows[key]["grows"][gid] = {"plant": raw, "entries": [], "grow_num": gid}
        grows[key]["grows"][gid]["entries"].append(i)
    return grows, entries


# ---- Routes ----

@app.route("/")
def index():
    grows, entries = get_grows()
    plant_names = load_plant_names()
    return render_template("index.html", grows=grows, entries=entries,
                           plant_names=plant_names)


@app.route("/grow/<name>")
def grow_view(name):
    grows, entries = get_grows()
    grow = grows.get(name)
    if not grow:
        return "Grow not found", 404
    return render_template("grow.html", grow=grow, entries=entries)


@app.route("/entry/<int:idx>")
def entry_detail(idx):
    history = load_history()
    entries = history.get("analyses", [])
    if idx < 0 or idx >= len(entries):
        return "Entry not found", 404
    entry = entries[idx]
    entry_idx = idx
    raw_plant = entry.get("plant", "")
    if " #" in raw_plant:
        plant_name = raw_plant.rsplit(" #", 1)[0].strip()
        grow_num = raw_plant.rsplit(" #", 1)[1].strip()
    else:
        plant_name = raw_plant
        grow_num = ""
    return render_template("entry.html", entry=entry, entry_idx=entry_idx,
                           plant_name=plant_name, grow_num=grow_num,
                           entries=entries)


@app.route("/timelapse")
def timelapse_view():
    grows, entries = get_grows()
    tl_folders = []
    if os.path.isdir(TIMELAPSE_DIR):
        for name in sorted(os.listdir(TIMELAPSE_DIR)):
            fp = os.path.join(TIMELAPSE_DIR, name)
            if os.path.isdir(fp):
                count = len([f for f in os.listdir(fp) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
                tl_folders.append({"name": name, "count": count})
    return render_template("timelapse.html", tl_folders=tl_folders, grows=grows)


@app.route("/timelapse/<folder_name>")
def timelapse_folder(folder_name):
    folder = os.path.join(TIMELAPSE_DIR, folder_name)
    if not os.path.isdir(folder):
        return "Folder not found", 404
    dates = {}
    total = 0
    for root_dir, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in sorted(files):
            if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            fpath = os.path.join(root_dir, fname)
            date_str = ""
            if fname.startswith("tl-") and len(fname) >= 11:
                try:
                    date_str = datetime.strptime(fname[3:11], "%Y%m%d").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            if not date_str and len(fname) >= 10 and fname[4] == "-" and fname[7] == "-":
                date_str = fname[:10]
            if not date_str:
                try:
                    dt = datetime.fromtimestamp(os.path.getmtime(fpath))
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    date_str = "Unknown"
            if date_str not in dates:
                dates[date_str] = 0
            dates[date_str] += 1
            total += 1
    sorted_dates = sorted(dates.keys(), reverse=True)
    return render_template("timelapse_folder.html", folder_name=folder_name,
                           dates=dates, sorted_dates=sorted_dates, total_photos=total)


@app.route("/api/timelapse/dates/<folder>/<date>")
def api_tl_dates(folder, date):
    folder_path = os.path.join(TIMELAPSE_DIR, folder)
    if not os.path.isdir(folder_path):
        return jsonify({"error": "not found"}), 404
    photos = []
    counts = {}
    for root_dir, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in sorted(files):
            if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            fpath = os.path.join(root_dir, fname)
            date_str = ""
            if fname.startswith("tl-") and len(fname) >= 11:
                try:
                    date_str = datetime.strptime(fname[3:11], "%Y%m%d").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            if not date_str:
                try:
                    dt = datetime.fromtimestamp(os.path.getmtime(fpath))
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    date_str = "Unknown"
            if date != "all" and date_str != date:
                continue
            if date_str not in counts:
                counts[date_str] = 0
            counts[date_str] += 1
            photos.append({"fname": fname, "date": date_str, "rel": os.path.relpath(fpath, folder_path)})
    return jsonify({"photos": photos, "counts": counts})


@app.route("/charts")
def charts():
    grows, entries = get_grows()
    return render_template("charts.html", grows=grows, entries=entries)


@app.route("/settings")
def settings():
    plant_names = load_plant_names()
    return render_template("settings.html", plant_names=plant_names)


# ---- API ----

@app.route("/api/entry/<int:idx>", methods=["DELETE"])
def api_delete_entry(idx):
    history = load_history()
    entries = history.get("analyses", [])
    if idx < 0 or idx >= len(entries):
        return jsonify({"error": "not found"}), 404
    entries.pop(idx)
    save_history(history)
    return jsonify({"ok": True})


@app.route("/api/entry/<int:idx>/stage", methods=["POST"])
def api_set_stage(idx):
    history = load_history()
    entries = history.get("analyses", [])
    if idx < 0 or idx >= len(entries):
        return jsonify({"error": "not found"}), 404
    data = request.get_json()
    entries[idx]["growth_stage"] = data.get("stage", "")
    save_history(history)
    return jsonify({"ok": True})


@app.route("/api/entry/<int:idx>/yield", methods=["POST"])
def api_set_yield(idx):
    history = load_history()
    entries = history.get("analyses", [])
    if idx < 0 or idx >= len(entries):
        return jsonify({"error": "not found"}), 404
    data = request.get_json()
    entries[idx]["yield_dry_g"] = data.get("yield_dry_g")
    entries[idx]["yield_cured_g"] = data.get("yield_cured_g")
    save_history(history)
    return jsonify({"ok": True})


@app.route("/api/photo/<path:filepath>")
def api_serve_photo(filepath):
    full = os.path.expanduser(filepath)
    if not os.path.isfile(full):
        return "Not found", 404
    return send_file(full)


@app.route("/api/timelapse/photo/<folder>/<date>/<fname>")
def api_tl_photo(folder, date, fname):
    fp = os.path.join(TIMELAPSE_DIR, folder, date, fname)
    if not os.path.isfile(fp):
        return "Not found", 404
    return send_file(fp)


@app.route("/api/timelapse/thumb/<folder>/<date>/<fname>")
def api_tl_thumb(folder, date, fname):
    fp = os.path.join(TIMELAPSE_DIR, folder, date, fname)
    if not os.path.isfile(fp):
        return "Not found", 404
    thumb_dir = os.path.join(TIMELAPSE_DIR, folder, date, ".thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, fname)
    if not os.path.isfile(thumb_path):
        try:
            img = Image.open(fp)
            img.thumbnail((600, 400), Image.LANCZOS)
            img.save(thumb_path)
        except Exception:
            return send_file(fp)
    return send_file(thumb_path)


@app.route("/api/sync", methods=["POST"])
def api_sync_pi():
    result = {"status": "running"}
    def do_sync():
        try:
            pi_dir = "~/plant-monitor/data/photos/timelapse"
            for name in os.listdir(TIMELAPSE_DIR) if os.path.isdir(TIMELAPSE_DIR) else []:
                local_dir = os.path.join(TIMELAPSE_DIR, name)
                if not os.path.isdir(local_dir):
                    continue
                subprocess.run(
                    ["rsync", "-az", "--timeout=10", "--relative",
                     f"hawkewing@raspberrypi.local:{pi_dir}/{name}/",
                     local_dir + "/"],
                    timeout=120,
                )
            result["status"] = "done"
        except Exception as e:
            result["status"] = f"error: {e}"
    threading.Thread(target=do_sync, daemon=True).start()
    return jsonify(result)


@app.route("/api/sensor_data/<int:entry_idx>")
def api_sensor_data(entry_idx):
    history = load_history()
    entries = history.get("analyses", [])
    if entry_idx < 0 or entry_idx >= len(entries):
        return jsonify({"error": "not found"}), 404
    entry = entries[entry_idx]
    csv_path = entry.get("csv_path", "")
    if not (csv_path and os.path.isfile(csv_path)):
        return jsonify({})
    stats, fieldnames, raw_rows = parse_csv_file(csv_path)
    time_col = None
    for c in fieldnames:
        if c.lower() == "time":
            time_col = c
            break
    times = []
    if time_col:
        for row in raw_rows:
            t = row.get(time_col, "")
            try:
                times.append(datetime.fromisoformat(t.replace("Z", "+00:00")) if "T" in t else datetime.strptime(t, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                try:
                    times.append(datetime.strptime(t, "%m/%d/%Y, %H:%M:%S"))
                except Exception:
                    try:
                        times.append(datetime.strptime(t, "%m/%d/%Y %H:%M"))
                    except Exception:
                        times.append(None)
    series = {}
    for col, s in stats.items():
        vals = s.get("values", [])
        if not vals:
            continue
        n = len(vals)
        if n <= 40:
            labels = []
            for i in range(n):
                if i < len(times) and times[i]:
                    labels.append(times[i].strftime("%d %H:%M"))
                else:
                    labels.append(str(i))
            series[col] = {"values": vals, "labels": labels, "avg": s["avg"], "min": s["min"], "max": s["max"], "samples": s["samples"]}
        else:
            chunk = max(1, n // 40)
            reduced = []
            labels = []
            for i in range(0, n, chunk):
                chunk_vals = vals[i:i+chunk]
                reduced.append(round(sum(chunk_vals) / len(chunk_vals), 2))
                t_idx = min(i + chunk // 2, len(times) - 1) if times else -1
                if t_idx >= 0 and times[t_idx]:
                    labels.append(times[t_idx].strftime("%d %H:%M"))
                else:
                    labels.append(str(i))
            series[col] = {"values": reduced, "labels": labels, "avg": s["avg"], "min": s["min"], "max": s["max"], "samples": s["samples"]}
    return jsonify(series)


def parse_csv_file(path):
    stats = {}
    raw_rows = []
    fieldnames = []
    temp_is_celsius = False
    try:
        with open(path, newline="") as f:
            all_lines = f.readlines()
    except Exception:
        return stats, fieldnames, raw_rows

    header_idx = None
    for i, line in enumerate(all_lines):
        stripped = line.strip()
        if stripped.lower().startswith("temperature units"):
            if "c" in stripped.lower().split(",")[-1].strip():
                temp_is_celsius = True
        if stripped.startswith("Time,"):
            header_idx = i
            break
    if header_idx is None:
        return stats, fieldnames, raw_rows

    data_text = "".join(all_lines[header_idx:])
    reader = csv.DictReader(io.StringIO(data_text))
    fieldnames = reader.fieldnames or []
    for row in reader:
        if all((v or "").strip() == "" for v in row.values()):
            continue
        raw_rows.append(row)
    if not raw_rows:
        return stats, fieldnames, raw_rows

    for col in fieldnames:
        if "time" in col.lower():
            continue
        vals = []
        for row in raw_rows:
            raw = (row.get(col) or "").strip()
            if raw in ("", "N/A", "--"):
                continue
            try:
                vals.append(float(raw))
            except ValueError:
                continue
        if vals:
            avg_val = sum(vals) / len(vals)
            stats[col] = {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(avg_val, 2),
                "samples": len(vals),
                "values": vals,
            }
    return stats, fieldnames, raw_rows


@app.route("/api/entries_json")
def api_entries_json():
    history = load_history()
    return jsonify(history.get("analyses", []))


if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)

    def find_free_port(start=5000):
        for p in range(start, start + 100):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("0.0.0.0", p))
                    return p
                except OSError:
                    continue
        return start

    port = find_free_port()
    port_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".web_port")
    with open(port_file, "w") as f:
        f.write(str(port))
    print(f"\n  GrowLab Web running:")
    print(f"    Phone:  http://{local_ip}:{port}")
    print(f"    Local:  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
