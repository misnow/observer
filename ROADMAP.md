# LightGuide — roadmap and open issues

What is planned, what is broken, and **where in the code each one lands**. The
file locations are the point: picking a task up in a new environment should not
start with half an hour of grep.

Status vocabulary:

- **Built & verified** — works, with a test that drives the real path.
- **Built, unverified against hardware** — logic proven offline; the physical
  half is untested.
- **Stub** — exists in the UI and does nothing.
- **Not started**.

---

## 1. Known defects

### 1a. Intermittent access violation when switching properties panels

**Severity: high — it kills the app during ordinary use.**

Clicking between certain asset properties panels in Studio crashes with a
Windows access violation (exit `139`), distinct from the `qFatal()` abort that
CLAUDE.md describes. `faulthandler` **does** catch this one and names a
`studio.py` frame inside a `safe_slot` wrapper.

- **Pre-existing** — reproduced at commit `ce09819`, before the layout work.
- **Timing-dependent** — it vanishes under `sys.settrace`, so it is a race
  against `deleteLater()` being serviced, not a fixed code path.
- The **3D Model panel is the common factor**; the HTML item's QWebEngineView
  makes it worse.
- Repro: insert a 3D Model asset and one other asset, then click between them
  in the sequence tree. Two-asset probes caught `model → capture` and
  `model → html`; a fuller tree also died on `model → text` and on *closing* a
  window holding an HTML or 3D Model asset.

**Where:** `studio.py` → `build_asset_properties()`, `clear_layout()`, and the
`Interactive3DModelItem` / `InteractiveHTMLItem` branches in `canvas_items.py`.

**Note:** `tests/verify_studio_layout.py` deliberately dodges this — one asset
per fresh window, HTML excluded, windows never closed, `os._exit()` at the end.
Each dodge is commented as such. It still trips the crash occasionally.

**Likely next step:** panel teardown already hides, reparents and defers
deletion. The remaining suspect is a `QGraphicsItem` child — the 3D model's
worker-backed repaint, or the proxy widget — being touched after its C++ object
is gone. Instrument with a **streamed, per-line-flushed log**, never a tracer,
which makes it disappear.

### 1b. Intermittent `0xC0000409` on exit

Long-standing. `closeEvent` stops web views and videos and waits on workers.
Crash dumps arrive in **pairs seconds apart with different memory footprints**
(~75 MB and ~105 MB) — Studio *and* Observatory going down together, not one
process. Dumps land in `%LOCALAPPDATA%\CrashDumps`.

**Untested hypothesis:** `stop_video()` stops the player but never clears
`setVideoOutput(...)`. The `QGraphicsVideoItem` is a *child* of the media item,
so Qt can destroy the sink while `QMediaPlayer` still points at it.
`QAudioOutput` teardown ordering is a second candidate.

**Where:** `studio.py` → `closeEvent()`; `canvas_items.py` →
`InteractiveMediaItem.stop_video()`.

### 1c. Two stale test scripts

Both fail identically at HEAD, so neither is a regression — their stubs predate
features they test.

- `tests/verify_model3d.py` — predates Ctrl-drag; its fake event has no
  `modifiers()`. **Fix:** give `FakeEv` a `modifiers()` returning
  `Qt.KeyboardModifier.NoModifier`.
- `tests/verify_worker_crashfix.py` — still expects
  `InteractiveCaptureItem.roi`, deliberately removed in the Image Capture
  migration. **Fix:** drop the ROI assertions; the capture item is now a
  *reference* to an Observatory tool and owns no ROI by design.

Everything else in `tests/` passes: **22 of 24** offline scripts, the two
above being the only failures. The other three scripts are live-only
(`*_live.py`) and need an API key, a network, or the LGS station.

---

## 2. Stubs — present in the UI, not implemented

### 2a. Human Rigging (Pose)

`MP_AVAILABLE` adds **"Human Rigging (Pose)"** to the Observatory tool dropdown
and `self.pose_tracker` is constructed in `__init__` — but there is **no
per-frame processing for it anywhere**. The tool can be placed and does nothing.

**Where:** `main.py` → the tool list, the `pose_tracker` init, and the
per-frame loop in `_update_frame_body()` where the other tool types are handled.

**Open design question:** what should it *produce*? The natural fit is to
publish joint coordinates the way blob geometry is now published
(`tool_geometry` → `vision_state.json`), so Studio can map them through the
projector calibration and place graphics on a person. That would also give the
human-identifier work (§3a) something to build on.

---

## 3. Planned features

### 3a. Human identifier tied into the blob tool

A saved **pattern library with lookup** that can also return coordinates —
"which of my known patterns is this, and where is it".

**Prerequisites that already exist:** blob geometry over IPC (`tool_geometry`),
the projector↔camera calibration, and Pattern Match's trained-template
machinery (`raw_template_b64` persistence, `train_pattern_tool`).

**Open decision — answer before coding.** Does this:

1. extend **Pattern Match** to hold *several* named templates and report which
   matched,
2. build on the **pose tracker** (§2a) for genuinely human-specific identity, or
3. become a **new matcher** with its own saved library?

Option 1 is the smallest step and reuses persistence that already works.

**Where:** `vision_tools.py` (`SearchROI` fields), `main.py` (per-frame
handling + properties panel), `canvas_items.py` if it needs a new canvas asset.

### 3b. Undo

Nothing exists today. Needs a command/undo-stack layer over asset edits and
sequence-tree edits — an architectural addition, not a bolt-on, because edits
are currently applied directly to Qt objects from signal handlers.

**Shape:** `QUndoStack` + `QUndoCommand` is the idiomatic Qt answer and gives
undo/redo and a history view for free. Every mutating path would have to route
through a command object.

**Where:** `studio.py` — `insert_asset()`, `delete_selected_tree_node()`, the
properties-panel setters, and the canvas item `set_*` methods in
`canvas_items.py`.

**Watch out:** the properties panel is rebuilt constantly, and its widgets write
straight to the model in `valueChanged` / `textChanged` handlers. Naively
pushing a command per signal would record one undo step *per keystroke*.
Commands need compressing (`QUndoCommand.mergeWith`) or pushing on edit
*completion* rather than on every change.

### 3c. Calibration wizard

The maths is **built and verified** (`calibration.py`): both pattern strategies,
detection, the quality gate, and a test that recovers a known homography to
0.65 px. What is missing is the **live orchestration**:

```
Studio projects pattern -> commands Observatory to capture ->
Observatory detects -> result returns -> Studio solves and stores
```

**Where:** needs a new `studio_command.json` verb alongside `trigger_llm` and
`capture_image`, handled in `main.py` → `check_for_studio_commands()`, driven
from a wizard dialog in Studio. `load_calibration()` and `save_calibration()`
already exist in `studio.py`, so a calibration can be loaded from JSON today.

### 3d. Light Guide Systems Web API — confirm the endpoints

`lg_api.py` is built and offline-tested, but **only `/Programs/Run` is a
verified path**; the other 13 are inferred from request names and carry
`verified=False`.

**Next step, on the LGS machine:**

```
python tests\verify_lg_api_live.py <lgs-ip> <port> [collection.json]
```

GET-only, so it cannot start a work instruction. Exporting the LGS Postman
collection and passing it as the third argument replaces every guess with the
real URL via `LGClient.load_collection()`.

**Note:** the target station is reported to use port **54321**, not the wiki's
favoured 54274. The port is per-install — the LGS Web API settings file
declares where it self-hosts.

### 3e. The reverse integration direction

Today the integration is one-way: this app calls the LGS Web API. Making LGS
drive *this* app needs either an HTTP listener on our side or LGS-side outbound
documentation, which is not in the wiki PDF.

### 3f. Image → 3D

No local step. External options: TripoSR (local, CPU-viable, low quality) or a
Tripo/Meshy API. **Everything either side of it is built** — `segmenter.py`
produces a clean cutout, and `mesh_to_step.py` exports STEP validated by
re-import through OpenCASCADE.

**Where:** between `segmenter.py` and `mesh_to_step.py`; `segmenter.py`'s
docstring already sketches the intended pipeline.

---

## 4. Smaller follow-ups

| Item | Where |
|---|---|
| Literal titled **"O"** output column in the tool list. Today it is a per-row colour icon; a titled column needs a `QTreeWidget`, which risks the rename-in-place and selection paths | `main.py` → `active_tools_list` |
| `studio.py` is ~160 KB again — next natural seams are the sequence engine and the properties-panel builders | `studio.py` |
| Fix the two stale tests (§1c). `verify_worker_crashfix` guards one of the three documented crash causes, so it is worth having back | `tests/` |
| SAM2/SAM3 predictor API is detected but not wired; it raises a clear message rather than guessing at an unverified API | `segmenter.py` → `_segment_sam()` |

---

## 5. Done and verified — so nobody rebuilds it

Canvas-to-screen assignment with a fallback that never overwrites saved config ·
canvas fit modes (fit / stretch / actual) · PDF text and image extraction with
OCR · Gemini segmentation backend, verified live · `$ACTIVE` LLM activation
tokens in both apps · blob detection rewrite (per-type scoring, Otsu, motion
mode, darkness bias) · projector↔camera calibration maths · blob-following
canvas text · timer countdown on canvas · LGS Web API client with key-safety
assertions · scrollable properties panels in both apps.
