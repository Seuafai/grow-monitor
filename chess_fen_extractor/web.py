"""Local web UI: upload a book PDF / photos, correct the recognized positions,
confirm them (which also calibrates the recognizer), and export a ChessBase PGN.

Run with:  .venv/bin/python -m chess_fen_extractor.web
"""
import os
import tempfile
import uuid

import cv2
import numpy as np
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

from .board_detect import load_image
from .fen import grid_to_fen, validate_fen
from .pdf import pdf_pages_to_images
from .pgn_export import export_pgn
from .pieces import (BOOK_TEMPLATES, add_book_template, clear_book_templates,
                     extract_piece, save_book_templates)
from .pipeline import process_image, _legality_warnings
from . import pieces as pieces_mod

TEMPLATES_PATH = os.path.join(os.path.dirname(__file__), "book_templates.npz")

app = Flask(__name__)
SESSIONS = {}
TMP = tempfile.mkdtemp(prefix="chess_fen_web_")

GRID_KEYS = list("KQRBNPkqrbnp")


def _grid_to_str(grid):
    chars = []
    for row in grid:
        for cell in row:
            if cell is None or cell[0] is None:
                chars.append(".")
            else:
                chars.append(cell[0].upper() if cell[1] == "w" else cell[0])
    return "".join(chars)


def _str_to_grid(s):
    if len(s) != 64:
        raise ValueError("grid must be 64 chars")
    grid = []
    for r in range(8):
        row = []
        for c in range(8):
            ch = s[r * 8 + c]
            if ch == ".":
                row.append(None)
            else:
                row.append((ch.lower(), "w" if ch.isupper() else "b"))
        grid.append(row)
    return grid


def _grid_to_grid_cells(grid):
    return [[(c[0], c[1]) if c else None for c in row] for row in grid]


def _montage_path(gray, rect):
    """Build an 8x8 mosaic of the board cells and return its file path."""
    from .board_detect import extract_board_cells
    _, cells = extract_board_cells(gray, rect)
    size = 64
    canvas = np.full((size * 8 + 2 * 9, size * 8 + 2 * 9), 255, np.uint8)
    for i, cell in enumerate(cells):
        r, c = divmod(i, 8)
        tile = cv2.resize(cell, (size, size))
        canvas[2 + r * (size + 2):2 + r * (size + 2) + size,
               2 + c * (size + 2):2 + c * (size + 2) + size] = tile
    path = os.path.join(TMP, f"{uuid.uuid4().hex}.png")
    cv2.imwrite(path, canvas)
    return path


def _reprocess_page(session_id, key):
    """Re-run recognition for a stored page image and refresh its positions."""
    page = SESSIONS[session_id]["pages"][key]
    positions = process_image(page["gray"], source=page["source"], page=page["page"])
    new_positions = []
    for p in positions:
        new_positions.append({
            "id": f"{session_id}-{key}-{p['diagram']}",
            "session_id": session_id,
            "page_key": key,
            "source": p["source"],
            "page": p["page"],
            "diagram": p["diagram"],
            "grid_str": _grid_to_str(p["grid"]),
            "fen": p["fen"],
            "flipped": p["flipped"],
            "side": p["side_to_move"],
            "eval": p.get("eval_symbol"),
            "warnings": p["warnings"],
            "montage": _montage_path(page["gray"], p["rect"]),
        })
    SESSIONS[session_id]["positions"] = [
        q for q in SESSIONS[session_id]["positions"] if q["page_key"] != key
    ] + new_positions


def _update_position(session_id, pos_id, grid_str, side=None):
    grid = _str_to_grid(grid_str)
    positions = SESSIONS[session_id]["positions"]
    for p in positions:
        if p["id"] == pos_id:
            if side is None:
                side = p.get("side", "w")
            fen = grid_to_fen(_grid_to_grid_cells(grid), side_to_move=side)
            ok, board = validate_fen(fen)
            warnings = _legality_warnings(board) if ok else ["invalid FEN"]
            p["grid_str"] = grid_str
            p["fen"] = fen
            p["side"] = side
            p["warnings"] = warnings
            return p
    raise ValueError("unknown position")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    session_id = uuid.uuid4().hex
    SESSIONS[session_id] = {"pages": {}, "positions": [], "source": None}
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files uploaded"}), 400
    keys = []
    for f in files:
        fname = f.filename or "upload"
        if not fname.strip():
            continue
        key = uuid.uuid4().hex
        low = fname.lower()
        if low.endswith(".pdf"):
            tmp_pdf = os.path.join(TMP, f"{key}.pdf")
            f.save(tmp_pdf)
            for pno, img in pdf_pages_to_images(tmp_pdf):
                pkey = f"{key}-p{pno}"
                SESSIONS[session_id]["pages"][pkey] = {
                    "gray": cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
                    "source": fname, "page": pno,
                }
                keys.append(pkey)
        else:
            tmp_img = os.path.join(TMP, f"{key}.img")
            f.save(tmp_img)
            gray = cv2.cvtColor(load_image(tmp_img), cv2.COLOR_BGR2GRAY)
            SESSIONS[session_id]["pages"][key] = {
                "gray": gray, "source": fname, "page": None,
            }
            keys.append(key)
    if not keys:
        return jsonify({"error": "no usable files"}), 400
    SESSIONS[session_id]["source"] = ", ".join(sorted({os.path.basename(
        SESSIONS[session_id]["pages"][k]["source"]) for k in SESSIONS[session_id]["pages"]}))
    for k in keys:
        _reprocess_page(session_id, k)
    return jsonify({"session_id": session_id})


@app.route("/view/<session_id>")
def view(session_id):
    if session_id not in SESSIONS:
        return "session not found", 404
    return render_template("view.html", session_id=session_id)


@app.route("/data/<session_id>")
def data(session_id):
    if session_id not in SESSIONS:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "source": SESSIONS[session_id]["source"],
        "positions": SESSIONS[session_id]["positions"],
    })


@app.route("/montage/<pos_id>.png")
def montage(pos_id):
    for sess in SESSIONS.values():
        for p in sess["positions"]:
            if p["id"] == pos_id:
                return send_file(p["montage"], mimetype="image/png")
    return "not found", 404


@app.route("/pos/<pos_id>/update", methods=["POST"])
def pos_update(pos_id):
    body = request.get_json(force=True)
    session_id, grid_str = body["session_id"], body["grid"]
    p = _update_position(session_id, pos_id, grid_str)
    return jsonify({"fen": p["fen"], "warnings": p["warnings"]})


@app.route("/pos/<pos_id>/side", methods=["POST"])
def pos_side(pos_id):
    body = request.get_json(force=True)
    session_id = body["session_id"]
    side = body.get("side", "b")
    for p in SESSIONS[session_id]["positions"]:
        if p["id"] == pos_id:
            p2 = _update_position(session_id, pos_id, p["grid_str"], side=side)
            return jsonify({"fen": p2["fen"], "warnings": p2["warnings"],
                            "side": p2["side"]})
    return jsonify({"error": "not found"}), 404


@app.route("/pos/<pos_id>/flip", methods=["POST"])
def pos_flip(pos_id):
    body = request.get_json(force=True)
    session_id = body["session_id"]
    for p in SESSIONS[session_id]["positions"]:
        if p["id"] == pos_id:
            grid = _str_to_grid(p["grid_str"])
            grid = [list(reversed(row)) for row in reversed(grid)]
            grid_str = _grid_to_str(grid)
            p2 = _update_position(session_id, pos_id, grid_str)
            p2["flipped"] = not p2["flipped"]
            return jsonify({"fen": p2["fen"], "warnings": p2["warnings"],
                            "grid": p2["grid_str"], "flipped": p2["flipped"]})
    return jsonify({"error": "not found"}), 404


@app.route("/pos/<pos_id>/learn", methods=["POST"])
def pos_learn(pos_id):
    body = request.get_json(force=True)
    session_id, grid_str = body["session_id"], body["grid"]
    pos = _update_position(session_id, pos_id, grid_str)
    # learn the glyph of every occupied square from the actual board image
    sess = SESSIONS[session_id]
    page = sess["pages"][pos["page_key"]]
    square, cells = _extract_cells(page["gray"], pos_id, session_id)
    learned = 0
    already = 0
    grid = _str_to_grid(grid_str)
    for r in range(8):
        for c in range(8):
            cell = grid[r][c]
            if cell is None:
                continue
            sil, _ = extract_piece(cells[r * 8 + c])
            if sil is not None:
                if add_book_template(cell[0], sil):
                    learned += 1
                else:
                    already += 1
    save_book_templates(TEMPLATES_PATH)
    _reprocess_page(session_id, pos["page_key"])
    return jsonify({"learned": learned, "already": already, "session_id": session_id})


def _extract_cells(gray, pos_id, session_id):
    from .board_detect import extract_board_cells
    for p in SESSIONS[session_id]["positions"]:
        if p["id"] == pos_id:
            # find the board rect again from a fresh detection
            from .board_detect import detect_boards
            boards = detect_boards(gray)
            diagram = p["diagram"]
            if diagram <= len(boards):
                return extract_board_cells(gray, boards[diagram - 1]["rect"])
    raise ValueError("board rect not found")


@app.route("/export/<session_id>")
def export(session_id):
    if session_id not in SESSIONS:
        return "not found", 404
    sess = SESSIONS[session_id]
    positions = [{"fen": p["fen"], "page": p["page"], "diagram": p["diagram"]}
                 for p in sess["positions"]]
    out = os.path.join(TMP, f"{session_id}.pgn")
    export_pgn(positions, out, source=sess["source"])
    return send_file(out, as_attachment=True,
                     download_name="chess_positions.pgn")


@app.route("/api/templates")
def api_templates():
    return jsonify({p: len(BOOK_TEMPLATES[p]) for p in BOOK_TEMPLATES})


@app.route("/api/templates/reset", methods=["POST"])
def api_templates_reset():
    clear_book_templates()
    save_book_templates(TEMPLATES_PATH)
    return jsonify({p: 0 for p in BOOK_TEMPLATES})


def main():
    pieces_mod.load_book_templates(TEMPLATES_PATH)
    print("Opening Chess Position Recognizer at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
