"""Verification that the Observatory camera view cannot be squeezed out.

The complaint: selecting a tool - the LLM Vision tool worst of all - made the
properties panel so large that the video feed and its ROIs were crowded out,
and parts of the panel itself were illegible.

This measures ACTUAL PIXEL WIDTHS with every tool type selected, at the real
window size, rather than eyeballing a screenshot. The invariants:

  * the camera viewport never falls below its minimum, whatever is selected
  * the inspector column never exceeds its cap
  * no widget inside the inspector demands more width than the column, which
    is what an unwrapped QLabel silently does
  * tall panels scroll instead of growing

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_observatory_layout.py
"""
import os
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_observatory_layout.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_layout_")
os.chdir(ISO)

from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import main as om  # noqa: E402

obs = om.ObservatoryEngine(logger=lambda m: None)
obs.resize(1450, 850)
obs.show()
app.processEvents()

CAM_MIN = obs.cam_view.minimumWidth()
PANEL_CAP = obs.right_scroll.maximumWidth()
w(f"=== window 1450x850 | cam min {CAM_MIN}px | inspector cap {PANEL_CAP}px ===")

check("camera view has a hard minimum width", CAM_MIN >= 400, f"{CAM_MIN}px")
check("inspector column is capped", 0 < PANEL_CAP <= 500, f"{PANEL_CAP}px")
check("inspector is inside a scroll area",
      obs.right_scroll.widgetResizable() is True)
check("horizontal scrolling is off (content must wrap, not slide)",
      obs.right_scroll.horizontalScrollBarPolicy()
      == Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
check("Global Configuration starts collapsed",
      obs.global_group.isCheckable() and not obs.global_group.isChecked()
      and not obs.global_body.isVisible())

# ---------------------------------------------------------------- per tool
w("")
w("=== camera viewport width with each tool selected ===")
w(f"   {'tool':28s} {'cam width':>10s} {'panel':>7s} {'panel content h':>16s}")

TOOLS = ["Image Capture", "Pattern Match", "Blob Detection",
         "Delete (Missing Object)", "Motion Detection", "Reference Plane",
         "LLM Vision"]

widths = {}
for tool_type in TOOLS:
    obs.tool_selector.setCurrentText(tool_type)
    obs.spawn_tool_roi(300, 200)
    item = [i for i in obs.cam_scene.items()
            if getattr(i, 'tool_type', None) == tool_type][-1]
    obs.load_tool_properties_to_ui(item)
    app.processEvents()
    obs.resize(1450, 850)
    app.processEvents()

    cam_w = obs.cam_view.width()
    panel_w = obs.right_scroll.width()
    content_h = obs.right_scroll.widget().sizeHint().height()
    widths[tool_type] = cam_w
    w(f"   {tool_type:28s} {cam_w:>9d}px {panel_w:>6d}px {content_h:>15d}px")

    check(f"  camera not squeezed by '{tool_type}'", cam_w >= CAM_MIN,
          f"{cam_w}px (floor {CAM_MIN}px)")
    check(f"  inspector within cap for '{tool_type}'", panel_w <= PANEL_CAP,
          f"{panel_w}px (cap {PANEL_CAP}px)")

w("")
worst = min(widths, key=widths.get)
w(f"   narrowest camera viewport: {widths[worst]}px (with '{worst}' selected)")
check("camera stays usable across every tool type", widths[worst] >= CAM_MIN,
      f"{widths[worst]}px")
check("LLM Vision - the worst offender - no longer crowds the camera",
      widths["LLM Vision"] >= CAM_MIN, f"{widths['LLM Vision']}px")

# --------------------------------------------------- the unwrapped-label trap
w("")
w("=== no inspector widget demands more width than the column ===")
obs.tool_selector.setCurrentText("LLM Vision")
llm = [i for i in obs.cam_scene.items()
       if getattr(i, 'tool_type', None) == "LLM Vision"][-1]
obs.load_tool_properties_to_ui(llm)
app.processEvents()

offenders = []
for label in obs.right_scroll.widget().findChildren(QLabel):
    if not label.text():
        continue
    needed = label.minimumSizeHint().width()
    if needed > PANEL_CAP and not label.wordWrap():
        offenders.append((needed, " ".join(label.text().split())[:60]))
offenders.sort(reverse=True)
for needed, text in offenders:
    w(f"     {needed:>5d}px  {text}")
check("no unwrapped label sets a minimum wider than the column",
      not offenders,
      f"{len(offenders)} offender(s) - these are what squeezed the video")

long_labels = [l for l in obs.right_scroll.widget().findChildren(QLabel)
               if len(l.text()) > 80]
unwrapped = [" ".join(l.text().split())[:50] for l in long_labels if not l.wordWrap()]
check("every long help text is word-wrapped",
      bool(long_labels) and not unwrapped,
      f"{len(long_labels)} long label(s), {len(unwrapped)} unwrapped {unwrapped}")

# --------------------------------------------------- stale-widget regression
w("")
w("=== rebuilding a panel must not leave the old widgets behind ===")
# clear_layout() takes widgets out of the LAYOUT but deleteLater() does not
# destroy them until the next event-loop pass. Without an explicit hide() +
# setParent(None) they stay in the paint tree and draw over the incoming
# widgets - which is what made a freshly selected tool look half-illegible,
# with two labels overlapping. Rebuilding the same panel repeatedly must not
# accumulate children.
def panel_label_count():
    obs.load_tool_properties_to_ui(llm)
    app.processEvents()
    return len(obs.right_scroll.widget().findChildren(QLabel))


counts = [panel_label_count() for _ in range(4)]
w(f"   QLabel count after 4 consecutive rebuilds: {counts}")
check("panel does not accumulate stale widgets across rebuilds",
      len(set(counts)) == 1,
      f"{counts} - a rising count means old widgets are still parented")

# switching between tool types must not accumulate either
mixed = []
for t in ("Image Capture", "LLM Vision", "Motion Detection", "LLM Vision"):
    obs.tool_selector.setCurrentText(t)
    it = [i for i in obs.cam_scene.items() if getattr(i, 'tool_type', None) == t][-1]
    obs.load_tool_properties_to_ui(it)
    app.processEvents()
    mixed.append((t, len(obs.right_scroll.widget().findChildren(QLabel))))
w(f"   switching tool types: {mixed}")
llm_counts = [n for t, n in mixed if t == "LLM Vision"]
check("returning to the same tool type gives the same widget count",
      len(set(llm_counts)) == 1, str(mixed))

# --------------------------------------------------------------- scrolling
w("")
w("=== tall panels scroll instead of growing ===")
obs.global_group.setChecked(True)     # expand provider config: the tallest case
app.processEvents()
content_h = obs.right_scroll.widget().sizeHint().height()
viewport_h = obs.right_scroll.viewport().height()
w(f"   LLM Vision + expanded Global Config: content {content_h}px, "
  f"viewport {viewport_h}px")
check("expanding Global Configuration still does not squeeze the camera",
      obs.cam_view.width() >= CAM_MIN, f"{obs.cam_view.width()}px")
if content_h > viewport_h:
    check("a vertical scrollbar appears when content overflows",
          obs.right_scroll.verticalScrollBar().maximum() > 0,
          f"range 0..{obs.right_scroll.verticalScrollBar().maximum()}")
else:
    w("   (content fits; nothing to scroll at this window size)")
obs.global_group.setChecked(False)

# ------------------------------------------------------- narrow window case
w("")
w("=== a small window still protects the video ===")
obs.resize(1000, 700)
app.processEvents()
w(f"   at 1000px wide: cam {obs.cam_view.width()}px, "
  f"inspector {obs.right_scroll.width()}px, left {obs.cam_view.x()}px")
check("camera keeps its minimum even in a cramped window",
      obs.cam_view.width() >= CAM_MIN, f"{obs.cam_view.width()}px")

obs.close()
app.processEvents()

w("")
w("=== live settings untouched ===")
_after = QSettings("LightGuide", "Observatory")
after = {k: _after.value(k) for k in _after.allKeys()}
changed = [k for k in set(SETTINGS_BEFORE) | set(after)
           if SETTINGS_BEFORE.get(k) != after.get(k)]
check("no live setting written", not changed, f"changed={changed}")

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
