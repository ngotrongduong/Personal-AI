# Handoff / current state

Read this before touching anything else — before `AGENTS.md`'s other docs, before
`git log`. It exists so a new session (same assistant on a different day, or a
different assistant entirely) doesn't have to reconstruct "what's actually going
on" from commit history and old chat scrollback.

**Update discipline:** whoever finishes a meaningful chunk of work (opened or
merged a PR, hit a new blocker, changed the plan) updates this file **in the same
push**, before moving on — not "later." This file was already dropped once
because a PR got merged in between two pushes to it (see "Lessons" below); don't
repeat that.

## Orientation checklist for a new session

1. Read `AGENTS.md` (rules/invariants), this file (current state), and
   `docs/PLAN.md` (granular v0.3 task checklist).
2. `git fetch --all` and check open branches/PRs before starting anything — the
   tables below may already be stale by the time you read them.
3. Run `scripts/test.ps1` locally before trusting any check — CI is currently
   blocked, see "Known blockers" below.
4. Before you stop working, update this file and `docs/PLAN.md` if the picture
   changed — in the same commit/push as the rest of your work.

## Right now (2026-09-23)

v0.3 ("Game State + Rules", tracked in Issue #1) is in progress on the
integration branch `feature/v0.3-game-state` (PR #2 into `main`, still draft).
Two assistants develop this repo concurrently — Claude Code (`claude/<task>`
branches) and Codex/ChatGPT (`codex/<task>` branches) — per `AGENTS.md`'s
"Multi-AI coordination" section, which now also has a default task-assignment
rule: machine-required work (live GUI/F8/input/capture testing) defaults to
Claude, everything else defaults to Codex.

### Merged into `feature/v0.3-game-state`

PRs #3 (collab tooling: `AGENTS.md`/`CLAUDE.md`/pytest+ruff), #6 (Issue #4 fix),
and #5 (Multiple Named Detectors → GameState bridge) are all merged, in that
order. Local verification on the merged branch: compile check + ruff + pytest
**28/28 pass**.

### Open PRs / branches

| PR/branch | Base | Owner | What it does |
|-----------|------|-------|---------------|
| #2 `feature/v0.3-game-state` | `main` | Codex | v0.3 integration branch itself, still draft |
| #8 `codex/v0.3-resource-bars` | `feature/v0.3-game-state` | Codex | v0.3: add HP/resource bar measurement (task 3). Not yet merged. |
| `claude/live-detector-loop` (this work) | `feature/v0.3-game-state` | Claude | Task 11: wires `DetectorRegistry`/vision→`GameState` into the live Tk capture loop, shows multi-detector status in GUI/log. Live-smoke-tested on Windows (two named templates, both FOUND simultaneously; F8 verified unaffected). 31/31 tests, ruff clean. About to open as a PR. |

Re-verify this table (`git branch -a`, open PRs) before merging or branching from
any of it — it is a snapshot, not a live view.

### Next task

Task 11 (live-loop wiring, above) is done pending PR review/merge. The next item
in `docs/PLAN.md` is **task 12: gated action dispatcher** (`ActionIntent` →
`InputController`, only when input control is explicitly enabled) — likely
Claude's lane since it touches `core`/input safety, but not yet formally
assigned in an Issue #1 comment. Confirm assignment there before starting.

### Known blockers

- **GitHub Actions cannot run right now.** Every workflow run on this repo fails
  immediately with *"The job was not started because recent account payments have
  failed or your spending limit needs to be increased."* — a billing issue on the
  repo owner's GitHub account, unrelated to this repo's code. Until the owner
  fixes billing at `github.com/settings/billing`, local verification
  (`scripts/test.ps1`) is the actual merge gate — record it explicitly in every
  PR description instead of waiting on a green check.

### Open issues

- **#1** — v0.3 Game State + Rules (tracking issue; see `docs/PLAN.md` for the
  same checklist with status/owner columns).
- **#4** — delayed-lambda exception bug in `main.py`. Fixed, merged (PR #6).

## Lessons

- **A doc pushed as a second commit to an already-reviewed PR can be merged out
  from under you.** PR #3 got squash-merged between this file's first and second
  push, so the second push (this file) never made it in. If you push a
  meaningful update to an open PR, say so out loud to whoever might merge it —
  don't assume "I pushed it" means "it's in."

## Longer-term plan

See `docs/ROADMAP.md` for the v0.1 → v1.0 milestone plan and `docs/ARCHITECTURE.md`
for the intended runtime loop and module responsibilities. This file only tracks
short-lived, fast-changing state (open PRs, active blockers) — put anything that
should outlive the current sprint in those files instead, not here.
