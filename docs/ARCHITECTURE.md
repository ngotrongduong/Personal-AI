# Architecture

## Runtime loop target

```text
DXcam frame
   ↓
vision detectors
   ↓
GameState
   ↓
RuleEngine
   ↓
ActionIntent
   ↓
gated action dispatcher (future v0.3 step)
   ↓
InputController
   ↓
Windows/game
```

## Separation of responsibilities

### `core/`
Low-level Windows primitives:
- window discovery/focus
- capture
- explicitly gated mouse/keyboard input
- emergency cleanup

### `vision/`
Perception only:
- templates
- ROI
- bars
- OCR
- later object detection

Vision may create structured observations but must not send input.

### `agent/`
State and decisions:
- most recent observations
- stale-data handling
- deterministic rules
- cooldown/debounce
- action intents

Action intents are data, not input side effects.

### Dispatcher (next v0.3 increment)

A gated dispatcher will translate supported `ActionIntent` objects into `InputController` calls only when:
1. explicit input control is enabled,
2. the target window/profile is valid,
3. the intent is still current,
4. F8 has not disabled the system.

## Why the LLM is not in the fast loop

The future local LLM belongs above the deterministic rule layer. It can choose goals or strategies every few seconds, but frame-by-frame gameplay remains deterministic for latency, predictability, and safety.
