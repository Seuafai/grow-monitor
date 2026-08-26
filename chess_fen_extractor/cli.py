"""Command-line interface: convert chess image(s)/PDF to a PGN of positions."""
import argparse
import sys

from .board_detect import load_image
from .pipeline import process_image
from .pgn_export import export_pgn


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Recognize chess positions from book pages / photos and "
                    "export them to a PGN file for ChessBase.")
    ap.add_argument("input", nargs="+", help="image file(s) and/or PDF files")
    ap.add_argument("-o", "--output", default="positions.pgn", help="output PGN path")
    ap.add_argument("-s", "--side", default="w", choices=["w", "b"], help="side to move (default: w)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    positions = []
    for path in args.input:
        low = path.lower()
        if low.endswith(".pdf"):
            from .pdf import pdf_pages_to_images
            for page, img in pdf_pages_to_images(path):
                pos = process_image(img, source=path, page=page, side_to_move=args.side)
                print(f"{path} page {page}: {len(pos)} position(s)")
                positions.extend(pos)
        else:
            img = load_image(path)
            pos = process_image(img, source=path, side_to_move=args.side)
            print(f"{path}: {len(pos)} position(s)")
            positions.extend(pos)
        if args.verbose:
            for p in positions:
                if p.get("warnings"):
                    print(f"  ! {p['source']} p{p.get('page')} d{p['diagram']}: {p['warnings']}")

    if not positions:
        print("No positions recognized.", file=sys.stderr)
        return 1

    export_pgn(positions, args.output, source=", ".join(args.input))
    print(f"\nExported {len(positions)} position(s) to {args.output}")
    for i, p in enumerate(positions, start=1):
        flag = "  [check]" if p.get("warnings") else ""
        print(f"  {i:2d}. {p['fen']}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
