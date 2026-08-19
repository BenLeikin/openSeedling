#!/usr/bin/env python3
"""Per-tray canopy growth measurement from a tray photo.

Measures canopy coverage (share of plant pixels via the excess-green index)
for each TRAY region of the camera grid. Earlier versions measured per cell,
but once seedlings spill over cell boundaries the per-cell attribution is
fiction; tray boundaries are physical, so tray totals stay honest for the
whole run. Camera-based soil-dryness estimation was removed for the same
reason: closed canopies hide the soil entirely.

Run as a subprocess so OpenCV's memory is released after each call, the same
pattern as detect_corners.py.

Usage:  growth.py <image.jpg> '<grid-json>'
  grid-json: {"corners": [[x,y] x4 as TL,TR,BR,BL fractions], "rows": R,
              "cols": C, "trays": [{"id": "1", "cols": 3}, ...]}
  "trays" splits the grid's columns left-to-right; omitted, the whole grid
  is one region with id "all".
Prints:  {"ok": true, "readings": {"canopy:1": 12.3, "canopy:2": 8.1}}
    or:  {"ok": false, "error": "..."}
"""
import json
import sys

# Plant detection uses the excess-green index (ExG = 2G - R - B), not hue.
# The magenta grow light makes leaves read as magenta, so hue-based detection
# zeros out real plants; ExG measures *relative* greenness and survives coloured
# light. Soil sits deeply negative, so a modest positive threshold rejects it
# cleanly. Raise EXG_MIN if soil/perlite trips it, lower it to catch fainter
# leaves. (Absolute coverage is understated under magenta light; for true
# coverage, capture under white light. Good for tracking and ranking either way.)
EXG_MIN = 15
ANALYSIS_W = 1000   # longest image side scaled to this before counting


def _bil(C, u, v):
    """Bilinear interpolation of the four corners; mirrors bil() in app.js."""
    tx = (1 - u) * C[0][0] + u * C[1][0]
    ty = (1 - u) * C[0][1] + u * C[1][1]
    bx = (1 - u) * C[3][0] + u * C[2][0]
    by = (1 - u) * C[3][1] + u * C[2][1]
    return ((1 - v) * tx + v * bx, (1 - v) * ty + v * by)


def rectify(img, corners, out_w=None, out_h=None, cols=6, rows=4):
    """Flatten the tray plane to a rectangle using the four grid corners.

    The camera looks at the trays from an angle, so a region at the far edge
    covers fewer pixels than a near one and its boundaries bow. Coverage is
    already normalised by each region's own area, so this is a refinement
    rather than a correction: it gives true projective boundaries (bilinear
    interpolation of corners is not the same mapping) and an even pixel
    budget per region.

    `corners` are TL, TR, BR, BL as fractions of the image.
    """
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    src = np.float32([[x * w, y * h] for x, y in corners])
    if out_w is None or out_h is None:
        # keep cells square: scale from the longest measured edge
        top = np.linalg.norm(src[1] - src[0])
        bottom = np.linalg.norm(src[2] - src[3])
        left = np.linalg.norm(src[3] - src[0])
        right = np.linalg.norm(src[2] - src[1])
        out_w = int(max(top, bottom))
        out_h = int(max(left, right))
        # square up the cells given the grid shape
        per = max(out_w / max(cols, 1), out_h / max(rows, 1))
        out_w, out_h = int(per * cols), int(per * rows)
    out_w = max(64, min(4000, out_w))
    out_h = max(64, min(4000, out_h))
    dst = np.float32([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]])
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, M, (out_w, out_h))


def analyze(path, grid, rectify_first=True, rotate=0):
    try:
        import cv2
        import numpy as np
    except Exception:
        return {"ok": False, "error": "OpenCV not installed on the Pi."}

    img = cv2.imread(path)
    if img is None:
        return {"ok": False, "error": "Could not read the photo."}

    if rotate:
        codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
        if rotate in codes:
            img = cv2.rotate(img, codes[rotate])

    h, w = img.shape[:2]
    scale = ANALYSIS_W / float(max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
        h, w = img.shape[:2]

    def _green_mask(bgr):
        """Plant mask via excess-green index, robust to the magenta grow light."""
        b, g, r = (bgr[:, :, 0].astype(np.int32), bgr[:, :, 1].astype(np.int32),
                   bgr[:, :, 2].astype(np.int32))
        return ((2 * g - r - b) > EXG_MIN).astype(np.uint8) * 255

    green = _green_mask(img)

    try:
        C = grid["corners"]
        R = int(grid.get("rows", 4))
        K = int(grid.get("cols", 4))
        if len(C) != 4 or not (1 <= R <= 12 and 1 <= K <= 12):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "Bad grid geometry."}

    # tray regions: contiguous column spans, left to right across the grid
    trays = grid.get("trays") or [{"id": "all", "cols": K}]
    spans, col = [], 0
    for t in trays:
        tc = int(t.get("cols", 0))
        if tc < 1:
            continue
        spans.append((str(t.get("id", len(spans) + 1)),
                      col / K, min(col + tc, K) / K))
        col += tc
        if col >= K:
            break
    if not spans:
        return {"ok": False, "error": "No tray columns to analyze."}

    if rectify_first:
        # After rectifying, the tray plane fills the frame exactly, so the
        # sampling regions become straight vertical bands.
        try:
            img = rectify(img, C, cols=K, rows=R)
            h, w = img.shape[:2]
            green = _green_mask(img)
            C = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        except Exception as e:
            print(f"rectify failed ({e}); sampling the original frame")

    readings = {}
    for tid, u0, u1 in spans:
        quad = [_bil(C, u0, 0.0), _bil(C, u1, 0.0),
                _bil(C, u1, 1.0), _bil(C, u0, 1.0)]
        pts = np.array([[int(round(x * w)), int(round(y * h))] for x, y in quad],
                       dtype=np.int32)
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(mask, [pts], 255)
        area = int(np.count_nonzero(mask))
        if area == 0:
            continue
        green_px = int(np.count_nonzero(cv2.bitwise_and(green, mask)))
        readings["canopy:" + tid] = round(100.0 * green_px / area, 1)
    return {"ok": True, "readings": readings}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: growth.py <image> <grid-json>"}))
        return
    try:
        grid = json.loads(sys.argv[2])
    except Exception:
        print(json.dumps({"ok": False, "error": "Bad grid JSON."}))
        return
    print(json.dumps(analyze(sys.argv[1], grid,
                             rectify_first=bool(grid.get("rectify", True)),
                             rotate=int(grid.get("rotate", 0)))))


if __name__ == "__main__":
    main()
