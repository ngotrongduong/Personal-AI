# v1.0 detailed plan — Personal Game Agent

Granular checklist for the current milestone (GitHub Issue #82), with
status and owner, so progress can be checked without opening GitHub. This is
the same checklist as Issue #82. **Keep them in sync:** when you tick
something here, tick or comment it there too, in the same commit or timeframe
as the work.

For the current PR and branch situation, see `docs/HANDOFF.md` instead. That
file changes faster than this one should.

Earlier versions of this file are preserved in git history on `main`:
- v0.4 Local AI Planner;
- v0.5 Demonstration recording;
- v0.6 Game Profiles + Skills;
- v0.7 Closed-loop planner;
- v0.8 Session memory.

`docs/ROADMAP.md`'s Completed section summarizes them.

## Goal

v1.0 closes the loop the roadmap has pointed at since v0.4:
vision → state → plan → action → **observation**. The earlier steps were:

- v0.6 Profiles + Skills (released, Issue #50);
- v0.7 closed-loop planner that picks skills (released, Issue #61);
- v0.8 session memory (released, Issue #71);
- **v1.0 one personal game agent (this milestone)**.

v1.0 delivers:
- **Observed effects.** A skill may declare what it should change on screen
  (`expect`: a detector that should become visible or disappear within a few
  seconds). After the step, the app watches `GameState` and records the
  effect as `confirmed` or `not_seen`. The planner sees the effect in its
  recent steps, the session log records it, and auto mode counts `not_seen`
  as a failed step.
- **Agent runs.** One **Agent** panel with Preflight, Start Agent and Stop
  Agent:
  - *Preflight* checks everything a run needs (profile, capture, planner
    settings, Ollama and the model, enabled skills, input control) and shows
    each check with a clear reason.
  - *Start Agent* runs the preflight and starts the planner only when every
    required check passes.
  - A **run budget** (`planner.max_run_minutes`) and an optional **goal
    condition** (`planner.stop_when`: a detector seen on screen) end the run
    by themselves.
- **A user guide** (`docs/USER_GUIDE.md`) that takes a new user from install
  to a first supervised agent run.

Design decisions (made by Claude on 2026-09-25 under the user's standing grant
of full autonomy):
- An agent run *is* a planner session. The budget and the goal condition
  apply to every planner session, however it was started, and a run ends on
  every planner stop path (v0.8 session end).
- Expectations live next to the skill in `profile.json` but are parsed into a
  separate observation-only table. `agent/skills.py` does not change.
- The effect of a step is a separate session-log record (`effect`), written
  when it resolves, so v0.8 logs stay valid.

## Design constraint (read before implementing anything)

1. **Observation never adds input.**
   - Expectations, effect watches, preflight, the run budget and the goal
     condition only read `GameState`, the profile and the clock. They never
     build an `ActionIntent`, never call `SkillBook.build_intent`, the
     `SkillExecutor` or the `ActionDispatcher`.
   - `agent/skill_effects.py` and `agent/agent_session.py` never import
     `skills`, `rule_engine`, `autopilot`, `skill_executor`,
     `action_dispatcher`, `core.input_controller` or `pydirectinput`. A test
     enforces this.
2. **Stops only reduce activity.**
   - The budget, the goal condition and a failed preflight can stop the
     planner or refuse to start it. They never enable input control, a skill,
     a rule or auto mode.
   - Start Agent never turns on input control or auto mode. Both stay
     explicit user actions, and auto still needs its confirmation dialog.
   - `not_seen` can only turn auto off sooner (it counts toward the v0.7
     3-failures stop). It never retries a step.
3. **Expectations are bounded** (`agent/skill_effects.py`):
   - `expect` is `{"detector": <declared detector>, "visible": true|false,
     "within_seconds": (0, 10], "min_confidence": [0, 1]}`. The defaults are
     `visible` true, `within_seconds` 2.0 and `min_confidence` 0.8. Unknown
     fields and undeclared detectors are rejected on load.
   - Only an observation made *after the step finished* counts. A step that
     did not run (refused, failed, cancelled) gets no watch and no effect.
   - Effects are `confirmed`, `not_seen` or `none` (no expectation).
4. **At most one effect watch, on the Tk thread.**
   - The watch is polled from `_poll_preview`. No new thread.
   - While a watch is pending the planner does not call Ollama, just as it
     waits for a pending proposal or a running skill (v0.7 gate).
   - F8, input off, planner stop, profile load and Clear Rules drop the watch
     without recording an effect.
5. **Runs are bounded.**
   - `planner.max_run_minutes`: default **15**, hard cap **120**. At the
     limit the planner stops, auto turns off and the session ends with
     "run budget reached".
   - `planner.stop_when`: `{"detector": <declared detector>, "visible":
     true|false, "min_confidence": [0, 1]}`, default none. It is met only by a
     fresh observation (at most 1.0 s old) made after the run started. Then
     the planner stops, auto turns off and the session ends with
     "goal reached".
   - Preflight's network check (Ollama `/api/tags`) runs off the Tk thread
     with the profile's timeout, and its result is applied on the Tk thread.
6. **F8 / window close:** input off → `executor.cancel()` → stop the planner
   (drops the proposal, the effect watch and the run, turns auto off, cancels
   the sinks) → end the session log → stop recording. The v0.8 order is
   unchanged.
7. The v0.4–v0.8 invariants still hold: the planner invariant, the skill
   invariant, the memory invariant and "recording only listens". Skills are
   disabled by default. A profile loads only while input control is off.
   `profiles/*` and `memory/` are gitignored (except
   `profiles/example/profile.json`).

## Observed effects (`agent/skill_effects.py`)

- `Expectation(detector, visible=True, within_seconds=2.0,
  min_confidence=0.8)`, validated in `__post_init__`.
- `parse_expectation(block, detector_names) -> Expectation`.
- `EffectWatch(skill_name, expectation, finished_at)`:
  `check(state, now) -> "pending" | "confirmed" | "not_seen"`.
  - `visible: true` is confirmed by an observation with `visible` and
    `confidence >= min_confidence`, observed after `finished_at`.
  - `visible: false` is confirmed by an observation observed after
    `finished_at` that is not visible or below `min_confidence`.
  - After `finished_at + within_seconds` with no match: `not_seen`.
- `GameProfile.expectations: Mapping[str, Expectation]` (skill name →
  expectation). `save_profile` writes `expect` back into the skill block.

## Agent runs (`agent/agent_session.py`)

- `PreflightFacts`: profile name or None, capture running, window title,
  planner settings present, Ollama result (ok / detail), enabled skill names,
  input control on, goal text.
- `run_preflight(facts) -> PreflightReport` with `PreflightCheck(name, ok,
  required, detail)`. Required: profile loaded, capture running, planner
  settings present, Ollama reachable with the model, at least one enabled
  skill. Advisory: input control on (steps are refused until it is), goal
  set.
- `RunBudget(max_seconds, started_at)`: `remaining(now)`, `expired(now)`.
- `GoalCondition(detector, visible=True, min_confidence=0.8)`:
  `met(state, started_at, now)`.
- `AgentRun(budget, goal)`: `stop_reason(state, now) -> str | None` and a
  status line for the panel.
- `agent/ollama_client.py`: `OllamaClient.check_model() -> OllamaResult`
  (`GET /api/tags`, the configured model must be listed).

## Loop integration

- `StepRecord` gains `effect` (`none` / `confirmed` / `not_seen`). The
  prompt's recent-steps lines show it, e.g.
  `type_x (approved) → DONE, effect confirmed (x_glyph visible)`.
- Session log: a new record type `effect` with `skill`, `effect`
  (`confirmed` / `not_seen`), `detector` and `waited_s`. Old logs stay valid.
- Autopilot: a step with an expectation calls `record_result` once, when the
  effect resolves, with `ok and effect != "not_seen"`.
- `should_plan` also waits while an effect watch is pending.

## Profile additions

- Skill blocks: optional `expect` (above).
- `planner` block: `max_run_minutes` (number, default 15, (0, 120]) and
  `stop_when` (above, default none). `load_planner_config` validates the
  shape; `parse_profile` checks that the detector is declared.
- `profiles/example/profile.json` shows both.

## Components

- `agent/skill_effects.py` (new, pure).
- `agent/agent_session.py` (new, pure).
- `agent/profile.py`, `agent/planner_config.py`: the new fields.
- `agent/step_history.py`, `agent/session_log.py`, `agent/autopilot.py`
  callers, `agent/ollama_client.py`: as above.
- `main.py`: the **Agent** panel (Preflight / Start Agent / Stop Agent, a
  check list, a status line with time left, steps and effects, and the goal
  state), the effect watch in `_poll_preview`, and the budget and goal stops.
- `scripts/memory.py`: shows `effect` records.
- `docs/USER_GUIDE.md` (new).

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done | Claude | Issue #82, `feature/v1.0-personal-agent` from `main`, this file, `AGENTS.md` / `docs/HANDOFF.md` / `docs/ROADMAP.md`, draft PR feature→`main` (#84). Merged via PR #83. |
| 1 | `agent/skill_effects.py` + profile `expect` | Done | Claude (Codex out of quota until 2026-09-25 13:55) | `tests/test_skill_effects.py` (17) + `tests/test_observation_boundary.py` (2). `Expectation` / `parse_expectation` / `EffectWatch` / `observation_matches`; `GameProfile.expectations` is a read-only mapping and `save_profile(expectations=)` writes `expect` back (the Save button passes the loaded profile's). An observation made after the deadline counts as `not_seen`. Parse/validate, defaults, unknown field and undeclared detector rejected, confirmed/not_seen for both `visible` values, observations before the finish ignored, save round-trip, import-boundary test. |
| 2 | `agent/agent_session.py` + planner `max_run_minutes` / `stop_when` + `check_model` | Done | Claude | `tests/test_agent_session.py` (24). `PreflightFacts` / `run_preflight` / `PreflightReport` (required: profile, capture, planner settings, Ollama + model, enabled skill; advisory: input control, goal), `RunBudget`, `GoalCondition` (fresh ≤ 1.0 s, after the start), `AgentRun.stop_reason` / `status_line`. `GoalCondition` lives in `agent_session.py` (imported by `planner_config.py`, not the other way round); `parse_profile` and `save_profile` check the `stop_when` detector. `OllamaClient.check_model()` (`GET /api/tags`, untagged model matches `:latest`, new error kind `model_missing`). The observation-boundary test now requires both modules. |
| 3 | Loop integration: step effect, prompt, `effect` log record, autopilot, planner gate | Todo | Claude, safety-reviewer | Tests: effect in `StepRecord` and prompt, `effect` records validate, `not_seen` ×3 turns auto off, no plan while a watch is pending, F8 drops the watch. |
| 4 | UI Agent panel + budget / goal stops | Todo | Claude, safety-reviewer | Tk tests: preflight list, Start Agent refuses on a failed required check and starts nothing, never enables input or auto, budget and goal end the session with their reasons, Stop Agent. |
| 5 | `docs/USER_GUIDE.md`, example profile, `scripts/memory.py` effects | Todo | Claude | Example profile loads in a test. |
| 6 | Live Windows smoke test | Todo | Claude | Notepad + Ollama `qwen3.5:9b`, the acceptance criteria below. |
| R | Release close-out (v1.0.0) | Todo | Claude | CHANGELOG, README, ROADMAP, ARCHITECTURE, AGENTS, `APP_VERSION = "1.0.0"`, `setup.ps1` / `check_system.ps1` banners. Feature→`main` as a **merge commit**, which closes Issue #82. |

Order: 0 → 1 → 2 → 3 → 4 → 5 → 6 → R.
- Each task lands through a `claude/…` or `codex/…` sub-branch PR into
  `feature/v1.0-personal-agent`.
- Codex prompts must say to run **no git commands** (see the operational note
  in `docs/HANDOFF.md`).

## Acceptance criteria (task 6, live on Notepad with Ollama `qwen3.5:9b`)

1. Preflight lists every check. Start Agent refuses with a clear reason when
   a required check fails (capture off, no enabled skill, Ollama unreachable
   or model missing), and starts nothing.
2. Start Agent starts the planner and its session log. It never turns on
   input control or auto mode.
3. A skill with `expect` gets effect `confirmed` when its detector changes as
   expected and `not_seen` when it does not. The effect shows in the panel,
   in the next prompt's recent steps and in the session log.
4. In auto mode, three `not_seen` steps in a row turn auto off.
5. No Ollama call starts while an effect watch is pending.
6. The run budget ends the run ("run budget reached"), and `stop_when` ends
   it when the goal detector is seen ("goal reached").
7. F8 during a run stops everything in the v0.8 order and drops the watch.
8. A profile with a bad `expect`, `stop_when` or `max_run_minutes` is
   rejected with a clear error, and the example profile loads.
9. All v0.3–v0.8 safety invariants still hold, `scripts/memory.py validate
   --all` passes, and `git status` shows nothing under `memory/` or
   `profiles/`.

## Explicitly out of scope for v1.0

- Sending frames or screenshots to an LLM (multimodal planning).
- Learning skills from recordings, or the LLM defining new skills, keys,
  coordinates or durations.
- Retrying a step automatically because its effect was not seen.
- Multi-step goals, goal trees or scheduling runs.
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior. This is a
  standing invariant from `AGENTS.md`.
