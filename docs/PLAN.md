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
| 4 | Planner cadence/timer, isolated from the fast loop | Not started | Codex (default) | Runs on its own interval on a background thread/timer; explicit non-blocking behavior toward the Tk UI thread and the per-frame vision/rule loop. Unblocked now that `RuleEngine` is thread-safe (task 3b). |
| 5 | Fallback/timeout handling | Not started | Codex (default) | Ollama unreachable/slow/invalid-output paths all fall back to "keep last-known rule configuration, log why, keep running" — no exceptions escape into the fast loop. |
| 6 | Config surface | Not started | Codex (default) | Model name, Ollama host/port, poll interval, and an explicit enable/disable toggle (defaults to **off**, mirroring input-control's default-off pattern) in the game profile config. |
| 7 | Planner decision log | Not started | Codex (default) | Log every planner cycle's outcome (directive accepted / rejected+reason / Ollama error) the same way the v0.3 action dispatcher logs DISPATCHED/BLOCKED. |
| 8 | Tests with a fake/stub Ollama client | Not started | Codex (default) | Mirrors the v0.3 OCR pattern (fake `OcrEngine`): unit tests must not require a real Ollama install. Cover schema validation, rejection of malformed/out-of-vocabulary output, and the fallback/timeout path. |
| 9 | Real Ollama Windows install + live integration smoke test | Not started | Claude (machine-required) | Install Ollama, pull a small local model, run the actual planner end-to-end against the existing rule engine/dispatcher on a harmless/offline target; confirm F8 and all v0.3 gates still hold with the planner active. |
| 10 | Minimal planner visibility in UI (optional) | Not started | Claude or Codex, TBD | A small status label/log line in `main.py`'s existing UI showing current planner state (last directive, last call latency/status). Defer if it adds meaningful `main.py` complexity — not required for the acceptance criteria below. |

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
