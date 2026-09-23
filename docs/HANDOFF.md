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
(PR #2, merge commit `ef3ad40`). Issue #1 is closed. 64/64 tests pass, ruff
clean, on `main`.

**v0.4 ("Local AI Planner", Issue #15) is now scoped and in progress** on
`feature/v0.4-llm-planner`. Backend decision: **Ollama** (user confirmed,
2026-09-23) — headless REST API, no GUI dependency, fits local scripted
verification. See `docs/PLAN.md` for the full checklist/design constraint
(the planner proposes directives from a closed vocabulary; it can never
synthesize raw input or bypass any v0.3 safety gate).

Tasks 1-3 are **done and merged** into `feature/v0.4-llm-planner`:
`agent/ollama_client.py` (stdlib Ollama REST client, PR #17),
`agent/llm_planner_schema.py` (closed-vocabulary directive parser —
`enable_rule`/`disable_rule`/`noop`; `set_priority` was deferred, PR #17),
and `agent/llm_planner.py` (`LlmPlanner.plan_once`: prompt from `GameState`
→ Ollama → `parse_directive` → `RuleEngine.enable_rule`/`disable_rule` only,
fails closed on any error, PR #20). Along the way, task 3 surfaced that
`RuleEngine` had no enable/disable-rule API at all — that gap was fixed
first (`RuleEngine.enable_rule`/`disable_rule`/`is_rule_enabled`, PR #19)
before task 3 could be built. Both PR #19 and PR #20 got an explicit
`safety-reviewer` PASS before merging, since they touch the core
LLM-to-rule-engine safety boundary. 89/89 tests pass on
`feature/v0.4-llm-planner`.

Next up: task 4 (planner cadence/timer isolated from the fast loop/Tk UI
thread) — depends on task 3, ready to hand to Codex. Tasks 5-8 remain
Codex's default lane; tasks 9 (real Ollama install + live smoke test) and
10 (optional UI) are Claude's/TBD lane per `AGENTS.md`'s default routing.

**Operational note:** Codex's CLI sandbox intermittently denies git writes
(can't reliably run `git checkout -b`/`git commit` itself, even though it
can edit/create files fine). Workaround: tell it explicitly to run no git
commands at all and just edit files in the working tree; Claude branches/
commits/pushes afterward. Apply this to future Codex delegations here.

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

Delegate v0.4 task 4 (planner cadence/timer) to Codex: runs `LlmPlanner.plan_once`
on its own slow interval (seconds, not frames) on a background thread, explicitly
non-blocking toward the Tk UI thread and the per-frame vision/rule loop. Depends
on task 3 (done, PR #20). PRs target `feature/v0.4-llm-planner`, not `main`.

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

- **#15** — v0.4 Local AI Planner. `docs/PLAN.md` mirrors its checklist with
  status/owner columns.

Issue #1 (v0.3) and Issue #4 are both completed/closed.

## Lessons

- Before merging an already-reviewed PR, verify its current HEAD again. A later
  push can otherwise be left behind by a fast merge.
- Put handoff/plan updates in the same push as the meaningful work they describe.

## Longer-term plan

See `docs/ROADMAP.md` for milestone planning and `docs/ARCHITECTURE.md` for
runtime responsibilities.
