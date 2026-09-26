# v1.1 detailed plan — Meters

**Status: active** on `feature/v1.1-meters` (Issue #92).

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
| 4 | Live wiring | Not started | Claude/machine lane | Measure each vision tick, status line, planner prompt, preflight note; safety review. |
| 5 | Meter tools/docs | Implementation complete — pending CI/merge | ChatGPT (PR #99) | Read-only `suggest`/`test`, USER_GUIDE and example; strict generated-option validation; clipped-ROI rejection; synthetic tests. |
| 6 | Windows smoke test | Not started | Claude/machine lane | `scripts/meter_demo.py`; run all acceptance criteria. |
| R | Release v1.1.0 | Not started | Shared | CHANGELOG/README/ROADMAP/ARCHITECTURE/version, green CI, release PR merge commit. |

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

## Out of scope

- OCR-based numeric meters;
- object detection;
- model-generated meter definitions;
- automatic profile editing by the LLM;
- replay/imitation-learning changes;
- anti-cheat, memory reading/injection or packet manipulation.