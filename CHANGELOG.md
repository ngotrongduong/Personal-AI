# Changelog

All notable project changes are tracked here.

## v0.5.0 — Demonstration recording

- Added a "Recording" panel (default off, explicit Record button, always-visible
  "● REC" status). It records your own demonstrations of the captured game window
  into `recordings/<YYYYmmdd_HHMMSS>/`:
  - `frames/000001.jpg` frames, 1-30 fps (default 10);
  - `events.jsonl` (frames, `GameState` snapshots, keys, mouse buttons,
    throttled mouse moves, scroll, focus changes, start/stop markers), all on
    one monotonic clock;
  - `session.json` with counts and the stop reason.
- Input is recorded only while the captured window is in the foreground. Mouse
  coordinates are client-relative, and mouse events outside the client area are
  dropped. F8 and software-injected input (SendInput, on-screen keyboard, remote
  desktop, automation tools) are never recorded.
- The `recording/` package only listens: it never sends input and never imports
  the input path. Recording and autonomous input control are mutually exclusive:
  Record is refused while input control is on, and enabling input control
  stops recording.
- F8 and window close stop recording after releasing input and stopping the
  planner. Sessions also stop at 30 minutes, when free disk drops below 1 GB,
  or when the window closes. Frames are encoded on a background thread, and
  frames are dropped and counted when the writer falls behind; events are never
  dropped.
- Added `python scripts/recordings.py`:
  - `list` shows sessions;
  - `validate` checks timing, counts and frame files;
  - `export` writes an aligned `dataset.jsonl`: one row per frame with its
    state and the actions until the next frame;
  - `review` is an OpenCV viewer with click/key/mouse overlays.
  It never deletes data and refuses to overwrite unless `--overwrite` is given.
- `recordings/` and exported datasets are gitignored.
- Live-smoke-tested on Windows at 150% display scaling: 0 dropped frames at
  10 fps. Stopping on input-control enable and on F8, focus lost/gained events,
  and filtering of injected input were all verified.

## v0.4.0 — Local AI Planner

- Added an optional local LLM planner backed by Ollama (default model
  `qwen3.5:9b`), off by default.
- The planner only proposes directives from a closed vocabulary
  (`enable_rule` / `disable_rule` / `noop`), validated before being applied
  through `RuleEngine`. It has no path to `ActionIntent`, `ActionDispatcher` or
  `InputController`; all v0.3 safety gates are unchanged.
- Planner cycles run on a background thread at a configurable interval and
  fail closed on connection errors, timeouts, HTTP errors and invalid output.
- `RuleEngine` gained `enable_rule` / `disable_rule` / `is_rule_enabled` and
  an `RLock` for safe concurrent use.
- "Planner (Ollama)" UI panel: enable toggle, model, interval, status, and the
  last cycle's time, latency, status and outcome. F8, Clear Rules and window
  close stop the planner; F8 and close release input first.
- Every planner cycle's outcome is logged.
- Added the provider-neutral `model_runtime` package and model catalog
  (`configs/models.v1.json`); models are never auto-downloaded.

## v0.3.0 — Game State + Rules

- Added multiple named detectors per game profile and regions of interest.
- Added HP/resource bar measurement and basic OCR (Tesseract).
- Added persistent `GameState` with stale-data handling.
- Added a deterministic rule engine with cooldowns/debouncing.
- Added the gated `ActionDispatcher` (`ActionIntent` → `InputController`).

## v0.2.0 — Vision baseline

- Added live DXcam capture of a selected Windows application.
- Added gated keyboard/mouse control with PyDirectInput.
- Added target-window focus handling before keyboard input.
- Added global F8 emergency stop.
- Added template selection directly on the live preview.
- Added OpenCV template matching at approximately 10 Hz.
- Added confidence threshold control and match bounding box.
- Added snapshot/template folders for later datasets.

## v0.1.2 — Input/focus fixes

- Fixed keyboard tests being sent to the AI window instead of the selected target.
- Added explicit Focus Game control.
- Improved emergency-stop visibility.

## v0.1.1 — Environment fixes

- Added Python 3.14-compatible setup flow.
- Improved hardware diagnostics.

## v0.1.0 — Initial Windows control prototype

- Window selection and capture.
- Keyboard/mouse test controls.
- Event log and emergency stop.
