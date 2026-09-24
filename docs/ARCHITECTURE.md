# Architecture

## Fast runtime loop (v0.3, unchanged in v0.4/v0.5)

```text
DXcam frame
   ↓
vision detectors / measurements
   ↓
GameState
   ↓
RuleEngine
   ↓
ActionIntent
   ↓
ActionDispatcher
   ↓
InputController
   ↓
Windows/game
```

The fast loop is deterministic and inspectable. Vision produces observations;
rules produce intents; only the dispatcher is allowed to translate supported
intents into real input.

## Separation of responsibilities

### `core/`

Low-level Windows primitives:

- window discovery/focus
- capture
- explicitly gated mouse/keyboard input
- emergency cleanup

Input control starts disabled. Disabling input, including F8 emergency stop,
releases app-generated held keys/buttons.

### `vision/`

Perception only:

- template matching
- named detectors
- regions of interest
- HP/resource bars
- OCR
- later object detection

Vision may create structured observations or measurements but must never send
mouse/keyboard input.

### `agent/`

State and decisions:

- `GameState` stores the latest named observations
- stale-data handling prevents old observations from being treated as current
- `RuleEngine` evaluates deterministic rules and cooldown/debounce
- `ActionIntent` represents a requested action without executing it
- vision/state bridges translate detector/resource/OCR results into observations

### ActionDispatcher

`agent/action_dispatcher.py` is the only bridge from `ActionIntent` to
`InputController`.

Before sending input it checks:

1. input control is explicitly enabled,
2. the requested action is supported,
3. the intent is still fresh,
4. a target bbox exists,
5. the captured target window is still resolvable.

The live loop passes the hwnd from the active capture session rather than the
window-picker combobox, because the combobox can change while capture continues
against the original window.

F8 remains dominant because it disables `InputController`; both the initial
dispatcher gate and `InputController.click()` itself reject input after disable,
including the race where F8 fires between those checks.

## Live v0.3 status

Merged and machine-smoke-tested on Windows:

- multiple named template detectors updating `GameState`
- simultaneous detector visibility display/logging
- gated click rules
- blocked dispatch while input is disabled
- real click dispatch after explicit enablement
- F8 immediately blocking further autonomous dispatch while vision continues

HP/resource-bar measurement is implemented as pure vision/state logic and can be
wired into game profiles as needed.

Basic OCR (`vision/ocr.py`, `agent/ocr_state_bridge.py`) is merged and was
smoke-tested against real Tesseract on Windows (PR #11).

## v0.4 local AI planner (optional, default off)

```text
GameState snapshot + current rule on/off settings
   ↓  (every few seconds, background daemon thread)
OllamaClient → local model (default qwen3.5:9b)
   ↓
parse_directive  — closed vocabulary: enable_rule / disable_rule / noop
   ↓
RuleEngine.enable_rule / disable_rule   (never ActionIntent, never input)
```

- `agent/ollama_client.py` — stdlib REST client for `127.0.0.1:11434`, bounded
  timeouts, sends `"think": false` so thinking models return JSON.
- `agent/llm_planner_schema.py` — the only authority on what model output means;
  anything outside the closed vocabulary is rejected and logged.
- `agent/llm_planner.py` — `LlmPlanner.plan_once` fails closed: any Ollama error,
  empty response, or validation rejection leaves rule settings unchanged.
- `agent/planner_scheduler.py` — `PlannerScheduler` runs cycles on its own daemon
  thread, isolated from the Tk thread and the fast loop; logs each outcome and
  offers an observation-only `on_cycle` report for the UI.
- `agent/planner_controller.py` — `PlannerController` owns the scheduler
  lifecycle for `main.py`. `stop()` never blocks the Tk thread, and a directive
  that arrives after stop is discarded (`PlannerCancelledError`).
- `agent/planner_config.py` — `PlannerConfig`, disabled unless explicitly enabled.

`RuleEngine` is guarded by an `RLock` because the planner thread toggles rules
while the fast loop evaluates them. F8 and window close release input *before*
stopping the planner. The planner has no path to `ActionIntent`,
`ActionDispatcher`, or `InputController`; the dispatcher's gates are unchanged.

## v0.5 demonstration recording (optional, default off)

```text
WindowCapture.latest_frame() + GameState.snapshot()   (sampler thread, 1-30 fps)
pynput keyboard/mouse listeners → InputRecorder       (listen only)
   ↓  one monotonic clock
SessionWriter  (bounded queue, JPG encode on a background thread)
   ↓
recordings/<stamp>/  frames/*.jpg · events.jsonl · session.json
   ↓  offline
scripts/recordings.py  list / validate / export (dataset.jsonl) / review
```

- `recording/schema.py` defines the event and session records and validates
  them strictly: unknown event types are rejected.
- `recording/session_writer.py` (`SessionWriter`): when the queue is full,
  frames are dropped and counted, but events are never dropped. `close()`
  flushes and writes `session.json`.
- `recording/input_recorder.py` (`InputRecorder`):
  - records only while the captured hwnd is in the foreground;
  - converts mouse coordinates to client-relative and drops mouse events
    outside the client area;
  - skips F8 and injected input (`LLKHF_INJECTED` / `LLMHF_INJECTED`);
  - throttles mouse moves and emits `focus` gained/lost.
- `recording/recorder_controller.py` (`RecordingController`) owns the sampler
  thread, the listeners and the writer.
  - `stop()` never blocks the Tk thread.
  - It enforces the 30-minute cap and the 1 GB free-disk floor, and it stops
    when the window closes.
- `recording/dataset.py` handles reading, validation, the aligned export
  (frame ↔ state ↔ actions until the next frame) and the review viewer. It
  never deletes data.

Boundary: `recording/` never imports `InputController`, `ActionDispatcher`,
`ActionIntent` or `pydirectinput`, and it never sends input. Recording and input
control are mutually exclusive, so a dataset holds only human input:
- `main.py` refuses Record while input control is on;
- enabling input control stops recording.

F8 and window close release input first, then stop the planner, then stop
recording.

## Why the LLM is not in the fast loop

The local LLM sits above the deterministic rule layer. It can choose goals or
strategies every few seconds, but frame-by-frame gameplay stays deterministic
for latency, predictability, debuggability, and safety.
