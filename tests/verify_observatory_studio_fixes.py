"""Verification for the five Observatory/Studio fixes.

Covers:
  1. Camera switching actually moves the live feed (was: only tool visibility)
  2. Motion "Pixel Diff Thresh" default (was: 127, which never triggered)
  3. Capture/cutout full paths + cutout thumbnail in the Observatory panel
  4. Raw-vs-segmented picker: cutout published by Observatory, chosen in Studio
  5. Studio File -> New

Drives real code paths: the real Qt signal for the camera combo, the real
cv2 motion pipeline, a real GrabCut segmentation on a real worker thread, and
a real vision_state.json round trip.

Also asserts the live QSettings("LightGuide", "Observatory") namespace is
byte-for-byte unchanged at the end - test scripts polluting that namespace
has bitten this project before (see HANDOFF.md section 3).

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_observatory_studio_fixes.py
"""
import os
import sys
import json
import time
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_observatory_studio_fixes.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_fixes_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication(sys.argv)

# Snapshot the LIVE settings namespace so we can prove we didn't touch it.
_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import vision_tools  # noqa: E402
import studio  # noqa: E402
import main as om  # noqa: E402

# ===========================================================================
w("=== 2. Motion Detection pixel-diff threshold default ===")
# (checked first: pure, no app construction needed)
roi_motion = vision_tools.SearchROI(0, 0, "m", "Motion Detection")
roi_delete = vision_tools.SearchROI(0, 0, "d", "Delete (Missing Object)")
roi_blob = vision_tools.SearchROI(0, 0, "b", "Blob Detection")
check("Motion Detection default is 25", roi_motion.threshold == 25, f"got {roi_motion.threshold}")
check("Delete (Missing Object) default is 25", roi_delete.threshold == 25, f"got {roi_delete.threshold}")
check("Blob Detection min-area default UNCHANGED at 127", roi_blob.threshold == 127,
      f"got {roi_blob.threshold} (same field means a different thing for blobs)")

# Behavioural proof, through the exact cv2 pipeline main.py runs: a realistic
# inter-frame delta must register at the new default and be invisible at the old.
ref = np.full((100, 100), 100, np.uint8)
cur = ref.copy()
cur[30:70, 30:70] = 130          # +30 grey levels - a normal lighting/motion delta
delta = cv2.absdiff(ref, cur)


def motion_area(threshold_value):
    _, th = cv2.threshold(delta, threshold_value, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return sum(cv2.contourArea(c) for c in contours)


area_new, area_old = motion_area(25), motion_area(127)
check("a realistic +30 delta IS detected at the new default", area_new > 0, f"area={area_new:.0f}")
check("the same delta was INVISIBLE at the old default of 127", area_old == 0,
      f"area={area_old:.0f} - this is the reported bug")

# ===========================================================================
w("")
w("=== 1. Camera switching moves the live feed ===")
opened, released = [], []
_real_videocapture = cv2.VideoCapture


class FakeCap:
    """Stands in for a real device so switching can be driven deterministically
    on a machine that may have zero (or one) camera attached."""

    def __init__(self, index, *a, **k):
        self.index = index
        self._open = True
        opened.append(index)

    def isOpened(self):
        return self._open

    def set(self, *a):
        return True

    def read(self):
        return True, np.full((240, 320, 3), 40, np.uint8)

    def release(self):
        self._open = False
        released.append(self.index)


cv2.VideoCapture = FakeCap
try:
    obs = om.ObservatoryEngine(logger=lambda m: w("   [OBS]", m))
    check("camera selector populated", obs.camera_selector.count() >= 2,
          f"{obs.camera_selector.count()} entries")

    obs.camera_selector.setCurrentIndex(0)
    opened.clear(); released.clear()
    obs.toggle_camera()
    check("feed starts on camera 0", obs.timer.isActive() and opened == [0],
          f"opened={opened}")

    opened.clear(); released.clear()
    # The real Qt signal - this is the path that was broken.
    obs.camera_selector.setCurrentIndex(1)
    app.processEvents()
    check("old camera released on switch", 0 in released, f"released={released}")
    check("new camera opened on switch", 1 in opened, f"opened={opened}")
    check("camera_index tracks the new device", obs.camera_index == 1, f"got {obs.camera_index}")
    check("feed still running after switch", obs.timer.isActive())

    # Switching while stopped must NOT start a feed.
    obs.toggle_camera()
    check("feed stopped", not obs.timer.isActive())
    opened.clear()
    obs.camera_selector.setCurrentIndex(0)
    app.processEvents()
    check("switching while stopped does not auto-start", not obs.timer.isActive() and not opened,
          f"opened={opened}")
    obs.camera_selector.setCurrentIndex(1)
    app.processEvents()
finally:
    cv2.VideoCapture = _real_videocapture

# ===========================================================================
w("")
w("=== 4. Cutout published by Observatory, selectable in Studio ===")
frame = np.full((300, 400, 3), 20, np.uint8)
cv2.circle(frame, (200, 150), 70, (60, 200, 240), -1)
obs.current_raw_frame = frame.copy()

obs.tool_selector.setCurrentText("Image Capture")
obs.spawn_tool_roi(200, 150)
cap_tool = [i for i in obs.cam_scene.items()
            if getattr(i, 'tool_type', None) == "Image Capture"][0]
cap_tool.update_size(200, 200)
cap_tool.setPos(100, 50)
cap_tool.camera_index = obs.current_camera_index()

path = obs.capture_tool_image(cap_tool)
check("capture written", bool(path) and os.path.exists(path), str(path))
check("capture published to Studio", cap_tool.tool_name in obs.captures)

import segmenter as _seg  # noqa: E402
ok_grabcut = _seg.backend_status().get("grabcut", (False, ""))[0]
w("   grabcut backend available:", ok_grabcut)

if ok_grabcut:
    obs.segment_tool_capture(cap_tool, "grabcut")
    deadline = time.time() + 60
    while time.time() < deadline and not cap_tool.cutout_path:
        app.processEvents()
        time.sleep(0.05)
    check("real GrabCut segmentation produced a cutout",
          bool(cap_tool.cutout_path) and os.path.exists(cap_tool.cutout_path),
          str(cap_tool.cutout_path))
    entry = obs.captures.get(cap_tool.tool_name, {})
    check("cutout PUBLISHED alongside the raw capture (the missing link)",
          bool(entry.get("cutout_path")) and entry.get("cutout_time", 0) > 0,
          f"keys={sorted(entry)}")
else:
    check("grabcut backend available", False, "cannot exercise segmentation")
    entry = {}

# --- Studio side: pick between the two variants over a real vision_state ---
studio.AuthoringInterface.launch_observatory = lambda self: None
ui = studio.AuthoringInterface(studio.ProjectorCanvas())
ui.add_new_step_node()

import canvas_items  # noqa: E402
item = canvas_items.InteractiveCaptureItem("Cap", "", cap_tool.tool_name)
ui.insert_asset(item, "Capture")

with open("vision_state.json", "w") as f:
    json.dump({"states": {}, "responses": {}, "response_times": {},
               "captures": obs.captures}, f)

item.source_variant = "Captured"
item.last_shown_time = 0.0
ui.read_live_vision_state()
check("Studio shows the RAW capture when variant=Captured",
      os.path.normcase(item.image_path or "") == os.path.normcase(path or ""),
      os.path.basename(item.image_path or "(none)"))

if entry.get("cutout_path"):
    item.source_variant = "Segmented Cutout"
    item.last_shown_time = 0.0
    ui.read_live_vision_state()
    check("Studio swaps to the CUTOUT when variant=Segmented Cutout",
          os.path.normcase(item.image_path or "") == os.path.normcase(entry["cutout_path"]),
          os.path.basename(item.image_path or "(none)"))

# --- variant survives a save/load round trip ---
proj = os.path.join(ISO, "variant.json")
ui.prompt_save_file = lambda *a, **k: proj
ui.prompt_open_file = lambda *a, **k: proj
item.source_variant = "Segmented Cutout"
ui.save_project()
ui.load_project()
restored = [t for t in
            [ui.step_tree.topLevelItem(i).child(j).data(1, Qt.ItemDataRole.UserRole)
             for i in range(ui.step_tree.topLevelItemCount())
             for j in range(ui.step_tree.topLevelItem(i).childCount())]
            if isinstance(t, canvas_items.InteractiveCaptureItem)]
check("source_variant survives save/load",
      bool(restored) and restored[0].source_variant == "Segmented Cutout",
      restored[0].source_variant if restored else "no item restored")

# ===========================================================================
w("")
w("=== 3. Observatory panel shows full paths + cutout thumbnail ===")
# Build the REAL Image Capture properties panel - this is the code path that
# was missing the cutout thumbnail and the file locations.
obs.load_tool_properties_to_ui(cap_tool)
app.processEvents()
check("panel built a capture path field", hasattr(obs, 'capture_path_field'))
check("panel built a cutout path field", hasattr(obs, 'cutout_path_field'))
check("panel built a cutout thumbnail", hasattr(obs, 'cutout_thumb_label'))
check("capture path field holds the full capture location",
      getattr(obs, 'capture_path_field', None) is not None
      and obs.capture_path_field.text() == path,
      getattr(obs, 'capture_path_field', None) and obs.capture_path_field.text())
if entry.get("cutout_path"):
    check("cutout path field holds the full cutout location",
          obs.cutout_path_field.text() == cap_tool.cutout_path,
          obs.cutout_path_field.text())
    check("cutout thumbnail actually rendered a pixmap",
          not obs.cutout_thumb_label.pixmap().isNull(),
          f"{obs.cutout_thumb_label.pixmap().width()}x"
          f"{obs.cutout_thumb_label.pixmap().height()}")
    check("capture thumbnail actually rendered a pixmap",
          not obs.capture_thumb_label.pixmap().isNull(),
          f"{obs.capture_thumb_label.pixmap().width()}x"
          f"{obs.capture_thumb_label.pixmap().height()}")

row, field = obs._make_path_row(path)
check("_make_path_row shows the FULL path, not the basename",
      field.text() == path and os.sep in field.text(), field.text())
check("path row has a tooltip for long paths", field.toolTip() == path)
row_none, field_none = obs._make_path_row("")
check("empty path row degrades gracefully", field_none.text() == "")
check("reveal_in_explorer is guarded against a missing file",
      obs.reveal_in_explorer("") is None)
check("update_cutout_thumbnail is safe with no panel built",
      obs.update_cutout_thumbnail(cap_tool) is None)

# ===========================================================================
w("")
w("=== 5. Studio File -> New ===")
file_menu = None
for act in ui.menuBar().actions():
    if act.text() == "File":
        file_menu = act.menu()
labels = [a.text() for a in file_menu.actions()] if file_menu else []
w("   File menu:", labels)
check("File menu exposes New Project", "New Project" in labels)
check("Save/Load still present", "Save Project" in labels and "Load Project" in labels)

_real_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
try:
    before_blocks = ui.step_tree.topLevelItemCount()
    ui.new_project()
    check("answering No leaves the project alone",
          ui.step_tree.topLevelItemCount() == before_blocks)

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    ui.new_project()
    check("answering Yes resets to a single empty step block",
          ui.step_tree.topLevelItemCount() == 1
          and ui.step_tree.topLevelItem(0).childCount() == 0,
          f"{ui.step_tree.topLevelItemCount()} block(s), "
          f"{ui.step_tree.topLevelItem(0).childCount()} child(ren)")
    check("canvases cleared", not ui.all_canvas_items(),
          f"{len(ui.all_canvas_items())} items left")
    check("left in Design Mode", ui.in_design_mode and not ui.is_running)
finally:
    QMessageBox.question = _real_question

# ===========================================================================
w("")
w("=== live QSettings namespace untouched ===")
_after = QSettings("LightGuide", "Observatory")
settings_after = {k: _after.value(k) for k in _after.allKeys()}
changed = [k for k in set(SETTINGS_BEFORE) | set(settings_after)
           if SETTINGS_BEFORE.get(k) != settings_after.get(k)]
check("no live setting was written by this test", not changed, f"changed={changed}")

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
