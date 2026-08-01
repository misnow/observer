"""Verification for three reported defects.

  1. The output canvas did not fill the screen. Fitting happened in __init__
     and on resize, but until a window is mapped its viewport still reports
     the default 640x480 - so the scale was computed for the wrong size and
     never recomputed, leaving a 1920x1080 scene drawn at a third of its size.
  2. Observatory opened with a dead feed.
  3. An unconfigured LLM Call sat on "Waiting for Observatory response..."
     for ever, because emit_llm_trigger_command returned SILENTLY when the
     Observatory file or target tool was missing.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_canvas_fit_and_llm.py
"""
import os
import sys
import json
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_canvas_fit_and_llm.log")
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


ISO = tempfile.mkdtemp(prefix="lg_fit_")
os.chdir(ISO)

from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import canvas_items  # noqa: E402
import studio  # noqa: E402

# =====================================================================
w(">>> 1. the output canvas must FILL the screen <<<")
canvas = studio.ProjectorCanvas()
view = canvas.view
canvas.resize(1536, 864)
canvas.show()
for _ in range(6):
    app.processEvents()

vp_w, vp_h = view.viewport().width(), view.viewport().height()
scale = view.transform().m11()
expected = min(vp_w / 1920.0, vp_h / 1080.0)
w(f"   viewport {vp_w}x{vp_h}, scale {scale:.4f}, expected {expected:.4f}")
check("the scene is scaled to fill the window", abs(scale - expected) < 0.03,
      f"{scale:.4f} vs {expected:.4f} - a stale 0.33 means it fitted at the "
      f"pre-show default 640x480")

mapped = view.mapFromScene(canvas.scene.sceneRect()).boundingRect()
w(f"   the 1920x1080 scene draws as {mapped.width()}x{mapped.height()}")
check("it occupies most of the viewport, not a small centred island",
      mapped.width() > vp_w * 0.9, f"{mapped.width()} of {vp_w}px wide")
check("aspect ratio is preserved (no skew)",
      abs(view.transform().m11() - view.transform().m22()) < 1e-6,
      f"m11 {view.transform().m11():.4f} m22 {view.transform().m22():.4f}")

check("no horizontal scrollbar", not view.horizontalScrollBar().isVisible())
check("no vertical scrollbar", not view.verticalScrollBar().isVisible())
check("nothing to scroll to anyway",
      view.horizontalScrollBar().maximum() == 0
      and view.verticalScrollBar().maximum() == 0,
      "a non-zero range means part of the canvas is off-screen")

w("")
w("   --- moved to a different-sized projector ---")
canvas.resize(1024, 768)
for _ in range(4):
    app.processEvents()
vp_w2, vp_h2 = view.viewport().width(), view.viewport().height()
expected2 = min(vp_w2 / 1920.0, vp_h2 / 1080.0)
w(f"   viewport {vp_w2}x{vp_h2}, scale {view.transform().m11():.4f}, "
  f"expected {expected2:.4f}")
check("it re-fits to the new screen size",
      abs(view.transform().m11() - expected2) < 0.03)

# =====================================================================
w("")
w("   --- fit modes ---")
canvas.resize(1600, 700)          # deliberately NOT 16:9
for _ in range(4):
    app.processEvents()

canvas.set_fit_mode("fit")
for _ in range(3):
    app.processEvents()
t = canvas.view.transform()
w(f"   fit     -> m11 {t.m11():.4f} m22 {t.m22():.4f}")
check("'fit' keeps the shape (equal x and y scale)",
      abs(t.m11() - t.m22()) < 1e-6, "a circle stays a circle")

canvas.set_fit_mode("stretch")
for _ in range(3):
    app.processEvents()
t = canvas.view.transform()
vw, vh = canvas.view.viewport().width(), canvas.view.viewport().height()
w(f"   stretch -> m11 {t.m11():.4f} m22 {t.m22():.4f}  (viewport {vw}x{vh})")
check("'stretch' fills the screen exactly, distorting as asked",
      abs(t.m11() - vw / 1920.0) < 0.02 and abs(t.m22() - vh / 1080.0) < 0.02,
      "this is the skew-to-fit behaviour")
check("and the two axes really do differ on a non-16:9 screen",
      abs(t.m11() - t.m22()) > 1e-3, f"{t.m11():.4f} vs {t.m22():.4f}")

canvas.set_fit_mode("actual")
for _ in range(3):
    app.processEvents()
t = canvas.view.transform()
check("'actual' applies no scaling at all",
      abs(t.m11() - 1.0) < 1e-6 and abs(t.m22() - 1.0) < 1e-6,
      f"{t.m11():.4f}")
canvas.set_fit_mode("fit")

import canvas_display as cd  # noqa: E402

a = cd.Assignment("X", 0, True, "stretch")
check("fit mode survives an Assignment round trip",
      cd.Assignment.from_dict(a.to_dict()).fit_mode == "stretch")
check("an unknown fit mode falls back to 'fit', not garbage",
      cd.Assignment("X", 0, True, "banana").fit_mode == "fit")

w("")
w(">>> 2. Observatory brings the feed up on launch <<<")
import main as om  # noqa: E402

check("autostart_camera exists and is what main() calls",
      callable(getattr(om.ObservatoryEngine, "autostart_camera", None)))
src = open(os.path.join(PROJECT, "main.py"), encoding="utf-8").read()
check("main() schedules it after the window is shown",
      "observatory.show()" in src and "autostart_camera" in src.split("def main()")[1])
check("it is NOT called from the constructor",
      "autostart_camera" not in src.split("def __init__")[1].split("def ")[0],
      "a constructor that grabbed the camera would have the tests fighting "
      "over real hardware")

obs = om.ObservatoryEngine(logger=lambda m: None)
check("constructing the engine alone starts no feed", not obs.timer.isActive())

obs.camera_selector.clear()
obs.camera_selector.addItem("No Cameras Found", None)
obs.autostart_camera()
check("with no camera present it declines quietly rather than raising",
      not obs.timer.isActive())

# =====================================================================
w("")
w("   --- opening a project must start the feed too ---")
src_main = open(os.path.join(PROJECT, "main.py"), encoding="utf-8").read()
load_body = src_main.split("def load_project")[1].split("\n    def ")[0]
check("load_project starts the camera when none is running",
      "autostart_camera" in load_body,
      "opening a project and being met with a dead view looked broken")
check("and switches camera if the project uses a different one",
      "switching from" in load_body)

w("")
w(">>> 3. an unconfigured LLM Call must SAY so <<<")
studio.AuthoringInterface.launch_observatory = lambda self: None
ui = studio.AuthoringInterface(studio.ProjectorCanvas())
ui.add_new_step_node()
logged = []
ui.log_message = lambda m: logged.append(m)

llm = canvas_items.InteractiveLLMTextItem()
ui.insert_asset(llm, "LLM")
initial = llm.content.toPlainText()
w(f"   initial text: {initial!r}")

# No Observatory file, no source tool - exactly the reported setup.
logged.clear()
ui.emit_llm_trigger_command(llm)
w(f"   after trigger: {llm.content.toPlainText()[:80]!r}")
check("it no longer sits silently on the initial text",
      llm.content.toPlainText() != initial,
      "previously it returned silently and this never changed")
check("the canvas says what is missing",
      "Not configured" in llm.content.toPlainText())
check("it names the Observatory project", "Observatory project"
      in llm.content.toPlainText())
check("and the missing target tool", "LLM Vision tool"
      in llm.content.toPlainText())
check("it is reported in the log too", any("Not configured" in m for m in logged),
      str(logged)[:90])

w("")
w("   --- a file that is set but missing from disk ---")
llm2 = canvas_items.InteractiveLLMTextItem()
llm2.observatory_file = os.path.join(ISO, "gone.json")
llm2.source_tool = "tool01_LLM"
ui.insert_asset(llm2, "LLM2")
ui.emit_llm_trigger_command(llm2)
check("a deleted Observatory file is reported specifically",
      "gone.json" in llm2.content.toPlainText(), llm2.content.toPlainText()[:80])

w("")
w("   --- fully configured: the command IS written ---")
obs_file = os.path.join(ISO, "obs.json")
with open(obs_file, "w") as f:
    json.dump({"camera_index": 0, "tools": [
        {"name": "tool01_LLM", "type": "LLM Vision", "x": 0.0, "y": 0.0,
         "w": 250.0, "h": 250.0}]}, f)
llm3 = canvas_items.InteractiveLLMTextItem()
llm3.observatory_file = obs_file
llm3.source_tool = "tool01_LLM"
llm3.prompt = "Is there a person here?"
ui.insert_asset(llm3, "LLM3")
if os.path.exists("studio_command.json"):
    os.remove("studio_command.json")
ui.emit_llm_trigger_command(llm3)
check("a configured LLM Call writes the IPC command",
      os.path.exists("studio_command.json"))
if os.path.exists("studio_command.json"):
    cmd = json.load(open("studio_command.json"))
    w(f"   command: {json.dumps({k: v for k, v in cmd.items() if k != 'timestamp'})}")
    check("the action is trigger_llm", cmd.get("action") == "trigger_llm")
    check("the PROMPT is included", cmd.get("prompt") == "Is there a person here?",
          str(cmd.get("prompt")))
    check("the target tool is named", cmd.get("tool_name") == "tool01_LLM")
    check("the Observatory file travels with it, so Observatory can load it",
          cmd.get("observatory_file") == obs_file)
check("and it now says it is waiting, not that it is unconfigured",
      "Waiting" in llm3.content.toPlainText(), llm3.content.toPlainText()[:60])

w("")
w(">>> 4. commands must not vanish when Observatory is closed <<<")
launched = []
ui.launch_observatory = lambda: launched.append(1)

if os.path.exists("vision_state.json"):
    os.remove("vision_state.json")
ui.observatory_process = None
check("with no vision_state.json, Observatory is judged NOT alive",
      ui.observatory_is_alive() is False)

launched.clear()
ui._observatory_launch_pending = False
ui.ensure_observatory_running("test")
check(">>> a command with no Observatory running LAUNCHES one <<<",
      len(launched) == 1,
      "previously the command was written to disk and simply ignored")

launched.clear()
ui.ensure_observatory_running("test again")
check("a second call does not spawn a duplicate", not launched,
      "a launch already in flight must not be doubled")

# A freshly-written vision_state means one IS alive.
ui._observatory_launch_pending = False
with open("vision_state.json", "w") as f:
    json.dump({"states": {}, "responses": {}, "response_times": {},
               "captures": {}, "geometry": {}}, f)
check("a freshly written vision_state.json counts as alive",
      ui.observatory_is_alive() is True,
      "liveness is judged by EVIDENCE, not by owning the process")
launched.clear()
ui.ensure_observatory_running("should not launch")
check("and nothing is launched when one is already running", not launched)

old_mtime = os.path.getmtime("vision_state.json")
os.utime("vision_state.json", (old_mtime - 3600, old_mtime - 3600))
check("a stale vision_state.json does NOT count as alive",
      ui.observatory_is_alive() is False,
      "an Observatory that died leaves its last state file behind")

w("")
w(">>> Observatory no longer swallows IPC errors <<<")
check("the command listener reports exceptions instead of `pass`",
      "IPC command error" in src,
      "a swallowed IPC exception is one of the silent failures CLAUDE.md names")

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
