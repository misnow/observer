"""Verification that Blob Detection actually detects blobs.

The reported symptom was no activation at all, whatever the sliders did. Two
causes, both real:

  1. score = len(blobs) * 20, compared against a default sensitivity of 50.
     ONE blob scored 20 and could never trigger - you needed three. The single
     most useful case was the one that could not work.
  2. The mask was cv2.threshold(work, 127, ...) - a fixed level regardless of
     lighting - and the slider the user was moving is Min Area, not that level.
     Nothing they dragged could affect binarisation.

So this drives the real per-frame detection over SYNTHETIC CAMERA FRAMES at
several brightness levels and with noise, and asserts a blob is actually found
and scored - not merely that the code runs.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_blob_detection.py
"""
import os
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_blob_detection.log")
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


ISO = tempfile.mkdtemp(prefix="lg_blob_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import vision_tools  # noqa: E402
import main as om  # noqa: E402

W, H = 640, 480
RNG = np.random.RandomState(11)


def scene(bg=60, parts=((320, 240, 45),), fg=210, noise=6, clutter=0):
    """A camera frame: light parts on a darker background, plus noise."""
    frame = np.full((H, W, 3), bg, np.uint8)
    for _ in range(clutter):
        x, y = RNG.randint(40, W - 40), RNG.randint(40, H - 40)
        cv2.circle(frame, (x, y), RNG.randint(3, 7),
                   (int(bg + 35), int(bg + 35), int(bg + 35)), -1)
    for (x, y, r) in parts:
        cv2.circle(frame, (x, y), r, (fg, fg, fg), -1)
    if noise:
        frame = np.clip(frame.astype(np.int16)
                        + RNG.randint(-noise, noise + 1, frame.shape),
                        0, 255).astype(np.uint8)
    return frame


obs = om.ObservatoryEngine(logger=lambda m: None)
obs.tool_selector.setCurrentText("Blob Detection")
obs.spawn_tool_roi(320, 240)
tool = [i for i in obs.cam_scene.items()
        if getattr(i, 'tool_type', None) == "Blob Detection"][-1]
tool.setPos(60, 40)
tool.update_size(520, 400)
tool.blob_color = 255            # light parts on a dark background
tool.threshold = 200             # min area
tool.blob_max_area = 200000

w(f"=== tool: ROI {tool.rect().width():.0f}x{tool.rect().height():.0f} at "
  f"({tool.pos().x():.0f},{tool.pos().y():.0f}), sensitivity {tool.sensitivity} ===")


class FakeCamera:
    """Feeds one prepared frame, so the REAL per-frame path runs unchanged."""

    def __init__(self):
        self.frame = None

    def isOpened(self):
        return self.frame is not None

    def read(self):
        return (True, self.frame.copy()) if self.frame is not None else (False, None)

    def release(self):
        pass


camera = FakeCamera()
obs.camera = camera


def run(frame):
    """One real detection pass, exactly as the live feed drives it.

    Goes through update_frame() rather than poking the detector directly -
    the bug being fixed lived in that path, and a test that bypassed it would
    not have caught the scoring problem in the first place.
    """
    camera.frame = frame
    obs.update_frame()
    return tool.current_score, obs.tool_states.get(tool.tool_name, False)


# =====================================================================
w("")
w("=== defaults ===")
check("target defaults to ONE blob", tool.blob_target_count == 1,
      str(tool.blob_target_count))
check("Otsu is the default mode", tool.blob_thresh_mode == "Otsu",
      tool.blob_thresh_mode)

# =====================================================================
w("")
w(">>> THE REPORTED BUG: a single blob must trigger <<<")
score, state = run(scene())
w(f"   one blob, default settings -> score {score}, activated {state}")
check("a single blob scores 100 (was 20, below the 50 default)", score == 100,
      f"score {score}")
check("and the tool ACTIVATES", state is True,
      "this is the case that could never fire before")

w("")
w("=== it works across lighting, which a fixed 127 could not ===")
for bg, fg, label in ((30, 90, "dim scene, both sides below 127"),
                      (60, 210, "normal contrast"),
                      (150, 245, "bright scene, both sides above 127"),
                      (100, 160, "low contrast")):
    score, state = run(scene(bg=bg, parts=((320, 240, 45),), fg=fg))
    w(f"   bg={bg:3d} fg={fg:3d}  {label:34s} -> score {score:3d}, active {state}")
    check(f"  detected: {label}", state is True, f"score {score}")

# =====================================================================
w("")
w("=== a noisy field of view ===")
score, state = run(scene(noise=18, clutter=40))
w(f"   heavy noise + 40 specks -> score {score}, active {state}")
check("the real part is still found among specks", state is True, f"score {score}")

geom = obs.tool_geometry.get(tool.tool_name, {})
check("only the wanted number of blobs is reported", geom.get("count") == 1,
      f"count {geom.get('count')}")
if geom.get("found"):
    dx = abs(geom["camera_x"] - 320)
    dy = abs(geom["camera_y"] - 240)
    w(f"   reported centre ({geom['camera_x']},{geom['camera_y']}) vs true (320,240)")
    check("the reported centroid is the real part, not a speck",
          dx < 15 and dy < 15, f"off by ({dx},{dy})")

# =====================================================================
w("")
w("=== selectable blob count ===")
three = ((180, 180, 40), (330, 250, 38), (460, 330, 42))
tool.blob_target_count = 3
score, state = run(scene(parts=three))
geom = obs.tool_geometry.get(tool.tool_name, {})
w(f"   3 parts, target 3 -> score {score}, active {state}, "
  f"reported {geom.get('count')}")
check("3 of 3 scores 100", score == 100, f"score {score}")
check("all three are published for followers", geom.get("count") == 3,
      str(geom.get("count")))
check("blobs are ordered largest first",
      [b["area"] for b in geom.get("blobs", [])]
      == sorted([b["area"] for b in geom.get("blobs", [])], reverse=True))

score, state = run(scene(parts=three[:2]))
w(f"   only 2 present, target 3 -> score {score}, active {state}")
check("a shortfall scores proportionally (2 of 3 = 66)", score == 66, f"score {score}")

tool.blob_target_count = 1
score, _ = run(scene(parts=three))
geom = obs.tool_geometry.get(tool.tool_name, {})
check("target 1 amid 3 parts keeps only the largest",
      geom.get("count") == 1 and score == 100, f"count {geom.get('count')}")
if geom.get("blobs"):
    check("and it is the biggest one",
          abs(geom["blobs"][0]["camera_x"] - 460) < 20, "the r=42 part")

# =====================================================================
w("")
w("=== nothing there means nothing found ===")
score, state = run(scene(parts=()))
check("an empty scene scores 0", score == 0, f"score {score}")
check("and does not activate", state is False)
check("and publishes found=False rather than a stale position",
      obs.tool_geometry.get(tool.tool_name, {}).get("found") is False)

# =====================================================================
w("")
w("=== Motion mode: the part that moved, in a cluttered scene ===")
tool.blob_thresh_mode = "Motion"
tool.blob_motion_thresh = 25

# Train on the empty-but-cluttered bench...
background = scene(parts=(), clutter=60, noise=4)
x1, y1 = int(tool.pos().x()), int(tool.pos().y())
x2 = x1 + int(tool.rect().width())
y2 = y1 + int(tool.rect().height())
tool.reference_frame = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)[y1:y2, x1:x2]

score, state = run(background)
w(f"   unchanged cluttered bench -> score {score}, active {state}")
check("a busy but UNCHANGED background produces no blob", state is False,
      f"score {score} - static clutter must subtract away")

# ...then a part appears on it.
with_part = background.copy()
cv2.circle(with_part, (300, 250), 45, (215, 215, 215), -1)
score, state = run(with_part)
geom = obs.tool_geometry.get(tool.tool_name, {})
w(f"   same bench + a new part -> score {score}, active {state}")
check("the part that APPEARED is detected", state is True, f"score {score}")
if geom.get("found"):
    check("and it is located on the new part, not the clutter",
          abs(geom["camera_x"] - 300) < 20 and abs(geom["camera_y"] - 250) < 20,
          f"({geom['camera_x']},{geom['camera_y']}) vs (300,250)")

tool.reference_frame = None
score, state = run(with_part)
check("Motion mode without a reference frame reports rather than crashing",
      state is False and "reference" in getattr(tool, 'last_blob_note', ''),
      getattr(tool, 'last_blob_note', ''))

# =====================================================================
w("")
w("=== Manual mode ===")
tool.blob_thresh_mode = "Manual"
tool.blob_manual_thresh = 150
score, state = run(scene(bg=60, fg=210))
check("manual level detects when set between bg and fg", state is True, f"score {score}")
tool.blob_manual_thresh = 245
score, state = run(scene(bg=60, fg=210))
check("a level above the part finds nothing (the slider really does something)",
      state is False, f"score {score}")

# =====================================================================
w("")
w("=== settings persist ===")
tool.blob_thresh_mode = "Motion"
tool.blob_target_count = 4
tool.blob_denoise = 9
proj = os.path.join(ISO, "blob.json")
obs.prompt_save_file = lambda *a, **k: proj
obs.prompt_open_file = lambda *a, **k: proj
obs.save_project()
obs.load_project()
back = [i for i in obs.cam_scene.items()
        if getattr(i, 'tool_type', None) == "Blob Detection"]
check("tool reloaded", bool(back))
if back:
    b = back[-1]
    check("mode persists", b.blob_thresh_mode == "Motion", b.blob_thresh_mode)
    check("target count persists", b.blob_target_count == 4, str(b.blob_target_count))
    check("denoise persists", b.blob_denoise == 9, str(b.blob_denoise))

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
