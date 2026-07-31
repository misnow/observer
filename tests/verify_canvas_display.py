"""Verification for canvas_display.py - output canvas to screen assignment.

The behaviour that matters most, and the one asserted hardest: a projector that
isn't attached must NOT rewrite the saved configuration. Silently persisting a
fallback would destroy a working projection setup every time someone powered a
projector off.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_canvas_display.py
"""
import os
import sys
import json
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_canvas_display.log")
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


os.chdir(tempfile.mkdtemp(prefix="lg_disp_"))

from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

import canvas_display as cd  # noqa: E402

# =====================================================================
w("=== detected screens ===")
screens = cd.list_screens()
for i, name, label, geo in screens:
    w(f"   {label}   at ({geo.x()},{geo.y()})")
check("at least one screen detected", len(screens) >= 1, f"{len(screens)}")
REAL_NAME = screens[0][1]

# =====================================================================
w("")
w("=== screen resolution: name beats index ===")
check("resolves a real screen by name", cd.find_screen(name=REAL_NAME) is not None)
check("resolves by index when no name given", cd.find_screen(index=0) is not None)
check("unknown name falls through to the index",
      cd.find_screen(name="\\\\.\\NOSUCHDISPLAY", index=0) is not None,
      "index is the documented fallback")
check("unknown name AND bad index resolves to nothing",
      cd.find_screen(name="\\\\.\\NOSUCHDISPLAY", index=99) is None)
check("out-of-range index alone resolves to nothing",
      cd.find_screen(index=99) is None)

# =====================================================================
w("")
w("=== assignment + persistence ===")
cfg = cd.DisplayConfig()
cfg.assign(0, screen_name=REAL_NAME, screen_index=0, fullscreen=True, forced=True)
cfg.assign(1, screen_name="\\\\.\\DISPLAY9", screen_index=8, fullscreen=True, forced=True)
check("two assignments stored", len(cfg.assignments) == 2)

st = QSettings("LightGuideTest", "CanvasDisplayVerify")
for k in st.allKeys():
    st.remove(k)
cfg.save(st)
loaded = cd.DisplayConfig.load(st)
check("config survives a QSettings round trip",
      loaded.get(0).screen_name == REAL_NAME
      and loaded.get(1).screen_name == "\\\\.\\DISPLAY9",
      str(loaded.assignments))
check("fullscreen flag survives", loaded.get(0).fullscreen is True)

check("dict round trip is stable",
      cd.DisplayConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict())

st.setValue(cd.SETTINGS_KEY, "{not valid json at all")
try:
    salvaged = cd.DisplayConfig.load(st)
    check("a corrupt stored value degrades to an empty config, not a crash",
          isinstance(salvaged, cd.DisplayConfig) and not salvaged.assignments)
except Exception as e:
    check("a corrupt stored value degrades to an empty config, not a crash",
          False, f"{type(e).__name__}: {e}")
check("garbage inside a valid JSON object is skipped, not fatal",
      not cd.DisplayConfig.from_dict({"notanint": {"screen_name": "x"}}).assignments)
cfg.save(st)   # restore a good value for the checks below

# =====================================================================
w("")
w("=== THE RULE: a missing screen must not rewrite the config ===")


class FakeCanvas:
    """Records what was done to it without needing a real projector."""

    def __init__(self):
        self.calls = []
        self._geo = None
        self.frameless = None
        self.fitted = 0

    def setWindowFlag(self, flag, on):
        self.frameless = bool(on)
        self.calls.append(("setWindowFlag", bool(on)))

    def fit_scene(self):
        self.fitted += 1

    def showNormal(self):
        self.calls.append("showNormal")

    def showFullScreen(self):
        self.calls.append("showFullScreen")

    def show(self):
        self.calls.append("show")

    def setGeometry(self, *a):
        self.calls.append(("setGeometry", a if len(a) > 1 else a[0]))

    def windowHandle(self):
        return None


canvases = [FakeCanvas(), FakeCanvas()]
before = json.dumps(cfg.to_dict(), sort_keys=True)
results = cfg.apply(canvases)
after = json.dumps(cfg.to_dict(), sort_keys=True)

for r in results:
    w(f"   {r}")

check("canvas 0 landed on its real screen", results[0].applied,
      results[0].reason)
check("canvas 0 was actually made fullscreen",
      "showFullScreen" in canvases[0].calls, str(canvases[0].calls))
check("canvas 1 fell back (its screen is not attached)", results[1].fell_back)
check("the fallback explains itself and says the setting is kept",
      "not attached" in results[1].reason and "kept" in results[1].reason,
      results[1].reason)
check("canvas 1 fell back to a WINDOW, not fullscreen",
      "showFullScreen" not in canvases[1].calls, str(canvases[1].calls))
check("a fullscreen projection is BORDERLESS - no title bar stealing pixels",
      canvases[0].frameless is True, str(canvases[0].frameless))
check("and the scene is re-fitted to the screen it landed on",
      canvases[0].fitted >= 1, str(canvases[0].fitted))

check(">>> the saved config is UNCHANGED after a fallback <<<", before == after,
      "a fallback must never persist itself")
check("the missing screen's assignment is still intact",
      cfg.get(1).screen_name == "\\\\.\\DISPLAY9",
      "plugging the projector back in must restore it")

# and it must still be intact after a save/load cycle following a fallback
cfg.save(st)
reloaded = cd.DisplayConfig.load(st)
check("still intact after save/load following a fallback",
      reloaded.get(1) is not None
      and reloaded.get(1).screen_name == "\\\\.\\DISPLAY9",
      str(reloaded.assignments))

# =====================================================================
w("")
w("=== only a FORCED change overwrites ===")
guard = cd.DisplayConfig()
guard.assign(0, screen_name="ORIGINAL", forced=True)
changed = guard.assign(0, screen_name="SNEAKY")          # not forced
check("an unforced assign does NOT overwrite an existing one",
      changed is False and guard.get(0).screen_name == "ORIGINAL",
      guard.get(0).screen_name)
changed = guard.assign(0, screen_name="DELIBERATE", forced=True)
check("a forced assign does overwrite",
      changed is True and guard.get(0).screen_name == "DELIBERATE",
      guard.get(0).screen_name)
check("assigning a NEW canvas needs no force",
      guard.assign(3, screen_name="NEW") is True)

# =====================================================================
w("")
w("=== an unassigned canvas is left ALONE ===")
# ProjectorCanvas already auto-fullscreens onto a second screen; "no
# assignment" must mean no opinion, not "force a window".
empty = cd.DisplayConfig()
untouched = FakeCanvas()
res = empty.apply([untouched])
check("no assignment means the window is not touched at all",
      untouched.calls == [], str(untouched.calls))
check("and it is reported as unassigned, not as a failure",
      "no screen assigned" in res[0].reason, res[0].reason)

# =====================================================================
w("")
w("=== the setup dialog ===")
dlg = cd.CanvasSetupDialog(3, cfg)
check("dialog builds for 3 canvases", dlg is not None)
out = dlg.result_config()
check("dialog returns assignments for the canvases that have a screen",
      isinstance(out, cd.DisplayConfig))
check("dialog output is written as forced (it is a deliberate user choice)",
      all(isinstance(a, cd.Assignment) for a in out.assignments.values()))
dlg.close()

for k in st.allKeys():
    st.remove(k)

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")
_log.flush()
sys.exit(1 if failures else 0)
