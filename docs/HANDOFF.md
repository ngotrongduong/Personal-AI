# Handoff / current state

Read this before touching anything else — before `AGENTS.md`'s other docs, before
`git log`. It exists so a new session does not have to reconstruct current work
from commit history or old chat scrollback.

**Update discipline:** whoever finishes a meaningful chunk of work updates this
file and `docs/PLAN.md` in the same push as the work.

## Orientation checklist for a new session

1. Read `AGENTS.md`, this file, and `docs/PLAN.md`.
2. `git fetch --all` and check open branches/PRs before starting anything.
3. Check the GitHub Actions CI result on the PR (Windows runner: compile,
   ruff, pytest). Also run `scripts/test.ps1` locally for anything touching
   live GUI/capture/input behavior, which CI cannot exercise.
4. Update this file and `docs/PLAN.md` before stopping if the picture changed.

## Right now (2026-09-24)

**v0.7 "Closed-loop planner" (Issue #61) is in progress** on
`feature/v0.7-closed-loop-planner`, branched from `main` at the v0.6.0 release.
Task 0 (kickoff: issue, branch, `docs/PLAN.md` spec, `AGENTS.md` planner
invariant, draft PR #63 feature→`main`) and task 1 (`run_skill`
directive, closed-loop prompt, `StepHistory`, `ProposalMailbox`) and task 2
(`Autopilot` approve/auto state machine) are done. The user approved the
design on 2026-09-24:
- the LLM proposes `run_skill` with a skill name only;
- approve-each-step is the default, and auto mode is opt-in with a step cap and
  auto-off triggers;
- a Goal field is saved as `planner.goal`;
- v0.4 rule toggles are kept and still apply directly.

Codex's CLI is out of quota until 2026-09-25 13:55, so Claude implements.

**v0.6.0 "Game Profiles + Skills" (Issue #50) is released.** The
integration branch `feature/v0.6-profiles-skills` merged into `main` via PR #52
as a merge commit (`f1cc71c`).
The user chose to move toward v1.0 in steps:
- v0.6 profiles + skills, no LLM;
- v0.7 a closed-loop planner that picks skill *names* only, approve-each-step
  by default, with an explicit opt-in auto mode;
- v0.8 session memory;
- v1.0 integration.

Task 0 (kickoff) covered the issue, the branch, `docs/PLAN.md`,
`AGENTS.md`, this file, `docs/ROADMAP.md`, `profiles/*` gitignored, and the
draft PR feature→`main` (PR #51; the draft release PR is #52). Task 1
(`agent/skills.py`) merged via PR #53, task 2 (`agent/profile.py` plus
`profiles/example/profile.json`) via PR #54, task 3 (dispatcher `press`/`hold`,
foreground check, allowlist re-check, rate limit, cancel) via PR #55, task 4
(`agent/skill_executor.py`, with a safety-reviewer pass) via PR #56, task 5
(UI Profile panel: Load/Save) via PR #57, task 6 (Skills panel, rule→skill
through the executor, F8 wiring, with a safety-reviewer pass) via PR #58.
Task 7, the live Notepad smoke test, passed on 2026-09-24, including F8 inside
the real pynput hook releasing a held key at once, a click skill rule, and a
key skill rule BLOCKED while Notepad was not foreground (details in PLAN row 7).
Codex was retried on 2026-09-24, but its CLI still reported the usage limit
(reset 2026-09-25 13:55), so Claude keeps implementing. Task R (release
close-out: CHANGELOG, README, ROADMAP, ARCHITECTURE, AGENTS, `APP_VERSION =
"0.6.0"`) followed, then PR #52 closed Issue #50.

Local leftover branches could not be deleted by Claude (a `git branch -D` was
denied) and are safe for the user to delete: `codex/v0.6-profile`,
`codex/v0.6-dispatcher`, `claude/v0.6-skills`, `claude/v0.6-executor`,
`claude/v0.6-profile-ui`, `claude/v0.6-skills-ui`, `claude/v0.6-smoke`,
`claude/v0.6-release`.
See `docs/PLAN.md` for the design constraint and checklist.

**v0.5.0 ("Demonstration recording", Issue #41) is released**, merged into
`main` via PR #43 as a merge commit (`2f1e408`).

### v0.5 history (for reference)

Integration branch: `feature/v0.5-demo-recording` (branched from `main` at the v0.4.0
release). Task 0 (kickoff: issue, branch, `docs/PLAN.md`, `AGENTS.md`,
`recordings/` gitignored, draft PR feature→`main`) is done. Tasks 1-3
(`recording/schema.py`, `recording/session_writer.py`,
`recording/input_recorder.py`) were implemented by Claude and merged via
PR #44 because Codex hit its usage limit (reset 2026-09-25 13:55). Task 4
(`recording/recorder_controller.py`, `RecordingController`) was also done by
Claude and merged via PR #45. Task 5 (the `main.py` "Recording" panel) was done
by Claude and merged via PR #46. Task 6 (`recording/dataset.py`,
`recording/review.py`, `scripts/recordings.py`: `list` / `validate` / `export` /
`review`) was done by Claude and merged via PR #47 (safety-reviewed PASS WITH
NOTES, all fixed). 267 tests pass, plus 1 skipped symlink test that needs
Windows Developer Mode. Task 7 was a live Windows smoke test on Notepad at 150%
scale and passed. Enabling input control stopped recording and F8 stopped it;
focus lost/gained was logged; 0 frames were dropped at 10 fps; injected input
was dropped; `validate`/`export`/`review` worked on the real sessions. Details
are in `docs/PLAN.md` row 7. See `docs/PLAN.md` for the design
constraint (recording only listens, never sends input, is mutually exclusive
with autonomous input control, and records input only while the game window is
foreground).

**v0.4.0 ("Local AI Planner", Issue #15) is released: merged into `main`**
via PR #16 (merge commit `5b24233`). Issue #15 is closed, and
`feature/v0.4-llm-planner` plus its sub-branches were deleted. `main` has
`APP_VERSION = "0.4.0"`; 146/146 tests pass, ruff clean.

v0.3 ("Game State + Rules", Issue #1) was merged into `main` earlier (PR #2,
merge commit `ef3ad40`). Issue #1 is closed.

### v0.4 history (for reference)

**v0.4 was built** on
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

**Task 8 is now done and merged**: existing tests already used injected
fake Ollama transports throughout, so this filled the remaining untested
branches — Ollama response-format edge cases (missing/non-string
`response`, non-UTF-8 body), `URLError`-wrapped timeouts, schema
rejection of malformed/prose-wrapped/action-smuggling/case-variant
directives — plus the first composed end-to-end test
(`tests/test_planner_integration.py`: real `OllamaClient` with fake
transport → `LlmPlanner` → `RuleEngine`, driven by `PlannerScheduler`'s
background thread). Tests only, no production change, no bugs found.
Safety-reviewed PASS. PR #35. 128/128 tests pass.

**v0.4 task 9 done (2026-09-23): real Ollama + live smoke test.** Ollama
0.34.3 is installed on the Windows dev machine (`%LOCALAPPDATA%\Programs\Ollama`,
server on 127.0.0.1:11434) with model `qwen3.5:9b` (~6.6 GB) pulled.

Wiring (Codex): `agent/planner_controller.py`'s `PlannerController` owns the
optional scheduler lifecycle (`OllamaClient` → `LlmPlanner` →
`PlannerScheduler`, no input/dispatch role). `main.py` gained a default-off
"Planner (Ollama)" panel (enable checkbox, model default `qwen3.5:9b`, interval
≥ 1.0s, status label); planner INFO logs are forwarded to the Tk log. F8,
Clear Rules, and window close stop the planner, and F8/close release input
*before* stopping it. `stop()` is non-blocking (`join_timeout=0.0`), and a
per-start `_CancellableRuleControl` proxy makes any in-flight directive after
stop raise `PlannerCancelledError` (logged once as "discarded after stop; rule
settings unchanged", no misleading `changed=True` outcome).

Live findings fixed: `qwen3.5` is a thinking model — with `format:"json"` and
no `think` flag Ollama returned an empty `response` or HTTP 500, so
`OllamaClient` now sends `"think": false`. The model also invented keys
(`{"action": "noop"}`) until the prompt listed the three exact JSON shapes.
`parse_directive` remains the only authority.

Live results: headless planner 4/4 correct noops (~2.3s/call); scheduler with
the real model correctly enabled a disabled rule whose detector was visible;
`stop()` returned in <1ms and in-flight directives were discarded; unreachable
Ollama kept rules unchanged. GUI smoke on a harmless Notepad window: template
detector FOUND, rule dispatches stayed BLOCKED with input disabled, enabling
the planner did not enable input, planner cycles logged `noop`, F8 unticked
input *and* stopped the planner ("disabled by emergency stop"), and no cycles
ran afterwards. Real click dispatch was not exercised in this run (v0.3 gates
unchanged). Safety-reviewed twice (PASS; all Important/Minor findings fixed).
137/137 tests.

Follow-ups (not blocking): planner rule changes are logged without a reason
(could add an optional `reason` field to the directive schema); a quick
disable/re-enable can leave a cancelled worker waiting on HTTP (up to the 30s
timeout) alongside the new one — harmless, just wasted Ollama work; there are no
Tk-level tests for the planner panel.

**v0.4 task 10 done (2026-09-24): planner visibility in the UI.** Codex hit
its usage limit, so Claude implemented it directly. `PlannerScheduler` has an
optional observation-only `on_cycle(PlannerCycleReport)` callback;
`PlannerController.start` passes it through; the planner panel shows
"Last cycle: HH:MM:SS · latency · status · message". Stale reports from a
stopped scheduler are dropped via a Tk-thread-only generation counter. Live
smoke on the real model: 11.0s cold / 2.5s warm `noop` cycles displayed; F8
reset the label and stopped the planner. Safety-reviewed PASS (3 Minor
fixed). 146/146 tests. PR #38.

All numbered v0.4 tasks (1-10) are now done. **Release close-out (2026-09-24):**
`main` (PR #28) was merged into the v0.4 line on `claude/v0.4-release-prep`,
resolving the `APP_VERSION` conflict to `"0.4.0"`; `CHANGELOG.md` gained
v0.3.0 and v0.4.0 entries, `docs/ROADMAP.md` moved v0.4 to Completed (next:
v0.5 demonstration recording), `docs/ARCHITECTURE.md` documents the planner
layer, and `README.md` has a v0.4 overview (PR #39, a merge commit so `main`
stays an ancestor). The same pass fixed flaky test isolation: Tk objects from
`test_main_planner_visibility.py` could be garbage-collected on a later
test's background thread, where tkinter stalls about 1s. `tearDown` now runs
`gc.collect()` on the main thread. The feature branch was then merged into
`main` (PR #16, which closed Issue #15).

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

v0.7 task 3: the scheduler `should_plan` gate and the controller wiring
(cancellable proposal sink, generation per start; see `docs/PLAN.md`).
Never commit a recording, a profile template PNG or any other user data,
because the repo is public.

Open v0.5 follow-up (not blocking): per-monitor DPI awareness is currently set
implicitly by importing `dxcam`. Calling `SetProcessDpiAwareness(2)` explicitly
at startup would make this robust.

Ollama and `qwen3.5:9b` are installed locally (v0.4, not needed for v0.5).

Open v0.4 follow-ups (not blocking; could become small issues):
- an optional directive `reason` field;
- a cancelled worker can linger on HTTP after a fast disable/re-enable;
- the planner's `_schedule_planner_cycle_report` and `_PlannerLogHandler` call
  `root.after` from the scheduler thread, which can block that thread while the
  Tk thread is busy or closing; the Recording panel (v0.5 task 5) uses a
  `SimpleQueue` drained by `_poll_preview` instead, and the planner could too.

Re-check GitHub before starting new work because this file is a snapshot.

### CI status

Resolved 2026-09-24: the repository is now **public**, so GitHub Actions runs
again (it had been blocked by the owner's account billing/spending-limit
state). `.github/workflows/ci.yml` runs compile + ruff + pytest on
`windows-latest` / Python 3.14 for pushes and PRs on `main` and `feature/**`;
runs #106-#110 (main, the v0.5 branch, PRs #42/#43) all passed.

Merge gate: green CI on the PR, plus a Windows smoke test when the task
touches live GUI/capture/input behavior (CI has no real desktop/game window).
Because the repo is public, never commit recordings, screenshots, templates,
logs, secrets, or other user data (`.gitignore` covers `recordings/`,
`snapshots/`, `templates/`, `logs/`, and `profiles/` except
`profiles/example/`).

### Open issue

Issue #61 (v0.7) is open. Issue #50 (v0.6), Issue #41 (v0.5), Issue #15 (v0.4), Issue #1 (v0.3)
and Issue #4 are all completed and closed.

## Lessons

- Before merging an already-reviewed PR, verify its current HEAD again. A later
  push can otherwise be left behind by a fast merge.
- Put handoff/plan updates in the same push as the meaningful work they describe.
- A PR that merges `main` into an integration branch must land as a merge
  commit, not a squash. Otherwise `main` is not an ancestor, and the release PR
  hits the same conflicts again.

## Longer-term plan

See `docs/ROADMAP.md` for milestone planning and `docs/ARCHITECTURE.md` for
runtime responsibilities.
