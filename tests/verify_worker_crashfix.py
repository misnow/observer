import sys, os, time
result_path = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\result_worker_crashfix.txt"
log = open(result_path, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

PROJECT_DIR = r"C:\Users\mikes\PycharmProjects\lightproject"
SCRATCH = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad"
ISO = os.path.join(SCRATCH, "crashfix_isolated"); os.makedirs(ISO, exist_ok=True)
sys.path.insert(0, PROJECT_DIR)
os.chdir(ISO)

import numpy as np, cv2, trimesh
from PyQt6.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
import studio, segmenter

studio.AuthoringInterface.launch_observatory = lambda self: None
a = studio.AuthoringInterface(studio.ProjectorCanvas())

# fixtures
trimesh.creation.icosphere(subdivisions=4, radius=8).export(os.path.join(ISO, "a.stl"))
trimesh.creation.box(extents=(10, 10, 10)).export(os.path.join(ISO, "b.stl"))
img = np.random.default_rng(2).integers(0, 80, (300, 400, 3)).astype(np.uint8)
cv2.circle(img, (200, 150), 60, (60, 200, 240), -1)
cv2.imwrite(os.path.join(ISO, "cap.png"), img)

def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents(); time.sleep(0.01)

# === BUG A: rapid re-load must not drop a running QThread ===============
a.insert_3d_model()
m = next(i for i in a.all_canvas_items() if isinstance(i, studio.Interactive3DModelItem))
m.load_model_async(os.path.join(ISO, "a.stl"))
m.load_model_async(os.path.join(ISO, "b.stl"))   # supersedes while #1 runs
m.load_model_async(os.path.join(ISO, "a.stl"))   # and again
w("workers tracked concurrently:", len(m._loaders), "(must be >1 - none dropped)")
assert len(m._loaders) > 1, "each running worker must be retained, not overwritten"
pump(12)
w("after settle -> faces:", m.face_count(), "| error:", repr(m.load_error),
  "| leftover workers:", len(m._loaders))
assert m.load_error == ""
assert len(m._loaders) == 0, "workers must be retired once finished"
# Last request wins, not whichever finished last
w("final geometry is from the LAST request (a.stl, dense):", m.face_count() > 100)
assert m.face_count() > 100

# === BUG B: worker firing into a DELETED item must not crash ============
a.insert_3d_model()
doomed = [i for i in a.all_canvas_items() if isinstance(i, studio.Interactive3DModelItem)][-1]
doomed.load_model_async(os.path.join(ISO, "a.stl"))
# Delete the item out from under the running loader.
doomed.scene().removeItem(doomed)
w("item removed from scene while its loader is still running")
pump(12)
w("survived worker->deleted-item delivery")

# === BUG C: segmentation reporting into a TORN-DOWN panel ===============
a.insert_capture_image()
cap = next(i for i in a.all_canvas_items() if isinstance(i, studio.InteractiveCaptureItem))
cap.load_capture(os.path.join(ISO, "cap.png"))
cap.roi.setPos(130, 80); cap.roi.resize_roi(140, 140)

# Build the real properties panel so the real button/label widgets exist.
a.active_item = cap
a.build_asset_properties(cap)
app.processEvents()
btn = None
for r in range(a.prop_form.rowCount()):
    it_ = a.prop_form.itemAt(r, a.prop_form.ItemRole.SpanningRole)
    if it_ and it_.widget() and getattr(it_.widget(), "text", lambda: "")() == "✂️ Segment ROI":
        btn = it_.widget(); break
w("found real Segment button:", btn is not None)
assert btn is not None
btn.click()
w("segmentation started | workers:", len(getattr(cap, '_seg_workers', [])))
assert len(cap._seg_workers) == 1

# Immediately tear down the panel (exactly what selecting another item does)
a.clear_layout(a.prop_form)
app.processEvents()
w("properties panel destroyed mid-segmentation")
pump(15)
w("survived segmentation->deleted-widget delivery")
w("result still captured on the ITEM despite dead panel:", bool(cap.cutout_path),
  "->", os.path.basename(cap.cutout_path) if cap.cutout_path else None)
assert cap.cutout_path, "item state must survive a torn-down panel"
assert os.path.exists(cap.cutout_path)
assert len(cap._seg_workers) == 0, "segment workers must be retired"

# === double-click Segment twice quickly =================================
a.active_item = cap
a.build_asset_properties(cap)
app.processEvents()
btn2 = None
for r in range(a.prop_form.rowCount()):
    it_ = a.prop_form.itemAt(r, a.prop_form.ItemRole.SpanningRole)
    if it_ and it_.widget() and getattr(it_.widget(), "text", lambda: "")() == "✂️ Segment ROI":
        btn2 = it_.widget(); break
btn2.click()
btn2.setEnabled(True); btn2.click()      # force a second concurrent run
w("concurrent segment workers:", len(cap._seg_workers))
pump(15)
w("survived concurrent segmentation | leftover:", len(cap._seg_workers))
assert len(cap._seg_workers) == 0

# === close with a worker still in flight ================================
a.insert_3d_model()
m2 = [i for i in a.all_canvas_items() if isinstance(i, studio.Interactive3DModelItem)][-1]
m2.load_model_async(os.path.join(ISO, "a.stl"))
w("closing window with a loader mid-flight...")
a.close()
app.processEvents()
w("closed cleanly with in-flight worker")

w("ALL WORKER-LIFECYCLE CRASH FIXES VERIFIED")
log.close()
sys.exit(0)
