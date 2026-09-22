---
applyTo: "vision/**/*.py"
---

Vision modules only observe and measure frames. They must never send mouse/keyboard input.

Return explicit confidence/measurement data and document detector failure modes.

Prefer deterministic OpenCV methods before adding heavyweight ML dependencies.

Avoid blocking the Tkinter UI thread.

Do not add game-memory reading, injection, packet inspection, or anti-cheat workarounds.
