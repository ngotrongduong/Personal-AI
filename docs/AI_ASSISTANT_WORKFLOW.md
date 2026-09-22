# AI Assistant Workflow

This repository contains GitHub-native AI customization so different coding assistants can work consistently.

## Always-on context

- `AGENTS.md`: project mission, architecture, safety invariants, Git workflow.
- `.github/copilot-instructions.md`: concise repository-wide Copilot instructions.
- `.github/instructions/*.instructions.md`: path-specific rules for core, vision, and agent code.

## Specialist agents

- **vision-specialist** — capture, CV, template matching, ROI, HP/resource measurement, OCR.
- **game-state-specialist** — structured state, rules, cooldowns, action intents, game profiles.
- **safety-test-specialist** — regression testing and safety review of any input-producing path.

These live in `.github/agents/` and are intended for GitHub Copilot custom-agent workflows.

## Reusable skills / prompt files

The project treats reusable prompt files as task skills:

- `/implement-v0.3` — take the next coherent Issue #1 task.
- `/vision-debug` — investigate visual-detection failures.
- `/safety-review` — audit input/autonomy safety.
- `/release-check` — prepare a branch for merge/release review.

They live in `.github/prompts/`.

## Recommended development sequence

1. Work on a `feature/*` branch.
2. Select the specialist agent that matches the task.
3. Use a reusable prompt/skill if applicable.
4. Make the smallest coherent change.
5. Run tests and safety review.
6. Open/update a PR.
7. Merge only after the tested branch is stable.

## Human control

AI helpers can propose and implement code, but safety-sensitive behavior should remain reviewable. The project deliberately keeps fast gameplay behavior deterministic/state-machine based rather than delegating frame-by-frame input decisions to a language model.
