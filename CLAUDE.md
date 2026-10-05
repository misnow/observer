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
| `lg_api.py` | Client for the **external** Light Guide Systems Web API (port 54274) |
| `calibration.py` | Projector<->camera homography: pattern generation, detection, quality-gated solve |
| `canvas_display.py` | Which physical screen each output canvas fills, and the Canvas Setup dialog |


## Where the other documentation lives

| File | Read it when |
|---|---|
| `SETUP.md` | Standing up a new machine from a bare clone — prerequisites, install (including an air-gapped wheel-bundle route), verification, per-machine config |
| `ROADMAP.md` | Planned work, known defects and stubs, each with the file it lives in |
| `HANDOFF.md` | Current state and the session-by-session record of what changed and why |
| `tests/README.md` | What each of the 27 verification scripts covers, and the conventions they follow |

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

### There is a *second*, different crash — tell them apart

A genuine **access violation**, exit code **139** under Git Bash, hit by
clicking between certain asset properties panels in Studio.

|  | `qFatal()` abort | Access violation |
|---|---|---|
| Exit code | `-1073740791` / `0xC0000409` | `139` |
| `faulthandler` | prints **nothing** | **prints a traceback** |
| Cause | ordinary Python exception in a Qt slot | real memory error — a dead C++ object |

So **output from `faulthandler` means you are looking at the second one**, and
silence confirms the first. The second is timing-dependent and disappears under
`sys.settrace`, so instrument it with a streamed, per-line-flushed log rather
than a tracer. Details and repro: ROADMAP.md §1a.

---

## Rebuilding a properties panel

`clear_layout()` must **hide + reparent** each widget, not just `deleteLater()`
it:

```python
widget.hide(); widget.setParent(None); widget.deleteLater()
```

`takeAt()` only removes a widget from *layout management*. It stays parented
and keeps painting at its old geometry until `deleteLater()` is serviced on the
next event-loop pass, so a rebuilt panel drew the outgoing widgets **on top of**
the incoming ones — text overlapping text, half-illegible. Keep
`deleteLater()` for the actual destruction: deleting synchronously here is
unsafe when the call comes from inside a widget's own signal handler.

Both apps had this. `tests/verify_observatory_layout.py` guards it by asserting
the panel's child count doesn't grow across repeated rebuilds.

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

## The *other* LightGuide

`lg_api.py` talks to **Light Guide Systems (LGS)**, a separate commercial
product that happens to share this project's name. In that module "LG" always
means the external product.

- **Port 54274**, per the LGS wiki: *"Port favored for Light Guide Web API
  (spells LG API on phones)"*. **54448 is a different thing** — LGS's general
  TCP/IP port — and does not serve the Web API.
- The LGS box is normally **another PC on the line**, so the host is
  configurable; never assume localhost.
- Replies come wrapped: `{"Verb", "Endpoint", "ReturnType", "ResponseItem"}`.
- **Only `/Programs/Run` is a verified path.** Everything else in
  `ENDPOINTS` is inferred from request *names* and carries `verified=False`.
  The authoritative source is the Postman collection the wiki tells you to
  download — `LGClient.load_collection()` reads it and overrides the guesses.
  The offline test demonstrates why: a plausible-looking
  `/Window/Focus/Canvas` guess loses to a real `/Application/Window/FocusCanvas`.
- `run_program`, `shutdown` and `restart` drive industrial equipment in front
  of an operator, so they require `confirm=True`. `discover()` is GET-only for
  the same reason — probing with POST would start work instructions.

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
  the alphanumeric core with word boundaries.
- **Gemini free-tier 429s come in two flavours** and they look identical.
  A *per-minute* limit clears in about a minute; the *daily* cap does not.
  Both observed on 2026-07-25 — the first cleared on retry, and later the same
  day two 65s waits still returned 429, which is the daily cap, not a defect
  and not a dead key. Distinguish by retrying once: if it survives a couple of
  minutes, stop testing against the live API until tomorrow.

---

## Known open items

Tracked in **ROADMAP.md**, with the file each one lives in. The headlines:

- **Two live crashes** — the exit-time `0xC0000409`, and the properties-panel
  access violation above. Both intermittent, neither pinned down.
- **"Human Rigging (Pose)" is a stub** — it is in the tool dropdown and the
  tracker is constructed, but nothing processes frames for it.
- **Not started:** human identifier / pattern library, undo, the calibration
  wizard (the maths is built and verified; only the live orchestration is
  missing), image→3D.
- `segmenter.py` detects SAM3/SAM2 but its predictor API is **not wired** — it
  raises a clear message rather than guessing at an unverified API. SAM3 is
  real (`facebookresearch/sam3`); PyPI packages of that name are third-party
  repackagings. The `gemini` backend *is* wired and verified live.
