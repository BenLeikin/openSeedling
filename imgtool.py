#!/usr/bin/env python3
"""Image work for the controller, run as a separate short-lived process.

OpenCV costs about 40 MB the moment it is imported and never gives it back,
on a 512 MB board where the controller was killed for memory on 26 Sep. So
the controller never imports it: every warp, crop, rotation, resize and
sharpness score happens here, launched through camera.oom_first() so that
under pressure the kernel kills this, not the controller. Memory is returned
when the process exits.

    imgtool.py rectify SRC DST CORNERS_JSON COLS ROWS [--max-w N] [--min-side N] [--q N]
    imgtool.py crop    SRC DST ROI_JSON [--max-w N] [--min-side N] [--q N]
    imgtool.py rotate  PATH DEGREES [--q N]                  (in place, atomically)
    imgtool.py flatten DEST_DIR LIST_FILE CORNERS_JSON COLS ROWS [--q N]
    imgtool.py sharpness PATH [PATH ...]                     (one score per line)

DST may be "-" to write the JPEG to stdout. Exit status 0 on success; errors
go to stderr. ROI is [x, y, w, h] as fractions of the frame.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import cv2

import growth


def _opts(args):
    """Split '--name value' options off the positional arguments."""
    pos, opt = [], {}
    i = 0
    while i < len(args):
        if args[i].startswith("--"):
            opt[args[i][2:]] = args[i + 1]
            i += 2
        else:
            pos.append(args[i])
            i += 1
    return pos, opt


def _read(src, min_side):
    img = growth.imread_min(cv2, src, int(min_side)) if min_side else cv2.imread(str(src))
    if img is None:
        raise SystemExit(f"could not read {src}")
    return img


def _fit(img, max_w):
    if not max_w:
        return img
    h, w = img.shape[:2]
    m = int(max_w)
    if max(h, w) > m:
        s = m / float(max(h, w))
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    return img


def _write(img, dst, q):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(q)])
    if not ok:
        raise SystemExit("encode failed")
    data = buf.tobytes()
    if dst == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    part = Path(str(dst) + ".part")
    part.write_bytes(data)
    os.replace(part, dst)


def crop_array(img, roi):
    """The view crop; same arithmetic as camera.crop_array."""
    ih, iw = img.shape[:2]
    x, y, w, h = roi
    x0, y0 = int(round(iw * x)), int(round(ih * y))
    x1 = min(iw, x0 + max(2, int(round(iw * w))))
    y1 = min(ih, y0 + max(2, int(round(ih * h))))
    return img[y0:y1, x0:x1]


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    op, rest = argv[0], argv[1:]
    pos, opt = _opts(rest)
    q = opt.get("q", 85)
    if op == "rectify":
        src, dst, corners, cols, rows = pos
        img = _read(src, opt.get("min-side"))
        out = growth.rectify(img, json.loads(corners), cols=int(cols), rows=int(rows))
        _write(_fit(out, opt.get("max-w")), dst, q)
    elif op == "crop":
        src, dst, roi = pos
        img = _read(src, opt.get("min-side"))
        _write(_fit(crop_array(img, json.loads(roi)), opt.get("max-w")), dst, q)
    elif op == "rotate":
        path, degrees = pos
        rot = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
               270: cv2.ROTATE_90_COUNTERCLOCKWISE}[int(degrees)]
        img = cv2.imread(str(path))
        if img is None:
            raise SystemExit(f"could not read {path}")
        _write(cv2.rotate(img, rot), path, opt.get("q", 90))
    elif op == "flatten":
        dest, listfile, corners, cols, rows = pos
        dest = Path(dest)
        corners = json.loads(corners)
        frames = [l for l in Path(listfile).read_text().splitlines() if l.strip()]
        for i, src in enumerate(frames):
            out = dest / f"{i:06d}.jpg"
            try:
                img = cv2.imread(src)
                if img is None:
                    raise ValueError("unreadable")
                warped = growth.rectify(img, corners, cols=int(cols), rows=int(rows))
                cv2.imwrite(str(out), warped, [cv2.IMWRITE_JPEG_QUALITY, int(opt.get("q", 88))])
            except Exception:
                shutil.copyfile(src, out)      # a blip, not a gap in the timeline
    elif op == "sharpness":
        for path in pos:
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                print("nan")
                continue
            h, w = img.shape[:2]
            img = img[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
            print(float(cv2.Laplacian(img, cv2.CV_64F).var()))
    else:
        raise SystemExit(f"unknown operation: {op}")


if __name__ == "__main__":
    main(sys.argv[1:])
