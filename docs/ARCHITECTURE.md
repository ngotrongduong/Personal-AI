# Architecture

## Current v0.3 runtime loop

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

Basic OCR is the remaining v0.3 integration item while draft PR #11 is under
real-Tesseract Windows smoke testing.

## Why the LLM is not in the fast loop

A future local LLM belongs above the deterministic rule layer. It can choose
goals or strategies every few seconds, but frame-by-frame gameplay should remain
deterministic for latency, predictability, debuggability, and safety.
