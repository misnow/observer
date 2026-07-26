"""Verification that Studio's properties column scrolls and can't crowd the tree.

The complaint: most of an asset's properties could not be seen - the panel ran
off the bottom of the window instead of scrolling.

Measures ACTUAL PIXEL HEIGHTS and widths with every asset type selected, at the
real window size. Mirrors tests/verify_observatory_layout.py.

Each asset type is measured in a FRESH window. That is not fussiness: Studio
has a pre-existing, timing-dependent native access violation around tearing
down the 3D Model properties panel (see HANDOFF.md - reproduced at ce09819,
before any of the layout work, and it vanishes under sys.settrace). Measuring
one asset per window means this test never performs an asset-to-asset
transition, so it measures layout without tripping over a separate known bug.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_studio_layout.py
"""
import os
import sys
import faulthandler
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_studio_layout.log")
# Streamed and flushed per line. Studio can die with a native access violation,
# and a log written only on a clean exit says nothing about where it died.
_log = open(LOG, "w", encoding="utf-8", buffering=1)
faulthandler.enable(file=_log, all_threads=True)

failures = []


def w(*parts):
    line = " ".join(str(p) for p in parts)
    _log.write(line + "\n")
    _log.flush()
    os.fsync(_log.fileno())


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_slayout_")
os.chdir(ISO)

from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import canvas_items  # noqa: E402
import studio  # noqa: E402

studio.AuthoringInterface.launch_observatory = lambda self: None

svg_dir = os.path.join(PROJECT, "shape_library")
svg = ""
if os.path.isdir(svg_dir):
    svgs = [f for f in os.listdir(svg_dir) if f.lower().endswith(".svg")]
    if svgs:
        svg = os.path.join(svg_dir, svgs[0])

ASSETS = [
    ("Text", lambda: canvas_items.InteractiveTextItem("Hello")),
    ("Image", lambda: canvas_items.InteractiveMediaItem("missing.png", "Image")),
    ("Shape", lambda: canvas_items.InteractiveShapeItem("Circle")),
    ("Shape SVG", lambda: canvas_items.InteractiveShapeItem("Custom SVG", svg)),
    ("Capture", lambda: canvas_items.InteractiveCaptureItem("Cap 1")),
    ("LLM Call", lambda: canvas_items.InteractiveLLMTextItem()),
    ("3D Model", lambda: canvas_items.Interactive3DModelItem()),
]

# HTML is deliberately EXCLUDED, and this is a dodge of a known separate bug
# rather than a silent omission. Repeatedly building and tearing down an
# InteractiveHTMLItem's QWebEngineView trips the documented WebEngine teardown
# crash (CLAUDE.md calls this item the app's most reliable historical crash
# source; the closeEvent notes say the proxy owns the view). It costs this test
# nothing: measured at 432px content against a 619px viewport, the HTML panel
# is the SMALLEST of them all and is the one asset that never overflows, so it
# is irrelevant to the scrolling behaviour under test.


def new_window():
    ui = studio.AuthoringInterface(studio.ProjectorCanvas())
    ui.resize(1500, 850)
    ui.show()
    app.processEvents()
    ui.add_new_step_node()
    return ui


def select_in_tree(ui, asset):
    """Drive the REAL selection path.

    build_asset_properties() alone populates the form but leaves prop_group
    hidden, and a hidden widget contributes nothing to sizeHint - measuring
    that way reported every panel as 56px tall and "fits", which is exactly the
    vacuous pass this test exists to avoid. handle_tree_selection() is what the
    app actually runs when you click a row.
    """
    for i in range(ui.step_tree.topLevelItemCount()):
        top = ui.step_tree.topLevelItem(i)
        for j in range(top.childCount()):
            child = top.child(j)
            if child.data(1, Qt.ItemDataRole.UserRole) is asset:
                ui.step_tree.clearSelection()
                child.setSelected(True)
                ui.handle_tree_selection()
                return True
    return False


# ============================================================ structural
ui = new_window()
CAP = ui.right_scroll.maximumWidth()
w(f"=== window 1500x850 | properties cap {CAP}px ===")
check("properties column is inside a scroll area",
      ui.right_scroll.widgetResizable() is True)
check("properties column is capped", 0 < CAP <= 500, f"{CAP}px")
check("horizontal scrolling is off (content must wrap, not slide)",
      ui.right_scroll.horizontalScrollBarPolicy()
      == Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
check("AI Assistant starts collapsed",
      ui.ai_group.isCheckable() and not ui.ai_group.isChecked()
      and not ui.ai_body.isVisible())
check("centre pane has a hard minimum width",
      ui.center_stack.parentWidget().minimumWidth() >= 400,
      f"{ui.center_stack.parentWidget().minimumWidth()}px")

# stale-widget regression, on a Text asset (no risky transition involved)
w("")
w("=== rebuilding a panel must not leave the old widgets behind ===")
probe = canvas_items.InteractiveTextItem("Rebuild probe")
ui.insert_asset(probe, "Text probe")
assert select_in_tree(ui, probe)
app.processEvents()
counts = []
for _ in range(4):
    select_in_tree(ui, probe)
    app.processEvents()
    counts.append(len(ui.right_scroll.widget().findChildren(QLabel)))
w(f"   QLabel count after 4 consecutive rebuilds: {counts}")
check("panel does not accumulate stale widgets across rebuilds",
      len(set(counts)) == 1, f"{counts}")

w("")
w("=== expanding the AI Assistant ===")
ui.ai_group.setChecked(True)
app.processEvents()
check("AI Assistant body becomes visible", ui.ai_body.isVisible())
content_h = ui.right_scroll.widget().sizeHint().height()
viewport_h = ui.right_scroll.viewport().height()
w(f"   content {content_h}px vs viewport {viewport_h}px")
if content_h > viewport_h:
    check("scrollbar covers the expanded content",
          ui.right_scroll.verticalScrollBar().maximum() > 0,
          f"range 0..{ui.right_scroll.verticalScrollBar().maximum()}")
ui.ai_group.setChecked(False)

w("")
w("=== a small window still leaves a usable centre ===")
ui.resize(1050, 700)
app.processEvents()
w(f"   at 1050px: properties {ui.right_scroll.width()}px, "
  f"centre {ui.center_stack.width()}px")
check("centre pane stays usable in a cramped window",
      ui.center_stack.width() >= 300, f"{ui.center_stack.width()}px")
alive_structural = ui   # not closed, for the same reason as below

# ====================================================== per asset type
w("")
w("=== properties panel per asset type (fresh window each) ===")
w(f"   {'asset':12s} {'panel w':>8s} {'content h':>10s} {'viewport h':>11s}  scrolls?")

overflowing = 0
offenders_all = []
alive = []   # windows kept alive; see the note in the loop
for name, factory in ASSETS:
    ui = new_window()
    item = factory()
    ui.insert_asset(item, name)
    assert select_in_tree(ui, item), f"could not select {name}"
    app.processEvents()
    ui.resize(1500, 850)
    app.processEvents()
    assert ui.prop_group.isVisible(), f"{name}: properties group not shown"

    panel_w = ui.right_scroll.width()
    content_h = ui.right_scroll.widget().sizeHint().height()
    viewport_h = ui.right_scroll.viewport().height()
    bar_max = ui.right_scroll.verticalScrollBar().maximum()
    if content_h > viewport_h:
        overflowing += 1
    w(f"   {name:12s} {panel_w:>7d}px {content_h:>9d}px {viewport_h:>10d}px  "
      f"{'yes' if bar_max > 0 else 'fits'}")

    check(f"  '{name}' panel within the width cap", panel_w <= CAP,
          f"{panel_w}px (cap {CAP}px)")
    # The whole point: overflow must be REACHABLE, never cut off at the bottom.
    # A few pixels of overshoot can round to a zero-range scrollbar, so allow a
    # small tolerance rather than assert on an exact pixel.
    if content_h > viewport_h + 8:
        check(f"  '{name}' overflow is reachable by scrolling", bar_max > 0,
              f"content {content_h}px vs viewport {viewport_h}px, range 0..{bar_max}")

    for label in ui.right_scroll.widget().findChildren(QLabel):
        if label.text() and label.minimumSizeHint().width() > CAP \
                and not label.wordWrap():
            offenders_all.append(
                (name, label.minimumSizeHint().width(),
                 " ".join(label.text().split())[:50]))
    # Deliberately NOT closed. Tearing these windows down trips the
    # pre-existing teardown crash described at the top of this file; keeping
    # them alive until the process ends sidesteps it without affecting any
    # measurement, since every value above was already read.
    alive.append(ui)

w("")
w(f"   {overflowing} of {len(ASSETS)} asset panels are taller than the viewport;")
w("   before this change that overflow simply ran off the bottom, unreachable.")

w("")
w("=== no properties widget demands more width than the column ===")
for name, needed, text in offenders_all:
    w(f"     {name}: {needed}px  {text}")
check("no unwrapped label sets a minimum wider than the column",
      not offenders_all, f"{len(offenders_all)} offender(s)")

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
os.fsync(_log.fileno())
# os._exit, not sys.exit: the log is already complete, and letting the
# interpreter tear down eight Studio windows would hit the pre-existing
# teardown crash and report a false failure for work that already passed.
os._exit(1 if failures else 0)
