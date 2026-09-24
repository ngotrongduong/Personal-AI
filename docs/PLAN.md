# v0.4 detailed plan — Local AI Planner

Granular checklist for the current milestone (GitHub Issue #15), with status
and owner, so progress can be checked without opening GitHub. This is the same
checklist as Issue #15 — **keep them in sync**: when you tick something here,
tick/comment it there too (and vice versa), same commit/timeframe as the work.

For "what's the current PR/branch situation right now," see `docs/HANDOFF.md`
instead — that one changes faster than this file should.

The v0.3 version of this file (task-by-task history for Game State + Rules) is
preserved in git history on `main` before this milestone started; see
`docs/ROADMAP.md`'s Completed section for the summary.

## Goal

Add a local LLM "planner" layer that can choose high-level goals/strategy on a
slow cadence, sitting strictly *above* v0.3's deterministic rule engine and
gated action dispatcher — never inside the fast per-frame loop, and never able
to bypass any existing safety gate (see `AGENTS.md`'s non-negotiable invariants
and `docs/ARCHITECTURE.md`'s "Why the LLM is not in the fast loop").

Backend: **Ollama** (REST API on `localhost:11434`, runs headless as a local
service — no GUI dependency, scriptable/testable, easy model pulls via
`ollama pull`). Chosen over LM Studio because LM Studio's Local Server needs
the desktop app open and the toggle enabled manually, which doesn't fit
automated local verification the way `scripts/test.ps1` currently works.

## Design constraint (read before implementing anything)

The planner proposes; it never directly acts. Concretely:

- The LLM's output is **not** free text executed as-is. It must be parsed into
  a fixed, code-reviewed vocabulary of directives (e.g. "enable rule X",
  "disable rule X", "set priority target Y") that map onto existing
  `RuleEngine`/rule-config controls.
- The LLM can never synthesize a raw `ActionIntent`, click, or keypress
  directly. Only the existing `RuleEngine` → gated `ActionDispatcher` path
  (from v0.3) may produce input, and all of its gates (input-enabled, F8,
  cooldown, freshness, resolvable target) stay fully intact and unmodified.
- If Ollama is unreachable, slow, times out, or returns output that fails
  schema validation, the system keeps running on the last-known-good rule
  configuration. The LLM is strictly optional for the deterministic loop to
  keep functioning — never a single point of failure for safety.
- The planner runs on its own slow timer (seconds, not frames) on a background
  thread; it must never block the Tk UI thread or the fast vision/rule loop.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 1 | Ollama HTTP client wrapper | Done, merged | Codex | `agent/ollama_client.py`: stdlib-only, non-streaming `/api/generate`, injectable `Transport` for tests, `OllamaResult`/`OllamaError` with kind (CONNECTION/TIMEOUT/HTTP_STATUS/RESPONSE_FORMAT). 6 tests in `tests/test_ollama_client.py`. PR #17. |
| 1b | `RuleEngine.enable_rule`/`disable_rule`/`is_rule_enabled` | Done, merged | Codex | Not originally its own row — added when task 3 found `RuleEngine` had no such API. Disabled rules skipped in `evaluate()` before cooldown logic; cooldown state untouched by disable/enable. Safety-reviewed (PASS). PR #19. |
| 2 | Planner directive schema + validator | Done, merged | Codex | `agent/llm_planner_schema.py`: closed vocabulary is `enable_rule`/`disable_rule`/`noop` (no `set_priority` — deferred, not needed yet); exact-field validation, rejects unknown type/rule/extra fields. 9 tests in `tests/test_llm_planner_schema.py`. PR #17. |
| 3 | `agent/llm_planner.py` | Done, merged | Codex | `LlmPlanner.plan_once(state)`: builds prompt from `GameState` + rule enabled/disabled settings, calls Ollama, validates via `parse_directive`, applies accepted directives via `RuleEngine.enable_rule`/`disable_rule` only. Fails closed (rule state unchanged) on any Ollama or validation error, no exception escapes. Safety-reviewed (PASS). 7 tests in `tests/test_llm_planner.py`. PR #20. |
| 3b | `RuleEngine` thread-safety (`RLock`) | Done, merged | Codex | Not originally its own row — a first attempt at task 4 correctly flagged that `RuleEngine` had no synchronization around `_rules`/`_disabled_rule_names`/`_last_emitted_at`, which is a real data race once a background planner thread calls `enable_rule`/`disable_rule` concurrently with the main thread's `evaluate()`. Added `threading.RLock` matching `GameState`'s existing pattern; purely synchronization, no behavior/gate changes. New concurrency test (1000 concurrent toggles vs. 1000 `evaluate()` calls). Safety-reviewed (PASS). PR #22. |
| 4 | Planner cadence/timer, isolated from the fast loop | Done, merged | Codex | `agent/planner_scheduler.py`: `PlannerScheduler` runs any `Planner`-protocol `plan_once(state)` on its own daemon thread, configurable interval (default 5s). `start()`/`stop()` idempotent; `stop()` interrupts the wait promptly via `Event` + bounded `join(timeout=1.0)`. Per-cycle exceptions caught/logged individually — one bad cycle can't kill the thread or leak into the fast loop. Wired into `main.py` during task 9 preparation through `PlannerController`. Safety-reviewed (PASS). 4 tests in `tests/test_planner_scheduler.py`, using real background-thread concurrency. PR #24. |
| 5 | Fallback/timeout handling | Done, merged | Codex | Verified/documented rather than new logic: `agent/llm_planner.py`'s `plan_once` already failed closed on every Ollama error kind (CONNECTION/TIMEOUT/HTTP_STATUS/RESPONSE_FORMAT), missing response text, and directive-validation rejection. Added explicit test coverage in `tests/test_llm_planner.py` asserting exact rule-state is unchanged (full snapshot, not just the two rules under test) and `changed=False` for every failure path; added a docstring note on `plan_once` recording the fail-closed contract. No behavior change to `LlmPlanner`/`RuleEngine`/`PlannerScheduler`/`OllamaClient`. Safety-reviewed (PASS). PR #26. |
| 6 | Config surface | Done, merged | Codex | `agent/planner_config.py`: `PlannerConfig` (`enabled`, `ollama: OllamaClientConfig \| None`, `interval_seconds`) + `load_planner_config(profile)` reading an optional `"planner"` block from a game-profile mapping, reusing `OllamaClientConfig`'s own model/host/port/timeout validation. Defaults to fully disabled when the profile has no `"planner"` key; `enabled=True` without an Ollama config is rejected both by the loader and by the dataclass's own `__post_init__`, so the default-off invariant can't be bypassed by direct construction. Example block added to `configs/example_game_v0.3.json`; the minimal task 9 UI builds this same validated config at enable time. Safety-reviewed (PASS). 7 tests. PR #31. |
| 7 | Planner decision log | Done, merged | Codex | Found that `PlannerScheduler._run` was discarding `LlmPlanner.plan_once`'s `PlannerOutcome` return value entirely — nothing observed planner decisions unless a cycle raised. Fixed by logging the outcome (`PlannerOutcome.message`, already encoding accepted directive / rejected+reason / Ollama error) at INFO after each successful cycle, mirroring `main.py`'s existing DISPATCHED/BLOCKED logging for `ActionDispatcher`. No change to `LlmPlanner`/`RuleEngine`/`OllamaClient`/`ActionDispatcher`/`main.py`. Safety-reviewed (PASS, no findings). 1 new test. PR #33. |
| 8 | Tests with a fake/stub Ollama client | Done, merged | Codex | Audit showed tasks 1-5 already covered most of this with injected fake transports (no real Ollama needed); filled the remaining gaps. `test_ollama_client.py` +4 (missing/non-string `response` field, non-UTF-8 body, `URLError`-wrapped timeout, connection refusal). `test_llm_planner_schema.py` +6 (missing/non-string `type`, empty/missing `rule_name`, prose/markdown-wrapped JSON, action-smuggling shapes, case variants). New `test_planner_integration.py` (3): first composed test of real `OllamaClient` (fake transport) + `LlmPlanner` + `RuleEngine` + `PlannerScheduler` on its background thread. Tests only, no production change; no bugs found. Safety-reviewed (PASS). 128/128. PR #35. |
| 9 | Real Ollama Windows install + live integration smoke test | Done, merged | Codex (wiring), Claude (install + live smoke) | Ollama 0.34.3 + `qwen3.5:9b` installed. New `agent/planner_controller.py` (`PlannerController`, non-blocking `stop()`, `_CancellableRuleControl` → `PlannerCancelledError` for post-stop directives) + default-off "Planner (Ollama)" panel in `main.py`; F8/close release input before stopping the planner. Live fixes: `"think": false` in `OllamaClient` (thinking model returned empty/HTTP 500 under `format:"json"`), exact JSON shapes in the prompt. Live results: correct noop/enable decisions, stop <1ms, in-flight directive discarded, unreachable Ollama fails closed; GUI smoke on Notepad confirmed BLOCKED dispatch with input off, planner doesn't enable input, F8 stops both. Safety-reviewed twice (PASS). 137/137. PR #37. |
| 10 | Minimal planner visibility in UI (optional) | Done, merged | Claude (Codex was over its usage limit) | `PlannerScheduler` gained an optional observation-only `on_cycle` callback that receives a frozen `PlannerCycleReport` (status ok/cancelled/error, message, changed, duration, finished_at) after every cycle; callback errors are logged and never kill the thread; existing log lines unchanged. `PlannerController.start(..., on_cycle=...)` passes it through. `main.py` shows "Last cycle: HH:MM:SS · latency · status · message" under the planner status, marshaled onto Tk via `root.after(0)`; a Tk-thread-only generation counter (bumped on every start/stop: toggle, F8, clear rules) drops stale reports. Live smoke with `qwen3.5:9b`: label updated (11.0s cold, 2.5s warm, `noop`), F8 reset it to "—" and no stale report overwrote it. 9 new tests (incl. Tk-level stale-report tests in `tests/test_main_planner_visibility.py`), 146/146. Safety-reviewed (PASS; 3 Minor fixed: report built inside the callback guard, CHANGELOG, Tk tests). PR #38. |
| R | Release close-out (v0.4.0) | In review | Claude | Merged `main` into the v0.4 line (PR #28's version/architecture fix; `APP_VERSION` conflict resolved to `0.4.0`), updated `CHANGELOG.md` (v0.3.0 + v0.4.0 entries), `docs/ROADMAP.md` (v0.4 → Completed, next v0.5), `docs/ARCHITECTURE.md` (planner layer, stale OCR note), `README.md` header/overview. Then feature → `main` PR and close Issue #15. |

## Foundational infrastructure (outside the numbered checklist)

`model_runtime/` (types, `ModelProvider` protocol, `ModelCatalog`, `ModelRouter`,
`OllamaProvider`) plus `configs/models.v1.json` and `docs/MODELS.md` were merged
2026-09-23 (PR #29) from an external Codex research pass the user ran separately
(researching local AI models on GitHub for future roles: `visual_reasoner`,
`memory_embedding`, `heavy_reasoner`, `ocr_specialist`, `speech_to_text`). It is
purely additive groundwork — does not touch `agent/llm_planner.py`,
`agent/rule_engine.py`, `agent/planner_scheduler.py`, `agent/ollama_client.py`,
or `main.py`, and is not wired into the planner/dispatcher safety boundary or
the fast loop. `ModelRouter` never auto-downloads models; `scripts/models.ps1`
is an explicit, user-invoked `ollama pull`/`list` wrapper. Safety-reviewed
(PASS). 107/107 tests pass (13 new: `test_model_catalog.py`,
`test_model_router.py`, `test_ollama_provider.py`). Not yet used by any
numbered task above — future tasks that need a second model role (vision/OCR/
embedding) should route through this instead of hand-rolling another Ollama
client.

## Acceptance criteria

- The planner can only select from a predefined, code-reviewed set of
  directives — it cannot emit arbitrary keystrokes/clicks or any free-form
  command that bypasses `RuleEngine`/`ActionDispatcher`.
- All v0.3 safety invariants (F8 emergency stop, explicit input-enable gate,
  cooldown/debounce, dispatcher's 4 gates) remain fully intact and are not
  modified or special-cased by the planner's presence.
- If Ollama is unreachable, slow, or returns invalid output, the system keeps
  running deterministically on the existing/last valid rule configuration —
  no crash, no unsafe fallback action.
- Every planner cycle's outcome (accepted directive, rejected+reason, or
  connection error) is logged.
- The full loop — vision → state → rules → dispatcher, with the planner
  layered on top — is live-tested end-to-end on a harmless/offline target on
  Windows before targeting any real game profile, mirroring v0.3's task 10.

## Explicitly out of scope for v0.4

- Free-form or unconstrained LLM-generated actions of any kind.
- Per-frame or fast-loop LLM calls — the planner is strictly a slow-cadence,
  above-the-rule-engine layer.
- Demonstration recording / imitation learning (that's v0.5, see
  `docs/ROADMAP.md`).
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior — standing
  invariant from `AGENTS.md`, unaffected by this milestone.
