# Setting up LightGuide on a new machine

Everything needed to go from a bare `git clone` to both apps running. If a step
here is wrong, fix it here — this file is the one that gets followed on a
machine where nobody remembers how it was done the first time.

---

## 1. What you are installing

Two PyQt6 desktop apps that run as **separate processes** and talk through files
in the working directory:

```
studio.py   Authoring Studio   sequence editor + projection output canvases
main.py     Observatory        cameras, vision tools, LLM vision, capture
```

They find each other through `studio_command.json` and `vision_state.json` in
the **current working directory**. Both are gitignored. If the two apps are
launched with different working directories they will not see each other, and
nothing will say so — see §7.

---

## 2. Prerequisites

| Need | Detail |
|---|---|
| OS | Windows 10/11. Paths, `QSettings` (registry) and the DSHOW camera backend are Windows-specific. |
| Python | **3.12 specifically.** Not 3.13. |
| Disk | ~1.5 GB for dependencies (PyQt6 + Qt runtime, OpenCV, onnxruntime, cadquery/OpenCASCADE). |
| GPU | None needed. Everything is CPU-only by design. |

### Why 3.12 and not "whatever python is on PATH"

On the original machine a bare `python` resolves to **3.13 with a different
site-packages**. Installing into it looks like it worked and then every import
fails, which reads as "the package isn't installed". Always invoke the
interpreter by full path, or make a venv (§3) and use that.

Check what you actually have:

```powershell
py -0p
```

---

## 3. Install

### 3a. Clone

```powershell
git clone https://github.com/misnow/observer.git lightproject
cd lightproject
```

### 3b. Create a virtual environment (recommended)

A venv removes the whole 3.12-vs-3.13 problem — once it is active, plain
`python` means the right one.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -c "import sys; print(sys.version)"
```

`.venv/` is gitignored.

### 3c. Install dependencies — connected machine

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Without a venv, use the full interpreter path instead:

```powershell
C:\Users\mikes\AppData\Local\Programs\Python\Python312\python.exe -m pip install -r requirements.txt
```

### 3d. Install dependencies — **air-gapped / no PyPI**

If the target network has no PyPI access, build a wheel bundle on a connected
machine *of the same OS and Python version* and carry it across:

```powershell
# On a CONNECTED machine, same Windows + Python 3.12:
py -3.12 -m pip download -r requirements.txt -d wheelhouse
```

Copy `wheelhouse/` and the repo to the target, then:

```powershell
python -m pip install --no-index --find-links=wheelhouse -r requirements.txt
```

Notes for that case:

- `pip download` must run on the **same platform and Python version**. A Linux
  or 3.13 wheelhouse will not install here.
- The bundle is large — PyQt6 + Qt, OpenCV, onnxruntime and cadquery dominate.
- Trim first if you do not need everything. `cadquery` (STEP import/export) and
  `mediapipe` (the Pose tool) are the two big optional ones, and the app starts
  without either — see the comments in `requirements.txt`.
- **The Gemini backends cannot work without outbound internet.** LLM Vision,
  Gemini segmentation and Gemini OCR all call `generativelanguage.googleapis.com`.
  On a closed network use the local alternatives, which are deliberately the
  defaults: `grabcut` segmentation, `rapidocr` OCR, and an OpenAI-compatible
  local model endpoint for LLM Vision.

---

## 4. Verify the install before trusting it

The test suite is the fastest way to find a broken dependency, and it needs no
camera, projector or network.

```powershell
python tests\verify_canvas_items_split.py   ; echo "exit $LASTEXITCODE"
python tests\verify_doc_extract.py          ; echo "exit $LASTEXITCODE"
python tests\verify_blob_detection.py       ; echo "exit $LASTEXITCODE"
python tests\verify_calibration.py          ; echo "exit $LASTEXITCODE"
```

Exit `0` is a pass. Each writes a detailed log to `%TEMP%\verify_<name>.log` —
**read the log, not just the exit code**; exit codes are unreliable in this
environment (§7).

`tests/README.md` lists all 27 scripts and what each covers.

---

## 5. First run

```powershell
python studio.py      # Authoring Studio
python main.py        # Observatory  (Studio can also launch it on demand)
```

Run both from the **same directory** so the IPC files line up.

---

## 6. What does NOT come across in the repo

Deliberately. Configure these per machine:

| Thing | Where it lives | How to set it |
|---|---|---|
| **API keys** | Windows registry, `QSettings("LightGuide","Observatory")` — never in the repo | Observatory → Global Configuration (collapsed; click to expand) |
| LLM provider / base URL / model | same registry namespace | same panel |
| **Canvas → screen assignment** | registry, `canvas_display_config` | Studio → **🖥 Canvas Setup** |
| Camera selection | per Observatory project file | Camera Device dropdown, then save the project |
| LGS Web API host/port | registry, `lg_api_*` | see `lg_api.py`; the LGS box is normally another PC |
| `captures/`, `vision_state.json`, `studio_command.json` | working directory | regenerated at runtime; gitignored |

Keys live in the registry rather than a file on purpose, so a clone can never
carry one. There is nothing to scrub before pushing — `tests/verify_gemini_segmenter.py`
asserts keys never reach a URL, a request body, an error string or a project file.

### Per-machine checklist after cloning

1. Install dependencies (§3) and run the verification (§4).
2. Observatory: pick the camera, confirm the feed starts.
3. Observatory: set the LLM provider and key if you need LLM Vision.
4. Studio: **Canvas Setup** — assign each output canvas to its projector and
   pick a fit mode. Nothing is assumed about monitor layout.
5. If using the external Light Guide Systems integration, set its host/port —
   the default `54274` is only the wiki's favoured value and installs move it.

---

## 7. Troubleshooting

**Exit code `-1073740791` / `0xC0000409`, faulting `Qt6Core.dll`**
Not a native bug. This PyQt6 build escalates any unhandled Python exception
inside a Qt slot to `qFatal()`/abort, with no traceback. See CLAUDE.md for the
three recurring causes.

**Exit `139` with "Windows fatal exception: access violation"**
A *different*, genuine access violation — `faulthandler` catches this one. See
the known issue in ROADMAP.md.

**Exit code `127` with empty output**
Unreliable here. Cross-check Windows Event Viewer:

```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'} -MaxEvents 5
```

**"The package isn't installed" but you just installed it**
Wrong interpreter. See §2.

**Studio commands Observatory and nothing happens**
Both apps must run from the same working directory. Studio now detects a
missing Observatory — it judges liveness by the age of `vision_state.json` —
and starts one on demand, logging it to the console pane.

**The camera is black or missing**
The camera backend is DSHOW and the app probes indices 0–3 at startup. A camera
in use by another app will not open; the log says so rather than failing quietly.
