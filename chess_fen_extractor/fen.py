"""Build and validate FEN strings from recognized 8x8 grids."""
import chess

PIECE_TO_CHAR = {"p": "p", "n": "n", "b": "b", "r": "r", "q": "q", "k": "k"}


def _rank_str(row):
    """Convert one grid row (list of (piece, color) or None) to a FEN rank."""
    out = []
    empties = 0
    for cell in row:
        if cell is None or cell[0] is None:
            empties += 1
            continue
        if empties:
            out.append(str(empties))
            empties = 0
        piece, color = cell[0], cell[1]
        ch = PIECE_TO_CHAR.get(piece, "p")
        out.append(ch.upper() if color == "w" else ch)
    if empties:
        out.append(str(empties))
    return "".join(out)


def infer_castling(grid):
    """Infer castling rights from piece placement (king e-file + rooks a/h-file)."""
    rights = ""
    for color, rank in [("w", 7), ("b", 0)]:
        king = grid[rank][4]
        if king and king[0] == "k" and king[1] == color:
            kr = grid[rank][7]
            if kr and kr[0] == "r" and kr[1] == color:
                rights += "K" if color == "w" else "k"
            qr = grid[rank][0]
            if qr and qr[0] == "r" and qr[1] == color:
                rights += "Q" if color == "w" else "q"
    return rights if rights else "-"


def grid_to_fen(grid, side_to_move="w", castling="auto", ep="-", halfmove=0, fullmove=1):
    """Build a FEN from an 8x8 grid (list of 8 rows of cells)."""
    ranks = [_rank_str(row) for row in grid]  # row 0 = rank 8 (top)
    placement = "/".join(ranks)
    if castling == "auto":
        castling = infer_castling(grid)
    return f"{placement} {side_to_move} {castling} {ep} {halfmove} {fullmove}"


def validate_fen(fen):
    """Return (is_valid, board or error)."""
    try:
        board = chess.Board(fen)
        return True, board
    except ValueError as e:
        return False, str(e)


def best_fen(grid, side_to_move="w"):
    """Try the FEN as-is and, if invalid, the flipped (180-degree) version.
    Returns (fen, board, valid, used_flip)."""
    fen = grid_to_fen(grid, side_to_move=side_to_move)
    ok, board = validate_fen(fen)
    if ok:
        return fen, board, True, False
    flipped = [list(reversed(row)) for row in reversed(grid)]
    fen2 = grid_to_fen(flipped, side_to_move=side_to_move)
    ok2, board2 = validate_fen(fen2)
    if ok2:
        return fen2, board2, True, True
    return fen, board, False, False
