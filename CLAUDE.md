# Claude Code notes

`AGENTS.md` is the canonical source of project instructions (mission, architecture,
safety invariants, engineering rules, git/multi-AI workflow) and applies to every
assistant working in this repo, Claude included. Read it first. This file only adds
Claude Code-specific pointers and does not restate or override anything in
`AGENTS.md` — if something here ever looks inconsistent with `AGENTS.md`,
`AGENTS.md` wins.

## Claude-specific tooling

- `.claude/agents/` — Claude Code subagents scoped to this project:
  - `safety-reviewer` — reviews a diff against the safety invariants in `AGENTS.md`
    before it is committed or opened as a PR.
  - `vision-tester` — runs the `vision/` test suite and reports pass/fail after a
    vision-related change.
- For the equivalent GitHub Copilot–oriented instructions, see
  `.github/copilot-instructions.md`, `.github/instructions/`, `.github/agents/`,
  and `.github/prompts/`. They describe the same invariants for a different tool;
  keep them in sync with `AGENTS.md` rather than forking the policy.

## Working in this repo

- Follow the branch/PR conventions in `AGENTS.md` under "Multi-AI coordination":
  branch as `claude/<task>`, fetch and check existing branches/PRs before starting,
  never push directly to `main` or to the active integration branch.
- Run lint + tests locally before opening a PR (`ruff check .`, `pytest`, and the
  compile check — see `scripts/test.ps1` for the exact sequence used in CI).
