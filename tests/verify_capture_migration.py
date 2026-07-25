import sys, os, json, time
RES = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\result_capture_migration.txt"
log = open(RES, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n"); log.flush()

PROJ = r"C:\Users\mikes\PycharmProjects\lightproject"
ISO = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\mig_iso"
os.makedirs(ISO, exist_ok=True)
sys.path.insert(0, PROJ)
os.chdir(ISO)   # shared IPC dir for both apps

import numpy as np, cv2
from PyQt6.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
import studio, main as om

# ---------- Observatory: create an Image Capture tool and capture ----------
obs = om.ObservatoryEngine(logger=lambda m: w("[OBS]", m))
frame = np.full((300, 400, 3), 25, np.uint8)
cv2.circle(frame, (200, 150), 70, (60, 200, 240), -1)
class Cam:
    def isOpened(s): return True
    def read(s): return True, frame.copy()
obs.camera = Cam(); obs.current_raw_frame = frame.copy()

obs.tool_selector.setCurrentText("Image Capture")
obs.spawn_tool_roi(200, 150)
tool = obs.find_tool_by_name("tool01_Imag")
tool.update_size(200, 200); tool.setPos(100, 50)
w("Observatory tool:", tool.tool_name, "|", tool.tool_type, "| mode:", tool.capture_mode)
assert tool.tool_type == "Image Capture"

obs_json = os.path.join(ISO, "obs_proj.json")
obs.prompt_save_file = lambda *a, **k: obs_json
obs.save_project()

# The Studio dropdown reads tool names out of the Observatory file.
# Stub BEFORE constructing, or the real one spawns an Observatory subprocess
# that overwrites vision_state.json out from under this test.
studio.AuthoringInterface.launch_observatory = lambda self: None
a_ = studio.AuthoringInterface(studio.ProjectorCanvas())
names = a_.parse_observatory_file_silent(obs_json)
w("tools visible to Studio:", names)
assert "tool01_Imag" in names

# ---------- Studio: reference that tool ----------
a_.insert_capture_image()
item = next(i for i in a_.all_canvas_items() if isinstance(i, studio.InteractiveCaptureItem))
w("Studio item has no ROI of its own:", not hasattr(item, "roi"))
assert not hasattr(item, "roi"), "the ROI must live in Observatory now"
assert not hasattr(item, "roi_rect_in_image")
item.observatory_file = obs_json
item.source_tool = "tool01_Imag"
w("referencing:", os.path.basename(item.observatory_file), "->", item.source_tool)
assert item.image_path == ""

# ---------- Observatory captures; Studio picks it up over IPC ----------
obs.capture_tool_image(tool)
obs.write_vision_state()
a_.read_live_vision_state()
w("Studio mirrored image:", os.path.basename(item.image_path) if item.image_path else None)
assert item.image_path and os.path.exists(item.image_path)
assert item.rect().width() == 200 and item.rect().height() == 200, "sized to the ROI capture"
first_time = item.last_shown_time
assert first_time > 0

# Same state again -> must NOT reload (timestamp gate)
a_.read_live_vision_state()
assert item.last_shown_time == first_time, "unchanged capture must not reload every tick"
w("no redundant reload on repeat IPC tick: ok")

# A NEW capture -> Studio updates
time.sleep(0.05)
tool.capture_mode = "Full Frame"
obs.capture_tool_image(tool)
obs.write_vision_state()
a_.read_live_vision_state()
w("after full-frame capture, Studio size:", item.rect().width(), "x", item.rect().height())
assert item.last_shown_time > first_time
assert item.rect().width() == 400 and item.rect().height() == 300

# ---------- Studio save/load keeps the reference ----------
sp = os.path.join(ISO, "studio_proj.json")
a_.prompt_save_file = lambda *x, **k: sp
a_.save_project()
entry = next(c for b in json.load(open(sp)) for c in b["children"]
             if c["type"] == "InteractiveCaptureItem")
w("saved:", entry["source_tool"], "|", os.path.basename(entry["observatory_file"]),
  "| roi key absent:", "roi" not in entry)
assert entry["source_tool"] == "tool01_Imag"
assert "roi" not in entry

b_ = studio.AuthoringInterface(studio.ProjectorCanvas())
b_.prompt_open_file = lambda *x, **k: sp
b_.new_file(); b_.load_project()
loaded = next(i for i in b_.all_canvas_items() if isinstance(i, studio.InteractiveCaptureItem))
w("loaded ref:", loaded.source_tool, "| image:", os.path.basename(loaded.image_path))
assert loaded.source_tool == "tool01_Imag"
assert loaded.observatory_file == obs_json
assert loaded.rect().width() == 400

# ---------- Old-format project (had an roi key) still loads ----------
legacy = [{"type": "StepBlock", "children": [
    {"type": "InteractiveCaptureItem", "pos": [50.0, 60.0], "item_name": "Old Capture",
     "observatory_file": obs_json, "image_path": "", "roi": [10, 20, 100, 80],
     "trigger": {"wait_type": "Line", "observatory_file": "", "vision_target": "None",
                 "condition": "Is True"}}]}]
lp = os.path.join(ISO, "legacy.json"); json.dump(legacy, open(lp, "w"))
c_ = studio.AuthoringInterface(studio.ProjectorCanvas())
c_.prompt_open_file = lambda *x, **k: lp
c_.new_file(); c_.load_project()
old = next(i for i in c_.all_canvas_items() if isinstance(i, studio.InteractiveCaptureItem))
w("legacy project loaded:", old.item_name, "| source_tool default:", old.source_tool)
assert old.item_name == "Old Capture" and old.source_tool == "None"

w("ALL CAPTURE MIGRATION CHECKS PASSED")
log.close()
sys.exit(0)
