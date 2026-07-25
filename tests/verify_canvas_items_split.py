"""Regression test for the studio.py -> canvas_items.py split.

Static checks can't catch this project's characteristic failure: an exception
inside a Qt-invoked slot becomes a 0xC0000409 abort with no traceback. So this
drives the real code paths - real insert_asset calls, a real save/load round
trip through the real serializer - rather than asserting on imports.

Follows the house test pattern (see tests/README.md):
  - runs under the 3.12 runtime interpreter
  - chdir()s to an isolated temp dir so studio_command.json / vision_state.json
    never collide with the user's live session
  - stubs launch_observatory BEFORE constructing AuthoringInterface, or a real
    Observatory subprocess spawns
  - never touches QSettings("LightGuide", "Observatory") - the live namespace
  - writes results to a log file, not stdout (encoding + pipe issues)

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_canvas_items_split.py
"""
import os
import sys
import json
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_canvas_items_split.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


workdir = tempfile.mkdtemp(prefix="lg_split_")
os.chdir(workdir)

from PyQt6.QtCore import QCoreApplication, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

import canvas_items  # noqa: E402
import studio  # noqa: E402

w("=== module identity ===")
w("  cwd:", workdir)

# 1. The names studio has always exported must still resolve through studio,
#    and must be the *same objects* as canvas_items' - not stale copies.
MOVED = ["TriggerSettings", "AnimatableMixin", "InteractiveTextItem", "MediaResizeHandle",
         "InteractiveMediaItem", "InteractiveShapeItem", "Interactive3DModelItem",
         "InteractiveCaptureItem", "InteractiveHTMLItem", "InteractiveLLMTextItem"]
for name in MOVED:
    a, b = getattr(studio, name, None), getattr(canvas_items, name, None)
    check(f"studio.{name} is canvas_items.{name}", a is not None and a is b)

# 2. canvas_items must not depend on studio (one-way dependency).
check("canvas_items does not import studio",
      "studio" not in sys.modules or
      not any(getattr(v, "__module__", "") == "studio"
              for v in vars(canvas_items).values() if isinstance(v, type)))

app = QApplication(sys.argv)
app.setStyleSheet(studio.DARK_THEME)

# Stub BEFORE constructing, or a real Observatory subprocess spawns and
# starts overwriting vision_state.json.
launched = []
studio.AuthoringInterface.launch_observatory = lambda self: launched.append(1)

w("")
w("=== construct app ===")
canvas = studio.ProjectorCanvas()
ui = studio.AuthoringInterface(canvas)
check("AuthoringInterface constructed", ui is not None)
check("no Observatory subprocess spawned", not launched)

# 3. Insert one of every asset type through the REAL insert_asset path.
w("")
w("=== insert every asset type (real code path) ===")
ui.add_new_step_node()

svg_dir = os.path.join(PROJECT, "shape_library")
svg = ""
if os.path.isdir(svg_dir):
    svgs = [f for f in os.listdir(svg_dir) if f.lower().endswith(".svg")]
    if svgs:
        svg = os.path.join(svg_dir, svgs[0])

specs = [
    ("Text", lambda: canvas_items.InteractiveTextItem("Hello")),
    ("Media", lambda: canvas_items.InteractiveMediaItem("nonexistent.png", "Image")),
    ("Shape", lambda: canvas_items.InteractiveShapeItem("Circle")),
    ("ShapeSVG", lambda: canvas_items.InteractiveShapeItem("Custom SVG", svg)),
    ("Model3D", lambda: canvas_items.Interactive3DModelItem()),
    ("Capture", lambda: canvas_items.InteractiveCaptureItem("Capture 1")),
    ("LLM", lambda: canvas_items.InteractiveLLMTextItem()),
    ("HTML", lambda: canvas_items.InteractiveHTMLItem()),
]

made = {}
for label, factory in specs:
    try:
        item = factory()
        ui.insert_asset(item, label)
        made[label] = item
        check(f"insert {label}", True)
    except Exception as exc:
        check(f"insert {label}", False, f"{type(exc).__name__}: {exc}")

# 4. Exercise the moved behaviour itself: animation, scaling, resize, reset.
w("")
w("=== exercise moved behaviour ===")
for label, item in made.items():
    if not isinstance(item, canvas_items.AnimatableMixin):
        continue
    try:
        item.setVisible(True)
        for anim in ("Fade In", "Slide In", "Pulse", "Pan"):
            item.set_animation_type(anim)
            item.set_animation_direction("Left")
            item.set_animation_duration(0.5)
            item.update_animation()
        item.set_lock_aspect_ratio(False)
        item.set_scale_xy(1.5, 0.7)
        item.set_lock_aspect_ratio(True)
        check(f"{label}: animation + scale", abs(item.scale_factor - 1.1) < 1e-9,
              f"scale_factor={item.scale_factor:.4f} (mean of 1.5/0.7)")
        item.resize_by_drag(260, 190)
        item.reset_to_default()
        check(f"{label}: resize + reset_to_default",
              item.scale_factor == 1.0 and item.animation_type == "None")
    except Exception as exc:
        check(f"{label}: behaviour", False, f"{type(exc).__name__}: {exc}")

# curved text is a paint-path branch unique to InteractiveTextItem
try:
    t = made["Text"]
    t.set_curve_radius(180)
    br = t.boundingRect()
    check("curved text boundingRect grows", br.width() > 300, f"w={br.width():.0f}")
    t.set_curve_radius(0)
except Exception as exc:
    check("curved text", False, f"{type(exc).__name__}: {exc}")

# 5. Full save -> load round trip through the real serializer, with the file
#    dialogs stubbed out (the dialog is the only thing we replace).
w("")
w("=== save / load round trip ===")
proj = os.path.join(workdir, "roundtrip.json")
ui.prompt_save_file = lambda *a, **k: proj
ui.prompt_open_file = lambda *a, **k: proj

def assets_in_tree():
    """Every asset across ALL step blocks - not just topLevelItem(0), which
    can legitimately be an empty block and would make the comparison below
    pass vacuously at 0 == 0."""
    out = []
    for i in range(ui.step_tree.topLevelItemCount()):
        top = ui.step_tree.topLevelItem(i)
        for j in range(top.childCount()):
            a = top.child(j).data(1, Qt.ItemDataRole.UserRole)
            if a is not None:
                out.append(a)
    return out


before = [type(a).__name__ for a in assets_in_tree()]
check("assets present before save (guards against a vacuous round trip)",
      len(before) >= 7, f"{len(before)} assets")
try:
    ui.save_project()
    check("save_project wrote file", os.path.exists(proj),
          f"{os.path.getsize(proj)} bytes" if os.path.exists(proj) else "missing")
    with open(proj) as f:
        data = json.load(f)
    saved_types = [c["type"] for b in data for c in b.get("children", [])]
    check("every inserted asset was serialized", len(saved_types) == len(before),
          f"inserted {len(before)}, serialized {len(saved_types)}")
    w("  serialized types:", ", ".join(sorted(set(saved_types))))

    ui.load_project()
    restored_items = assets_in_tree()
    after = [type(a).__name__ for a in restored_items]
    w("  restored types:", ", ".join(sorted(set(after))))
    check("load_project restored every asset", sorted(after) == sorted(before),
          f"{len(before)} -> {len(after)}"
          + (f"  missing={sorted(set(before) - set(after))}" if set(before) - set(after) else ""))
    check("restored items are canvas_items classes",
          len(restored_items) > 0 and
          all(type(a).__module__ == "canvas_items" for a in restored_items),
          f"{len(restored_items)} items")
except Exception as exc:
    check("round trip", False, f"{type(exc).__name__}: {exc}")

# 6. Let the event loop actually spin, then close cleanly. A queued signal
#    landing on a torn-down widget is exactly how this app aborts.
w("")
w("=== event loop + teardown ===")
QTimer.singleShot(600, ui.close)
QTimer.singleShot(700, canvas.close)
QTimer.singleShot(900, app.quit)
app.exec()
check("event loop ran and closed cleanly", True)

w("")
w("=" * 52)
w("RESULT:", "PASS - all checks green" if not failures else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
