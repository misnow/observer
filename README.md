# LightGuide

Two cooperating PyQt6 desktop apps for **industrial vision and projected work
instructions**: a camera/vision engine and an authoring tool that projects
guidance onto a work surface.

```
studio.py    Authoring Studio   sequence editor + projection output canvases
main.py      Observatory        cameras, vision tools (ROIs), LLM vision, capture
```

They run as **separate processes** and talk through two JSON files in the
shared working directory — `studio_command.json` (Studio → Observatory) and
`vision_state.json` (Observatory → Studio), written atomically and polled every
50–500 ms.

---

## Start here

| If you want to… | Read |
|---|---|
| **Set up a new machine from a clone** | **[SETUP.md](SETUP.md)** |
| Know what's planned, broken, or stubbed — and where it lives | [ROADMAP.md](ROADMAP.md) |
| Work on the code | [CLAUDE.md](CLAUDE.md) — architecture and the crash mechanisms that cost a session to find |
| See the session-by-session record | [HANDOFF.md](HANDOFF.md) |
| Run or extend the tests | [tests/README.md](tests/README.md) |

Quick start, assuming Python **3.12**:

```powershell
git clone https://github.com/misnow/observer.git lightproject
cd lightproject
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python tests\verify_canvas_items_split.py    # exit 0 = install is sound
python studio.py
```

No PyPI access on the target network? SETUP.md §3d covers the offline
wheel-bundle route.

---

## What it does

**Observatory** runs the cameras and the vision tools — region-of-interest
tools for blob detection, motion, pattern match, missing-object, image capture,
and LLM vision. Tools publish their state *and geometry* (a blob's centroid in
camera pixels) for Studio to consume.

**Studio** authors a sequence of steps, each showing assets on one or more
projection canvases: text, images, video, shapes, 3D models, live HTML, PDF
extracts, countdown timers, Observatory captures, LLM responses, and text that
**follows a part the camera found**. Steps advance on timers, vision
conditions, operator buttons, or an LLM activation token.

Notable pieces:

- **Projector↔camera calibration** (`calibration.py`) — project a pattern,
  detect it, solve the homography, so a graphic can be placed onto a real part.
  Two pattern strategies; the solve is quality-gated and refuses a bad fit.
- **PDF extraction with OCR** (`doc_extract.py`) — pull text and images out of
  a document and onto a canvas. Local CPU OCR; only pages without a text layer
  are OCR'd.
- **Pluggable backends** for segmentation, OCR and LLM providers, each
  reporting clearly when unavailable rather than failing obscurely.
- **External integration** (`lg_api.py`) — client for the separate commercial
  Light Guide Systems Web API.

---

## Design constraints worth knowing

- **CPU-only.** No usable GPU, so 3D is software-rendered, OCR runs ~2.4 s per
  A4 page, and local vision LLMs take 10–40 s. Nothing GPU-hungry is used.
- **Windows-specific.** Registry-backed settings, DSHOW cameras, native paths.
- **Python 3.12**, not 3.13 — see SETUP.md §2.
- **No secrets in the repo.** API keys live in the Windows registry and are
  asserted never to reach a URL, request body, error string or project file.

---

## Tests

27 verification scripts in `tests/`. They drive **real code paths** rather than
mocks — real button clicks, real two-process IPC round trips, real camera
frames — because that is where the bugs actually were. They assert on
*quality*, not just the absence of exceptions: segmentation against a
known-truth mask, a recovered homography against the one it was generated from,
OCR against ground truth.

```powershell
python tests\verify_blob_detection.py
```

Exit `0` is a pass, but **read the log** at `%TEMP%\verify_<name>.log` — exit
codes are unreliable in this environment, and the logs say what was measured.
