---
applyTo: "agent/**/*.py"
---

Agent modules maintain state and decide action intents. They must not bypass the explicit input-enable gate.

Use monotonic time for cooldowns/debouncing.

Rules must be deterministic, inspectable, and safe with missing observations.

Keep game-specific conditions/config outside generic rule-engine code where possible.

Add unit tests for state transitions and cooldown behavior.
