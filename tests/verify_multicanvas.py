import sys, os, json
import os
import tempfile as _tempfile
# Portable paths. These were hardcoded to a session scratchpad that no longer
# exists and to one machine's project directory, so the script died on
# `open()` before it ran a single check - and would never run at all on a
# fresh clone. Derived from __file__ and the system temp dir instead.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST_TMP = os.path.join(_tempfile.gettempdir(), "lightguide_tests")
os.makedirs(_TEST_TMP, exist_ok=True)
result_path = os.path.join(_TEST_TMP, "result_multicanvas.txt")
log = open(result_path, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

PROJECT_DIR = _PROJECT_ROOT
SCRATCH = os.path.join(_TEST_TMP, "mc_isolated")
os.makedirs(SCRATCH, exist_ok=True)
sys.path.insert(0, PROJECT_DIR)
os.chdir(SCRATCH)

from PyQt6.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
import studio

studio.AuthoringInterface.launch_observatory = lambda self: None
canvas = studio.ProjectorCanvas()
a = studio.AuthoringInterface(canvas)

def visible(wd): return not wd.isHidden()

# --- Default state: 1 canvas, preview picker hidden ---
w("Initial canvases:", len(a.canvases), "| preview picker visible:", visible(a.cb_preview_canvas))
assert len(a.canvases) == 1
assert visible(a.cb_preview_canvas) is False

# --- Grow to 3 canvases ---
a.set_canvas_count(3)
w("After set_canvas_count(3):", len(a.canvases), "| picker visible:", visible(a.cb_preview_canvas),
  "| entries:", a.cb_preview_canvas.count())
assert len(a.canvases) == 3
assert visible(a.cb_preview_canvas) is True
assert a.cb_preview_canvas.count() == 3

# --- Clamped to 1..5 ---
a.set_canvas_count(99); w("set_canvas_count(99) ->", len(a.canvases))
assert len(a.canvases) == 5
a.set_canvas_count(0); w("set_canvas_count(0) ->", len(a.canvases))
assert len(a.canvases) == 1
a.set_canvas_count(3)

# --- Assets land on the chosen canvas ---
t1 = studio.InteractiveTextItem("On canvas 1"); a.insert_asset(t1, "Text")
t2 = studio.InteractiveTextItem("On canvas 2"); a.insert_asset(t2, "Text")
t3 = studio.InteractiveTextItem("On canvas 3"); a.insert_asset(t3, "Text")
a.assign_asset_to_canvas(t2, 1)
a.assign_asset_to_canvas(t3, 2)
w("output_canvas:", t1.output_canvas, t2.output_canvas, t3.output_canvas)
assert (t1.output_canvas, t2.output_canvas, t3.output_canvas) == (0, 1, 2)
assert t2.scene() is a.canvases[1].scene
assert t3.scene() is a.canvases[2].scene

texts = [i for i in a.all_canvas_items() if isinstance(i, studio.InteractiveTextItem)]
w("all_canvas_items text count:", len(texts))
assert len(texts) == 3

# --- Preview mirrors the SELECTED canvas ---
a.cb_preview_canvas.setCurrentIndex(1)
w("preview scene is canvas 2:", a.run_preview_view.scene() is a.canvases[1].scene)
assert a.run_preview_view.scene() is a.canvases[1].scene
a.cb_preview_canvas.setCurrentIndex(2)
assert a.run_preview_view.scene() is a.canvases[2].scene
a.cb_preview_canvas.setCurrentIndex(0)

# --- Clear Canvas targets one canvas, leaving others alone ---
for t in (t1, t2, t3): t.setVisible(True)
cc = studio.ClearCanvasData()
w("ClearCanvasData default target:", cc.output_canvas, "(-1 = all)")
assert cc.output_canvas == -1

a.hide_all_canvas_items(1)
w("After clearing canvas 2 only -> visible:", t1.isVisible(), t2.isVisible(), t3.isVisible())
assert t1.isVisible() is True
assert t2.isVisible() is False
assert t3.isVisible() is True

for t in (t1, t2, t3): t.setVisible(True)
a.hide_all_canvas_items(-1)
w("After clearing ALL -> visible:", t1.isVisible(), t2.isVisible(), t3.isVisible())
assert not any((t1.isVisible(), t2.isVisible(), t3.isVisible()))

# --- Save / load round trips asset canvas AND clear-node target ---
a.insert_clear_canvas()
top = a.step_tree.topLevelItem(0)
clear_node = next(i for i in
                  [top.child(j).data(1, studio.Qt.ItemDataRole.UserRole) for j in range(top.childCount())]
                  if isinstance(i, studio.ClearCanvasData))
clear_node.output_canvas = 2

save_path = os.path.join(SCRATCH, "mc.json")
a.prompt_save_file = lambda *x, **k: save_path
a.save_project()
children = [c for b_ in json.load(open(save_path)) for c in b_["children"]]
saved_canvases = sorted(c.get("output_canvas") for c in children if c["type"] == "InteractiveTextItem")
saved_clear = [c.get("output_canvas") for c in children if c["type"] == "ClearCanvasData"]
w("saved text output_canvas:", saved_canvases, "| saved clear target:", saved_clear)
assert saved_canvases == [0, 1, 2]
assert saved_clear == [2]

canvas2 = studio.ProjectorCanvas()
b = studio.AuthoringInterface(canvas2)
b.prompt_open_file = lambda *x, **k: save_path
b.new_file(); b.load_project()
w("canvases auto-grown on load:", len(b.canvases))
assert len(b.canvases) >= 3

loaded_texts = sorted((i.toPlainText(), i.output_canvas)
                      for i in b.all_canvas_items() if isinstance(i, studio.InteractiveTextItem))
w("loaded texts:", loaded_texts)
assert loaded_texts == [("On canvas 1", 0), ("On canvas 2", 1), ("On canvas 3", 2)]
for txt, idx in loaded_texts:
    item = next(i for i in b.all_canvas_items()
                if isinstance(i, studio.InteractiveTextItem) and i.toPlainText() == txt)
    assert item.scene() is b.canvases[idx].scene, f"{txt} not in canvas {idx+1} scene"

top_b = b.step_tree.topLevelItem(0)
loaded_clear = next(i for i in
                    [top_b.child(j).data(1, studio.Qt.ItemDataRole.UserRole) for j in range(top_b.childCount())]
                    if isinstance(i, studio.ClearCanvasData))
w("loaded clear target:", loaded_clear.output_canvas)
assert loaded_clear.output_canvas == 2

# --- Shrinking rescues assets instead of orphaning them ---
b.set_canvas_count(1)
survivors = sorted((i.toPlainText(), i.output_canvas)
                   for i in b.all_canvas_items() if isinstance(i, studio.InteractiveTextItem))
w("after shrink to 1 canvas:", survivors)
assert len(survivors) == 3, "assets on removed canvases must be rescued, not lost"
assert all(idx == 0 for _, idx in survivors)
assert visible(b.cb_preview_canvas) is False

w("ALL MULTI-CANVAS CHECKS PASSED")
log.close()
sys.exit(0)
