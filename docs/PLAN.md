# v1.2 detailed plan — Taps + demo labels

**Status: in progress** on `feature/v1.2-tap-demos` (Issue #107).

v1.2 is the first step of the v1.2 → v2.0 track, which ends in an agent that
learns from the user's own play (see `docs/ROADMAP.md`). Testing on a real
game (Pixel Dungeon ML, 2026-09-28) showed two gaps:

- most moves in a tile/touch game are taps on the map, which no skill type can
  express. A click skill needs a detected template;
- after a click skill, the cursor stays on the button. That changes how the
  button looks, so its template match falls to 0 until the mouse moves.

v1.2 adds a `tap` skill type and restores the cursor after every click. It also
labels recorded demonstrations with the profile's skills, which gives the
imitation work in v1.3 its data.

## Design constraint (tap invariant, permanent from v1.2)

1. **The profile is the only source of a tap point.** A tap skill carries a
   fixed point, `at: [fx, fy]`, as fractions (0..1) of the captured window's
   client area. The LLM still chooses only a skill *name*; it never supplies
   or changes a point.
2. **Tap is fail-closed:**
   - it runs only while the captured window is the foreground window, like key
     skills, because nothing on screen confirms what is under a fixed point;
   - every `requires` condition must hold on a fresh observation at intent
     build time, or no intent is built;
   - the point must fall inside the live client area when dispatched.
3. **Same gates as every skill.** A tap goes Skill → `SkillExecutor` →
   `ActionDispatcher` → `InputController`:
   - input on / F8;
   - intent freshness;
   - rate limit;
   - cancel;
   - disabled by default.
4. **Cursor restore never adds input.** After a click, `InputController`
   moves the cursor back to where it was. That is a cursor move only, with no
   button or key event. If reading the position fails, nothing is restored.
5. **Labeling only reads.** `label` reads a recording and a profile. It never
   sends input, never edits the profile, and writes a file only when `--out`
   is given, never overwriting unless `--overwrite` is given. Labels are data
   for later milestones; nothing in v1.2 acts on them.

## Profile format

```json
{"name": "step_right", "type": "tap", "at": [0.58, 0.47],
 "requires": [{"detector": "btn_wait", "visible": true},
              {"meter": "hp", "above": 0.3}],
 "max_observation_age_seconds": 0.75,
 "enabled": false,
 "expect": {"detector": "btn_wait", "visible": true}}
```

- `at`:
  - exactly 2 finite numbers in `[0, 1]` (bools rejected);
  - the dispatcher maps it to `left + min(int(fx * width), width - 1)`, and
    the same for `y`.
- `requires`:
  - optional, at most 4 conditions;
  - a condition is either a detector condition
    `{"detector", "visible", "min_confidence"}` or a meter threshold
    `{"meter", "below"|"above", "min_confidence"}`;
  - `rises` / `falls` are rejected here, because nothing gives them a
    baseline;
  - names must be declared detectors / meters.
- `max_observation_age_seconds`:
  - how fresh each `requires` observation must be;
  - default 0.75 s, the same as click skills.
- Save/Load round-trips a tap skill unchanged.

## Components

- `agent/skills.py`:
  - `TapSkill`;
  - `SkillBook.build_intent` for taps (checks `requires`, builds
    `ActionIntent(action="tap", tap_point=(fx, fy))`).
- `agent/skill_requirements.py`: pure, fail-closed `requires` parsing and
  evaluation. It never imports the input path.
- `agent/rule_engine.py`: `ActionIntent.tap_point`.
- `agent/profile.py`: parse and save `tap` skills.
- `agent/action_dispatcher.py`: a `tap` branch with the foreground gate,
  client-area resolution and bounds check, rate limit, cancel and click.
- `core/input_controller.py`: `click` restores the cursor.
- `recording/labels.py` + `scripts/recordings.py label`:
  - a mouse-button `down` inside a fresh visible detector bbox → that
    detector's click skill;
  - a click within `--radius` (default 0.03 of the client diagonal) of a tap
    point → the nearest tap skill;
  - a key `down` → the press or hold skill with that key (hold when it was
    held at least half the hold time);
  - anything else → `unlabeled` with its normalized point;
  - the output is per-skill counts, plus an optional JSONL file.
- `main.py`:
  - `describe_skill` for taps (`tap (58%, 47%)`);
  - Skills panel Run;
  - planner prompt skill lines.
- `docs/USER_GUIDE.md`: a "Tap skills" section and a "Label your demos"
  section.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | In progress | Claude | Issue #107, `feature/v1.2-tap-demos`, this plan, tap invariant, HANDOFF/ROADMAP, draft release PR. |
| 1 | `tap` skill: skills/profile/intent/dispatcher + `requires` | Done (PR #110) | Codex + Claude | Tests for parsing, round-trip, fail-closed `requires`, foreground, bounds and hit-test gates. |
| 2 | Cursor restore in `InputController.click` | Done (PR #110) | Codex + Claude | SetCursorPos + read-back before the click; restore outside the input lock, even when the click raises. |
| 3 | Demo labeling (`recording/labels.py`, `recordings.py label`) | To do | Codex (else Claude) | Synthetic sessions in tests. |
| 4 | `main.py` wiring + safety review | Done | Claude | describe_skill, capture allow-list (only key skills run without capture), auto foreground check, Save Profile drops/logs taps whose `requires` detector is gone. Safety review: no Critical/Important. |
| 5 | Docs + example | Done | Claude | USER_GUIDE "Tap skills" (fields, refusals, cursor restore); example profile gets a disabled `tap_centre` (no templates committed). Label docs came with task 3. |
| 6 | Windows smoke test | To do | Claude | Pixel Dungeon ML: harmless taps only (no fights, permadeath); cursor restore fixes the LOST button. |
| R | Release v1.2.0 | To do | Claude | CHANGELOG/README/ROADMAP/ARCHITECTURE/AGENTS/version, merge commit. |

## Acceptance criteria

1. A tap skill round-trips through Save/Load. A malformed `at` or `requires`
   (out of range, a bool, the wrong length, an unknown name, `rises`) is
   rejected with one clear error.
2. A tap intent is built only when every `requires` condition holds on a
   fresh observation. A missing, stale, low-confidence or invalid
   observation builds no intent.
3. The dispatcher sends a tap only with input on and the captured window in
   the foreground, at the profile's point inside the live client area, under
   the rate limit. F8 or cancel blocks it.
4. The LLM can run a tap skill only by name. The prompt shows the skill, and
   no directive carries coordinates.
5. After any click (click skill, tap or UI rule), the cursor returns to its
   previous position. On the real game, a clicked button is detected again
   on the next vision tick without the user moving the mouse.
6. `recordings.py label` maps a synthetic session's clicks and keys to the
   right skills and counts unlabeled input. It writes nothing without
   `--out`.
7. Existing v1.1 profiles and all earlier invariants are unchanged.
8. Tests and ruff are clean; CI is green.

## Out of scope

- tap points chosen by the model, or relative to a detection (maybe later);
- drags, right clicks, scrolls;
- training any model on labels (v1.3);
- acting on labels.
