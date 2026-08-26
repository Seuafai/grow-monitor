"""Piece recognition: classify the contents of each board square as a chess piece.

Design for printed (monochrome) book diagrams and flat screenshots:
- Black pieces render as solid dark glyphs, white pieces as outlined (hollow)
  glyphs, so piece *type* is matched against filled silhouette templates and
  piece *color* is decided by how filled the glyph silhouette is.
"""
import glob
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PIECES = ["p", "n", "b", "r", "q", "k"]

OCCUPANCY_DARK_THRESH = 70
OCCUPANCY_MIN_PIXELS = 350       # interior (non-gridline) dark pixels; pawns ~450-760
# Combined color score = hole_ratio - 0.08*(fill - 0.5). Hollow (white) pieces
# have enclosed bright regions (hole) and thin outlines (low fill); solid
# (black) pieces have neither.
COLOR_WHITE_SCORE = 0.03

# book-style templates learned from confirmed positions, keyed by piece char
BOOK_TEMPLATES = {p: [] for p in PIECES}


def _font_candidates():
    paths = []
    for pattern in [
        "/usr/share/fonts/**/DejaVuSans.ttf",
        "/usr/share/fonts/**/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/**/DejaVuSansMono.ttf",
        "/usr/share/fonts/**/FreeSerif.ttf",
        "/usr/share/fonts/**/FreeSans.ttf",
        "/usr/share/fonts/**/NotoSans*.ttf",
        "/usr/share/fonts/**/NotoSerif*.ttf",
        "/usr/share/fonts/**/URWGothic-Book.otf",
        "/usr/share/fonts/**/URWBookman*.otf",
    ]:
        paths.extend(glob.glob(pattern, recursive=True))
    seen = set()
    out = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _glyph_map(font_path):
    """Return {piece_char: mask(224x224)} for chess glyphs present in a font."""
    size = 224
    out = {}
    for piece, code in zip(PIECES, [0x265F, 0x265E, 0x265D, 0x265C, 0x265B, 0x265A]):
        img = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            return out
        # filled (black) glyphs, centered
        draw.text((size // 2, size // 2), chr(code), font=font, fill=255, anchor="mm")
        mask = np.array(img) > 40
        if mask.sum() < size * size * 0.01:
            continue
        out[piece] = mask
    return out


def _normalize_silhouette(mask, out=64):
    """Resize a boolean (or 0/255 uint8) silhouette to `out x out`, centered,
    keeping aspect."""
    mask = np.asarray(mask)
    if mask.dtype != bool:
        mask = mask > 0
    if mask.size == 0 or not mask.any():
        return np.zeros((out, out), np.uint8)
    ys, xs = np.where(mask)
    top, bottom, left, right = ys.min(), ys.max(), xs.min(), xs.max()
    h, w = bottom - top + 1, right - left + 1
    scale = (out - 4) / max(h, w)
    nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    cropped = mask[top:bottom + 1, left:right + 1].astype(np.uint8) * 255
    resized = cv2.resize(cropped, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((out, out), np.uint8)
    y0 = (out - nh) // 2
    x0 = (out - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def _fill_holes(mask, close_kernel=5):
    """Seal gaps in an outline glyph, then fill enclosed bright regions so a
    hollow (white) piece becomes a filled silhouette matching black pieces."""
    f = mask.astype(np.uint8)
    if close_kernel > 1:
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel, close_kernel))
        f = cv2.morphologyEx(f, cv2.MORPH_CLOSE, kern)
    ff = f.copy()
    flood = np.zeros((f.shape[0] + 2, f.shape[1] + 2), np.uint8)
    cv2.floodFill(ff, flood, (0, 0), 255)
    holes = cv2.bitwise_not(ff) & (f == 0)
    f[holes == 255] = 255
    return f > 0


_TEMPLATES = None


def get_templates():
    """Build silhouette templates once, keyed by piece char."""
    global _TEMPLATES
    if _TEMPLATES is not None:
        return _TEMPLATES
    tpl = {p: [] for p in PIECES}
    for path in _font_candidates():
        glyphs = _glyph_map(path)
        for piece, mask in glyphs.items():
            tpl[piece].append(_normalize_silhouette(mask))
    # dedupe identical shapes from near-duplicate fonts
    for p in PIECES:
        kept = []
        for m in tpl[p]:
            if all(np.mean(np.abs(m - k)) > 2 for k in kept):
                kept.append(m)
        tpl[p] = kept
    _TEMPLATES = tpl
    return tpl


def _color_score_holes(inner, dark_main):
    """Color signal for a glyph: fraction of the glyph bbox that is BRIGHT and
    enclosed by the dark outline (not connected to the bbox border).

    Hollow (white) pieces enclose a large bright region; solid (black) pieces
    have almost none. Robust to light vs dark squares because 'bright' is
    defined relative to the cell's own background."""
    bg = max(120.0, np.percentile(inner, 98))
    ys, xs = np.where(dark_main)
    if len(ys) == 0:
        return 0.0
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    reg = inner[y0:y1 + 1, x0:x1 + 1]
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    sealed = cv2.morphologyEx(dark_main.astype(np.uint8), cv2.MORPH_CLOSE, kern) > 0
    sealed = sealed[y0:y1 + 1, x0:x1 + 1]
    bright = (reg > 0.8 * bg) & ~sealed
    bbox_area = (y1 - y0 + 1) * (x1 - x0 + 1)
    if bbox_area == 0:
        return 0.0
    n, labels = cv2.connectedComponents(bright.astype(np.uint8), 8)
    border_labels = set(labels[0, :]) | set(labels[-1, :])
    border_labels |= set(labels[:, 0]) | set(labels[:, -1])
    border_labels.discard(0)
    connected = np.isin(labels, list(border_labels))
    holes = bright & ~connected
    return float(holes.sum()) / bbox_area


def extract_piece(cell):
    """Given a single cell image (BGR or gray, any size), return
    (silhouette_mask_64x64, color_score) or (None, None) if empty.

    color_score = interior_mean/paper_estimate - fill_ratio. Positive => likely
    white (hollow, paper-bright interior); negative => black (solid)."""
    if cell.ndim == 3:
        cell = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    cell = cell.astype(np.float64)
    h, w = cell.shape
    crop = 4
    inner = cell[crop:h - crop, crop:w - crop]
    dark = inner < OCCUPANCY_DARK_THRESH
    # discard grid-line / square-outline pixels: dark components that touch the
    # cell border (the printed grid between squares and the board border). What
    # remains inside is the piece glyph itself.
    dark_u8 = dark.astype(np.uint8)
    n, labels = cv2.connectedComponents(dark_u8, 8)
    border = set(labels[0, :]) | set(labels[-1, :])
    border |= set(labels[:, 0]) | set(labels[:, -1])
    border.discard(0)
    # remove border-touching components only when they are LINE-LIKE (the thin
    # printed grid between squares) or RING-LIKE (a hollow frame wrapping the
    # whole cell, e.g. a dark square's printed outline at a board corner).
    # Large glyphs that happen to fill a cell (kings) must be kept.
    remove = []
    for l in border:
        comp = labels == l
        ys, xs = np.where(comp)
        bh = ys.max() - ys.min() + 1
        bw = xs.max() - xs.min() + 1
        area = int(comp.sum())
        if min(bh, bw) <= 5 or max(bh, bw) / min(bh, bw) > 5:
            remove.append(l)
        elif bw >= 0.85 * w and bh >= 0.85 * h and area / (bh * bw) < 0.30:
            remove.append(l)
    if remove:
        interior_dark = dark & ~np.isin(labels, list(remove))
    else:
        interior_dark = dark
    if interior_dark.sum() < OCCUPANCY_MIN_PIXELS * (inner.size / (64 * 64)):
        return None, None
    # keep only the largest interior dark component (the glyph) to avoid noise
    n, labels, stats, _ = cv2.connectedComponentsWithStats(interior_dark.astype(np.uint8), 8)
    main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    dark_main = labels == main
    ys, xs = np.where(dark_main)
    bbox_area = (ys.max() - ys.min() + 1) * (xs.max() - xs.min() + 1)
    fill_ratio = dark_main.sum() / bbox_area
    sil = _fill_holes(dark_main)
    hole_ratio = _color_score_holes(inner, dark_main)
    # color: hollow (white) pieces enclose bright regions and have thin outlines;
    # solid (black) pieces have dense ink and almost no enclosed bright area.
    color_score = hole_ratio - 0.08 * (fill_ratio - 0.5)
    return _normalize_silhouette(sil), color_score


def classify_piece(sil):
    """Match a 64x64 silhouette to piece-type templates (font + learned book
    templates). Returns (piece_char, confidence) or (None, 0.0)."""
    templates = get_templates()
    best_piece, best_score = None, -1.0
    silf = sil.astype(np.float64)
    for piece in PIECES:
        masks = BOOK_TEMPLATES[piece] + templates[piece]
        for m in masks:
            mf = m.astype(np.float64)
            a = silf - silf.mean()
            b = mf - mf.mean()
            denom = np.sqrt((a * a).sum() * (b * b).sum())
            if denom == 0:
                continue
            score = float((a * b).sum() / denom)
            if score > best_score:
                best_score = score
                best_piece = piece
    return best_piece, best_score


def classify_cell(cell):
    """Classify a single board square. Returns (piece, color, confidence) or
    (None, None, None) if empty. color in {'w', 'b'}."""
    sil, color_score = extract_piece(cell)
    if sil is None:
        return None, None, None
    color = "w" if color_score > COLOR_WHITE_SCORE else "b"
    piece, conf = classify_piece(sil)
    return piece, color, conf


def add_book_template(piece, sil):
    """Learn a piece glyph from a user-confirmed square (self-calibration).
    Returns True if a genuinely new glyph was added."""
    normalized = _normalize_silhouette(sil)
    for existing in BOOK_TEMPLATES[piece]:
        if np.mean(np.abs(existing - normalized)) <= 2:
            return False
    BOOK_TEMPLATES[piece].append(normalized)
    return True


def save_book_templates(path):
    """Persist learned templates to a file so they survive restarts."""
    np.savez_compressed(path, **{
        f"{p}_{i}": m for p, masks in BOOK_TEMPLATES.items()
        for i, m in enumerate(masks)
    })


def load_book_templates(path):
    """Load learned templates from a file."""
    try:
        data = np.load(path)
    except Exception:
        return
    BOOK_TEMPLATES.clear()
    for p in PIECES:
        BOOK_TEMPLATES[p] = []
    keys = sorted(data.files, key=lambda k: (k.split("_")[0], int(k.split("_")[1])))
    for k in keys:
        piece = k.split("_")[0]
        if piece in BOOK_TEMPLATES:
            BOOK_TEMPLATES[piece].append(data[k])
    return True


def clear_book_templates():
    for p in PIECES:
        BOOK_TEMPLATES[p] = []


def _autoload():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "book_templates.npz")
    if os.path.exists(path):
        load_book_templates(path)


_autoload()


def classify_board(square):
    """Classify an entire warped board. Returns 8x8 list of rows, each cell
    being a dict {'piece','color','conf'} or None."""
    size = square.shape[0]
    cell = size // 8
    grid = []
    for r in range(8):
        row = []
        for c in range(8):
            square_cell = square[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell]
            row.append(classify_cell(square_cell))
        grid.append(row)
    return grid
