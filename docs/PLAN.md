# v1.3 detailed plan — Imitation (first target: Merchant Guilds)

**Status: in progress** (`feature/v1.3-imitation`, Issue #115).

v1.2 gave recorded demos their first labels. v1.3 is the first milestone where
the agent **copies the user's own play**: it looks at the live screen, finds
the moments in the user's recordings that looked the same, and repeats the
click the user made there.

This is retrieval behaviour cloning. Nothing is trained and nothing is
downloaded; the "model" is the user's own demo clicks plus two small image
features per click. The first target is Merchant Guilds (an idle
crafting/trading game in Google Play Games), where almost every action is a
left click/tap.

## Design constraint (imitation invariant, permanent from v1.3)

1. **Points come only from the user's own recorded clicks.** A proposed point
   is exactly the normalised point of one recorded left click (never an
   average, never from the LLM, never from a detector). The recordings used
   are the ones the profile's `imitation` block selects.
2. **Act only on a recognised screen.** A click is proposed only when the live
   screen is similar to the screen before that demo click
   (`screen_threshold`) **and** the image around the point still matches
   (`patch_threshold`). Otherwise the policy abstains. An unknown screen never
   produces input.
3. **Deny-zones.** The profile lists rectangles (fractions of the client area)
   that imitation never taps, e.g. the `$`/gem/shop buttons. They are checked
   by the policy and again right before a live step is submitted.
4. **Dry run by default.** Dry run only logs and shows what it would click.
   Live mode is an unsaved checkbox. It needs input control on, and it turns
   off on:
   - F8 or input off;
   - profile load;
   - recording start;
   - planner auto;
   - a capture/window change;
   - 3 failed steps in a row.
5. **Same gates as every tap.** A live step is an
   `ActionIntent(action="tap", tap_point=…)` submitted to `SkillExecutor` →
   `ActionDispatcher` → `InputController`. That applies:
   - input on and F8;
   - foreground and hit-test;
   - intent freshness;
   - the profile rate limit;
   - cancel.

   On top of that, imitation keeps its own `min_interval_seconds` between
   steps and a per-target cooldown.
6. **Read-only data.** The `imitation/` modules never import the input path
   (`core/`, `agent/action_dispatcher.py`, `agent/skill_executor.py`,
   pynput, win32 input). They never write or delete a recording. The only
   file they write is an eval report, and only with `--out`.

## Profile format

```json
"imitation": {
  "window_title": "Merchant Guilds",
  "sessions": [],
  "k": 20,
  "screen_threshold": 0.92,
  "patch_threshold": 0.8,
  "cooldown_seconds": 3.0,
  "min_interval_seconds": 1.5,
  "deny_zones": [[0.0, 0.0, 0.25, 0.08]]
}
```

- The block is optional; a profile without it loads as before.
- `window_title`: a non-empty string. A recording is used when its
  `session.json` `window_title` contains it (case-insensitive).
- `sessions`: optional folder names under `recordings/` (plain names, no path
  separators). When non-empty, only these are used; they must still match
  `window_title`.
- `k`: an integer from 1 to 20.
- `screen_threshold`, `patch_threshold`: numbers in (0, 1].
- `cooldown_seconds`: from 0 to 60.
- `min_interval_seconds`: from 0.5 to 60.
- `deny_zones`: at most 16 zones. Each is `[x, y, w, h]` as fractions, with
  `w, h > 0`, `x + w <= 1` and `y + h <= 1`.
- Unknown keys and bools-as-numbers are rejected. Save/Load round-trips the
  block unchanged.

## Components

### `imitation/features.py` (pure numpy/cv2)

- `screen_feature(frame_bgr, side=48) -> np.ndarray`:
  - grayscale;
  - `cv2.resize(..., (side, side), INTER_AREA)`;
  - float32, zero-mean, L2-normalised and flattened;
  - an all-flat frame gives the zero vector.
- `screen_similarity(a, b) -> float`: the dot product, clamped to [-1, 1]. A
  zero vector gives 0.0.
- `patch_at(frame_bgr, fx, fy, fraction=0.16, side=30) -> np.ndarray`
  (`PATCH_FRACTION`, `PATCH_SIDE`; 0.08/24 until #120):
  - a square crop centred on the point, with side
    `max(8, round(fraction * min(h, w)))`;
  - pixels outside the frame are filled by edge replication, so every point in
    [0, 1] works;
  - then grayscale, resized to `side` x `side`, as uint8.
- `patch_similarity(a, b) -> float`: normalised cross-correlation in [-1, 1].
  - If both patches are flat (std < 2), the result is 1.0 when the mean
    difference is ≤ 8, else 0.0.
  - If only one is flat, the result is 0.0.
- `cell_patch_similarity(a, b, cells=3, keep=6) -> float` (#120): splits both
  patches into a 3×3 grid, scores each cell with `patch_similarity`, and
  averages the 6 best. Recorded frames show the mouse cursor (with trails) on
  the click point, which sank whole-patch NCC to a median ~0.75 at the *same*
  target; dropping the worst cells tolerates such small occluders.
- Frames are read with `cv2.imdecode(np.fromfile(path, np.uint8), IMREAD_COLOR)`
  so non-ASCII paths work. An unreadable image gives `None`, never an
  exception.

### `imitation/demo_bank.py`

- `DemoClick` (frozen dataclass) has these fields:
  - `session` (folder name), `t`, `frame_index`, `frame_path`;
  - `fx`, `fy`: normalised by `session.json` `client_width/height`, clamped to
    [0, 1];
  - `screen` (`np.ndarray`, float32) and `patch` (`np.ndarray`, uint8).
- `extract_demo_clicks(session_dir, *, lead_seconds=0.05, max_frame_age=0.5, drag_fraction=0.02, patch_fraction=PATCH_FRACTION) -> ExtractResult(clicks, skipped)`:
  - loads with `recording.dataset.load_session`;
  - a session whose report has errors is skipped whole (`skipped["invalid_session"]`);
  - considers every mouse_button `down` with `button == "left"`, and pairs it
    with the next left `up`;
  - skips, with a reason counted in `skipped`:
    - `non_left`;
    - `no_release`;
    - `drag` (the up point is more than `drag_fraction` × client diagonal
      away);
    - `no_frame` (no frame with `t <= down.t - lead_seconds`, or that frame is
      older than `max_frame_age`);
    - `unreadable_frame`;
  - the pre-click frame is the last `frame` event with
    `t <= down.t - lead_seconds`. Using the frame *before* the press avoids a
    pressed-button look.
- `select_sessions(root, window_title, names=()) -> list[Path]`:
  - uses `recording.dataset.list_sessions`;
  - keeps complete or incomplete sessions whose `window_title` contains
    `window_title` (case-insensitive);
  - if `names` is non-empty, keeps only those names;
  - sorted by name.
- `DemoBank`:
  - built with `DemoBank.build(session_dirs, **extract_kwargs)`;
  - holds `clicks` (a tuple), per-session counts, the skipped totals, and a
    stacked `screens` matrix (N × D) for fast similarity;
  - `DemoBank.from_clicks(clicks)` builds one without disk, for tests and for
    eval folds.

### `imitation/policy.py`

- `PolicyConfig(k=20, screen_threshold=0.92, patch_threshold=0.8, cooldown_seconds=3.0, target_radius=0.03, deny_zones=())`, validated in `__post_init__`.
- `Proposal` (frozen) has these fields:
  - `fx`, `fy`;
  - `screen_similarity`, `patch_similarity`;
  - `votes` (the cluster size), `score`;
  - `demo_session`, `demo_t`;
  - `reason`, a one-line human-readable text.
- `Abstention(reason)`.
- `ImitationPolicy(bank, config).propose(frame_bgr, *, now, recent=()) -> Proposal | Abstention`:
  - `recent` holds `(fx, fy, t)` of recent taps.
  - Steps:
    1. `screen_feature` of the live frame, then similarity to every demo.
    2. Take the top `k` whose similarity ≥ `screen_threshold`. If there are
       none, abstain: "unknown screen (best 0.xx)".
    3. For each of them, compute `cell_patch_similarity(patch_at(live, fx, fy), demo.patch)`
       and keep those ≥ `patch_threshold`. If none remain, abstain: "screen
       known but no demo target matches".
    4. Drop candidates in a deny-zone, and candidates within `target_radius`
       (fraction of the client diagonal, using the live frame's size) of a
       `recent` tap younger than `cooldown_seconds`. If none remain, abstain
       with that reason.
    5. Greedily cluster the survivors by `target_radius`. Each candidate's
       score is `screen_sim * patch_sim`, and a cluster's score is the sum of
       its members'.
    6. Pick the cluster with the best score. The proposal's point is that
       cluster's best single member, so the point is always a real recorded
       point.
  - Deterministic: ties break by earlier demo (session name, then `t`).
- `in_deny_zone(fx, fy, zones) -> bool`: the zone interval is inclusive.

### `imitation/evaluate.py` + `scripts/imitation.py`

- `evaluate(bank, config, *, mode="loso", gap_seconds=10.0) -> EvalReport`:
  - For each demo click, reload its pre-click frame, build a fold bank and
    call `propose` on that frame with `recent=()`.
    - In `loso` mode the fold bank holds the clicks from other sessions.
    - In `loco` mode it holds every other click, except those from the same
      session within ±`gap_seconds`.
  - Each click scores as:
    - `hit`: the proposal is within `target_radius` of the true point;
    - `miss`: a proposal elsewhere;
    - `abstain`.
  - The report gives the counts, `precision = hits / (hits + misses)`,
    `coverage = (hits + misses) / total`, a per-session breakdown and the
    abstain reasons.
- `scripts/imitation.py`:
  - `bank <recordings_root> --window TITLE [--sessions a,b]`: sessions, click
    counts and skip reasons;
  - `eval <recordings_root> (--window TITLE | --profile DIR) [--mode loso|loco] [--out FILE] [--overwrite]`:
    - the window, sessions and thresholds come from the profile's `imitation`
      block when `--profile` is given, otherwise `--window` and the defaults;
    - `--out` is written atomically and never over a recording file.
  - Read-only apart from `--out`.

### Profile (`agent/profile.py`)

- The `imitation` block is parsed into `ImitationConfig` (frozen), saved, and
  round-tripped.
- The loader does not touch recordings.

### `main.py` wiring (Claude)

- An "Imitation" panel:
  - Load Demos, which builds the `DemoBank` on a background thread from
    `recordings/` with the profile's block;
  - status "N clicks from M sessions (skipped …)";
  - Start / Stop;
  - a "Live (send taps)" checkbox that is never saved and is off by default;
  - the last proposal or abstention.
- While running, at most once per `min_interval_seconds` and only when the
  executor is idle, the policy runs on the latest frame (on a worker, not the
  Tk thread).
  - Dry run: log "Would tap (fx, fy) — screen 0.95, patch 0.88, votes 3,
    demo <session> t=…".
  - Live: the Tk thread re-checks the deny-zones, input on, not recording,
    planner auto off and the same window. Then it submits
    `ActionIntent(rule_name="imitation", action="tap", tap_point=(fx, fy), skill_name=None, …)`
    with `source="imitation"`.
- The stop conditions are listed in invariant item 4.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 0 | Kickoff | Done (#116) | Claude | Issue #115, `feature/v1.3-imitation`, this plan, imitation invariant, HANDOFF/ROADMAP, draft release PR #117. |
| 1 | `imitation/features.py` + `imitation/demo_bank.py` | Done (#118) | Codex | Deterministic features and validated extraction with pre-click frame paths. |
| 2 | `imitation/policy.py` | Done (#118) | Codex | Exact recorded points; abstention, deny-zone, cooldown, clustering and tie tests. |
| 3 | `imitation/evaluate.py` + `scripts/imitation.py` | Done (#118) | Codex | LOSO/LOCO, readable summaries and atomic guarded `--out`; CLI smoke tests. |
| 4 | Profile `imitation` block | Done (#118) | Codex | Strict parse/save/round-trip, Save preservation and input-boundary coverage. |
| 5 | `main.py` Imitation panel + live wiring + safety review | Done (#119) | Claude | `imitation/runner.py` worker threads; dry run by default; live only after confirmation, via `SkillExecutor` gates; tap `created_at` is the frame time. safety-reviewer blocker (live var set before the dialog answered) fixed with a separate confirmed flag. |
| 6 | Real demos + offline eval + smoke on Merchant Guilds | In progress (#120) | Claude + user | Demos and eval done (see "Task 6 results"); in-app dry run and harmless live taps next. |
| R | Release v1.3.0 | Todo | Claude | Docs, version, merge commit. |

## Task 6 results (2026-09-28)

**Demos.** Four Merchant Guilds sessions were recorded.
- Two sessions are usable: about 10 minutes, 675 left clicks. After the
  deny-zones (side menu, top bar, offer corner), 634 remain.
- The first two sessions are excluded. The game ignored every click in them:
  no click changed the next frames.

**"Invisible curtain".** During those two sessions the user could not interact
with the game window until they minimised and restored it. We checked:
- The pynput hooks add ~0 ms latency, and the same hooks were active in the
  good sessions.
- No window covers the game.

So this looks like a Google Play Games input glitch, not the recorder. The
workaround is to minimise and restore the game. Note also that `PrintWindow`
on the game window may trigger it, so our diagnostics avoid it; capture uses
Desktop Duplication only.

**Offline eval** (`scripts/imitation.py eval recordings --profile profiles/merchant_guilds`, gap 10 s):

| Patch matching | loco precision | loco coverage | loso precision | loso coverage |
|---|---|---|---|---|
| Whole-patch NCC, 8%/24 px, k=5 (#118) | 16.9% | – | – | – |
| 3×3 cells, best 6, 16%/30 px, k=20 (#120) | 36.9% | 80.3% | 33.3% | 25.8% |

- Counting any click the user made within the next 3 s as correct raises loco
  precision to ~54% (offline experiment). Most of the remaining misses are
  screens where the user makes several valid, different clicks.
- Loso coverage is low because the second session is short: 41 clicks, mostly
  screens the first session never showed.
- Larger patches, finer grids and other `keep` values were not significantly
  better.

## Acceptance criteria

1. `scripts/imitation.py bank` lists the user's Merchant Guilds sessions with
   click counts and skip reasons, and writes nothing.
2. On synthetic sessions, the policy proposes the recorded point for a
   matching screen and abstains on an unknown screen, a mismatching patch, a
   deny-zone or a target in cooldown.
3. `scripts/imitation.py eval` reports hits, misses, abstains, precision and
   coverage in loso and loco modes.
4. Dry run never sends input. Its log line names the demo the point came from.
5. Live mode sends a tap only through `SkillExecutor` → `ActionDispatcher`,
   with input on and the game in the foreground. F8, input off, profile load,
   recording start or planner auto turns it off.
6. A deny-zone point is never tapped, even if a demo click was inside it.
7. Profiles without `imitation`, and every earlier invariant, are unchanged.
8. Tests and ruff are clean; CI is green.

## Out of scope (maybe later)

- keys, drags, scrolls and right clicks in imitation;
- captions of demo clicks by `qwen3.5:9b` (vision), and Set-of-Mark
  prompting;
- training any model;
- self-review and profile patches (v1.4 / v1.5).
