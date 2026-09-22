Debug a Personal Game AI visual-detection problem.

Use `vision/`, relevant screenshots/templates supplied by the user, and the active game profile.

Check in this order:
1. capture region and coordinate mapping
2. template/ROI dimensions
3. grayscale/color preprocessing
4. confidence threshold
5. scaling/display-DPI changes
6. animation/lighting changes
7. false positives elsewhere in the frame
8. detector runtime/performance

Do not solve vision failures by introducing input automation or memory reading.

Return:
- probable root cause
- evidence
- smallest proposed fix
- how to reproduce
- how to verify the fix
- expected false-positive/false-negative tradeoff
