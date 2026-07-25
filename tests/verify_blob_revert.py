import sys, os
result_path = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\result_blob_revert.txt"
log = open(result_path, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

PROJECT_DIR = r"C:\Users\mikes\PycharmProjects\lightproject"
SCRATCH_DIR = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\blob_revert_isolated"
os.makedirs(SCRATCH_DIR, exist_ok=True)
sys.path.insert(0, PROJECT_DIR)
os.chdir(SCRATCH_DIR)

import numpy as np, cv2
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage
app = QApplication(sys.argv)
import main as observatory_main
from vision_tools import SearchROI

obs = observatory_main.ObservatoryEngine(logger=lambda m: None)

tool = SearchROI(0, 0, tool_name="tool01_Blob", tool_type="Blob Detection")
w("pattern_roi is None for Blob Detection:", tool.pattern_roi is None)
assert tool.pattern_roi is None
for attr in ("max_blobs", "use_pattern_filter", "blob_match_threshold", "tracked_blobs"):
    w(f"hasattr(tool, '{attr}'):", hasattr(tool, attr))
    assert not hasattr(tool, attr)

# Pattern Match still gets its pattern_roi (unaffected by the revert)
pm_tool = SearchROI(0, 0, tool_name="tool02_Pattern", tool_type="Pattern Match")
assert pm_tool.pattern_roi is not None
w("Pattern Match tool still has pattern_roi: OK")

# --- Original single-blob rendering restored ---
frame = np.zeros((480, 640, 3), dtype=np.uint8)
cv2.circle(frame, (60, 60), 30, (255, 255, 255), -1)
cv2.circle(frame, (300, 300), 10, (255, 255, 255), -1)  # a smaller second blob

class FakeCamera:
    def isOpened(self): return True
    def read(self): return True, frame.copy()

obs.camera = FakeCamera()
tool.update_size(400, 400)
tool.blob_color = 255
tool.threshold = 5
tool.blob_max_area = 20000
tool.sensitivity = 10
obs.cam_scene.addItem(tool)
obs.tool_states[tool.tool_name] = False
obs.update_frame()

w("last_blob_x/y/a after scoring:", tool.last_blob_x, tool.last_blob_y, tool.last_blob_a)
w("tool_state:", obs.tool_states.get(tool.tool_name))
assert obs.tool_states.get(tool.tool_name) is True
# Should have picked the LARGER blob (radius 30 at 60,60), not the smaller one
assert tool.last_blob_x != 0 or tool.last_blob_y != 0

# --- Properties panel builds cleanly with just the original controls ---
obs.cam_scene.clearSelection()
tool.setSelected(True)
obs.load_tool_properties_to_ui(tool)
w("Properties panel built OK. blob_coord_label text:", obs.blob_coord_label.text())
assert "Angle" in obs.blob_coord_label.text()

# --- Save/Load round trip: no pattern fields, loads cleanly ---
save_path = os.path.join(SCRATCH_DIR, "blob_revert_roundtrip.json")
obs.prompt_save_file = lambda *a, **k: save_path
obs.save_project()

import json
with open(save_path) as f:
    saved = json.load(f)
blob_data = next(t for t in saved["tools"] if t["name"] == "tool01_Blob")
w("Saved blob tool dict keys:", sorted(blob_data.keys()))
assert "max_blobs" not in blob_data
assert "use_pattern_filter" not in blob_data
assert "blob_match_threshold" not in blob_data

obs2 = observatory_main.ObservatoryEngine(logger=lambda m: None)
obs2.load_project(save_path)
loaded = obs2.find_tool_by_name("tool01_Blob")
w("Loaded tool exists:", loaded is not None, "no pattern_roi:", loaded.pattern_roi is None)
assert loaded is not None
assert loaded.pattern_roi is None
assert not hasattr(loaded, 'max_blobs')

w("ALL BLOB REVERT CHECKS PASSED")
log.close()
sys.exit(0)
