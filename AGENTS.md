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
- The active integration branch is `feature/v0.6-profiles-skills` (Issue #50), branched from `main` at the v0.5.0 release.
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

v0.6 "Game Profiles + Skills" (Issue #50) on `feature/v0.6-profiles-skills`.
It is the first step toward v1.0: v0.6 profiles + skills, then v0.7 a planner
that picks skills, then v0.8 memory, then v1.0. Read `docs/PLAN.md`'s design
constraint section before implementing anything here.

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
