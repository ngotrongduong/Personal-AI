# v1.1 detailed plan — Meters

This is the checklist for the current milestone (GitHub Issue #92), with
each task's status and owner, so progress can be checked without opening
GitHub. It is the same checklist as Issue #92. **Keep the two in sync:**
when you tick something here, tick or comment it there too, in the same
commit or timeframe as the work.

For the current PR and branch situation, see `docs/HANDOFF.md` instead. That
file changes faster than this one should.

Earlier versions of this file are kept in git history on `main`:
- v0.4 Local AI Planner;
- v0.5 Demonstration recording;
- v0.6 Game Profiles + Skills;
- v0.7 Closed-loop planner;
- v0.8 Session memory;
- v1.0 Personal Game Agent.

`docs/ROADMAP.md`'s Completed section summarizes them.

## Goal

Up to v1.0, the agent knows one thing about the screen: whether a template is
visible. Many game decisions depend on a **number** instead:
- drink a potion when HP is below 30%;
- use a skill when mana is full;
- after a potion, HP should go up.

The pieces already exist from v0.3, but nothing in the app uses them yet:
- `vision/resource_bar.py` (`ResourceBarSpec`, `measure_resource_bars`)
  measures how full a bar is, using HSV color ranges inside a ROI;
- `agent/resource_state_bridge.py` (`apply_resource_measurements`) writes
  the result into `GameState`: `value` is the fill fraction from 0 to 1, and
  the source is `vision:resource_bar`.

v1.1 wires them in:
- A profile declares **meters**.
- The app measures them on every vision tick.
- The planner sees `hp: 42%` in its prompt.
- `expect`, `stop_when` and rules can use threshold conditions
  (`below` / `above`) and change conditions (`rises` / `falls`).
- `scripts/meters.py` helps calibrate a meter from a snapshot.

Design decisions (made by Claude on 2026-09-25 under the user's standing grant
of full autonomy):
- **No OCR in v1.1.** It needs Tesseract and `pytesseract` installed (an
  external download) and is less reliable. It stays a later candidate.
- **One idea of "unknown".** A reading below the meter's `min_confidence` is
  written to `GameState` as invalid (`visible` false, `value` None), the same
  as a ROI outside the frame. Conditions therefore only need to ask "is there
  a valid value", and they carry no confidence of their own.
- **Change conditions** (`rises` / `falls`) compare against the meter's last
  valid reading taken *before the step was submitted*. `main.py` records
  that reading when it submits the step. With no valid baseline, the effect
  is `not_seen` at once (fail closed). This refines the kickoff sketch, which
  compared against the first reading after the step. A potion that works at
  once would already show in that reading, so the rise would never be seen.
- Meter names share one namespace with detectors, because both are keys in
  `GameState`.

## Design constraint (read before implementing anything)

1. **Meters only read.**
   - Measuring a meter, writing it to `GameState` and checking a meter
     condition never build an `ActionIntent` directly. They never call
     `SkillBook.build_intent`, the `SkillExecutor` or the
     `ActionDispatcher`.
   - The only path from a meter to input is the one that already exists: a
     `MeterRule` (or the planner) names a profile skill, and that skill goes
     through permissions and the `ActionDispatcher` like any other.
   - `agent/meter_conditions.py` never imports `skills`, `rule_engine`,
     `autopilot`, `skill_executor`, `action_dispatcher`,
     `core.input_controller` or `pydirectinput`. The observation-boundary
     test enforces this.
2. **Fail closed.** A meter condition is false when the reading is missing,
   invalid (`value` None), not a finite number in [0, 1], not from
   `vision:resource_bar`, or stale. So:
   - a `MeterRule` does not fire;
   - an `expect` ends `not_seen`;
   - a `stop_when` is not met.
3. **Bounded, user-written conditions.**
   - Thresholds (`below` / `above`) are numbers strictly between 0 and 1.
   - Deltas (`rises` / `falls`) are numbers in (0, 1].
   - Exactly one comparison per condition.
   - Unknown fields and undeclared meters are rejected on load.
   - The model can never define or change a meter, a threshold or a rule. It
     can only enable or disable rules the profile already has (v0.4).
4. **Meter rules fire skills only.**
   - A `MeterRule` always uses `SKILL_RULE_ACTION` with a profile skill.
   - It has a cooldown and an observation-age check, the same as
     `VisibilityRule`.
   - `enable_rule` / `disable_rule` / Clear Rules / F8 treat it exactly like
     a visibility rule.
5. **Measuring stays cheap and on the vision tick.**
   - Meters are measured in `_run_vision_if_due`, right after
     `detect_all`, on the same frame and with the same timestamp.
   - No new thread.
   - A measurement error for one meter marks that meter invalid and logs
     once; it never stops the vision loop.
6. All v0.3–v1.0 invariants still hold: the agent, memory, planner and skill
   invariants, and "recording only listens".
   - F8 order is unchanged.
   - Skills are disabled by default.
   - A profile loads only while input control is off.
   - v1.0 profiles (detectors only) load and behave exactly as before.

## Profile: the `meters` block

```json
"meters": [
  {"name": "hp", "roi": [20, 40, 200, 12],
   "colors": [{"lower": [0, 120, 80], "upper": [10, 255, 255]}],
   "direction": "left_to_right", "min_confidence": 0.5}
]
```

- `name`: the same name rules as detectors, and unique across detectors and
  meters.
- `roi`: required, `[x, y, width, height]` in frame pixels (reuse
  `_parse_roi`, but null is not allowed). It should cover the bar's fill area
  only, not its frame.
- `colors`: 1–4 HSV ranges in OpenCV units (H 0–179, S and V 0–255), each
  `{"lower": [h, s, v], "upper": [h, s, v]}` with lower ≤ upper per channel.
  Red wraps around hue 0, so it usually needs two ranges.
- `direction`: `left_to_right` (default), `right_to_left`, `top_to_bottom`
  or `bottom_to_top`.
- `min_confidence`: [0, 1], default 0.5.
- `agent/profile.py`:
  - `MeterDefinition(name, roi, colors, direction, min_confidence)`;
  - `.spec() -> ResourceBarSpec`;
  - `GameProfile.meters: tuple[MeterDefinition, ...]` (default empty);
  - `save_profile(meters=...)` writes the block back, and Save → Load is a
    round trip.
  - A profile without `meters` is valid.

## Conditions (`agent/meter_conditions.py`, new, pure)

- `meter_value(observation, *, now=None, max_age=None) -> float | None`.
  - Returns the fill fraction only for a valid, finite
    `vision:resource_bar` reading in [0, 1].
  - When `now` and `max_age` are given, the reading must also be at most
    `max_age` seconds old.
  - Otherwise it returns None.
- `gate_measurement(measurement, min_confidence) -> ResourceBarMeasurement`.
  It returns an invalid measurement when the confidence is below
  `min_confidence`, and the measurement unchanged otherwise.
- `MeterThreshold(meter, below=None, above=None)`.
  - Exactly one of `below` / `above`, strictly inside (0, 1).
  - `holds(value) -> bool`.
  - `describe()`, e.g. `hp below 30%`.
  - `to_block()`.
- `MeterChange(meter, rises=None, falls=None)`.
  - Exactly one of `rises` / `falls`, in (0, 1].
  - `holds(baseline, value) -> bool`.
  - `describe()`, e.g. `hp rises by 10%`.
  - `to_block()`.
- `parse_meter_condition(block, meter_names, *, allow_change: bool, extra_fields=())`.
  - Strict: unknown fields are rejected, the meter must be declared, and
    there must be exactly one comparison.
  - It is shared by `expect`, `stop_when` and meter rules. `extra_fields`
    lets each caller keep its own options, such as `within_seconds`.

### `expect` (`agent/skill_effects.py`)

- A skill's `expect` is either the v1.0 detector form or a meter form:
  - `{"meter": "hp", "below"|"above": x, "within_seconds": s}`;
  - `{"meter": "hp", "rises"|"falls": d, "within_seconds": s}`.
- It is parsed into `MeterExpectation(condition, within_seconds=2.0)`, with
  the same `within_seconds` bounds as v1.0.
- `EffectWatch` accepts either kind of expectation and gains an optional
  `baseline: float | None`.
  - **Threshold form:** confirmed by a valid reading observed after
    `finished_at` and no later than the deadline, for which `holds` is
    true.
  - **Change form:** confirmed when such a reading moved from the baseline
    by at least the delta, in the stated direction. With no baseline it
    resolves `not_seen` at once.
  - Otherwise the effect is `not_seen` at the deadline.
  - `EffectResult.detector` carries the meter name.
- `GameProfile.expectations` holds both kinds, and `save_profile` writes both
  back.

### `stop_when` (`agent/agent_session.py`, `agent/planner_config.py`)

- `stop_when` is either the v1.0 detector form or
  `{"meter": "hp", "below"|"above": x}`. Change conditions are not allowed
  here.
- It is parsed into `MeterGoal(condition)`. `met(state, started_at, now)`
  needs a valid reading observed after the run started, no more than
  `GOAL_FRESH_SECONDS` old, for which `holds` is true.
- `check_goal_detector` becomes a check against both detector and meter
  names. `describe()` / `to_block()` behave as in v1.0.

## Meter rules (`agent/rule_engine.py`, `agent/profile.py`)

- Profile rule with a `meter` instead of a `detector`:
  `{"name": "auto_potion", "meter": "hp", "below": 0.3, "skill": "drink_potion",
  "cooldown_seconds": 5, "max_observation_age_seconds": 0.75, "enabled": true}`.
  - Exactly one of `detector` / `meter`.
  - `min_confidence` is only allowed with `detector`.
- `MeterRule(name, meter, below=None, above=None, skill,
  max_observation_age_seconds=0.75, cooldown_seconds=1.0)`.
  - It is frozen and validated.
  - `action` is always `SKILL_RULE_ACTION`.
  - `RuleEngine` holds both rule kinds in one list, and the names are
    unique across both kinds.
  - `evaluate` fires a meter rule only for a fresh, valid reading that
    satisfies the threshold, respecting the cooldown and the disabled set.
  - The `ActionIntent` has `detector_name` = the meter,
    `confidence` = the reading's confidence, `target_bbox` None,
    `skill_name` = the skill, and a reason such as
    `hp 25% below 30%`.
- `rule_engine` may import `meter_conditions`, never the reverse.
- `_rule_block` writes both kinds, and the prompt's rules section describes a
  meter rule by its condition.

## App wiring (`main.py`, `agent/llm_planner.py`, `agent/agent_session.py`)

- **Profile load / unload** keeps the loaded profile's `MeterDefinition`s
  next to the detectors. Loading another profile or clearing it drops the
  specs and the meters' `GameState` entries.
- **`_run_vision_if_due`**, after `detect_all` on the same frame:
  - run `measure_resource_bars` for each meter, then `gate_measurement`,
    then `apply_resource_measurements` with the detection timestamp;
  - an exception for one meter writes that meter invalid and logs once;
  - a meter that turns valid or invalid is logged as `METER hp: 42%` /
    `METER hp: unknown` (transitions only, not every tick);
  - the Detectors line adds `hp=42%` or `hp=?`;
  - rules are evaluated after the meters are written.
- **Prompt** (`_format_observations`): a meter reading is shown as
  `- hp: 42% (meter)`, or `- hp: unknown (meter)` when it is invalid,
  instead of the raw observation fields. Detector lines are unchanged.
- **Step submission:** for a skill whose `expect` is a change condition,
  record the meter's `meter_value` at submit time and pass it as the watch's
  baseline. The recent-steps line shows the effect as in v1.0, e.g.
  `effect confirmed (hp rises by 10%)`.
- **Preflight:** when the profile has meters, an advisory check `Meters`
  lists each one (`hp 42%, mp unknown`). It is `[NOTE]` if any meter is
  unknown and never blocks Start Agent.
- **Save Profile** passes the loaded profile's meters, as it already does
  for expectations.

## Calibration tool (`scripts/meters.py`, read-only)

- `suggest <snapshot.png> --roi x,y,w,h [--direction ...]`:
  - reads the ROI, finds the dominant saturated hue of the filled part and
    prints a suggested `colors` block;
  - handles red's wrap around hue 0 by suggesting two ranges;
  - prints a ready-to-paste `meters` entry.
- `test <profile_dir> <snapshot.png>`:
  - loads the profile without registering anything;
  - measures every meter on the image;
  - prints `name  percent  confidence  valid`;
  - exits with 1 when the profile is invalid or a meter is unknown.
- Snapshots come from the existing Save Snapshot button, and the tool never
  writes a profile.

## Components

- `agent/meter_conditions.py` (new, pure).
- `agent/profile.py`: `meters`, the meter rule form and the meter
  `expect` / `stop_when` forms.
- `agent/skill_effects.py`, `agent/agent_session.py`,
  `agent/planner_config.py`: meter expectations and goals.
- `agent/rule_engine.py`: `MeterRule`.
- `agent/llm_planner.py`: meter lines in the prompt, meter rules in the rules
  section.
- `main.py`: measuring, logging, the Detectors line, the step baseline, the
  preflight note and save.
- `scripts/meters.py` (new), `scripts/meter_demo.py` (new, task 6).
- `docs/USER_GUIDE.md`: a "Meters" section.
- `tests/`: new tests for every module above, and the observation-boundary
  test extended to `agent/meter_conditions.py`.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | In progress | Claude | Issue #92, `feature/v1.1-meters` from `main`, this file, `AGENTS.md` meter invariant, `docs/HANDOFF.md` / `docs/ROADMAP.md`, draft PR feature→`main`. |
| 1 | Profile `meters` block | Not started | Codex (else Claude) | `MeterDefinition`, `.spec()`, parse (ROI required, 1–4 colors, direction, `min_confidence`), shared namespace with detectors, `save_profile(meters=)` round trip, tests. |
| 2 | `agent/meter_conditions.py` + meter `expect` / `stop_when` | Not started | Codex (else Claude) | `meter_value`, `gate_measurement`, `MeterThreshold`, `MeterChange`, `parse_meter_condition`; `MeterExpectation` + `EffectWatch` baseline; `MeterGoal`; v1.0 forms unchanged; observation-boundary test. |
| 3 | `MeterRule` | Not started | Codex (else Claude) | `RuleEngine` evaluates both kinds (fresh, valid, cooldown, disabled set); profile rule form with `meter`; `_rule_block`; the prompt's rules section. |
| 4 | `main.py` wiring | Not started | Claude, safety-reviewer | Measuring on each vision tick, gating, transition logs, Detectors line, prompt meter lines, the change baseline at submit, the preflight `Meters` note, Save passes the meters, load/unload. |
| 5 | `scripts/meters.py`, USER_GUIDE, example | Not started | Codex (else Claude) | `suggest` / `test`, read-only, exit codes, tests on synthetic images; USER_GUIDE "Meters" section with an auto-potion example. |
| 6 | Live Windows smoke test | Not started | Claude | `scripts/meter_demo.py` (below); the 9 acceptance criteria. |
| R | Release close-out (v1.1.0) | Not started | Claude | CHANGELOG, README, ROADMAP, ARCHITECTURE, AGENTS, `APP_VERSION = "1.1.0"`, `setup.ps1` / `check_system.ps1` banners. Feature→`main` as a **merge commit**, which closes Issue #92. |

Order: 0 → 1 → 2 → 3 → 4 → 5 → 6 → R.
- Each task lands through a `claude/…` or `codex/…` sub-branch PR into
  `feature/v1.1-meters`.
- Codex prompts must say to run **no git commands** (see the operational note
  in `docs/HANDOFF.md`).

## Smoke test target (task 6)

Notepad has no colored bar to measure. Task 6 adds `scripts/meter_demo.py`:
- It is a small Tk window that belongs to this project, not a game, and holds
  no user data.
- It draws a red bar that drains by itself (about 2% per second).
- The key `x` refills it by 20%, and the window shows its true percentage as
  text for checking.

It is the only window besides Notepad that the smoke test may send input
to. A real game is never touched.

## Acceptance criteria (task 6, live on `scripts/meter_demo.py`)

1. The meter shows the right percentage (±5 points against the demo's own
   number) on the Detectors line, in the prompt and in
   `scripts/meters.py test`.
2. A wrong ROI or wrong colors give `unknown`, and then no meter rule fires
   and no meter goal is met.
3. A `MeterRule` `hp below 0.3` fires the `refill` skill (key `x`) at most
   once per cooldown, and only while input control is on.
4. `expect` `{"meter": "hp", "rises": 0.1}` gives `confirmed` after a refill
   and `not_seen` when the key is refused.
5. `stop_when` `{"meter": "hp", "above": 0.9}` ends the run with
   "goal reached".
6. F8 stops the meter rule at once (input off, no further dispatch).
7. A profile with a bad meter (bad ROI, bad color range, duplicate name,
   unknown meter in `expect` / `stop_when` / a rule) is rejected with a clear
   error.
8. A v1.0 profile with detectors only behaves as before, and the example
   profile loads.
9. All v0.3–v1.0 safety invariants still hold, tests and ruff pass, and
   `git status` shows nothing under `memory/`, `profiles/` or `snapshots/`.

## Explicitly out of scope for v1.1

- OCR of numbers or text (needs Tesseract; a later candidate).
- Multiple templates per detector, object detection, or sending frames to an
  LLM.
- Meters or conditions defined by the model, and arithmetic between meters.
- Retrying a step because its effect was not seen.
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior. This is a
  standing invariant from `AGENTS.md`.
