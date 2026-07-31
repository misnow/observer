"""Verification for the Timer node's optional on-canvas countdown.

The Timer step has always been invisible. It can now show a countdown on an
output canvas, movable and scalable like any other asset.

The property that matters: the sequence engine stays the ONLY clock. The
canvas item just displays what it is told, so the projected number can never
disagree with the step actually running.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_timer_on_canvas.py
"""
import os
import sys
import json
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_timer_on_canvas.log")
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


ISO = tempfile.mkdtemp(prefix="lg_timer_")
os.chdir(ISO)

from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import canvas_items  # noqa: E402
import studio  # noqa: E402

# =====================================================================
w("=== the canvas item itself ===")
item = canvas_items.InteractiveTimerItem(12.0)
check("starts showing the full duration", item.remaining == 12.0)
check("formats seconds with one decimal", item.format_time(4.25) == "4.2",
      item.format_time(4.25))
item.set_time_format("MM:SS")
check("MM:SS format", item.format_time(125) == "02:05", item.format_time(125))
check("MM:SS pads correctly", item.format_time(5) == "00:05", item.format_time(5))
item.set_time_format("Seconds")
check("never shows a negative time", item.format_time(-3) == "0.0",
      item.format_time(-3))

item.set_remaining(7.5)
check("set_remaining updates the visible text", item.label.toPlainText() == "7.5",
      item.label.toPlainText())
item.set_remaining(-2)
check("clamped at zero", item.remaining == 0.0 and item.label.toPlainText() == "0.0")

w("")
w("=== moveable and scalable, like other assets ===")
check("is movable", bool(item.flags() & canvas_items.QGraphicsItem
                         .GraphicsItemFlag.ItemIsMovable))
check("is selectable", bool(item.flags() & canvas_items.QGraphicsItem
                            .GraphicsItemFlag.ItemIsSelectable))
check("has a corner resize handle like the other assets",
      isinstance(item.handle, canvas_items.MediaResizeHandle))
check("uses the shared animation mixin",
      isinstance(item, canvas_items.AnimatableMixin))

before_font = item.current_font_size
item.resize_by_drag(640, 320)
check("dragging the corner resizes the box",
      item.rect().width() == 640 and item.rect().height() == 320,
      f"{item.rect().width()}x{item.rect().height()}")
check("the digits scale WITH the box", item.current_font_size > before_font,
      f"{before_font} -> {item.current_font_size}")
check("the handle follows the new corner",
      abs(item.handle.pos().x() - 634) < 1 and abs(item.handle.pos().y() - 314) < 1,
      f"{item.handle.pos()}")
item.resize_by_drag(10, 10)
check("resize clamps to a sane minimum",
      item.rect().width() >= 120 and item.rect().height() >= 60,
      f"{item.rect().width()}x{item.rect().height()}")

item.set_show_background(False)
check("background can be turned off (for projecting over live imagery)",
      item.show_background is False)
item.reset_to_default()
check("reset_to_default restores size, font and colour",
      item.rect().width() == 320 and item.current_font_size == 64
      and item.show_background is True)

# =====================================================================
w("")
w("=== wired into Studio ===")
studio.AuthoringInterface.launch_observatory = lambda self: None
ui = studio.AuthoringInterface(studio.ProjectorCanvas())
ui.add_new_step_node()
app.processEvents()

ui.insert_timer()
timers = []
for i in range(ui.step_tree.topLevelItemCount()):
    top = ui.step_tree.topLevelItem(i)
    for j in range(top.childCount()):
        a = top.child(j).data(1, Qt.ItemDataRole.UserRole)
        if isinstance(a, studio.TimerData):
            timers.append(a)
check("a Timer node was inserted", len(timers) == 1)
node = timers[0]
check("display is OFF by default (unchanged behaviour)",
      node.show_on_canvas is False and node.display_item is None)

def canvas_timer_items():
    return [i for c in ui.canvases for i in c.scene.items()
            if isinstance(i, canvas_items.InteractiveTimerItem)]


check("nothing on the canvas while unchecked", not canvas_timer_items())

ui.set_timer_on_canvas(node, True)
app.processEvents()
check("ticking the box puts a countdown on the canvas",
      node.display_item is not None and len(canvas_timer_items()) == 1)
check("it shows the node's duration", node.display_item.remaining == node.duration,
      f"{node.display_item.remaining} vs {node.duration}")

ui.set_timer_on_canvas(node, False)
app.processEvents()
check("unticking removes it from the canvas",
      node.display_item is None and not canvas_timer_items())

ui.set_timer_on_canvas(node, True)
app.processEvents()

# =====================================================================
w("")
w("=== the ENGINE owns the clock; the item only mirrors it ===")
ui.execution_sequence = ui.build_execution_sequence()
ui.sync_timer_displays(remaining=3.25, running=True)
check("engine countdown reaches the canvas item",
      node.display_item.remaining == 3.25, str(node.display_item.remaining))
check("and is rendered", node.display_item.label.toPlainText() == "3.2",
      node.display_item.label.toPlainText())
ui.sync_timer_displays(remaining=0.0, running=True)
check("hitting zero displays 0.0", node.display_item.label.toPlainText() == "0.0")
ui.sync_timer_displays(running=False)
check("when not running it shows the configured duration, not a stale value",
      node.display_item.remaining == node.duration, str(node.display_item.remaining))

# a dead display item must not break the tick
node.display_item.scene().removeItem(node.display_item)
node.display_item = None
try:
    ui.sync_timer_displays(remaining=1.0, running=True)
    check("a removed display item does not break the engine tick", True)
except Exception as e:
    check("a removed display item does not break the engine tick", False,
          f"{type(e).__name__}: {e}")

# =====================================================================
w("")
w("=== survives a save/load round trip ===")
ui.set_timer_on_canvas(node, True)
node.display_item.setPos(410, 265)
node.display_item.resize_by_drag(480, 240)
node.display_item.set_font_size(96)
node.display_item.set_text_color(QColor("#e74c3c"))
node.display_item.set_time_format("MM:SS")
node.display_item.set_show_background(False)
node.duration = 42.0

proj = os.path.join(ISO, "timer.json")
ui.prompt_save_file = lambda *a, **k: proj
ui.prompt_open_file = lambda *a, **k: proj
ui.save_project()

with open(proj) as f:
    saved = json.load(f)
entry = [c for b in saved for c in b.get("children", []) if c["type"] == "TimerData"][0]
w("   saved: " + json.dumps({k: v for k, v in entry.items() if k != "display"}))
check("show_on_canvas persisted", entry.get("show_on_canvas") is True)
check("display block persisted", isinstance(entry.get("display"), dict),
      str(entry.get("display"))[:90])

ui.load_project()
app.processEvents()
reloaded = []
for i in range(ui.step_tree.topLevelItemCount()):
    top = ui.step_tree.topLevelItem(i)
    for j in range(top.childCount()):
        a = top.child(j).data(1, Qt.ItemDataRole.UserRole)
        if isinstance(a, studio.TimerData):
            reloaded.append(a)
check("timer node reloaded", len(reloaded) == 1)
rt = reloaded[0]
check("duration survives", rt.duration == 42.0, str(rt.duration))
check("show_on_canvas survives", rt.show_on_canvas is True)
check("the countdown is back on the canvas", rt.display_item is not None
      and len(canvas_timer_items()) == 1)
if rt.display_item:
    d = rt.display_item
    check("position restored", abs(d.pos().x() - 410) < 1 and abs(d.pos().y() - 265) < 1,
          f"{d.pos()}")
    check("size restored", d.rect().width() == 480 and d.rect().height() == 240,
          f"{d.rect().width()}x{d.rect().height()}")
    check("font size restored", d.current_font_size == 96, str(d.current_font_size))
    check("colour restored", d.text_color.name() == "#e74c3c", d.text_color.name())
    check("format restored", d.time_format == "MM:SS", d.time_format)
    check("background flag restored", d.show_background is False)

w("")
w("=== a project saved before this feature still loads ===")
legacy = [{"type": "StepBlock", "children": [{"type": "TimerData", "duration": 9.0}]}]
legacy_path = os.path.join(ISO, "legacy.json")
with open(legacy_path, "w") as f:
    json.dump(legacy, f)
ui.prompt_open_file = lambda *a, **k: legacy_path
ui.load_project()
app.processEvents()
old = [top.child(j).data(1, Qt.ItemDataRole.UserRole)
       for i in range(ui.step_tree.topLevelItemCount())
       for top in [ui.step_tree.topLevelItem(i)]
       for j in range(top.childCount())]
old_timers = [a for a in old if isinstance(a, studio.TimerData)]
check("legacy timer loads", len(old_timers) == 1)
if old_timers:
    check("and defaults to not displayed (behaviour unchanged)",
          old_timers[0].show_on_canvas is False
          and old_timers[0].display_item is None)
    check("duration intact", old_timers[0].duration == 9.0)

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
