"""
Object segmentation - isolate a subject from its background.

Why this exists: the image -> 3D -> STEP pipeline works far better on a
clean, background-free subject. Single-image-to-3D models reconstruct
whatever they're shown, so a cluttered background becomes part of the mesh.
Segmenting first is the difference between a usable part and a blob.

    Capture Image  ->  [segment]  ->  image-to-3D  ->  mesh_to_step  ->  .step
                        ^^^^^^^^^                      (already built)

Backends are pluggable, mirroring how LLMProviderSettingsWidget abstracts
LLM providers, because the right choice depends on hardware:

  "grabcut"  - classical OpenCV. No downloads, no GPU, works right now.
               Needs a bounding box around the subject and does a genuinely
               decent job on a well-separated object. This is the default
               precisely because it always works.

  "gemini"   - Google's hosted vision model. Best quality available here
               without a GPU, and needs no local model at all. Two costs to
               be aware of, which is why it is never the default: **the image
               leaves the machine** (it is uploaded to Google), and each call
               needs an AI Studio API key and consumes quota. Gemini has no
               bounding-box prompt, so the tool's ROI is used to pick among
               the objects it finds rather than to constrain it; naming the
               subject ("the bracket") sharpens the result.

  "sam3"     - Meta's Segment Anything 3 (facebookresearch/sam3). Much
  "sam2"       better quality, especially on cluttered scenes. Requires
  "sam"        installing the package AND downloading model weights, and
               realistically wants a GPU - on a CPU-only machine expect tens
               of seconds per image. Not installed by default; this module
               reports clearly when a backend is unavailable rather than
               failing obscurely.

Note on provenance: Meta ships SAM from source on GitHub. PyPI packages
named sam2/sam3 are third-party repackagings, so verify what you're
installing before trusting it.
"""

import os
import io
import json
import base64
import urllib.request
import urllib.error
import numpy as np
import cv2
from PyQt6.QtCore import QThread, pyqtSignal


AVAILABLE_BACKENDS = ("grabcut", "gemini", "sam3", "sam2", "sam")

GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "{model}:generateContent")
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _redact(text, secret):
    """Keep an API key out of an error string.

    Defined locally rather than imported from ui_components: these support
    modules are deliberately standalone, and pulling in QtWidgets just to
    share four lines would be a bad trade.
    """
    out = str(text)
    secret = str(secret or "").strip()
    if len(secret) >= 8:
        out = out.replace(secret, "***REDACTED***")
    return out


def backend_status(api_key=""):
    """Which backends can actually run right now, for UI display."""
    status = {"grabcut": (True, "Ready (OpenCV, no download needed)")}
    status["gemini"] = ((True, "Ready (cloud API - sends the image to Google)")
                        if api_key else
                        (False, "Needs a Gemini API key (Settings -> Gemini API Key)"))
    for name, module in (("sam3", "sam3"), ("sam2", "sam2"), ("sam", "segment_anything")):
        try:
            __import__(module)
            status[name] = (True, "Installed")
        except Exception:
            status[name] = (False, f"Not installed (pip install {module})")
    return status


def segment(image_bgr, bbox=None, backend="grabcut", checkpoint="", iterations=5,
            api_key="", model=DEFAULT_GEMINI_MODEL, target="", timeout=90):
    """Return a boolean foreground mask the same HxW as image_bgr.

    bbox is (x, y, w, h) in image pixels - the ROI from a Capture Image
    item maps straight onto this. GrabCut requires it; SAM backends use it
    as a prompt.
    """
    if image_bgr is None or image_bgr.size == 0:
        raise ValueError("No image supplied to segment().")

    h, w = image_bgr.shape[:2]
    if bbox is None:
        # Default to a generous centre box - GrabCut needs *some* hint about
        # where the subject is.
        bbox = (int(w * 0.1), int(h * 0.1), int(w * 0.8), int(h * 0.8))
    x, y, bw, bh = (int(v) for v in bbox)
    x = max(0, min(w - 2, x)); y = max(0, min(h - 2, y))
    bw = max(1, min(w - x, bw)); bh = max(1, min(h - y, bh))

    if backend == "grabcut":
        return _segment_grabcut(image_bgr, (x, y, bw, bh), iterations)
    if backend == "gemini":
        return _segment_gemini(image_bgr, (x, y, bw, bh), api_key, model, target, timeout)
    if backend in ("sam3", "sam2", "sam"):
        return _segment_sam(image_bgr, (x, y, bw, bh), backend, checkpoint)
    raise ValueError(f"Unknown segmentation backend '{backend}'. "
                     f"Expected one of: {', '.join(AVAILABLE_BACKENDS)}")


def _segment_grabcut(image_bgr, bbox, iterations):
    mask = np.zeros(image_bgr.shape[:2], np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(image_bgr, mask, tuple(bbox), bgd, fgd, iterations, cv2.GC_INIT_WITH_RECT)
    # GrabCut labels: 0/2 = background & probable background, 1/3 = foreground.
    return np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD))


def _gemini_request(image_bgr, api_key, model, target, timeout):
    """One generateContent call asking for segmentation, returning parsed JSON.

    The key goes in a header, never the query string - a secret in a URL is
    recorded by proxies and server logs. Errors are redacted before they
    escape, because this message ends up in the Observatory log.
    """
    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        raise RuntimeError("Could not encode the image for the Gemini request.")

    what = target.strip() or "the main foreground object"
    prompt = (
        f"Give the segmentation mask for {what}. "
        'Return one entry with "box_2d" as [ymin, xmin, ymax, xmax] normalized '
        'to 0-1000, "mask" as the outline polygon flattened to '
        "[x1, y1, x2, y2, ...] with the same 0-1000 normalization, and "
        '"label" as a short name.')

    # A response schema, not just "give me JSON". Without one the same prompt
    # returned the polygon four different ways across four calls - a CSV
    # string, a flat number list, a list wrapping a flat list, and a list of
    # [x, y] pairs. Pinning the schema makes it consistent; _as_rings still
    # accepts the other shapes because a model can always surprise you.
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/png",
                             "data": base64.b64encode(buf.tobytes()).decode("ascii")}},
            {"text": prompt},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                        "mask": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                        "label": {"type": "STRING"},
                    },
                    "required": ["box_2d", "mask", "label"],
                },
            },
        },
    }
    request = urllib.request.Request(
        GEMINI_ENDPOINT.format(model=model),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:
            detail = str(e)
        hint = ""
        if e.code in (401, 403):
            hint = (" - check the API key. Note a Google AI Pro subscription does not "
                    "include API access; the key must come from AI Studio.")
        elif e.code == 404:
            hint = f" - the model '{model}' may not exist or not be available to this key."
        elif e.code == 429:
            hint = " - rate limited or out of quota."
        raise RuntimeError(_redact(f"Gemini API error {e.code}{hint} {detail}", api_key)) from e
    except Exception as e:
        raise RuntimeError(_redact(f"Gemini request failed: {e}", api_key)) from e

    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Gemini returned no usable content (response keys: {sorted(body)}). "
            f"If this says the request was blocked, try a different image.") from e

    text = text.strip()
    if text.startswith("```"):                      # occasionally fenced despite the mime type
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini did not return JSON: {text[:200]}") from e

    if isinstance(parsed, dict):
        # Accept either a bare list or a wrapper object around one.
        for key in ("boxes", "masks", "segmentation_masks", "items", "objects"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise RuntimeError(f"Unexpected Gemini JSON shape: {type(parsed).__name__}")
    return parsed


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_point(value):
    # Exactly two numbers. Requiring len == 2 matters: a flat ring like
    # [200, 200, 800, 200, ...] would otherwise look like a single point
    # because its first two entries are numbers.
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_number(c) for c in value))


def _pairs(numbers):
    """[x1, y1, x2, y2, ...] -> [[x1, y1], [x2, y2], ...]"""
    if len(numbers) % 2:
        numbers = numbers[:-1]
    return [[numbers[i], numbers[i + 1]] for i in range(0, len(numbers), 2)]


def _as_rings(raw):
    """Normalise Gemini's polygon output into a list of [x, y] rings.

    Every one of these came back from the same prompt against
    gemini-2.5-flash on 2026-07-25:

        "250,250,750,250,750,750,250,750"       CSV string
        [150, 150, 850, 150, 850, 850, ...]     flat numbers
        [[200, 200, 800, 200, 800, 800, ...]]   list wrapping a flat list
        [[[250, 250], [750, 250], ...]]         list of rings of pairs

    The request now pins a response schema so it should always be the flat
    form, but all four are still accepted - a model can always surprise you,
    and the cost of tolerating them is a dozen lines.
    """
    if isinstance(raw, str):
        parts = [p for p in raw.replace("[", " ").replace("]", " ")
                 .replace(",", " ").split() if p]
        try:
            ring = _pairs([float(p) for p in parts])
        except ValueError:
            return []
        return [ring] if len(ring) >= 3 else []

    if not isinstance(raw, (list, tuple)) or not raw:
        return []

    if all(_is_number(v) for v in raw):                 # flat coordinate run
        ring = _pairs([float(v) for v in raw])
        return [ring] if len(ring) >= 3 else []

    if all(_is_point(v) for v in raw):                  # list of [x, y] pairs
        ring = [[float(p[0]), float(p[1])] for p in raw]
        return [ring] if len(ring) >= 3 else []

    rings = []                                          # otherwise: nested
    for sub in raw:
        rings.extend(_as_rings(sub))
    return rings


def _rings_to_mask(rings, box_px, shape):
    """Rasterise normalised polygon rings into a full-image boolean mask.

    The docs normalise coordinates to 0-1000, but do not say whether that is
    relative to the whole image or to box_2d. Both readings are rasterised and
    the one whose own extent best reproduces box_2d wins.

    The scoring deliberately compares *extents* rather than containment: a
    box-relative polygon sits inside the box by construction, so a containment
    test scores it a perfect 1.0 every time and it always wins. That bug
    silently shrank every mask to about a quarter of its area (measured 4-11%
    image coverage against a true 20.8% on live responses) while producing a
    cutout that looked entirely reasonable.
    """
    height, width = shape[:2]
    x0, y0, x1, y1 = box_px

    def raster(scale):
        out = np.zeros((height, width), np.uint8)
        polys = []
        for ring in rings:
            pts = scale(np.asarray(ring, np.float64))
            if len(pts) >= 3:
                polys.append(np.round(pts).astype(np.int32))
        if polys:
            cv2.fillPoly(out, polys, 1)
        return out

    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    whole = raster(lambda p: np.column_stack([p[:, 0] / 1000.0 * width,
                                              p[:, 1] / 1000.0 * height]))
    within = raster(lambda p: np.column_stack([x0 + p[:, 0] / 1000.0 * bw,
                                               y0 + p[:, 1] / 1000.0 * bh]))

    def box_agreement(m):
        ys, xs = np.where(m)
        if len(xs) == 0:
            return -1.0
        mx0, my0, mx1, my1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        iw = max(0, min(mx1, x1) - max(mx0, x0))
        ih = max(0, min(my1, y1) - max(my0, y0))
        inter = iw * ih
        union = ((mx1 - mx0) * (my1 - my0)) + ((x1 - x0) * (y1 - y0)) - inter
        return inter / union if union else 0.0

    return (whole if box_agreement(whole) >= box_agreement(within) else within) > 0


def _decode_gemini_mask(entry, box_px, shape):
    """Turn one returned entry into a full-image boolean mask.

    Handles both shapes the API is documented/observed to use - a polygon of
    normalized [x, y] points, and a base64-encoded PNG probability map sized
    to the bounding box. Guessing wrong silently would produce a plausible but
    completely wrong cutout, so an unrecognised shape raises instead.
    """
    height, width = shape[:2]
    x0, y0, x1, y1 = box_px
    mask = np.zeros((height, width), np.uint8)
    raw = entry.get("mask")

    # Coordinates first, whatever their container. A polygon can arrive as a
    # CSV string ("250,250,750,250,..."), which must not be mistaken for
    # base64 just because it happens to be a str.
    rings = _as_rings(raw)
    if rings:
        return _rings_to_mask(rings, box_px, shape)

    if isinstance(raw, str) and raw.strip():
        data = raw.split(",", 1)[1] if raw.startswith("data:") else raw
        try:
            decoded = base64.b64decode(data, validate=False)
        except Exception as e:
            raise RuntimeError("Gemini mask was a string but not valid base64.") from e
        png = cv2.imdecode(np.frombuffer(decoded, np.uint8), cv2.IMREAD_UNCHANGED)
        if png is None:
            raise RuntimeError("Gemini mask string did not decode to an image.")
        if png.ndim == 3:
            png = png[:, :, -1] if png.shape[2] == 4 else cv2.cvtColor(png, cv2.COLOR_BGR2GRAY)
        bw, bh = max(1, x1 - x0), max(1, y1 - y0)
        mask[y0:y0 + bh, x0:x0 + bw] = cv2.resize(png, (bw, bh),
                                                  interpolation=cv2.INTER_NEAREST)
        return mask > 127

    raise RuntimeError(
        "Gemini returned no mask this build understands (expected a base64 PNG "
        f"or a coordinate polygon, got {type(raw).__name__}). "
        "Use the 'grabcut' backend, or send me the raw response and I'll wire the real shape.")


def _segment_gemini(image_bgr, bbox, api_key, model, target, timeout):
    """Segment via the Gemini API.

    Unlike grabcut this leaves the machine: the capture is uploaded to Google.
    That is the user's call to make, so it is never the default and the UI
    says so.
    """
    if not api_key:
        raise RuntimeError(
            "The Gemini segmentation backend needs an API key. Set it in the LLM "
            "provider settings (Gemini API Key). A Google AI Pro subscription does "
            "not include API access - the key comes from AI Studio.")

    height, width = image_bgr.shape[:2]
    entries = _gemini_request(image_bgr, api_key, model, target, timeout)
    if not entries:
        raise RuntimeError("Gemini returned no objects for this image. "
                           "Try naming the subject, or use the 'grabcut' backend.")

    x, y, bw, bh = bbox
    want = (x, y, x + bw, y + bh)

    def to_px(box):
        ymin, xmin, ymax, xmax = (float(v) for v in box)
        return (max(0, min(width - 1, int(xmin / 1000.0 * width))),
                max(0, min(height - 1, int(ymin / 1000.0 * height))),
                max(1, min(width, int(xmax / 1000.0 * width))),
                max(1, min(height, int(ymax / 1000.0 * height))))

    def overlap(box_px):
        ax0, ay0, ax1, ay1 = box_px
        bx0, by0, bx1, by1 = want
        iw = max(0, min(ax1, bx1) - max(ax0, bx0))
        ih = max(0, min(ay1, by1) - max(ay0, by0))
        return iw * ih

    # Gemini has no bounding-box prompt, so the ROI is used to *choose* among
    # the objects it found rather than to constrain it.
    scored = []
    for entry in entries:
        if not isinstance(entry, dict) or "box_2d" not in entry:
            continue
        try:
            box_px = to_px(entry["box_2d"])
        except Exception:
            continue
        scored.append((overlap(box_px), box_px, entry))
    if not scored:
        raise RuntimeError("Gemini returned entries without a usable 'box_2d'.")

    scored.sort(key=lambda s: s[0], reverse=True)
    _score, box_px, best = scored[0]
    return _decode_gemini_mask(best, box_px, image_bgr.shape)


def _segment_sam(image_bgr, bbox, backend, checkpoint):
    """Prompt a SAM-family model with the bounding box.

    Kept behind one function so swapping SAM versions doesn't ripple through
    the rest of the app. Raises a clear, actionable error when the backend
    or its weights are missing - a silent fallback to GrabCut would quietly
    give worse results while claiming to be SAM.
    """
    if backend == "sam":
        try:
            from segment_anything import sam_model_registry, SamPredictor
        except ImportError as e:
            raise RuntimeError(
                "segment-anything is not installed. Install it and supply a checkpoint, "
                "or use the 'grabcut' backend which needs no download.") from e
        if not checkpoint or not os.path.exists(checkpoint):
            raise RuntimeError("SAM requires a model checkpoint (.pth); none was provided.")
        model_type = "vit_h"
        for key in ("vit_b", "vit_l", "vit_h"):
            if key in os.path.basename(checkpoint):
                model_type = key
        sam = sam_model_registry[model_type](checkpoint=checkpoint)
        predictor = SamPredictor(sam)
        predictor.set_image(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
        x, y, bw, bh = bbox
        masks, scores, _ = predictor.predict(
            box=np.array([x, y, x + bw, y + bh])[None, :], multimask_output=False)
        return masks[0].astype(bool)

    # SAM 2 / SAM 3 share the image-predictor shape but live in different
    # modules; both need weights on disk.
    module = "sam2" if backend == "sam2" else "sam3"
    try:
        __import__(module)
    except ImportError as e:
        raise RuntimeError(
            f"{module} is not installed. Meta ships it from source "
            f"(github.com/facebookresearch/{module}); PyPI packages of this name are "
            f"third-party repackagings. The 'grabcut' backend needs no download.") from e
    raise RuntimeError(
        f"{module} is installed but this build has not been wired to its predictor API "
        f"yet, and its API has not been verified here. Use 'grabcut', or tell me your "
        f"{module} version and checkpoint path and I'll wire it against the real API.")


def refine_mask(mask, min_area_ratio=0.002, close_px=5):
    """Drop specks and close pinholes.

    Segmenters routinely leave scattered fragments; feeding those into a
    3D reconstruction produces floating debris in the mesh.
    """
    m = mask.astype(np.uint8)
    if close_px > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_px, close_px))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return m.astype(bool)
    min_area = m.shape[0] * m.shape[1] * min_area_ratio
    keep = np.zeros_like(m, dtype=bool)
    for i in range(1, n):   # 0 is background
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep |= (labels == i)
    # If filtering removed everything, the threshold was too aggressive -
    # return the unfiltered mask rather than nothing at all.
    return keep if keep.any() else m.astype(bool)


def mask_to_cutout(image_bgr, mask, crop=True, pad=8):
    """Build a transparent-background BGRA cutout of the subject.

    This is the artifact an image-to-3D model wants: the subject alone, no
    background to reconstruct by mistake.
    """
    alpha = (mask.astype(np.uint8)) * 255
    bgra = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    if not crop or not mask.any():
        return bgra
    ys, xs = np.where(mask)
    y0, y1 = max(0, ys.min() - pad), min(mask.shape[0], ys.max() + 1 + pad)
    x0, x1 = max(0, xs.min() - pad), min(mask.shape[1], xs.max() + 1 + pad)
    return bgra[y0:y1, x0:x1]


def segment_file(image_path, out_path, bbox=None, backend="grabcut", checkpoint="",
                 api_key="", model=DEFAULT_GEMINI_MODEL, target="", timeout=90):
    """Segment an image file and write a transparent PNG cutout."""
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
    mask = refine_mask(segment(image, bbox, backend, checkpoint,
                               api_key=api_key, model=model, target=target,
                               timeout=timeout))
    cutout = mask_to_cutout(image, mask)
    cv2.imwrite(out_path, cutout)
    coverage = float(mask.mean())
    return {"path": out_path, "coverage": coverage,
            "size": (cutout.shape[1], cutout.shape[0]), "backend": backend}


class SegmentWorker(QThread):
    """Segmentation off the UI thread.

    GrabCut on a large image takes a noticeable moment, and SAM on CPU takes
    far longer - blocking the interface for either looks like a hang.
    """
    finished_ok = pyqtSignal(object, str)   # result dict, error

    def __init__(self, image_path, out_path, bbox=None, backend="grabcut",
                 checkpoint="", api_key="", model=DEFAULT_GEMINI_MODEL, target="",
                 timeout=90, parent=None):
        super().__init__(parent)
        self.image_path, self.out_path = image_path, out_path
        self.bbox, self.backend, self.checkpoint = bbox, backend, checkpoint
        self.api_key, self.model, self.target = api_key, model, target
        self.timeout = timeout

    def run(self):
        try:
            self.finished_ok.emit(
                segment_file(self.image_path, self.out_path, self.bbox,
                             self.backend, self.checkpoint, self.api_key,
                             self.model, self.target, self.timeout), "")
        except Exception as e:
            # Redacted: this string is logged and shown on screen.
            self.finished_ok.emit(None, _redact(f"{type(e).__name__}: {e}", self.api_key))
