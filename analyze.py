"""Analyze plant health from photos, sensor CSV, and notes.

Runs on your PC. Calls Gemini directly — no cloud storage needed.
Supports multiple images (plant, pH meter, EC meter, etc.) and DWC context.
Maintains a full conversation history so Gemini tracks your grow over time.

Requirements:
    pip install google-genai Pillow

Environment:
    GEMINI_API_KEY  get yours free at https://aistudio.google.com

Usage:
    python3 analyze.py --image plant.jpg ph.jpg
    python3 analyze.py --image plant.jpg ph.jpg --csv history.csv
    python3 analyze.py --image plant.jpg ph.jpg --note "Week 6, pH runoff 5.8"
    python3 analyze.py --chat "Is the leaf curling from overwatering or heat?"
    python3 analyze.py --image plant.jpg --chat "What's causing the yellow tips?"
"""

import argparse
import base64
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

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
        return stats

    import io
    data_text = "".join(all_lines[header_idx:])
    reader = csv.DictReader(io.StringIO(data_text))
    rows = [row for row in reader if not all(v.strip() == "" for v in row.values())]
    if not rows:
        return stats

    for col in rows[0]:
        if "time" in col.lower():
            continue
        vals = []
        for row in rows:
            raw = row.get(col, "").strip()
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
                "trend": trend,
                "pct_change": round(pct_change, 1),
                "optimal": optimal,
                "range_status": range_status,
            }
    return stats


def load_history(path):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return {"analyses": []}


def save_history(path, history):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(history, f, indent=2)


def load_images(image_paths):
    parts = []
    for path in image_paths:
        suffix = Path(path).suffix.lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }.get(suffix, "image/jpeg")
        with open(path, "rb") as f:
            data = f.read()
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    return parts


def build_history_text(history):
    if not history["analyses"]:
        return ""
    past = []
    for entry in history["analyses"][-10:]:
        score = entry["analysis"].get("health_score", "?")
        assessment = entry["analysis"].get("overall_assessment", "")[:120]
        note_str = f" | note: {entry['note']}" if entry.get("note") else ""
        past.append(f"  [{entry['date_taken'] or 'unknown'}] score={score} — {assessment}{note_str}")
    return "\nPast observations (most recent 10):\n" + "\n".join(past)


def analyze(image_paths, csv_path=None, note=None, history_path=DEFAULT_HISTORY):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: set GEMINI_API_KEY first", file=sys.stderr)
        print("  export GEMINI_API_KEY=your-key-here", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    history = load_history(history_path)
    metrics = parse_csv(csv_path) if csv_path else None

    date_taken = None
    if image_paths:
        date_taken = extract_date_taken(image_paths[0])

    prompt_parts = [
        DWC_CONTEXT,
        "Analyze the attached images. The first is the main plant photo.",
        "Additional images may be pH meter, EC meter, or other measurements.",
        f"Return a JSON object with exactly these keys:\n{SCHEMA_HINT}",
        "Look for: root rot indicators, pH-related nutrient lockout, nutrient burn,",
        "calcium deficiency, light stress, heat stress, and DWC-specific problems.",
        "Cross-reference all images, metrics, and notes.",
        "Make recommendations concrete and actionable for a DWC grower.",
    ]

    if date_taken:
        prompt_parts.append(f"\nPhoto taken: {date_taken}")

    history_text = build_history_text(history)
    if history_text:
        prompt_parts.append(history_text)

    if metrics:
        csv_lines = []
        anomalies = []
        for col, m in metrics.items():
            trend = m.get("trend", "stable")
            opt = m.get("optimal")
            line = f"{col}: avg={m['avg']}, range={m['min']}-{m['max']}, trend={trend}"
            if opt:
                line += f", optimal={opt['min']}-{opt['max']}{opt['unit']}"
            csv_lines.append(line)
            range_s = m.get("range_status", "ok")
            if range_s != "ok":
                label = opt["label"] if opt else col
                anomalies.append(f"{label} is {range_s} (avg {m['avg']}{opt['unit'] if opt else ''})")
        prompt_parts.append(f"\nSensor data ({metrics[list(metrics.keys())[0]]['samples']} samples):")
        prompt_parts.append("\n".join(csv_lines))
        if anomalies:
            prompt_parts.append("ALERTS: " + "; ".join(anomalies))

    if note:
        prompt_parts.append(f"\nGrower note: {note}")

    image_parts = load_images(image_paths) if image_paths else []

    chat = client.chats.create(model="gemini-3.6-flash")
    response = chat.send_message(
        [types.Part.from_text(text="\n".join(prompt_parts))] + image_parts,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )

    raw = response.text
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        analysis = {"raw_response": raw}

    entry = {
        "timestamp": datetime.now().isoformat(),
        "date_taken": date_taken,
        "photo_paths": [os.path.basename(p) for p in image_paths],
        "csv_path": csv_path,
        "csv_metrics": metrics,
        "note": note,
        "analysis": analysis,
    }
    history["analyses"].append(entry)
    save_history(history_path, history)

    return analysis


def chat(question, history_path=DEFAULT_HISTORY):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: set GEMINI_API_KEY first", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    history = load_history(history_path)

    prompt_parts = [
        DWC_CONTEXT,
        "You are continuing a conversation about a DWC grow.",
        "Full history is provided below. Answer the grower's question using context.",
    ]

    history_text = build_history_text(history)
    if history_text:
        prompt_parts.append("\nHistory:" + history_text)

    prompt_parts.append(f"\nGrower question: {question}")

    chat = client.chats.create(model="gemini-3.6-flash")
    response = chat.send_message(
        [types.Part.from_text(text="\n".join(prompt_parts))],
        config=types.GenerateContentConfig(temperature=0.2),
    )

    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "chat",
        "question": question,
        "answer": response.text,
    }
    history["analyses"].append(entry)
    save_history(history_path, history)

    return response.text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", nargs="+", help="one or more photos to analyze")
    parser.add_argument("--csv", help="AC Infinity exported CSV")
    parser.add_argument("--note", help="add context about this observation")
    parser.add_argument("--chat", help="ask a follow-up question using history")
    parser.add_argument("--save", help="save report to this JSON file")
    parser.add_argument("--history", default=DEFAULT_HISTORY)
    parser.add_argument("--no-history", action="store_true")
    args = parser.parse_args()

    if not args.image and not args.chat:
        parser.error("Provide --image and/or --chat")

    for img in (args.image or []):
        if not os.path.isfile(img):
            print(f"Image not found: {img}")
            sys.exit(1)
    if args.csv and not os.path.isfile(args.csv):
        print(f"CSV not found: {args.csv}")
        sys.exit(1)

    history_path = None if args.no_history else args.history

    if args.chat:
        answer = chat(args.chat, history_path)
        print(answer)
        return

    result = analyze(args.image, args.csv, args.note, history_path)

    if args.save:
        with open(args.save, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Report saved to {args.save}")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
