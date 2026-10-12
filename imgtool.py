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
    imgtool.py align   CAM1 CAM2 [--matches OUT] [--rotate D] (JSON: camera 2 -> camera 1)
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
MIN_INLIERS = 12  # points agreeing on one fit (a 4-parameter fit, so 12 is plenty)
# Plants stand up off the tray plane and the cameras are close, so the same
# leaf shifts by different amounts between the two views (parallax). A tight
# planar fit rejected most true matches (15 of 85 on the rig); the fit
# is now a shift, turn and scale with a tolerance of 2% of the frame, and only
# refined to a full perspective fit when that keeps nearly all of them.
FIT_TOL = 0.02
MAX_TURN_DEG = 15  # each camera is already turned the right way up
SCALE_RANGE = (0.8, 1.25)


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


_ROT = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _rot_pt(x, y, w, h, degrees):
    """A pixel of a w x h image, after cv2.rotate by `degrees` clockwise."""
    return {
        90: (h - 1 - y, x),
        180: (w - 1 - x, h - 1 - y),
        270: (y, w - 1 - x),
    }.get(degrees, (x, y))


def draw_matches(p1, p2, s, k1, k2, good, mask, out, rotate=0):
    """Camera 1 and camera 2 side by side with what matched between them:
    green lines are matches that agree on one fit, red ones were rejected,
    yellow dots are the detail found in each frame. Turned like the photos."""
    frames = []
    for p in (p1, p2):
        img = cv2.imread(str(p))
        frames.append(cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA))
        del img
    fw = frames[0].shape[1]
    comp = np.hstack(frames)
    del frames
    for kps, dx in ((k1, 0), (k2, fw)):
        for kp in kps:
            cv2.circle(comp, (int(kp.pt[0] + dx), int(kp.pt[1])), 1, (0, 170, 200), -1)
    agree = [bool(v) for v in (mask.ravel() if mask is not None else [False] * len(good))]
    for m, ok in sorted(zip(good, agree), key=lambda g: g[1]):  # green drawn on top
        a = tuple(int(v) for v in k1[m.trainIdx].pt)
        b = (int(k2[m.queryIdx].pt[0] + fw), int(k2[m.queryIdx].pt[1]))
        col = (60, 220, 60) if ok else (60, 60, 230)
        cv2.line(comp, a, b, col, 2 if ok else 1, cv2.LINE_AA)
        cv2.circle(comp, a, 5, col, 2)
        cv2.circle(comp, b, 5, col, 2)
    h, w = comp.shape[:2]
    if rotate in _ROT:
        comp = cv2.rotate(comp, _ROT[rotate])
    fs = max(0.6, comp.shape[1] / 1600.0)

    def label(text, x, y):
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 2)
        x, y = int(max(4, min(comp.shape[1] - tw - 4, x - tw / 2))), int(max(th + 6, y))
        cv2.rectangle(comp, (x - 4, y - th - 6), (x + tw + 4, y + 6), (20, 26, 21), -1)
        cv2.putText(
            comp, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, fs, (235, 240, 230), 2, cv2.LINE_AA
        )

    for i in (0, 1):
        # each frame's label at the top of where that frame ended up
        cx, cy = _rot_pt(fw * i + fw / 2, h / 2, w, h, rotate)
        half_h = h if rotate in (0, 180) else fw
        label(f"camera {i + 1}", cx, cy - half_h / 2 + 30 * fs)
    n_ok = sum(agree)
    label(
        f"{len(good)} matches, {n_ok} agree" if mask is not None else f"{len(good)} matches",
        comp.shape[1] / 2,
        comp.shape[0] - 14 * fs,
    )
    _write(comp, out, 80)


def align(p1, p2, matches_out=None, rotate=0):
    """Match features between the two frames and fit camera 2 -> camera 1.
    With matches_out, also draws what matched (draw_matches), pass or fail."""
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
    # SIFT finds about twice the matches of ORB in a thin overlap of soil and
    # leaves; ORB only where this OpenCV lacks it
    if hasattr(cv2, "SIFT_create"):
        det, norm = cv2.SIFT_create(nfeatures=4000), cv2.NORM_L2
    else:
        det, norm = cv2.ORB_create(nfeatures=5000), cv2.NORM_HAMMING
    (k1, d1), (k2, d2) = (det.detectAndCompute(x, None) for x in small)
    del small

    def drawn(good, mask=None):
        if matches_out:
            draw_matches(p1, p2, s, k1, k2, good, mask, matches_out, rotate)

    if d1 is None or d2 is None or len(k1) < MIN_INLIERS or len(k2) < MIN_INLIERS:
        drawn([])
        return {"ok": False, "error": "too little detail in the frames to match (is the light on?)"}
    pairs = cv2.BFMatcher(norm).knnMatch(d2, d1, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.8 * n.distance]
    if len(good) < MIN_INLIERS:
        drawn(good)
        return {
            "ok": False,
            "error": f"only {len(good)} matching points; the views need to overlap more "
            "(about a fifth of each frame)",
        }
    src = np.float32([k2[m.queryIdx].pt for m in good])
    dst = np.float32([k1[m.trainIdx].pt for m in good])
    tol = FIT_TOL * ALIGN_WORK_PX
    A, mask = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=tol, maxIters=5000, confidence=0.995
    )
    inliers = int(mask.sum()) if mask is not None else 0
    Hs = None
    if A is not None:
        Hs = np.vstack([A, [0.0, 0.0, 1.0]])
        scale = float(np.hypot(A[0, 0], A[1, 0]))
        turn = float(np.degrees(np.arctan2(A[1, 0], A[0, 0])))
        if not (SCALE_RANGE[0] <= scale <= SCALE_RANGE[1] and abs(turn) <= MAX_TURN_DEG):
            drawn(good, mask)
            return {
                "ok": False,
                "error": f"the best fit turns camera 2 by {turn:.0f} degrees and scales it "
                f"{scale:.2f}x; check each camera's rotation and that they share a view",
            }
        # with enough agreement, a perspective fit on those points follows a
        # camera that looks at the trays at a slight slant
        if inliers >= 40:
            sel = mask.ravel().astype(bool)
            Hp, pm = cv2.findHomography(src[sel], dst[sel], cv2.RANSAC, tol / 2)
            if Hp is not None and pm is not None and pm.sum() >= 0.85 * inliers:
                Hs = Hp
    drawn(good, mask)
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
        print(json.dumps(align(cam1, cam2, opt.get("matches"), int(opt.get("rotate", 0)))))
    elif op == "stitch":
        cam1, cam2, dst, H = pos
        _write(stitch(cam1, cam2, json.loads(H), opt.get("max-w")), dst, q)
    else:
        raise SystemExit(f"unknown operation: {op}")


if __name__ == "__main__":
    main(sys.argv[1:])
