Prepare Personal Game AI for a merge/release candidate.

Perform:
1. syntax/compile checks for all Python files
2. unit tests under `tests/`
3. check `requirements.txt`
4. check `.gitignore` for runtime/user data
5. verify F8/input safety invariants in the changed code
6. inspect README/ROADMAP/CHANGELOG consistency
7. review the diff for accidental debug code or absolute local paths

Do not merge automatically.

Return a concise release report with:
- checks passed
- checks failed
- blockers
- files that need follow-up
- exact commands used
