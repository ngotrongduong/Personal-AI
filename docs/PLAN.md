# v0.5 detailed plan — Demonstration recording

Granular checklist for the current milestone (GitHub Issue #41), with status
and owner, so progress can be checked without opening GitHub. This is the same
checklist as Issue #41 — **keep them in sync**: when you tick something here,
tick/comment it there too (and vice versa), same commit/timeframe as the work.

For "what's the current PR/branch situation right now," see `docs/HANDOFF.md`
instead — that one changes faster than this file should.

The v0.4 version of this file (task-by-task history for the Local AI Planner)
is preserved in git history on `main` (see the v0.4.0 release, PR #16); see
`docs/ROADMAP.md`'s Completed section for the summary.

## Goal

Record the user's own demonstrations — game-window frames, the user's
keyboard/mouse input, and v0.3 `GameState` observations, all on one monotonic
clock — into local per-session folders. Then review, validate, and export them
as an aligned dataset (frame ↔ state ↔ action) for later learning experiments.

User decisions (2026-09-24): record frame + input + game state; one folder per
session with JPG frames + `events.jsonl` + `session.json`; record input only
while the game window is foreground; scope is record + review + export only.

## Design constraint (read before implementing anything)

Recording observes; it never acts. Concretely:

- The `recording/` package only **listens**. It never sends input and never
  imports `core.input_controller`, `agent.action_dispatcher`, `ActionIntent`,
  or `pydirectinput`. Nothing in v0.5 replays recorded input into a game.
- Default off. Recording starts only from an explicit Record button, and the UI
  always shows a visible "● REC" indicator with elapsed time while it runs
  (no hidden/background recording).
- **Recording and autonomous input control are mutually exclusive in v0.5.**
  Record is refused while input control is enabled, and enabling input control
  stops an active recording. Events flagged as injected (`LLKHF_INJECTED` /
  `LLMHF_INJECTED`, via pynput's `win32_event_filter`) are dropped, so the
  dataset contains only the human's own actions.
- Privacy: input is recorded only while the captured window (`capture.hwnd`)
  is the foreground window. Mouse events outside its client area are dropped;
  recorded coordinates are client-area-relative (same frame of reference as
  frame pixels and `Observation.bbox`). F8 itself is never recorded.
- F8 and window close: release input → stop planner → stop recording, in that
  order. Stopping never blocks the Tk thread; the writer flushes on its own
  background thread.
- Fail-safe limits: a maximum session duration (default 30 min) and an
  automatic stop when free disk space drops below 1 GB. The stop reason is
  written to `session.json`.
- JPEG encoding and disk writes never run on the Tk thread or the fast loop.
  If the writer falls behind, frames are dropped and counted; input/state
  events are never dropped.
- Data stays local under `recordings/` (gitignored). Nothing is uploaded.

## Session format

`recordings/<YYYYmmdd_HHMMSS>/`:

- `session.json` — `format_version`, `app_version`, window title, client size,
  `record_fps`, `started_at` (wall clock) + monotonic `t0`, `ended_at`,
  frame/dropped/event counts, stop reason.
- `frames/000001.jpg` — BGR frame encoded with `cv2.imencode` (quality 85).
- `events.jsonl` — one JSON object per line, `t` = seconds since `t0`
  (monotonic), plus `type`:
  - `frame` `{index, file}`
  - `state` `{observations}` (serialized `Observation`s, sampled with the frame)
  - `key` `{key, action: down|up}`
  - `mouse_button` `{button, action: down|up, x, y}`
  - `mouse_move` `{x, y}` (throttled to ~30 Hz)
  - `scroll` `{dx, dy, x, y}`
  - `focus` `{action: gained|lost}`
  - `marker` `{action: start|stop, reason}`

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done | Claude | Issue #41, `feature/v0.5-demo-recording` from `main`, this file, `AGENTS.md`/`docs/HANDOFF.md` updated, `recordings/` gitignored, draft PR feature→`main`. |
| 1 | `recording/schema.py` | Not started | Codex | Dataclasses for each event type + session metadata; strict to/from JSON (unknown `type`/extra fields rejected); `Observation` serialization. Pure-logic tests. |
| 2 | `recording/session_writer.py` | Not started | Codex | `SessionWriter(root_dir, clock)`: creates the session folder; background writer thread + bounded queue; JPEG encode off-thread; full queue drops frames only and counts `dropped`; `close()` flushes and writes `session.json`. Tests with a temp dir. |
| 3 | `recording/input_recorder.py` | Not started | Codex | Wraps pynput keyboard + mouse listeners with injectable `is_target_foreground()`, `client_region()`, `clock`. Foreground, client-area, F8 and injected-event filters; client-relative coordinates; mouse-move throttle; `focus` gained/lost events. Tests drive the callbacks directly with fakes. |
| 4 | `recording/recorder_controller.py` | Not started | Codex | `RecordingController.start(hwnd, capture, game_state, root_dir, fps)` / non-blocking `stop(reason)`: sampler thread (default 10 fps, 1–30) pairs `capture.latest_frame()` with `game_state.snapshot()` on one timestamp; wires `InputRecorder` + `SessionWriter`; duration/disk limits; observation-only `on_status` (frames, dropped, events, elapsed). Tests with fake capture/listener. |
| 5 | `main.py` "Recording" panel | Not started | Codex (Claude review) | Record/Stop button, fps spinbox, status "● REC 00:42 · 420 frames · 0 dropped · 1.2k events". Enabled only while capturing and input control is off. F8/close/enabling input stop recording (after input release). Status marshaled via `root.after(0)` + generation counter like the planner panel. Tk tests like `tests/test_main_planner_visibility.py`. |
| 6 | Review + export: `recording/dataset.py` + `scripts/recordings.py` | Not started | Codex | CLI `list` (sessions + summary), `validate` (referenced frames exist, `t` non-decreasing, counts match `session.json`), `export` (one `dataset.jsonl` row per frame: frame file, state at that time, actions in `[t, next_frame_t)`), `review` (cv2 window, overlay clicks/keys, ←/→ to step). Never deletes data. |
| 7 | Live Windows smoke test | Not started | Claude | ~1 min on Notepad or an offline game: typing in another app not recorded; alt-tab emits `focus lost`; input control on blocks/stops Record; F8 stops immediately and the session closes cleanly; dropped ≈ 0 at 10 fps; `validate`/`export`/`review` work. `safety-reviewer` pass for tasks 3/4/5. |
| R | Release close-out (v0.5.0) | Not started | Claude | `CHANGELOG.md`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md` (recording layer), `README.md`, `APP_VERSION = "0.5.0"`, script banners; feature→`main` as a **merge commit**; close Issue #41. |

Order: 0 → 1, 2, 3 (parallel) → 4 → 5 → 6 → 7 → R. Each task lands through a
`codex/…` or `claude/…` sub-branch PR into `feature/v0.5-demo-recording`.
Codex prompts must say to run **no git commands** (see `docs/HANDOFF.md`
operational note); Claude branches/commits/pushes afterwards.

## Acceptance criteria

- A recorded session contains frames, state, and the user's own input on one
  monotonic clock, and `validate` passes on it.
- No input is recorded while the game window is not foreground, and no
  injected/app-generated input is recorded.
- `recording/` contains no import path to `InputController`,
  `ActionDispatcher`, `ActionIntent`, or `pydirectinput`.
- All v0.3/v0.4 safety invariants (F8, input-enable gate, dispatcher gates,
  planner closed vocabulary) are unchanged. F8 also stops recording.
- The Tk thread never blocks on JPEG encoding or disk I/O.
- `export` produces an aligned dataset usable without the app running.

## Explicitly out of scope for v0.5

- Imitation learning / model training of any kind.
- Replaying recorded input into a game (would need to go through the
  dispatcher and is a later milestone).
- Recording while the agent controls input.
- Audio, MP4/video containers.
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior — standing
  invariant from `AGENTS.md`, unaffected by this milestone.
