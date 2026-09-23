# v0.3 detailed plan — Game State + Rules

Granular checklist for the current milestone (GitHub Issue #1), with status and
owner, so progress can be checked without opening GitHub. This is the same
checklist as Issue #1 — **keep them in sync**: when you tick something here,
tick/comment it there too (and vice versa), same commit/timeframe as the work.

For "what's the current PR/branch situation right now," see `docs/HANDOFF.md`
instead — that one changes faster than this file should.

## Goal

Move Personal Game AI from seeing one visual template to maintaining a usable
game state and performing safe rule-based actions.

## Checklist

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 1 | Multiple named visual detectors per game profile | Pure logic done | Codex (PR #5) | `vision/detector_registry.py`. Not yet wired into the live Tk capture loop — that's task 11. |
| 2 | Configurable regions of interest (ROI) | Done | Codex (PR #5) | ROI → full-frame bbox translation, with tests. |
| 3 | HP/resource bar measurement | Done | Codex (PR #8) | HSV/ROI measurement for horizontal/vertical bars, four fill directions, multiple color ranges, gap tolerance, confidence, GameState bridge, synthetic tests. Cross-checked with `scripts/test.ps1` on Windows (41/41 pass, ruff clean) before merge. |
| 4 | Basic OCR for simple text/numbers | Assigned to Codex | Codex (`codex/v0.3-ocr`, in progress) | Pure logic (given a frame/ROI in, text out); assigned via Issue #1 comment. `PytesseractEngine` implementation + fake-engine-testable abstraction; Claude will install real Tesseract and live-smoke-test once Codex's PR lands. |
| 5 | Persistent game-state variables | Done | Codex (PR #2) | `agent/game_state.py` (`GameState`, `Observation`). |
| 6 | Safe rule engine (`IF X.visible THEN click`) | Done | Codex (PR #2) | `agent/rule_engine.py` (`RuleEngine`, `VisibilityRule`). Produces `ActionIntent`s only — nothing dispatches them yet. |
| 7 | Action cooldowns/debouncing | Done | Codex (PR #2) | Built into `RuleEngine` (monotonic-time cooldown per rule). |
| 8 | Action log (rule, target, confidence, result) | Done | Claude (task 12, PR #10) | Satisfied as a side effect of task 12: `main.py`'s `_run_vision_if_due` logs `Rule '<name>' target=<detector> confidence=<c> -> DISPATCHED/BLOCKED: <reason>` for every dispatch attempt. |
| 9 | F8 global emergency stop active for every autonomous action | Done | Claude | Verified live with a real autonomous action now dispatched (task 12, PR #10): F8 immediately flips `input.enabled` off, further rule firings log `BLOCKED: Input control is disabled.`, and vision/detection keeps running unaffected. |
| 10 | Test the full loop on a harmless/offline target before any real game profile | Done | Claude | Live-smoke-tested task 12's dispatcher end-to-end against a throwaway Notepad window (PR #10). |

## Not part of this checklist (tracked separately)

Two extra increments came out of getting Claude+Codex working the repo together,
not from Issue #1's original scope — tracked here so they don't get lost:

| # | Task | Status | Owner | Notes |
|---|------|--------|-------|-------|
| 11 | Wire `DetectorRegistry`/vision→GameState bridge into the live Tk capture loop; show multiple live detector states in GUI/log | Done | Claude (PR #9, merged) | `_selection_release` now also registers the dragged ROI into `DetectorRegistry`; `_run_vision_if_due` runs `detect_all` each tick and feeds results through `apply_detections` into `GameState`, updating a `Detectors:` status line and logging FOUND/LOST transitions. Legacy single-template path untouched. Live-smoke-tested on Windows with two named templates (`line1`, `line3`) against a real Notepad window — both showed simultaneous `FOUND(1.00)`; confirmed F8 emergency stop still works and vision keeps running after it (vision never sends input). 31/31 tests pass, ruff clean. |
| 12 | Gated action dispatcher (`ActionIntent` → `InputController`, only when input explicitly enabled) | Done, PR open (#10) | Claude | Branch `claude/action-dispatcher`, retargeted directly onto `feature/v0.3-game-state` now that PR #9 merged. New `agent/action_dispatcher.py` (`ActionDispatcher`, `DispatchResult`) enforces all 4 gates from `docs/ARCHITECTURE.md` in order: input control explicitly enabled (also covers "F8 hasn't fired," since `emergency_stop()` sets `input.enabled = False`), action supported (`click` only), intent not stale (`max_intent_age_seconds`, default 0.5s), target window/bbox resolvable. Wired into `main.py`'s `_run_vision_if_due` via a new "Rules" UI section (add/clear rules, min confidence). Dispatch hwnd is sourced from `self.capture.hwnd` (the window actually being captured), not the window-picker combobox selection — a safety-reviewer subagent caught that the two can diverge and this was fixed before commit, with a dedicated regression test (`tests/test_main_action_dispatch.py`). Live-smoke-tested on Windows against a throwaway Notepad window: dispatch correctly blocked before input control was enabled, correctly dispatched a real click at the right coordinates once enabled (visually confirmed), and F8 immediately blocked further dispatch again while vision kept running. 42/42 tests pass, ruff clean. |

## Acceptance criteria (from Issue #1, unchanged)

- Multiple detectors can run without overwriting each other. — satisfied at the
  pure-logic level (task 1/2 tests); not yet exercised live (task 11).
- Game state updates from live screen capture. — satisfied (task 11).
- A rule can trigger a keyboard/mouse action only when input control is
  explicitly enabled. — satisfied and live-smoke-tested (task 12).
- Repeated detections cannot spam actions due to cooldown/debounce logic. —
  satisfied at the pure-logic level (task 7); not yet exercised live.
- F8 immediately disables autonomous input and releases held inputs. — verified
  live against a real autonomous action (task 12 smoke test).

## Explicitly out of scope for v0.3

Local LLM planning, imitation learning, anti-cheat-related behavior, memory
injection, and packet manipulation — see `AGENTS.md` for the standing safety
invariants these fall under.
