"""Export recognized positions to a PGN file that ChessBase can open."""
import datetime


def _game_pgn(fen, index, source, page=None, diagram=None, side_to_move="w", eval_symbol=None, result="*"):
    white = f"Position {index}"
    parts = [source] if source else []
    if page is not None:
        parts.append(f"page {page}")
    if diagram is not None:
        parts.append(f"diagram {diagram}")
    if parts:
        white += " (" + ", ".join(parts) + ")"

    headers = [
        f'[Event "{source or "Chess positions"}"]',
        f'[Site "?"]',
        f'[Date "{datetime.date.today().isoformat()}"]',
        f'[Round "?"]',
        f'[White "{white}"]',
        f'[Black "{""}"]',
        f'[Result "{result}"]',
        f'[SetUp "1"]',
        f'[FEN "{fen}"]',
    ]
    comment = f"{{ eval {eval_symbol} }}" if eval_symbol else ""
    return "\n".join(headers) + f"\n{comment} {result}\n" if comment else "\n".join(headers) + f"\n{result}\n"


def export_pgn(positions, out_path, source="Chess positions"):
    """positions: list of dicts with keys fen, page, diagram, side_to_move."""
    with open(out_path, "w") as f:
        for i, pos in enumerate(positions, start=1):
            f.write(_game_pgn(
                pos["fen"], i, source,
                page=pos.get("page"), diagram=pos.get("diagram"),
                side_to_move=pos.get("side_to_move", "w"),
                eval_symbol=pos.get("eval_symbol"),
            ))
            f.write("\n")
    return out_path
