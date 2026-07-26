"""LIVE check that a real vision model actually emits the activation token.

The offline suite proves the matcher and the wiring. The thing only a real
model can answer is whether it *complies* with a "reply with only $ACTIVE"
instruction, and whether it correctly declines on a negative image.

Two images are sent, both drawn here - no camera, no project data:
  positive: a crude human figure  -> expect activation
  negative: an empty room         -> expect NO activation

Never prints the API key. Exits cleanly if none is configured.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_llm_activation_live.py
"""
import os
import sys
import time
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_llm_activation_live.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


def finish(code):
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    print("\n".join(_lines))
    print(f"\nlog: {LOG}")
    sys.exit(code)


ISO = tempfile.mkdtemp(prefix="lg_actlive_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import QSettings, Qt, QCoreApplication  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from vision_tools import img_to_b64, evaluate_activation  # noqa: E402
from ui_components import LLMWorker  # noqa: E402

settings = QSettings("LightGuide", "Observatory")
api_key = str(settings.value("gemini_api_key", settings.value("api_key", "")) or "").strip()

w("=== LLM activation token, live ===")
w(f"  key present: {'yes (' + str(len(api_key)) + ' chars)' if api_key else 'NO'}")
if not api_key:
    w("")
    w("  No Gemini API key configured - nothing to check.")
    w("  Enter it in the app: LLM provider settings -> Gemini API Key.")
    finish(0)

TOKEN = "$ACTIVE"
PROMPT = (f"Is there a person in this image? "
          f"Reply with only {TOKEN} if yes, or only NONE if no.")

# positive: a stick figure on a plain floor
person = np.full((360, 480, 3), 210, np.uint8)
cv2.rectangle(person, (0, 250), (480, 360), (150, 140, 130), -1)
cv2.circle(person, (240, 120), 28, (60, 50, 45), -1)
cv2.rectangle(person, (222, 150), (258, 250), (40, 60, 120), -1)
cv2.line(person, (222, 170), (180, 225), (40, 60, 120), 12)
cv2.line(person, (258, 170), (300, 225), (40, 60, 120), 12)
cv2.line(person, (232, 250), (222, 320), (30, 30, 40), 14)
cv2.line(person, (248, 250), (258, 320), (30, 30, 40), 14)

# negative: the same room, nobody in it
empty = np.full((360, 480, 3), 210, np.uint8)
cv2.rectangle(empty, (0, 250), (480, 360), (150, 140, 130), -1)
cv2.rectangle(empty, (60, 190), (170, 260), (120, 90, 60), -1)   # a crate

results = []
for name, image, expected in (("person present", person, True),
                              ("empty room", empty, False)):
    w("")
    w(f"  --- {name} (expect {'ACTIVATION' if expected else 'no activation'}) ---")
    # The free tier rate-limits bursts, and this script makes two calls back
    # to back. A 429 has two causes that read identically: a per-minute limit,
    # which clears in about a minute, and the daily cap, which does not.
    # Retrying separates them - if it survives two waits it is the daily cap,
    # which is not a defect and not a dead key.
    response = ""
    for attempt in range(3):
        worker = LLMWorker({"provider": "Gemini", "api_key": api_key},
                           PROMPT, img_to_b64(image))
        got = {}
        worker.finished.connect(lambda r, d=got: d.setdefault("response", r))
        t0 = time.time()
        worker.start()
        # Wait for the RESPONSE, not for the thread. `finished` is delivered as
        # a queued signal on this thread, so it can still be sitting in the
        # event queue after isRunning() has already gone false - polling the
        # thread instead reads an empty result every time.
        while "response" not in got and time.time() - t0 < 120:
            app.processEvents()
            time.sleep(0.05)
        worker.wait(5000)
        app.processEvents()
        response = got.get("response", "")
        if "429" not in response:
            break
        if attempt < 2:
            w("      rate limited (429) - waiting 65s for the per-minute window")
            waited = time.time()
            while time.time() - waited < 65:
                app.processEvents()
                time.sleep(0.2)

    if api_key:
        response_shown = response.replace(api_key, "***REDACTED***")
    w(f"      replied in {time.time() - t0:.1f}s: {response_shown.strip()[:120]!r}")

    if response.strip().lower().startswith(("api error", "connection error", "error:")):
        if "429" in response:
            w("      still rate limited after two waits - this is the DAILY free-tier")
            w("      cap, not a defect and not a bad key. Re-run tomorrow.")
        else:
            w("      call failed - cannot judge activation")
        results.append((name, expected, None, False))
        continue

    exact = evaluate_activation(response, TOKEN, "Exact reply")
    ends = evaluate_activation(response, TOKEN, "Ends with")
    w(f"      Exact reply -> {exact}    Ends with -> {ends}")
    results.append((name, expected, exact, exact == expected))

w("")
w("=" * 58)
ok = all(r[3] for r in results) and all(r[2] is not None for r in results)
for name, expected, got_val, passed in results:
    w(f"  {'PASS' if passed else 'FAIL'}  {name}: expected {expected}, got {got_val}")
w("")
if ok:
    w("RESULT: PASS - the model complies with the token instruction both ways.")
    finish(0)
w("RESULT: FAIL - see above. Model compliance varies between runs and models;")
w("re-run before concluding, and prefer 'Exact reply' with a prompt that")
w("permits nothing else.")
finish(1)
