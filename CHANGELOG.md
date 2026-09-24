# Changelog

All notable project changes are tracked here.

## v0.7.0 — Closed-loop planner

- The Ollama planner can now propose running one skill from the loaded
  profile: `{"type": "run_skill", "skill": "<name>", "reason": "..."}`.
  - It picks a skill **name** only. Keys, coordinates and durations always
    come from the profile.
  - The skill must exist and be enabled. Extra fields, unknown or disabled
    skills and bad reasons are rejected.
  - `enable_rule` / `disable_rule` / `noop` from v0.4 are kept and still apply
    directly, because they send no input.
- **Closed loop:** each prompt carries the goal, the observations, the enabled
  skills, the rules and the last 5 steps with their decision and outcome.
- The **Planner** panel gained:
  - a **Goal** field;
  - a mode choice, **Approve each step** (the default) or **Auto**, with a
    step counter;
  - a proposal line with **Approve** / **Reject** buttons and a countdown.
- In approve mode nothing runs until you click Approve. A proposal expires
  after 10 s.
- **Auto mode** is an explicit opt-in.
  - It needs input control on and a confirmation, and it is never saved.
    Every start, profile load and F8 returns to approve mode.
  - Auto steps run only in the window that was confirmed, and click skills
    also need it in the foreground.
  - Auto turns itself off after `auto_max_steps` steps (default 20, hard cap
    100), after 3 refused, BLOCKED or failed steps in a row, on F8, when input
    control goes off, on profile load, on Clear Rules and when the planner is
    turned off.
- One step at a time: the planner thread only posts proposals to a
  single-slot mailbox, and only the Tk thread submits the skill, after
  rebuilding it from fresh state. The dispatcher's gates are unchanged.
  - No Ollama request is made while a proposal is pending or a skill is
    running.
  - Each planner start gets a new generation, and proposals from a stopped
    planner are dropped.
- F8 now also drops the pending proposal and turns auto off. The order is
  unchanged: release input, cancel the skill, stop the planner, stop
  recording.
- Profiles gained `planner.goal` (up to 500 characters) and
  `planner.auto_max_steps` (1–100). Load fills the Goal field and Save writes
  both.
- Planner reports and log lines now reach the UI through a queue drained on
  the Tk thread instead of `root.after` from the scheduler thread.
- Live-smoke-tested on Windows with Notepad and Ollama `qwen3.5:9b`:
  - approve, reject and expiry;
  - no Ollama request while a proposal was pending;
  - the auto step cap and the 3-failures stop;
  - F8 during auto;
  - a Save → Load round trip of the goal and step cap.

## v0.6.0 — Game Profiles + Skills

- Added runtime game profiles: `profiles/<name>/profile.json` plus
  `templates/*.png`, with named detectors, skills, rules, key permissions and a
  planner block.
  - The loader validates strictly. It rejects unknown fields, duplicate names,
    missing references and missing templates, and it rejects template paths
    that leave the profile folder.
  - A bad profile changes nothing.
- Added a **Profile** panel: pick a profile, Load it, or Save the current
  detectors and rules as a new profile.
  - Load is refused while keyboard/mouse control is on.
  - Save asks before writing into an existing folder and never deletes files.
  - Saved click skills start disabled.
- Added named **skills**: `click` (a detector's box), `press` (one key) and
  `hold` (a key for N seconds).
  - Every skill starts disabled. A disabled skill never runs, whether it comes
    from a rule or from Run.
  - Keys must be in the profile's `permissions.allowed_keys`. `f8`, the Windows
    keys, `apps` and key combos are always refused.
  - Holds are capped at `max_hold_seconds`, which can never exceed 5 s.
  - Keys are checked twice, by the loader and again by the dispatcher.
- Added a **Skills** panel with an Enabled checkbox, a Run button and the last
  result for each skill.
  - Run needs input control on, and it focuses the game window first.
  - Rules can fire skills.
- Skills run one at a time on a worker thread (`SkillExecutor`); a second skill
  is refused while one is running. `ActionDispatcher` is still the only path to
  input. It now also:
  - sends key skills only while the game window is in the foreground, and
    blocks them otherwise;
  - enforces the profile's `max_actions_per_second`.
- F8 turns input control off and releases held keys, cancels the running skill,
  then stops the planner and recording. A hold ends at once on F8, when input
  control goes off, or when the game window loses focus.
- `profiles/*` is gitignored except `profiles/example/profile.json`, a
  press/hold demo for Notepad.
- Live-smoke-tested on Windows with Notepad:
  - skills and rules;
  - F8 during a hold through the real global hotkey;
  - key rules blocked while the window was not in the foreground;
  - loader rejections;
  - a Save → Load round trip.

## v0.5.0 — Demonstration recording

- Added a "Recording" panel (default off, explicit Record button, always-visible
  "● REC" status). It records your own demonstrations of the captured game window
  into `recordings/<YYYYmmdd_HHMMSS>/`:
  - `frames/000001.jpg` frames, 1-30 fps (default 10);
  - `events.jsonl` (frames, `GameState` snapshots, keys, mouse buttons,
    throttled mouse moves, scroll, focus changes, start/stop markers), all on
    one monotonic clock;
  - `session.json` with counts and the stop reason.
- Input is recorded only while the captured window is in the foreground. Mouse
  coordinates are client-relative, and mouse events outside the client area are
  dropped. F8 and software-injected input (SendInput, on-screen keyboard, remote
  desktop, automation tools) are never recorded.
- The `recording/` package only listens: it never sends input and never imports
  the input path. Recording and autonomous input control are mutually exclusive:
  Record is refused while input control is on, and enabling input control
  stops recording.
- F8 and window close stop recording after releasing input and stopping the
  planner. Sessions also stop at 30 minutes, when free disk drops below 1 GB,
  or when the window closes. Frames are encoded on a background thread, and
  frames are dropped and counted when the writer falls behind; events are never
  dropped.
- Added `python scripts/recordings.py`:
  - `list` shows sessions;
  - `validate` checks timing, counts and frame files;
  - `export` writes an aligned `dataset.jsonl`: one row per frame with its
    state and the actions until the next frame;
  - `review` is an OpenCV viewer with click/key/mouse overlays.
  It never deletes data and refuses to overwrite unless `--overwrite` is given.
- `recordings/` and exported datasets are gitignored.
- Live-smoke-tested on Windows at 150% display scaling: 0 dropped frames at
  10 fps. Stopping on input-control enable and on F8, focus lost/gained events,
  and filtering of injected input were all verified.

## v0.4.0 — Local AI Planner

- Added an optional local LLM planner backed by Ollama (default model
  `qwen3.5:9b`), off by default.
- The planner only proposes directives from a closed vocabulary
  (`enable_rule` / `disable_rule` / `noop`), validated before being applied
  through `RuleEngine`. It has no path to `ActionIntent`, `ActionDispatcher` or
  `InputController`; all v0.3 safety gates are unchanged.
- Planner cycles run on a background thread at a configurable interval and
  fail closed on connection errors, timeouts, HTTP errors and invalid output.
- `RuleEngine` gained `enable_rule` / `disable_rule` / `is_rule_enabled` and
  an `RLock` for safe concurrent use.
- "Planner (Ollama)" UI panel: enable toggle, model, interval, status, and the
  last cycle's time, latency, status and outcome. F8, Clear Rules and window
  close stop the planner; F8 and close release input first.
- Every planner cycle's outcome is logged.
- Added the provider-neutral `model_runtime` package and model catalog
  (`configs/models.v1.json`); models are never auto-downloaded.

## v0.3.0 — Game State + Rules

- Added multiple named detectors per game profile and regions of interest.
- Added HP/resource bar measurement and basic OCR (Tesseract).
- Added persistent `GameState` with stale-data handling.
- Added a deterministic rule engine with cooldowns/debouncing.
- Added the gated `ActionDispatcher` (`ActionIntent` → `InputController`).

## v0.2.0 — Vision baseline

- Added live DXcam capture of a selected Windows application.
- Added gated keyboard/mouse control with PyDirectInput.
- Added target-window focus handling before keyboard input.
- Added global F8 emergency stop.
- Added template selection directly on the live preview.
- Added OpenCV template matching at approximately 10 Hz.
- Added confidence threshold control and match bounding box.
- Added snapshot/template folders for later datasets.

## v0.1.2 — Input/focus fixes

- Fixed keyboard tests being sent to the AI window instead of the selected target.
- Added explicit Focus Game control.
- Improved emergency-stop visibility.

## v0.1.1 — Environment fixes

- Added Python 3.14-compatible setup flow.
- Improved hardware diagnostics.

## v0.1.0 — Initial Windows control prototype

- Window selection and capture.
- Keyboard/mouse test controls.
- Event log and emergency stop.
