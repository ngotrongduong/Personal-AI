# Personal Game AI — User Guide (v1.0)

This guide takes you from a fresh checkout to a first supervised agent run.
The target is Notepad, so nothing can go wrong in a game. It then shows how
to point the agent at your own game.

## What the agent is, and what it will never do

The app watches one window through screen capture. Named detectors turn each
frame into a game state, for example "the OK button is visible at 0.93". A
local language model (Ollama) reads that state and your goal, then proposes
one **skill** from your profile, such as "press x" or "hold space for 1 s".

The app, not the model, decides whether anything is sent:
- **Input control** is off at start. Nothing reaches the keyboard or mouse
  until you tick **Enable keyboard/mouse control**.
- **Approve each step** is the default. Every proposal waits for your
  **Approve** click. **Auto** mode needs input control and a confirmation
  dialog, it is never saved, and it turns itself off after a step limit or
  after 3 failed steps in a row.
- **Skills come only from your profile.** They start disabled. The keys come
  from `permissions.allowed_keys`, and F8, the Windows keys and key combos
  are never allowed. The model picks a skill by name. It never picks keys,
  positions or hold times.
- **F8 stops everything, at any time**, even when the app window is not
  focused. It turns input control off, releases any held key, stops the
  planner and ends the run.
- **Runs are bounded.** Every agent run has a time budget (15 min by
  default, 120 max) and can have a goal that ends it.
- **Nothing leaves your PC.** Profiles, recordings, session logs and notes
  stay in local, gitignored folders. Ollama runs locally.

Use automation only in offline or single-player games, or in games whose
rules allow it. The app has no anti-cheat bypass, memory reading or packet
tricks.

## 1. Install

You need Windows 10/11, 64-bit Python 3.10+ (the `py` launcher) and
[Ollama](https://ollama.com).

```powershell
.\setup.ps1          # creates .venv and installs the dependencies
.\check_system.ps1   # optional: shows Windows, CPU, RAM, GPU and Python
ollama pull qwen3.5:9b
```

`scripts/models.ps1` has other model options. `docs/MODELS.md` compares them.

Start the app with `.\run.bat`.

## 2. A first run on Notepad

1. **Open Notepad** with an empty document. The app only sends keys to the
   window you pick, so keep your other windows out of the way.
2. **Pick the window.** In **Game window**, choose `Untitled - Notepad`
   (press **Refresh** if it is missing), then press **Start Capture**. The
   preview shows Notepad.
3. **Load the example profile.** In **Profile**, choose `example` and press
   **Load Profile**. Input control must be off to load a profile. The
   example has two skills, `type_x` and `hold_space`. It also sets the Goal
   ("Type one x in the Notepad window, then stop."), the model
   (`qwen3.5:9b`) and a 5-minute run budget.
4. **Enable a skill.** In **Skills**, tick **Enabled** next to `type_x`.
   Leave `hold_space` off.
5. **Preflight.** In **Agent**, press **Preflight**. Each check shows
   `[OK]`, `[FAIL]` or `[NOTE]`:
   - `[OK] Profile`, `[OK] Capture`, `[OK] Planner settings`, `[OK] Ollama`
     and `[OK] Enabled skills: type_x` are required.
   - `[NOTE] Input control: off` is only advice. Proposals still appear,
     but a step is refused until you turn input control on.
   - `[NOTE] Goal` is advice too. Without a goal the model guesses.
   If a required check fails, see [Troubleshooting](#7-troubleshooting).
6. **Turn input control on.** Tick **Enable keyboard/mouse control**.
7. **Start Agent.** It runs the preflight again. If everything required
   passes, it starts the planner. The run line shows the time left, the
   number of steps, the effects and the goal. Start Agent never turns on
   input control or auto mode for you.
8. **Approve.** After a few seconds a proposal appears in the **Planner**
   box, e.g. `type_x — type an x`, with a countdown. Press **Approve**. The
   app focuses Notepad and presses `x`. **Reject** drops the proposal, and an
   unanswered proposal expires after 10 s.
9. **Stop.** Press **Stop Agent**, or F8 anywhere. The log explains why the
   run ended.

## 3. How a run ends

| What happens | The run ends with | Auto mode | Input control |
|---|---|---|---|
| You press **Stop Agent** | `agent stopped` | off | unchanged |
| The time budget runs out | `run budget reached` | off | unchanged |
| The goal detector is seen (`stop_when`) | `goal reached` | off | unchanged |
| You untick **Enable LLM planner** | `planner disabled` | off | unchanged |
| You load a profile or press **Clear Rules** | the reason, in words | off | unchanged |
| **F8** | `emergency stop` | off | **off** |

On every stop, a skill the planner started is cancelled (a held key is
released), and a proposal that was still waiting is dropped. Only F8 also
turns input control off.

## 4. Your own game: detectors, expected effects and a goal

The agent sees only what your **detectors** see. A detector is a small
template image with a name.

1. Start capture on your game window.
2. Type a name in **Detector name** (e.g. `ok_button`), press **Select
   Template on Preview** and drag a rectangle around the element on the
   preview. Repeat for each thing the agent should notice.
3. Press **Save Profile…** and give the profile a name. The app writes
   `profiles/<name>/profile.json` and the template images next to it. The
   `profiles/` folder is gitignored, so your templates stay local.
4. Close the app or turn input control off, then edit `profile.json` by
   hand:
   - Add `permissions.allowed_keys` and key skills (`press` / `hold`). A
     `click` skill clicks a detector's box.
   - Add an **`expect`** block to a skill: what the skill should change on
     screen. After each step the app watches for it and records the effect
     as `confirmed` or `not_seen`.
   - Add a **`stop_when`** block to `planner`: a detector that means "done".
   - Set **`max_run_minutes`** in `planner` (default 15, at most 120).
5. Press **Load Profile**. If a block is wrong (an unknown field, a detector
   that is not declared, a budget over 120), loading fails with a message
   that names the problem.

An example with all three:

```json
{
  "format_version": 1,
  "name": "My game",
  "permissions": {"allowed_keys": ["e", "space"], "max_hold_seconds": 1.5,
                  "max_actions_per_second": 5},
  "detectors": [
    {"name": "chest", "template": "templates/chest.png", "threshold": 0.85, "roi": null},
    {"name": "chest_open", "template": "templates/chest_open.png", "threshold": 0.85, "roi": null},
    {"name": "level_done", "template": "templates/level_done.png", "threshold": 0.9, "roi": null}
  ],
  "skills": [
    {"name": "open_chest", "type": "press", "key": "e", "enabled": false,
     "expect": {"detector": "chest_open", "visible": true, "within_seconds": 2.0}},
    {"name": "jump", "type": "press", "key": "space", "enabled": false}
  ],
  "rules": [],
  "planner": {
    "enabled": false,
    "model": "qwen3.5:9b",
    "goal": "Open every chest you see, then stop.",
    "auto_max_steps": 10,
    "max_run_minutes": 20,
    "stop_when": {"detector": "level_done", "visible": true}
  }
}
```

The `expect` fields:
- `detector`: a detector declared in the profile;
- `visible`: `true` means it should appear, `false` means it should disappear;
- `within_seconds`: up to 10, default 2;
- `min_confidence`: 0 to 1, default 0.8.

The `stop_when` fields are `detector`, `visible` and `min_confidence`.

What effects change:
- **The model sees them.** The planner's recent-steps list reads, for
  example, `open_chest (approved) → DONE, effect not seen (chest_open
  visible)`.
- **Auto mode counts them.** In auto mode a `not_seen` step counts as a
  failed step. Three in a row turn auto off. A step is never retried
  automatically.
- **The planner waits for them.** While the app is still watching for an
  effect, the planner does not ask the model for the next step.
- **They only ever reduce activity.** The goal and the effects can stop
  the planner or turn auto off. They never turn anything on.

A goal only counts when the detector is seen *after* the run started, in a
fresh frame (at most 1 s old), so a state left over from before the run
never ends it. The check runs on every frame, though: if the goal is
already on screen when you press Start Agent, the run ends at once. Start
from a screen where the goal detector is not visible.

## 5. Auto mode

Auto mode runs proposals without the Approve click. It needs:
- input control on;
- a running planner;
- your **Yes** in the confirmation dialog.

It runs at most **Auto max steps** steps, and only into the window you
confirmed it for. It turns off after 3 failed, blocked or `not_seen` steps
in a row, and on every stop in the table above. It is never saved in the
profile. Use it only after watching a few approved steps work.

## 6. What happened? Session logs and notes

Every run writes a session log, `memory/<profile>/sessions/<stamp>.jsonl`.
It has one line per event: the start settings, each planner cycle, each
step and its decision, each effect, auto on/off, note changes, and the
reason the run ended.

```powershell
python scripts/memory.py list
python scripts/memory.py show memory/<profile>/sessions/<stamp>.jsonl
python scripts/memory.py validate --all
```

`show` prints the log readably. When there are effects it adds a line such
as `effects: 3 confirmed / 1 not seen`, and lines such as `effect: open_chest
confirmed (chest_open, 0.4s)`.

The **Memory** box holds short notes that the planner sees as hints, e.g.
"the chest needs a moment to open". Notes never change skills, keys or
permissions. Tick **Let the planner write notes** if the model may add its
own.

## 7. Troubleshooting

| Preflight says | Do this |
|---|---|
| `[FAIL] Profile: load a game profile` | Turn input control off, pick a profile and press **Load Profile**. |
| `[FAIL] Capture: start capture on the game window` | Pick the window and press **Start Capture**. |
| `[FAIL] Planner settings` | Type a model name in the **Model** field, e.g. `qwen3.5:9b`. |
| `[FAIL] Ollama: … not installed; run: ollama pull …` | Run the `ollama pull` command it shows. |
| `[FAIL] Ollama: Could not reach Ollama: …` | Start Ollama (the tray app or `ollama serve`). If the profile sets `host` / `port`, check them. |
| `[FAIL] Ollama: the planner settings changed during the check` | You edited the Model field while it was checking. Press Preflight again. |
| `[FAIL] Enabled skills: enable at least one skill` | Tick **Enabled** for a skill in **Skills**. |
| `[NOTE] Input control: off` | Steps are refused until you tick **Enable keyboard/mouse control**. |

Other problems:
- **A step is refused because the window is not in the foreground.** The app focuses the
  window before each approved step. Keep the game window visible, and do
  not click into other windows during a step.
- **Detections flicker.** Raise the detector's threshold, or select a
  smaller, more distinctive template. Avoid parts that animate.
- **The model proposes nothing useful.** Make the goal concrete ("press e
  when a chest is visible") and add notes. A larger model can help; see
  `docs/MODELS.md`.
- **Loading a profile fails.** The message names the field. Common causes
  are an `expect` or `stop_when` detector that is not in `detectors`, and
  `max_run_minutes` over 120.

## 8. Where things live

| Path | What | In git? |
|---|---|---|
| `profiles/example/profile.json` | the Notepad demo | yes |
| `profiles/<name>/` | your profiles and template images | no (gitignored) |
| `memory/<profile>/` | session logs and notes | no (gitignored) |
| `recordings/` | your demonstrations (see the README) | no (gitignored) |

The app never deletes anything in these folders.
