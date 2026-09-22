# Handoff / current state

Read this before touching anything else — before `AGENTS.md`'s other docs, before
`git log`. It exists so a new session (same assistant on a different day, or a
different assistant entirely) doesn't have to reconstruct "what's actually going
on" from commit history and old chat scrollback.

**Update discipline:** whoever finishes a meaningful chunk of work (opened or
merged a PR, hit a new blocker, changed the plan) updates this file in that same
commit/PR, before moving on. A stale HANDOFF.md is worse than none — if a section
below turns out to be wrong when you read it, fix it as part of your work rather
than trusting it blindly.

## Orientation checklist for a new session

1. Read `AGENTS.md` (rules/invariants) and this file (current state).
2. `git fetch --all` and check open branches/PRs before starting anything — the
   table below may already be stale by the time you read it.
3. Run `scripts/test.ps1` locally before trusting any check — CI is currently
   blocked, see "Known blockers" below.
4. Before you stop working, update the sections below if the picture changed.

## Right now (2026-09-23)

v0.3 ("Game State + Rules", tracked in Issue #1) is in progress on the
integration branch `feature/v0.3-game-state` (PR #2 into `main`, still draft).
Two assistants are developing this repo concurrently — Claude Code
(`claude/<task>` branches) and Codex/ChatGPT (`codex/<task>` branches) — per
`AGENTS.md`'s "Multi-AI coordination" section.

### Open PRs

| PR | Branch | Base | Owner | What it does |
|----|--------|------|-------|---------------|
| #2 | `feature/v0.3-game-state` | `main` | Codex | v0.3 foundation: `agent/game_state.py`, `agent/rule_engine.py`, CI, `.github/agents`/`instructions`/`prompts` |
| #3 | `claude/collab-tooling` | `feature/v0.3-game-state` | Claude | Multi-AI rules in `AGENTS.md`, `CLAUDE.md`, pytest+ruff, `tests/test_template_matcher.py`, this file |
| #5 | `codex/v0.3-multi-detectors` | `feature/v0.3-game-state` | Codex | Multiple Named Detectors → `GameState` bridge. Independently re-run by Claude: 18/18 tests pass, doesn't touch `main.py`/`AGENTS.md`/CI |
| #6 | `claude/fix-delayed-exception-callback` | `claude/collab-tooling` | Claude | Fixes Issue #4 (deferred-callback `NameError` in `main.py`); live smoke-tested on the real app |

Recommended merge order: **#3 → #6 → #5 → ... → #2 into `main`** once v0.3 is
stable. #6 is stacked on #3 (needs its `pyproject.toml`/pytest setup) — retarget
#6 to `feature/v0.3-game-state` once #3 merges, or GitHub will do it automatically
if the branch is deleted on merge.

Re-verify this table (`git branch -a`, open PRs) before merging or branching from
any of it — it is a snapshot, not a live view.

### Known blockers

- **GitHub Actions cannot run right now.** Every workflow run on this repo fails
  immediately with *"The job was not started because recent account payments have
  failed or your spending limit needs to be increased."* — a billing issue on the
  repo owner's GitHub account, unrelated to this repo's code. Confirmed by
  checking the Actions UI directly; the very first CI run (before any PR existed)
  failed the same way. Until the owner fixes billing at
  `github.com/settings/billing`, treat local verification (`scripts/test.ps1`:
  compile check + ruff + pytest) as the actual gate, and say so explicitly in PR
  descriptions instead of waiting on a green check.

### Open issues

- **#1** — v0.3 Game State + Rules (tracking issue for the whole milestone).
- **#4** — delayed-lambda exception bug in `main.py`. Fixed in PR #6.

## Longer-term plan

See `docs/ROADMAP.md` for the v0.1 → v1.0 milestone plan and `docs/ARCHITECTURE.md`
for the intended runtime loop and module responsibilities. This file only tracks
short-lived, fast-changing state (open PRs, active blockers) — put anything that
should outlive the current sprint in those files instead, not here.
