# Personal Game AI Roadmap

## Completed

### Stage 0 — Environment
- Windows/Python environment verified.
- Core dependencies installed.

### Stage 1 — Capture and input
- Select a target application window.
- Live DXcam capture.
- Mouse and keyboard output.
- Target focus handling.
- F8 global emergency stop.

### Stage 2 — Vision baseline (v0.2.0)
- User-defined visual templates.
- OpenCV template matching.
- Match score and coordinates.
- Snapshot capture.

### Stage 3 — Game State + Rules (v0.3, Issue #1, closed)
- Multiple named detectors per game profile.
- Region-of-interest definitions.
- HP/resource bar measurement.
- OCR for simple text/numbers (real-Tesseract smoke-tested on Windows).
- Persistent game-state variables.
- Safe rule engine such as `IF CollectButton.visible THEN click`.
- Action cooldowns and debouncing to prevent repeated accidental input.
- Gated action dispatcher (`ActionIntent` → `InputController`), F8 emergency
  stop verified live against a real autonomous action.

### Stage 4 — Local AI planner (v0.4, Issue #15)
- Ollama backend (headless REST on localhost), default model `qwen3.5:9b`.
- Planner proposes directives from a closed vocabulary
  (`enable_rule` / `disable_rule` / `noop`) applied only via `RuleEngine`;
  it can never create input or bypass v0.3's dispatcher gates.
- Runs on its own slow background thread; fails closed on any Ollama error,
  timeout, or invalid output.
- Default-off "Planner (Ollama)" UI panel with last-cycle time/latency/status;
  F8 stops input first, then the planner.
- Live-smoke-tested on Windows with the real model.

## Next — v0.5 Demonstration recording

- Record screen state plus the user's actions.
- Build datasets for repeatable tasks.
- Optional imitation-learning experiments.

## Later

### v1.0 — Personal Game Agent
- Vision -> state -> plan -> action -> observation loop.
- Per-game profiles.
- Long-term session memory.
- Explicit permissions and safety controls.

## Scope / safety

Development targets offline/single-player games or games that permit automation. The project will not add anti-cheat bypassing, protected-process evasion, memory injection, or packet manipulation.
