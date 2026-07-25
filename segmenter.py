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
import numpy as np
import cv2
from PyQt6.QtCore import QThread, pyqtSignal


AVAILABLE_BACKENDS = ("grabcut", "sam3", "sam2", "sam")


def backend_status():
    """Which backends can actually run right now, for UI display."""
    status = {"grabcut": (True, "Ready (OpenCV, no download needed)")}
    for name, module in (("sam3", "sam3"), ("sam2", "sam2"), ("sam", "segment_anything")):
        try:
            __import__(module)
            status[name] = (True, "Installed")
        except Exception:
            status[name] = (False, f"Not installed (pip install {module})")
    return status


def segment(image_bgr, bbox=None, backend="grabcut", checkpoint="", iterations=5):
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


def segment_file(image_path, out_path, bbox=None, backend="grabcut", checkpoint=""):
    """Segment an image file and write a transparent PNG cutout."""
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
    mask = refine_mask(segment(image, bbox, backend, checkpoint))
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
                 checkpoint="", parent=None):
        super().__init__(parent)
        self.image_path, self.out_path = image_path, out_path
        self.bbox, self.backend, self.checkpoint = bbox, backend, checkpoint

    def run(self):
        try:
            self.finished_ok.emit(
                segment_file(self.image_path, self.out_path, self.bbox,
                             self.backend, self.checkpoint), "")
        except Exception as e:
            self.finished_ok.emit(None, f"{type(e).__name__}: {e}")
