# Personal Game AI — Agent Instructions

## Mission

Build a local Windows game-playing assistant that observes the screen, maintains game state, and can perform safe mouse/keyboard actions for offline/single-player games or games that explicitly permit automation.

## Current architecture

- `main.py`: Tkinter UI and orchestration.
- `core/`: capture, window management, and gated input control.
- `vision/`: visual detectors.
- `agent/`: game state and decision/rule logic.
- `configs/`: per-game configuration.
- `tests/`: deterministic tests.
- `docs/`: architecture, roadmap, and operating notes.

## Non-negotiable safety invariants

1. Keyboard/mouse control starts disabled.
2. F8 must remain a global emergency stop.
3. Disabling input must release all app-generated held keys/buttons.
4. Autonomous actions may run only when input control is explicitly enabled.
5. Never add anti-cheat bypassing, protected-process evasion, memory injection, packet manipulation, credential theft, or stealth/persistence behavior.
6. Prefer screen observation plus ordinary Windows input APIs.

## Engineering rules

- Target Windows 11 and Python 3.14-compatible code.
- Keep the UI responsive; do not run expensive vision or decision loops on the Tkinter UI thread.
- Prefer small modules with clear responsibilities over deep abstraction.
- Add or update tests for game-state/rule behavior.
- Use monotonic time for cooldown/debounce logic.
- Vision confidence must be explicit; do not silently convert weak detections into actions.
- All autonomous actions need logging with the rule/action name and reason.
- Game-specific behavior belongs in profiles/configuration rather than hard-coded core logic.
- Do not commit `.venv`, screenshots, runtime templates, logs, model weights, secrets, or user data.

## Git workflow

- `main` is the tested baseline.
- New work goes to `feature/*` branches.
- Update `CHANGELOG.md` and relevant docs for user-visible behavior changes.
- Keep commits scoped and descriptive.
- Before merge: run syntax checks and the test suite.

## v0.3 priority

Follow GitHub Issue #1: multiple named detectors, game-state variables, safe rules, cooldown/debounce, action logs, ROI support, HP/resource measurement, then basic OCR.
