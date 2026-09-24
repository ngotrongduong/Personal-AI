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
  `record_fps`, `started_at` / `ended_at` (wall clock, ISO-8601),
  frame/dropped/event counts, stop reason. Written at open (with
  `ended_at: null`) and rewritten atomically at close, so a crashed session is
  still recognisable. The monotonic `t0` itself is not stored: it is only
  meaningful within one boot, and every `t` is already relative to it.
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
| 1 | `recording/schema.py` | Done (#44) | Claude (Codex out of quota) | `FORMAT_VERSION = 1`; frozen slotted dataclasses per event type + `ObservationRecord` + `SessionInfo`; constructors validate too (`t` ≥ 0 and finite, enums, bool ≠ int); strict `event_from_dict`/`session_from_dict` raise `SchemaError` on unknown type, missing/extra fields, wrong types. `observation_to_record(obs, t0)` stores `observed_t` relative to `t0`; non-finite values become `null`. 16 tests. |
| 2 | `recording/session_writer.py` | Done (#44) | Claude (Codex out of quota) | `SessionWriter(root_dir, *, …, clock, max_queue_frames=64, jpeg_quality=85, encoder)`: unique session folder (`name`, `name_2`, …), daemon writer thread; `write_frame` drops + counts when pending frames hit the cap; frame indices are assigned on successful write, so files stay contiguous even after encode errors; `write_event` never drops; `close(reason, join_timeout=0.0)` never blocks, thread drains then rewrites `session.json` atomically; `stats()`. 9 tests. |
| 3 | `recording/input_recorder.py` | Done (#44) | Claude (Codex out of quota) | pynput keyboard + mouse listeners via injectable `listener_factory`; `win32_event_filter` hides `LLKHF_INJECTED`/`LLMHF_INJECTED` events from the handlers (never suppresses them for the system); foreground, client-area (half-open) and F8 filters; client-relative coordinates; ~30 Hz move throttle; `focus` gained/lost via `poll_focus()` or on input. One lock serialises both listener threads so the sink sees events in `t` order; sink/foreground errors are logged, never raised into pynput. 13 tests + an AST test that `recording/` never imports input senders or any `Controller`. |
| 4 | `recording/recorder_controller.py` | Done (#45) | Claude (Codex out of quota) | `RecordingController(app_version=…, input_control_enabled=…)`; `start(hwnd, capture, game_state, root_dir, fps=10, on_status=…)` returns at once (False if a session is still open; fps outside 1–30 raises `ValueError`; input control on raises `RecordingRefusedError`) and does everything else on a daemon `recording-sampler` thread: builds `SessionWriter` (folder + first `session.json`, off the Tk thread), writes a `start` marker, starts `InputRecorder` (foreground = `GetForegroundWindow() == hwnd`, `client_region(hwnd)`, writer's `t0`), then samples `latest_frame()` (already a copy) + `snapshot()` on one `t`, polls focus, and auto-stops when input control becomes enabled (or the check fails) / max duration (30 min) / free disk < 1 GB (checked every 5 s) / window closed. `stop(reason)` is non-blocking, idempotent, first reason wins; a stop during a slow open never installs the hooks. The thread stops the listeners, writes a `stop` marker, reports `"stopping"`, closes the writer and reports `"stopped"` once `session.json` is final; `is_running` stays True until then. Open failures report `"failed"` (with `session_dir` if a folder was created and closed). Limits must be finite. `on_status` runs on the sampler thread. `wait_stopped(timeout)` for tests/shutdown. 19 tests; safety-reviewer PASS WITH NOTES, all notes fixed except the task 7 live check that the captured hwnd is top-level. |
| 5 | `main.py` "Recording" panel | Done (#46) | Claude (Codex out of quota) | Record/Stop button, FPS spinbox (1–30, default 10), status "● REC 00:42 · 420 frames · 0 dropped · 1.2k events" → "Recording saved: recordings/<stamp> · … · stop: <reason>". Record is enabled only while capturing and input control is off; `RecordingController(input_control_enabled=lambda: self.input.enabled)` is the second gate. Enabling input control stops recording *before* input is enabled; F8 / close stop it after input release and planner stop (reasons `f8` / `app_close`); Stop Capture stops it (`capture_stopped`). `on_status` only does a non-blocking `SimpleQueue.put` (never `root.after`, which can block the recording thread on a busy/closing Tk thread); `_poll_preview` drains it on the Tk thread, drops stale generations (bumped per Record), never lets a late `recording` report undo `stopping`, and resets the panel if the recorder finished without a final report. Close waits at most 3 s (`wait_stopped`) for `session.json`. 20 Tk/format tests (fake recorder); live check on the app's own window: 25 frames at 10 fps, 0 dropped, F8 → `stop_reason: f8`. safety-reviewer PASS WITH NOTES, all 3 Minor notes fixed. |
| 6 | Review + export: `recording/dataset.py`, `recording/review.py`, `scripts/recordings.py` | Done (PR pending) | Claude (Codex out of quota) | `python scripts/recordings.py [--root DIR] list | validate <session>|--all | export <session> [--out F] [--overwrite] | review <session> [--start N]` (session = folder name under `recordings/` or a path; exit 0 ok / 1 validation failed / 2 usage or data error). `list`: status complete / incomplete (`ended_at` null, e.g. app crash) / invalid, wall-clock length, fps, counts, stop reason, window title. `validate`: strict schema per line; `t` non-decreasing within the sampler source (frame/state/marker) and the input source (key/mouse/scroll/focus) separately; frame indices contiguous from 1 and named exactly `frames/NNNNNN.jpg` (any other path is an error and is never opened), files exist; counts match `session.json` when finished; warnings for unreferenced frame files, marker problems, an unfinished session, and a truncated last line of an unfinished session; at most 10 messages per kind. `export`: refuses invalid sessions; one `dataset.jsonl` row per frame `{index, t, next_t, file, image, focused, state_t, state, actions}` — state = latest `state` with `t` ≤ frame `t`, actions = input events in `[t, next_t)` (last frame open-ended; input before the first frame is counted, not exported), `image` relative to the output file; default `<session>/dataset.jsonl`, written via `.tmp` + rename, existing output only replaced with `--overwrite`, never writes a file named `events.jsonl`/`session.json`/`*.jpg` or a folder. `review`: OpenCV window, state bboxes + clicks/mouse path/scrolls/keys drawn per frame, ←/→ (a/d) step, `[`/`]` ±10, Home/End, space play at the recorded fps, q/Esc or window X to quit; frames loaded with `np.fromfile` + `imdecode` (non-ASCII paths). Never deletes data. Safety-reviewed PASS WITH NOTES, all fixed: the export temp file is created exclusively (an existing `<out>.tmp`, e.g. a hardlink to `events.jsonl`, is refused, never truncated or removed); hostile JSON (huge integers, deep nesting) is reported, not raised; frame files that symlink out of the session are an error; review clamps playback delay and drawn coordinates; `dataset*.jsonl` / `*.jsonl.tmp` gitignored. Tests in `tests/test_recording_dataset.py` + `tests/test_recording_review.py` (fake cv2 window); full suite 267 passed, 1 skipped (symlink test needs Windows Developer Mode). Live: CLI on the task 5 smoke sessions (crashed session listed as incomplete, validates with a warning); real cv2 review window on a synthetic session. Known quirk: `poll_focus()` stamps its event a few µs after the frame's `t`, so the first frame's `focused` is usually `null`. |
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
