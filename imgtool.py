#!/usr/bin/env python3
"""Image work for the controller, run as a separate short-lived process.

OpenCV costs about 40 MB the moment it is imported and never gives it back,
on a 512 MB board where memory is the scarcest resource. So the controller
never imports it: every warp, crop, rotation, resize and
sharpness score happens here, launched through camera.oom_first() so that
under pressure the kernel kills this, not the controller. Memory is returned
when the process exits.

    imgtool.py rectify SRC DST CORNERS_JSON COLS ROWS [--max-w N] [--min-side N] [--q N]
    imgtool.py crop    SRC DST ROI_JSON [--max-w N] [--min-side N] [--q N]
    imgtool.py rotate  PATH DEGREES [--q N]                  (in place, atomically)
    imgtool.py flatten DEST_DIR LIST_FILE CORNERS_JSON COLS ROWS [--q N]
    imgtool.py sharpness PATH [PATH ...]                     (one score per line)
    imgtool.py align   CAM1 CAM2                              (JSON: camera 2 -> camera 1)
    imgtool.py stitch  CAM1 CAM2 DST H_JSON [--max-w N] [--q N]

DST may be "-" to write the JPEG to stdout. Exit status 0 on success; errors
go to stderr. ROI is [x, y, w, h] as fractions of the frame.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

import growth

# Two side-by-side cameras: a homography maps camera 2's frame onto camera 1's
# plane. The trays are close to flat and seen from above, so one homography
# holds for the whole overlap (plants standing up are off that plane and may
# double slightly in the seam). It is found once by `align` and reused by
# every `stitch`, which is then only a warp and a blend.
ALIGN_WORK_PX = 1280  # features are matched on frames scaled to this
MIN_INLIERS = 25


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
        img = cv2.resize(
            img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA
        )
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


def canvas_box(H, w, h):
    """(x0, y0, W, Hh): the box holding camera 1's frame and camera 2's
    warped frame, in camera 1's pixel coordinates."""
    quad = cv2.perspectiveTransform(
        np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), np.float64(H)
    ).reshape(-1, 2)
    xs = np.concatenate([quad[:, 0], [0, w]])
    ys = np.concatenate([quad[:, 1], [0, h]])
    x0, y0 = int(np.floor(xs.min())), int(np.floor(ys.min()))
    return x0, y0, int(np.ceil(xs.max())) - x0, int(np.ceil(ys.max())) - y0, quad


def align(p1, p2):
    """Match features between the two frames and fit camera 2 -> camera 1."""
    a = cv2.imread(str(p1), cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(str(p2), cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return {"ok": False, "error": "could not read a camera frame"}
    if a.shape != b.shape:
        return {"ok": False, "error": "the two cameras captured different sizes"}
    h, w = a.shape
    s = min(1.0, ALIGN_WORK_PX / float(max(h, w)))
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    small = [
        clahe.apply(cv2.resize(x, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)) for x in (a, b)
    ]
    del a, b
    orb = cv2.ORB_create(nfeatures=5000)
    (k1, d1), (k2, d2) = (orb.detectAndCompute(x, None) for x in small)
    if d1 is None or d2 is None or len(k1) < MIN_INLIERS or len(k2) < MIN_INLIERS:
        return {"ok": False, "error": "too little detail in the frames to match (is the light on?)"}
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d2, d1, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < MIN_INLIERS:
        return {
            "ok": False,
            "error": f"only {len(good)} matching points; the views need to overlap more",
        }
    src = np.float32([k2[m.queryIdx].pt for m in good])
    dst = np.float32([k1[m.trainIdx].pt for m in good])
    Hs, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    inliers = int(mask.sum()) if mask is not None else 0
    if Hs is None or inliers < MIN_INLIERS:
        return {
            "ok": False,
            "error": f"only {inliers} points agree on a fit; the views need to overlap more",
        }
    S = np.diag([s, s, 1.0])
    H = np.linalg.inv(S) @ Hs @ S
    H /= H[2, 2]
    x0, y0, W, Hh, quad = canvas_box(H, w, h)
    # camera 2 must land beside camera 1, overlapping it, the right way round
    # and about the same size; anything else is a false match
    q32 = quad.astype(np.float32)
    convex = bool(cv2.isContourConvex(q32))
    area = float(cv2.contourArea(q32))
    frame = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    inside = float(cv2.intersectConvexConvex(q32, frame)[0]) if convex else 0.0
    if (
        not convex
        or not 0.5 * w * h <= area <= 2.0 * w * h
        or W > 2.6 * w
        or Hh > 2.0 * h
        or not 0.03 * area <= inside <= 0.97 * area
    ):
        return {
            "ok": False,
            "error": "the match does not look like two cameras side by side; check the overlap",
        }
    return {
        "ok": True,
        "h": [[float(v) for v in row] for row in H],
        "size": [w, h],
        "canvas": [W, Hh],
        "offset": [x0, y0],
        "inliers": inliers,
        "overlap_pct": round(100.0 * inside / area, 1),
    }


def stitch(p1, p2, H, max_w=None):
    """Camera 1 and camera 2 warped onto one canvas, blended across the overlap."""
    a = cv2.imread(str(p1))
    b = cv2.imread(str(p2))
    if a is None or b is None:
        raise SystemExit("could not read a camera frame")
    h, w = a.shape[:2]
    if b.shape[:2] != (h, w):
        raise SystemExit("the two cameras captured different sizes")
    H = np.float64(H)
    x0, y0, W, Hh, quad = canvas_box(H, w, h)
    sc = min(1.0, float(max_w) / W) if max_w else 1.0
    T = np.array([[sc, 0, -x0 * sc], [0, sc, -y0 * sc], [0, 0, 1.0]])
    size = (max(1, int(round(W * sc))), max(1, int(round(Hh * sc))))
    ones = np.full((h, w), 255, np.uint8)
    out = cv2.warpPerspective(a, T, size, flags=cv2.INTER_LINEAR)
    ma = cv2.warpPerspective(ones, T, size, flags=cv2.INTER_NEAREST)
    del a
    wb = cv2.warpPerspective(b, T @ H, size, flags=cv2.INTER_LINEAR)
    mb = cv2.warpPerspective(ones, T @ H, size, flags=cv2.INTER_NEAREST)
    del b, ones
    both = (ma > 0) & (mb > 0)
    only_b = (mb > 0) & (ma == 0)
    del ma, mb
    # the same model of camera can still differ a little in brightness and
    # colour: match camera 2 to camera 1 over the shared area
    if both.any():
        for c in range(3):
            m1 = float(out[..., c][both].mean())
            m2 = float(wb[..., c][both].mean())
            if m2 > 1:
                r = min(1.25, max(0.8, m1 / m2))
                lut = np.clip(np.arange(256) * r, 0, 255).astype(np.uint8)
                wb[..., c] = cv2.LUT(wb[..., c], lut)
    out[only_b] = wb[only_b]
    del only_b
    cols = np.where(both.any(axis=0))[0]
    if cols.size:
        c0, c1 = int(cols[0]), int(cols[-1]) + 1
        # a feathered seam: camera 1 fades into camera 2 across the overlap,
        # in whichever direction camera 2 sits
        t = np.linspace(0.0, 1.0, c1 - c0, dtype=np.float32)
        if quad[:, 0].mean() < w / 2.0:
            t = t[::-1]
        alpha = t[None, :, None]
        # a few rows at a time: the whole band in float is tens of MB
        for r0 in range(0, out.shape[0], 64):
            ra = out[r0 : r0 + 64, c0:c1]
            rb = wb[r0 : r0 + 64, c0:c1]
            sel = both[r0 : r0 + 64, c0:c1]
            mixed = (ra * (1.0 - alpha) + rb * alpha).astype(np.uint8)
            ra[sel] = mixed[sel]
    return out


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
        rot = {
            90: cv2.ROTATE_90_CLOCKWISE,
            180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE,
        }[int(degrees)]
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
                shutil.copyfile(src, out)  # a blip, not a gap in the timeline
    elif op == "sharpness":
        for path in pos:
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                print("nan")
                continue
            h, w = img.shape[:2]
            img = img[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
            print(float(cv2.Laplacian(img, cv2.CV_64F).var()))
    elif op == "align":
        cam1, cam2 = pos
        print(json.dumps(align(cam1, cam2)))
    elif op == "stitch":
        cam1, cam2, dst, H = pos
        _write(stitch(cam1, cam2, json.loads(H), opt.get("max-w")), dst, q)
    else:
        raise SystemExit(f"unknown operation: {op}")


if __name__ == "__main__":
    main(sys.argv[1:])
