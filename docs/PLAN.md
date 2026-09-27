# v1.1 detailed plan — Meters

**Status: released** as v1.1.0 — `feature/v1.1-meters` merged into `main`
via PR #94 (merge commit), closing Issue #92.

v1.1 turns the existing v0.3 resource-bar primitive into a first-class profile
observation. A game profile can declare meters such as HP, mana, stamina or
progress. The app measures them every vision tick and exposes normalized values
through `GameState`, the planner prompt, expectations, stop conditions and
rules.

## Goal

Make numeric/color bars usable by the agent without widening permissions.

Example outcome:

```text
hp: 42%
mana: 78%
```

The planner may observe those values. The profile — never the model — defines
what the meter means, where it is, which colors count, what thresholds matter
and which skill a rule may trigger.

## Design constraint

### 1. Meters only read

- Meter modules never import `skills`, `skill_executor`,
  `action_dispatcher`, `core.input_controller` or `pydirectinput`.
- Measurement only produces observations.
- A meter condition cannot directly send input.

### 2. Fail closed

A meter condition is false unless the latest observation:

- exists;
- is valid/visible;
- carries a numeric value in `[0.0, 1.0]`;
- meets its configured confidence;
- is fresh enough for that use.

Invalid, stale or low-confidence data therefore:

- fires no rule;
- confirms no effect;
- meets no stop condition.

### 3. Profile is the authority

The LLM never defines:

- meter ROIs;
- HSV ranges;
- meter names;
- thresholds;
- rule targets.

All of those come from the loaded profile and are validated before use.

### 4. Shared namespace

Meter names and detector names share one observation namespace. Duplicate names
are rejected on profile load so `GameState["hp"]` can never ambiguously refer
to both a detector and a meter.

### 5. Conditions are bounded

A meter condition references one declared meter and exactly one comparator:

- `below`: current value < threshold;
- `above`: current value > threshold;
- `rises`: value increased by at least delta from a valid baseline;
- `falls`: value decreased by at least delta from a valid baseline.

Thresholds/deltas are normalized numbers in `[0, 1]`.

For effects, the baseline is the fresh meter value at the end of the executed
step and the comparison must resolve within the expectation window. For rules,
change conditions compare consecutive accepted fresh samples. No stale sample
may become a baseline.

### 6. Existing input safety is unchanged

A meter rule can only choose an already-declared enabled skill. Execution still
goes through `SkillExecutor -> ActionDispatcher -> InputController`.
F8/input-off/foreground/rate-limit/permission gates remain authoritative.

## Profile format

Proposed `meters` block:

```json
{
  "meters": [
    {
      "name": "hp",
      "roi": [20, 30, 220, 16],
      "hsv_ranges": [
        {"lower": [50, 180, 120], "upper": [80, 255, 255]}
      ],
      "direction": "left_to_right",
      "min_slice_coverage": 0.5,
      "max_gap_slices": 1,
      "min_confidence": 0.8
    }
  ]
}
```

All coordinates are client-frame coordinates, matching the existing vision
pipeline. `save_profile` must round-trip the block without inventing values.

## Conditions

Threshold form:

```json
{"meter": "hp", "below": 0.25, "min_confidence": 0.8}
```

Change form:

```json
{"meter": "hp", "rises": 0.20, "min_confidence": 0.8}
```

Exactly one of `below`, `above`, `rises`, `falls` is allowed.

Meter forms are added to:

- skill `expect`;
- planner `stop_when`;
- profile rules.

Existing detector forms remain valid and unchanged.

## Components

- `agent/profile.py`: parse/save `meters`, shared-name validation.
- `agent/meter_conditions.py`: pure meter condition parsing/evaluation.
- `agent/rule_engine.py`: `MeterRule` with cooldown/freshness/fail-closed behavior.
- `main.py`: measure configured meters each vision tick, bridge to GameState,
  show values, include them in planner state.
- `agent/skill_effects.py` / `agent/agent_session.py`: meter condition forms.
- `scripts/meters.py`: read-only `suggest` / `test` helpers.
- `docs/USER_GUIDE.md`: profile meter setup and calibration.
- `scripts/meter_demo.py`: harmless Windows smoke-test target.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done | ChatGPT (PR #93) | Issue #92, active integration branch, PLAN/invariant/HANDOFF/ROADMAP and draft release PR #94 are in place. |
| 1 | Profile `meters` block | Done | ChatGPT (PR #95) | `MeterDefinition`, strict HSV/ROI/direction/confidence validation, save/load round-trip, detector/meter shared namespace rejection, dedicated tests; green Windows CI. |
| 2 | Meter conditions | Done | ChatGPT (PR #96) | Pure fail-closed meter conditions; threshold/change operators; meter forms of `expect` and `stop_when`; effect/goal baselines; profile round-trip; observation-boundary tests; green Windows CI. |
| 3 | `MeterRule` | Done | ChatGPT (PR #97) | Skill-only rule; cooldown/freshness; consecutive accepted-sample change logic; float-boundary fix; baseline reset across disable/re-enable; green Windows CI. |
| 4 | Live wiring | Done (PR #102) | Claude + Codex plugin | Every vision tick measures meters (same timestamp as detectors, clipped ROI/error → invalid, per-meter isolation, logs on valid/invalid transitions). Status line `hp=42%(0.97)`/`hp=?`, prompt `- hp: 42% (meter, …)`/`unknown`, advisory preflight `Meters` note, fresh step-end baseline for `rises`/`falls` expects. Safety review fixes: a meter reading is never a visibility-rule/click-skill target and its name is refused for UI click rules/detectors; the prompt and status show only confident fresh readings; vision skips frozen frames after a capture error. |
| 5 | Meter tools/docs | Done | ChatGPT (PR #99) | Read-only `suggest`/`test`, USER_GUIDE and example; strict generated-option validation; clipped-ROI rejection; synthetic tests; green Windows CI. |
| 6 | Windows smoke test | Done (PR #103) | ChatGPT helper (PR #100) + Claude | Live run on Windows 11 with `scripts/meter_demo.py` and Ollama `qwen3.5:9b`: 46/46 checks, all 9 acceptance criteria passed (see "Smoke test results"). The demo is now DPI aware and turns its own IME off. |
| R | Release v1.1.0 | Done (this PR + PR #94) | Claude | CHANGELOG/README/ROADMAP/ARCHITECTURE/version, green CI, release PR merge commit. |

## Acceptance criteria

1. A valid profile meter round-trips through load/save without changing its
   meaning; malformed meters and detector/meter name collisions are rejected.
2. Live capture updates meter observations in `GameState` with normalized
   values and confidence.
3. Invalid, stale or below-confidence measurements satisfy no meter condition.
4. The planner prompt can show a meter as a percentage but cannot alter its
   definition.
5. `expect` supports meter threshold/change conditions and only observations
   made after a completed step can confirm the effect.
6. `stop_when` supports meter conditions and only a fresh valid reading can
   end a run.
7. A meter rule can trigger only its declared skill, respects cooldown/freshness,
   and all existing dispatcher/input/F8 gates remain unchanged.
8. Consecutive-sample `rises` / `falls` logic never uses an invalid or stale
   sample as its baseline.
9. The Windows smoke test on `scripts/meter_demo.py` demonstrates live
   measurement, a threshold condition, a change condition, planner visibility
   and F8 behavior without introducing a new input path.

## Smoke test results

Run on 2026-09-28, Windows 11 at 150% display scaling, RTX 4070 Ti, Ollama
`qwen3.5:9b`, against the `scripts/meter_demo.py` window only (no game, no
user app received input). An in-process script drove the real app (capture,
vision tick, rule engine, planner, executor, dispatcher, F8 hook) and
approved each planner proposal. Result: **46/46 checks passed**.

| # | Criterion | Result |
|---|-----------|--------|
| 1 | Round-trip and rejection | PASS — meters, meter rules, `expect` and `stop_when` round-trip unchanged; a bad HSV range, a rule or `expect` naming an unknown meter, and a detector/meter name collision are each rejected with one clear load error (the only 4 dialogs of the run). |
| 2 | Live measurement | PASS — `hp` read 50% then followed the demo to 80% (confidence 1.00, source `vision:resource_bar`); status line `hp=80%(1.00)`. |
| 3 | Fail closed | PASS — a ROI outside the frame reads `hp=?`; neither an invalid nor a stale reading fired the rule with input on; an invalid reading with the bar at 100% did not end a run with goal `hp above 95%`. |
| 4 | Planner visibility | PASS — the prompt showed `- hp: 50% (meter, confidence 1.000)`, then the new level after each step, and `- hp: unknown (meter)` for the invalid meter; preflight showed `Meters: hp 50%`. |
| 5 | `expect` | PASS — `heal` with `rises 0.1` was confirmed twice (50→70→90%); `damage` with `falls 0.1` at 0% was recorded `not seen`. |
| 6 | `stop_when` | PASS — the third heal reached 100% and the run ended with `goal reached`. |
| 7 | Meter rule | PASS — `hp below 30%` was BLOCKED with input off; with input on it started `heal` through the executor, 0→20→40%, the second firing 1.1 s later (cooldown 1 s), then stopped above the threshold. |
| 8 | Baseline | PASS — a 2 s-old reading gave no baseline; a fresh one did. |
| 9 | F8 | PASS — F8 turned input off, stopped the planner (`session_end` `emergency stop`); afterwards the meter rule fired nothing at 0% and no Ollama request was sent. |

Findings fixed or recorded during the smoke test:

- **An empty or wrong-color bar is a valid 0% reading**, not unknown. Only a
  ROI clipped by the frame (or a measurement error) gives `hp=?`. The user
  guide already tells users to check `scripts/meters.py test`.
- **`scripts/meter_demo.py` was DPI unaware** — at 150% scaling Windows
  bitmap-scaled the bar, so the example ROI missed it. The demo now calls
  `SetProcessDpiAwareness(1)`.
- **A Vietnamese IME swallowed the demo's keys** (H/D/digits sent to the Tk
  window). The demo now disables its own IME with `ImmDisableIME(0)`.
- **Windows' foreground lock** can refuse `SetForegroundWindow` for the smoke
  script; the harness retries after a zero-distance mouse move (no key sent).
  The app itself is unaffected: a user clicks the game window.
- **Ollama:** the first load of `qwen3.5:9b` with little free RAM took over
  4 minutes (longer than the planner timeout), and once the server stopped
  answering after a cancelled request until restarted. Prewarm the model
  (the preflight `Ollama` check only lists models) before a supervised run.

## Out of scope

- OCR-based numeric meters;
- object detection;
- model-generated meter definitions;
- automatic profile editing by the LLM;
- replay/imitation-learning changes;
- anti-cheat, memory reading/injection or packet manipulation.
