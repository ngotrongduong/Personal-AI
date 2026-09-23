# Handoff / current state

Read this before touching anything else — before `AGENTS.md`'s other docs, before
`git log`. It exists so a new session does not have to reconstruct current work
from commit history or old chat scrollback.

**Update discipline:** whoever finishes a meaningful chunk of work updates this
file and `docs/PLAN.md` in the same push as the work.

## Orientation checklist for a new session

1. Read `AGENTS.md`, this file, and `docs/PLAN.md`.
2. `git fetch --all` and check open branches/PRs before starting anything.
3. Run `scripts/test.ps1` locally before trusting any check; CI is blocked by
   GitHub account billing/spending-limit state.
4. Update this file and `docs/PLAN.md` before stopping if the picture changed.

## Right now (2026-09-23)

v0.3 ("Game State + Rules", Issue #1) is in progress on
`feature/v0.3-game-state` (PR #2 into `main`, still draft).

Claude handles work that genuinely needs the user's Windows machine. Codex/ChatGPT
defaults to pure logic, algorithms, tests, docs, and config.

### Merged into `feature/v0.3-game-state`

PRs #3 (collab tooling), #6 (Issue #4 callback fix), #5 (Multiple Named
Detectors → GameState), and #7 (handoff/plan tracking docs) are merged.

Claude independently verified the pre-#7 integration state with compile + Ruff +
pytest **28/28 pass**. PR #7 is docs/coordination only.

### Active work

| Branch/task | Owner | What it does |
|-------------|-------|--------------|
| `feature/v0.3-game-state` / PR #2 | Codex | v0.3 integration branch |
| Task #11 | Claude | Wire DetectorRegistry/vision→GameState into the live Tk capture loop and smoke-test at least two named templates |
| `codex/v0.3-resource-bars` / Task #3 | Codex | Pure HP/resource-bar measurement + GameState bridge; isolated tests 13/13 pass before push |

Re-check GitHub before merging because this table is a snapshot.

### Known blocker

GitHub Actions cannot start because of the repo owner's current account
billing/spending-limit state. This is unrelated to repository code. Until billing
is restored, the merge gate is local verification:

- compile check
- Ruff
- pytest
- Windows smoke test when the task touches live GUI/capture/input behavior

Record that local verification explicitly in each PR.

### Open issue

- **#1** — v0.3 Game State + Rules. `docs/PLAN.md` mirrors its checklist with
  status/owner columns.

Issue #4 is completed and merged via PR #6.

## Lessons

- Before merging an already-reviewed PR, verify its current HEAD again. A later
  push can otherwise be left behind by a fast merge.
- Put handoff/plan updates in the same push as the meaningful work they describe.

## Longer-term plan

See `docs/ROADMAP.md` for milestone planning and `docs/ARCHITECTURE.md` for
runtime responsibilities.
