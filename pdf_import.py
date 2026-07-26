"""
PDF import dialog - browse a document, extract it, pick what goes on the canvas.

Lives in its own module rather than in studio.py, which was just split for
being oversized; the dependency runs one way (studio -> pdf_import ->
doc_extract) and nothing here imports studio.

The dialog owns the *choosing*. It never touches the canvas itself: it hands
back a list of ("text", str) / ("image", path) pairs and lets Studio insert
them, so the asset classes stay Studio's business.

Crash rules from CLAUDE.md apply throughout:
  - every button handler takes no signal argument, because clicked(bool)
    otherwise lands in the first positional parameter
  - the extraction QThread is held in a list, never a bare attribute
  - every widget touch in a worker callback is wrapped in try/except
    RuntimeError, since the dialog can be closed mid-extraction
"""

import os

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
                             QLineEdit, QPushButton, QComboBox, QSpinBox, QListWidget,
                             QListWidgetItem, QProgressBar, QFileDialog, QMessageBox,
                             QDialogButtonBox, QWidget, QAbstractItemView)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QIcon, QPixmap

import doc_extract


# Loading a thumbnail per image is fine for a report; for a 500-page scan it
# would mean thousands of decodes before the list is usable.
MAX_THUMBNAILS = 200


class PdfImportDialog(QDialog):
    def __init__(self, parent=None, start_dir=""):
        super().__init__(parent)
        self.setWindowTitle("Import PDF")
        self.setMinimumSize(720, 620)
        self._workers = []
        self._result = None
        self._manifest = None
        self._start_dir = start_dir or os.getcwd()

        root = QVBoxLayout(self)

        # --- source -------------------------------------------------------
        src = QHBoxLayout()
        self.path_field = QLineEdit()
        self.path_field.setPlaceholderText("Choose a PDF...")
        self.path_field.setReadOnly(True)
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self._slot(self.browse))
        src.addWidget(self.path_field)
        src.addWidget(btn_browse)
        root.addLayout(src)

        self.summary = QLabel("<i>No document selected.</i>")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        # --- options ------------------------------------------------------
        opts = QFormLayout()
        self.cb_ocr = QComboBox()
        self.cb_ocr.addItem("Auto - only pages with no text layer", "auto")
        self.cb_ocr.addItem("Never - text layer only (fastest)", "never")
        self.cb_ocr.addItem("Always - re-OCR every page (slowest)", "always")
        opts.addRow("OCR:", self.cb_ocr)

        self.cb_backend = QComboBox()
        for name, (ok, msg) in doc_extract.backend_status().items():
            if name in ("pdf",):
                continue
            self.cb_backend.addItem(f"{name}{'' if ok else '  (unavailable)'}", name)
            self.cb_backend.setItemData(self.cb_backend.count() - 1, msg,
                                        Qt.ItemDataRole.ToolTipRole)
        idx = self.cb_backend.findData("rapidocr")
        if idx >= 0:
            self.cb_backend.setCurrentIndex(idx)
        opts.addRow("OCR backend:", self.cb_backend)

        self.spin_dpi = QSpinBox()
        self.spin_dpi.setRange(72, 400)
        self.spin_dpi.setValue(200)
        self.spin_dpi.setToolTip("Render resolution for OCR. Higher is slower "
                                 "but reads small print better.")
        opts.addRow("OCR render DPI:", self.spin_dpi)
        root.addLayout(opts)

        # --- run ----------------------------------------------------------
        run = QHBoxLayout()
        self.btn_extract = QPushButton("Extract")
        self.btn_extract.setStyleSheet("background-color: #16a085; font-weight: bold;")
        self.btn_extract.setEnabled(False)
        self.btn_extract.clicked.connect(self._slot(self.start_extract))
        self.btn_cancel_job = QPushButton("Stop")
        self.btn_cancel_job.setEnabled(False)
        self.btn_cancel_job.clicked.connect(self._slot(self.cancel_extract))
        run.addWidget(self.btn_extract)
        run.addWidget(self.btn_cancel_job)
        root.addLayout(run)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # --- results ------------------------------------------------------
        root.addWidget(QLabel("<b>Select what to place on the canvas:</b>"))
        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.results.setIconSize(QSize(64, 64))
        root.addWidget(self.results, 1)

        pick = QHBoxLayout()
        for label, fn in (("All", self._check_all), ("None", self._check_none),
                          ("Text only", self._check_text), ("Images only", self._check_images)):
            b = QPushButton(label)
            b.clicked.connect(self._slot(fn))
            pick.addWidget(b)
        pick.addStretch()
        root.addLayout(pick)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Insert Selected")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    @staticmethod
    def _slot(fn):
        """Wrap a handler so it takes no signal argument.

        clicked(bool) otherwise overwrites the first positional parameter -
        the single most repeated crash cause in this project.
        """
        def wrapper(*_args, **_kwargs):
            try:
                fn()
            except Exception as e:
                print(f"[pdf_import] {type(e).__name__}: {e}")
        return wrapper

    def _set(self, widget_attr, method, *args):
        try:
            widget = getattr(self, widget_attr, None)
            if widget is not None:
                getattr(widget, method)(*args)
        except RuntimeError:
            pass    # dialog closed mid-extraction; the C++ object is gone

    # ------------------------------------------------------------------
    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select PDF", self._start_dir,
                                              "PDF Files (*.pdf)")
        if not path:
            return
        self.path_field.setText(path)
        self.results.clear()
        self._result = None
        try:
            info = doc_extract.probe(path)
        except Exception as e:
            self.summary.setText(f"<span style='color:#e74c3c;'>Could not read: {e}</span>")
            self.btn_extract.setEnabled(False)
            return

        mb = info["size_bytes"] / (1024 * 1024)
        warn = ""
        if info["pages_needing_ocr"]:
            mins = info["estimated_ocr_seconds"] / 60.0
            cost = (f"~{info['estimated_ocr_seconds']:.0f}s" if mins < 1
                    else f"~{mins:.1f} min")
            warn = (f"<br><span style='color:#f39c12;'>{info['pages_needing_ocr']} page(s) "
                    f"have no text layer and will be OCR'd - roughly {cost} on this "
                    f"machine (CPU only).</span>")
        self.summary.setText(
            f"<b>{os.path.basename(path)}</b> - {info['pages']} pages, "
            f"{info['pages_with_text']} with a text layer, "
            f"{info['embedded_images']} embedded image(s), {mb:.1f} MB."
            f"<br>Recommended mode: <b>{info['recommended_mode']}</b>"
            f"{' (large - text stays on disk and is read on demand)' if info['recommended_mode'] == 'folder' else ''}"
            f"{warn}")
        self.btn_extract.setEnabled(True)

    # ------------------------------------------------------------------
    def start_extract(self):
        path = self.path_field.text().strip()
        if not path:
            return
        self.btn_extract.setEnabled(False)
        self.btn_cancel_job.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.status.setText("Starting...")
        self.results.clear()

        worker = doc_extract.ExtractWorker(
            path, out_dir=None, mode="auto",
            ocr=self.cb_ocr.currentData(),
            ocr_backend=self.cb_backend.currentData() or "rapidocr",
            dpi=self.spin_dpi.value())
        worker.progressed.connect(self._on_progress)
        worker.finished_ok.connect(self._on_done)
        # A list, not a single attribute: a second run must not garbage
        # collect a QThread that is still executing.
        self._workers.append(worker)
        worker.start()

    def cancel_extract(self):
        for worker in self._workers:
            worker.cancel()
        self._set("status", "setText", "Stopping after the current page...")

    def _on_progress(self, done, total, note):
        try:
            self.progress.setMaximum(max(1, total))
            self.progress.setValue(done)
            self.status.setText(note)
        except RuntimeError:
            pass

    def _on_done(self, result, error):
        # Data model first, widgets after - so a closed dialog never costs us
        # the result of a long extraction.
        self._result = result
        if result:
            self._manifest = dict(result)
        try:
            self.btn_extract.setEnabled(True)
            self.btn_cancel_job.setEnabled(False)
            self.progress.setVisible(False)
            if error:
                self.status.setText(f"Failed: {error}")
                QMessageBox.warning(self, "Extraction failed", error)
                return
            self._populate(result)
        except RuntimeError:
            pass
        finally:
            self._workers = [w for w in self._workers if not w.isFinished()]

    # ------------------------------------------------------------------
    def _populate(self, result):
        self.results.clear()
        pages = result["pages"]
        ocr_note = (f", OCR'd {len(result['ocr_pages'])} page(s) "
                    f"via {result['ocr_backend']}" if result["ocr_pages"] else "")
        self.status.setText(
            f"Extracted {pages} page(s) in {result['seconds']}s{ocr_note}. "
            f"Files in: {result['out_dir']}")

        def add(label, kind, payload, icon=None, checked=False):
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, (kind, payload))
            if icon is not None:
                item.setIcon(icon)
            self.results.addItem(item)

        if result["chars"]:
            add(f"📄 Full text ({result['chars']:,} chars, all {pages} pages)",
                "full_text", None)

        for n in range(1, pages + 1):
            text = self._page_text(result, n)
            if not text.strip():
                continue
            preview = " ".join(text.split())[:70]
            flag = "  [OCR]" if n in result["ocr_pages"] else ""
            add(f"📃 Page {n}{flag} - {preview}", "page_text", n)

        for i, img in enumerate(result["images"]):
            if img.get("reused"):
                continue      # same file already offered above
            abs_path = os.path.join(result["out_dir"], img["path"])
            icon = None
            if i < MAX_THUMBNAILS:
                pix = QPixmap(abs_path)
                if not pix.isNull():
                    icon = QIcon(pix)
            size = (f" ({img.get('width')}x{img.get('height')})"
                    if img.get("width") else "")
            add(f"🖼️ p{img['page']}  {os.path.basename(img['path'])}{size}",
                "image", abs_path, icon)

        if self.results.count() == 0:
            add("(nothing extracted - the document may be empty)", "none", None)

    def _page_text(self, result, n):
        if result.get("page_texts"):
            return result["page_texts"][n - 1]
        try:
            return doc_extract.read_page_text(result, n)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def _each(self):
        for i in range(self.results.count()):
            yield self.results.item(i)

    def _check_all(self):
        for item in self._each():
            item.setCheckState(Qt.CheckState.Checked)

    def _check_none(self):
        for item in self._each():
            item.setCheckState(Qt.CheckState.Unchecked)

    def _check_text(self):
        for item in self._each():
            kind = item.data(Qt.ItemDataRole.UserRole)[0]
            item.setCheckState(Qt.CheckState.Checked if kind in ("full_text", "page_text")
                               else Qt.CheckState.Unchecked)

    def _check_images(self):
        for item in self._each():
            kind = item.data(Qt.ItemDataRole.UserRole)[0]
            item.setCheckState(Qt.CheckState.Checked if kind == "image"
                               else Qt.CheckState.Unchecked)

    # ------------------------------------------------------------------
    def selected_assets(self):
        """[("text", str) | ("image", path)] for everything ticked."""
        out = []
        if not self._result:
            return out
        for item in self._each():
            if item.checkState() != Qt.CheckState.Checked:
                continue
            kind, payload = item.data(Qt.ItemDataRole.UserRole)
            if kind == "full_text":
                text = self._result.get("text") or ""
                if not text:
                    full = os.path.join(self._result["out_dir"], self._result["full_text"])
                    try:
                        with open(full, encoding="utf-8") as fh:
                            text = fh.read()
                    except OSError:
                        text = ""
                if text.strip():
                    out.append(("text", text.strip()))
            elif kind == "page_text":
                text = self._page_text(self._result, payload)
                if text.strip():
                    out.append(("text", text.strip()))
            elif kind == "image":
                out.append(("image", payload))
        return out

    def result_info(self):
        return self._result

    def closeEvent(self, event):
        # Never leave a running extraction thread behind a closed dialog.
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            worker.wait(3000)
        super().closeEvent(event)
