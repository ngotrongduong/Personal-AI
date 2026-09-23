# Personal Game AI — Agent Instructions

## Start here

Before anything else, read [`docs/HANDOFF.md`](docs/HANDOFF.md) (current open
PRs, who owns what, active blockers) and [`docs/PLAN.md`](docs/PLAN.md) (granular
v0.3 checklist with status/owner per task). This file (`AGENTS.md`) is the stable
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
- `feature/v0.3-game-state` is the current v0.3 integration branch; v0.3 work branches from it, not from `main`.
- New work goes to `feature/*` branches (or a sub-branch of the active integration branch, see below).

### Multi-AI coordination

This repo is developed by more than one AI assistant at once (Claude Code, Codex/ChatGPT, and GitHub Copilot via `.github/agents` and `.github/prompts`). To avoid collisions and duplicated work:

- Claude Code uses branch names `claude/<task>`.
- Codex/ChatGPT uses branch names `codex/<task>`.
- Nobody pushes directly to `main` or to an active integration branch (e.g. `feature/v0.3-game-state`); every change lands through a PR.
- Before starting a task: `git fetch` and check existing branches/open PRs to confirm the task is not already in progress on another branch.
- PRs for v0.3 sub-tasks target `feature/v0.3-game-state`, not `main`. That branch merges into `main` only once v0.3 is complete and stable.

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
  | PR (v0.3 complete)
feature/v0.3-game-state
  ^
  |-- claude/<task>   (PR back into feature/v0.3-game-state)
  |-- codex/<task>    (PR back into feature/v0.3-game-state)
  `-- codex/<task-2>
```

- Update `CHANGELOG.md` and relevant docs for user-visible behavior changes.
- Keep commits scoped and descriptive.
- Before merge: run syntax checks, lint, and the test suite (see `scripts/test.ps1` / CI).

## v0.3 priority

Follow GitHub Issue #1: multiple named detectors, game-state variables, safe rules, cooldown/debounce, action logs, ROI support, HP/resource measurement, then basic OCR.
