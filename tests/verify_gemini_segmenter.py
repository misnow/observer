"""Verification for the Gemini segmentation backend and API-key safety.

No API key and no network are needed: urlopen is stubbed so the REAL request
building, response parsing and mask decoding all run against canned replies.
That covers everything except whether Google's live response matches the
documented shape - which tests/verify_gemini_segmenter_live.py checks once a
key is configured.

Mask decoding is asserted by IoU against a known-truth shape, not merely
"something came back", because a mask decoded under the wrong coordinate
convention still looks like a perfectly plausible mask.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_gemini_segmenter.py
"""
import os
import sys
import json
import base64
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_gemini_segmenter.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_gem_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
import urllib.request  # noqa: E402
import segmenter  # noqa: E402
import ui_components  # noqa: E402

FAKE_KEY = "AIzaSyFAKEKEYFORTESTINGONLY1234567890"

# A 400x300 image with a known rectangular subject.
IMG = np.full((300, 400, 3), 30, np.uint8)
cv2.rectangle(IMG, (100, 75), (300, 225), (200, 180, 40), -1)
TRUTH = np.zeros((300, 400), bool)
TRUTH[75:225, 100:300] = True
BBOX = (90, 65, 220, 170)            # x, y, w, h - roughly around the subject


def iou(a, b):
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union else 0.0


# ---------------------------------------------------------------- stub HTTP
captured = {}


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_urlopen(entries):
    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"candidates": [{"content": {"parts": [
            {"text": json.dumps(entries)}]}}]})
    return fake_urlopen


_real_urlopen = urllib.request.urlopen

# =========================================================================
w("=== backend registration ===")
check("gemini is an available backend", "gemini" in segmenter.AVAILABLE_BACKENDS)
status_no_key = segmenter.backend_status()
status_key = segmenter.backend_status(api_key=FAKE_KEY)
check("reported unavailable without a key", not status_no_key["gemini"][0],
      status_no_key["gemini"][1])
check("reported available with a key", status_key["gemini"][0], status_key["gemini"][1])
check("grabcut still always available", status_no_key["grabcut"][0])

# =========================================================================
w("")
w("=== KEY SAFETY ===")
box_2d = [65 / 300 * 1000, 90 / 400 * 1000, 235 / 300 * 1000, 310 / 400 * 1000]
poly_full = [[100 / 400 * 1000, 75 / 300 * 1000], [300 / 400 * 1000, 75 / 300 * 1000],
             [300 / 400 * 1000, 225 / 300 * 1000], [100 / 400 * 1000, 225 / 300 * 1000]]

urllib.request.urlopen = make_urlopen([{"box_2d": box_2d, "mask": poly_full, "label": "part"}])
try:
    segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen

w("   request URL:", captured["url"])
check("API key is NOT in the request URL", FAKE_KEY not in captured["url"])
check("URL has no query string at all", "?" not in captured["url"])
header_keys = {k.lower(): v for k, v in captured["headers"].items()}
check("API key is sent in the x-goog-api-key header",
      header_keys.get("X-goog-api-key".lower()) == FAKE_KEY
      or header_keys.get("x-goog-api-key") == FAKE_KEY,
      str(sorted(header_keys)))
check("key does not appear anywhere in the request body",
      FAKE_KEY not in json.dumps(captured["body"]))

# redaction
check("segmenter._redact scrubs the key",
      FAKE_KEY not in segmenter._redact(f"boom {FAKE_KEY} boom", FAKE_KEY))
check("ui_components.redact scrubs the key",
      FAKE_KEY not in ui_components.redact(f"url?key={FAKE_KEY}", FAKE_KEY))
check("redact leaves the rest of the message intact",
      "Connection refused" in ui_components.redact(
          f"Connection refused {FAKE_KEY}", FAKE_KEY))
check("redact ignores empty/short secrets (can't blank a whole message)",
      ui_components.redact("hello world", "") == "hello world"
      and ui_components.redact("hello world", "abc") == "hello world")

# a failing call must not leak the key into the error the UI shows
class Boom(Exception):
    pass


def exploding_urlopen(request, timeout=None):
    raise Boom(f"tried {request.full_url} with x-goog-api-key: {FAKE_KEY}")


urllib.request.urlopen = exploding_urlopen
try:
    segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
    check("network failure raises", False, "no exception")
except RuntimeError as e:
    check("network failure raises RuntimeError", True)
    check("the error message does NOT contain the key", FAKE_KEY not in str(e),
          str(e)[:90])
finally:
    urllib.request.urlopen = _real_urlopen

# worker-level redaction (this string is logged and written to disk)
worker = segmenter.SegmentWorker("nope.png", "out.png", backend="gemini", api_key=FAKE_KEY)
emitted = []
worker.finished_ok.connect(lambda r, e: emitted.append((r, e)))
worker.run()
check("worker reports the failure", emitted and emitted[0][0] is None)
check("worker error text carries no key", emitted and FAKE_KEY not in emitted[0][1],
      emitted[0][1][:80] if emitted else "")

# =========================================================================
w("")
w("=== mask decoding, IoU against known truth ===")

urllib.request.urlopen = make_urlopen(
    [{"box_2d": box_2d, "mask": poly_full, "label": "part"}])
try:
    mask_poly = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
score = iou(mask_poly, TRUTH)
check("full-image polygon decoded correctly", score > 0.95, f"IoU={score:.3f}")

# box-relative polygon: same shape expressed 0-1000 *within* box_2d
bx0, by0, bx1, by1 = 90, 65, 310, 235
poly_rel = [[(100 - bx0) / (bx1 - bx0) * 1000, (75 - by0) / (by1 - by0) * 1000],
            [(300 - bx0) / (bx1 - bx0) * 1000, (75 - by0) / (by1 - by0) * 1000],
            [(300 - bx0) / (bx1 - bx0) * 1000, (225 - by0) / (by1 - by0) * 1000],
            [(100 - bx0) / (bx1 - bx0) * 1000, (225 - by0) / (by1 - by0) * 1000]]
urllib.request.urlopen = make_urlopen(
    [{"box_2d": box_2d, "mask": poly_rel, "label": "part"}])
try:
    mask_rel = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
score_rel = iou(mask_rel, TRUTH)
check("box-relative polygon also resolved correctly (self-checking decoder)",
      score_rel > 0.90, f"IoU={score_rel:.3f}")

# --- REGRESSION: polygon whose extent exactly equals box_2d ---------------
# This is what the live API actually returns, and it is the case that caught a
# real bug. The old decoder chose between the whole-image and box-relative
# readings by "how much lands inside box_2d" - but a box-relative polygon is
# inside the box by construction and always scored a perfect 1.0, so it always
# won and every mask came back shrunk to a quarter of its area. Offline tests
# passed anyway because a polygon strictly *inside* the box ties 1.0 vs 1.0 and
# the tie-break happened to pick correctly. A polygon that fills the box
# exactly breaks that tie and exposes it.
tight_box = [75 / 300 * 1000, 100 / 400 * 1000, 225 / 300 * 1000, 300 / 400 * 1000]
tight_poly = [100 / 400 * 1000, 75 / 300 * 1000, 300 / 400 * 1000, 75 / 300 * 1000,
              300 / 400 * 1000, 225 / 300 * 1000, 100 / 400 * 1000, 225 / 300 * 1000]
urllib.request.urlopen = make_urlopen(
    [{"box_2d": tight_box, "mask": tight_poly, "label": "part"}])
try:
    mask_tight = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
tight_score = iou(mask_tight, TRUTH)
check("polygon filling box_2d exactly is NOT shrunk to the box-relative reading",
      tight_score > 0.95, f"IoU={tight_score:.3f} (the bug gave ~0.25 area)")
check("and its coverage matches the subject, not a quarter of it",
      abs(mask_tight.mean() - TRUTH.mean()) < 0.03,
      f"got {mask_tight.mean() * 100:.1f}%, truth {TRUTH.mean() * 100:.1f}%")

# --- the four mask shapes the live API really emits ------------------------
flat = tight_poly
shapes = {
    "flat number list": flat,
    "list wrapping a flat list": [flat],
    "CSV string": ",".join(str(int(v)) for v in flat),
    "list of [x, y] pairs": [[flat[i], flat[i + 1]] for i in range(0, len(flat), 2)],
    "list of rings of pairs": [[[flat[i], flat[i + 1]] for i in range(0, len(flat), 2)]],
}
for name, shape in shapes.items():
    urllib.request.urlopen = make_urlopen(
        [{"box_2d": tight_box, "mask": shape, "label": "part"}])
    try:
        m = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
        check(f"mask shape accepted: {name}", iou(m, TRUTH) > 0.95, f"IoU={iou(m, TRUTH):.3f}")
    except Exception as e:
        check(f"mask shape accepted: {name}", False, f"{type(e).__name__}: {e}")
    finally:
        urllib.request.urlopen = _real_urlopen

# --- the request pins a response schema (this is what stops the variance) --
urllib.request.urlopen = make_urlopen([{"box_2d": tight_box, "mask": flat, "label": "p"}])
try:
    segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
gen_cfg = captured["body"].get("generationConfig", {})
check("request pins responseMimeType", gen_cfg.get("responseMimeType") == "application/json")
check("request pins a responseSchema so the shape stops varying",
      isinstance(gen_cfg.get("responseSchema"), dict)
      and "box_2d" in gen_cfg["responseSchema"]["items"]["properties"],
      str(sorted(gen_cfg.get("responseSchema", {}).get("items", {})
                 .get("properties", {}))))

# base64 PNG probability map sized to the box
png_mask = np.zeros((by1 - by0, bx1 - bx0), np.uint8)
png_mask[(75 - by0):(225 - by0), (100 - bx0):(300 - bx0)] = 255
b64 = base64.b64encode(cv2.imencode(".png", png_mask)[1].tobytes()).decode("ascii")
urllib.request.urlopen = make_urlopen([{"box_2d": box_2d, "mask": b64, "label": "part"}])
try:
    mask_png = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
score_png = iou(mask_png, TRUTH)
check("base64 PNG mask decoded correctly", score_png > 0.95, f"IoU={score_png:.3f}")

# data: URI prefix variant
urllib.request.urlopen = make_urlopen(
    [{"box_2d": box_2d, "mask": "data:image/png;base64," + b64, "label": "part"}])
try:
    mask_uri = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
check("data: URI prefixed mask decoded", iou(mask_uri, TRUTH) > 0.95)

# ROI is used to CHOOSE among several returned objects
far_box = [10, 10, 60, 60]
far_poly = [[5, 5], [50, 5], [50, 50], [5, 50]]
urllib.request.urlopen = make_urlopen([
    {"box_2d": far_box, "mask": far_poly, "label": "distractor"},
    {"box_2d": box_2d, "mask": poly_full, "label": "part"}])
try:
    mask_pick = segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
finally:
    urllib.request.urlopen = _real_urlopen
check("the object overlapping the ROI is chosen over a distractor",
      iou(mask_pick, TRUTH) > 0.95, f"IoU={iou(mask_pick, TRUTH):.3f}")

# =========================================================================
w("")
w("=== failure modes are loud, never a silent bad mask ===")
try:
    segmenter.segment(IMG, BBOX, backend="gemini", api_key="")
    check("missing key raises", False, "no exception")
except RuntimeError as e:
    check("missing key raises a clear RuntimeError",
          "API key" in str(e) and "AI Studio" in str(e), str(e)[:80])

for label, entries, expect in [
    ("empty result", [], "no objects"),
    ("entry with no box_2d", [{"mask": poly_full}], "box_2d"),
    ("unrecognised mask type", [{"box_2d": box_2d, "mask": 42}], "understand"),
]:
    urllib.request.urlopen = make_urlopen(entries)
    try:
        segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
        check(f"{label} raises", False, "no exception")
    except RuntimeError as e:
        check(f"{label} raises a clear error", expect in str(e), str(e)[:80])
    finally:
        urllib.request.urlopen = _real_urlopen


def bad_json_urlopen(request, timeout=None):
    return FakeResponse({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})


urllib.request.urlopen = bad_json_urlopen
try:
    segmenter.segment(IMG, BBOX, backend="gemini", api_key=FAKE_KEY)
    check("non-JSON reply raises", False, "no exception")
except RuntimeError as e:
    check("non-JSON reply raises a clear error", "did not return JSON" in str(e), str(e)[:70])
finally:
    urllib.request.urlopen = _real_urlopen

# =========================================================================
w("")
w("=== grabcut regression (must be untouched) ===")
mask_gc = segmenter.segment(IMG, BBOX, backend="grabcut")
check("grabcut still produces a sane mask", iou(mask_gc, TRUTH) > 0.80,
      f"IoU={iou(mask_gc, TRUTH):.3f}")
out = os.path.join(ISO, "cut.png")
res = segmenter.segment_file(
    os.path.join(ISO, "src.png") if cv2.imwrite(os.path.join(ISO, "src.png"), IMG) else "",
    out, bbox=BBOX, backend="grabcut")
check("segment_file still works with its original signature",
      os.path.exists(out) and res["coverage"] > 0.1, f"coverage={res['coverage']:.3f}")

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
