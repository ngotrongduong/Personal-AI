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

### Stage 5 — Demonstration recording (v0.5, Issue #41)
- Default-off "Recording" panel that saves the captured window's frames,
  `GameState` snapshots and the user's own keyboard/mouse input, all on one
  monotonic clock, into per-session folders (`frames/*.jpg`, `events.jsonl`,
  `session.json`).
- Input is recorded only while the game window is in the foreground, with
  client-relative coordinates. F8 and injected input are never recorded.
- The `recording/` package only listens and never sends input. Recording and
  autonomous input control are mutually exclusive.
- Fail-safes: 30-minute session cap and a stop below 1 GB of free disk. Frames
  can be dropped and counted when the writer falls behind; events never are.
- `scripts/recordings.py` offers `list` / `validate` / `export` (aligned
  frame ↔ state ↔ action `dataset.jsonl`) / `review` (overlay viewer).
- Live-smoke-tested on Windows at 150% scaling with 0 dropped frames at 10 fps.

### Stage 6 — Game Profiles + Skills (v0.6, Issue #50)
- Per-game profiles loaded at runtime: `profiles/<name>/profile.json` plus
  templates. Save them from the UI, then edit the JSON. Loading is strictly
  validated and refused while input control is on.
- Named skills (`click` / `press` / `hold`), disabled by default.
- Per-profile permissions:
  - a key allowlist, checked by the loader and again by the dispatcher;
  - `f8`, the Windows keys and key combos are always forbidden;
  - a hold cap of at most 5 s;
  - a rate limit.
- Rules fire skills. The Skills panel runs them by hand. Skills run one at a
  time on a worker thread, still through `ActionDispatcher`.
- Key skills run only while the game window is in the foreground. F8 releases
  held keys, then cancels the running skill.
- Live-smoke-tested on Windows with Notepad, including F8 during a hold.

## Next (toward v1.0)

- v0.7 — closed-loop planner. The LLM picks a skill *name* from the profile,
  never coordinates or keys. Approve-each-step by default; explicit opt-in auto
  mode with a rate limit and an action budget. The outcome is observed after
  each action.
- v0.8 — session memory: a structured JSONL log plus bounded, user-editable
  notes written by the LLM. Memory can never widen permissions.
- Imitation-learning experiments on recorded datasets remain a possible side
  track (offline training only; any replay goes through `ActionDispatcher`).

## Later

### v1.0 — Personal Game Agent
- Vision -> state -> plan -> action -> observation loop.
- Per-game profiles.
- Long-term session memory.
- Explicit permissions and safety controls.

## Scope / safety

Development targets offline/single-player games or games that permit automation. The project will not add anti-cheat bypassing, protected-process evasion, memory injection, or packet manipulation.
