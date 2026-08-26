"""Board detection: locate all chess diagrams on a page image and extract 8x8 cell grids.

Strategy
--------
1. Bright-component analysis: the 32 light squares of a printed diagram form one
   connected bright region (8-connected), giving the board's bounding box exactly.
2. Line-based grid detection (Hough) as a fallback for pages where the boards
   are not separable from the background by brightness.
3. Every candidate is verified by checking the 8x8 alternating checker pattern.
"""
import cv2
import numpy as np


def load_image(path):
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"Cannot read image: {path}")
    return img


def _cluster(values, gap=5):
    values = sorted(values)
    out = []
    for v in values:
        if out and v - out[-1][0] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], v))
        else:
            out.append((v, v))
    return np.array([int(round((a + b) / 2)) for a, b in out])


def _hough_lines(gray, min_frac=0.06):
    h, w = gray.shape
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=60,
                            minLineLength=int(min(h, w) * min_frac), maxLineGap=6)
    if lines is None:
        return [], []
    hs, vs = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        if abs(x2 - x1) > abs(y2 - y1):
            hs.append((y1, x1, x2))
        else:
            vs.append((x1, y1, y2))
    return hs, vs


def _uniform_runs(pos, tol=0.25, want_intervals=8, max_missing=3):
    """Find runs of line positions forming `want_intervals` intervals of uniform
    spacing, tolerating a small number of missing lines (gaps ~= 2x spacing).

    Returns list of (start_idx, end_idx, spacing, intervals)."""
    runs = []
    n = len(pos)
    if n < 2:
        return runs
    for i in range(n - 1):
        s = pos[i + 1] - pos[i]
        j = i + 1
        missing = 0
        intervals = 1
        while j < n - 1 and intervals < want_intervals:
            d = pos[j + 1] - pos[j]
            if abs(d - s) <= tol * s:
                s = (s + d) / 2
                j += 1
                intervals += 1
            elif abs(d - 2 * s) <= tol * 2 * s and missing < max_missing:
                missing += 1
                s = (s + d / 2) / 2
                j += 1
                intervals += 2
            else:
                break
        if intervals == want_intervals:
            runs.append((i, j, s, intervals))
    return runs


def _boards_from_lines(gray):
    h, w = gray.shape
    hs, vs = _hough_lines(gray)
    if not hs or not vs:
        return []
    hpos = _cluster([int(y) for y, _, _ in hs])
    vpos = _cluster([int(x) for x, _, _ in vs])
    if len(hpos) < 2 or len(vpos) < 2:
        return []
    hruns = _uniform_runs(hpos)
    vruns = _uniform_runs(vpos)
    out = []
    for hi, hj, hs_spacing, _ in hruns:
        for vi, vj, vs_spacing, _ in vruns:
            top = hpos[hi]
            left = vpos[vi]
            right = left + 8 * vs_spacing
            bottom = top + 8 * hs_spacing
            out.append((left, top, right, bottom))
    return out


def _boards_from_components(gray):
    h, w = gray.shape
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    thr = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    thr = 255 - thr  # keep bright regions
    n, labels, stats, _ = cv2.connectedComponentsWithStats(thr, 8)
    areas = stats[:, cv2.CC_STAT_AREA]
    if n < 2:
        return []
    page_idx = int(np.argmax(areas))
    out = []
    min_area = 0.002 * w * h
    max_area = 0.6 * w * h
    for i in range(1, n):
        if i == page_idx:
            continue
        x, y, cw, ch, area = stats[i]
        if not (min_area <= area <= max_area):
            continue
        if not (0.6 < cw / ch < 1.7):
            continue
        out.append((x, y, x + cw, y + ch))
    return out


def _checker_score(gray, rect, rows=8, cols=8):
    """Return Pearson correlation between cell brightness and the checker pattern.

    Score in [-1, 1]: close to 1 = clean alternating board."""
    square, cells = extract_board_cells(gray, rect, rows, cols)
    means = np.zeros((rows, cols), dtype=np.float64)
    for r in range(rows):
        for c in range(cols):
            means[r, c] = cells[r * cols + c].mean()
    pattern = np.fromfunction(lambda r, c: 1 - 2 * ((r + c) % 2), (rows, cols))
    a = means.ravel()
    b = pattern.ravel()
    if a.std() < 3:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _runs(mask):
    """Indices of contiguous True runs in a 1-D boolean array."""
    out = []
    start = None
    for i in range(len(mask)):
        if mask[i] and start is None:
            start = i
        if not mask[i] and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def _merge_runs(runs, gap):
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def page_crop(gray, col_thr=140, row_thr=150, col_gap=130, row_gap=15, margin=25):
    """Find the bounding box of a bright page region on a dark background
    (e.g. a photo of a book page). Returns (x1, y1, x2, y2) or None."""
    h, w = gray.shape
    col = gray.mean(axis=0)
    cols = _merge_runs(_runs(col > col_thr), col_gap)
    if not cols:
        return None
    x1, x2 = max(cols, key=lambda t: t[1] - t[0])
    row = gray[:, x1:x2 + 1].mean(axis=1)
    rows = _merge_runs(_runs(row > row_thr), row_gap)
    if not rows:
        return None
    y1, y2 = max(rows, key=lambda t: t[1] - t[0])
    if (x2 - x1) > 0.9 * w or (y2 - y1) > 0.9 * h:
        return None
    return (max(0, x1 - margin), max(0, y1 - margin),
            min(w, x2 + margin), min(h, y2 + margin))


def detect_boards(gray, min_size_frac=0.04):
    """Return list of dicts describing each detected board in `gray`."""
    h, w = gray.shape

    def validate(cands):
        kept = []
        for left, top, right, bottom in cands:
            cw, ch = right - left, bottom - top
            if cw < min_size_frac * w or ch < min_size_frac * h:
                continue
            if not (0.5 < ch / cw < 2.0):
                continue
            score = _checker_score(gray, (left, top, right, bottom))
            if score < 0.5:
                continue
            kept.append(((left, top, right, bottom), score))
        kept.sort(key=lambda t: -t[1])
        chosen = []
        for rect, score in kept:
            x1, y1, x2, y2 = rect
            a = (x2 - x1) * (y2 - y1)
            dup = False
            for (cx1, cy1, cx2, cy2), _ in chosen:
                ox = min(x2, cx2) - max(x1, cx1)
                oy = min(y2, cy2) - max(y1, cy1)
                if ox <= 0 or oy <= 0:
                    continue
                inter = ox * oy
                b = (cx2 - cx1) * (cy2 - cy1)
                if inter / min(a, b) > 0.15:
                    dup = True
                    break
            if not dup:
                chosen.append((rect, score))
        return [r for r, _ in chosen]

    comps = _boards_from_components(gray)
    found = validate(comps)
    if not found:
        found = validate(_boards_from_lines(gray))

    results = []
    for left, top, right, bottom in sorted(found, key=lambda r: (r[1], r[0])):
        results.append({
            "rect": (int(left), int(top), int(right), int(bottom)),
            "rows": 8,
            "cols": 8,
        })
    return results


def extract_board_cells(gray, rect, rows=8, cols=8):
    """Warp the board rectangle to a square and return a list of cell images + the square."""
    left, top, right, bottom = rect
    size = 640
    src = np.float32([[left, top], [right, top], [right, bottom], [left, bottom]])
    dst = np.float32([[0, 0], [size, 0], [size, size], [0, size]])
    M = cv2.getPerspectiveTransform(src, dst)
    square = cv2.warpPerspective(gray, M, (size, size))
    cells = []
    cell = size // 8
    for r in range(8):
        for c in range(8):
            cells.append(square[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell])
    return square, cells
