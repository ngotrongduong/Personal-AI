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
LLM-to-rule-engine safety boundary.

A first attempt at task 4 then surfaced a second prerequisite gap:
`RuleEngine` had no thread synchronization at all, which is a real data
race once a background planner thread starts calling `enable_rule`/
`disable_rule` concurrently with the main thread's per-frame `evaluate()`.
Fixed with a `threading.RLock` matching `GameState`'s existing pattern
(task 3b, PR #22, safety-reviewed PASS, new concurrency test).

**Task 4 is now done and merged**: `agent/planner_scheduler.py`
(`PlannerScheduler`, PR #24) runs `LlmPlanner.plan_once` on its own daemon
background thread at a configurable interval (default 5s), fully isolated
from the Tk UI thread and the fast loop. Idempotent `start()`/`stop()`,
prompt interruptible shutdown, per-cycle exception isolation (one bad
Ollama/network error can't kill the thread). Not yet wired into
`main.py`'s lifecycle — that's a separate future task. Safety-reviewed
PASS. 94/94 tests pass on `feature/v0.4-llm-planner`.

**Task 5 is now done and merged**: as expected, `LlmPlanner.plan_once` and
`PlannerScheduler` already failed closed on every Ollama/validation error
path, so this task was verification + documentation rather than new logic.
Added explicit test coverage in `tests/test_llm_planner.py` (full rule-state
snapshot comparison across CONNECTION/TIMEOUT/HTTP_STATUS/RESPONSE_FORMAT
Ollama errors, missing response text, and directive-validation rejection)
and a docstring note on `plan_once` recording the fail-closed contract for
future reviewers. Safety-reviewed PASS (docstring + tests only, no logic
change). Merged via PR #26. 95/95 tests pass.

**Task 6 is now done and merged**: `agent/planner_config.py` (`PlannerConfig`
+ `load_planner_config`, PR #31) adds the config surface — model name,
Ollama host/port, poll interval, all reusing `OllamaClientConfig`'s own
validation — plus an explicit `enabled` toggle that defaults to off at
two independent layers (the loader's defaults, and the dataclass's own
`__post_init__`), so it can't be silently turned on. Example block added
to `configs/example_game_v0.3.json`. Not wired into `main.py`'s runtime
yet (deliberately out of scope, same as `PlannerScheduler`). Safety-
reviewed PASS. 114/114 tests pass.

**Task 7 is now done and merged**: `PlannerScheduler._run` was discarding
`LlmPlanner.plan_once`'s `PlannerOutcome` return value entirely, so
nothing observed planner decisions unless a cycle raised an exception.
Fixed by logging the outcome (accepted directive / rejected+reason /
Ollama error, already encoded in `PlannerOutcome.message`) at INFO after
each successful cycle, mirroring `main.py`'s existing DISPATCHED/BLOCKED
logging for `ActionDispatcher`. No change to `LlmPlanner`, `RuleEngine`,
`OllamaClient`, `ActionDispatcher`, or `main.py`. Safety-reviewed PASS
(no findings). PR #33. 115/115 tests pass.

Next up: task 8 (tests with a fake/stub Ollama client). Task 8 remains
Codex's default lane; tasks 9 (real Ollama install + live smoke test)
and 10 (optional UI) are Claude's/TBD lane per `AGENTS.md`'s default
routing.

**Previously-unreviewed branches: both resolved (2026-09-23).** The two
external Codex branches noted above turned out to originate from the user
separately asking ChatGPT to research useful local AI models on GitHub.
Both were reviewed (dry-run 3-way merge test + full test suite +
`safety-reviewer` PASS) and merged:

- `codex/v0.4-model-foundation` → merged into `feature/v0.4-llm-planner`
  via PR #29. Adds the `model_runtime` package (see "Foundational
  infrastructure" in `docs/PLAN.md`). Purely additive, doesn't touch the
  planner/dispatcher safety boundary, never auto-downloads models.
- `codex/v0.3-release-metadata` → its raw diff against `main` looked like
  a regression at first glance (it appeared to delete `vision/ocr.py`
  etc.) because the branch was based on a pre-OCR commit — those were
  divergence artifacts, not real changes. A real 3-way merge test showed
  it only fixes two things: `main.py`'s `APP_VERSION` was still `"0.2.0"`
  despite v0.3 being fully merged, and `docs/ARCHITECTURE.md` still
  described the dispatcher as future work. Merged into `main` via PR #28
  (squash), 64/64 tests pass, no files deleted.

Both throwaway local test branches and the merged remote branches have
been deleted. `main` is now at `APP_VERSION = "0.3.0"` with an accurate
architecture doc; `feature/v0.4-llm-planner` has 107/107 tests passing
(94 planner tests + 13 new `model_runtime` tests).

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

Delegate v0.4 task 5 (fallback/timeout handling) or task 6 (config surface) to
Codex — see `docs/PLAN.md`'s checklist notes for task 5 on why it may mostly be
verification/docs rather than new code, since `LlmPlanner.plan_once` (PR #20)
and `PlannerScheduler` (PR #24) already fail closed and isolate exceptions.
Depends on task 4 (done, PR #24). PRs target `feature/v0.4-llm-planner`, not
`main`.

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
