Perform a safety-focused review of the current Personal Game AI diff.

Pay special attention to:
- explicit enablement before any generated input
- F8 emergency-stop dominance
- releasing all generated held keys/buttons
- exception/cleanup paths
- repeated action spam
- cooldown/debounce correctness
- thread interaction with Tkinter
- focus changes
- stale vision/game-state data causing actions
- accidental commits of screenshots, templates, logs, secrets, or model files

Do not rank stylistic issues above behavioral safety issues.

Output findings grouped as:
- Blocker
- Important
- Minor

For each finding give the exact file/function, failure scenario, and smallest safe fix.
