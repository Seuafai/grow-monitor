"""High-level pipeline: image/PDF -> list of recognized positions."""
import cv2
import chess

from .board_detect import detect_boards, extract_board_cells, page_crop
from .pieces import classify_board
from .fen import best_fen


def _legality_warnings(board):
    """Return a list of human-readable issues with the position, or []."""
    warnings = []
    white_kings = len(board.pieces(chess.KING, chess.WHITE))
    black_kings = len(board.pieces(chess.KING, chess.BLACK))
    if white_kings != 1:
        warnings.append(f"white has {white_kings} kings")
    if black_kings != 1:
        warnings.append(f"black has {black_kings} kings")
    for color, name in [(chess.WHITE, "white"), (chess.BLACK, "black")]:
        pawns = len(board.pieces(chess.PAWN, color))
        if pawns > 8:
            warnings.append(f"{name} has {pawns} pawns")
    if board.board_fen().count("k") + board.board_fen().count("K") == 0:
        warnings.append("no kings at all")
    # impossible to have too many pieces
    total = sum(len(board.pieces(p, c)) for p in chess.PIECE_TYPES for c in [chess.WHITE, chess.BLACK])
    if total > 32:
        warnings.append(f"{total} pieces on the board")
    return warnings


def detect_side_marker(gray, rect):
    """Read the '1.' / '1...' marker printed under a diagram's bottom-left
    corner: '1.' (1 dot) means white to move, '1...' (3 dots) black to move.
    Returns "w", "b", or None if no marker is found."""
    x1, y1, x2, y2 = rect
    region_w, region_h = 90, 28
    reg = gray[y2 + 1:y2 + 1 + region_h, x1:x1 + region_w]
    if reg.size == 0:
        return None
    thr = cv2.threshold(reg, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n, _, stats, _ = cv2.connectedComponentsWithStats(thr, 8)
    comps = [stats[j] for j in range(1, n)]
    comps = [c for c in comps if c[4] >= 2 and c[2] <= region_w * 0.6]
    if not comps:
        return None
    # the leading "1": narrow and tall, leftmost
    tall = [c for c in comps if 2 <= c[2] <= 5 and c[3] >= 6]
    if not tall:
        return None
    stroke = min(tall, key=lambda c: c[0])
    s_right = stroke[0] + stroke[2]
    # the glyph right after the dots: a taller blob further right
    glyphs = [c for c in comps
              if 4 <= c[2] <= 9 and 5 <= c[3] <= 14 and 10 <= c[4] <= 40
              and c[0] >= s_right - 2]
    glyph_left = min((c[0] for c in glyphs), default=region_w)
    # count small dot-like components between the "1" and the glyph, in the
    # lower part of the marker (the periods of "1." / "1...")
    y0 = stroke[1] + int(stroke[3] * 0.35)
    dots = [c for c in comps
            if s_right + 1 <= c[0] < glyph_left
            and c[1] >= y0
            and max(c[2], c[3]) <= 4 and c[4] <= 12
            and abs(c[2] - c[3]) <= 2]
    return "b" if len(dots) >= 2 else "w"


def detect_eval_symbol(gray, rect):
    """Read the evaluation symbol printed under a diagram's bottom-right
    corner (from the book's legend: +-, =, etc.). Returns a string or None."""
    x1, y1, x2, y2 = rect
    reg = gray[y2 + 1:y2 + 1 + 26, x2 - 45:x2 + 5]
    if reg.size == 0:
        return None
    thr = cv2.threshold(reg, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n, _, stats, _ = cv2.connectedComponentsWithStats(thr, 8)
    comps = []
    for j in range(1, n):
        x, y, w, h, a = stats[j]
        if a < 5:
            continue
        comps.append((x, y, w, h, int(a), a / (w * h)))
    plus = [c for c in comps if c[2] >= 5 and c[3] >= 4 and c[5] <= 0.7]
    if plus:
        return "+-"
    bars = [c for c in comps if c[3] <= 6 and c[2] >= 4 and c[5] > 0.7]
    if bars:
        span = max(c[1] + c[3] for c in bars) - min(c[1] for c in bars)
        return "=" if span >= 3 else "-"
    return None


def process_image(gray_or_bgr, source="image", page=None, side_to_move="w"):
    """Recognize every board in an image. Returns list of position dicts."""
    if gray_or_bgr.ndim == 3:
        gray = cv2.cvtColor(gray_or_bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = gray_or_bgr
    boards = detect_boards(gray)
    crop_box = page_crop(gray)
    if crop_box is not None:
        crop = gray[crop_box[1]:crop_box[3], crop_box[0]:crop_box[2]]
        crop_boards = detect_boards(crop)
        if len(crop_boards) > len(boards):
            ox, oy = crop_box[0], crop_box[1]
            boards = [{
                "rect": (r["rect"][0] + ox, r["rect"][1] + oy,
                         r["rect"][2] + ox, r["rect"][3] + oy),
                "rows": r["rows"], "cols": r["cols"],
            } for r in crop_boards]
    positions = []
    for di, b in enumerate(boards, start=1):
        marker = detect_side_marker(gray, b["rect"])
        side = marker if marker is not None else side_to_move
        eval_symbol = detect_eval_symbol(gray, b["rect"])
        square, _ = extract_board_cells(gray, b["rect"])
        grid = classify_board(square)
        fen, board, valid, flipped = best_fen(grid, side_to_move=side)
        positions.append({
            "index": di,
            "page": page,
            "diagram": di,
            "source": source,
            "rect": b["rect"],
            "grid": grid,
            "fen": fen,
            "flipped": flipped,
            "side_to_move": side,
            "side_marker": marker,
            "eval_symbol": eval_symbol,
            "syntactically_valid": valid,
            "warnings": _legality_warnings(board) if valid else ["invalid FEN"],
        })
    return positions
