"""python -m chess_fen_extractor  ->  launch the web UI."""
from .web import main as web_main

if __name__ == "__main__":
    web_main()
