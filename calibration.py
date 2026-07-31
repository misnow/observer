"""
Projector <-> camera calibration.

Studio projects a known pattern onto the work surface; Observatory sees it
through the camera. Matching the two point sets gives a homography, and with
it either direction:

    canvas pixel  --H-->  camera pixel      "where will this graphic land?"
    camera pixel  --H'->  canvas pixel      "a blob is HERE - what do I
                                             project so it lands on it?"

The second direction is the point of the exercise: it is what lets a graphic
be placed onto a part the camera found.

NOT the same as the Reference Plane tool. That one maps camera -> real-world
inches from four measured distances. This maps camera <-> projector, and needs
no tape measure: the projector already knows exactly where it drew each point.

TWO PATTERNS, BOTH USEFUL
-------------------------
  "circles" - an asymmetric circle grid, found in one shot by
              cv2.findCirclesGrid. Dozens of correspondences at sub-pixel
              accuracy, and it is self-validating: the grid is either found
              whole or not at all, so a partial or mis-ordered detection
              cannot quietly produce a plausible-but-wrong mapping. The
              asymmetric layout also fixes the orientation, which a
              symmetric grid cannot.

  "dots"    - one dot at a time, each detected as the brightest blob. Slower
              (a project/capture round trip per point) but it needs no grid
              detection at all, so it survives defocus, a cluttered or
              textured surface, and partial occlusion that would defeat the
              grid. The fallback when "circles" refuses.

QUALITY, NOT JUST SUCCESS
-------------------------
Solving always "works" - four points and a bad match still yield a matrix.
`CalibrationResult.reprojection_error` is the honest measure: canvas points
pushed through the homography and compared against where they were actually
seen. A large error means the mapping is wrong even though nothing raised.
`solve()` refuses a fit worse than `max_error` rather than handing back a
mapping that would place graphics in the wrong place.
"""

import numpy as np
import cv2


AVAILABLE_PATTERNS = ("circles", "dots")

# Asymmetric grid: 4 points per row, 11 rows - the layout OpenCV's own
# tutorials use, and enough correspondences to average out detection noise.
DEFAULT_GRID_COLS = 4
DEFAULT_GRID_ROWS = 11

# A fit worse than this (in canvas pixels, RMS) is treated as a failed
# calibration rather than a usable one.
DEFAULT_MAX_ERROR = 5.0


class CalibrationError(RuntimeError):
    """Calibration could not be completed."""


class CalibrationResult:
    """A solved mapping, and how much to trust it."""

    def __init__(self, matrix, reprojection_error, point_count, pattern,
                 canvas_size):
        self.matrix = matrix                          # canvas -> camera
        self.reprojection_error = float(reprojection_error)
        self.point_count = int(point_count)
        self.pattern = pattern
        self.canvas_size = canvas_size                # (w, h) it was solved for

    @property
    def inverse(self):
        """camera -> canvas. The direction that places a graphic on a blob."""
        return np.linalg.inv(self.matrix)

    def canvas_to_camera(self, x, y):
        return _apply(self.matrix, x, y)

    def camera_to_canvas(self, x, y):
        return _apply(self.inverse, x, y)

    def to_dict(self):
        return {"matrix": self.matrix.tolist(),
                "reprojection_error": self.reprojection_error,
                "point_count": self.point_count,
                "pattern": self.pattern,
                "canvas_size": list(self.canvas_size)}

    @classmethod
    def from_dict(cls, data):
        return cls(np.array(data["matrix"], dtype=np.float64),
                   data.get("reprojection_error", 0.0),
                   data.get("point_count", 0),
                   data.get("pattern", ""),
                   tuple(data.get("canvas_size", (1920, 1080))))

    def __repr__(self):
        return (f"CalibrationResult({self.pattern}, {self.point_count} pts, "
                f"err={self.reprojection_error:.2f}px)")


def _apply(matrix, x, y):
    """Push one point through a 3x3 homography."""
    vec = np.array([float(x), float(y), 1.0], dtype=np.float64)
    out = matrix @ vec
    if abs(out[2]) < 1e-12:
        raise CalibrationError(
            f"Point ({x}, {y}) maps to infinity under this homography - the "
            f"calibration is degenerate.")
    return float(out[0] / out[2]), float(out[1] / out[2])


# --------------------------------------------------------------------------
# Pattern generation - what Studio projects
# --------------------------------------------------------------------------
def make_circle_grid(canvas_w, canvas_h, cols=DEFAULT_GRID_COLS,
                     rows=DEFAULT_GRID_ROWS, margin_ratio=0.12, radius_ratio=0.28):
    """White asymmetric circle grid on black, plus the canvas centres.

    Returns (image, points) where points[i] is the canvas coordinate of the
    i-th circle in the SAME ORDER cv2.findCirclesGrid returns them, so the two
    sets correspond index by index with no sorting step to get wrong.
    """
    image = np.zeros((int(canvas_h), int(canvas_w), 3), np.uint8)

    # Asymmetric layout: every other row is offset by half a step, which is
    # what makes the grid's orientation unambiguous.
    #
    # The spacing must be UNIFORM - rows d apart, columns 2d - or the detector
    # will not recognise the geometry. Deriving separate x and y steps from the
    # canvas aspect stretches the grid (3:1 on a 16:9 canvas) and
    # findCirclesGrid then finds nothing at all. So one step is computed to fit
    # whichever axis is tighter, and the grid is centred in what is left.
    usable_w = canvas_w * (1 - 2 * margin_ratio)
    usable_h = canvas_h * (1 - 2 * margin_ratio)
    span_x_units = 2 * (cols - 1) + 1      # widest row, in units of d
    span_y_units = rows - 1
    step = min(usable_w / max(1, span_x_units), usable_h / max(1, span_y_units))

    origin_x = (canvas_w - span_x_units * step) / 2.0
    origin_y = (canvas_h - span_y_units * step) / 2.0
    radius = max(3, int(step * radius_ratio))

    points = []
    for row in range(rows):
        for col in range(cols):
            x = origin_x + (2 * col + (row % 2)) * step
            y = origin_y + row * step
            points.append((x, y))
            cv2.circle(image, (int(round(x)), int(round(y))), radius,
                       (255, 255, 255), -1, lineType=cv2.LINE_AA)
    return image, np.array(points, dtype=np.float64)


def make_single_dot(canvas_w, canvas_h, x, y, radius_ratio=0.02):
    """One white dot on black, for the sequential pattern."""
    image = np.zeros((int(canvas_h), int(canvas_w), 3), np.uint8)
    radius = max(4, int(min(canvas_w, canvas_h) * radius_ratio))
    cv2.circle(image, (int(round(x)), int(round(y))), radius,
               (255, 255, 255), -1, lineType=cv2.LINE_AA)
    return image


def dot_sequence(canvas_w, canvas_h, margin_ratio=0.15):
    """Canvas points for the sequential pattern.

    Five points, not four: the four corners pin the perspective, and the
    centre one is what reveals a bad fit. With exactly four points the
    homography passes through them perfectly by construction, so the
    reprojection error is always ~0 and tells you nothing.
    """
    mx = canvas_w * margin_ratio
    my = canvas_h * margin_ratio
    return np.array([
        (mx, my),
        (canvas_w - mx, my),
        (canvas_w - mx, canvas_h - my),
        (mx, canvas_h - my),
        (canvas_w / 2.0, canvas_h / 2.0),
    ], dtype=np.float64)


# --------------------------------------------------------------------------
# Detection - what Observatory does with the camera frame
# --------------------------------------------------------------------------
def _permissive_detector():
    """A blob detector that will accept a projected circle.

    The default SimpleBlobDetector is tuned for a printed target: it filters on
    area, circularity, inertia and convexity, and a projected circle that is
    defocused, keystoned or unusually large fails several of those. Only area
    is kept, with a wide range.
    """
    params = cv2.SimpleBlobDetector_Params()
    params.filterByArea = True
    params.minArea = 8
    params.maxArea = 500000
    params.filterByCircularity = False
    params.filterByInertia = False
    params.filterByConvexity = False
    params.filterByColor = False
    return cv2.SimpleBlobDetector_create(params)


def detect_circle_grid(frame, cols=DEFAULT_GRID_COLS, rows=DEFAULT_GRID_ROWS):
    """Find the asymmetric circle grid. Returns Nx2 centres, or None.

    Tries the INVERTED image first. cv2.findCirclesGrid looks for dark blobs
    on a light background - the convention for a printed target - but a
    projector emits light, so the pattern arrives white on black and the
    default search finds nothing at all. Both polarities are attempted so the
    same call works for a projected pattern and a printed one.
    """
    gray = _as_gray(frame)
    flags = cv2.CALIB_CB_ASYMMETRIC_GRID
    detector = _permissive_detector()

    for candidate in (cv2.bitwise_not(gray), gray):
        for blob_detector in (detector, None):
            try:
                if blob_detector is None:
                    found, centres = cv2.findCirclesGrid(
                        candidate, (cols, rows), flags=flags)
                else:
                    found, centres = cv2.findCirclesGrid(
                        candidate, (cols, rows), flags=flags,
                        blobDetector=blob_detector)
            except cv2.error:
                continue
            if found and centres is not None:
                return centres.reshape(-1, 2).astype(np.float64)
    return None


def detect_bright_dot(frame, min_area=12, threshold=None):
    """Centroid of the brightest blob, or None.

    Thresholds relative to the frame's own maximum rather than a fixed level,
    so it works on a dim projector or a bright room without retuning.
    """
    gray = _as_gray(frame)
    peak = int(gray.max())
    if peak < 40:
        return None                    # nothing bright enough to be the dot
    level = threshold if threshold is not None else max(40, int(peak * 0.6))
    _ret, mask = cv2.threshold(gray, level, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0.0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area <= best_area:
            continue
        moments = cv2.moments(contour)
        if abs(moments["m00"]) < 1e-9:
            continue
        best = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])
        best_area = area
    return best


def _as_gray(frame):
    if frame is None:
        raise CalibrationError("No camera frame supplied.")
    if frame.ndim == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame


# --------------------------------------------------------------------------
# Solving
# --------------------------------------------------------------------------
def solve(canvas_points, camera_points, pattern="", canvas_size=(1920, 1080),
          max_error=DEFAULT_MAX_ERROR):
    """Fit canvas -> camera and refuse a bad fit.

    RANSAC rather than a plain least-squares fit: one mis-detected point
    otherwise drags the whole mapping, and a single bad correspondence is
    exactly what happens when a reflection or a bright fixture is mistaken
    for a projected dot.
    """
    canvas = np.asarray(canvas_points, dtype=np.float64).reshape(-1, 2)
    camera = np.asarray(camera_points, dtype=np.float64).reshape(-1, 2)
    if len(canvas) != len(camera):
        raise CalibrationError(
            f"Point counts differ: {len(canvas)} projected vs {len(camera)} "
            f"detected. They must correspond one to one.")
    if len(canvas) < 4:
        raise CalibrationError(
            f"A homography needs at least 4 points; got {len(canvas)}.")

    matrix, _mask = cv2.findHomography(canvas, camera, cv2.RANSAC, 3.0)
    if matrix is None:
        raise CalibrationError(
            "Could not fit a homography to these points - they may be "
            "collinear, or the detected points may not correspond to the "
            "projected ones.")

    error = reprojection_error(matrix, canvas, camera)
    if error > max_error:
        raise CalibrationError(
            f"Calibration fit is too poor to use: {error:.1f}px RMS "
            f"reprojection error (limit {max_error:.1f}px). The detected "
            f"points probably do not correspond to the projected ones - "
            f"check the whole pattern is visible and in focus.")
    return CalibrationResult(matrix, error, len(canvas), pattern, canvas_size)


def reprojection_error(matrix, canvas_points, camera_points):
    """RMS distance, in camera pixels, between where each projected point
    should have landed and where it was actually seen."""
    canvas = np.asarray(canvas_points, dtype=np.float64).reshape(-1, 1, 2)
    camera = np.asarray(camera_points, dtype=np.float64).reshape(-1, 2)
    mapped = cv2.perspectiveTransform(canvas, matrix).reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum((mapped - camera) ** 2, axis=1))))
