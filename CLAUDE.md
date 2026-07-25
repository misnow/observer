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

`studio.py` is ~190KB. **Never patch it by index-range slicing**
(`s[:s.index(A)] + s[s.index(B):]`) — that silently deletes everything
between the markers. It destroyed `Interactive3DModelItem` and
`sync_llm_call_displays` in one session.

Use the `Edit` tool (exact match, fails loudly). If scripting is
unavoidable, use **pure insertion against a unique anchor**, or explicit
content replacement with an assert on match count.

**Commit before substantial changes**, not just after.

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

---

## Known open items

- `studio.py` needs splitting (canvas item classes → own module). Safe now
  that git exists.
- Intermittent `0xC0000409` on exit still unresolved; `closeEvent` stops web
  views, videos, and waits on workers, but a repro hasn't been pinned down.
- `segmenter.py` supports SAM3/SAM2 detection but its predictor API is
  **not wired** — it raises a clear message rather than guessing at an
  unverified API. SAM3 is real (`facebookresearch/sam3`); PyPI packages of
  that name are third-party repackagings.
- No local image→3D model. That step is external (TripoSR local, or
  Tripo/Meshy API). Everything either side of it is built.
