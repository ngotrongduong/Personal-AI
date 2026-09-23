# HP / resource bar measurement

Task #3 adds a pure-vision measurement primitive for color-coded HP, mana,
energy, stamina, progress, or similar UI bars.

## Data flow

```text
frame + ResourceBarSpec
        ↓
HSV color mask inside ROI
        ↓
slice coverage along fill direction
        ↓
contiguous fill extent
        ↓
ResourceBarMeasurement
        ↓
apply_resource_measurements(...)
        ↓
GameState Observation.value (0.0–1.0)
```

No keyboard or mouse input is generated.

## Configuration

A `ResourceBarSpec` defines:

- stable `name`
- `roi=(x, y, width, height)` around the fill area
- one or more OpenCV HSV ranges
- fill direction: left→right, right→left, top→bottom, or bottom→top
- minimum matching-color coverage required for a row/column to count as filled
- a small tolerated gap count for separators or one-pixel rendering artifacts

Multiple HSV ranges can represent a bar whose fill changes color (for example
green → yellow → red), and also handle hue wrap-around by supplying two ranges.

## Measurement semantics

`fraction` is normalized to `0.0..1.0`; `percent` is a convenience property.

`valid=False` means the configured ROI does not overlap the current frame. It
is deliberately distinct from a valid empty bar (`valid=True, fraction=0.0`).

`confidence` is a segmentation-cleanliness score inside the configured ROI. It
does **not** prove that the ROI still points at the intended UI element. Live
integration should therefore pair fixed/profile ROIs with stable window sizing
or another detector/anchor when needed.

## GameState bridge

`agent.resource_state_bridge.apply_resource_measurements` stores a valid bar as:

- `Observation.visible=True`
- `Observation.value=<fraction>`
- `Observation.confidence=<segmentation confidence>`
- `Observation.source="vision:resource_bar"`

An invalid measurement is stored as not visible with `value=None`, so an
off-screen/misconfigured ROI cannot silently masquerade as zero HP.

## Current scope

This is pure logic only. The live Tk capture loop does not yet invoke resource
bars. Claude owns the machine-tested live-loop integration work under task #11;
resource-bar live wiring can be added after the detector loop shape is settled.
