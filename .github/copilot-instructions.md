Personal Game AI is a Windows Python project for screen-observation and safe mouse/keyboard automation.

Always preserve these invariants:
- Input control is disabled by default.
- F8 is a global emergency stop and must release app-generated held inputs.
- Autonomous actions require explicit input enablement.
- Do not implement anti-cheat bypassing, protected-process evasion, memory injection, packet manipulation, stealth, persistence, or credential handling.
- Prefer ordinary screen capture/computer vision plus Windows input APIs.

Architecture:
- `core/` owns capture/window/input primitives.
- `vision/` owns perception only; it must not send input.
- `agent/` owns game state and decision/rule logic.
- `main.py` owns UI/orchestration.
- `configs/` owns game-specific configuration.
- `tests/` owns deterministic tests.

Coding expectations:
- Keep modules small and explicit.
- Avoid over-engineering and speculative abstractions.
- Keep Tkinter UI work non-blocking.
- Use monotonic time for cooldowns.
- Log autonomous decisions/actions.
- Add tests when changing state/rule logic.
- Keep runtime screenshots/templates/logs/model files out of Git.
- Treat Issue #1 as the active v0.3 implementation plan.
