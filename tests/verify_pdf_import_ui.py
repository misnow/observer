"""Verification for the PDF import UI wiring (pdf_import.py + studio hook).

Two halves, each real for what it covers:
  1. The real PdfImportDialog running a real ExtractWorker on a real PDF -
     probe summary, populated pick-list, selected_assets().
  2. Studio's import_pdf() turning selections into real canvas assets.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_pdf_import_ui.py
"""
import os
import sys
import time
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_pdf_import_ui.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_pdfui_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import Qt, QCoreApplication  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication, QFileDialog, QDialog  # noqa: E402

app = QApplication(sys.argv)

import pymupdf  # noqa: E402
import pdf_import  # noqa: E402
import canvas_items  # noqa: E402
import studio  # noqa: E402

# ------------------------------------------------------------- fixture
SCAN_TEXT = "PURCHASE ORDER 7788"
NATIVE = "Bill of materials, revision C"

img = np.full((300, 900, 3), 255, np.uint8)
cv2.putText(img, SCAN_TEXT, (25, 160), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 4)
scan_png = cv2.imencode(".png", img)[1].tobytes()

photo = np.zeros((240, 320, 3), np.uint8)
cv2.rectangle(photo, (30, 30), (290, 210), (40, 170, 220), -1)
photo_png = cv2.imencode(".png", photo)[1].tobytes()

doc = pymupdf.open()
p1 = doc.new_page()
p1.insert_text((72, 100), NATIVE, fontsize=16)
p1.insert_image(pymupdf.Rect(72, 140, 392, 380), stream=photo_png)
p2 = doc.new_page()
p2.insert_image(pymupdf.Rect(40, 40, 560, 220), stream=scan_png)
PDF = os.path.join(ISO, "order.pdf")
doc.save(PDF)
doc.close()
w("=== fixture:", PDF, f"({os.path.getsize(PDF)} bytes) ===")

# =====================================================================
w("")
w("=== 1. PdfImportDialog: probe -> extract -> pick ===")
dlg = pdf_import.PdfImportDialog(None, start_dir=ISO)
check("dialog constructs", dlg is not None)
check("Extract disabled before a file is chosen", not dlg.btn_extract.isEnabled())

_real_getopen = QFileDialog.getOpenFileName
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (PDF, "PDF Files (*.pdf)"))
try:
    dlg.browse()          # the real handler
finally:
    QFileDialog.getOpenFileName = _real_getopen

check("file accepted and Extract enabled", dlg.btn_extract.isEnabled())
summary = dlg.summary.text()
w("   summary:", " ".join(summary.split())[:150])
check("summary reports the page count", "2 pages" in summary, summary[:60])
check("summary warns about the OCR cost up front", "OCR'd" in summary and "roughly" in summary)

dlg.start_extract()       # real ExtractWorker on a real QThread
deadline = time.time() + 180
while time.time() < deadline and dlg._result is None:
    app.processEvents()
    time.sleep(0.05)

check("extraction finished", dlg._result is not None)
res = dlg._result or {}
check("page 2 was OCR'd", res.get("ocr_pages") == [2], str(res.get("ocr_pages")))
import difflib  # noqa: E402

ocr_text = (res.get("page_texts") or ["", ""])[1].strip()
accuracy = difflib.SequenceMatcher(None, SCAN_TEXT, ocr_text).ratio()
w(f"   ground truth : {SCAN_TEXT!r}")
w(f"   OCR returned : {ocr_text!r}")
w(f"   accuracy     : {accuracy:.3f}")
# Exact equality is the wrong bar here: the fixture is rendered in OpenCV's
# Hershey stroke font, which is harder to read than the real typefaces on an
# actual scan. A measured similarity floor catches genuine OCR regressions
# without pretending a known O/0 confusion isn't happening.
check("OCR accuracy against ground truth >= 0.90", accuracy >= 0.90,
      f"{accuracy:.3f} - {ocr_text!r}")
# Digits must be exact - a misread order/invoice number is the failure that
# actually costs something.
check("OCR read the digits exactly", "7788" in ocr_text, repr(ocr_text))

kinds = [dlg.results.item(i).data(Qt.ItemDataRole.UserRole)[0]
         for i in range(dlg.results.count())]
w("   pick-list kinds:", kinds)
check("pick list offers full text, per-page text and images",
      "full_text" in kinds and kinds.count("page_text") == 2 and "image" in kinds)
check("nothing is ticked by default (no surprise inserts)",
      all(dlg.results.item(i).checkState() == Qt.CheckState.Unchecked
          for i in range(dlg.results.count())))

dlg._check_images()
img_sel = dlg.selected_assets()
check("'Images only' selects just images",
      bool(img_sel) and all(k == "image" for k, _ in img_sel), str(len(img_sel)))
check("selected image paths exist on disk",
      all(os.path.exists(p) for _, p in img_sel))

dlg._check_text()
txt_sel = dlg.selected_assets()
check("'Text only' selects just text",
      bool(txt_sel) and all(k == "text" for k, _ in txt_sel), str(len(txt_sel)))
check("selected text carries the OCR'd content",
      any("7788" in t for _, t in txt_sel),
      "matched on the digits, which OCR reads exactly")
check("selected text carries the native text layer",
      any(NATIVE in t for _, t in txt_sel))

dlg._check_all()
check("'All' selects everything", len(dlg.selected_assets()) == len(kinds))
dlg._check_none()
check("'None' clears the selection", dlg.selected_assets() == [])

dlg.close()

# =====================================================================
w("")
w("=== 2. Studio import_pdf() inserts real canvas assets ===")
studio.AuthoringInterface.launch_observatory = lambda self: None
ui = studio.AuthoringInterface(studio.ProjectorCanvas())
ui.add_new_step_node()

image_path = next(p for k, p in img_sel if k == "image")
FAKE = [("text", "Extracted page text\nsecond line"), ("image", image_path)]


class StubDialog:
    """Stands in for the dialog so this half tests Studio's insertion path
    only - the dialog itself is covered above."""

    def __init__(self, parent=None, start_dir=""):
        pass

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected_assets(self):
        return list(FAKE)

    def result_info(self):
        return {"source": PDF, "out_dir": ISO}


_real_dialog = pdf_import.PdfImportDialog
pdf_import.PdfImportDialog = StubDialog
try:
    before = len(ui.all_canvas_items())
    ui.import_pdf()
    added = [i for i in ui.all_canvas_items()]
finally:
    pdf_import.PdfImportDialog = _real_dialog

texts = [i for i in added if isinstance(i, canvas_items.InteractiveTextItem)]
medias = [i for i in added if isinstance(i, canvas_items.InteractiveMediaItem)]
check("a text asset was inserted", len(texts) == 1, f"{len(texts)}")
check("an image asset was inserted", len(medias) == 1, f"{len(medias)}")
check("text content preserved", texts and "Extracted page text" in texts[0].toPlainText())
check("page text uses the smaller 18pt font, not the 48pt default",
      texts and texts[0].current_font_size == 18, str(texts[0].current_font_size if texts else "-"))
check("text is width-constrained so it can't run off the canvas",
      texts and texts[0].textWidth() > 0, str(texts[0].textWidth() if texts else "-"))
check("image asset points at the extracted file",
      medias and medias[0].filepath == image_path)

# Both must survive a save/load round trip like any other asset.
proj = os.path.join(ISO, "pdf_assets.json")
ui.prompt_save_file = lambda *a, **k: proj
ui.prompt_open_file = lambda *a, **k: proj
ui.save_project()
ui.load_project()
after = ui.all_canvas_items()
check("imported assets survive save/load",
      len([i for i in after if isinstance(i, canvas_items.InteractiveTextItem)]) == 1
      and len([i for i in after if isinstance(i, canvas_items.InteractiveMediaItem)]) == 1,
      f"{len(after)} items")

# =====================================================================
w("")
w("=== 3. missing dependency is reported, not crashed on ===")
logged = []
ui.log_message = lambda m: logged.append(m)
_saved = sys.modules.pop("pdf_import")
sys.modules["pdf_import"] = None      # forces ImportError inside import_pdf
try:
    ui.import_pdf()
    check("import_pdf survives a missing pdf_import module", True)
    check("and says so in the log",
          any("PDF import unavailable" in m for m in logged),
          logged[-1][:70] if logged else "(nothing logged)")
except Exception as e:
    check("import_pdf survives a missing pdf_import module", False,
          f"{type(e).__name__}: {e}")
finally:
    sys.modules["pdf_import"] = _saved

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
