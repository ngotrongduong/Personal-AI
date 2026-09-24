# v0.6 detailed plan — Game Profiles + Skills

Granular checklist for the current milestone (GitHub Issue #50), with status
and owner, so progress can be checked without opening GitHub. This is the same
checklist as Issue #50 — **keep them in sync**: when you tick something here,
tick/comment it there too (and vice versa), same commit/timeframe as the work.

For "what's the current PR/branch situation right now," see `docs/HANDOFF.md`
instead — that one changes faster than this file should.

Earlier versions of this file (v0.4 Local AI Planner, v0.5 Demonstration
recording) are preserved in git history on `main`; see `docs/ROADMAP.md`'s
Completed section for the summaries.

## Goal

First step toward v1.0 (vision → state → plan → action → observation). Road to
v1.0, one spec and one release per step:

- **v0.6 Profiles + Skills (this milestone, no LLM)**;
- v0.7 closed-loop planner that picks skills (approve-each-step by default,
  explicit opt-in auto mode with rate limit and action budget);
- v0.8 session memory (structured JSONL log plus bounded, user-editable notes
  written by the LLM);
- v1.0 integration, permissions panel, long smoke test.

v0.6 delivers:
- runtime-loadable **game profiles** (`profiles/<name>/profile.json` plus
  template PNGs), saved from the UI and then hand-edited as JSON;
- **skills** (`click` / `press` / `hold`) with per-profile **permissions**;
- rules that fire skills;
- a Skills panel where the user runs a skill manually.

User decisions (2026-09-24):
- From v0.7 the LLM only picks a **skill name** defined by the profile.
  Coordinates, keys and durations always come from the profile, and all input
  goes through `ActionDispatcher`.
- Skills cover clicks and keys. No specific game yet; smoke tests use Notepad.
- Profiles are created with "Save Profile" in the UI and then edited by hand.

## Design constraint (read before implementing anything)

1. `ActionDispatcher` stays the **only** bridge to `InputController`. The
   existing gates keep their order: input enabled → supported action → fresh
   intent → target resolvable.
2. Keys are validated **twice**: the profile loader rejects bad profiles, and
   the dispatcher checks again at dispatch time.
   - A key must be in the profile's `permissions.allowed_keys`.
   - Forbidden in code, whatever the profile says: `f8`, `win` / `winleft` /
     `winright`, `apps`, and any combo (no combos in v0.6).
   - `hold.seconds` ≤ `permissions.max_hold_seconds` ≤ the hard cap of
     **5.0 s**.
3. Key skills (`press` / `hold`) dispatch **only while the captured window is
   the foreground window**, otherwise they are BLOCKED, so keys never go to
   another app. The manual Run button focuses the game first, as `test_w`
   does.
4. Global rate limit in the dispatcher (`permissions.max_actions_per_second`,
   default 5). Only one skill runs at a time. While one is running, a new one
   is rejected ("busy"), not queued.
5. Every skill has an `enabled` flag that defaults to **off**. A disabled
   skill never runs, whether fired by a rule or by Run. This is the
   permission layer the v0.7 planner will use.
6. F8 / window close: `input.set_enabled(False)` (releases held keys) → cancel
   the running skill → stop the planner → stop recording.
7. A profile can be loaded only while input control is **off**, so rules
   cannot fire the moment it loads.
8. `profiles/*` is gitignored except `profiles/example/profile.json`. The repo
   is public: never commit template PNGs.

The v0.5 recording invariant is unchanged: `recording/` only listens, and
recording and input control stay mutually exclusive.

## Profile format

`profiles/<name>/profile.json`, templates in `profiles/<name>/templates/*.png`:

```json
{
  "format_version": 1,
  "name": "Notepad demo",
  "permissions": {"allowed_keys": ["x", "space"], "max_hold_seconds": 1.5,
                  "max_actions_per_second": 5},
  "detectors": [{"name": "ok_button", "template": "templates/ok_button.png",
                 "threshold": 0.9, "roi": null}],
  "skills": [
    {"name": "press_ok", "type": "click", "detector": "ok_button", "min_confidence": 0.9, "enabled": false},
    {"name": "type_x", "type": "press", "key": "x", "enabled": false},
    {"name": "hold_space", "type": "hold", "key": "space", "seconds": 1.0, "enabled": false}
  ],
  "rules": [{"name": "auto_ok", "detector": "ok_button", "skill": "press_ok",
             "min_confidence": 0.9, "cooldown_seconds": 1.0, "enabled": true}],
  "planner": {"enabled": false, "model": "qwen3.5:9b", "interval_seconds": 5.0}
}
```

Strict validation:
- Reject unknown keys, duplicate names, and any `format_version` other than 1.
- Every detector and skill reference must resolve.
- Template paths must stay **inside** the profile folder: no `..` and no
  absolute paths.
- A missing template file gives a clear error.
- The `planner` block reuses `load_planner_config` (`agent/planner_config.py`).

## Components

- `agent/skills.py` (new, pure):
  - `ClickSkill` / `PressSkill` / `HoldSkill`;
  - `SkillPermissions`, `FORBIDDEN_KEYS`, `HARD_MAX_HOLD_SECONDS`;
  - skill → `ActionIntent`. A click needs a visible, fresh detection with
    enough confidence.
- `agent/rule_engine.py`:
  - `ActionIntent` gains optional `skill_name`, `key` and `hold_seconds`,
    with defaults so existing callers keep working;
  - `VisibilityRule` gains an optional `skill`.
- `agent/profile.py` (new):
  - `GameProfile`, `load_profile`, `save_profile` (never overwrites an
    existing profile without confirmation), `list_profiles`;
  - detectors are registered with `DetectorRegistry.register_file`.
- `agent/action_dispatcher.py`:
  - `press` / `hold` added to the supported actions;
  - allowlist and hold-cap re-check, foreground check (injectable), rate
    limit;
  - `hold` is `key_down`, then wait on a cancel event, then `key_up` in
    `finally`.
- `agent/skill_executor.py` (new):
  - one worker thread; `submit` is rejected while busy; `cancel()` for F8;
  - results go to a `SimpleQueue` that `_poll_preview` drains, the same
    pattern as the v0.5 Recording panel.
- `main.py`:
  - Profile panel: profile list, Load, Save Profile, status.
  - Skills panel: one row per skill with an Enabled checkbox, a Run button
    and the last result.
  - F8 / close wiring.
- `profiles/example/profile.json`: press/hold skills only (no detectors, so
  no PNG), for the Notepad smoke test.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done | Claude | Issue #50, `feature/v0.6-profiles-skills` from `main`, this file, `AGENTS.md`/`docs/HANDOFF.md`/`docs/ROADMAP.md` updated, `profiles/*` gitignored except `profiles/example/`, draft PR feature→`main`. |
| 1 | `agent/skills.py` + `ActionIntent`/`VisibilityRule` extensions | Done (#53) | Claude | `validate_key`: one lowercase key name matching `[a-z0-9]{1,16}` or a single punctuation key. That rules out combos (`+`, spaces) and uppercase, and `FORBIDDEN_KEYS` (`f8`, `win`/`winleft`/`winright`/`lwin`/`rwin`, `apps`) are always rejected. `SkillPermissions(allowed_keys: frozenset, max_hold_seconds ≤ 5.0, max_actions_per_second ≤ 20)`, with `key_denial`/`hold_denial` for the dispatcher to re-check. Frozen `ClickSkill`/`PressSkill`/`HoldSkill` validate their own fields; `enabled` defaults to False and must be a real bool. `SkillBook(skills, permissions)` rejects duplicate or unpermitted skills, holds runtime enable flags behind a lock, and `build_intent(name, state, source=, now=)` returns a `SkillIntentResult` (intent, or the reason: unknown / disabled / not permitted / click detection missing, hidden, weak, stale or without a bbox). `ActionIntent` gains `skill_name`/`key`/`hold_seconds` (defaults None). `VisibilityRule` gains `skill`, which must be set exactly when `action == SKILL_RULE_ACTION` (`"skill"`), and `RuleEngine` copies it into the intent. `tests/test_skills.py`. 291 passed, 1 skipped. |
| 2 | `agent/profile.py` load/save/list + `profiles/example/profile.json` | Done (#54) | Claude (Codex out of quota until 2026-09-25 13:55) | `load_profile(dir, registry=None)` / `parse_profile` reject unknown fields at every level, `format_version` other than int 1, duplicate names, dangling detector/skill references, template paths that are absolute, contain `..`, resolve outside the folder, are not `.png` or do not exist, and any skill the permissions forbid (built through `SkillBook`). Rules become `VisibilityRule(action="skill")` plus an `enabled` flag; `planner` goes through `load_planner_config`. Detectors are registered only after full validation, and a failed registration unregisters what this call added. `save_profile(root, name, detectors=[(DetectorDefinition, bgr)], skills, rules, permissions, planner, overwrite=False)` validates everything first, writes to a slug folder (`[a-z0-9_-]`, max 64), refuses an existing folder unless `overwrite=True`, and never deletes files. `list_profiles(root)`. `GameProfile.skill_book()` / `rule_engine()`. `tests/test_profile.py`. 313 passed, 1 skipped. |
| 3 | Dispatcher press/hold + foreground + allowlist + rate limit | Done (#55) | Claude (Codex out of quota), safety-reviewer | `ActionDispatcher(..., permissions_provider=, foreground_checker=is_foreground, known_keys=pydirectinput.KEYBOARD_MAPPING)`. `SUPPORTED_ACTIONS = {click, press, hold}`; `"skill"` is never directly dispatchable. Key gates, after the unchanged enabled → supported → fresh checks: window selected → permissions loaded (none, or a provider error, blocks) → `key_denial` → key known to pydirectinput → `hold_denial` (hold only; a press carrying `hold_seconds` is rejected) → target window is foreground (checker error blocks). Click keeps its bbox/region gates and does not need the foreground. Sliding-window rate limit from `max_actions_per_second` (fractional rates use a longer window), shared by clicks only while permissions are loaded; the slot is reserved under a lock and released if the input call fails, and rejected attempts use no slot. While a press or hold runs (`busy`), every dispatch is rejected as busy. `press` = `tap_key(key, PRESS_SECONDS)`. `hold` = `key_down` → wait on the cancel event in 50 ms slices, also ending when input is disabled (F8) or the window loses the foreground → `key_up` in `finally` (a `key_up` error is reported, not raised; a failed `key_down` still sends `key_up` unless InputController refused it because input is off). A cancel event that is already set rejects the action before any key goes down. `cancel()` from any thread only ends a running action; `dispatch(cancel_event=)` is for the executor. The lock is never held while waiting. `core.window_utils.is_foreground` (a null handle is never foreground). Safety-reviewer: no blockers; fixed foreground re-check during a hold, null hwnd, pre-set cancel, press counted as busy, deque growth without permissions. Modifier keys (`shift`/`ctrl`/`alt`) stay allowed when listed in `allowed_keys` (games use them); the foreground re-check stops a held modifier from leaking into another app. **Carry into tasks 4/6:** a hold blocks its caller for up to 5 s, so skills must run on the executor's worker thread, never the Tk thread, and the F8 listener must call `input.set_enabled(False)` and `executor.cancel()` directly (both thread-safe) before scheduling the UI part with `root.after`. `tests/test_action_dispatcher_keys.py` (fake input/foreground/keys, threads with join timeouts, a real `InputController` disable race, `is_foreground`). 355 passed, 1 skipped. |
| 4 | `agent/skill_executor.py` | Done (#56) | Claude (Codex out of quota), safety-reviewer | `SkillExecutor(dispatcher)` runs each intent through `ActionDispatcher.dispatch(intent, hwnd=, cancel_event=)` on a fresh daemon `skill-executor` thread, one at a time. `submit(intent, hwnd=, source="manual")` never blocks and returns None or why it did not start: busy (rejected, never queued) or shut down. `cancel()` is safe from any thread: it sets the run's own cancel event, so a cancel before the worker reaches the dispatcher sends no key, and calls `dispatcher.cancel()`. Each run gets a fresh event. Results are `SkillRun(source, result, finished_at)` on a `queue.SimpleQueue`, drained by the Tk loop with `drain()`, and are queued before `busy` clears. A dispatch exception becomes a rejected result. `shutdown(join_timeout=1.0)` refuses new skills, cancels and joins; it does not touch input, so callers disable input first (F8 order). `tests/test_skill_executor.py` (a blocking fake dispatcher, plus the real dispatcher over fake input: F8 order releases a held key quickly). Safety-review fixes: a cancel before the worker reaches the dispatcher now also stops a **click** (`_run` checks the event first and `_dispatch_click` checks `cancel_event` after admission, freeing the rate slot); a failed `thread.start()` no longer leaves the executor stuck busy; new tests for cancel-before-dispatch (press/hold/click, input still on), concurrent submits, shutdown idle/timeout/from the worker; hold tests use 5 s so CI timing has slack. |
| 5 | UI Profile panel (Load/Save) | Done (PR pending) | Claude | Profile panel in `main.py`: combobox of `profiles/*` folders (`list_profiles`), Refresh, Load Profile, Save Profile…, status line. **Load** is refused while input control is on; it builds a fresh `DetectorRegistry` from `read_templates` (new in `agent/profile.py`), the profile's `RuleEngine` and `SkillBook` first, so a bad profile changes nothing, then stops the planner and swaps registry, rules, skills and profile, clearing `GameState`. The dispatcher's `permissions_provider` reads the loaded profile's permissions, so with no profile no key can be sent. **Save** asks for a name (`profile_slug` folder), asks before writing into an existing folder (files replaced, never deleted), and writes every detector whose template the app kept (`_selection_release` now keeps it) plus rules via `collect_profile_contents`: a UI click rule becomes a skill rule firing a **disabled** click skill (`click_<detector>`), the loaded profile's skills and permissions are kept, rules without a saved detector or skill are skipped and logged. Vision can be enabled with profile detectors only. Skill rules still dispatch as "skill" and are rejected until task 6. `tests/test_main_profile_panel.py`. |
| 6 | UI Skills panel + rule→skill + F8 wiring | Todo | Claude | Tk tests; F8 cancels the executor after releasing input. |
| 7 | Live Windows smoke test | Todo | Claude | See acceptance criteria. |
| R | Release close-out (v0.6.0) | Todo | Claude | CHANGELOG, README, ROADMAP, ARCHITECTURE, `APP_VERSION = "0.6.0"`, banners. Feature→`main` as a **merge commit**; closes Issue #50. |

Order: 0 → 1, 2 → 3 → 4 → 5 → 6 → 7 → R.
- Each task lands through a `codex/…` or `claude/…` sub-branch PR into
  `feature/v0.6-profiles-skills`.
- Codex prompts must say to run **no git commands** (see `docs/HANDOFF.md`
  operational note). Claude branches, commits and pushes afterwards.

## Acceptance criteria (task 7, live on Notepad)

1. Load `profiles/example`.
2. Run on a disabled skill is blocked.
3. With the skill and input control enabled, Run `type_x` types "x" into
   Notepad.
4. `hold_space` holds space for about 1 s, then releases it.
5. F8 during a hold releases the key at once and turns input control off.
6. A key rule is BLOCKED while Notepad is not the foreground window.
7. A key outside the allowlist, or `f8`, in the JSON is rejected by the loader.
8. Save Profile from a detector drawn on the preview, then Load it back: the
   click rule works.
9. Loading a profile while input control is on is refused.
10. All v0.3–v0.5 safety invariants still hold (F8, input-enable gate,
    dispatcher gates, planner closed vocabulary, recording only listens).

## Explicitly out of scope for v0.6

- LLM skill choice and approval/auto modes (v0.7).
- Logs and memory (v0.8).
- Key combos, skill sequences, mouse move/drag.
- A UI skill editor.
- Auto-focusing the window when a rule fires: a key rule is simply BLOCKED if
  the game is not the foreground window.
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior — standing
  invariant from `AGENTS.md`.
