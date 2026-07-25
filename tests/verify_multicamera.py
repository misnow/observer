import sys, os, json
result_path = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\result_multicamera.txt"
log = open(result_path, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

PROJECT_DIR = r"C:\Users\mikes\PycharmProjects\lightproject"
SCRATCH = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\multicam_isolated"
os.makedirs(SCRATCH, exist_ok=True)
sys.path.insert(0, PROJECT_DIR)
os.chdir(SCRATCH)

import numpy as np, cv2
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
import main as observatory_main
from vision_tools import SearchROI

obs = observatory_main.ObservatoryEngine(logger=lambda m: w("[OBS]", m))

# This machine only enumerates camera 0, so add a second/third entry by hand
# to exercise multi-camera behaviour.
obs.camera_selector.blockSignals(True)
obs.camera_selector.clear()
for i in (0, 1, 2):
    obs.camera_selector.addItem(f"Camera {i}", i)
obs.camera_selector.setCurrentIndex(0)
obs.camera_selector.blockSignals(False)
w("Camera dropdown entries:", obs.camera_selector.count())

# --- New tools inherit the camera currently being viewed ---
obs.tool_selector.setCurrentText("Motion Detection")
obs.spawn_tool_roi(300, 300)
cam0_tool = obs.find_tool_by_name("tool01_Moti")
w("Tool spawned on camera 0 -> camera_index:", cam0_tool.camera_index)
assert cam0_tool.camera_index == 0

obs.camera_selector.setCurrentIndex(1)   # now viewing camera 1
obs.tool_selector.setCurrentText("Blob Detection")
obs.spawn_tool_roi(300, 300)
cam1_tool = obs.find_tool_by_name("tool02_Blob")
w("Tool spawned while viewing camera 1 -> camera_index:", cam1_tool.camera_index)
assert cam1_tool.camera_index == 1

# --- Visibility follows the selected camera ---
obs.sync_tool_visibility_to_camera()
w("Viewing cam1: cam0_tool visible=", cam0_tool.isVisible(), "cam1_tool visible=", cam1_tool.isVisible())
assert cam0_tool.isVisible() is False
assert cam1_tool.isVisible() is True
w("Tool list shows only cam1's tools:", [obs.active_tools_list.item(i).text()
                                          for i in range(obs.active_tools_list.count())])
assert obs.active_tools_list.count() == 1

obs.camera_selector.setCurrentIndex(0)   # back to camera 0
w("Viewing cam0: cam0_tool visible=", cam0_tool.isVisible(), "cam1_tool visible=", cam1_tool.isVisible())
assert cam0_tool.isVisible() is True
assert cam1_tool.isVisible() is False
assert obs.active_tools_list.count() == 1

# --- Off-camera tools are NOT evaluated against the wrong camera's frame ---
frame = np.zeros((480, 640, 3), dtype=np.uint8)
cv2.circle(frame, (320, 320), 40, (255, 255, 255), -1)
class FakeCamera:
    def isOpened(self): return True
    def read(self): return True, frame.copy()
obs.camera = FakeCamera()
cam1_tool.blob_color = 255; cam1_tool.threshold = 5; cam1_tool.sensitivity = 10
obs.tool_states["tool02_Blob"] = False
obs.update_frame()   # viewing camera 0; the blob tool belongs to camera 1
w("After frame on cam0, cam1's blob tool state:", obs.tool_states.get("tool02_Blob"))
assert obs.tool_states.get("tool02_Blob") is False, "camera-1 tool must not evaluate camera-0 frames"

# --- Reassigning a tool's camera moves it ---
obs.reassign_tool_camera(cam1_tool, 0)
obs.sync_tool_visibility_to_camera()
w("After reassigning cam1_tool -> camera 0: visible=", cam1_tool.isVisible(),
  "list count=", obs.active_tools_list.count())
assert cam1_tool.camera_index == 0
assert cam1_tool.isVisible() is True
assert obs.active_tools_list.count() == 2
obs.reassign_tool_camera(cam1_tool, 1)   # put it back
obs.sync_tool_visibility_to_camera()

# --- Save / load round trip preserves per-tool camera ---
save_path = os.path.join(SCRATCH, "multicam.json")
obs.prompt_save_file = lambda *a, **k: save_path
obs.save_project()
with open(save_path) as f:
    saved = json.load(f)
w("Saved camera_index per tool:", {t["name"]: t.get("camera_index") for t in saved["tools"]})
assert {t["name"]: t["camera_index"] for t in saved["tools"]} == {"tool01_Moti": 0, "tool02_Blob": 1}

obs2 = observatory_main.ObservatoryEngine(logger=lambda m: None)
obs2.camera_selector.blockSignals(True)
obs2.camera_selector.clear()
for i in (0, 1, 2):
    obs2.camera_selector.addItem(f"Camera {i}", i)
obs2.camera_selector.setCurrentIndex(0)
obs2.camera_selector.blockSignals(False)
obs2.load_project(save_path)
l0 = obs2.find_tool_by_name("tool01_Moti"); l1 = obs2.find_tool_by_name("tool02_Blob")
w("Loaded camera indices:", l0.camera_index, l1.camera_index,
  "| visible on cam0:", l0.isVisible(), l1.isVisible(),
  "| list count:", obs2.active_tools_list.count())
assert l0.camera_index == 0 and l1.camera_index == 1
assert l0.isVisible() is True and l1.isVisible() is False
assert obs2.active_tools_list.count() == 1

# --- Backward compatibility: a project with no camera_index loads as cam 0 ---
legacy = {"camera_index": 0, "world_matrix_b64": None, "tools": [
    {"name": "legacy01_Moti", "type": "Motion Detection", "x": 10.0, "y": 10.0,
     "w": 100.0, "h": 100.0, "sensitivity": 50, "threshold": 127, "min_area": 100,
     "use_thresh": False, "filter_thresh": 127, "use_edge": False, "filter_edge": 100,
     "use_depth": False, "filter_depth": 5, "blob_color": 0, "blob_max_area": 5000,
     "auto_capture_reference": False, "raw_ref_frame_b64": None, "raw_template_b64": None}]}
legacy_path = os.path.join(SCRATCH, "legacy.json")
with open(legacy_path, "w") as f:
    json.dump(legacy, f)
obs3 = observatory_main.ObservatoryEngine(logger=lambda m: None)
obs3.load_project(legacy_path)
lt = obs3.find_tool_by_name("legacy01_Moti")
w("Legacy tool (no camera_index in file) ->", lt.camera_index, "visible:", lt.isVisible())
assert lt.camera_index == 0 and lt.isVisible() is True

# --- New Project clears EVERY camera's tools, not just the visible ones ---
obs.camera_selector.setCurrentIndex(0)
obs.sync_tool_visibility_to_camera()
before = len([i for i in obs.cam_scene.items() if hasattr(i, 'tool_type')])
observatory_main.QMessageBox.question = staticmethod(
    lambda *a, **k: observatory_main.QMessageBox.StandardButton.Yes)
obs.new_project()
after = [i for i in obs.cam_scene.items() if hasattr(i, 'tool_type')]
w("New Project: tools in scene before=", before, "after=", len(after),
  "| list count=", obs.active_tools_list.count())
assert before == 2, f"expected 2 tools across 2 cameras, got {before}"
assert len(after) == 0, "New Project must clear tools on ALL cameras"
assert obs.active_tools_list.count() == 0

w("ALL MULTI-CAMERA CHECKS PASSED")
log.close()
sys.exit(0)
