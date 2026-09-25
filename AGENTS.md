# Personal Game AI — Agent Instructions

## Start here

Before anything else, read [`docs/HANDOFF.md`](docs/HANDOFF.md) (current open
PRs, who owns what, active blockers) and [`docs/PLAN.md`](docs/PLAN.md) (granular
checklist for the current milestone with status/owner per task). This file (`AGENTS.md`) is the stable
rulebook; those two are fast-changing state — update them when you finish a
meaningful chunk of work, **in the same push**, not as an afterthought (a doc
pushed as a later, separate commit to an already-reviewed PR can be merged out
from under you — it happened once already, see `docs/HANDOFF.md` "Lessons").

## Mission

Build a local Windows game-playing assistant that observes the screen, maintains game state, and can perform safe mouse/keyboard actions for offline/single-player games or games that explicitly permit automation.

## Current architecture

- `main.py`: Tkinter UI and orchestration.
- `core/`: capture, window management, and gated input control.
- `vision/`: visual detectors.
- `agent/`: game state and decision/rule logic.
- `configs/`: per-game configuration.
- `profiles/`: runtime game profiles (v0.6+; gitignored except `profiles/example/`).
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
- Prefer files under roughly 300 lines as a soft readability target, not a hard gate. Do not split a module mechanically just to hit a line count; refactor only when a file's responsibilities have actually grown unclear (existing larger files such as `main.py` do not need forced splitting outside a planned refactor).

## Git workflow

- `main` is the tested baseline.
- Active integration branch: `feature/v1.0-personal-agent` (Issue #82). v0.8 (`feature/v0.8-session-memory`, Issue #71) merged into `main` as a merge commit at release.
- New work goes to `feature/*` branches (or a sub-branch of the active integration branch, see below).

### Multi-AI coordination

This repo is developed by more than one AI assistant at once (Claude Code, Codex/ChatGPT, and GitHub Copilot via `.github/agents` and `.github/prompts`). To avoid collisions and duplicated work:

- Claude Code uses branch names `claude/<task>`.
- Codex/ChatGPT uses branch names `codex/<task>`.
- Nobody pushes directly to `main` or to an active integration branch (e.g. `feature/v0.3-game-state`); every change lands through a PR.
- Before starting a task: `git fetch` and check existing branches/open PRs to confirm the task is not already in progress on another branch.
- PRs for a milestone's sub-tasks target that milestone's integration branch, not `main`. The integration branch merges into `main`, as a merge commit, only once the milestone is complete and stable.

**Default task assignment:** Claude Code's context/token budget for this repo is
more limited per session than Codex/ChatGPT's, so route work accordingly rather
than defaulting everything to whichever assistant is already in the conversation:

- **Claude's lane:** anything that genuinely needs the user's Windows machine —
  live GUI smoke tests, F8/input/capture verification, local tooling/CI setup,
  anything you'd need to actually run and watch happen.
- **Codex's lane (default):** pure logic, algorithms, data structures, tests,
  docs, config — anything verifiable by reading code and running a test suite,
  no machine access required. When a task doesn't clearly need a real machine,
  it defaults here, not to Claude.
- When a task is unclear, say so in `docs/HANDOFF.md`/the tracking issue and let
  it get picked up rather than either assistant guessing.

```
main
  ^
  | PR (milestone complete, merge commit)
feature/<milestone>
  ^
  |-- claude/<task>   (PR back into feature/<milestone>)
  |-- codex/<task>    (PR back into feature/<milestone>)
  `-- codex/<task-2>
```

- Update `CHANGELOG.md` and relevant docs for user-visible behavior changes.
- Keep commits scoped and descriptive.
- Before merge: run syntax checks, lint, and the test suite (see `scripts/test.ps1` / CI).

## Current priority

v1.0 "Personal Game Agent" (Issue #82) on `feature/v1.0-personal-agent`.
It closes the loop vision → state → plan → action → observation:
- observed effects: a skill's optional `expect`, recorded as `confirmed` /
  `not_seen` after each step;
- agent runs: Preflight / Start Agent / Stop Agent, a run budget and an
  optional goal condition;
- a user guide.

Earlier steps: v0.6 profiles + skills, v0.7 a planner that picks skills, v0.8
session memory (all released). See `docs/PLAN.md`'s design constraint before
implementing anything.

The invariants below stay in force for every later milestone.

Agent invariant (permanent, from v1.0):
- Observation never adds input. Expectations, effect watches, preflight, the
  run budget and the goal condition only read state, the profile and the
  clock. `agent/skill_effects.py` and `agent/agent_session.py` never import
  the input path.
- Stops only reduce activity. The budget, the goal condition and a failed
  preflight can only stop the planner or refuse to start it. Start Agent never
  turns on input control or auto mode.
- An effect `not_seen` can only turn auto off sooner; a step is never
  retried because of it.
- At most one effect watch, polled on the Tk thread; the planner makes no LLM
  call while it is pending. F8 drops it.
- A run is bounded: `planner.max_run_minutes` has a hard cap of 120.

Memory invariant (permanent, from v0.8):
- Memory never widens permissions. Notes and session logs are read only by
  the Memory panel, the planner prompt and `scripts/memory.py`. They are never
  read by the profile loader, skills, permissions, rules, autopilot, executor
  or dispatcher.
- The memory modules never import the input path.
- The LLM's `remember` directive is text only (1–200 chars), it is off unless
  the profile sets `planner.llm_notes`, and it never edits or deletes a note.
  The Memory panel's "Let the planner write notes" checkbox is an unsaved
  edit of that field: it needs a loaded profile and a valid `notes.json`.
- A `notes.json` that is invalid, or changed outside the app, is never
  overwritten; the notes turn read-only until the profile is loaded again.
- Notes are bounded:
  - at most 20 notes, of which at most 10 come from the LLM;
  - at most one LLM note per 30 s.
- Memory data lives under the gitignored `memory/` folder. It is never
  committed and never deleted by the app.

Planner invariant (permanent, from v0.7):
- The LLM directive `run_skill` carries only a skill *name* and a display-only
  reason, and the skill must be enabled.
- Only the Tk thread submits a planner step to the `SkillExecutor`, after
  rebuilding the intent from fresh state. The planner thread only posts
  proposals to a single-slot mailbox.
- Approve-each-step is the default, and a proposal older than its 10 s TTL
  never runs. Auto mode is opt-in, never persisted, pinned to the window it
  was confirmed for, step-capped (hard cap 100), and turns off on F8, input
  off, profile load, Clear Rules, planner off, or 3 failed steps in a row.
- No LLM call is made while a proposal is pending or a skill is running.

Skill invariant (permanent, from v0.6):
- `ActionDispatcher` is the only path from a skill to `InputController`.
- A key must be in the profile's allowlist, and it is checked by the loader
  and again by the dispatcher.
- F8, Win and Apps keys and key combos are forbidden in code.
- A hold is capped at 5.0 s.
- Key skills run only while the captured window is foreground.
- A skill is disabled by default.
- From v0.7 an LLM may only choose a skill *name* from the profile; it never
  supplies coordinates, keys or durations.

Recording invariant (permanent): recording only listens — it must never send input, never run while
autonomous input control is enabled, and never record input while the game
window is not foreground. See `docs/PLAN.md`'s design constraint section before
implementing anything here.
