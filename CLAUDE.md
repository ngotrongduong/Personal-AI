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
- Default non-machine tasks to Codex per `AGENTS.md`'s "Default task assignment" —
  Claude's session budget on this repo is the scarcer resource, so spend it on
  work that genuinely needs the user's machine.

## Token economy

Claude's context/output budget is limited per session; these are standing habits
for this repo, not just this-session workarounds:

- Prefer `Grep`/targeted `Read` (with `offset`/`limit`) over reading a whole file
  when only a symbol or a few lines matter.
- Don't re-read a file you just wrote or edited — the tool result already shows
  the diff; a follow-up `Read` of the same file is wasted tokens.
- Batch independent tool calls into one turn instead of one-at-a-time round trips.
- When checking a web page's text/state, prefer `get_page_text`/`read_page` over
  `screenshot`; reserve screenshots and `zoom` for checks that are genuinely
  visual (layout, an image, something text extraction can't answer).
- For open-ended investigation across many files, delegate to the `Explore`
  subagent instead of doing a long chain of exploratory reads directly — it keeps
  the results out of the main session's context and only returns what's relevant.
- Keep commit messages and PR descriptions informative but not exhaustive; link
  to `docs/PLAN.md`/`docs/HANDOFF.md`/the issue instead of restating their
  content.
