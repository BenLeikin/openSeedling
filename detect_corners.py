#!/usr/bin/env python3
"""Best-effort tray-corner detection for the grow dashboard.

Usage:  detect_corners.py <image.jpg>
Prints: {"ok": true, "corners": [[x,y],...]}   (TL, TR, BR, BL as 0-1 fractions)
   or:  {"ok": false, "error": "..."}

Run as a subprocess so OpenCV's memory is freed after each call rather than
staying resident in the controller process. Detection is a starting point only;
the dashboard always lets you drag the corners to correct it.
"""

import json
import sys


def detect(path):
    try:
        import cv2
        import numpy as np
    except Exception:
        return {"ok": False, "error": "OpenCV isn't installed on the Pi; place corners manually."}

    img = cv2.imread(path)
    if img is None:
        return {"ok": False, "error": "Could not read the latest photo."}

    h, w = img.shape[:2]
    scale = 800.0 / max(h, w)
    small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
    H, W = small.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((5, 5), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return {"ok": False, "error": "Nothing detected; place corners manually."}

    def order(pts):
        pts = np.array(pts, dtype=np.float32)
        s = pts.sum(1)
        d = np.diff(pts, axis=1).ravel()
        return [
            pts[np.argmin(s)],
            pts[np.argmin(d)],
            pts[np.argmax(s)],
            pts[np.argmax(d)],
        ]  # TL, TR, BR, BL

    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)
    quad = None
    for c in cnts[:5]:
        if cv2.contourArea(c) < 0.15 * H * W:
            break
        approx = cv2.approxPolyDP(c, 0.04 * cv2.arcLength(c, True), True)
        if len(approx) == 4:
            quad = order(approx.reshape(4, 2))
            break
    if quad is None:
        quad = order(cv2.boxPoints(cv2.minAreaRect(cnts[0])))

    corners = [
        [round(min(1.0, max(0.0, float(x) / W)), 4), round(min(1.0, max(0.0, float(y) / H)), 4)]
        for x, y in quad
    ]
    return {"ok": True, "corners": corners}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "no image path given"}))
        sys.exit(0)
    try:
        print(json.dumps(detect(sys.argv[1])))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
