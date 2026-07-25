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

## What each covers

| Script | Covers |
|---|---|
| `verify_capture_migration.py` | Observatory Image Capture → Studio reference over IPC, legacy project load |
| `verify_multicanvas.py` | 1–5 canvases, per-asset assignment, Clear Canvas targeting, shrink-rescue |
| `verify_multicamera.py` | Per-tool camera binding, visibility filtering, File→New across cameras |
| `verify_model3d.py` | 3D load/render/pose/save (predates Ctrl-drag; its fake event lacks `modifiers()`) |
| `verify_worker_crashfix.py` | QThread lifecycle: rapid reloads, deleted-item delivery, torn-down panels |
| `verify_run_mode_ui.py` | Run Mode panel hiding + active-step highlight |
| `verify_user_wait_vision_gate.py` | User Wait as an *additional* gate on top of Vision Wait |
| `verify_blob_revert.py` | Blob tool back to original single-blob behaviour |
