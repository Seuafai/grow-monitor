"""Plant health analysis GUI.

Wraps analyze.py in a desktop interface for selecting photos, CSVs,
adding notes, and chatting with Gemini about your DWC grow.

Requirements:
    pip install google-genai Pillow

Environment:
    GEMINI_API_KEY  get yours free at https://aistudio.google.com

Usage:
    python3 grow_gui.py
"""

import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, scrolledtext, ttk

from google import genai
from google.genai import types
from PIL import Image, ImageTk

csv.field_size_limit(sys.maxsize)

DEFAULT_HISTORY = os.path.expanduser("~/plant-monitor/data/history.json")

SCHEMA_HINT = json.dumps(
    {
        "health_score": "number 0-100",
        "overall_assessment": "string",
        "issues": [{"type": "string", "severity": "string", "description": "string"}],
        "recommendations": ["string"],
    },
    indent=2,
)

DWC_CONTEXT = """
You are analyzing a Deep Water Culture (DWC) hydroponic grow.
Key DWC parameters to monitor:
- pH: should be 5.5-6.5 (optimal 5.8-6.0). Swings cause lockout.
- Water temperature: should be 65-80F (18-26C). Too warm = root rot, low dissolved oxygen.
- EC/PPM: varies by growth stage. Veg 800-1200 PPM, flower 1000-1400 PPM.
- Dissolved oxygen: air pump must run 24/7. Brown/mushy roots = root rot.
- Reservoir level: tops should not touch solution, 1-2 inches gap.
- Nutrients: use hydro-specific nutrients, not soil.
- VPD: should be 0.4-1.2 kPa depending on growth stage.
Watch for: root rot (brown slimy roots), pH swings, nutrient burn tips,
calcium deficiency (common in DWC), and light stress.
"""


def extract_date_taken(path):
    try:
        img = Image.open(path)
        exif = img.getexif()
        for tag_id in (36867, 36868, 306):
            if tag_id in exif:
                raw = exif[tag_id]
                return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:
        pass
    return None


DWC_OPTIMAL_RANGES = {
    "temp": {"min": 65, "max": 80, "unit": "F", "label": "Temperature"},
    "temperature": {"min": 65, "max": 80, "unit": "F", "label": "Temperature"},
    "humidity": {"min": 40, "max": 70, "unit": "%", "label": "Humidity"},
    "vpd": {"min": 0.8, "max": 1.2, "unit": "kPa", "label": "VPD"},
    "co2": {"min": 800, "max": 1500, "unit": "ppm", "label": "CO2"},
    "ec": {"min": 0.8, "max": 2.5, "unit": "mS/cm", "label": "EC"},
    "ph": {"min": 5.5, "max": 6.5, "unit": "", "label": "pH"},
}

def parse_csv(path):
    stats = {}
    raw_rows = []
    fieldnames = []
    temp_is_celsius = False

    with open(path, newline="") as f:
        all_lines = f.readlines()

    header_idx = None
    for i, line in enumerate(all_lines):
        stripped = line.strip()
        if stripped.lower().startswith("temperature units"):
            if "℃" in stripped or "c" in stripped.lower().split(",")[-1].strip():
                temp_is_celsius = True
        if stripped.startswith("Time,"):
            header_idx = i
            break

    if header_idx is None:
        return stats, fieldnames, raw_rows

    import io
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
            first_half = vals[: len(vals) // 2] if len(vals) > 2 else vals
            second_half = vals[len(vals) // 2 :] if len(vals) > 2 else vals
            first_avg = sum(first_half) / len(first_half) if first_half else 0
            second_avg = sum(second_half) / len(second_half) if second_half else 0
            diff = second_avg - first_avg
            pct_change = (diff / first_avg * 100) if first_avg else 0
            if abs(pct_change) < 3:
                trend = "stable"
            elif pct_change > 0:
                trend = "rising"
            else:
                trend = "falling"

            col_lower = col.lower()
            optimal = None
            range_status = "ok"
            for key, r in DWC_OPTIMAL_RANGES.items():
                if key in col_lower:
                    optimal = r
                    if avg_val < r["min"]:
                        range_status = "low"
                    elif avg_val > r["max"]:
                        range_status = "high"
                    break

            if temp_is_celsius and "temp" in col_lower and optimal and optimal["unit"] == "F":
                optimal = {"min": round((optimal["min"] - 32) * 5/9, 1), "max": round((optimal["max"] - 32) * 5/9, 1), "unit": "°C", "label": optimal["label"]}
                if avg_val < optimal["min"]:
                    range_status = "low"
                elif avg_val > optimal["max"]:
                    range_status = "high"
                else:
                    range_status = "ok"

            stats[col] = {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(avg_val, 2),
                "samples": len(vals),
                "values": vals,
                "trend": trend,
                "pct_change": round(pct_change, 1),
                "optimal": optimal,
                "range_status": range_status,
            }
    return stats, fieldnames, raw_rows


TIME_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y, %H:%M:%S",
              "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S")


def _parse_time_col(raw_t):
    for fmt in TIME_FMTS:
        try:
            return datetime.strptime(raw_t.strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw_t.strip())
    except Exception:
        return None


def compute_windowed_csv_stats(csv_path, start_dt, end_dt):
    stats, fieldnames, raw_rows = parse_csv(csv_path)
    if not raw_rows:
        return {}
    time_col = None
    for col in fieldnames:
        if "time" in col.lower() or "date" in col.lower():
            time_col = col
            break
    if not time_col:
        return stats
    filtered = []
    for row in raw_rows:
        parsed = _parse_time_col(row.get(time_col, ""))
        if parsed is None:
            continue
        if start_dt and parsed < start_dt:
            continue
        if end_dt and parsed >= end_dt:
            continue
        filtered.append(row)
    if not filtered:
        return {}
    windowed = {}
    for col in fieldnames:
        if "time" in col.lower():
            continue
        vals = []
        for row in filtered:
            raw = row.get(col, "").strip()
            if raw in ("", "N/A", "--"):
                continue
            try:
                vals.append(float(raw))
            except ValueError:
                continue
        if not vals:
            continue
        avg_val = sum(vals) / len(vals)
        first_half = vals[:len(vals) // 2] if len(vals) > 2 else vals
        second_half = vals[len(vals) // 2:] if len(vals) > 2 else vals
        first_avg = sum(first_half) / len(first_half) if first_half else 0
        second_avg = sum(second_half) / len(second_half) if second_half else 0
        diff = second_avg - first_avg
        pct_change = (diff / first_avg * 100) if first_avg else 0
        trend = "stable" if abs(pct_change) < 3 else ("rising" if pct_change > 0 else "falling")
        col_lower = col.lower()
        optimal = None
        range_status = "ok"
        for key, r in DWC_OPTIMAL_RANGES.items():
            if key in col_lower:
                optimal = r
                if avg_val < r["min"]:
                    range_status = "low"
                elif avg_val > r["max"]:
                    range_status = "high"
                break
        windowed[col] = {
            "min": round(min(vals), 2), "max": round(max(vals), 2),
            "avg": round(avg_val, 2), "samples": len(vals),
            "trend": trend, "pct_change": round(pct_change, 1),
            "optimal": optimal, "range_status": range_status,
        }
    return windowed


def load_history(path):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return {"analyses": []}


def save_history(path, history):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(history, f, indent=2)


def build_history_text(history):
    if not history["analyses"]:
        return ""
    past = []
    for entry in history["analyses"][-10:]:
        plant_str = f" [{entry.get('plant', '')}]" if entry.get("plant") else ""
        score = entry.get("analysis", {}).get("health_score", "?")
        assessment = entry.get("analysis", {}).get("overall_assessment", "")[:120]
        note_str = f" | note: {entry.get('note')}" if entry.get("note") else ""
        past.append(f"  [{entry.get('date_taken', 'unknown')}]{plant_str} score={score} -- {assessment}{note_str}")
    return "\nPast observations (most recent 10):\n" + "\n".join(past)


# --- Colors ---
BG = "#0d1117"
BG_CARD = "#161b22"
BG_INPUT = "#21262d"
BG_HEADER = "#010409"
FG = "#e6edf3"
FG_DIM = "#7d8590"
FG_ACCENT = "#58a6ff"
GREEN = "#3fb950"
RED = "#f85149"
YELLOW = "#d29922"
ORANGE = "#db6d28"
BORDER = "#30363d"
SIDEBAR_BG = "#0d1117"


class GrowGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GrowLab")
        self.root.geometry("1280x820")
        self.root.minsize(960, 650)
        self.root.configure(bg=BG)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_INPUT, bordercolor=BORDER)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TFrame", background=BG)
        style.configure("TCombobox", fieldbackground=BG_INPUT, foreground=FG)
        style.configure("TButton", background=BG_CARD, foreground=FG)
        style.map("TButton", background=[("active", BORDER)])
        style.configure("Accent.TButton", background="#238636", foreground="white")
        style.map("Accent.TButton", background=[("active", "#2ea043")])
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), foreground=FG_ACCENT)
        style.configure("CardHeader.TLabel", font=("Segoe UI", 10, "bold"), foreground=FG)
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground=FG_DIM)
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), background="#238636", foreground="white")
        style.configure("Treeview", background=BG_INPUT, foreground=FG, fieldbackground=BG_INPUT, rowheight=26)
        style.configure("Treeview.Heading", background=BG_CARD, foreground=FG_ACCENT, font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1f3a5f")])
        style.configure("Metric.TLabel", font=("Consolas", 18, "bold"), foreground=FG)
        style.configure("MetricDim.TLabel", font=("Segoe UI", 8), foreground=FG_DIM)

        self.plant_names_file = os.path.expanduser("~/plant-monitor/data/plant_names.json")
        self.hist_photo_images = []
        self._grow_tree_map = {}
        self._entry_tree_map = {}
        self._tl_dates = {}
        self._tl_photos = []
        self._tl_photo_items = []
        self._tl_thumb_cache = {}
        self._tl_pending_paths = []
        self._tl_loaded_count = 0
        self._tl_batch_size = 20
        self.tl_slideshow_id = None
        self.tl_slideshow_idx = 0
        self.tl_slideshow_paths = []
        self._tl_selected = set()
        self._tl_excluded = set()
        self._current_view = None
        self._sidebar_grow_items = {}
        self._sidebar_tl_items = {}
        self._active_grow = None

        self._build_ui()

    # ================================================================== UI
    def _build_ui(self):
        # Status bar (bottom, always visible)
        self.status_frame = tk.Frame(self.root, bg=BG_HEADER, height=30)
        self.status_frame.pack(side="bottom", fill="x")
        self.status_frame.pack_propagate(False)
        self.tl_status = tk.Label(self.status_frame, text="", bg=BG_HEADER, fg=FG_DIM, font=("Consolas", 9))
        self.tl_status.pack(side="right", padx=10)
        tk.Label(self.status_frame, text=" GrowLab", bg=BG_HEADER, fg=FG_DIM, font=("Segoe UI", 8)).pack(side="left", padx=10)

        # Main area: sidebar | content
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        # Sidebar
        self.sidebar = tk.Frame(main, bg=SIDEBAR_BG, width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        # Divider
        sep = tk.Frame(main, bg=BORDER, width=1)
        sep.pack(side="left", fill="y")

        # Content area
        self.content_frame = ttk.Frame(main)
        self.content_frame.pack(side="left", fill="both", expand=True)

        # Default to log view
        self._show_log_view()
        self._refresh_history()
        self.root.after(500, self._check_pi_connection)
        self.root.after(2000, self._start_timelapse_monitor)

    def _build_sidebar(self):
        # App title
        tk.Label(self.sidebar, text=" GrowLab", bg=SIDEBAR_BG, fg=FG_ACCENT,
                 font=("Segoe UI", 16, "bold"), anchor="w").pack(fill="x", padx=10, pady=(14, 0))
        tk.Label(self.sidebar, text=" DWC Health Monitor", bg=SIDEBAR_BG, fg=FG_DIM,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10, pady=(0, 10))

        # --- Grows section ---
        tk.Label(self.sidebar, text=" GROWS", bg=SIDEBAR_BG, fg=FG_DIM,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(4, 2))

        self.sidebar_grow_list = tk.Listbox(
            self.sidebar, bg=BG_INPUT, fg=FG, selectbackground="#1f3a5f",
            selectforeground="white", font=("Segoe UI", 9), relief="flat",
            activestyle="none", height=8, bd=0, highlightthickness=0,
            selectborderwidth=0,
        )
        self.sidebar_grow_list.pack(fill="x", padx=8)
        self.sidebar_grow_list.bind("<<ListboxSelect>>", self._on_sidebar_grow_select)

        grow_btns = tk.Frame(self.sidebar, bg=SIDEBAR_BG)
        grow_btns.pack(fill="x", padx=8, pady=4)
        tk.Button(grow_btns, text="+ New Grow", command=self._new_grow_dialog,
                  bg="#238636", fg="white", font=("Segoe UI", 8, "bold"),
                  bd=0, padx=8, pady=3, cursor="hand2").pack(side="left")
        tk.Button(grow_btns, text="+ Entry", command=self._show_entry_form,
                  bg=BG_CARD, fg=FG, font=("Segoe UI", 8),
                  bd=0, padx=8, pady=3, cursor="hand2").pack(side="left", padx=(6, 0))

        # --- Separator ---
        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x", padx=10, pady=10)

        # --- Timelapse section ---
        tk.Label(self.sidebar, text=" TIMELAPSE", bg=SIDEBAR_BG, fg=FG_DIM,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 2))

        self.sidebar_tl_list = tk.Listbox(
            self.sidebar, bg=BG_INPUT, fg=FG, selectbackground="#1f3a5f",
            selectforeground="white", font=("Segoe UI", 9), relief="flat",
            activestyle="none", height=8, bd=0, highlightthickness=0,
            selectborderwidth=0,
        )
        self.sidebar_tl_list.pack(fill="x", padx=8)
        self.sidebar_tl_list.bind("<<ListboxSelect>>", self._on_sidebar_tl_select)

        tl_btns = tk.Frame(self.sidebar, bg=SIDEBAR_BG)
        tl_btns.pack(fill="x", padx=8, pady=4)
        tk.Button(tl_btns, text="Timelapse", command=self._show_timelapse_view,
                  bg=BG_CARD, fg=FG, font=("Segoe UI", 8),
                  bd=0, padx=8, pady=3, cursor="hand2").pack(side="left")

        self.tl_status_frame = tk.Frame(self.sidebar, bg=SIDEBAR_BG)
        self.tl_status_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.tl_status_dot = tk.Label(self.tl_status_frame, text="\u25cf", bg=SIDEBAR_BG, fg="#666",
                                      font=("Segoe UI", 8))
        self.tl_status_dot.pack(side="left")
        self.tl_status_text = tk.Label(self.tl_status_frame, text="Checking...", bg=SIDEBAR_BG, fg=FG_DIM,
                                       font=("Segoe UI", 8))
        self.tl_status_text.pack(side="left", padx=(2, 0))
        tk.Button(self.tl_status_frame, text="Restart", command=self._tl_restart,
                  bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 7),
                  bd=0, padx=4, pady=1, cursor="hand2").pack(side="right")

        # --- Separator ---
        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x", padx=10, pady=10)

        # --- Bottom buttons ---
        tk.Button(self.sidebar, text="Live View", command=self._open_live_view,
                  bg=BG_CARD, fg=FG, font=("Segoe UI", 9),
                  bd=0, padx=8, pady=4, cursor="hand2", anchor="w").pack(fill="x", padx=8, pady=(0, 4))

        # Sidebar refresh button
        tk.Button(self.sidebar, text="Refresh", command=self._refresh_all,
                  bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 8),
                  bd=0, padx=8, pady=3, cursor="hand2", anchor="w").pack(fill="x", padx=8)

    def _refresh_all(self):
        self._refresh_history()
        self._refresh_sidebar_grows()
        self._refresh_sidebar_tl()
        self._tl_refresh_grows()

    def _check_timelapse_status(self):
        def check():
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     "hawkewing@raspberrypi.local", "pgrep -f 'timelapse.py'"],
                    capture_output=True, text=True, timeout=8,
                )
                running = bool(result.stdout.strip())
                self.root.after(0, lambda: self._update_tl_status(running))
            except Exception:
                self.root.after(0, lambda: self._update_tl_status(None))
        threading.Thread(target=check, daemon=True).start()

    def _update_tl_status(self, running):
        if running is True:
            self.tl_status_dot.config(fg="#2ea043")
            self.tl_status_text.config(text="Running")
        elif running is False:
            self.tl_status_dot.config(fg="#e04040")
            self.tl_status_text.config(text="Stopped")
        else:
            self.tl_status_dot.config(fg="#e0a030")
            self.tl_status_text.config(text="Pi offline")

    def _tl_restart(self):
        def do_restart():
            try:
                self.root.after(0, lambda: self.tl_status_text.config(text="Restarting..."))
                self.root.after(0, lambda: self.tl_status_dot.config(fg="#e0a030"))
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     "hawkewing@raspberrypi.local",
                     "pkill -f timelapse.py; sleep 1; cd ~/plant-monitor && nohup python3 scripts/timelapse.py > logs/timelapse.log 2>&1 &"],
                    capture_output=True, timeout=15,
                )
                time.sleep(2)
                self.root.after(0, lambda: self._check_timelapse_status())
            except Exception:
                self.root.after(0, lambda: self._update_tl_status(None))
        threading.Thread(target=do_restart, daemon=True).start()

    def _start_timelapse_monitor(self):
        self._check_timelapse_status()
        self.root.after(30000, self._start_timelapse_monitor)

    # ----------------------------------------------------------- Content views
    def _clear_content(self):
        for w in self.content_frame.winfo_children():
            w.destroy()

    def _show_log_view(self):
        if self._current_view == "log":
            return
        self._clear_content()
        self._current_view = "log"
        container = self.content_frame
        pad = {"padx": 8, "pady": 4}

        # Top action bar
        top_bar = ttk.Frame(container)
        top_bar.pack(fill="x", **pad)
        ttk.Button(top_bar, text="New Grow", style="Accent.TButton", command=self._new_grow_dialog).pack(side="left")
        ttk.Button(top_bar, text="Add Entry", command=self._show_entry_form).pack(side="left", padx=(8, 0))
        ttk.Button(top_bar, text="Edit", command=self._edit_history_entry).pack(side="right")
        ttk.Button(top_bar, text="Delete Entry", command=self._delete_history_entry).pack(side="right", padx=(0, 4))
        ttk.Button(top_bar, text="Delete Grow", command=self._delete_grow).pack(side="right", padx=(0, 4))

        # Split: entries list + detail
        split = ttk.PanedWindow(container, orient="vertical")
        split.pack(fill="both", expand=True, **pad)

        # -- Entry list (top) --
        entries_frame = ttk.Frame(split)
        split.add(entries_frame, weight=1)

        entry_cols = ("date", "type", "score", "note")
        self.hist_entry_tree = ttk.Treeview(entries_frame, columns=entry_cols, show="headings", selectmode="browse", height=8)
        self.hist_entry_tree.heading("date", text="Date")
        self.hist_entry_tree.heading("type", text="Type")
        self.hist_entry_tree.heading("score", text="Score")
        self.hist_entry_tree.heading("note", text="Note")
        self.hist_entry_tree.column("date", width=120)
        self.hist_entry_tree.column("type", width=55, anchor="center")
        self.hist_entry_tree.column("score", width=45, anchor="center")
        self.hist_entry_tree.column("note", width=350)

        esb = ttk.Scrollbar(entries_frame, orient="vertical", command=self.hist_entry_tree.yview)
        self.hist_entry_tree.configure(yscrollcommand=esb.set)
        self.hist_entry_tree.pack(side="left", fill="both", expand=True)
        esb.pack(side="right", fill="y")
        self.hist_entry_tree.bind("<<TreeviewSelect>>", self._on_entry_select)

        # -- Detail notebook (bottom) --
        detail_nb = ttk.Notebook(split)
        split.add(detail_nb, weight=2)

        detail_tab = ttk.Frame(detail_nb)
        detail_nb.add(detail_tab, text=" Detail ")
        self.hist_detail = scrolledtext.ScrolledText(
            detail_tab, wrap="word", state="disabled",
            bg=BG_CARD, fg=FG, relief="flat",
            font=("Consolas", 9), padx=6, pady=4,
        )
        self.hist_detail.pack(fill="both", expand=True)

        photo_tab = ttk.Frame(detail_nb)
        detail_nb.add(photo_tab, text=" Photos ")
        photo_scroll_frame = tk.Frame(photo_tab, bg=BG_CARD)
        photo_scroll_frame.pack(fill="both", expand=True)
        self.hist_photo_canvas = tk.Canvas(photo_scroll_frame, bg=BG_CARD, highlightthickness=0)
        hist_photo_scroll = ttk.Scrollbar(photo_scroll_frame, orient="vertical", command=self.hist_photo_canvas.yview)
        self.hist_photo_canvas.configure(yscrollcommand=hist_photo_scroll.set)
        hist_photo_scroll.pack(side="right", fill="y")
        self.hist_photo_canvas.pack(side="left", fill="both", expand=True)
        self.hist_photo_images = []

        # Populate grow info header if a grow is active
        if self._active_grow:
            self._on_sidebar_grow_select(None)

    def _show_timelapse_view(self):
        if self._current_view == "timelapse":
            return
        self._clear_content()
        self._current_view = "timelapse"
        container = self.content_frame
        pad = {"padx": 8, "pady": 4}

        # -- Folder row --
        ff = ttk.Frame(container)
        ff.pack(fill="x", **pad)
        ttk.Label(ff, text="Folder:", font=("Segoe UI", 9, "bold")).pack(side="left")
        self.tl_folder_var = tk.StringVar()
        self.tl_folder = tk.Entry(ff, font=("Consolas", 9), textvariable=self.tl_folder_var, bg=BG_INPUT, fg=FG, insertbackground=FG)
        self.tl_folder.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(ff, text="Browse...", command=self._tl_browse_folder).pack(side="left", padx=(4, 0))
        ttk.Button(ff, text="Sync from Pi", command=self._tl_sync_from_pi).pack(side="left", padx=(4, 0))
        ttk.Button(ff, text="Load", command=self._tl_load_folder).pack(side="left", padx=(4, 0))

        # -- Playback controls --
        sf = ttk.Frame(container)
        sf.pack(fill="x", **pad)
        ttk.Button(sf, text="Play", command=self._tl_play).pack(side="left")
        ttk.Button(sf, text="Select All", command=self._tl_select_all).pack(side="left", padx=(12, 0))
        ttk.Button(sf, text="Load from History", command=self._tl_load_from_history).pack(side="left", padx=(12, 0))
        ttk.Label(sf, text="Speed (ms):").pack(side="left", padx=(20, 0))
        self.tl_speed = tk.Entry(sf, font=("Consolas", 9), width=6, bg=BG_INPUT, fg=FG, insertbackground=FG)
        self.tl_speed.insert(0, "500")
        self.tl_speed.pack(side="left", padx=(4, 0))
        ttk.Label(sf, text="Size:").pack(side="left", padx=(12, 0))
        self.tl_size_var = tk.StringVar(value="Fullscreen")
        ttk.Combobox(sf, textvariable=self.tl_size_var, state="readonly", width=14,
                      values=["Fullscreen", "1920x1080", "1280x720", "800x600"],
                      font=("Consolas", 9)).pack(side="left", padx=(4, 0))

        # -- Split: dates list + photo grid --
        split = ttk.Frame(container)
        split.pack(fill="both", expand=True, **pad)

        left = ttk.Frame(split)
        left.pack(side="left", fill="y", padx=(0, 8))
        self.tl_date_list = tk.Listbox(
            left, width=16, bg=BG_INPUT, fg=FG, selectbackground="#1f3a5f",
            selectforeground="white", font=("Consolas", 9), relief="flat", activestyle="none",
            selectmode="extended",
        )
        self.tl_date_list.pack(fill="both", expand=True)
        self.tl_date_list.bind("<<ListboxSelect>>", self._tl_on_date_select)

        right = ttk.Frame(split)
        right.pack(side="left", fill="both", expand=True)
        self.tl_canvas = tk.Canvas(right, bg=BG_CARD, highlightthickness=0)
        self.tl_scroll = ttk.Scrollbar(right, orient="vertical", command=self.tl_canvas.yview)
        self.tl_canvas.configure(yscrollcommand=self.tl_scroll.set)
        self.tl_scroll.pack(side="right", fill="y")
        self.tl_canvas.pack(side="left", fill="both", expand=True)
        self.tl_grid_frame = ttk.Frame(self.tl_canvas)
        self.tl_canvas_window = self.tl_canvas.create_window((0, 0), window=self.tl_grid_frame, anchor="nw")
        self.tl_canvas.bind("<Configure>", lambda e: self.tl_canvas.itemconfig(self.tl_canvas_window, width=e.width))
        self.tl_canvas.bind("<MouseWheel>", self._tl_on_scroll)
        self.tl_canvas.bind("<Button-4>", self._tl_on_scroll)
        self.tl_canvas.bind("<Button-5>", self._tl_on_scroll)
        self.tl_photos = []
        self.tl_photo_items = []

        self._tl_refresh_grows()
        self._refresh_timelapse_dates()

    def _show_entry_form(self, grow_num=None, plant_name=None):
        self._clear_content()
        self._current_view = "entry_form"
        container = self.content_frame

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        form = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        pad = {"padx": 6, "pady": 3}

        form.image_paths = []
        form.image_types = []
        form.csv_path = None
        form.csv_stats = None
        form.csv_fieldnames = None
        form.csv_raw_rows = None
        form.nutrient_rows = []
        form.thumb_photos = []
        form.thumb_items = []

        # -- Back button + header --
        hdr = ttk.Frame(form)
        hdr.pack(fill="x", **pad)
        ttk.Button(hdr, text="< Back", command=self._show_log_view).pack(side="left")
        if grow_num:
            ttk.Label(hdr, text=f"  New Entry -- #{grow_num}", style="Header.TLabel").pack(side="left", padx=(12, 0))
        else:
            ttk.Label(hdr, text="  New Grow", style="Header.TLabel").pack(side="left", padx=(12, 0))

        # -- Plant info --
        info = ttk.Frame(form)
        info.pack(fill="x", **pad)
        ttk.Label(info, text="Plant:", font=("Segoe UI", 9, "bold")).pack(side="left")
        plant_names = self._load_plant_names()
        form.plant_var = tk.StringVar(value=plant_name or "")
        ttk.Combobox(info, textvariable=form.plant_var, width=18, values=plant_names).pack(side="left", padx=(4, 4))
        ttk.Label(info, text="#", font=("Segoe UI", 9, "bold")).pack(side="left")
        form.grow_num_var = tk.StringVar(value=grow_num or "1")
        tk.Entry(info, font=("Consolas", 9), width=3, textvariable=form.grow_num_var, bg=BG_INPUT, fg=FG, insertbackground=FG).pack(side="left", padx=(2, 12))
        ttk.Label(info, text="Planted:", font=("Segoe UI", 9, "bold")).pack(side="left")
        form.date_planted = tk.Entry(info, font=("Consolas", 9), width=10, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.date_planted.pack(side="left", padx=(4, 12))
        form.date_planted.insert(0, "YYYY-MM-DD")
        ttk.Label(info, text="Popped:", font=("Segoe UI", 9, "bold")).pack(side="left")
        form.date_popped = tk.Entry(info, font=("Consolas", 9), width=10, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.date_popped.pack(side="left", padx=(4, 0))
        form.date_popped.insert(0, "YYYY-MM-DD")
        ttk.Label(info, text="Observed:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(12, 0))
        form.date_observed = tk.Entry(info, font=("Consolas", 9), width=10, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.date_observed.pack(side="left", padx=(4, 0))
        form.date_observed.insert(0, datetime.now().strftime("%Y-%m-%d"))

        # -- Pre-fill from last entry of this grow --
        entries = load_history(DEFAULT_HISTORY).get("analyses", [])
        light_on = light_off = dli = notes = ph = ec = litres = ""
        gn = grow_num or form.grow_num_var.get().strip()
        if gn and entries:
            for e in reversed(entries):
                raw = e.get("plant", "")
                if raw and raw.endswith(f" #{gn}"):
                    if e.get("date_planted"):
                        form.date_planted.delete(0, "end")
                        form.date_planted.insert(0, e["date_planted"])
                    if e.get("date_popped"):
                        form.date_popped.delete(0, "end")
                        form.date_popped.insert(0, e["date_popped"])
                    lighting = e.get("lighting", {})
                    nutrient = e.get("nutrients", {})
                    notes = e.get("note", "")
                    ph = nutrient.get("ph", "")
                    ec = nutrient.get("ec", "")
                    litres = nutrient.get("reservoir_litres", "")
                    light_on = lighting.get("light_on", "")
                    light_off = lighting.get("light_off", "")
                    dli = lighting.get("dli", "")
                    break

        # -- Model / Pi --
        model_row = ttk.Frame(form)
        model_row.pack(fill="x", **pad)
        ttk.Label(model_row, text="Model:", font=("Segoe UI", 9, "bold")).pack(side="left")
        form.model_var = tk.StringVar(value="gemini-3.6-flash")
        ttk.Combobox(model_row, textvariable=form.model_var, width=20, state="readonly",
            values=["gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.5-pro"]).pack(side="left", padx=(4, 12))

        # -- Photos --
        ttk.Label(form, text="Photos", style="Header.TLabel").pack(anchor="w", **pad)
        pf = ttk.Frame(form)
        pf.pack(fill="x", **pad)
        def browse_photos():
            paths = filedialog.askopenfilenames(filetypes=[("Images", "*.jpg *.jpeg *.png *.webp")])
            if paths:
                form.image_paths.extend(paths)
                form.image_types.extend([form.photo_type_var.get()] * len(paths))
                refresh_thumbs()
        ttk.Button(pf, text="Add Photos...", command=browse_photos).pack(side="left")
        def clear_photos():
            form.image_paths.clear()
            form.image_types.clear()
            refresh_thumbs()
            form.photo_label.config(text="No photos selected")
        ttk.Button(pf, text="Clear", command=clear_photos).pack(side="left", padx=(4, 0))
        form.photo_label = ttk.Label(pf, text="No photos selected", style="Sub.TLabel")
        form.photo_label.pack(side="left", padx=(8, 0))
        ttk.Label(pf, text="Type:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(12, 0))
        form.photo_type_var = tk.StringVar(value="plant")
        ttk.Combobox(pf, textvariable=form.photo_type_var, width=10, state="readonly",
            values=["plant", "pH", "EC", "nutrients", "other"]).pack(side="left", padx=(4, 0))

        thumb_outer = ttk.Frame(form)
        thumb_outer.pack(fill="x", **pad)
        form.thumb_canvas = tk.Canvas(thumb_outer, height=160, bg=BG_CARD, highlightthickness=0)
        form.thumb_scroll = ttk.Scrollbar(thumb_outer, orient="horizontal", command=form.thumb_canvas.xview)
        form.thumb_canvas.configure(xscrollcommand=form.thumb_scroll.set)
        form.thumb_scroll.pack(side="bottom", fill="x")
        form.thumb_canvas.pack(side="top", fill="x")
        form.thumb_frame = ttk.Frame(form.thumb_canvas)
        form.thumb_canvas_window = form.thumb_canvas.create_window((0, 0), window=form.thumb_frame, anchor="nw")
        form.thumb_canvas.bind("<Configure>", lambda e: form.thumb_canvas.itemconfig(form.thumb_canvas_window, height=155))

        def refresh_thumbs():
            for w in form.thumb_frame.winfo_children():
                w.destroy()
            form.thumb_photos.clear()
            form.thumb_items.clear()
            form.photo_label.config(text=f"{len(form.image_paths)} photo(s)")
            for idx, p in enumerate(form.image_paths):
                try:
                    img = Image.open(p)
                    img.thumbnail((150, 150), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    form.thumb_photos.append(photo)
                    f = tk.Frame(form.thumb_frame, bg=BG_CARD, bd=1, relief="solid")
                    f.pack(side="left", padx=2, pady=2)
                    lbl = tk.Label(f, image=photo, bg=BG_CARD)
                    lbl.pack()
                    lbl.bind("<Button-3>", lambda e, i=idx: _rotate_thumb(i))
                    form.thumb_items.append(f)
                except Exception:
                    pass

        def _rotate_thumb(idx):
            if idx >= len(form.image_paths):
                return
            path = form.image_paths[idx]
            try:
                img = Image.open(path)
                img = img.rotate(-90, expand=True)
                img.save(path)
                refresh_thumbs()
            except Exception as e:
                messagebox.showerror("Rotate Failed", str(e))

        # -- CSV --
        ttk.Label(form, text="Sensor CSV", style="Header.TLabel").pack(anchor="w", **pad)
        cf = ttk.Frame(form)
        cf.pack(fill="x", **pad)
        def browse_csv():
            path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
            if path:
                try:
                    stats, fieldnames, raw_rows = parse_csv(path)
                    form.csv_path = path
                    form.csv_stats = stats
                    form.csv_fieldnames = fieldnames
                    form.csv_raw_rows = raw_rows
                    n = len(raw_rows)
                    form.csv_label.config(text=f"{os.path.basename(path)} ({n} rows)")
                except Exception as e:
                    form.csv_label.config(text=f"Error: {e}")
        def clear_csv():
            form.csv_path = None
            form.csv_stats = None
            form.csv_fieldnames = None
            form.csv_raw_rows = None
            form.csv_label.config(text="No CSV selected")
        ttk.Button(cf, text="Select CSV...", command=browse_csv).pack(side="left")
        ttk.Button(cf, text="Clear", command=clear_csv).pack(side="left", padx=(4, 0))
        form.csv_label = ttk.Label(cf, text="No CSV selected", style="Sub.TLabel")
        form.csv_label.pack(side="left", padx=(8, 0))

        # -- Lighting / DLI --
        ttk.Label(form, text="Lighting / DLI", style="Header.TLabel").pack(anchor="w", **pad)
        lf = ttk.Frame(form)
        lf.pack(fill="x", **pad)
        ttk.Label(lf, text="On:").grid(row=0, column=0, sticky="w")
        form.light_on = tk.Entry(lf, font=("Consolas", 9), width=7, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.light_on.grid(row=0, column=1, padx=(4, 10))
        ttk.Label(lf, text="Off:").grid(row=0, column=2, sticky="w")
        form.light_off = tk.Entry(lf, font=("Consolas", 9), width=7, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.light_off.grid(row=0, column=3, padx=(4, 10))
        ttk.Label(lf, text="DLI:").grid(row=0, column=4, sticky="w")
        form.dli_manual = tk.Entry(lf, font=("Consolas", 9), width=7, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.dli_manual.grid(row=0, column=5, padx=(4, 10))

        if light_on:
            form.light_on.insert(0, light_on)
        if light_off:
            form.light_off.insert(0, light_off)
        if dli:
            form.dli_manual.insert(0, str(dli))

        # -- Nutrients / pH / EC --
        ttk.Label(form, text="Nutrients / pH / EC", style="Header.TLabel").pack(anchor="w", **pad)
        nf = ttk.Frame(form)
        nf.pack(fill="x", **pad)
        nl = ttk.Frame(nf)
        nl.pack(fill="x")
        ttk.Label(nl, text="Mix:", font=("Segoe UI", 9, "bold")).pack(side="left")
        form.nutrient_rows_frame = ttk.Frame(nl)
        form.nutrient_rows_frame.pack(side="left", padx=(4, 0))

        def add_nutrient_row(name="", strength=""):
            row_frame = tk.Frame(form.nutrient_rows_frame)
            row_frame.pack(side="left", padx=(0, 4))
            name_var = tk.StringVar(value=name)
            strength_var = tk.StringVar(value=strength)
            tk.Entry(row_frame, font=("Consolas", 8), width=12, textvariable=name_var, bg=BG_INPUT, fg=FG, insertbackground=FG).pack(side="left")
            tk.Entry(row_frame, font=("Consolas", 8), width=5, textvariable=strength_var, bg=BG_INPUT, fg=FG, insertbackground=FG).pack(side="left", padx=(1, 0))
            ttk.Label(row_frame, text="ml/L", style="Sub.TLabel").pack(side="left")
            def remove(r=row_frame):
                for row in form.nutrient_rows:
                    if row["frame"] is r:
                        form.nutrient_rows.remove(row)
                        break
                r.destroy()
            ttk.Button(row_frame, text="x", command=remove, width=2).pack(side="left", padx=(1, 0))
            form.nutrient_rows.append({"frame": row_frame, "name_var": name_var, "strength_var": strength_var})

        ttk.Button(nl, text="+", command=add_nutrient_row, width=3).pack(side="left", padx=(4, 0))

        rf = ttk.Frame(nf)
        rf.pack(fill="x", pady=(3, 0))
        ttk.Label(rf, text="Reservoir (L):").pack(side="left")
        form.nutrient_litres = tk.Entry(rf, font=("Consolas", 9), width=6, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.nutrient_litres.pack(side="left", padx=(4, 10))
        ttk.Label(rf, text="pH:").pack(side="left")
        form.ph_entry = tk.Entry(rf, font=("Consolas", 9), width=6, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.ph_entry.pack(side="left", padx=(4, 10))
        ttk.Label(rf, text="EC:").pack(side="left")
        form.ec_entry = tk.Entry(rf, font=("Consolas", 9), width=6, bg=BG_INPUT, fg=FG, insertbackground=FG)
        form.ec_entry.pack(side="left", padx=(4, 10))

        if litres:
            form.nutrient_litres.insert(0, str(litres))
        if ph:
            form.ph_entry.insert(0, str(ph))
        if ec:
            form.ec_entry.insert(0, str(ec))

        existing_mix = []
        if gn and entries:
            for e in reversed(entries):
                raw = e.get("plant", "")
                if raw and raw.endswith(f" #{gn}"):
                    existing_mix = e.get("nutrients", {}).get("mix", [])
                    break
        for n in existing_mix:
            add_nutrient_row(n.get("name", ""), str(n.get("strength_ml_per_l", "")) if n.get("strength_ml_per_l") else "")

        # -- Notes --
        ttk.Label(form, text="Notes", style="Header.TLabel").pack(anchor="w", **pad)
        form.notes = tk.Text(form, wrap="word", bg=BG_INPUT, fg=FG, insertbackground=FG,
            relief="flat", font=("Segoe UI", 9), padx=6, pady=4, height=3)
        form.notes.pack(fill="x", **pad)
        if notes:
            form.notes.insert("1.0", notes)

        # -- Results --
        ttk.Label(form, text="Results", style="Header.TLabel").pack(anchor="w", **pad)
        form.results = scrolledtext.ScrolledText(form, height=8, wrap="word", state="disabled",
            bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=("Consolas", 9), padx=6, pady=4)
        form.results.pack(fill="both", expand=True, **pad)

        def set_results(text):
            form.results.config(state="normal")
            form.results.delete("1.0", "end")
            form.results.insert("1.0", text)
            form.results.config(state="disabled")

        # -- Analyze button --
        def do_analyze():
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                messagebox.showwarning("No API Key", "Set GEMINI_API_KEY environment variable.")
                return
            analyze_btn.config(state="disabled", text="Analyzing...")
            threading.Thread(target=lambda: _run_in_thread(), daemon=True).start()

        def _run_in_thread():
            try:
                api_key = os.environ.get("GEMINI_API_KEY", "").strip()
                client = genai.Client(api_key=api_key)
                model_name = form.model_var.get()
                plant = f"{form.plant_var.get().strip()} #{form.grow_num_var.get().strip()}"
                lighting = {
                    "light_on": form.light_on.get().strip(),
                    "light_off": form.light_off.get().strip(),
                }
                try:
                    on_t = self._parse_time(lighting["light_on"])
                    off_t = self._parse_time(lighting["light_off"])
                    if on_t and off_t:
                        hours = (off_t - on_t).seconds / 3600
                        if hours < 0:
                            hours += 24
                        lighting["photoperiod_hours"] = round(hours, 1)
                except Exception:
                    pass
                dli_str = form.dli_manual.get().strip()
                if dli_str:
                    lighting["dli"] = float(dli_str)
                elif lighting.get("photoperiod_hours"):
                    lighting["dli"] = round(lighting["photoperiod_hours"] * 1000 * 0.0036, 1)

                nutrients = {"mix": []}
                for r in form.nutrient_rows:
                    name = r["name_var"].get().strip()
                    strength = r["strength_var"].get().strip()
                    if name:
                        n = {"name": name}
                        if strength:
                            n["strength_ml_per_l"] = float(strength)
                        nutrients["mix"].append(n)
                litres = form.nutrient_litres.get().strip()
                if litres:
                    nutrients["reservoir_litres"] = float(litres)
                ph = form.ph_entry.get().strip()
                if ph:
                    nutrients["ph"] = float(ph)
                ec = form.ec_entry.get().strip()
                if ec:
                    nutrients["ec"] = float(ec)

                note = form.notes.get("1.0", "end").strip()
                date_planted = form.date_planted.get().strip()
                date_popped = form.date_popped.get().strip()
                observation_date = form.date_observed.get().strip()

                images = []
                for p in form.image_paths:
                    with open(p, "rb") as f:
                        images.append(types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"))

                history = load_history(DEFAULT_HISTORY)
                hist_text = build_history_text(history)
                csv_metrics = form.csv_stats or {}

                if csv_metrics and observation_date:
                    prev_date = None
                    for e in reversed(history.get("analyses", [])):
                        if e.get("plant") != plant:
                            continue
                        p_obs = e.get("observation_date", "")
                        if p_obs and p_obs < observation_date:
                            try:
                                prev_date = datetime.strptime(p_obs, "%Y-%m-%d")
                            except ValueError:
                                pass
                            break
                    csv_path = form.csv_path
                    if csv_path and prev_date and os.path.isfile(csv_path):
                        try:
                            end_dt = datetime.strptime(observation_date, "%Y-%m-%d") + timedelta(days=1)
                            windowed = compute_windowed_csv_stats(csv_path, prev_date, end_dt)
                            if windowed:
                                csv_metrics = windowed
                        except Exception:
                            pass

                prompt_parts = [DWC_CONTEXT]
                prompt_parts.append(f"\nPlant: {plant}")
                if date_popped and date_popped != "YYYY-MM-DD":
                    try:
                        ref = datetime.strptime(observation_date, "%Y-%m-%d") if observation_date else datetime.now()
                        days = (ref - datetime.strptime(date_popped, "%Y-%m-%d")).days
                        prompt_parts.append(f"Age: {days} days ({days // 7} weeks) as of {observation_date}")
                    except ValueError:
                        pass
                if date_planted and date_planted != "YYYY-MM-DD":
                    prompt_parts.append(f"Planted: {date_planted}")
                if form.image_paths:
                    prompt_parts.append("\nAnalyze the plant health from the provided photos.")
                else:
                    prompt_parts.append("\nAnalyze the plant health based on the available sensor data and notes.")
                prompt_parts.append(SCHEMA_HINT)
                if hist_text:
                    prompt_parts.append(f"\nRecent history:\n{hist_text}")
                if csv_metrics:
                    prompt_parts.append(f"\nSensor data (since last entry):")
                    for col, vals in csv_metrics.items():
                        prompt_parts.append(f"  {col}: min={vals.get('min','')} avg={vals.get('avg','')} max={vals.get('max','')} trend={vals.get('trend','')}")
                if lighting:
                    prompt_parts.append(f"\nLighting: on={lighting.get('light_on','')} off={lighting.get('light_off','')} photoperiod={lighting.get('photoperiod_hours','')}h DLI={lighting.get('dli','')}")
                if nutrients.get("mix"):
                    prompt_parts.append(f"\nNutrients: {', '.join(n['name'] + (' ' + str(n.get('strength_ml_per_L','')) + 'ml/L' if n.get('strength_ml_per_L') else '') for n in nutrients['mix'])}")
                if nutrients.get("ph"):
                    prompt_parts.append(f"\npH: {nutrients['ph']}")
                if nutrients.get("ec"):
                    prompt_parts.append(f"EC: {nutrients['ec']}")
                if nutrients.get("reservoir_litres"):
                    prompt_parts.append(f"Reservoir: {nutrients['reservoir_litres']}L")
                if note:
                    prompt_parts.append(f"\nGrower note: {note}")

                chat = client.chats.create(model=model_name)
                response = chat.send_message(
                    [types.Part.from_text(text="\n".join(prompt_parts))] + images,
                    config=types.GenerateContentConfig(temperature=0.2),
                )
                raw_text = response.text
                try:
                    clean = raw_text.strip()
                    if clean.startswith("```"):
                        clean = clean.split("\n", 1)[1]
                    if clean.endswith("```"):
                        clean = clean[:-3]
                    clean = clean.strip()
                    parsed = json.loads(clean)
                    score = parsed.get("health_score", "?")
                    assessment = parsed.get("overall_assessment", "")
                    issues = parsed.get("issues", [])
                    recs = parsed.get("recommendations", [])
                    report_lines = [f"HEALTH SCORE: {score}/100", "", assessment]
                    if issues:
                        report_lines.append("\nIssues:")
                        for i in issues:
                            report_lines.append(f"  [{i.get('severity','?')}] {i.get('type','')}: {i.get('description','')}")
                    if recs:
                        report_lines.append("\nRecommendations:")
                        for r in recs:
                            report_lines.append(f"  - {r}")
                    report = "\n".join(report_lines)
                    analysis = parsed
                except Exception:
                    report = raw_text
                    analysis = {"raw_text": raw_text}

                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "observation_date": observation_date if observation_date else datetime.now().strftime("%Y-%m-%d"),
                    "type": "analysis",
                    "plant": plant,
                    "date_planted": date_planted if date_planted != "YYYY-MM-DD" else "",
                    "date_popped": date_popped if date_popped != "YYYY-MM-DD" else "",
                    "photo_paths": [os.path.basename(p) for p in form.image_paths],
                    "photo_full_paths": list(form.image_paths),
                    "photo_types": list(form.image_types),
                    "csv_path": form.csv_path,
                    "csv_metrics": {k: {kk: vv for kk, vv in v.items() if kk != "values"} for k, v in (csv_metrics or {}).items()},
                    "lighting": lighting,
                    "nutrients": nutrients,
                    "note": note,
                    "analysis": analysis,
                }
                history["analyses"].append(entry)
                save_history(DEFAULT_HISTORY, history)
                if form.plant_var.get().strip():
                    self._save_plant_names(form.plant_var.get().strip())

                self.root.after(0, lambda: set_results(report))
                self.root.after(0, lambda: self._on_analysis_done())
            except Exception as e:
                self.root.after(0, lambda: set_results(f"Error: {e}"))
                self.root.after(0, lambda: analyze_btn.config(state="normal", text="Analyze"))

        def _on_analysis_done():
            analyze_btn.config(state="normal", text="Analyze")
            self._refresh_sidebar_grows()

        self._on_analysis_done = _on_analysis_done

        analyze_btn = ttk.Button(form, text="Analyze", style="Big.TButton", command=do_analyze)
        analyze_btn.pack(pady=6)

    # ----------------------------------------------------------- Sidebar
    def _on_sidebar_grow_select(self, event):
        sel = self.sidebar_grow_list.curselection()
        if not sel:
            return
        idx = sel[0]
        grow_info = self._sidebar_grow_items.get(idx)
        if not grow_info:
            return
        self._active_grow = grow_info

        if self._current_view != "log":
            self._show_log_view()

        # Update the log view detail with this grow's info
        self._refresh_history()
        self._show_grow_detail(grow_info)

    def _on_sidebar_tl_select(self, event):
        sel = self.sidebar_tl_list.curselection()
        if not sel:
            return
        idx = sel[0]
        grow_info = self._sidebar_tl_items.get(idx)
        if not grow_info:
            return

        if self._current_view != "timelapse":
            self._show_timelapse_view()

        grow_name = grow_info
        safe_name = grow_name.replace("#", "").replace(" ", "_").strip()
        local_dir = os.path.expanduser(f"~/plant-monitor/data/photos/timelapse/{safe_name}")
        os.makedirs(local_dir, exist_ok=True)
        self.tl_folder_var.set(local_dir)
        self._tl_load_folder()

    def _show_grow_detail(self, grow_info):
        entry_indices = grow_info["entries"]
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])

        self.hist_entry_tree.delete(*self.hist_entry_tree.get_children())
        self._entry_tree_map = {}

        indexed_entries = []
        for idx in entry_indices:
            if idx >= len(entries):
                continue
            entry = entries[idx]
            obs = entry.get("observation_date", "")
            if obs:
                try:
                    obs_dt = datetime.strptime(obs, "%Y-%m-%d")
                except ValueError:
                    obs_dt = datetime.min
            else:
                ts = entry.get("timestamp", "")
                try:
                    obs_dt = datetime.fromisoformat(ts)
                except Exception:
                    obs_dt = datetime.min
            ts_str = entry.get("timestamp", "")
            try:
                ts_dt = datetime.fromisoformat(ts_str)
            except Exception:
                ts_dt = datetime.min
            indexed_entries.append((idx, entry, obs_dt, ts_dt))

        indexed_entries.sort(key=lambda x: (x[2], x[3]), reverse=True)

        for idx, entry, obs_dt, ts_dt in indexed_entries:
            obs = entry.get("observation_date", "")
            if obs:
                try:
                    date_str = datetime.strptime(obs, "%Y-%m-%d").strftime("%d-%m-%Y")
                except ValueError:
                    date_str = obs
            else:
                ts = entry.get("timestamp", "")
                try:
                    t = datetime.fromisoformat(ts)
                    date_str = t.strftime("%d-%m-%Y %H:%M")
                except Exception:
                    date_str = ts[:16]

            etype = entry.get("type", "analysis")
            analysis = entry.get("analysis", {})
            score = str(analysis.get("health_score", "")) if analysis else ""
            note = entry.get("note", "")
            assessment = analysis.get("overall_assessment", "") if analysis else ""
            question = entry.get("question", "")

            display_note = ""
            if etype == "chat":
                display_note = question[:60] if question else "(chat)"
            elif assessment:
                display_note = assessment[:60]
            elif note:
                display_note = note[:60]

            item_id2 = self.hist_entry_tree.insert("", "end", values=(
                date_str, etype[:3], score, display_note
            ))
            self._entry_tree_map[item_id2] = idx

        self.hist_detail.config(state="normal")
        self.hist_detail.delete("1.0", "end")
        gid = f"#{grow_info['grow_int']}" if grow_info['grow_int'] != 9999 else "?"
        header = f"Grow {gid}: {grow_info['plant_name']}\n{len(entry_indices)} entries\n"
        csv_files = []
        for idx in entry_indices:
            if idx >= len(entries):
                continue
            e = entries[idx]
            cp = e.get("csv_path")
            if cp and os.path.isfile(cp):
                csv_files.append(cp)
        if csv_files:
            all_vals = {}
            for cp in csv_files:
                try:
                    s, _, _ = parse_csv(cp)
                except Exception:
                    continue
                for col, v in s.items():
                    if col not in all_vals:
                        all_vals[col] = []
                    all_vals[col].append(v.get("avg"))
            if all_vals:
                header += "\n=== HISTORICAL AVERAGES ===\n"
                key_cols = ["temp", "temperature", "humidity", "vpd", "ph", "ec", "co2"]
                shown = set()
                for key in key_cols:
                    for col in all_vals:
                        if key in col.lower() and col not in shown:
                            avgs = [a for a in all_vals[col] if a is not None]
                            if avgs:
                                overall = round(sum(avgs) / len(avgs), 2)
                                header += f"  {col:<14} avg={overall:>7}  ({len(avgs)} entries)\n"
                            shown.add(col)
                            break
                for col in all_vals:
                    if col not in shown:
                        avgs = [a for a in all_vals[col] if a is not None]
                        if avgs:
                            overall = round(sum(avgs) / len(avgs), 2)
                            header += f"  {col:<14} avg={overall:>7}  ({len(avgs)} entries)\n"
        self.hist_detail.insert("1.0", header)
        self.hist_detail.config(state="disabled")

        self.hist_photo_canvas.delete("all")
        self.hist_photo_images.clear()

    def _refresh_sidebar_grows(self):
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        grows = {}
        for i, entry in enumerate(entries):
            raw = entry.get("plant", "")
            grow_num = ""
            plant_name = raw or "(no plant)"
            if raw and " #" in raw:
                parts = raw.rsplit(" #", 1)
                plant_name = parts[0]
                grow_num = parts[1]
            try:
                grow_int = int(grow_num)
            except (ValueError, TypeError):
                grow_int = 9999
            ts = entry.get("observation_date") or entry.get("timestamp", "")
            try:
                if entry.get("observation_date"):
                    t = datetime.strptime(ts, "%Y-%m-%d")
                else:
                    t = datetime.fromisoformat(ts)
            except Exception:
                t = datetime.min
            if grow_num not in grows:
                grows[grow_num] = {
                    "grow_int": grow_int,
                    "plant_name": plant_name,
                    "entries": [],
                    "last_date": t,
                    "last_date_str": t.strftime("%d-%m-%Y") if t != datetime.min else "",
                }
            grows[grow_num]["entries"].append(i)
            if t > grows[grow_num]["last_date"]:
                grows[grow_num]["last_date"] = t
                grows[grow_num]["last_date_str"] = t.strftime("%d-%m-%Y")

        sorted_grows = sorted(grows.values(), key=lambda g: g["grow_int"], reverse=True)
        self.sidebar_grow_list.delete(0, "end")
        self._sidebar_grow_items = {}
        self._grow_tree_map = {}
        for i, g in enumerate(sorted_grows):
            gid = f"#{g['grow_int']}" if g['grow_int'] != 9999 else "?"
            display = f" {gid}  {g['plant_name']}  ({len(g['entries'])})"
            self.sidebar_grow_list.insert("end", display)
            self._sidebar_grow_items[i] = g
            self._grow_tree_map[str(i)] = g

    def _refresh_sidebar_tl(self):
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        grows = {}
        for entry in entries:
            raw = entry.get("plant", "")
            if raw and " #" in raw:
                grows[raw] = True
            elif raw:
                grows[raw] = True
        names = sorted(grows.keys())
        self.sidebar_tl_list.delete(0, "end")
        self._sidebar_tl_items = {}
        for i, name in enumerate(names):
            self.sidebar_tl_list.insert("end", f" {name}")
            self._sidebar_tl_items[i] = name

    # ----------------------------------------------------------- Plant names
    def _parse_time(self, s):
        s = s.strip()
        for fmt in ("%H:%M", "%I:%M %p", "%H"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _load_plant_names(self):
        try:
            if os.path.isfile(self.plant_names_file):
                with open(self.plant_names_file) as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_plant_names(self, name):
        names = self._load_plant_names()
        if name and name not in names:
            names.insert(0, name)
            os.makedirs(os.path.dirname(self.plant_names_file), exist_ok=True)
            with open(self.plant_names_file, "w") as f:
                json.dump(names, f)

    # ----------------------------------------------------------- Grow/Entry dialogs
    def _new_grow_dialog(self):
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        max_grow = 0
        for e in entries:
            raw = e.get("plant", "")
            if raw and " #" in raw:
                try:
                    g = int(raw.rsplit(" #", 1)[1])
                    if g > max_grow:
                        max_grow = g
                except ValueError:
                    pass
        self._show_entry_form(grow_num=str(max_grow + 1), plant_name="")

    # ----------------------------------------------------------- Live View
    def _open_live_view(self, addr=None):
        if addr is None:
            addr = "raspberrypi.local"
        port = "5555"
        if not addr:
            messagebox.showwarning("Missing", "Enter the Pi's address.")
            return
        win = tk.Toplevel(self.root)
        win.title("Live View")
        win.geometry("680x560")
        win.configure(bg=BG)
        canvas = tk.Canvas(win, bg=BG, highlightthickness=0)
        canvas.pack(expand=True, fill="both")
        status = ttk.Label(win, text="Starting server on Pi...", style="Sub.TLabel")
        status.pack(side="bottom", fill="x", padx=10, pady=6)
        url = f"http://{addr}:{port}/frame"
        running = [True]

        def on_close():
            running[0] = False
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

        def start_server():
            host = addr
            user = "hawkewing"
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     f"{user}@{host}",
                     "pgrep -f live_server.py"],
                    capture_output=True, timeout=10,
                )
                if result.returncode == 0:
                    return
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     f"{user}@{host}",
                     "cd ~/plant-monitor && source ~/webcam-env/bin/activate && nohup python3 scripts/live_server.py > /dev/null 2>&1 & disown"],
                    capture_output=True, timeout=10,
                )
                time.sleep(4)
            except Exception:
                pass

        def poll():
            start_server()
            while running[0]:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "GrowGUI"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = resp.read()
                    img = Image.open(io.BytesIO(data))
                    img = img.resize((640, 480), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    canvas.delete("all")
                    canvas.create_image(320, 240, image=photo, anchor="center")
                    canvas.image = photo
                    try:
                        status.config(text=f"Connected -- {addr}:{port}", foreground=GREEN)
                    except Exception:
                        return
                except Exception as e:
                    try:
                        status.config(text=f"Waiting for camera... ({e})", foreground="#e0a030")
                    except Exception:
                        return
                for _ in range(10):
                    if not running[0]:
                        return
                    time.sleep(0.1)
        threading.Thread(target=poll, daemon=True).start()

    # ----------------------------------------------------------- History
    def _refresh_history(self):
        old_grow = self._active_grow
        self._refresh_sidebar_grows()

        # Re-lookup active grow from fresh sidebar data
        if old_grow:
            key = (old_grow.get("plant_name", ""), old_grow.get("grow_int", 9999))
            for g in self._sidebar_grow_items.values():
                if (g.get("plant_name", ""), g.get("grow_int", 9999)) == key:
                    self._active_grow = g
                    break

        # If a grow is active, re-show its detail
        if self._active_grow and self._current_view == "log" and hasattr(self, "hist_entry_tree"):
            self._show_grow_detail(self._active_grow)
        elif self._current_view == "log" and hasattr(self, "hist_detail"):
            self.hist_detail.config(state="normal")
            self.hist_detail.delete("1.0", "end")
            self.hist_detail.insert("1.0", "Select a grow from the sidebar to view entries.")
            self.hist_detail.config(state="disabled")
            if hasattr(self, "hist_entry_tree"):
                self.hist_entry_tree.delete(*self.hist_entry_tree.get_children())

    def _format_detail(self, entry, prev_date=None):
        lines = []
        obs = entry.get("observation_date", "")
        if obs:
            lines.append(f"Observed: {obs}")
        ts = entry.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            lines.append(f"Entered:  {dt.strftime('%Y-%m-%d %H:%M')}")
        except Exception:
            lines.append(f"Entered:  {ts}")
        plant = entry.get("plant", "")
        if plant:
            lines.append(f"Plant:    {plant}")
        popped = entry.get("date_popped")
        if popped:
            try:
                ref_date = datetime.strptime(obs, "%Y-%m-%d") if obs else datetime.now()
                days = (ref_date - datetime.strptime(popped, "%Y-%m-%d")).days
                lines.append(f"Age:      {days} days ({days // 7} weeks)")
            except Exception:
                lines.append(f"Popped:   {popped}")

        csv_path = entry.get("csv_path")
        metrics = None
        if csv_path and os.path.isfile(csv_path) and prev_date is not None:
            try:
                end_dt = datetime.strptime(obs, "%Y-%m-%d") + timedelta(days=1) if obs else None
                metrics = compute_windowed_csv_stats(csv_path, prev_date, end_dt)
            except Exception:
                pass
        if not metrics:
            metrics = entry.get("csv_metrics")
        if metrics:
            lines.append("")
            if prev_date is not None:
                prev_str = prev_date.strftime("%d-%m-%Y") if isinstance(prev_date, datetime) else str(prev_date)
                end_str = obs if obs else "now"
                lines.append(f"=== SENSOR DATA ({prev_str} → {end_str}) ===")
            else:
                lines.append("=== SENSOR DATA ===")
            trend_icons = {"rising": "↑", "falling": "↓", "stable": "—"}
            range_labels = {"ok": "OK", "low": "LOW", "high": "HIGH"}
            key_cols = ["temp", "temperature", "humidity", "vpd", "ph", "ec", "co2"]
            shown = set()
            for key in key_cols:
                for col, vals in metrics.items():
                    if key in col.lower() and col not in shown:
                        trend = trend_icons.get(vals.get("trend", "stable"), "—")
                        status = range_labels.get(vals.get("range_status", "ok"), "")
                        opt = vals.get("optimal")
                        opt_str = ""
                        if opt:
                            opt_str = f"  (optimal {opt['min']}-{opt['max']}{opt['unit']})"
                        lines.append(
                            f"  {col:<14} {vals.get('min',''):>7}  —  {vals.get('avg',''):>7}  —  {vals.get('max',''):>7}  {trend} {status}{opt_str}"
                        )
                        shown.add(col)
                        break
            for col, vals in metrics.items():
                if col not in shown:
                    trend = trend_icons.get(vals.get("trend", "stable"), "—")
                    status = range_labels.get(vals.get("range_status", "ok"), "")
                    lines.append(
                        f"  {col:<14} {vals.get('min',''):>7}  —  {vals.get('avg',''):>7}  —  {vals.get('max',''):>7}  {trend} {status}"
                    )
            lines.append("")

        analysis = entry.get("analysis", {})
        if analysis and entry.get("type") != "chat":
            if "raw_text" in analysis and "health_score" not in analysis:
                try:
                    clean = analysis["raw_text"].strip()
                    if clean.startswith("```"):
                        clean = clean.split("\n", 1)[1]
                    if clean.endswith("```"):
                        clean = clean[:-3]
                    analysis = json.loads(clean.strip())
                except Exception:
                    pass
            score = analysis.get("health_score", "?")
            lines.append(f"HEALTH SCORE: {score}/100")
            assessment = analysis.get("overall_assessment", "")
            if assessment:
                lines.append(f"\n{assessment}")
            issues = analysis.get("issues", [])
            if issues:
                lines.append("\nIssues:")
                for i in issues:
                    lines.append(f"  [{i.get('severity','?')}] {i.get('type','')}: {i.get('description','')}")
            recs = analysis.get("recommendations", [])
            if recs:
                lines.append("\nRecommendations:")
                for r in recs:
                    lines.append(f"  - {r}")

        note = entry.get("note", "")
        if note:
            lines.append(f"\nNote: {note}")

        lighting = entry.get("lighting", {})
        if lighting:
            lparts = []
            if lighting.get("light_on") and lighting.get("light_off"):
                lparts.append(f"{lighting['light_on']}-{lighting['light_off']}")
            if lighting.get("photoperiod_hours"):
                lparts.append(f"{lighting['photoperiod_hours']}h photoperiod")
            if lighting.get("dli"):
                lparts.append(f"DLI {lighting['dli']}")
            if lparts:
                lines.append(f"\nLighting: {', '.join(lparts)}")

        nutrients = entry.get("nutrients", {})
        if nutrients:
            nparts = []
            for n in nutrients.get("mix", []):
                s = n.get("strength_ml_per_l")
                nparts.append(n["name"] + (f" {s}ml/L" if s else ""))
            if nutrients.get("reservoir_litres"):
                nparts.append(f"Reservoir {nutrients['reservoir_litres']}L")
            if nutrients.get("ph"):
                nparts.append(f"pH {nutrients['ph']}")
            if nutrients.get("ec"):
                nparts.append(f"EC {nutrients['ec']}")
            if nparts:
                lines.append(f"\nNutrients: {', '.join(nparts)}")

        photos = entry.get("photo_paths", [])
        if photos:
            lines.append(f"\nPhotos: {len(photos)}")

        if entry.get("type") == "chat":
            lines.append(f"\n> {entry.get('question', '')}")
            lines.append(f"{entry.get('answer', '')}")

        return "\n".join(lines)

    def _on_entry_select(self, event):
        sel = self.hist_entry_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        if item_id not in self._entry_tree_map:
            return
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        real_idx = self._entry_tree_map[item_id]
        if real_idx >= len(entries):
            return
        entry = entries[real_idx]

        prev_date = None
        if self._active_grow:
            indexed = []
            for idx in self._active_grow["entries"]:
                if idx >= len(entries):
                    continue
                e = entries[idx]
                obs = e.get("observation_date", "")
                ts = e.get("timestamp", "")
                try:
                    t = datetime.strptime(obs, "%Y-%m-%d") if obs else datetime.fromisoformat(ts)
                except Exception:
                    t = datetime.min
                indexed.append((idx, e, t))
            indexed.sort(key=lambda x: x[2])
            for i, (idx, e, t) in enumerate(indexed):
                if idx == real_idx and i > 0:
                    prev_date = indexed[i - 1][2]
                    break

        self.hist_detail.config(state="normal")
        self.hist_detail.delete("1.0", "end")
        self.hist_detail.insert("1.0", self._format_detail(entry, prev_date=prev_date))
        self.hist_detail.config(state="disabled")
        self._show_history_photos(entry)

    def _show_history_photos(self, entry):
        self.hist_photo_canvas.delete("all")
        self.hist_photo_images.clear()

        full_paths = entry.get("photo_full_paths", [])
        basenames = entry.get("photo_paths", [])

        paths = []
        for p in full_paths:
            if p and os.path.isfile(p):
                paths.append(p)
        if not paths:
            for b in basenames:
                if not b:
                    continue
                if os.path.isfile(b):
                    paths.append(b)
                else:
                    for search_dir in [os.path.expanduser("~/Pictures"), os.path.expanduser("~/plant-monitor/data/photos"), os.path.expanduser("~/plant-monitor/data/photos/timelapse")]:
                        candidate = os.path.join(search_dir, b)
                        if os.path.isfile(candidate):
                            paths.append(candidate)
                            break

        if not paths:
            for b in basenames:
                if b:
                    self.hist_photo_canvas.create_text(150, 10, text=f"[{b}]", fill=FG_DIM, font=("Segoe UI", 8), anchor="nw")
            return

        self.root.after(50, lambda: self._render_history_photos(paths))

    def _render_history_photos(self, paths):
        self.hist_photo_canvas.delete("all")
        self.hist_photo_images.clear()
        canvas_w = self.hist_photo_canvas.winfo_width()
        if canvas_w < 50:
            canvas_w = 400
        thumb_h = 400
        padding = 6
        x = padding
        y = padding
        max_w = canvas_w - padding * 2
        for p in paths:
            try:
                img = Image.open(p)
                ratio = min(thumb_h / img.height, 1)
                new_w = int(img.width * ratio)
                new_h = int(img.height * ratio)
                if x + new_w + padding > canvas_w:
                    x = padding
                    y += thumb_h + padding
                img = img.resize((new_w, new_h), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.hist_photo_images.append(photo)
                item = self.hist_photo_canvas.create_image(x + new_w // 2, y + new_h // 2, image=photo, anchor="center")
                self.hist_photo_canvas.tag_bind(item, "<Button-1>", lambda e, p=p: self._open_photo_viewer(p))
                self.hist_photo_canvas.tag_bind(item, "<Button-3>", lambda e, p=p: self._rotate_entry_photo(p))
                x += new_w + padding
            except Exception:
                name = os.path.basename(p) if p else "?"
                if x + 100 > canvas_w:
                    x = padding
                    y += thumb_h + padding
                self.hist_photo_canvas.create_text(x, y, text=f"[{name}]", fill=FG_DIM, font=("Segoe UI", 8), anchor="nw")
                x += 100
        self.hist_photo_canvas.configure(scrollregion=self.hist_photo_canvas.bbox("all"))

    def _rotate_entry_photo(self, path):
        try:
            img = Image.open(path)
            img = img.rotate(-90, expand=True)
            img.save(path)
            sel = self.hist_entry_tree.selection()
            if sel and sel[0] in self._entry_tree_map:
                history = load_history(DEFAULT_HISTORY)
                entries = history.get("analyses", [])
                real_idx = self._entry_tree_map[sel[0]]
                if real_idx < len(entries):
                    self._show_history_photos(entries[real_idx])
        except Exception as e:
            messagebox.showerror("Rotate Failed", str(e))

    def _open_photo_viewer(self, path):
        if not os.path.isfile(path):
            return
        win = tk.Toplevel(self.root)
        win.title(os.path.basename(path))
        win.configure(bg=BG)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        ww, wh = min(900, sw - 100), min(700, sh - 100)
        win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2}")

        img = Image.open(path)
        state = {"img": img, "zoom": 1.0}

        canvas = tk.Canvas(win, bg="#1a1a1a", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        photo_ref = [None]

        def render():
            w = max(1, int(img.width * state["zoom"]))
            h = max(1, int(img.height * state["zoom"]))
            resized = img.resize((w, h), Image.LANCZOS) if state["zoom"] != 1.0 else img.copy()
            photo = ImageTk.PhotoImage(resized)
            photo_ref[0] = photo
            canvas.delete("all")
            cw = canvas.winfo_width() or ww
            ch = canvas.winfo_height() or wh
            cx = max(0, (cw - w) // 2)
            cy = max(0, (ch - h) // 2)
            canvas.create_image(cx, cy, image=photo, anchor="nw")

        def zoom_in(e=None):
            state["zoom"] = min(state["zoom"] * 1.25, 10.0)
            render()

        def zoom_out(e=None):
            state["zoom"] = max(state["zoom"] / 1.25, 0.1)
            render()

        def zoom_reset(e=None):
            state["zoom"] = 1.0
            render()

        win.bind("+", zoom_in)
        win.bind("=", zoom_in)
        win.bind("-", zoom_out)
        win.bind("0", zoom_reset)
        win.bind("<MouseWheel>", lambda e: zoom_in() if e.delta > 0 else zoom_out())
        win.bind("<Button-4>", zoom_in)
        win.bind("<Button-5>", zoom_out)

        btns = ttk.Frame(win)
        btns.pack(side="bottom", fill="x")
        ttk.Button(btns, text="+", command=zoom_in, width=3).pack(side="left", padx=2, pady=4)
        ttk.Button(btns, text="-", command=zoom_out, width=3).pack(side="left", padx=2, pady=4)
        ttk.Button(btns, text="1:1", command=zoom_reset, width=5).pack(side="left", padx=2, pady=4)
        ttk.Label(btns, text="Scroll or +/- to zoom", style="Sub.TLabel").pack(side="left", padx=8)

        win.after(50, render)

    def _delete_history_entry(self):
        sel = self.hist_entry_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        if item_id not in self._entry_tree_map:
            return
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        real_idx = self._entry_tree_map[item_id]
        if real_idx >= len(entries):
            return
        entry = entries[real_idx]
        ts = entry.get("timestamp", "this entry")
        plant = entry.get("plant", "")
        if not messagebox.askyesno("Delete", f"Delete entry for '{plant}' at {ts}?"):
            return
        entries.pop(real_idx)
        save_history(DEFAULT_HISTORY, history)
        self._refresh_history()

    def _delete_grow(self):
        if not self._active_grow:
            return
        grow = self._active_grow
        gid = f"#{grow['grow_int']}" if grow["grow_int"] != 9999 else "?"
        plant = grow["plant_name"]
        n = len(grow["entries"])
        if not messagebox.askyesno("Delete Grow", f"Delete ALL {n} entries for '{plant} #{gid}'?\nThis cannot be undone."):
            return
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        for idx in sorted(grow["entries"], reverse=True):
            if idx < len(entries):
                entries.pop(idx)
        save_history(DEFAULT_HISTORY, history)
        self._active_grow = None
        self._refresh_history()

    def _edit_history_entry(self):
        if not hasattr(self, "hist_entry_tree"):
            return
        sel = self.hist_entry_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        if item_id not in self._entry_tree_map:
            return
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        real_idx = self._entry_tree_map[item_id]
        if real_idx >= len(entries):
            return
        entry = entries[real_idx]
        win = tk.Toplevel(self.root)
        win.title(f"Edit -- {entry.get('plant', '')}")
        win.geometry("650x550")
        win.configure(bg=BG)
        ttk.Label(win, text="Edit JSON:", style="Header.TLabel").pack(anchor="w", padx=10, pady=(10, 0))
        text = tk.Text(win, bg=BG_INPUT, fg=FG, insertbackground=FG, font=("Consolas", 9), relief="flat", padx=6, pady=4)
        text.pack(fill="both", expand=True, padx=10, pady=6)
        text.insert("1.0", json.dumps(entry, indent=2, default=str))

        def save():
            try:
                edited = json.loads(text.get("1.0", "end"))
                entries[real_idx] = edited
                save_history(DEFAULT_HISTORY, history)
                self._refresh_history()
                win.destroy()
            except json.JSONDecodeError as e:
                messagebox.showerror("JSON Error", str(e))
        ttk.Button(win, text="Save", style="Accent.TButton", command=save).pack(pady=8)

    # ----------------------------------------------------------- Timelapse
    def _tl_browse_folder(self):
        folder = filedialog.askdirectory(title="Select timelapse folder")
        if folder:
            self.tl_folder_var.set(folder)
            self._tl_load_folder()

    def _tl_refresh_grows(self):
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        grows = {}
        for entry in entries:
            raw = entry.get("plant", "")
            if raw and " #" in raw:
                grows[raw] = True
            elif raw:
                grows[raw] = True
        names = sorted(grows.keys())
        if hasattr(self, "tl_grow_combo"):
            self.tl_grow_combo["values"] = names
            if names and not self.tl_grow_var.get():
                self.tl_grow_var.set(names[0])

    def _tl_sync_from_pi(self):
        grow_name = ""
        if hasattr(self, "tl_folder_var"):
            folder = self.tl_folder_var.get().strip()
            if folder:
                grow_name = os.path.basename(folder)
        if not grow_name:
            messagebox.showwarning("Missing", "Select a timelapse grow first.")
            return
        local_dir = os.path.expanduser(f"~/plant-monitor/data/photos/timelapse/{grow_name}")
        pi_dir = "~/plant-monitor/data/photos/timelapse"
        os.makedirs(local_dir, exist_ok=True)
        addr = "raspberrypi.local"
        user = "hawkewing"
        self.tl_folder_var.set(local_dir)

        def set_status(msg, color=FG_DIM):
            self.root.after(0, lambda: self.tl_status.config(text=msg, fg=color) if self.root.winfo_exists() else None)

        def do_sync():
            try:
                set_status("Connecting to Pi...", "#e0a030")
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     f"{user}@{addr}", f"find {pi_dir} -name '*.jpg' -type f 2>/dev/null"],
                    capture_output=True, text=True, timeout=10,
                )
                files = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
                if not files:
                    set_status("No photos on Pi", "#e0a030")
                    return
                set_status(f"Syncing {len(files)} files...", "#e0a030")
                subprocess.run(
                    ["rsync", "-az", "--timeout=10", "--relative",
                     f"{user}@{addr}:{pi_dir}/", local_dir + "/"],
                    timeout=120,
                )
                set_status("Cleaning up Pi...", "#e0a030")
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     f"{user}@{addr}", f"rm -f {pi_dir}/*.jpg"],
                    capture_output=True, timeout=10,
                )
                self.root.after(0, self._tl_load_folder)
                set_status(f"Synced {len(files)} files", GREEN)
            except Exception as e:
                set_status(f"Sync failed: {e}", "#e04040")

        threading.Thread(target=do_sync, daemon=True).start()

    def _get_photo_date(self, fpath):
        fname = os.path.basename(fpath)

        if len(fname) >= 10 and fname[4] == "-" and fname[7] == "-":
            return fname[:10]

        if fname.startswith("tl-") and len(fname) >= 12:
            try:
                return datetime.strptime(fname[3:11], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pass

        if len(fname) >= 12 and fname[3] == "_" and fname[11] == "_":
            try:
                return datetime.strptime(fname[4:12], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pass

        try:
            img = Image.open(fpath)
            exif = img.getexif()
            for tag_id in (36867, 36868, 306):
                if tag_id in exif:
                    raw = exif[tag_id]
                    return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d")
        except Exception:
            pass

        try:
            dt = datetime.fromtimestamp(os.path.getmtime(fpath))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

        return "Unknown"

    def _tl_load_folder(self):
        folder = self.tl_folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            return
        self._tl_thumb_cache.clear()
        self._tl_dates.clear()
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            fpath = os.path.join(folder, fname)
            date_str = ""
            if len(fname) >= 10 and fname[4] == "-" and fname[7] == "-":
                date_str = fname[:10]
            elif fname.startswith("tl-") and len(fname) >= 11:
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
            if date_str not in self._tl_dates:
                self._tl_dates[date_str] = []
            self._tl_dates[date_str].append(fpath)
        self._refresh_timelapse_dates()

    def _refresh_timelapse_dates(self):
        if hasattr(self, "tl_date_list"):
            self.tl_date_list.delete(0, "end")
            for date_str in sorted(self._tl_dates.keys(), reverse=True):
                count = len(self._tl_dates[date_str])
                self.tl_date_list.insert("end", f"{date_str}  ({count})")

    def _tl_on_date_select(self, event):
        sel = self.tl_date_list.curselection()
        if not sel:
            return
        all_paths = []
        for s in sel:
            text = self.tl_date_list.get(s)
            date_str = text.split("  ")[0]
            all_paths.extend(self._tl_dates.get(date_str, []))
        self._tl_show_photos(all_paths)

    def _tl_show_photos(self, paths):
        for item in self._tl_photo_items:
            item.destroy()
        self._tl_photo_items.clear()
        self._tl_photos.clear()
        self._tl_pending_paths = list(paths)
        self._tl_loaded_count = 0
        self._tl_load_batch()
        self.tl_canvas.yview_moveto(0)

    def _tl_load_batch(self):
        if self._tl_loaded_count >= len(self._tl_pending_paths):
            return
        end = min(self._tl_loaded_count + self._tl_batch_size, len(self._tl_pending_paths))
        batch = self._tl_pending_paths[self._tl_loaded_count:end]
        missing = [p for p in batch if p not in self._tl_thumb_cache]
        if missing:
            threading.Thread(target=self._tl_gen_thumbs, args=(missing,), daemon=True).start()
        cols = 2
        for i, path in enumerate(batch, start=self._tl_loaded_count):
            if path in self._tl_thumb_cache and self._tl_thumb_cache[path] is not None:
                self._tl_add_widget(path, i, cols)
            else:
                self._tl_add_placeholder(path)
        self._tl_loaded_count = end
        self.tl_grid_frame.update_idletasks()
        self.tl_canvas.configure(scrollregion=self.tl_canvas.bbox("all"))

    def _tl_gen_thumbs(self, paths):
        from PIL import Image as PILImage
        for p in paths:
            try:
                img = PILImage.open(p)
                img.thumbnail((400, 300), PILImage.LANCZOS)
                self._tl_thumb_cache[p] = img.copy()
            except Exception:
                self._tl_thumb_cache[p] = None
        self.root.after(0, self._tl_refresh_visible)

    def _tl_refresh_visible(self):
        for widget in self._tl_photo_items:
            for child in widget.winfo_children():
                path = getattr(child, "_tl_path", None)
                if not path or path not in self._tl_thumb_cache:
                    continue
                cached = self._tl_thumb_cache[path]
                if cached is None:
                    child.config(text="Error", image="")
                    continue
                thumb = ImageTk.PhotoImage(cached)
                child._tl_thumb_ref = thumb
                child.config(image=thumb, text="")

    def _tl_add_placeholder(self, path):
        cols = 3
        i = self._tl_photo_items.__len__()
        self._tl_add_widget(path, i, cols, placeholder=True)

    def _tl_add_widget(self, path, i, cols, placeholder=False):
        row, col = divmod(i, cols)
        excluded = path in self._tl_excluded
        selected = path in self._tl_selected
        if excluded:
            bg = "#3d1f1f"
        elif selected:
            bg = "#1f3a5f"
        else:
            bg = BG_CARD
        frame = tk.Frame(self.tl_grid_frame, bg=bg, bd=1, relief="solid")
        frame.grid(row=row, column=col, padx=3, pady=3)

        if placeholder or path not in self._tl_thumb_cache:
            lbl = tk.Label(frame, text="Loading...", bg=bg, fg=FG_DIM, width=30, height=12)
            lbl.pack()
            lbl._tl_path = path
        else:
            cached = self._tl_thumb_cache[path]
            if cached:
                photo = ImageTk.PhotoImage(cached)
                lbl = tk.Label(frame, image=photo, bg=bg, cursor="hand2")
                lbl._tl_thumb_ref = photo
            else:
                lbl = tk.Label(frame, text="Error", bg=bg, fg=FG_DIM)
            lbl.pack()
            lbl._tl_path = path
        lbl.bind("<Button-1>", lambda e, p=path, w=lbl: self._tl_toggle_select(p, w))
        lbl.bind("<Button-3>", lambda e, p=path: self._tl_rotate_photo(p))
        ts = os.path.basename(path)
        if len(ts) > 20:
            ts = ts[:17] + "..."
        info_row = tk.Frame(frame, bg=bg)
        info_row.pack(fill="x")
        lbl2 = tk.Label(info_row, text=ts, bg=bg, fg=FG_DIM, font=("Segoe UI", 7), wraplength=500)
        lbl2.pack(side="left", fill="x", expand=True)
        del_btn = tk.Label(info_row, text="\u2715", bg=bg, fg="#e04040", font=("Segoe UI", 8, "bold"), cursor="hand2")
        del_btn.pack(side="right", padx=2)
        del_btn.bind("<Button-1>", lambda e, p=path: self._tl_delete_photo(p))
        if excluded:
            lbl2.config(text="EXCLUDED")
        self._tl_photo_items.append(frame)

    def _tl_toggle_select(self, path, widget):
        if path in self._tl_excluded:
            return
        if path in self._tl_selected:
            self._tl_selected.discard(path)
            widget.master.config(bg=BG_CARD)
        else:
            self._tl_selected.add(path)
            widget.master.config(bg="#1f3a5f")

    def _tl_on_scroll(self, event):
        if self._tl_loaded_count >= len(self._tl_pending_paths):
            return
        bottom = self.tl_canvas.bbox("all")
        if not bottom:
            return
        view_top, view_bot = self.tl_canvas.yview()
        if view_bot > 0.85:
            self._tl_load_batch()

    def _tl_rotate_photo(self, path):
        try:
            self._tl_thumb_cache.pop(path, None)
            img = Image.open(path)
            img = img.rotate(-90, expand=True)
            img.save(path)
            sel = self.tl_date_list.curselection()
            if not sel:
                return
            all_paths = []
            for s in sel:
                text = self.tl_date_list.get(s)
                date_str = text.split("  ")[0]
                all_paths.extend(self._tl_dates.get(date_str, []))
            self._tl_show_photos(all_paths)
        except Exception as e:
            messagebox.showerror("Rotate Failed", str(e))

    def _tl_delete_photo(self, path):
        if not os.path.isfile(path):
            return
        try:
            os.remove(path)
            self._tl_thumb_cache.pop(path, None)
            sel = self.tl_date_list.curselection()
            if sel:
                all_paths = []
                for s in sel:
                    text = self.tl_date_list.get(s)
                    date_str = text.split("  ")[0]
                    all_paths.extend(self._tl_dates.get(date_str, []))
                self._tl_show_photos(all_paths)
        except Exception as e:
            messagebox.showerror("Delete Failed", str(e))

    def _tl_play(self):
        sel = self.tl_date_list.curselection()
        if sel:
            all_paths = []
            for s in sel:
                text = self.tl_date_list.get(s)
                date_str = text.split("  ")[0]
                all_paths.extend(self._tl_dates.get(date_str, []))
        else:
            all_paths = []
            for paths in self._tl_dates.values():
                all_paths.extend(paths)
        self.tl_slideshow_paths = [p for p in all_paths if p not in self._tl_excluded]
        if not self.tl_slideshow_paths:
            return
        self.tl_slideshow_idx = 0
        try:
            speed = int(self.tl_speed.get())
        except ValueError:
            speed = 500

        win = tk.Toplevel(self.root)
        win.title("Timelapse Playback")
        win.configure(bg=BG)
        size_str = self.tl_size_var.get()
        if size_str == "Fullscreen":
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            win.geometry(f"{sw}x{sh}+0+0")
            win.update_idletasks()
            win.attributes("-fullscreen", True)
        else:
            w, h = size_str.split("x")
            sw, sh = int(w), int(h)
            x = (self.root.winfo_screenwidth() - sw) // 2
            y = (self.root.winfo_screenheight() - sh) // 2
            win.geometry(f"{sw}x{sh}+{x}+{y}")
        win.resizable(True, True)

        running = [True]
        photo_ref = [None]
        idx = [0]
        after_id = [None]

        def on_close():
            running[0] = False
            if after_id[0]:
                try:
                    win.after_cancel(after_id[0])
                except Exception:
                    pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.bind("<Escape>", lambda e: on_close())

        canvas = tk.Canvas(win, bg=BG, highlightthickness=0)
        info_lbl = tk.Label(win, text="", bg=BG_CARD, fg=FG, font=("Consolas", 10))
        info_lbl.pack(side="bottom", fill="x", padx=4, pady=4)
        canvas.pack(fill="both", expand=True)
        close_btn = tk.Button(win, text="X  Close", command=on_close, bg="#3d1f1f", fg="white", font=("Consolas", 10, "bold"), bd=0, padx=12, pady=4, cursor="hand2")
        close_btn.place(x=10, y=10)

        paused = [False]
        def toggle_pause():
            paused[0] = not paused[0]
            pause_btn.config(text="Resume" if paused[0] else "Pause")
        pause_btn = tk.Button(win, text="Pause", command=toggle_pause, bg=BG_CARD, fg="white", font=("Consolas", 10), bd=0, padx=12, pady=4, cursor="hand2")
        pause_btn.place(x=110, y=10)

        win.bind("<space>", lambda e: toggle_pause())

        def tick():
            if not running[0]:
                return
            if paused[0]:
                after_id[0] = win.after(speed, tick)
                return
            if idx[0] >= len(self.tl_slideshow_paths):
                idx[0] = 0
            path = self.tl_slideshow_paths[idx[0]]
            try:
                cw = canvas.winfo_width()
                ch = canvas.winfo_height()
                if cw < 100:
                    cw, ch = sw, sh - 40
                img = Image.open(path)
                iw, ih = img.size
                scale = min(cw / iw, ch / ih)
                new_w = int(iw * scale)
                new_h = int(ih * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                photo_ref[0] = photo
                canvas.delete("all")
                canvas.create_image(cw // 2, ch // 2, image=photo, anchor="center")
                fname = os.path.basename(path)
                info_lbl.config(text=f"{idx[0] + 1}/{len(self.tl_slideshow_paths)}  {fname}")
            except Exception as e:
                info_lbl.config(text=f"Error: {e}")
            idx[0] += 1
            after_id[0] = win.after(speed, tick)

        tick()

    def _tl_select_all(self):
        self._tl_selected.clear()
        for paths in self._tl_dates.values():
            for p in paths:
                if p not in self._tl_excluded:
                    self._tl_selected.add(p)

    def _tl_load_from_history(self):
        history = load_history(DEFAULT_HISTORY)
        entries = history.get("analyses", [])
        entries_with_photos = [
            (i, e) for i, e in enumerate(entries)
            if e.get("photo_full_paths") or e.get("photo_paths")
        ]
        if not entries_with_photos:
            messagebox.showinfo("Info", "No entries with photos found in history.")
            return

        win = tk.Toplevel(self.root)
        win.title("Select Photos from History")
        win.geometry("700x500")
        win.configure(bg=BG)

        ttk.Label(win, text="Select entries to load photos from:", style="Header.TLabel").pack(anchor="w", padx=10, pady=(10, 0))

        list_frame = tk.Frame(win, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        listbox = tk.Listbox(
            list_frame, bg=BG_INPUT, fg=FG, selectbackground="#1f3a5f",
            selectforeground="white", font=("Consolas", 9), relief="flat",
            activestyle="none", selectmode="extended", yscrollcommand=scrollbar.set,
        )
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        entry_map = []
        for i, e in entries_with_photos:
            ts = e.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_str = ts[:16]
            plant = e.get("plant", "")
            n_photos = len(e.get("photo_full_paths") or e.get("photo_paths") or [])
            listbox.insert("end", f"{date_str}  {plant}  ({n_photos} photos)")
            entry_map.append(i)

        def load_selected():
            sel_indices = listbox.curselection()
            if not sel_indices:
                return
            all_photos = []
            for idx in sel_indices:
                real_idx = entry_map[idx]
                entry = entries[real_idx]
                full_paths = entry.get("photo_full_paths", [])
                basenames = entry.get("photo_paths", [])
                for p in full_paths:
                    if p and os.path.isfile(p):
                        all_photos.append(p)
                if not all_photos:
                    for b in basenames:
                        if b and os.path.isfile(b):
                            all_photos.append(b)
            if all_photos:
                self._tl_dates["From History"] = all_photos
                self.tl_folder_var.set("(history)")
                self._refresh_timelapse_dates()
                self._tl_show_photos(all_photos)
            win.destroy()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_frame, text="Load Selected", style="Accent.TButton", command=load_selected).pack(side="right")
        ttk.Button(btn_frame, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 6))

    # ----------------------------------------------------------- Run
    def run(self):
        self.root.mainloop()

    def _check_pi_connection(self):
        def check():
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     "hawkewing@raspberrypi.local", "echo ok"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    self.root.after(0, lambda: self.tl_status.config(text="Pi connected", fg=GREEN) if self.root.winfo_exists() else None)
                else:
                    self.root.after(0, lambda: self.tl_status.config(text="Pi unreachable", fg="#e04040") if self.root.winfo_exists() else None)
            except Exception:
                self.root.after(0, lambda: self.tl_status.config(text="Pi offline", fg="#e04040") if self.root.winfo_exists() else None)
        threading.Thread(target=check, daemon=True).start()


if __name__ == "__main__":
    GrowGUI().run()
