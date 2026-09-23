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

v0.3 ("Game State + Rules", Issue #1) is **done and merged into `main`**
(PR #2, merge commit `ef3ad40`). Issue #1 is closed. All 10 planned items and
all 5 acceptance criteria are satisfied — see `docs/PLAN.md` for the per-task
breakdown. 64/64 tests pass, ruff clean, on `main`.

Next milestone per `docs/ROADMAP.md`: **v0.4 — Local AI planner** (LM Studio/Ollama
backend for high-level strategy; fast reactions stay deterministic/state-machine
based). No branch/issue opened for it yet.

Claude handles work that genuinely needs the user's Windows machine. Codex/ChatGPT
defaults to pure logic, algorithms, tests, docs, and config.

### v0.3 history (for reference)

PRs #3 (collab tooling), #6 (Issue #4 callback fix), #5 (Multiple Named
Detectors → GameState), and #7 (handoff/plan tracking docs) were merged into
`feature/v0.3-game-state` early in the milestone.

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

PR #11 (`codex/v0.3-ocr`, task 4: basic OCR for simple text/numbers) — `vision/ocr.py`
(`OcrEngine` protocol, `PytesseractEngine`), `agent/ocr_state_bridge.py`, ROI/whitelist/
confidence-threshold filtering. Live-smoke-tested on Windows against real Tesseract
(UB-Mannheim 5.4.0 via winget): found and fixed (Codex) a confidence-clamping bug where
whitelist-filtered text with Tesseract-reported `-1` confidence was silently dropped
instead of retained at zero confidence — two regression tests added. Realistic tight-ROI
digit reads verified at 0.93+ confidence. Squash-merged into `feature/v0.3-game-state`;
64/64 tests pass, ruff clean on the merged branch.

### Next task

Start scoping v0.4 (local AI planner) per `docs/ROADMAP.md`: decide LM Studio vs.
Ollama, define the boundary between the deterministic rule engine (fast reactions,
already built in v0.3) and the local-LLM planner (high-level strategy only), and
open a tracking issue before assigning implementation work.

Re-check GitHub before starting new work because this file is a snapshot.

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

None open right now. Issue #1 (v0.3) and Issue #4 are both completed/closed.

## Lessons

- Before merging an already-reviewed PR, verify its current HEAD again. A later
  push can otherwise be left behind by a fast merge.
- Put handoff/plan updates in the same push as the meaningful work they describe.

## Longer-term plan

See `docs/ROADMAP.md` for milestone planning and `docs/ARCHITECTURE.md` for
runtime responsibilities.
