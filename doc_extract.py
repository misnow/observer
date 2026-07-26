"""
PDF scraping - pull text and images out of a document so they can be placed
on a Studio canvas.

    PDF  ->  [text layer | OCR]  ->  text files
         ->  [embedded images]   ->  PNG/JPEG files  ->  canvas assets

Two shapes of document, one code path:

  Small  - everything is returned in memory *and* written to disk, so a
           caller can drop the text straight onto a canvas without touching
           the filesystem.
  Large  - only a summary and a manifest come back. A 500-page scan holds a
           lot of text, and materialising all of it just to insert three
           pages of it is wasteful. Files land in a folder; `manifest.json`
           indexes them.

Images are always written to files regardless of mode, because that is what
InteractiveMediaItem consumes - it takes a filepath, not a buffer.

OCR backends are pluggable, mirroring segmenter.py, because the right choice
depends on what you are willing to install and send off the machine:

  "rapidocr" - RapidOCR on onnxruntime. Local, offline, CPU-only, no GPU
               needed and no API cost. Measured here at ~2.4s per A4 page at
               200dpi. This is the default.
  "gemini"   - Gemini vision. Better on messy or handwritten scans, but every
               page is a network round trip and an API call you pay for.
               NOTE: written against the documented google-genai API and
               exercised only as far as a key-less run allows - unlike the
               rapidocr path it has not been verified end to end here.
  "none"     - never OCR; pages with no text layer come back empty.

The default `ocr="auto"` only OCRs pages whose text layer is missing or
near-empty. That distinction matters: at ~2.4s/page, OCRing a 500-page
document that already has a perfectly good text layer wastes twenty minutes
and produces worse text than simply reading it.

On accuracy: OCR is not lossless. Measured here on a synthetic fixture,
rapidocr scored 0.947 against ground truth and turned "ORDER" into "0RDER" -
the classic O/0 confusion - while reading every digit correctly. Treat
OCR'd text as needing a human eye before it goes in front of an operator,
and prefer the text layer whenever one exists (which is what "auto" does).

Output folder layout:

    <out_dir>/
        text/page_0001.txt        one file per page
        text/full.txt             everything, in page order
        images/page_0001_img_01.png
        manifest.json

`manifest.json` records source, page count, which pages were OCR'd and by
which backend, every text file, and every image with its page and pixel
size - enough to drive a picker UI without reopening the PDF.
"""

import os
import json
import time

try:
    import pymupdf
except ImportError:                                  # older wheels only ship "fitz"
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None

from PyQt6.QtCore import QThread, pyqtSignal


AVAILABLE_OCR_BACKENDS = ("rapidocr", "gemini", "none")

# A page needs OCR when its text layer holds less than this many characters.
# Scanned pages routinely carry a stray page number or a header artifact, so
# "zero characters" is too strict a test for "this page is an image".
MIN_TEXT_CHARS = 20

# Above this many pages, stop returning the full text in memory.
INLINE_PAGE_LIMIT = 25

# Embedded images smaller than this on either side are almost always spacers,
# rules, or bullet glyphs rather than content.
MIN_IMAGE_PX = 64

_OCR_ENGINE = None          # RapidOCR construction costs ~0.5s; build it once.


def backend_status():
    """Which pieces can actually run right now, for UI display."""
    status = {}
    if pymupdf is None:
        status["pdf"] = (False, "PyMuPDF not installed (pip install pymupdf)")
    else:
        ver = getattr(pymupdf, "__version__", None) or str(getattr(pymupdf, "version", ""))
        status["pdf"] = (True, f"PyMuPDF {ver}")
    try:
        import rapidocr_onnxruntime  # noqa: F401
        status["rapidocr"] = (True, "Ready (local, CPU, ~2.4s/page)")
    except Exception:
        status["rapidocr"] = (False, "Not installed (pip install rapidocr-onnxruntime)")
    try:
        from google import genai  # noqa: F401
        status["gemini"] = (True, "SDK present - needs an API key per call")
    except Exception:
        status["gemini"] = (False, "Not installed (pip install google-genai)")
    status["none"] = (True, "OCR disabled - text-layer pages only")
    return status


def _require_pymupdf():
    if pymupdf is None:
        raise RuntimeError(
            "PyMuPDF is not installed, so no PDF can be read. "
            "Install it with:  pip install pymupdf")


def probe(pdf_path):
    """Cheap look at a document: how big, how much of it needs OCR.

    Deliberately does not render or OCR anything - this is what a UI calls to
    decide whether to warn about a long job before committing to it.
    """
    _require_pymupdf()
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)

    doc = pymupdf.open(pdf_path)
    try:
        pages = doc.page_count
        with_text = needing_ocr = images = 0
        for page in doc:
            if len((page.get_text() or "").strip()) >= MIN_TEXT_CHARS:
                with_text += 1
            else:
                needing_ocr += 1
            images += len(page.get_images(full=True))
    finally:
        doc.close()

    return {
        "path": pdf_path,
        "pages": pages,
        "pages_with_text": with_text,
        "pages_needing_ocr": needing_ocr,
        "embedded_images": images,
        "size_bytes": os.path.getsize(pdf_path),
        "recommended_mode": "inline" if pages <= INLINE_PAGE_LIMIT else "folder",
        # Rough, but honest enough to warn on: OCR dominates everything else.
        "estimated_ocr_seconds": round(needing_ocr * 2.4, 1),
    }


# --------------------------------------------------------------------------
# OCR backends
# --------------------------------------------------------------------------
def _ocr_rapidocr(png_bytes):
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:
        raise RuntimeError(
            "rapidocr-onnxruntime is not installed, so scanned pages cannot be read. "
            "Install it with:  pip install rapidocr-onnxruntime") from e
    import numpy as np
    import cv2

    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()

    arr = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return ""
    result, _elapse = _OCR_ENGINE(arr)
    if not result:
        return ""
    # Each row is [box, text, score]; we only want the text, in reading order.
    return "\n".join(str(row[1]) for row in result if len(row) > 1)


def _ocr_gemini(png_bytes, api_key="", model="gemini-3.6-flash"):
    """OCR a page image with Gemini vision.

    Unverified end to end here (no key was exercised), unlike the rapidocr
    path - it raises clearly rather than degrading quietly.
    """
    if not api_key:
        raise RuntimeError(
            "The Gemini OCR backend needs an API key. Note that a Google AI Pro "
            "subscription does not include API access - the key comes from AI Studio.")
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "google-genai is not installed (pip install google-genai).") from e

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
            "Transcribe all text in this page image exactly. Output only the "
            "transcribed text, preserving line breaks and reading order. "
            "If the page contains no text, output nothing.",
        ],
    )
    return (getattr(response, "text", "") or "").strip()


def _ocr_page(png_bytes, backend, api_key=""):
    if backend == "none":
        return ""
    if backend == "rapidocr":
        return _ocr_rapidocr(png_bytes)
    if backend == "gemini":
        return _ocr_gemini(png_bytes, api_key)
    raise ValueError(f"Unknown OCR backend '{backend}'. "
                     f"Expected one of: {', '.join(AVAILABLE_OCR_BACKENDS)}")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
def default_out_dir(pdf_path, base=None):
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem).strip() or "document"
    return os.path.join(base or os.path.dirname(os.path.abspath(pdf_path)), f"{safe}_extracted")


def extract(pdf_path, out_dir=None, mode="auto", ocr="auto", ocr_backend="rapidocr",
            api_key="", dpi=200, min_image_px=MIN_IMAGE_PX, max_pages=None,
            progress=None, should_stop=None):
    """Pull text and images out of `pdf_path`.

    ocr   - "auto" (only pages with no usable text layer), "always", or "never"
    mode  - "auto" (inline under INLINE_PAGE_LIMIT pages, else folder),
            "inline", or "folder"
    progress(done, total, note) is called per page; should_stop() lets a
    worker cancel a long job between pages.
    """
    _require_pymupdf()
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)
    if ocr not in ("auto", "always", "never"):
        raise ValueError(f"ocr must be auto/always/never, got {ocr!r}")

    started = time.time()
    out_dir = out_dir or default_out_dir(pdf_path)
    text_dir = os.path.join(out_dir, "text")
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(text_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    try:
        total = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
        if mode == "auto":
            mode = "inline" if total <= INLINE_PAGE_LIMIT else "folder"
        keep_inline = (mode == "inline")

        page_text_files, images, ocr_pages, page_texts = [], [], [], []
        seen_xrefs = {}      # xref -> relative path, so a repeated logo is written once
        total_chars = 0
        cancelled = False

        full_path = os.path.join(text_dir, "full.txt")
        with open(full_path, "w", encoding="utf-8") as full_fh:
            for index in range(total):
                if should_stop is not None and should_stop():
                    cancelled = True
                    break

                page = doc[index]
                number = index + 1
                text = (page.get_text() or "").strip()

                needs_ocr = (ocr == "always"
                             or (ocr == "auto" and len(text) < MIN_TEXT_CHARS))
                if needs_ocr and ocr_backend != "none":
                    png = page.get_pixmap(dpi=dpi).tobytes("png")
                    ocr_text = _ocr_page(png, ocr_backend, api_key)
                    if ocr_text:
                        # Prefer OCR only when it actually found more than the
                        # thin text layer did, so a good layer is never
                        # replaced by a worse transcription.
                        if len(ocr_text) > len(text):
                            text = ocr_text
                            ocr_pages.append(number)

                rel_txt = os.path.join("text", f"page_{number:04d}.txt")
                with open(os.path.join(out_dir, rel_txt), "w", encoding="utf-8") as fh:
                    fh.write(text)
                page_text_files.append(rel_txt)
                total_chars += len(text)
                if keep_inline:
                    page_texts.append(text)
                full_fh.write(f"\n\n===== Page {number} =====\n\n{text}")

                # --- embedded images ---
                for slot, info in enumerate(page.get_images(full=True), start=1):
                    xref = info[0]
                    if xref in seen_xrefs:
                        # Same image object reused (a header logo on every
                        # page); record the reference, don't write it again.
                        images.append({"page": number, "path": seen_xrefs[xref],
                                       "reused": True})
                        continue
                    try:
                        raw = doc.extract_image(xref)
                    except Exception:
                        continue
                    width, height = raw.get("width", 0), raw.get("height", 0)
                    if width < min_image_px or height < min_image_px:
                        continue
                    rel_img = os.path.join("images",
                                           f"page_{number:04d}_img_{slot:02d}.{raw['ext']}")
                    with open(os.path.join(out_dir, rel_img), "wb") as fh:
                        fh.write(raw["image"])
                    seen_xrefs[xref] = rel_img
                    images.append({"page": number, "path": rel_img, "reused": False,
                                   "width": width, "height": height})

                if progress is not None:
                    progress(number, total, f"page {number}/{total}")
    finally:
        doc.close()

    manifest = {
        "source": os.path.abspath(pdf_path),
        "generated": time.time(),
        "pages": total,
        "cancelled": cancelled,
        "mode": mode,
        "ocr_backend": ocr_backend if ocr_pages else None,
        "ocr_pages": ocr_pages,
        "chars": total_chars,
        "text_dir": "text",
        "images_dir": "images",
        "full_text": os.path.join("text", "full.txt"),
        "page_text": page_text_files,
        "images": images,
        "seconds": round(time.time() - started, 2),
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    result = dict(manifest)
    result["out_dir"] = out_dir
    result["manifest"] = manifest_path
    # Absolute paths for the caller; the manifest keeps relative ones so the
    # folder stays portable if it's moved.
    result["image_paths"] = [os.path.join(out_dir, i["path"]) for i in images]
    result["text"] = "\n\n".join(page_texts) if keep_inline else ""
    result["page_texts"] = page_texts if keep_inline else []
    return result


def load_manifest(out_dir):
    """Re-open a previous extraction without touching the original PDF."""
    path = out_dir if out_dir.endswith(".json") else os.path.join(out_dir, "manifest.json")
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    root = os.path.dirname(os.path.abspath(path))
    manifest["out_dir"] = root
    manifest["image_paths"] = [os.path.join(root, i["path"]) for i in manifest.get("images", [])]
    return manifest


def read_page_text(manifest, page_number):
    """Text of one page, read on demand - the point of folder mode."""
    root = manifest["out_dir"]
    for rel in manifest.get("page_text", []):
        if rel.endswith(f"page_{page_number:04d}.txt"):
            with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
                return fh.read()
    return ""


class ExtractWorker(QThread):
    """Extraction off the UI thread.

    Essential rather than merely polite: OCR runs about 2.4s per page, so a
    scanned document blocks the interface for minutes and is indistinguishable
    from a hang.
    """
    progressed = pyqtSignal(int, int, str)      # done, total, note
    finished_ok = pyqtSignal(object, str)       # result dict, error

    def __init__(self, pdf_path, out_dir=None, mode="auto", ocr="auto",
                 ocr_backend="rapidocr", api_key="", dpi=200, max_pages=None, parent=None):
        super().__init__(parent)
        self.pdf_path, self.out_dir, self.mode = pdf_path, out_dir, mode
        self.ocr, self.ocr_backend, self.api_key = ocr, ocr_backend, api_key
        self.dpi, self.max_pages = dpi, max_pages
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            result = extract(
                self.pdf_path, self.out_dir, self.mode, self.ocr, self.ocr_backend,
                self.api_key, self.dpi, max_pages=self.max_pages,
                progress=lambda d, t, n: self.progressed.emit(d, t, n),
                should_stop=lambda: self._stop)
            self.finished_ok.emit(result, "")
        except Exception as e:
            self.finished_ok.emit(None, f"{type(e).__name__}: {e}")
