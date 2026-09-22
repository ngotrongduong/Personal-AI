---
applyTo: "core/**/*.py"
---

Core input/capture code is safety-critical.

Input must default to disabled. F8/disable paths must release all app-generated held keys/buttons.

Keep window focus, screen capture, and input sending as separate responsibilities.

Do not add stealth input, protected-process evasion, memory injection, packet manipulation, or anti-cheat bypass behavior.

Favor explicit cleanup and conservative error handling.
