"""Verification for calibration.py - projector <-> camera homography.

No projector and no camera needed. A KNOWN homography is invented, the
projected pattern is warped through it into a synthetic "camera" frame, and
the calibration is then asked to recover it. If it recovers the matrix we
started from, the maths and the point ordering are both right.

That is the whole point: an ordering bug between the projected points and the
detected ones produces a matrix that still looks like a matrix. Only checking
it against ground truth catches that.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_calibration.py
"""
import os
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_calibration.log")
_log = open(LOG, "w", encoding="utf-8", buffering=1)
failures = []


def w(*parts):
    line = " ".join(str(p) for p in parts)
    _log.write(line + "\n")
    _log.flush()


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


os.chdir(tempfile.mkdtemp(prefix="lg_calib_"))

import numpy as np  # noqa: E402
import cv2  # noqa: E402
import calibration as cal  # noqa: E402

CANVAS_W, CANVAS_H = 1280, 720
CAM_W, CAM_H = 1024, 768

# A deliberately off-axis view: the camera is not square-on to the surface,
# which is the realistic case and the one a homography exists to handle.
TRUTH_SRC = np.float32([(0, 0), (CANVAS_W, 0), (CANVAS_W, CANVAS_H), (0, CANVAS_H)])
TRUTH_DST = np.float32([(140, 90), (900, 150), (960, 660), (80, 600)])
TRUTH = cv2.getPerspectiveTransform(TRUTH_SRC, TRUTH_DST)


def fake_camera_view(canvas_image, blur=3, noise=4, brightness=1.0):
    """Warp what the projector shows into what the camera would see."""
    seen = cv2.warpPerspective(canvas_image, TRUTH, (CAM_W, CAM_H))
    if brightness != 1.0:
        seen = np.clip(seen.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    if blur:
        seen = cv2.GaussianBlur(seen, (blur | 1, blur | 1), 0)
    if noise:
        seen = np.clip(seen.astype(np.int16)
                       + np.random.RandomState(7).randint(-noise, noise + 1,
                                                          seen.shape), 0,
                       255).astype(np.uint8)
    return seen


def truth_map(x, y):
    p = TRUTH @ np.array([x, y, 1.0])
    return p[0] / p[2], p[1] / p[2]


# =====================================================================
w("=== pattern generation ===")
grid_img, grid_pts = cal.make_circle_grid(CANVAS_W, CANVAS_H)
check("circle grid image is canvas-sized",
      grid_img.shape[:2] == (CANVAS_H, CANVAS_W), str(grid_img.shape))
check("grid has cols*rows points",
      len(grid_pts) == cal.DEFAULT_GRID_COLS * cal.DEFAULT_GRID_ROWS,
      f"{len(grid_pts)}")
check("every grid point is inside the canvas",
      bool(np.all(grid_pts[:, 0] >= 0) and np.all(grid_pts[:, 0] < CANVAS_W)
           and np.all(grid_pts[:, 1] >= 0) and np.all(grid_pts[:, 1] < CANVAS_H)))
check("rows are staggered, i.e. it really is ASYMMETRIC",
      abs(grid_pts[0][0] - grid_pts[cal.DEFAULT_GRID_COLS][0]) > 1,
      "a symmetric grid could not fix the orientation")

dots = cal.dot_sequence(CANVAS_W, CANVAS_H)
check("dot sequence has 5 points (4 corners + centre)", len(dots) == 5,
      f"{len(dots)}")
check("the 5th is the centre - with only 4 the error is always ~0 and "
      "tells you nothing",
      abs(dots[4][0] - CANVAS_W / 2) < 1 and abs(dots[4][1] - CANVAS_H / 2) < 1)

# =====================================================================
w("")
w("=== 'circles': recover a KNOWN homography end to end ===")
seen = fake_camera_view(grid_img)
found = cal.detect_circle_grid(seen)
check("grid detected in the warped camera view", found is not None,
      f"{0 if found is None else len(found)} centres")

if found is not None:
    check("all points found", len(found) == len(grid_pts), f"{len(found)}")
    result = cal.solve(grid_pts, found, pattern="circles",
                       canvas_size=(CANVAS_W, CANVAS_H))
    w(f"   {result}")
    check("reprojection error is small", result.reprojection_error < 3.0,
          f"{result.reprojection_error:.2f}px")

    # Ground truth: does the recovered mapping agree with the real one?
    worst = 0.0
    for cx, cy in [(100, 100), (640, 360), (1180, 620), (300, 500), (900, 200)]:
        got = result.canvas_to_camera(cx, cy)
        want = truth_map(cx, cy)
        worst = max(worst, float(np.hypot(got[0] - want[0], got[1] - want[1])))
    check(">>> recovered mapping matches the TRUE homography <<<", worst < 3.0,
          f"worst disagreement {worst:.2f}px over 5 probe points")

    # The direction that actually matters for placing graphics on a part.
    worst_rt = 0.0
    for cx, cy in [(200, 150), (640, 360), (1000, 550)]:
        camx, camy = truth_map(cx, cy)
        back = result.camera_to_canvas(camx, camy)
        worst_rt = max(worst_rt, float(np.hypot(back[0] - cx, back[1] - cy)))
    check("camera -> canvas round trips (this is what places a graphic on a blob)",
          worst_rt < 3.0, f"worst {worst_rt:.2f}px")

    check("inverse is a real inverse",
          float(np.abs(result.matrix @ result.inverse - np.eye(3)).max()) < 1e-6)

# =====================================================================
w("")
w("=== 'dots': sequential detection ===")
detected = []
for x, y in dots:
    frame = fake_camera_view(cal.make_single_dot(CANVAS_W, CANVAS_H, x, y))
    centre = cal.detect_bright_dot(frame)
    detected.append(centre)
missed = [i for i, d in enumerate(detected) if d is None]
check("every projected dot was detected", not missed,
      f"{len(detected) - len(missed)}/{len(detected)} found"
      + (f", missed {missed}" if missed else ""))

if all(d is not None for d in detected):
    worst = max(float(np.hypot(d[0] - truth_map(*p)[0], d[1] - truth_map(*p)[1]))
                for d, p in zip(detected, dots))
    check("each detected centroid is near where it truly landed", worst < 4.0,
          f"worst {worst:.2f}px")
    res_dots = cal.solve(dots, detected, pattern="dots",
                         canvas_size=(CANVAS_W, CANVAS_H))
    w(f"   {res_dots}")
    check("dot calibration solves", res_dots.point_count == 5)
    worst = max(float(np.hypot(*(np.array(res_dots.canvas_to_camera(cx, cy))
                                 - np.array(truth_map(cx, cy)))))
                for cx, cy in [(200, 200), (640, 360), (1000, 500)])
    check("dot mapping also matches the true homography", worst < 6.0,
          f"worst {worst:.2f}px")

w("")
w("=== dots survive what would defeat the grid ===")
hard = fake_camera_view(cal.make_single_dot(CANVAS_W, CANVAS_H, 640, 360),
                        blur=15, noise=12, brightness=0.45)
check("a dim, defocused, noisy dot is still found",
      cal.detect_bright_dot(hard) is not None,
      "this is why the sequential fallback exists")
check("a frame with no dot at all returns None, not a guess",
      cal.detect_bright_dot(np.zeros((CAM_H, CAM_W, 3), np.uint8)) is None)

# =====================================================================
w("")
w("=== QUALITY GATE: a bad fit must be refused, not returned ===")
scrambled = np.array(found)[::-1] if found is not None else None
if scrambled is not None:
    try:
        cal.solve(grid_pts, scrambled, pattern="circles")
        check("scrambled correspondences are rejected", False,
              "it returned a mapping for mismatched points!")
    except cal.CalibrationError as e:
        check("scrambled correspondences are REJECTED, not silently fitted",
              "do not correspond" in str(e) or "too poor" in str(e), str(e)[:80])

try:
    cal.solve(dots[:3], dots[:3])
    check("fewer than 4 points is refused", False, "no exception")
except cal.CalibrationError as e:
    check("fewer than 4 points is refused", "at least 4" in str(e), str(e)[:60])

try:
    cal.solve(dots, dots[:3])
    check("mismatched counts are refused", False, "no exception")
except cal.CalibrationError as e:
    check("mismatched counts are refused", "differ" in str(e), str(e)[:60])

try:
    collinear = np.array([(0, 0), (10, 10), (20, 20), (30, 30)], dtype=np.float64)
    cal.solve(collinear, collinear * 2 + 5)
    check("collinear points are refused", False, "no exception")
except cal.CalibrationError as e:
    check("collinear points are refused", True, str(e)[:60])

# =====================================================================
w("")
w("=== persistence ===")
if found is not None:
    d = result.to_dict()
    back = cal.CalibrationResult.from_dict(d)
    check("result survives a dict round trip",
          float(np.abs(back.matrix - result.matrix).max()) < 1e-12
          and back.pattern == "circles" and back.point_count == result.point_count)
    check("canvas size is remembered (a mapping is only valid for one)",
          tuple(back.canvas_size) == (CANVAS_W, CANVAS_H), str(back.canvas_size))

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")
_log.flush()
sys.exit(1 if failures else 0)
