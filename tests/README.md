# Verification scripts

Ad-hoc end-to-end checks written while building each feature. Not a unit
test suite — they drive the **real** code paths (real button clicks, real
two-process IPC, real camera frames) because that's where the bugs actually
were.

## Running

Use the 3.12 runtime, not a bare `python`:

```
C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe tests\verify_capture_migration.py
```

Each writes a `result_*.txt` log; the last line is the pass marker. **Check
the log, not the exit code** — exit codes are unreliable in this environment
(see `CLAUDE.md`).

## Paths

These carry absolute paths to a session temp folder that no longer exists.
Expect to fix the `RES`/`ISO` constants at the top of each before running.
The *logic* is the valuable part.

`verify_canvas_items_split.py` is the exception — it derives the project root
from its own `__file__` and makes its own temp dir, so it runs as-is and its
exit code is meaningful (0 = pass).

## Conventions worth keeping

- `os.chdir()` into an isolated temp dir — IPC files are relative, and
  sharing a directory with a live session causes real interference.
- Stub `AuthoringInterface.launch_observatory` **before** constructing it, or
  a real Observatory subprocess spawns and overwrites `vision_state.json`.
- Never write to `QSettings("LightGuide", "Observatory")` — that's the live
  app config. Use a throwaway namespace.
- Assert on **quality**, not just absence of exceptions: IoU against a known
  mask, STEP re-imported and volume-checked. Three real bugs passed
  "it didn't crash" while producing garbage.
- When a result is *inherently* lossy, assert a **measured floor** rather than
  equality — and log the actual value so a regression stays visible. OCR is
  the example: it scores 0.947 on the fixture and reads "ORDER" as "0RDER".
  The test asserts accuracy ≥ 0.90 *and* separately requires the digits to be
  exact, instead of loosening the check until it goes green.

## What each covers

| Script | Covers |
|---|---|
| `verify_capture_migration.py` | Observatory Image Capture → Studio reference over IPC, legacy project load |
| `verify_multicanvas.py` | 1–5 canvases, per-asset assignment, Clear Canvas targeting, shrink-rescue |
| `verify_multicamera.py` | Per-tool camera binding, visibility filtering, File→New across cameras |
| `verify_model3d.py` | 3D load/render/pose/save (predates Ctrl-drag; its fake event lacks `modifiers()`) |
| `verify_worker_crashfix.py` | QThread lifecycle: rapid reloads, deleted-item delivery, torn-down panels (predates the Image Capture migration; still expects `InteractiveCaptureItem.roi`, which deliberately no longer exists) |
| `verify_canvas_items_split.py` | `canvas_items.py` split: name surface, one-way dependency, every asset type inserted + save/load round trip |
| `verify_observatory_studio_fixes.py` | Camera switching moves the feed, motion threshold default (incl. proof the old 127 was blind to a realistic delta), capture/cutout paths + thumbnails, raw-vs-cutout picker over real IPC, Studio File→New. Also asserts the live QSettings namespace is untouched |
| `verify_llm_activation_token.py` | `$ACTIVE` scoring: matcher across all modes incl. the refusal case (*"I will not output $ACTIVE"*), token-fidelity (a reply of `ACTIVE` must match a token of `$ACTIVE`) and word boundaries, score/state wiring, persistence, legacy projects. No key needed |
| `verify_llm_activation_live.py` | **Two real API calls.** Whether a real model actually complies with the token instruction — one image with a person, one without |
| `verify_gemini_segmenter.py` | Gemini segmentation offline: **API-key safety** (never in URL/body, redacted from every error), mask decoding by IoU across all five observed shapes, ROI-based object choice, loud failure modes, grabcut regression. No key or network needed |
| `verify_gemini_segmenter_live.py` | **Makes one real API call.** The only thing mocks can't check: whether Google's live response matches the documented shape. Sends a synthetic image only, never prints the key, and reports "no key configured" cleanly if there isn't one |
| `verify_doc_extract.py` | PDF scraping: probe, text layer vs OCR, OCR against ground truth, image extraction + logo de-duplication, manifest round trip, inline vs folder mode, cancellation, loud errors |
| `verify_pdf_import_ui.py` | The import dialog on a real worker (probe summary, pick list, selections) and Studio's `import_pdf()` inserting real assets. Asserts **measured OCR accuracy ≥ 0.90** rather than exact equality — see below |
| `verify_run_mode_ui.py` | Run Mode panel hiding + active-step highlight |
| `verify_user_wait_vision_gate.py` | User Wait as an *additional* gate on top of Vision Wait |
| `verify_blob_revert.py` | Blob tool back to original single-blob behaviour |
