import sys, os, json, time
import os
import tempfile as _tempfile
# Portable paths. These were hardcoded to a session scratchpad that no longer
# exists and to one machine's project directory, so the script died on
# `open()` before it ran a single check - and would never run at all on a
# fresh clone. Derived from __file__ and the system temp dir instead.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST_TMP = os.path.join(_tempfile.gettempdir(), "lightguide_tests")
os.makedirs(_TEST_TMP, exist_ok=True)
result_path = os.path.join(_TEST_TMP, "result_model3d.txt")
log = open(result_path, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

PROJECT_DIR = _PROJECT_ROOT
SCRATCH = _TEST_TMP
ISO = os.path.join(SCRATCH, "m3d_isolated")
os.makedirs(ISO, exist_ok=True)
sys.path.insert(0, PROJECT_DIR)
os.chdir(ISO)

import numpy as np
from PyQt6.QtCore import Qt, QCoreApplication, QPointF
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication, QStyleOptionGraphicsItem
from PyQt6.QtGui import QImage, QPainter
app = QApplication(sys.argv)
import studio

studio.AuthoringInterface.launch_observatory = lambda self: None
canvas = studio.ProjectorCanvas()
a = studio.AuthoringInterface(canvas)

STL = os.path.join(SCRATCH, "m_box.stl")
STEP = os.path.join(SCRATCH, "test_box.step")

# Generate the fixtures instead of assuming they are lying around. They used to
# be leftovers in a session scratchpad, so this script failed on a machine that
# had never produced them - which is every fresh clone.
if not os.path.exists(STL) or not os.path.exists(STEP):
    import trimesh as _trimesh
    _box = _trimesh.creation.box(extents=(40.0, 30.0, 20.0))
    _box.export(STL)
    if not os.path.exists(STEP):
        import mesh_to_step as _m2s
        _m2s.mesh_to_step(_box.vertices, _box.faces, STEP)
    w("fixtures: generated %s and %s" % (os.path.basename(STL),
                                         os.path.basename(STEP)))

# --- Insert empty, as the button does ---
a.insert_3d_model()
item = next(i for i in a.all_canvas_items() if isinstance(i, studio.Interactive3DModelItem))
w("Inserted empty 3D item | faces:", item.face_count(), "| loading:", item.is_loading)
assert item.face_count() == 0

def wait_load(it, timeout=180):
    t0 = time.time()
    while (it.is_loading or (it.faces is None and not it.load_error)) and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.02)
    return time.time() - t0

# --- Load STL asynchronously ---
item.load_model_async(STL)
w("is_loading immediately after request:", item.is_loading, "(must be True - load is off the UI thread)")
assert item.is_loading is True
dt = wait_load(item)
w("STL loaded in %.2fs | faces:" % dt, item.face_count(), "| error:", repr(item.load_error))
assert item.load_error == ""
assert item.face_count() == 12

# --- It actually renders pixels ---
def render(it):
    img = QImage(int(it.rect().width()), int(it.rect().height()), QImage.Format.Format_RGB32)
    img.fill(0)
    p = QPainter(img)
    it.paint(p, QStyleOptionGraphicsItem(), None)
    p.end()
    stride = img.bytesPerLine(); ptr = img.constBits(); ptr.setsize(img.height()*stride)
    arr = np.frombuffer(bytes(ptr), np.uint8).reshape(img.height(), stride//4, 4)
    return arr[:, :img.width(), :3]

px = render(item)
lit = int(np.count_nonzero(px.sum(axis=2) > 60))
w("Rendered non-background pixels:", lit)
assert lit > 500, "model should actually paint geometry, not an empty box"

# --- Rotation changes what's drawn ---
before = render(item).copy()
item.set_rotation_3d(rx=75, ry=110, rz=25)
after = render(item)
w("Rotation changes rendered image:", not np.array_equal(before, after))
assert not np.array_equal(before, after)
w("Angles:", item.rot_x, item.rot_y, item.rot_z)
assert (item.rot_x, item.rot_y, item.rot_z) == (75.0, 110.0, 25.0)

# --- Scale + translate ---
item.set_model_scale(2.0); item.set_model_translation(tx=40, ty=-25)
w("scale:", item.model_scale, "translate:", item.model_tx, item.model_ty)
assert item.model_scale == 2.0 and item.model_tx == 40.0 and item.model_ty == -25.0
item.set_model_scale(0.001)   # clamped
w("scale clamped low ->", item.model_scale)
assert item.model_scale >= 0.05

# --- Left-drag orbits, and pivots about the grab point ---
item.reset_view()
r0 = (item.rot_x, item.rot_y)
t0_ = (item.model_tx, item.model_ty)

class FakeEv:
    def __init__(self, pos, btn=Qt.MouseButton.LeftButton):
        self._p, self._b = pos, btn
    def pos(self): return self._p
    def button(self): return self._b
    def accept(self): pass

item.mousePressEvent(FakeEv(QPointF(120, 90)))
item.mouseMoveEvent(FakeEv(QPointF(170, 130)))
item.mouseReleaseEvent(FakeEv(QPointF(170, 130)))
w("after drag rot:", round(item.rot_x,1), round(item.rot_y,1),
  "| translation compensated:", round(item.model_tx,2), round(item.model_ty,2))
assert (item.rot_x, item.rot_y) != r0, "left-drag must orbit"
assert (item.model_tx, item.model_ty) != t0_, "pivot compensation must shift translation"

# --- STEP loads through the same path ---
item2 = studio.Interactive3DModelItem()
a.insert_asset(item2, "3D Model")
item2.load_model_async(STEP)
dt = wait_load(item2)
w("STEP loaded in %.2fs | faces:" % dt, item2.face_count(), "| error:", repr(item2.load_error))
assert item2.load_error == ""
assert item2.face_count() == 12

# --- Missing file surfaces an error instead of crashing ---
item3 = studio.Interactive3DModelItem()
a.insert_asset(item3, "3D Model")
item3.load_model_async(os.path.join(ISO, "nope.stl"))
wait_load(item3, timeout=30)
w("missing file error:", item3.load_error[:60])
assert item3.load_error != ""
render(item3)   # must still paint (the error message) without raising

# --- Save / load round trip ---
item.set_rotation_3d(rx=33, ry=44, rz=55)
item.set_model_scale(1.75); item.set_model_translation(tx=12, ty=-8)
item.filepath = STL
save_path = os.path.join(ISO, "m3d.json")
a.prompt_save_file = lambda *x, **k: save_path
a.save_project()
entry = next(c for b_ in json.load(open(save_path)) for c in b_["children"]
             if c["type"] == "Interactive3DModelItem" and c["filepath"].endswith("m_box.stl"))
w("saved pose:", entry["rot_x"], entry["rot_y"], entry["rot_z"],
  entry["model_scale"], entry["model_tx"], entry["model_ty"])
assert (entry["rot_x"], entry["rot_y"], entry["rot_z"]) == (33.0, 44.0, 55.0)
assert entry["model_scale"] == 1.75

canvas2 = studio.ProjectorCanvas()
b = studio.AuthoringInterface(canvas2)
b.prompt_open_file = lambda *x, **k: save_path
b.new_file(); b.load_project()
loaded = next(i for i in b.all_canvas_items()
              if isinstance(i, studio.Interactive3DModelItem) and i.filepath.endswith("m_box.stl"))
w("loaded pose:", loaded.rot_x, loaded.rot_y, loaded.rot_z, loaded.model_scale,
  loaded.model_tx, loaded.model_ty)
assert (loaded.rot_x, loaded.rot_y, loaded.rot_z) == (33.0, 44.0, 55.0)
assert loaded.model_scale == 1.75
assert (loaded.model_tx, loaded.model_ty) == (12.0, -8.0)
wait_load(loaded)
w("geometry reloaded from disk on project load | faces:", loaded.face_count())
assert loaded.face_count() == 12

w("ALL 3D MODEL CHECKS PASSED")
log.close()
sys.exit(0)
