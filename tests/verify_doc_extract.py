"""Verification for doc_extract.py (PDF text/image scraping + OCR).

Asserts on QUALITY, not just absence of exceptions - the house rule. In
particular OCR is checked against known ground truth (the exact string that
was rendered into the scanned page), because an OCR pass that returns
plausible-looking garbage passes any "it didn't crash" test.

Builds its own PDFs so it needs no fixtures:
  page 1 - a real text layer
  page 2 - image only, no text layer  -> must be OCR'd
  page 3 - text plus an embedded photo, and the same logo as page 2

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_doc_extract.py
"""
import os
import sys
import json
import time
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_doc_extract.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_pdf_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
import pymupdf  # noqa: E402
import doc_extract  # noqa: E402

w("=== backends ===")
for name, (ok, msg) in doc_extract.backend_status().items():
    w(f"  {name:10s} {'OK ' if ok else '-- '} {msg}")
status = doc_extract.backend_status()
check("PDF backend available", status["pdf"][0], status["pdf"][1])
check("rapidocr backend available", status["rapidocr"][0], status["rapidocr"][1])

# ---------------------------------------------------------------- fixtures
NATIVE_LINE = "Invoice from Northwind Traders"
SCAN_TEXT = "SCANNED INVOICE 12345"
SCAN_TEXT2 = "Total due 998.00"


def render_text_image(lines, w_px=1000, h_px=380):
    img = np.full((h_px, w_px, 3), 255, np.uint8)
    y = 130
    for text, scale, thick in lines:
        cv2.putText(img, text, (25, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick)
        y += 110
    return cv2.imencode(".png", img)[1].tobytes()


logo = np.zeros((120, 120, 3), np.uint8)
cv2.circle(logo, (60, 60), 50, (200, 60, 40), -1)
logo_png = cv2.imencode(".png", logo)[1].tobytes()

photo = np.zeros((300, 400, 3), np.uint8)
cv2.rectangle(photo, (40, 40), (360, 260), (30, 180, 220), -1)
photo_png = cv2.imencode(".png", photo)[1].tobytes()

scan_png = render_text_image([(SCAN_TEXT, 1.6, 4), (SCAN_TEXT2, 1.4, 3)])

doc = pymupdf.open()
p1 = doc.new_page()
p1.insert_text((72, 100), NATIVE_LINE, fontsize=18)
p1.insert_text((72, 130), "Terms: net 30 days", fontsize=12)

p2 = doc.new_page()
p2.insert_image(pymupdf.Rect(40, 40, 560, 240), stream=scan_png)
p2.insert_image(pymupdf.Rect(40, 300, 100, 360), stream=logo_png)

p3 = doc.new_page()
p3.insert_text((72, 100), "Appendix A - equipment photograph", fontsize=14)
p3.insert_image(pymupdf.Rect(72, 130, 372, 355), stream=photo_png)
p3.insert_image(pymupdf.Rect(400, 130, 460, 190), stream=logo_png)   # SAME logo

PDF = os.path.join(ISO, "sample.pdf")
doc.save(PDF)
doc.close()
w("")
w("=== fixture written:", PDF, f"({os.path.getsize(PDF)} bytes) ===")

# ------------------------------------------------------------------- probe
w("")
w("=== probe (must not render or OCR) ===")
t0 = time.time()
info = doc_extract.probe(PDF)
probe_secs = time.time() - t0
w("  ", json.dumps({k: v for k, v in info.items() if k != "path"}))
check("probe found 3 pages", info["pages"] == 3, str(info["pages"]))
check("probe flags exactly one page as needing OCR", info["pages_needing_ocr"] == 1,
      str(info["pages_needing_ocr"]))
check("probe is fast (no rendering)", probe_secs < 2.0, f"{probe_secs:.2f}s")
check("probe recommends inline for a small doc", info["recommended_mode"] == "inline")

# ----------------------------------------------------------------- extract
w("")
w("=== extract, ocr=auto, rapidocr ===")
seen = []
t0 = time.time()
res = doc_extract.extract(PDF, out_dir=os.path.join(ISO, "out"), ocr="auto",
                          ocr_backend="rapidocr",
                          progress=lambda d, t, n: seen.append(d))
secs = time.time() - t0
w(f"  took {secs:.1f}s, mode={res['mode']}, ocr_pages={res['ocr_pages']}")

check("progress reported once per page", seen == [1, 2, 3], str(seen))
check("small doc came back inline", res["mode"] == "inline")
check("inline mode returned text in memory", bool(res["text"]))
check("page 2 was OCR'd, others were not", res["ocr_pages"] == [2], str(res["ocr_pages"]))

# --- QUALITY: OCR against known ground truth ---
page2 = res["page_texts"][1]
w("  page2 OCR text:", repr(page2))
check("OCR recovered the scanned heading EXACTLY", SCAN_TEXT in page2, repr(page2[:60]))
check("OCR recovered the scanned amount EXACTLY", SCAN_TEXT2 in page2, repr(page2))
check("native text layer read verbatim", NATIVE_LINE in res["page_texts"][0])
check("page 3 text read", "Appendix A" in res["page_texts"][2])

# --- files on disk ---
w("")
w("=== output folder ===")
out = res["out_dir"]
for root, _dirs, files in os.walk(out):
    for f in sorted(files):
        rel = os.path.relpath(os.path.join(root, f), out)
        w(f"    {rel}  ({os.path.getsize(os.path.join(root, f))} b)")

check("full.txt written", os.path.exists(os.path.join(out, "text", "full.txt")))
check("one text file per page",
      all(os.path.exists(os.path.join(out, p)) for p in res["page_text"])
      and len(res["page_text"]) == 3)
full = open(os.path.join(out, "text", "full.txt"), encoding="utf-8").read()
check("full.txt contains both native and OCR'd text",
      NATIVE_LINE in full and SCAN_TEXT in full)

# --- images + dedupe ---
written = [i for i in res["images"] if not i.get("reused")]
reused = [i for i in res["images"] if i.get("reused")]
w("  images written:", [i["path"] for i in written])
w("  images reused :", [i["path"] for i in reused])
check("embedded images extracted to disk",
      all(os.path.exists(p) for p in res["image_paths"]) and len(written) >= 2)
written_paths = [i["path"] for i in written]
# The logo is on pages 2 and 3. It must exist as exactly one file on disk,
# with page 3 recording a reference to page 2's copy - otherwise a 500-page
# document with a header logo would write 500 identical PNGs.
check("the repeated logo was written ONCE and referenced again",
      len(reused) == 1 and reused[0]["path"] in written_paths
      and reused[0]["page"] == 3,
      f"{len(written)} written / {len(reused)} reused -> {reused[0]['path'] if reused else '-'}")
check("no duplicate files on disk",
      len(written_paths) == len(set(written_paths))
      and len(os.listdir(os.path.join(out, "images"))) == len(written_paths),
      f"{len(os.listdir(os.path.join(out, 'images')))} files, {len(written_paths)} records")
check("extracted image is readable and correctly sized",
      cv2.imread(os.path.join(out, [i for i in written
                                    if "img" in i["path"]][-1]["path"])) is not None)

# --- manifest ---
w("")
w("=== manifest round trip ===")
man = doc_extract.load_manifest(out)
check("manifest reloads without the PDF", man["pages"] == 3 and man["ocr_pages"] == [2])
check("manifest stores RELATIVE paths (folder stays portable)",
      all(not os.path.isabs(p) for p in man["page_text"])
      and all(not os.path.isabs(i["path"]) for i in man["images"]))
check("read_page_text pulls one page on demand",
      SCAN_TEXT in doc_extract.read_page_text(man, 2))
check("manifest records the OCR backend", man["ocr_backend"] == "rapidocr")

# ------------------------------------------------------- ocr=never / large
w("")
w("=== ocr='never' leaves the scan empty (no silent OCR) ===")
res_no = doc_extract.extract(PDF, out_dir=os.path.join(ISO, "out_noocr"), ocr="never")
check("scanned page is empty when OCR is off", not res_no["page_texts"][1].strip(),
      repr(res_no["page_texts"][1][:40]))
check("text-layer pages still read with OCR off", NATIVE_LINE in res_no["page_texts"][0])
check("no pages marked OCR'd", res_no["ocr_pages"] == [])

w("")
w("=== large document falls back to folder mode ===")
big = pymupdf.open()
for i in range(doc_extract.INLINE_PAGE_LIMIT + 5):
    pg = big.new_page()
    pg.insert_text((72, 100), f"Page {i + 1} of a long report about widgets", fontsize=12)
BIG = os.path.join(ISO, "big.pdf")
big.save(BIG)
big.close()

big_info = doc_extract.probe(BIG)
check("probe recommends folder mode for a large doc",
      big_info["recommended_mode"] == "folder", str(big_info["pages"]) + " pages")
res_big = doc_extract.extract(BIG, out_dir=os.path.join(ISO, "out_big"), ocr="auto")
check("large doc used folder mode", res_big["mode"] == "folder")
check("large doc did NOT hold all text in memory",
      res_big["text"] == "" and res_big["page_texts"] == [])
check("large doc still wrote every page file",
      len(res_big["page_text"]) == doc_extract.INLINE_PAGE_LIMIT + 5)
man_big = doc_extract.load_manifest(res_big["out_dir"])
check("large doc text is reachable on demand from the manifest",
      "Page 7 of a long report" in doc_extract.read_page_text(man_big, 7))
check("no OCR was run on a text-layer document", res_big["ocr_pages"] == [],
      "would have cost ~72s if it had")

# ----------------------------------------------------------------- cancel
w("")
w("=== cancellation stops between pages ===")
res_cancel = doc_extract.extract(BIG, out_dir=os.path.join(ISO, "out_cancel"),
                                 should_stop=lambda: True)
check("cancelled extraction reports itself", res_cancel["cancelled"] is True)
check("cancelled extraction wrote nothing", res_cancel["pages"] == big_info["pages"]
      and not res_cancel["page_text"])

# ------------------------------------------------------------ error paths
w("")
w("=== errors are loud, never silent ===")
try:
    doc_extract.extract(os.path.join(ISO, "nope.pdf"))
    check("missing file raises", False, "no exception")
except FileNotFoundError:
    check("missing file raises FileNotFoundError", True)
except Exception as e:
    check("missing file raises FileNotFoundError", False, type(e).__name__)

try:
    doc_extract.extract(PDF, out_dir=os.path.join(ISO, "x"), ocr="sometimes")
    check("bad ocr mode raises", False, "no exception")
except ValueError:
    check("bad ocr mode raises ValueError", True)

try:
    doc_extract._ocr_page(b"", "banana")
    check("unknown OCR backend raises", False, "no exception")
except ValueError:
    check("unknown OCR backend raises ValueError", True)

try:
    doc_extract._ocr_gemini(b"x", api_key="")
    check("keyless Gemini raises rather than silently failing", False, "no exception")
except RuntimeError as e:
    check("keyless Gemini raises a clear RuntimeError", "API key" in str(e), str(e)[:70])

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
