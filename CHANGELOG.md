# Changelog

All notable project changes are tracked here.

## Unreleased — v0.4 Local AI Planner (in progress)

- Planner (Ollama) panel shows the last planner cycle: time, latency, status
  and directive/outcome. Display-only; stale reports after stop/F8 are dropped.

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
