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

| PR/branch | Base | Owner | What it does |
|-----------|------|-------|---------------|
| #2 `feature/v0.3-game-state` | `main` | Codex | v0.3 integration branch itself, still draft |
| `codex/v0.3-ocr` | `feature/v0.3-game-state` | Codex | Task 4: OCR implementation ready for draft PR: `OcrEngine`/optional `PytesseractEngine`, clipped ROI reads, whitelist and confidence filtering (including retaining non-empty text when Tesseract reports `-1` confidence after whitelist filtering), GameState bridge, fake-engine tests. Real Tesseract remains a Windows smoke-test follow-up. |

### Merged since last update

PR #8 (`codex/v0.3-resource-bars`, task 3: HP/resource bar measurement) —
cross-checked on Windows with `scripts/test.ps1` (41/41 pass, ruff clean),
squash-merged into `feature/v0.3-game-state`.

PR #9 (`claude/live-detector-loop`, task 11: wire `DetectorRegistry`/vision→`GameState`
into the live Tk capture loop) — live-smoke-tested on Windows (two named templates,
both FOUND simultaneously; F8 verified unaffected). 31/31 tests, ruff clean.

PR #10 (`claude/action-dispatcher`, task 12: gated `ActionIntent` → `InputController`
dispatcher) — input-enabled/F8, supported-action, freshness, resolvable-target gates.
New `agent/action_dispatcher.py`, wired into `main.py`'s vision loop via a new "Rules"
UI section. Uses `self.capture.hwnd` (not the window-picker combobox) as the dispatch
target — a safety-reviewer subagent caught that these can diverge and it was fixed
pre-commit, with a regression test. Live-smoke-tested on Windows: real click correctly
dispatched into a throwaway Notepad window once input control enabled, correctly
blocked before that and after F8. Squash-merged into `feature/v0.3-game-state`;
55/55 tests pass, ruff clean on the merged branch.

### Next task

Tasks 3, 11, and 12 are done. Task 4 (OCR) is implemented on
`codex/v0.3-ocr` and is ready for a draft PR. Claude will install real Tesseract
and live-smoke-test it after the PR lands. After that, re-check `docs/PLAN.md`'s
acceptance criteria for what's left in v0.3.

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
