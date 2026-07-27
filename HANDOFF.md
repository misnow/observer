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
`fast_simplification 0.1.13`, `mediapipe 0.10.35`, `google-genai 1.36.0`,
`pymupdf 1.28.0`, `rapidocr-onnxruntime 1.4.4` (+ `onnxruntime 1.28.0`).

Run either app:
```
C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe studio.py
C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe main.py
```

---

## 3. Credentials — where they live, deliberately not written down

**No secrets are stored in this repo, and none belong here.**

Handling rules now enforced in code (see `tests/verify_gemini_segmenter.py`,
which asserts every one of these):

- The API key goes in the **`x-goog-api-key` header, never the URL**. A secret
  in a query string is recorded by proxy and server access logs. This was a
  real defect — `ui_components._run_gemini` used `?key=` until 2026-07-25.
- **Error text is redacted** before it is emitted (`ui_components.redact`,
  `segmenter._redact`). This matters here because LLM error strings are shown
  on screen *and* written into `vision_state.json` — an unredacted one would
  put a live key in a file on disk.
- **No save path serializes a key.** Project files never contain one.
- QSettings is the Windows registry (`HKEY_CURRENT_USER\Software\LightGuide`),
  not a file in the repo, so a key cannot be committed by accident.
- Key fields use `QLineEdit.EchoMode.Password`.

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

- **`canvas_items.py` split (done).** The 9 canvas item classes +
  `TriggerSettings` moved out of `studio.py`: 3738 → 2789 lines (190KB →
  147KB), new module 992 lines. Verified byte-identical against git HEAD
  (every class body, all 79 methods), then driven for real — all 8 asset
  types inserted through `insert_asset`, full save/load round trip (8 in, 8
  restored), real `main()` launch, clean exit, no Event Viewer entries.
  Covered by `tests/verify_canvas_items_split.py`.

- **Observatory/Studio fix round (done).** Covered by
  `tests/verify_observatory_studio_fixes.py`:
  1. *Camera switching* now moves the live feed. `currentIndexChanged` was
     wired only to tool-visibility, so picking a device left the old one
     streaming. `toggle_camera` was split into `start_camera_feed` /
     `stop_camera_feed`, reused by `on_camera_selection_changed`.
     `current_raw_frame` is cleared on stop so a capture straight after a
     switch can't save a frame from the previous camera.
  2. *Motion "Pixel Diff Thresh"* defaulted to **127** — half the full 0-255
     range, which ordinary motion never reaches, so nothing triggered until
     the slider was dragged fully left. Now 25. **The same `threshold` field
     is a minimum contour area for Blob Detection**, so the default is set
     per tool type in `SearchROI.__init__`, not globally — verified by
     `verify_blob_revert.py` still passing.
  3. *Capture/cutout locations* — the panel showed a bare basename. Now a
     read-only full path with a "reveal in Explorer" button, plus a cutout
     thumbnail beside the existing capture thumbnail.
  4. *Raw vs segmented source* — the cutout was **never broadcast** to
     Studio, so it had no route onto the canvas. Observatory now publishes
     `cutout_path`/`cutout_time` in the `captures` payload; the Studio
     capture item gained a `source_variant` picker that persists in the
     project file.
  5. *Studio File→New* — `new_file()` already existed but was only reachable
     internally. Added a confirmed `new_project()` wrapper to the File menu.

- **PDF/OCR subsystem (done).** `doc_extract.py` (engine) + `pdf_import.py`
  (dialog), reachable from Studio's "📄 Import from PDF" button. Covered by
  `tests/verify_doc_extract.py` and `tests/verify_pdf_import_ui.py`.
  - **New dependencies**, installed into the 3.12 runtime:
    `pymupdf 1.28.0`, `rapidocr-onnxruntime 1.4.4` (pulls `onnxruntime`,
    `shapely`, `pyclipper`, `tqdm`). No torch, no GPU, no system binary.
  - Text layer is read directly; only pages *without* one get OCR'd
    (`ocr="auto"`). **OCR costs ~2.4s/page on this CPU**, so a 500-page scan
    is ~20 minutes — it runs on a worker thread, reports progress, can be
    cancelled, and the dialog states the estimate before you commit.
  - Small docs (≤25 pages) come back in memory *and* on disk; larger ones
    write files only and are read on demand via `manifest.json`, so a huge
    scan never materialises entirely in RAM.
  - Repeated images (a header logo on every page) are de-duplicated by xref —
    written once, referenced many times. Without this a 500-page document
    would emit 500 identical PNGs.
  - **OCR is lossy**: measured 0.947 against ground truth, turning "ORDER"
    into "0RDER" while reading all digits correctly. Treat OCR'd text as
    needing review.
  - The **Gemini OCR backend is written but NOT verified end to end** — it
    needs a real API key to exercise. The rapidocr path is fully verified.
    Note a Google AI Pro subscription does *not* grant API access.

- **Gemini segmentation backend (done, verified live).** `segmenter.py` gained
  a `"gemini"` backend beside `grabcut`; selectable per Image Capture tool,
  with an optional "Subject" prompt. The UI states plainly that the capture is
  uploaded to Google.
  - **Verified against the real API**, not just mocked — which mattered. The
    documented mask format (a flat polygon) is only one of **five** shapes the
    model actually emits: a CSV string, a flat number list, a list wrapping a
    flat list, a list of `[x,y]` pairs, and a list of rings of pairs. The
    request now pins a `responseSchema` to stop the variance, and `_as_rings`
    accepts all five anyway.
  - **A bug the offline tests missed.** The decoder chose between the
    whole-image and box-relative coordinate readings by "how much lands inside
    box_2d" — but a box-relative polygon is inside the box *by construction*
    and always scored 1.0, so it always won and every mask came back shrunk to
    roughly a quarter of its area (4–11% coverage against a true 20.8%). The
    offline suite passed regardless, because a polygon strictly inside the box
    ties and the tie-break happened to pick right; only a live response, whose
    polygon fills box_2d exactly, broke the tie. Now scored by extent
    agreement, with a regression test for exactly that case.
  - **Quality varies run to run**: IoU 0.574, 0.751, 0.801, 0.825, 0.837 over
    five calls on the same synthetic fixture. Expect variance; a *consistently*
    low score would indicate decoding, not the model.
  - Gemini has no bounding-box prompt, so the tool's ROI selects among the
    objects it returns rather than constraining it.

- **LLM activation token ($ACTIVE) (done).** An LLM Vision tool can now score
  on the *content* of the reply: ask the model to answer with a sentinel when
  a condition holds, and seeing it drives the score to 100. Covered by
  `tests/verify_llm_activation_token.py` (offline) and
  `verify_llm_activation_live.py` (one real call).
  - Previously `handle_llm_result` set `current_score = 100` for **any**
    reply — the score meant "a response arrived", not "the condition is
    true" — and LLM tools never touched `tool_states` at all, so they could
    not gate a Studio **Vision Wait**. Both now work when a token is set.
    With the mode left `Off` the old behaviour is unchanged.
  - **`llm_prompt` was never saved.** A configured prompt silently reverted
    to the default on reload, which made this feature unusable. Now persisted
    along with the token settings.
  - **Two findings that only live calls produced:**
    1. Asked to "reply with only `$ACTIVE`", gemini-2.5-flash answered
       `ACTIVE` — right judgement, but it **dropped the `$`** (models treat it
       as markup). Matching is therefore done on the token's *alphanumeric
       core* with word boundaries, so `$ACTIVE` matches `ACTIVE` but still
       not `proactive`.
    2. The free tier rate-limits bursts, and a 429 has **two causes that look
       identical**: a per-minute limit that clears in about a minute, and the
       daily cap that does not. Both were hit on 2026-07-25. Retry once; if it
       survives a couple of minutes it is the daily cap — not a defect and not
       a dead key.
  - **Live status:** both directions confirmed, in separate runs (the daily
    cap intervened before one run could cover both). Person image → the model
    replied `ACTIVE` → activates. Empty image → replied `NONE` → does not
    activate. Re-run `verify_llm_activation_live.py` on a fresh quota day to
    see both in a single pass.
  - **Mode matters.** `Exact reply` is recommended: the whole reply must be
    the token, prompted as *"reply with only $ACTIVE if yes, or only NONE if
    no"*. `Ends with` fits the natural phrasing but cannot fully separate a
    refusal — *"no person, so I will not output $ACTIVE."* genuinely ends
    with the token. A negation guard on the final clause catches the common
    phrasings, but it is a heuristic. `Contains` fires on any mention.

- **Observatory layout: the camera view is now protected (done).** The
  inspector had grown until it crowded out the video feed and its ROIs.
  Covered by `tests/verify_observatory_layout.py`, which measures real pixel
  widths rather than eyeballing.
  - Inspector lives in a **QScrollArea** (`setWidgetResizable(True)`,
    horizontal scrolling off) capped at 460px; the camera view has a 420px
    hard minimum, `setCollapsible(False)`, and the stretch factor, so spare
    width goes to the video. Measured: camera holds 772px at 1450px wide with
    every tool selected, and still 420px in a cramped 1000px window.
  - **Global Configuration is collapsible and starts collapsed** — it sat
    above the tallest panel in the app (LLM Vision) and is set once.
  - **Unwrapped `QLabel`s were a major cause.** A `QLabel` does not wrap by
    default and reports its full single-line width as its *minimum* size hint,
    so one long help sentence set the floor for the whole column. All help
    texts now go through `_hint()`, which wraps.
  - **Stale widgets were painting over new ones** — see the `clear_layout`
    section in CLAUDE.md. This is very likely what "half the LLM tool is not
    legible" actually was. Fixed in *both* apps.
  - Group title had a literal `&`, which Qt ate as a mnemonic.
  - **Not done:** moving the inspector to the bottom-left, or the global
    config to the left panel. The scroll + protected-view approach solved the
    crowding without relocating anything, so relocation stays available but
    unspent.

- **Studio layout: properties column now scrolls (done).** Same treatment as
  Observatory. Covered by `tests/verify_studio_layout.py`.
  - Properties column in a **QScrollArea** capped at 460px, horizontal
    scrolling off; centre pane (sequence tree / run preview) gets a 420px
    minimum, `setCollapsible(False)` and the stretch factor.
  - **AI Assistant is collapsible and starts collapsed** — it sat at the
    bottom of the same column and holds set-once provider settings.
  - Measured content heights against a 619px viewport: Text 704, Image 622,
    Shape 716, Shape SVG 676, Capture 820, LLM 620, **3D Model 1106**, HTML
    432. **7 of 8 overflow** — all of that used to run off the bottom and was
    simply unreachable.
  - All six long help labels now wrap (`_hint()`), plus the 3D **Live Pose**
    readout, which is column-aligned so it cannot wrap — it got a smaller
    font instead, since at the default size its minimum width alone exceeded
    the whole column.

- **LGS Web API client (`lg_api.py`, done — but paths need confirming).**
  Talks to **Light Guide Systems**, the separate commercial product that
  shares this project's name. Covered by `tests/verify_lg_api.py` (offline,
  40 checks) and `tests/verify_lg_api_live.py` (run on the LGS box).
  - **Port 54274**, from the wiki: *"Port favored for Light Guide Web API
    (spells LG API on phones)"*. Not 54321. **54448 is LGS's general TCP/IP
    port and does not serve the Web API** — a likely source of confusion.
  - The LGS machine is a **different PC**, so host/port are configurable and
    persist via QSettings (`lg_api_*` keys).
  - **Only `/Programs/Run` is a verified path** (read off a live Postman
    session). The other 13 are inferred from request names and are flagged
    `verified=False`. `LGClient.load_collection()` imports the LGS Postman
    JSON — which the wiki tells you to download — and overrides the guesses.
    **Do that before trusting any inferred path.**
  - `run_program` / `shutdown` / `restart` require `confirm=True`; they act on
    equipment in front of an operator. `discover()` is GET-only.
  - **Not yet done:** the reverse direction (LGS driving this app). LGS's Web
    API is for controlling LGS; making it call *us* needs either an HTTP
    listener on our side or LGS-side outbound docs I do not have. Also not
    wired into any UI yet — this is the client library only.

**Known open:**
1. **Studio crashes with a native access violation when switching between
   certain asset properties panels.** Found while testing the layout work;
   **pre-existing** — reproduces at `ce09819`, before any of it.
   - Repro: insert a 3D Model asset and another asset, then click between
     them in the sequence tree. `tests/../scratchpad/minimal_crash.py`-style
     two-asset probes caught `model -> capture` and `model -> html`; an
     eight-asset tree also died on `model -> text` and on *closing* a window
     holding an HTML or 3D Model asset.
   - **Timing-dependent**: it vanishes under `sys.settrace`, so it is a race
     against `deleteLater()` servicing rather than a deterministic path. The
     3D Model panel is the common factor; the HTML item's QWebEngineView
     aggravates it. `faulthandler` DOES catch this one ("Windows fatal
     exception: access violation", innermost frame a `studio.py` `wrapper`,
     i.e. inside a `safe_slot`) — unlike the `qFatal()` abort, which is
     silent.
   - Very likely the same family as the exit-time `0xC0000409` below.
   - `verify_studio_layout.py` sidesteps it deliberately and says so: one
     asset per fresh window, HTML excluded, windows never closed, and
     `os._exit()` at the end.
2. **Two preserved tests are stale** (they fail identically at HEAD — not
   regressions): `verify_model3d.py` predates Ctrl-drag and its fake event
   lacks `modifiers()`; `verify_worker_crashfix.py` still expects
   `InteractiveCaptureItem.roi`, deliberately removed in the Image Capture
   migration. `verify_blob_revert.py` merely has a dead log path (exits 127
   with empty output — see CLAUDE.md on why that code lies); with the path
   redirected it passes in full.
2. **Intermittent `0xC0000409` on exit.** Still unresolved. New evidence
   from `%LOCALAPPDATA%\CrashDumps`: the dumps arrive in **pairs seconds
   apart with different memory footprints (~75MB and ~105MB)** — i.e.
   Studio *and* Observatory going down together, not one process. Recorded
   on 2026-07-25 at 11:39, 14:37, 17:35, 20:30 and 20:57. The 20:57 pair is
   the session that produced the AVI/h264+mp3 traceback.
   **Untested hypothesis:** `closeEvent` calls `stop_video()`, which stops
   the player but never clears `setVideoOutput(...)`. The
   `QGraphicsVideoItem` is a *child* of the media item, so Qt can destroy
   the sink while `QMediaPlayer` still points at it; `QAudioOutput` teardown
   ordering is a second candidate. **A lead, not a diagnosis** — it did not
   reproduce across a full suite sweep plus a real `main()` launch.
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

   **There is a second, different crash that faulthandler DOES catch**: a
   genuine access violation (exit **139** under Git Bash, not 0xC0000409)
   while switching between asset properties panels.
   `faulthandler.enable(file=...)` prints "Windows fatal exception: access
   violation" with the innermost frame in `studio.py`. So: output means this
   one; silence means the `qFatal()` abort. It is timing-dependent and
   disappears under `sys.settrace`, so instrument it with a **streamed,
   per-line-flushed log**, never a tracer — and never a log written only at
   the end, which vanishes entirely when the process dies.

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

The `canvas_items.py` split is **done** (see §4). `studio.py` is now the app
shell + sequence engine + properties panels.

Pick from:

- **Fix the two stale tests** (§4 item 1). Small, and it restores real
  regression coverage on 3D pose and QThread lifecycle — the latter guards
  one of the three documented crash causes.
- **Split `studio.py` further** if it grows again. Next natural seams: the
  sequence/run engine, and the properties-panel builders. The method that
  worked: AST-locate the exact node span, extract verbatim, delete by
  **explicit content replacement with an assert on match count** (never
  index-range slicing), then prove nothing moved by diffing every class body
  against `git show HEAD:studio.py`. That verification script is worth
  rewriting each time — it's what turns a scary refactor into a boring one.
- **Chase the exit-time `0xC0000409`** (§4 item 2).

Whatever you pick: **commit before starting.**
