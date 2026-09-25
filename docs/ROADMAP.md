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

### Stage 7 — Closed-loop planner (v0.7, Issue #61)
- The planner can propose `run_skill` with a skill *name* and a reason. It
  never supplies keys, coordinates or durations, and the skill must be enabled.
- Closed loop: the prompt carries the goal, the observations, the enabled
  skills, the rules and the last 5 steps with their outcomes.
- Approve-each-step by default, with a 10 s expiry. Auto mode is opt-in, never
  saved, pinned to the confirmed window and capped at `auto_max_steps`
  (hard cap 100). It turns off after 3 failed steps in a row and on F8, input
  off, profile load, Clear Rules or planner off.
- A single-slot proposal mailbox. Only the Tk thread submits skills, after
  rebuilding them from fresh state, and the dispatcher's gates are unchanged.
  No LLM call is made while a proposal is pending or a skill is running.
- `planner.goal` and `planner.auto_max_steps` in the profile.
- Live-smoke-tested on Windows with Notepad and Ollama `qwen3.5:9b`, including
  F8 during auto.

### Stage 8 — Session memory (v0.8, Issue #71)
- A structured JSONL log per planner session under
  `memory/<profile>/sessions/`: cycles, steps with decision and outcome, auto
  on/off, note changes and the end reason. Capped at 5 MB, never raises.
- Bounded per-profile notes in `memory/<profile>/notes.json`, shown to the
  planner as hints: at most 20, at most 10 from the LLM, 200 characters each.
- The LLM may add a note with `remember` only when `planner.llm_notes` is on,
  at most one per 30 s; it never edits or deletes a note.
- A Memory panel to add, edit and delete notes, and `scripts/memory.py` to
  list, show and validate memory files.
- Memory never widens permissions.
- Live-smoke-tested on Windows with Notepad and Ollama `qwen3.5:9b`, including
  F8 with an LLM call in flight.

### Stage 9 — Personal Game Agent (v1.0, Issue #82)
- The loop vision → state → plan → action → observation is closed: a skill's
  optional `expect` is watched after each step and recorded as `confirmed` or
  `not_seen` in the next prompt, the run line and the session log. In auto
  mode a `not_seen` step counts as failed; a step is never retried.
- Agent runs: Preflight / Start Agent / Stop Agent, a run budget
  (`planner.max_run_minutes`) and an optional goal condition
  (`planner.stop_when`). Stops only reduce activity.
- `docs/USER_GUIDE.md` takes a new user from install to a first supervised
  run.
- Live-smoke-tested on Windows with Notepad and Ollama `qwen3.5:9b`: all 9
  acceptance criteria passed.

## Next (after v1.0)

- Not planned yet. Candidates: multiple templates per detector, OCR,
  color/HP-bar analysis and object detection.
- Imitation-learning experiments on recorded datasets remain a possible side
  track (offline training only; any replay goes through `ActionDispatcher`).

## Scope / safety

Development targets offline/single-player games or games that permit automation. The project will not add anti-cheat bypassing, protected-process evasion, memory injection, or packet manipulation.
