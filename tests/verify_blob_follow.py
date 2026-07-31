"""Verification for blob-following text: Observatory blob -> calibration -> canvas.

Exercises the whole chain with a KNOWN homography and a real vision_state.json,
so the maths and the IPC are both proven without a projector or a camera:

    blob centroid in camera pixels   (published by Observatory)
        -> camera_to_canvas via the calibration
            -> text placed at an anchor/offset around it

The behaviour that matters most: with no calibration, or with the blob lost,
the follower must NOT draw at a stale or arbitrary spot. A graphic in the wrong
place on a work surface is worse than an obvious "not calibrated".

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_blob_follow.py
"""
import os
import sys
import json
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_blob_follow.log")
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


ISO = tempfile.mkdtemp(prefix="lg_follow_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import calibration as cal  # noqa: E402
import canvas_items  # noqa: E402
import studio  # noqa: E402

CANVAS_W, CANVAS_H = 1920, 1080

# =====================================================================
w("=== the follow item on its own ===")
item = canvas_items.InteractiveBlobFollowItem("Insert bolt here")
check("starts uncalibrated", item.calibrated is False and item.blob_found is False)
check("says so on the canvas rather than drawing somewhere arbitrary",
      "not calibrated" in item.label.toPlainText(), item.label.toPlainText())
check("is movable and selectable like other assets",
      bool(item.flags() & canvas_items.QGraphicsItem.GraphicsItemFlag.ItemIsMovable))
check("has a resize handle", isinstance(item.handle, canvas_items.MediaResizeHandle))

item.set_blob_canvas_position(960, 540, found=True, calibrated=True)
check("shows the real message once calibrated and found",
      item.label.toPlainText() == "Insert bolt here", item.label.toPlainText())

w("")
w("=== anchoring around the blob ===")
BX, BY = 960.0, 540.0
for anchor in canvas_items.InteractiveBlobFollowItem.ANCHORS:
    item.set_anchor(anchor)
    item.set_blob_canvas_position(BX, BY, True, True)
    x, y = item.pos().x(), item.pos().y()
    wdt, hgt = item.rect().width(), item.rect().height()
    cx, cy = x + wdt / 2.0, y + hgt / 2.0
    w(f"   {anchor:7s} -> box centre ({cx:.0f},{cy:.0f}) vs blob ({BX:.0f},{BY:.0f})")
    if anchor == "Above":
        check("  Above places the text above the blob", cy < BY - 10)
        check("  ...and horizontally centred on it", abs(cx - BX) < 2)
    elif anchor == "Below":
        check("  Below places it below", cy > BY + 10)
    elif anchor == "Left":
        check("  Left places it left", cx < BX - 10)
        check("  ...and vertically centred", abs(cy - BY) < 2)
    elif anchor == "Right":
        check("  Right places it right", cx > BX + 10)
    else:
        check("  Centre sits on the blob", abs(cx - BX) < 2 and abs(cy - BY) < 2)

item.set_anchor("Above")
item.set_offset(40)
item.set_blob_canvas_position(BX, BY, True, True)
near = item.pos().y()
item.set_offset(200)
item.set_blob_canvas_position(BX, BY, True, True)
far = item.pos().y()
check("a larger offset moves the text further from the blob", far < near,
      f"y {near:.0f} -> {far:.0f}")

check("the marker ring is inside the bounding rect (or Qt leaves trails)",
      item.boundingRect().height() > item.rect().height(),
      f"{item.boundingRect().height():.0f} vs {item.rect().height():.0f}")

# =====================================================================
w("")
w("=== full chain: camera pixels -> calibration -> canvas ===")
# A known, off-axis projector/camera relationship.
src = np.float32([(0, 0), (CANVAS_W, 0), (CANVAS_W, CANVAS_H), (0, CANVAS_H)])
dst = np.float32([(210, 130), (1330, 210), (1420, 980), (120, 900)])
H = cv2.getPerspectiveTransform(src, dst)
result = cal.CalibrationResult(np.array(H, dtype=np.float64), 0.4, 44, "circles",
                               (CANVAS_W, CANVAS_H))


def canvas_to_camera(x, y):
    p = H @ np.array([x, y, 1.0])
    return p[0] / p[2], p[1] / p[2]


studio.AuthoringInterface.launch_observatory = lambda self: None
ui = studio.AuthoringInterface(studio.ProjectorCanvas())
ui.add_new_step_node()
app.processEvents()

follower = canvas_items.InteractiveBlobFollowItem("Place part", "", "tool01_Blob")
follower.set_anchor("Centre")
ui.insert_asset(follower, "Blob Follow")
app.processEvents()

# Pretend a part sits at this canvas position; that is what the camera sees.
TRUE_CANVAS = (1200.0, 700.0)
cam_x, cam_y = canvas_to_camera(*TRUE_CANVAS)
w(f"   part at canvas {TRUE_CANVAS} appears at camera ({cam_x:.0f},{cam_y:.0f})")

state = {"states": {}, "responses": {}, "response_times": {}, "captures": {},
         "geometry": {"tool01_Blob": {"type": "Blob Detection", "found": True,
                                      "camera_x": int(round(cam_x)),
                                      "camera_y": int(round(cam_y)),
                                      "angle": 12.0, "area": 900.0}}}
with open("vision_state.json", "w") as f:
    json.dump(state, f)

w("")
w("--- with NO calibration loaded ---")
ui.read_live_vision_state()
check("geometry crossed the IPC", "tool01_Blob" in ui.vision_geometry)
check("follower knows it is not calibrated", follower.calibrated is False)
check("and refuses to claim a position", follower.blob_found is False
      and follower.blob_canvas_pos is None)
check("it says so on the canvas", "not calibrated" in follower.label.toPlainText(),
      follower.label.toPlainText())

w("")
w("--- with the calibration loaded ---")
ui.calibration = result
ui.read_live_vision_state()
check("follower is now calibrated and has found the blob",
      follower.calibrated and follower.blob_found)
got = follower.blob_canvas_pos
err = float(np.hypot(got[0] - TRUE_CANVAS[0], got[1] - TRUE_CANVAS[1])) if got else 999
w(f"   mapped back to canvas {tuple(round(v) for v in got)} "
  f"(true {TRUE_CANVAS}), error {err:.2f}px")
check(">>> the blob maps back to where the part really is <<<", err < 2.0,
      f"{err:.2f}px")
centre = (follower.pos().x() + follower.rect().width() / 2.0,
          follower.pos().y() + follower.rect().height() / 2.0)
check("and the text is drawn there (anchor Centre)",
      abs(centre[0] - TRUE_CANVAS[0]) < 3 and abs(centre[1] - TRUE_CANVAS[1]) < 3,
      f"text centre {tuple(round(v) for v in centre)}")

w("")
w("--- the part is removed from the bench ---")
state["geometry"]["tool01_Blob"] = {"type": "Blob Detection", "found": False}
with open("vision_state.json", "w") as f:
    json.dump(state, f)
ui.read_live_vision_state()
check("follower reports the blob lost", follower.blob_found is False)
check("and does NOT strand the text at the last known spot",
      follower.blob_canvas_pos is None,
      "stale placement on a work surface is worse than none")

w("")
w("--- the part comes back ---")
state["geometry"]["tool01_Blob"] = {"type": "Blob Detection", "found": True,
                                    "camera_x": int(round(cam_x)),
                                    "camera_y": int(round(cam_y))}
with open("vision_state.json", "w") as f:
    json.dump(state, f)
ui.read_live_vision_state()
check("it reacquires", follower.blob_found and follower.blob_canvas_pos is not None)

w("")
w("--- a tool name that does not exist ---")
follower.source_tool = "no_such_tool"
ui.read_live_vision_state()
check("an unknown source tool is handled quietly, not crashed on",
      follower.blob_found is False)
follower.source_tool = "tool01_Blob"

# =====================================================================
w("")
w("=== calibration persistence through Studio ===")
cal_path = os.path.join(ISO, "calib.json")
ui.save_calibration(cal_path)
ui.calibration = None
loaded = ui.load_calibration(cal_path)
check("calibration saves and reloads", loaded is not None
      and loaded.point_count == 44 and loaded.pattern == "circles")
check("the reloaded matrix still maps correctly",
      abs(loaded.camera_to_canvas(cam_x, cam_y)[0] - TRUE_CANVAS[0]) < 2.0)

# =====================================================================
w("")
w("=== the asset survives a save/load round trip ===")
follower.set_anchor("Right")
follower.set_offset(150)
follower.set_font_size(48)
follower.set_message("Torque to 12Nm")
follower.show_marker = False

proj = os.path.join(ISO, "follow.json")
ui.prompt_save_file = lambda *a, **k: proj
ui.prompt_open_file = lambda *a, **k: proj
ui.save_project()
ui.load_project()
app.processEvents()

back = [i for i in ui.all_canvas_items()
        if isinstance(i, canvas_items.InteractiveBlobFollowItem)]
check("follower reloaded", len(back) == 1, f"{len(back)}")
if back:
    b = back[0]
    check("message survives", b.message == "Torque to 12Nm", b.message)
    check("anchor survives", b.anchor == "Right", b.anchor)
    check("offset survives", b.offset_px == 150, str(b.offset_px))
    check("font size survives", b.current_font_size == 48, str(b.current_font_size))
    check("source tool survives", b.source_tool == "tool01_Blob", b.source_tool)
    check("marker flag survives", b.show_marker is False)

w("")
_after = QSettings("LightGuide", "Observatory")
after = {k: _after.value(k) for k in _after.allKeys()}
changed = [k for k in set(SETTINGS_BEFORE) | set(after)
           if SETTINGS_BEFORE.get(k) != after.get(k)]
check("no live setting written", not changed, f"changed={changed}")

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")
_log.flush()
os._exit(1 if failures else 0)
