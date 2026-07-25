# LightGuide — session handoff

Paste this into a new chat to pick up cleanly. `CLAUDE.md` in this repo
auto-loads and carries the architecture + crash notes; this file carries
**current state, environment, and what to do next** so they aren't repeated.

---

## 1. Read this first

`CLAUDE.md` (same folder) documents:
- What the two apps are and how they talk (file IPC)
- **The `0xC0000409` crash mechanism** — the single most expensive thing to
  rediscover
- The `clicked(bool)` first-positional-parameter trap (caused 5 crashes)
- Editing rules for `studio.py` (never index-range slice)

Don't re-derive those. They cost a full session to learn.

---

## 2. Environment — get this right or you'll waste an hour

| Thing | Value |
|---|---|
| **Runtime interpreter** | `C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe` |
| Bare `python` on PATH | **3.13 — WRONG**, different site-packages |
| GPU | None usable (Intel UHD 620). Assume CPU-only. |
| OS / shell | Windows 11, Git Bash available + PowerShell |

Installing to 3.13 by accident is a silent failure that looks like "the
package isn't installed." **Always invoke the 3.12 path explicitly.**

Installed in 3.12: `PyQt6 6.11.0`, `PyQt6-WebEngine 6.11.0`, `opencv`,
`numpy 2.4.6`, `trimesh 4.12.2`, `cadquery 2.8.0` (bundles OpenCASCADE),
`fast_simplification 0.1.13`, `mediapipe 0.10.35`.

Run either app:
```
C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe studio.py
C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe main.py
```

---

## 3. Credentials — where they live, deliberately not written down

**No secrets are stored in this repo, and none belong here.**

All provider config lives in Windows `QSettings("LightGuide", "Observatory")`
— a namespace **shared by both apps**, so configuring in either affects both.
Keys: `llm_provider`, `gemini_api_key`, `openai_base_url`, `openai_model`,
`openai_api_key`, `llm_timeout_seconds`, `auto_launch_observatory`.

Current working config: local Ollama at `http://localhost:11434/v1`, model
`moondream`, watchdog timeout `60s`. A Gemini key is present but redacted.

⚠️ **Test scripts must not write to this namespace.** Mine did, and left the
base URL pointing at a non-routable test address until it was restored. If a
test needs settings, use a throwaway namespace like
`QSettings("LightGuideTest", "X")`.

---

## 4. Current state

Git repo initialised (3 commits). Working tree clean at `7f62b87`.

**Working and verified:**
- Studio: sequence engine, multi-canvas (1–5), Text/Image/Video/Shape/HTML/
  3D Model/Capture/LLM assets, animation presets, User Wait buttons,
  curved text, Run Mode step highlighting
- Observatory: vision tools, per-tool camera binding, Image Capture tool
  (ROI + full frame), LLM watchdog, File→New
- `mesh_to_step.py` — faceted STEP export, **validated by re-importing
  through OpenCASCADE** (valid solid, exact volume on a box)
- `model3d.py` — software 3D renderer (~6.7ms for 1200 faces)
- `segmenter.py` — GrabCut backend, **IoU 0.932 vs ground truth**
- Atomic IPC writes (4319 reads under hammering, zero corrupt)

**Known open:**
1. **`studio.py` is 190KB / 21 classes and needs splitting.** This is the
   next planned task. Canvas item classes → own module. Safe now that git
   exists. *Do this with a fresh context budget — running low is exactly how
   two methods got deleted.*
2. **Intermittent `0xC0000409` on exit.** Unresolved. Last occurrence logged
   Observatory auto-capturing during shutdown, suggesting a timer still
   firing during teardown — **a lead, not a diagnosis.** `closeEvent` already
   stops web views, videos, and waits on workers.
3. `segmenter.py` detects SAM3/SAM2 but its predictor API is **not wired** —
   raises a clear message rather than guessing at an unverified API. SAM3 is
   real (`facebookresearch/sam3`, 11k stars); PyPI packages of that name are
   third-party repackagings, so verify before installing.
4. **No local image→3D step.** External: TripoSR (local, CPU-viable, low
   quality) or Tripo/Meshy API. Everything either side of it is built.
5. Two small modules could merge into one support module if desired.

---

## 5. Debugging toolkit that actually worked

**The crash is almost never native.** `0xC0000409` = a Python exception
inside a Qt-invoked slot. To find it:

1. **Event Viewer is ground truth** (exit codes here lie):
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'} -MaxEvents 5
   ```
   Signature to expect: `Qt6Core.dll`, offset `0x1cf68`, code `0xc0000409`.
   Compare timestamps against when you ran something.

2. **`faulthandler` prints nothing** for this crash — it's `qFatal()`/abort,
   which bypasses Python. Its silence *confirms* the diagnosis.

3. **Bypass Qt's dispatch to see the real traceback.** Calling a handler
   directly raises normally; going through `btn.click()` aborts. That
   difference is how the `clicked(bool)` bug was finally caught.

4. **Beware `cmd | tail`** — `$?` reports *tail's* status, not Python's. This
   made a hard crash look like a clean pass. Redirect to a file instead.

5. **Exit code 127 with empty output is unreliable here.** Cross-check
   Event Viewer before concluding anything.

6. `cdb.exe`/WinDbg is **not installed**; dumps land in
   `%LOCALAPPDATA%\CrashDumps`. Reproducing beats dump-reading anyway.

**Test pattern that caught real bugs** (several under `scratchpad/`):
- Run under the **3.12** interpreter, `os.chdir()` to an isolated temp dir so
  IPC files don't collide with the user's real session
- Stub `AuthoringInterface.launch_observatory` **before constructing** it, or
  a real Observatory subprocess spawns and overwrites `vision_state.json`
- Write results to a log file, not stdout (encoding + pipe issues)
- Drive real code paths — real button clicks, real two-process IPC
- **Assert on quality, not just absence of exceptions**: segmentation vs a
  known-truth mask (IoU), STEP re-imported and volume-checked. Three bugs
  hid behind silent success: a swallowed IPC exception, a decimation no-op,
  a 0%-coverage segmentation.

---

## 6. Suggested first move

Split `studio.py`. Proposed: canvas item classes (`InteractiveTextItem`,
`InteractiveMediaItem`, `InteractiveShapeItem`, `InteractiveHTMLItem`,
`Interactive3DModelItem`, `InteractiveCaptureItem`, `InteractiveLLMTextItem`,
`AnimatableMixin`, handles) → `canvas_items.py`, leaving `studio.py` as the
app shell + sequence engine.

**Commit before starting.** Move by **pure insertion + explicit deletion of
matched content**, never index-range slicing. Verify with the existing
scratchpad suites after each move.
