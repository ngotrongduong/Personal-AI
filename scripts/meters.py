"""Read-only calibration and offline testing helpers for v1.1 meters.

Usage from the repo root:

    python scripts/meters.py suggest screenshot.png --roi X Y W H --name hp
    python scripts/meters.py test profiles/my_game screenshot.png
    python scripts/meters.py test profiles/my_game screenshot.png --meter hp

`suggest` prints a JSON meter block to stdout. It never edits a profile.
`test` loads an existing profile and measures its declared meters against one
image. Neither command captures a window or sends input.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.profile import PROFILE_FILENAME, ProfileError, load_profile  # noqa: E402
from vision.resource_bar import HSVRange, measure_resource_bar  # noqa: E402


class MeterCliError(ValueError):
    pass


_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _read_bgr(path: Path) -> np.ndarray:
    if not path.is_file():
        raise MeterCliError(f"No image file {path}.")
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError as error:
        raise MeterCliError(f"Could not read {path}: {error}") from error
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise MeterCliError(f"{path} is not a readable image.")
    return image


def _parse_roi(values: list[int]) -> tuple[int, int, int, int]:
    if len(values) != 4:
        raise MeterCliError("ROI must be X Y WIDTH HEIGHT.")
    x, y, width, height = values
    if x < 0 or y < 0:
        raise MeterCliError("ROI x/y cannot be negative.")
    if width <= 0 or height <= 0:
        raise MeterCliError("ROI width/height must be positive.")
    return x, y, width, height


def _crop_strict(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
) -> np.ndarray:
    x, y, width, height = roi
    frame_h, frame_w = image.shape[:2]
    if x < 0 or y < 0 or x + width > frame_w or y + height > frame_h:
        raise MeterCliError(
            f"ROI {roi} leaves the image ({frame_w}x{frame_h})."
        )
    return image[y : y + height, x : x + width]


def suggest_hsv_ranges(
    image_bgr: np.ndarray,
    roi: tuple[int, int, int, int],
    *,
    min_saturation: int = 80,
    min_value: int = 80,
    hue_radius: int = 10,
) -> tuple[tuple[HSVRange, ...], dict[str, float | int]]:
    """Suggest a dominant-color HSV range inside one ROI.

    This is intentionally a starting point, not automatic calibration. Dark and
    low-saturation pixels are ignored, then the most common hue is found. The
    returned range covers +/- `hue_radius` around that hue and uses a
    conservative lower S/V bound derived from the selected pixels.
    """

    if not 0 <= min_saturation <= 255:
        raise MeterCliError("min_saturation must be between 0 and 255.")
    if not 0 <= min_value <= 255:
        raise MeterCliError("min_value must be between 0 and 255.")
    if not 1 <= hue_radius <= 45:
        raise MeterCliError("hue_radius must be between 1 and 45.")

    crop = _crop_strict(image_bgr, roi)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    eligible = (
        (hsv[:, :, 1] >= min_saturation)
        & (hsv[:, :, 2] >= min_value)
    )
    eligible_count = int(eligible.sum())
    total = int(eligible.size)
    if eligible_count == 0:
        raise MeterCliError(
            "No sufficiently saturated/bright pixels in the ROI. "
            "Lower --min-saturation/--min-value or choose the colored fill area."
        )

    pixels = hsv[eligible]
    hues = pixels[:, 0].astype(np.int16)
    histogram = np.bincount(hues, minlength=180)
    peak = int(histogram.argmax())

    raw_distance = np.abs(hues - peak)
    circular_distance = np.minimum(raw_distance, 180 - raw_distance)
    cluster = pixels[circular_distance <= hue_radius]
    if cluster.size == 0:
        raise MeterCliError("Could not isolate a dominant hue cluster.")

    sat_floor = max(0, int(math.floor(float(np.percentile(cluster[:, 1], 10)))) - 10)
    value_floor = max(0, int(math.floor(float(np.percentile(cluster[:, 2], 10)))) - 10)

    low = peak - hue_radius
    high = peak + hue_radius
    ranges: list[HSVRange] = []
    if low < 0:
        ranges.append(HSVRange((0, sat_floor, value_floor), (high, 255, 255)))
        ranges.append(
            HSVRange((180 + low, sat_floor, value_floor), (179, 255, 255))
        )
    elif high > 179:
        ranges.append(
            HSVRange((low, sat_floor, value_floor), (179, 255, 255))
        )
        ranges.append(
            HSVRange((0, sat_floor, value_floor), (high - 180, 255, 255))
        )
    else:
        ranges.append(
            HSVRange((low, sat_floor, value_floor), (high, 255, 255))
        )

    stats: dict[str, float | int] = {
        "peak_hue": peak,
        "eligible_pixels": eligible_count,
        "total_pixels": total,
        "eligible_fraction": eligible_count / total,
        "cluster_pixels": int(len(cluster)),
    }
    return tuple(ranges), stats


def _range_block(value: HSVRange) -> dict[str, list[int]]:
    return {
        "lower": list(value.lower),
        "upper": list(value.upper),
    }



def _validate_suggest_options(args: argparse.Namespace) -> None:
    if not isinstance(args.name, str) or not _NAME_PATTERN.fullmatch(args.name):
        raise MeterCliError(
            "name must be 1-64 characters from A-Z, a-z, 0-9, '_', '.', '-'."
        )
    if (
        not math.isfinite(args.min_slice_coverage)
        or not 0.0 < args.min_slice_coverage <= 1.0
    ):
        raise MeterCliError("min_slice_coverage must be in (0.0, 1.0].")
    if args.max_gap_slices < 0:
        raise MeterCliError("max_gap_slices cannot be negative.")
    if (
        not math.isfinite(args.min_confidence)
        or not 0.0 <= args.min_confidence <= 1.0
    ):
        raise MeterCliError("min_confidence must be between 0.0 and 1.0.")


def cmd_suggest(args: argparse.Namespace) -> int:
    _validate_suggest_options(args)
    image = _read_bgr(args.image)
    roi = _parse_roi(args.roi)
    ranges, stats = suggest_hsv_ranges(
        image,
        roi,
        min_saturation=args.min_saturation,
        min_value=args.min_value,
        hue_radius=args.hue_radius,
    )

    block = {
        "name": args.name,
        "roi": list(roi),
        "hsv_ranges": [_range_block(value) for value in ranges],
        "direction": args.direction,
        "min_slice_coverage": args.min_slice_coverage,
        "max_gap_slices": args.max_gap_slices,
        "min_confidence": args.min_confidence,
    }
    print(json.dumps(block, indent=2))
    print(
        f"# dominant H={stats['peak_hue']}; "
        f"eligible={stats['eligible_pixels']}/{stats['total_pixels']} "
        f"({float(stats['eligible_fraction']) * 100.0:.1f}%)",
        file=sys.stderr,
    )
    print(
        "# suggestion only: verify it with the 'test' command and adjust the "
        "ROI/HSV range on real screenshots.",
        file=sys.stderr,
    )
    return 0


def _resolve_profile(value: Path) -> Path:
    if value.is_dir():
        return value
    if value.is_file() and value.name == PROFILE_FILENAME:
        return value.parent
    raise MeterCliError(
        f"{value} must be a profile folder or a {PROFILE_FILENAME} file."
    )


def cmd_test(args: argparse.Namespace) -> int:
    folder = _resolve_profile(args.profile)
    try:
        profile = load_profile(folder)
    except ProfileError as error:
        raise MeterCliError(str(error)) from error

    meters = list(profile.meters)
    if args.meter:
        wanted = set(args.meter)
        unknown = wanted - {meter.name for meter in meters}
        if unknown:
            raise MeterCliError(
                f"Unknown meter(s): {', '.join(sorted(unknown))}."
            )
        meters = [meter for meter in meters if meter.name in wanted]

    if not meters:
        raise MeterCliError(f"Profile {profile.name!r} declares no meters.")

    image = _read_bgr(args.image)
    failed = 0
    print(f"{'meter':<24} {'value':>8} {'conf':>7} {'status':<12} roi")
    for meter in meters:
        try:
            _crop_strict(image, meter.roi)
        except MeterCliError:
            print(
                f"{meter.name:<24} {'-':>8} {0.0:>7.3f} "
                f"{'INVALID ROI':<12} {meter.roi}"
            )
            failed += 1
            continue

        result = measure_resource_bar(image, meter.spec())
        accepted = result.valid and result.confidence >= meter.min_confidence
        status = "OK" if accepted else (
            "INVALID ROI" if not result.valid else "LOW CONF"
        )
        value = f"{result.percent:6.1f}%" if result.valid else "   -"
        bbox = result.bbox if result.bbox is not None else meter.roi
        print(
            f"{meter.name:<24} {value:>8} {result.confidence:>7.3f} "
            f"{status:<12} {bbox}"
        )
        if not accepted:
            failed += 1

    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meters.py",
        description="Suggest meter HSV ranges and test profile meters on screenshots.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    suggest = commands.add_parser(
        "suggest",
        help="suggest a meter HSV range from a screenshot ROI (read-only)",
    )
    suggest.add_argument("image", type=Path)
    suggest.add_argument(
        "--roi",
        nargs=4,
        type=int,
        metavar=("X", "Y", "W", "H"),
        required=True,
    )
    suggest.add_argument("--name", default="meter")
    suggest.add_argument(
        "--direction",
        choices=(
            "left_to_right",
            "right_to_left",
            "top_to_bottom",
            "bottom_to_top",
        ),
        default="left_to_right",
    )
    suggest.add_argument("--min-saturation", type=int, default=80)
    suggest.add_argument("--min-value", type=int, default=80)
    suggest.add_argument("--hue-radius", type=int, default=10)
    suggest.add_argument("--min-slice-coverage", type=float, default=0.5)
    suggest.add_argument("--max-gap-slices", type=int, default=1)
    suggest.add_argument("--min-confidence", type=float, default=0.8)
    suggest.set_defaults(func=cmd_suggest)

    test = commands.add_parser(
        "test",
        help="measure profile meters against one screenshot (read-only)",
    )
    test.add_argument("profile", type=Path)
    test.add_argument("image", type=Path)
    test.add_argument(
        "--meter",
        action="append",
        help="only test this meter name (repeatable)",
    )
    test.set_defaults(func=cmd_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (MeterCliError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())