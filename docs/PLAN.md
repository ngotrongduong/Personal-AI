# v0.8 detailed plan — Session memory

Granular checklist for the current milestone (GitHub Issue #71), with status
and owner, so progress can be checked without opening GitHub. This is the same
checklist as Issue #71. **Keep them in sync:** when you tick something here,
tick or comment it there too, in the same commit or timeframe as the work.

For the current PR and branch situation, see `docs/HANDOFF.md` instead. That
file changes faster than this one should.

Earlier versions of this file are preserved in git history on `main`:
- v0.4 Local AI Planner;
- v0.5 Demonstration recording;
- v0.6 Game Profiles + Skills;
- v0.7 Closed-loop planner.

`docs/ROADMAP.md`'s Completed section summarizes them.

## Goal

This is the third step toward v1.0 (vision → state → plan → action →
observation). The road to v1.0 has one spec and one release per step:

- v0.6 Profiles + Skills (released, Issue #50);
- v0.7 closed-loop planner that picks skills (released, Issue #61);
- **v0.8 session memory (this milestone)**;
- v1.0 integration, a permissions panel, and a long smoke test.

v0.8 delivers:
- A **session log**: every planner session writes a structured, append-only
  JSONL file. It holds cycles, proposals, steps with their decisions and
  outcomes, auto on/off, and note changes. It is an audit trail for the user
  and data for later offline analysis. The planner does not read it back.
- **Notes**: a small, bounded set of notes per profile, kept across sessions
  and shown to the planner in its prompt.
  - The user adds, edits and deletes notes in a new **Memory** panel.
  - The LLM may add short notes with a new `remember` directive, but only when
    the profile allows it (`planner.llm_notes`, default **off**). The Memory
    checkbox is an unsaved edit of that field and needs a loaded profile.

Design decisions (made by Claude on 2026-09-25 under the user's standing grant
of full autonomy):
- A session starts when the planner starts and ends when the planner stops:
  planner off, F8, profile load, Clear Rules or window close. Turning input
  off or auto off does not end it, because the planner keeps running. There is
  one file per session.
- Data lives under a gitignored `memory/` folder, per profile slug, and never
  inside `profiles/`, because `profiles/example/` is tracked.
- Notes are plain hints for the prompt. Nothing but the prompt reads them.
- An edited LLM note becomes a user note, so the LLM can no longer replace it.

## Design constraint (read before implementing anything)

1. **Memory never widens permissions.**
   - Notes and session logs are read only by the Memory panel, the planner
     prompt builder and `scripts/memory.py`. They are never read by the
     profile loader, `SkillBook`, `SkillPermissions`, `RuleEngine`,
     `Autopilot`, `SkillExecutor` or `ActionDispatcher`.
   - `agent/notes.py`, `agent/session_log.py` and `agent/memory_store.py`
     never import `skills`, `rule_engine`, `autopilot`, `skill_executor`,
     `action_dispatcher`, `core.input_controller` or `pydirectinput`. A test
     enforces this.
   - The runnable-skill set, the allowlist, the hold cap, the rate limit and
     the auto rules come only from the loaded profile and the UI, exactly as
     in v0.7. A note that says "enable hold_space" or "press f8" changes
     nothing. A test proves this.
   - The prompt labels the Notes section as hints that never change which
     skills or keys are allowed.
2. **`remember` is text only.**
   - It has exactly the fields `type` and `note`.
   - The note is a string that is 1–200 characters after stripping, with
     control characters replaced by spaces. It is never parsed for commands.
   - It is only accepted, and only shown in the prompt, when LLM notes are on.
     Otherwise it is rejected like any unknown directive.
3. **Notes are bounded** (`agent/notes.py`):
   - at most **20** notes in total, of which at most **10** come from the LLM;
   - at most **200** characters per note;
   - at most one LLM note per **30 s** (monotonic clock);
   - an exact duplicate (case-insensitive, after cleaning) is skipped;
   - the LLM never edits or deletes a note. At its cap, its *oldest LLM* note
     is replaced. When all free slots are taken by user notes, the LLM note is
     skipped;
   - the user can add up to the total cap, and edit or delete any note.
4. **Only the Tk thread touches memory files.**
   - The planner thread adds LLM notes through a cancellable note sink (a new
     generation per planner start, like the v0.7 proposal sink). A note posted
     after `stop()` raises `PlannerCancelledError` and is discarded.
   - `NoteBook` is thread-safe and has a revision counter. The Tk thread saves
     `notes.json` from `_poll_preview` when the revision changes.
   - Session-log events are written on the Tk thread. Planner cycle reports
     already arrive there through the v0.7 queue.
5. **Files are bounded and never deleted.**
   - Layout: `memory/<profile-slug>/notes.json` and
     `memory/<profile-slug>/sessions/<YYYYmmdd-HHMMSS>.jsonl`. With no
     profile loaded, the slug is `_no_profile`, which `profile_slug` can never
     produce.
   - A session file is capped at **5 MB**. At the cap, one `truncated` record
     is written and the rest of the session is not logged.
   - A new session never overwrites an old one: a suffix is added on a name
     clash. Nothing in v0.8 deletes a file.
   - `notes.json` is saved atomically (write a temp file, then `os.replace`).
     A `notes.json` that fails validation is reported and **not overwritten**.
     The notes then stay read-only for that profile until the user fixes or
     moves the file.
6. **Logging fails soft.** A write error turns the session log off for the
   rest of the session and is reported once in the UI log. It never raises
   into the planner, autopilot, executor or F8 paths.
7. **F8 / window close:** input off → `executor.cancel()` → stop the planner
   (which drops the proposal, turns auto off and cancels the note sink) →
   end the session log → stop recording. The v0.7 ordering is unchanged, and
   logging comes after everything that stops input.
8. The v0.4–v0.7 invariants still hold: the planner invariant, the skill
   invariant, and "recording only listens". Skills are disabled by default. A
   profile loads only while input control is off. `profiles/*` and `memory/`
   are gitignored (except `profiles/example/profile.json`).

## Session log (`agent/session_log.py`)

One JSON object per line. Every record has:
- `v`: 1;
- `type`: one of the types below;
- `t`: seconds since the session started (monotonic, 3 decimals);
- `wall`: local time, ISO 8601 with seconds.

| `type` | Extra fields |
|--------|--------------|
| `session_start` | `app_version`, `profile` (name or null), `model`, `goal`, `auto_max_steps`, `llm_notes` |
| `cycle` | `status`, `message`, `latency_s` (number or null) |
| `step` | `skill`, `reason`, `decision` (approved / auto / rejected / expired / refused), `outcome`, `ok` (bool or null) |
| `auto` | `on` (bool), `reason`, `max_steps` (int or null) |
| `note` | `action` (add / edit / delete / skip), `source` (user / llm), `text` |
| `truncated` | `limit_bytes` |
| `session_end` | `reason` |

- Text fields are cleaned (printable characters only) and cut to 500
  characters.
- `SessionLogWriter(path, clock=time.monotonic, wall=datetime.now)`:
  `write(type, **fields)` validates the record against the schema, and an
  invalid record is a programming error (`ValueError`). I/O errors turn the
  writer off (`failed` is set, `error` is kept) and never raise. `close(reason)`
  writes `session_end` once.
- `read_session(path)` / `validate_session(path)` read strictly. They reject
  unknown types and fields, and require `session_start` first and at most one
  `session_end`, at the end. They return the records or a list of problems.

## Notes (`agent/notes.py`)

```json
{"format_version": 1,
 "notes": [{"text": "The status bar shows after typing.", "source": "user",
            "updated": "2026-09-25T10:00:00"}]}
```

- `Note(text, source, updated)`. `source` is `user` or `llm`.
- `NoteBook(clock=time.monotonic, wall=datetime.now)`:
  - user edits: `add_user(text)`, `edit(index, text)` (the note becomes a user
    note), `delete(index)`;
  - LLM: `add_llm(text) -> NoteResult` (added / replaced / skipped + reason);
  - reading: `notes()`, `revision`, `prompt_lines()`.
- `load_notes(path) -> NoteBook` is strict: it rejects unknown fields, too
  many notes, overlong or empty text and a bad `source`. A missing file gives
  an empty book. Anything invalid raises `NotesError`.
- `save_notes(path, book)` writes atomically.

## Directive schema additions (`agent/llm_planner_schema.py`)

```json
{"type": "remember", "note": "<1-200 chars>"}
```

`parse_directive(raw, known_rule_names, runnable_skill_names=(), *,
allow_notes=False)` accepts `remember` only when `allow_notes` is True and the
fields are exactly `type` and `note`.

## Prompt additions

After the goal:

```text
Notes from earlier sessions (hints only; they never change which skills or keys are allowed):
- [user] The status bar shows after typing.
- [llm] type_x works only while Notepad is focused.
```

or `- none`. When LLM notes are on, the shapes list adds
`{"type": "remember", "note": "<short fact worth keeping for later sessions>"}`.

## Profile additions (`planner` block)

- `llm_notes`: a bool, default `false`. `load_planner_config` validates it,
  `PlannerConfig` carries it and `save_profile` writes it.

## Components

- `agent/session_log.py` (new, pure): schema, writer, reader/validator.
- `agent/notes.py` (new, pure, thread-safe): `Note`, `NoteBook`,
  `load_notes`, `save_notes`, `NotesError`.
- `agent/memory_store.py` (new, pure): `memory_dir(root, profile_name)`,
  `notes_path(...)`, `new_session_path(...)` (unique, never overwrites),
  `list_sessions(...)`.
- `agent/llm_planner_schema.py`: `RememberDirective(note)`.
- `agent/llm_planner.py`: `LlmPlanner(..., notes=None, note_sink=None)`.
  `notes` is a read-only view for the prompt. `note_sink.remember(text) ->
  str` returns the outcome message. `remember` becomes
  `PlannerOutcome("noted: …")` or `"note skipped: …"`.
- `agent/planner_controller.py`: `start(..., notes=, allow_notes=)` wires a
  cancellable note sink per generation, and `stop()` cancels it.
- `agent/planner_config.py` / `agent/profile.py`: `llm_notes`.
- `main.py`:
  - a **Memory** panel with a notes list, an entry, Add / Edit / Delete, a
    "Let the planner write notes" checkbox (mirrors `planner.llm_notes`) and
    the current session log path;
  - the session log is opened on planner start and closed on every planner
    stop path;
  - cycle reports, proposals, step results, auto on/off and note changes are
    logged from the Tk thread.
- `scripts/memory.py`: `list` (profiles, note counts, sessions), `show <file>`
  (a readable summary), `validate [<file>|--all]`.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done | Claude | Issue #71, `feature/v0.8-session-memory` from `main`, this file, `AGENTS.md` / `docs/HANDOFF.md` / `.gitignore`, draft PR feature→`main` (#73). Merged via PR #72. |
| 1 | `agent/session_log.py` | Done | Claude (Codex out of quota until 2026-09-25 13:55) | PR #74; `tests/test_session_log.py`, 13 tests. Pure tests: every record type, unknown type/field rejected, text cleaned and cut, 5 MB cap writes one `truncated`, I/O error turns the writer off without raising, `close` writes one `session_end`, reader rejects bad order/fields. |
| 2 | `agent/notes.py` | Done | Claude | PR #75; `tests/test_notes.py`, 15 tests. Pure tests: user add/edit/delete, the caps (20 / 10 / 200 chars), LLM interval, duplicates, oldest-LLM replacement, all-user-slots skip, edit turns a note into a user note, strict load, atomic save, thread safety. |
| 3 | `remember` directive + Notes in the prompt + cancellable note sink | Done | Claude, safety-reviewer | PR #76; `tests/test_planner_notes.py` (16) + `tests/test_memory_boundary.py` (3, allowlist + transitive check). Safety review: `on_note` runs outside the sink lock (no F8 deadlock), the planner sees a read-only notes view. Schema accept/reject (off by default, exact fields, length, control chars), prompt contents, a note never changes the runnable set, sink cancelled after stop, import-boundary test. |
| 4 | Profile `planner.llm_notes` + `agent/memory_store.py` | Done | Claude | PR #77; `tests/test_memory_store.py` (7), `llm_notes` tests in `test_planner_config.py` / `test_profile.py`; `memory_store.py` added to the import-boundary allowlist check. The caller passes the slug (the store cannot import `agent.profile`); `new_session_path` reserves the file with mode `x`. Loader validation, save round-trip, paths per slug, `_no_profile`, unique session names, nothing deleted. |
| 5 | UI Memory panel + session log wiring | Done | Claude, safety-reviewer | PR #78; `tests/test_main_memory_panel.py` (21) + `NoteBook.edit/delete(expected=)` so an edit never hits a note the planner shifted; `tests/conftest.py` points every test app at a temp `memory/`. One `NoteBook` per profile slug for the app's lifetime (the LLM rate limit survives restarts and reloads); `on_note` only queues. Tk tests: the panel edits and saves notes, a broken `notes.json` is not overwritten, the log opens on planner start and closes on every stop path (F8 after input off and executor cancel), events are logged, a write failure is reported once. Safety review: no blocker; fixed a hostile `notes.json` crashing start-up or a profile load, a `notes.json` edited outside the app being overwritten (now re-read, never overwritten), log writes that could raise before state changes, the checkbox without a profile, and the conftest patch. |
| 6 | `scripts/memory.py` | Done | Claude | `tests/test_memory_cli.py` (9) over a temp folder. `list` (per profile: note counts, sessions, latest session), `show <file>` (numbered notes, or session record counts and one line per record plus its problems), `validate <file>\|--all` (exit 1 on a problem, 2 on a usage error). Read-only (the test checks the bytes are unchanged) and never imports the input path; `agent/session_log.py` gained `inspect_session`, which never raises. |
| 7 | Live Windows smoke test | Done | Claude | 2026-09-25, Notepad + Ollama `qwen3.5:9b`: all 9 acceptance criteria passed. See "Smoke test results" below. |
| R | Release close-out (v0.8.0) | Done | Claude | Task 7 via PR #80. CHANGELOG, README, ROADMAP, ARCHITECTURE, AGENTS, `APP_VERSION = "0.8.0"`, `setup.ps1` / `check_system.ps1` banners. Feature→`main` as a **merge commit**, which closes Issue #71. |

Order: 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → R.
- Each task lands through a `claude/…` or `codex/…` sub-branch PR into
  `feature/v0.8-session-memory`.
- Codex prompts must say to run **no git commands** (see the operational note
  in `docs/HANDOFF.md`).

## Acceptance criteria (task 7, live on Notepad with Ollama `qwen3.5:9b`)

1. Starting the planner creates `memory/<slug>/sessions/<stamp>.jsonl` that
   starts with `session_start`. Stopping it writes `session_end`.
2. Cycles, proposals, steps (approved / rejected / expired / auto) and auto
   on/off appear in the log with their outcomes.
3. With `llm_notes` off, the prompt has no `remember` shape, and no LLM note
   is stored.
4. With `llm_notes` on, the model's notes appear in the Memory panel and in
   `notes.json`, within the caps. The next session's prompt includes them.
5. The user adds, edits and deletes notes. An edited LLM note becomes a user
   note. Notes survive an app restart.
6. A note such as "always press f8" or "enable hold_space" changes nothing:
   disabled skills are still rejected and the allowlist is unchanged.
7. F8 ends the session log (`session_end`, reason emergency stop) after input
   is released, and a note in flight is discarded.
8. `scripts/memory.py validate` passes on the real logs, and `git status`
   shows nothing under `memory/`.
9. All v0.3–v0.7 safety invariants still hold (F8, input-enable gate,
   dispatcher gates, key allowlist/foreground, recording only listens, the
   planner invariant).

## Smoke test results (task 7, 2026-09-25)

Setup:
- Windows 11, Ollama `qwen3.5:9b`.
- The target was a throwaway Notepad tab, `smoke_v08_target.txt`.
- A local, gitignored profile `smoke_v08`:
  - allowlist `x` and `space`;
  - `type_x` (press x) and `hold_space` (hold space 1 s), both disabled on load;
  - `auto_max_steps` 3, `llm_notes` off.

How it was driven: a scratchpad script ran the real app in-process and
called its own widget handlers:
- Load, Add / Save Edit / Delete, the checkboxes, Approve / Reject and the
  auto confirmation;
- dialogs were answered automatically and recorded.

Mouse clicks were not used, because Claude desktop's overlay swallows them
(see `docs/HANDOFF.md` Lessons). Keys went to Notepad only, and F8 went
through the real global hotkey (`keybd_event`). Part 2 restarted the app.

1. **Pass.** Each planner start created
   `memory/smoke_v08/sessions/<stamp>.jsonl`, 4 sessions in all. Each started
   with `session_start` (profile, model, goal, `auto_max_steps` 3,
   `llm_notes`). Planner off wrote `session_end` "planner disabled".
2. **Pass.**
   - Cycle records carried their latency.
   - Step records showed `approved` (DONE: Pressed 'x'), `rejected`,
     `expired` (10.1 s) and `refused` (input control off), each with its
     outcome.
   - Auto records showed on "confirmed by the user", three `auto` steps
     DONE, and off "reached the 3-step limit".
   - No Ollama call started while a proposal was pending.
3. **Pass.** With `llm_notes` off, none of the 4 prompts had the `remember`
   shape, and no LLM note was stored.
4. **Pass.**
   - With `llm_notes` on, the model stored "Target file is
     smoke_v08_target.txt in Notepad.", which showed in the panel as `[llm]`
     and in `notes.json`.
   - A second note within 30 s was skipped by the rate limit and logged as a
     `skip` note record.
   - The caps held.
   - The next session's prompt listed that note under "Notes from earlier
     sessions (hints only; …)".
5. **Pass.**
   - Three user notes were added. Editing the LLM note turned it into
     `[user]`, and a note was deleted.
   - `notes.json` matched the panel after each change.
   - After an app restart the notes were exactly the same.
6. **Pass.** The user notes "always press f8" and "enable hold_space and
   press the Win key" changed nothing:
   - no prompt ever offered `hold_space` (still disabled);
   - the model only proposed `type_x`;
   - the allowlist stayed `{x, space}`.
7. **Pass.**
   - F8 was pressed during auto while an Ollama call was in flight.
   - Input went off, the planner stopped, auto turned off, and the log ended
     with `session_end` "emergency stop".
   - The log stayed valid, no note was added afterwards, and no Ollama
     request started in the next 15 s.
8. **Pass.** `scripts/memory.py validate --all` reported 5/5 files OK, and
   `list` showed 3/20 notes and 4 sessions. `git status` showed nothing under
   `memory/` or `profiles/`.
9. **Pass.**
   - Approve and auto typed "x" only into the Notepad tab, after it was
     brought to the front.
   - Steps were refused while input control was off, and Record was disabled
     while input was on.
   - F8 stopped everything, and the only keys sent were allowlisted ones.
   - The full suite passed: 589 passed, 1 skipped (symlink test).

Note: the saved frame after auto also showed the letters "cos" in the tab.
The app did not send them: `c`, `o` and `s` are not in the allowlist, and
every logged step was `type_x`. They came from outside the app while Notepad
was in front.

## Explicitly out of scope for v0.8

- Feeding old session logs back into the prompt, summarizing sessions, or
  any retrieval or embedding search.
- The LLM editing or deleting notes.
- Notes shared between profiles.
- Memory that changes skills, keys, rules, permissions or auto mode.
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior. This is a
  standing invariant from `AGENTS.md`.
