# v0.7 detailed plan — Closed-loop planner

Granular checklist for the current milestone (GitHub Issue #61), with status
and owner, so progress can be checked without opening GitHub. This is the same
checklist as Issue #61. **Keep them in sync:** when you tick something here,
tick or comment it there too, in the same commit or timeframe as the work.

For the current PR and branch situation, see `docs/HANDOFF.md` instead. That
file changes faster than this one should.

Earlier versions of this file are preserved in git history on `main`:
- v0.4 Local AI Planner;
- v0.5 Demonstration recording;
- v0.6 Game Profiles + Skills.

`docs/ROADMAP.md`'s Completed section summarizes them.

## Goal

This is the second step toward v1.0 (vision → state → plan → action →
observation). The road to v1.0 has one spec and one release per step:

- v0.6 Profiles + Skills (released, Issue #50);
- **v0.7 closed-loop planner that picks skills (this milestone)**;
- v0.8 session memory: a structured JSONL log plus bounded, user-editable
  notes written by the LLM;
- v1.0 integration, a permissions panel, and a long smoke test.

v0.7 delivers:
- The Ollama planner can propose running one skill from the loaded profile, by
  **name only**.
- **Approve-each-step** is the default mode. Each proposal is shown in the
  Planner panel with Approve / Reject buttons.
- **Auto mode** is an explicit opt-in. Proposals then run without a click,
  inside a step budget and automatic stop conditions.
- **Closed loop:** each prompt carries the goal, the current observations, the
  enabled skills, the rules and the outcomes of the last steps.
- A **Goal** field in the Planner panel is saved in the profile.

User decisions (2026-09-24):
- The LLM only picks a **skill name** defined by the profile. Coordinates, keys
  and durations always come from the profile, and all input goes through
  `ActionDispatcher`.
- The goal comes from a UI field and is stored as `planner.goal` in the
  profile. Load fills the field, and Save writes it.
- The v0.4 `enable_rule` / `disable_rule` directives are kept and still apply
  directly, because they send no input. Only `run_skill` needs approval.
- Auto mode has a step cap. It also turns itself off on events and after
  repeated failures.

## Design constraint (read before implementing anything)

1. **The LLM chooses a name, nothing else.** `run_skill` has exactly the
   fields `type`, `skill` and `reason`. There is no field for a key,
   coordinate, duration or detector. The skill must exist in the loaded
   profile **and be enabled** when the directive is parsed. It is checked
   again when it runs. `reason` is display and prompt text only; it is never
   parsed for commands.
2. **Only the Tk thread submits skills.**
   - The planner thread puts a validated `SkillProposal` into a thread-safe
     single-slot `ProposalMailbox`. The Tk loop (`_poll_preview`) takes it
     from there.
   - The Tk thread decides (approve / auto / reject / expire), rebuilds the
     intent with `SkillBook.build_intent` from **fresh** state, and calls
     `SkillExecutor.submit(..., source="planner")`.
   - The planner never holds a handle to the executor, the dispatcher or
     `InputController`.
3. **Re-checks on execution:**
   - input control is on;
   - the skill still exists and is enabled;
   - the proposal has not expired;
   - the executor is idle;
   - `build_intent` succeeds (a click needs a fresh, confident detection).

   Every dispatcher gate stays unchanged: allowlist, foreground for keys, hold
   cap, rate limit, busy. A proposal that fails a re-check is recorded as
   `refused` and does not run.
4. **One step at a time.**
   - The mailbox holds at most one proposal. It stays occupied from the moment
     a proposal is posted until the Tk thread resolves it: rejected, expired,
     refused, or the skill's result drained.
   - The scheduler skips its LLM call (no Ollama request) while the mailbox is
     occupied or the executor is busy.
   - In approve mode, a proposal expires after **10 s**.
5. **Auto mode is opt-in and bounded.**
   - The mode is **never persisted**. Every app start, profile load and F8
     returns to approve mode.
   - Turning auto on requires input control to be on and a confirmation
     dialog.
   - Auto turns itself off:
     - after `planner.auto_max_steps` executed steps (default 20, hard cap
       **100**);
     - after **3** consecutive refused, BLOCKED or failed steps;
     - on F8;
     - when input control is turned off;
     - on profile load;
     - on Clear Rules;
     - when the planner is disabled.
6. **F8 / window close:** `input.set_enabled(False)` (releases held keys) →
   `executor.cancel()` → stop the planner, drop any pending proposal and turn
   auto off → stop recording. The ordering is unchanged from v0.6.
7. **Stale work is discarded.**
   - Each planner start gets a new generation.
   - A proposal posted after `stop()` raises `PlannerCancelledError`, the same
     pattern as the v0.4 `_CancellableRuleControl`.
   - The Tk thread drops any proposal from an older generation.
8. The v0.4, v0.5 and v0.6 invariants still hold. `recording/` only listens.
   Recording and input control stay mutually exclusive. Skills are disabled by
   default. A profile loads only while input control is off. `profiles/*` is
   gitignored except `profiles/example/profile.json`.

## Directive schema (`agent/llm_planner_schema.py`)

```json
{"type": "run_skill", "skill": "<an enabled skill name>", "reason": "<1-200 chars>"}
{"type": "enable_rule", "rule_name": "<rule name>"}
{"type": "disable_rule", "rule_name": "<rule name>"}
{"type": "noop"}
```

`parse_directive(raw, known_rule_names, runnable_skill_names=())` accepts
`run_skill` under these conditions:
- the fields are exactly `type`, `skill` and `reason`;
- `skill` is a non-empty string in `runnable_skill_names`;
- `reason` is a string that is 1–200 characters after stripping;
- control characters are replaced with spaces.

Every other shape is still rejected.

## Prompt (closed loop)

1. The instruction, plus the **goal**, or "(no goal set)".
2. The game state observations, in the v0.4 format.
3. The runnable skills, one line each: name, type, and detector (click) or key
   (press/hold). Only enabled skills are listed.
4. The rules and whether each is enabled.
5. The **last 5 steps**, oldest first. Each line gives the skill, the
   decision (approved / auto / rejected / expired / refused), the outcome
   message, and the age in seconds, or "none yet".
6. The four exact JSON shapes and the "only these keys" line.

## Profile additions (`planner` block)

```json
"planner": {"enabled": false, "model": "qwen3.5:9b", "interval_seconds": 5.0,
            "goal": "Type an x whenever the status bar is visible.",
            "auto_max_steps": 20}
```

- `goal`: a string of at most 500 characters. Default "".
- `auto_max_steps`: an int from 1 to 100. Default 20. `bool` is rejected.
- `save_profile` writes both fields. `load_planner_config` validates them, and
  `PlannerConfig` carries them.

## Components

- `agent/llm_planner_schema.py`: `RunSkillDirective(skill_name, reason)`, and
  `parse_directive` gains `runnable_skill_names`.
- `agent/step_history.py` (new, pure, thread-safe):
  - `StepRecord(skill_name, reason, decision, outcome, ok, finished_at)`;
  - `StepHistory(maxlen=5)` with `append`, `recent()`, `clear()` and
    `prompt_lines(now)`.
- `agent/llm_planner.py`:
  - `LlmPlanner(client, rule_control, *, skills=None, history=None,
    goal="", proposals=None)`.
  - `skills` is a read-only catalog (`runnable_skills()` → name/type/detail).
  - `proposals` is a sink whose `post(SkillProposal) -> bool` is False when
    the mailbox is full.
  - `run_skill` becomes `PlannerOutcome("proposed <skill>: <reason>")`, or
    "dropped" when the mailbox is full.
- `agent/proposal_mailbox.py` (new, pure, thread-safe):
  - `SkillProposal(skill_name, reason, created_at, generation)`;
  - `ProposalMailbox` with `post`, `take`, `release`, `occupied`, `clear`.
- `agent/autopilot.py` (new, pure, Tk-thread only): the state machine.
  - `mode` is `approve` or `auto`.
  - `offer(proposal, now)` returns `AWAIT` / `EXECUTE`.
  - `approve(now)`, `reject()` and `expire(now)` resolve a pending proposal.
  - `record_result(ok)` returns an auto-off reason when 3 failures come in a
    row.
  - Also `arm_auto(max_steps)`, `disarm(reason)`, `steps_taken`.
- `agent/planner_scheduler.py`: an optional `should_plan: Callable[[], bool]`
  gate. When it returns False, the cycle is skipped without calling the
  planner and nothing is reported.
- `agent/planner_controller.py`: `start(..., skills=, history=, goal=,
  mailbox=, should_plan=)` wires a cancellable proposal sink (a new
  generation per start). `stop()` cancels it.
- `agent/planner_config.py` / `agent/profile.py`: `goal`, `auto_max_steps`.
- `main.py`, Planner panel:
  - a Goal entry;
  - a mode radio (Approve each step / Auto), plus an auto step counter;
  - a proposal line with Approve / Reject buttons;
  - planner cycle reports and log lines now go through a `SimpleQueue`
    drained in `_poll_preview` instead of `root.after` from the scheduler
    thread (closes a v0.4 follow-up).

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done | Claude | Issue #61, `feature/v0.7-closed-loop-planner` from `main`, this file, `AGENTS.md` / `docs/HANDOFF.md`, draft PR feature→`main`. |
| 1 | `run_skill` directive + closed-loop prompt + `agent/step_history.py` + `agent/proposal_mailbox.py` | Done | Claude (Codex out of quota until 2026-09-25 13:55) | Pure tests: schema accept/reject (extra fields, disabled/unknown skill, reason length/type, control chars), prompt contents (goal, only enabled skills, history lines), mailbox single-slot/thread safety, history ring buffer. Fails closed on every error path. |
| 2 | `agent/autopilot.py` | Done | Claude | Pure tests: approve/reject/expire, auto execute and step cap, 3 consecutive failures, disarm reasons, a second offer while pending is dropped. |
| 3 | Scheduler gate + controller wiring | Todo | Claude, safety-reviewer | `should_plan` skip (no planner call), cancellable proposal sink (post after stop raises `PlannerCancelledError`), generation per start. |
| 4 | Profile `planner.goal` / `planner.auto_max_steps` | Todo | Claude | Loader validation, save round-trip, `PlannerConfig` fields. |
| 5 | UI Planner panel + executor + F8 wiring | Todo | Claude, safety-reviewer | Tk tests like `tests/test_main_skills_panel.py`: approve runs through the executor, reject/expire release the mailbox, auto needs input on and a confirmation, the auto-off triggers, F8 order, stale generation dropped, planner reports via queue. |
| 6 | Live Windows smoke test | Todo | Claude | See the acceptance criteria. |
| R | Release close-out (v0.7.0) | Todo | Claude | CHANGELOG, README, ROADMAP, ARCHITECTURE, AGENTS, `APP_VERSION = "0.7.0"`, `setup.ps1` / `check_system.ps1` banners. Feature→`main` as a **merge commit**, which closes Issue #61. |

Order: 0 → 1 → 2 → 3 → 4 → 5 → 6 → R.
- Each task lands through a `claude/…` or `codex/…` sub-branch PR into
  `feature/v0.7-closed-loop-planner`.
- Codex prompts must say to run **no git commands** (see the operational note
  in `docs/HANDOFF.md`).

## Acceptance criteria (task 6, live on Notepad with Ollama `qwen3.5:9b`)

1. Load `profiles/example` and set a goal. With the skills disabled, the
   planner proposes no `run_skill`.
2. Enable `type_x`, input control and the planner. A proposal appears with a
   reason. Nothing is typed until **Approve**, and then Notepad gets "x".
3. **Reject** releases the mailbox, and the next cycle can propose again. An
   unanswered proposal expires after 10 s.
4. While a proposal is pending, no Ollama request is made (checked in the
   log).
5. **Auto mode** needs input on and a confirmation. It runs steps by itself
   and turns off at `auto_max_steps` (set low, e.g. 3, for the test).
6. With auto on, taking Notepad out of the foreground gives BLOCKED steps, and
   auto turns off after 3 in a row.
7. F8 during auto releases input, cancels the skill, stops the planner, clears
   the proposal and returns to approve mode.
8. Save Profile writes `goal` / `auto_max_steps`, and Load restores them.
9. All v0.3–v0.6 safety invariants still hold (F8, input-enable gate,
   dispatcher gates, key allowlist/foreground, recording only listens).

## Explicitly out of scope for v0.7

- Multi-skill sequences or plans in one proposal.
- Persistent logs and memory (v0.8).
- The LLM enabling or disabling skills, or editing the profile.
- Auto-focusing the game window for planner steps: a key step is simply
  BLOCKED if the game is not foreground.
- Anti-cheat bypassing, protected-process evasion, memory injection, packet
  manipulation, credential theft, or stealth/persistence behavior. This is a
  standing invariant from `AGENTS.md`.
