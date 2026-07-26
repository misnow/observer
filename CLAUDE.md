# LightGuide — project notes

Two cooperating PyQt6 apps for industrial vision + projection.

- **`studio.py`** — Authoring Studio. Sequence editor + projection output canvases.
- **`main.py`** — Observatory. Cameras, vision tools (ROIs), LLM vision, image capture.

They are **separate processes** talking over files in the shared working
directory: `studio_command.json` (Studio → Observatory) and
`vision_state.json` (Observatory → Studio). Both are `.gitignore`d — they
churn constantly at runtime.

Supporting modules (deliberately standalone, keep them that way):

| File | Purpose |
|---|---|
| `canvas_items.py` | Canvas assets: Text/Media/Shape/3D/Capture/HTML/LLM items, `AnimatableMixin`, `MediaResizeHandle`, `TriggerSettings` |
| `doc_extract.py` | PDF text/image scraping + OCR (PyMuPDF, pluggable OCR backends) |
| `pdf_import.py` | The PDF import dialog; hands Studio a list of assets to insert |
| `vision_tools.py` | `SearchROI`, `PerspectivePlaneROI`, handles, image b64 helpers |
| `ui_components.py` | `DARK_THEME`, `LLMWorker`, `LLMProviderSettingsWidget` |
| `model3d.py` | 3D load (.stl/.step/…) + software renderer (numpy + QPainter) |
| `segmenter.py` | Object segmentation, pluggable backends (grabcut / SAM) |
| `mesh_to_step.py` | Faceted STEP (ISO-10303-21) export from a triangle mesh |

---

## ⚠️ The crash that will bite you

**Exit code `-1073740791` / `0xC0000409`, faulting `Qt6Core.dll` offset `0x1cf68`.**

This PyQt6 build escalates **any unhandled Python exception raised inside a
slot invoked through Qt's meta-object system** to `qFatal()`/abort. You get
no traceback, and `faulthandler` prints nothing. It looks like a native
crash; it is almost always ordinary Python (`AttributeError`, `RuntimeError`).

Recurring causes, all seen in this project:

1. **`clicked(bool)` overwrites the first positional parameter.**
   ```python
   def browse(i=item, lbl=label): ...     # PyQt6 passes the click's bool into `i`
   btn.clicked.connect(browse)            # → i is False → False.attr → abort
   btn.clicked.connect(self.safe_slot(browse))   # ✅ safe_slot takes no args
   ```
   This one bug has appeared **five separate times**. Always wrap button
   handlers in `self.safe_slot(...)`.

2. **Cross-thread signal landing on a deleted C++ object.** Properties panels
   are rebuilt constantly (`clear_layout` → `deleteLater`). A worker
   finishing later touches a dead widget → `RuntimeError` → abort. Guard
   every widget touch in a worker callback with `try/except RuntimeError`,
   and update the *data model* first and separately so results survive a
   torn-down panel.

3. **Dropped `QThread` references.** `self._worker = w` on a second call
   garbage-collects a still-running thread. Keep workers in a **list**,
   remove on completion.

`safe_slot(fn, *args)` wraps a callable, catches exceptions, logs them, and
crucially **accepts no signal arguments** — which is what defuses cause #1.

---

## Hard-won facts

- **Runtime is Python 3.12** (`C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe`).
  A bare `python` on PATH is **3.13** with a *different* site-packages.
  Installing to the wrong one is a silent, confusing failure. Always use the
  3.12 path explicitly.
- **No usable GPU** (Intel UHD 620). Anything GPU-hungry is impractical:
  local vision LLMs take 10–40s, and 3D is software-rendered for this reason.
  OCR is CPU-only too: **~2.4s per A4 page at 200dpi**, which is why
  `doc_extract` only OCRs pages that have no text layer — a 500-page scan is
  a 20-minute job, so it runs on a worker thread and warns before starting.
- **IPC files must be written atomically** (temp + `os.replace`). Plain
  `open(...,"w")` truncates first; the other process polls every 50–500ms and
  will read the empty window.
- **`exit code 127` with empty output is unreliable here** — cross-check
  Windows Event Viewer (`Application Error`) for a real crash. Also beware
  `cmd | tail` — `$?` then reports *tail's* status, not Python's.
- **QtWebEngine** must have `AA_ShareOpenGLContexts` set before
  `QApplication` is constructed (done in `studio.main()`); the import is lazy
  so nothing else inherits that constraint.

---

## Editing this codebase

`studio.py` is ~147KB (was ~190KB before the canvas items were split out).
**Never patch it by index-range slicing**
(`s[:s.index(A)] + s[s.index(B):]`) — that silently deletes everything
between the markers. It destroyed `Interactive3DModelItem` and
`sync_llm_call_displays` in one session.

The dependency runs **one way**: `studio` imports `canvas_items`, never the
reverse. `TriggerSettings` lives in `canvas_items.py` (only the item classes
construct one) and is re-exported from `studio` so `studio.TriggerSettings`
still resolves — several `tests/` scripts reach names through `studio`, so
that re-export is load-bearing, not decoration.

Use the `Edit` tool (exact match, fails loudly). If scripting is
unavoidable, use **pure insertion against a unique anchor**, or explicit
content replacement with an assert on match count.

**Commit before substantial changes**, not just after.

---

## Handling API keys

Both apps read provider keys from `QSettings("LightGuide", "Observatory")` —
the Windows registry, never a file in this repo. Three rules, each enforced by
assertions in `tests/verify_gemini_segmenter.py`:

1. **Header, never URL.** `x-goog-api-key`, not `?key=`. Query strings land in
   proxy and server logs. `ui_components._run_gemini` got this wrong until
   2026-07-25.
2. **Redact before emitting.** `ui_components.redact()` / `segmenter._redact()`
   scrub the key from any error string. LLM errors are shown on screen *and*
   written into `vision_state.json`, so an unredacted one puts a live key on
   disk.
3. **Never serialize a key** into a project file.

---

## Verification habits that paid off

- Drive the real code path, not a mock. Several bugs only appeared through a
  genuine button click or a real two-process IPC round trip.
- **Treat silent failure as a defect.** Three separate bugs hid behind quiet
  fallbacks: a swallowed IPC exception, a decimation no-op, a segmentation
  returning 0% coverage. Each now reports itself.
- Assert on *quality*, not just absence of exceptions — e.g. segmentation is
  checked against a known-truth mask (IoU), STEP export is re-imported
  through OpenCASCADE and volume-checked.
- **Mocks agree with your assumptions; live services don't.** The Gemini
  segmenter passed a full mocked suite while silently shrinking every mask to
  a quarter of its area — the offline fixtures happened to tie the decision it
  got wrong. One real API call exposed it, and revealed the model returns the
  mask in five different shapes. When a backend is reachable, verify against
  it once and turn what you learn into an offline regression test.
- **Models don't echo tokens literally.** Asked to "reply with only
  `$ACTIVE`", gemini-2.5-flash replied `ACTIVE` — correct, but the `$` was
  gone, and a literal string match scored the right answer as a miss. Match
  the alphanumeric core with word boundaries. Related: a 429 from the free
  tier is usually a *per-minute* limit that clears in about a minute, not a
  dead key — check before concluding anything.

---

## Known open items

- `studio.py` could be split further if it grows again — the sequence engine
  and the properties-panel builders are the next natural seams. (The canvas
  item classes are already out, in `canvas_items.py`.)
- Intermittent `0xC0000409` on exit still unresolved; `closeEvent` stops web
  views, videos, and waits on workers, but a repro hasn't been pinned down.
- `segmenter.py` supports SAM3/SAM2 detection but its predictor API is
  **not wired** — it raises a clear message rather than guessing at an
  unverified API. SAM3 is real (`facebookresearch/sam3`); PyPI packages of
  that name are third-party repackagings. (The `gemini` backend *is* wired
  and verified live.)
- No local image→3D model. That step is external (TripoSR local, or
  Tripo/Meshy API). Everything either side of it is built.
