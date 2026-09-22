# Personal Game AI v0.2.0

This release adds the first real computer-vision layer.

## Stage 1 features retained

- Select a visible Windows application/game window.
- DXcam live capture.
- Keyboard/mouse output through PyDirectInput.
- Focus target window before keyboard tests.
- Global F8 emergency stop.
- Start/stop capture and event logging.

## New in v0.2 — Vision

### Template selection
1. Start capture.
2. Press **Select Template on Preview**.
3. Drag a rectangle around a stable UI element, icon, button, or object.
4. The selected region is saved automatically in `templates/`.
5. Vision turns on automatically.

### Template detection
The program searches the current frame for the selected template at about 10 Hz.

When found it displays:
- match confidence score
- x/y coordinates
- width/height
- a green box around the best match

Default confidence threshold: `0.82`.

You can adjust the threshold from `0.50` to `0.99`.

### Snapshot capture
Press **Save Snapshot** to store the raw current game frame in `snapshots/`.

## Recommended first test

Use Notepad or another harmless desktop application.

1. Start Capture.
2. Select a visually distinctive toolbar icon or word as the template.
3. Move/resize the target window slightly without changing Windows display scaling.
4. Watch `Vision: FOUND` and the green detection box.
5. Hide the selected feature and confirm the status changes to `NOT FOUND`.

## Important v0.2 limitation

This is exact visual template matching. It is intentionally simple.

It can tolerate small pixel differences, but it is not yet robust against:
- large scale changes
- rotation
- significant animation
- dramatic lighting changes
- objects whose appearance changes constantly

Later versions will add multiple templates, OCR, color/HP-bar analysis, and object detection.

## Safety

Use automation with offline/single-player games or games whose rules permit it.

This project does not include anti-cheat bypassing, memory injection, packet manipulation,
or protected-process evasion.

## Next milestone — v0.3

- Multiple named detectors
- UI regions
- HP/resource bar detection
- OCR
- Game-state variables
- Rules such as:
  `IF CollectButton.visible THEN click(CollectButton.center)`
