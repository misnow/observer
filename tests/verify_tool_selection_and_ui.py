"""Verification for the reported Observatory/Studio UI defects.

  1. With one tool left - the usual state after deleting the others - clicking
     it did not show its properties. itemSelectionChanged only fires when the
     selection CHANGES, and Qt auto-selects a neighbouring row after takeItem,
     so the survivor was already selected and the click emitted nothing.
  2. No way to see which tools are triggering without selecting each in turn.
  3. Blob "Motion" mode could never work: the Capture Reference button existed
     only on Motion Detection tools, so a Blob tool had no way to obtain one.
  4. A projected canvas could be panned/scrolled, silently shifting every
     graphic away from the thing it was aligned to.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_tool_selection_and_ui.py
"""
import os
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_tool_selection_and_ui.log")
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


ISO = tempfile.mkdtemp(prefix="lg_sel_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication, QGraphicsView  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import main as om  # noqa: E402
import studio  # noqa: E402

obs = om.ObservatoryEngine(logger=lambda m: None)


def add(tool_type, x=300, y=220):
    obs.tool_selector.setCurrentText(tool_type)
    obs.spawn_tool_roi(x, y)
    return [i for i in obs.cam_scene.items()
            if getattr(i, 'tool_type', None) == tool_type][-1]


from PyQt6.QtWidgets import QSlider, QComboBox  # noqa: E402


def panel_is_populated():
    """Does the panel actually show a tool, rather than the empty message?

    Looks for controls that only a real tool panel builds. QFormLayout's
    rowCount() is not a reliable proxy - rows removed with takeAt() are gone
    from layout management but the count does not settle until the pending
    deleteLater()s are serviced, so it reads as populated when it is not.
    """
    live = [c for c in obs.tool_prop_group.findChildren((QSlider, QComboBox))
            if c.isVisible() or c.parent() is not None]
    return len(live) > 0


# =====================================================================
w(">>> 1. selecting the LAST REMAINING tool <<<")
t1 = add("Blob Detection", 250, 200)
t2 = add("Motion Detection", 450, 300)
app.processEvents()
check("two tools created", obs.active_tools_list.count() == 2,
      str(obs.active_tools_list.count()))

# Delete one, exactly as the button does.
obs.active_tools_list.setCurrentRow(0)
obs.delete_selected_tool()
app.processEvents()
check("one tool remains", obs.active_tools_list.count() == 1,
      str(obs.active_tools_list.count()))
check("the panel was cleared by the delete", not panel_is_populated())
check("no row is left selected (Qt auto-selects one after takeItem)",
      not obs.active_tools_list.selectedItems(),
      "a stale selection is what made the next click a no-op")

# Now click the survivor - the exact reported action.
row = obs.active_tools_list.item(0)
obs.select_tool_from_list(row)
app.processEvents()
check(">>> clicking the last remaining tool SHOWS its properties <<<",
      panel_is_populated(), "this is the reported bug")

# And clicking it AGAIN, with no selection change at all, must still work.
obs.build_empty_properties()
app.processEvents()
check("precondition: panel cleared again", not panel_is_populated())
obs.select_tool_from_list(row)
app.processEvents()
check("clicking an ALREADY-selected tool still rebuilds the panel",
      panel_is_populated(),
      "itemSelectionChanged would not fire here - itemClicked does")

w("")
w("=== selecting from the ROI in the scene still works ===")
t3 = add("Blob Detection", 600, 400)
app.processEvents()
obs.build_empty_properties()
obs.cam_scene.clearSelection()
t3.setSelected(True)
obs.sync_scene_to_list()
app.processEvents()
check("clicking the ROI populates the panel", panel_is_populated())
check("and highlights the matching row in the list",
      any(obs.active_tools_list.item(i).isSelected()
          for i in range(obs.active_tools_list.count())))

# =====================================================================
w("")
w("=== 2. trigger state visible without selecting ===")
obs.tool_states[t3.tool_name] = True
obs.tool_states[t2.tool_name] = False
obs.refresh_tool_list_indicators()
app.processEvents()

icons = {}
for i in range(obs.active_tools_list.count()):
    r = obs.active_tools_list.item(i)
    roi = r.data(Qt.ItemDataRole.UserRole)
    icons[roi.tool_name] = not r.icon().isNull()
w(f"   rows with an indicator: {icons}")
check("every row carries a trigger indicator", all(icons.values()), str(icons))


def icon_colour(tool):
    for i in range(obs.active_tools_list.count()):
        r = obs.active_tools_list.item(i)
        if r.data(Qt.ItemDataRole.UserRole) is tool:
            img = r.icon().pixmap(14, 14).toImage()
            return img.pixelColor(7, 7).name()
    return ""


fired = icon_colour(t3)
idle = icon_colour(t2)
w(f"   triggered colour {fired}, idle colour {idle}")
check("a triggered tool and an idle one look DIFFERENT", fired != idle,
      "the whole point - trigger state readable at a glance")
check("triggered reads as green", fired.lower().startswith("#2e"), fired)

# =====================================================================
w("")
w("=== 3. Blob Motion mode can obtain a reference frame ===")
blob = t3
blob.blob_thresh_mode = "Motion"
obs.load_tool_properties_to_ui(blob)
app.processEvents()

frame = np.full((480, 640, 3), 60, np.uint8)
obs.current_raw_frame = frame
blob.setPos(60, 40)
blob.update_size(400, 300)
check("precondition: no reference yet", blob.reference_frame is None)
obs.set_motion_reference(blob)
app.processEvents()
check(">>> a Blob tool can now train a reference frame <<<",
      blob.reference_frame is not None,
      "previously only Motion Detection tools could, so Blob Motion mode "
      "could never detect anything")

# and auto-capture covers blob tools too
blob2 = add("Blob Detection", 200, 200)
blob2.auto_capture_reference = True
blob2.has_auto_captured = False
blob2.setPos(50, 50)
blob2.update_size(200, 200)


class Cam:
    def isOpened(self):
        return True

    def read(self):
        return True, frame.copy()

    def release(self):
        pass


obs.camera = Cam()
obs.update_frame()
app.processEvents()
check("auto-capture on camera start now covers Blob tools",
      blob2.reference_frame is not None)

# =====================================================================
w("")
w("=== 4. a projected canvas cannot be panned ===")
canvas = studio.ProjectorCanvas()
view = canvas.view
check("no horizontal scrollbar",
      view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
check("no vertical scrollbar",
      view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
check("no drag-to-scroll", view.dragMode() == QGraphicsView.DragMode.NoDrag)
check("view is non-interactive (a projection is not a document)",
      view.isInteractive() is False)
check("the whole 1920x1080 scene is mapped to the window",
      view.sceneRect().width() == 1920 and view.sceneRect().height() == 1080,
      f"{view.sceneRect().width()}x{view.sceneRect().height()}")
check("fit_scene exists for re-fitting on a different projector",
      callable(getattr(canvas, "fit_scene", None)))

# =====================================================================
w("")
w("=== 5. LLM Call can have NO output canvas ===")
import canvas_items  # noqa: E402

studio.AuthoringInterface.launch_observatory = lambda self: None
ui = studio.AuthoringInterface(studio.ProjectorCanvas())
ui.add_new_step_node()
llm = canvas_items.InteractiveLLMTextItem()
ui.insert_asset(llm, "LLM")
app.processEvents()
check("starts on canvas 0", llm.output_canvas == 0 and llm.scene() is not None)

ui.assign_asset_to_canvas(llm, -1)
app.processEvents()
check("assigning None removes it from every canvas",
      llm.output_canvas == -1 and llm.scene() is None,
      "so $ACTIVE can gate a step without drawing anything")

check("activation defaults are present",
      hasattr(llm, "activation_enabled") and llm.activation_enabled is False
      and llm.recapture_seconds == 10.0 and llm.failsafe_seconds == 60.0)

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
