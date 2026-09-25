# Changelog

All notable project changes are tracked here.

## v1.0.0 — Personal Game Agent

v1.0 closes the loop vision → state → plan → action → observation. The
planner now sees what its steps did, runs are bounded, and a user guide takes
a new user from install to a first supervised run.

- **Observed effects** (`agent/skill_effects.py`): a skill may declare
  `expect` (`detector`, `visible`, `within_seconds` ≤ 10, `min_confidence`).
  - After a planner step that ran, the app watches for it and records the
    effect as `confirmed` or `not_seen`. Only observations made after the
    step finished and before the deadline count.
  - The effect shows in the run line, in the next prompt's recent steps
    ("effect confirmed (x_glyph visible)") and in a new session-log record,
    `effect`. v0.8 logs stay valid.
  - While a watch is pending the planner makes no LLM call. In auto mode a
    `not_seen` step counts as a failed step, so 3 in a row turn auto off. A
    step is never retried.
  - F8, input off, Clear Rules, planner off/restart and profile load drop the
    watch without an `effect` record. A hold cut short (the new
    `DispatchResult.interrupted`) or a step drained after input was switched
    off opens no watch.
- **Agent runs** (`agent/agent_session.py`) and a new **Agent** panel:
  - **Preflight** lists the checks: profile, capture, planner settings,
    Ollama and the model (`OllamaClient.check_model()`, `GET /api/tags`, on a
    worker thread), an enabled skill; input control and the goal are advice
    only.
  - **Start Agent** runs the preflight and, if everything required passes,
    starts the planner. It never turns on input control or auto mode.
    **Stop Agent** ends the run.
  - Every planner session is a run with a time budget,
    `planner.max_run_minutes` (default 15, at most 120), and an optional goal
    condition, `planner.stop_when` (a detector seen in a fresh frame after the
    run started). They end the run with `run budget reached` / `goal
    reached`, turn auto off and end the session log with that reason.
  - Every planner stop cancels a running planner skill, so a held key is
    released.
- The planner uses the profile's Ollama `host`, `port` and `timeout_seconds`,
  plus the Model field. Load Profile fills the Model field from
  `planner.model`.
- `docs/USER_GUIDE.md`: install, a first run on Notepad, how a run ends,
  detectors + `expect` + `stop_when` for your own game, auto mode, session
  logs and troubleshooting by preflight message. The example profile now has
  a goal, the model, `auto_max_steps` 3 and a 5-minute budget.
- `scripts/memory.py show` prints `effect:` lines and an
  `effects: X confirmed / Y not seen` summary.
- **Agent invariant:** observation never adds input, stops only reduce
  activity, a `not_seen` effect only turns auto off sooner, and a run is
  bounded. The observation modules never import the input path (checked by a
  test).
- Known harmless race: F8 pressed while a preflight's Ollama check is in
  flight still shows that check's result in the panel; the pending start was
  cancelled, so nothing starts.
- Live-smoke-tested on Windows with Notepad and Ollama `qwen3.5:9b`: all 9
  acceptance criteria passed (see `docs/PLAN.md` "Smoke test results").

## v0.8.0 — Session memory

- **Session log** (`agent/session_log.py`): each planner session writes an
  append-only JSONL file, `memory/<profile>/sessions/<stamp>.jsonl`.
  - Records: `session_start` (version, profile, model, goal, step cap,
    `llm_notes`), `cycle`, `step` (skill, reason, decision, outcome), `auto`
    (on/off with the reason), `note`, `truncated` and `session_end` (planner
    disabled, restarted, emergency stop or app closed).
  - The schema is strict. Text is cleaned and cut, a file stops at 5 MB with
    one `truncated` record, and a write failure turns logging off for that
    session without ever stopping the app.
  - The log opens on planner start and ends on every stop path. On F8 it ends
    after input is released, the skill cancelled and the planner stopped.
- **Notes** (`agent/notes.py`): a bounded, thread-safe notebook per profile in
  `memory/<profile>/notes.json`, loaded strictly and saved atomically.
  - At most 20 notes, at most 10 from the planner, 200 characters each,
    duplicates dropped, at most one planner note every 30 s. When the planner
    is at its limit its oldest note is replaced; user notes are never touched.
  - The planner prompt lists the notes as hints that never change which skills
    or keys are allowed.
- **`remember` directive:** `{"type": "remember", "note": "..."}` adds a planner
  note. It is offered only when the profile sets `planner.llm_notes` (new, off
  by default), and it can never edit or delete a note. A note in flight is
  discarded when the planner stops or F8 is pressed.
- **Memory panel:** the notes list with `[user]` / `[llm]` labels, Add / Save
  Edit / Delete, the "Let the planner write notes" checkbox (needs a loaded
  profile; Save Profile keeps it) and the current session log path.
  - Editing a planner note makes it a user note.
  - An invalid `notes.json`, or one changed outside the app, is never
    overwritten: the notes turn read-only until the profile is loaded again.
- `agent/memory_store.py` decides the paths per profile slug, and new session
  files never overwrite an old one.
- `scripts/memory.py list | show <file> | validate <file>|--all` inspects
  notes and session logs. It is read-only.
- **Memory never widens permissions:** the profile loader, skills,
  permissions, rules, autopilot, executor and dispatcher never read memory,
  and the memory modules never import the input path (checked by a test).
- Live-smoke-tested on Windows with Notepad and Ollama `qwen3.5:9b` (see
  `docs/PLAN.md` "Smoke test results").

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
