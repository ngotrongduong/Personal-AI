"""Game profiles (v0.6): detectors, skills, rules and permissions on disk.

A profile lives in `profiles/<name>/profile.json`, with its template PNGs in
`profiles/<name>/templates/`. Loading is strict: unknown fields, duplicate
names, dangling references, template paths that leave the profile folder and
skills the profile's permissions do not allow are all rejected with a
`ProfileError` before anything is registered. This module only reads and
writes files; it never sends input.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any

import cv2
import numpy as np

from vision.detector_registry import DetectorRegistry, DetectorSpec
from vision.resource_bar import Direction, HSVRange, ResourceBarSpec

from .agent_session import check_goal_detector
from .planner_config import PlannerConfig, load_planner_config
from .meter_conditions import MeterConditionError, parse_meter_condition
from .rule_engine import SKILL_RULE_ACTION, MeterRule, Rule, RuleEngine, VisibilityRule
from .skill_effects import ExpectationError, ExpectationLike, parse_expectation
from .skills import (
    ClickSkill,
    HoldSkill,
    PressSkill,
    Skill,
    SkillBook,
    SkillError,
    SkillPermissions,
)


PROFILE_FILENAME = "profile.json"
TEMPLATES_DIRNAME = "templates"
FORMAT_VERSION = 1

_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SLUG_INVALID = re.compile(r"[^a-z0-9_-]+")

_TOP_FIELDS = {"format_version", "name", "permissions", "detectors", "meters", "skills", "rules", "planner"}
_PERMISSION_FIELDS = {"allowed_keys", "max_hold_seconds", "max_actions_per_second"}
_DETECTOR_FIELDS = {"name", "template", "threshold", "roi"}
_METER_FIELDS = {
    "name",
    "roi",
    "hsv_ranges",
    "direction",
    "min_slice_coverage",
    "max_gap_slices",
    "min_confidence",
}
_HSV_RANGE_FIELDS = {"lower", "upper"}
_SKILL_FIELDS = {
    ClickSkill.TYPE: {
        "name",
        "type",
        "detector",
        "min_confidence",
        "max_observation_age_seconds",
        "enabled",
        "expect",
    },
    PressSkill.TYPE: {"name", "type", "key", "enabled", "expect"},
    HoldSkill.TYPE: {"name", "type", "key", "seconds", "enabled", "expect"},
}
_COMMON_RULE_FIELDS = {
    "name",
    "skill",
    "max_observation_age_seconds",
    "cooldown_seconds",
    "enabled",
}
_DETECTOR_RULE_FIELDS = _COMMON_RULE_FIELDS | {"detector", "min_confidence"}
_METER_RULE_FIELDS = _COMMON_RULE_FIELDS | {
    "meter",
    "below",
    "above",
    "rises",
    "falls",
    "min_confidence",
}


class ProfileError(ValueError):
    """A profile is malformed, unsafe or cannot be read or written."""


@dataclass(frozen=True, slots=True)
class DetectorDefinition:
    """One template detector; `template` is relative to the profile folder."""

    name: str
    template: str
    threshold: float = 0.82
    roi: tuple[int, int, int, int] | None = None

    def spec(self) -> DetectorSpec:
        return DetectorSpec(name=self.name, threshold=self.threshold, roi=self.roi)


@dataclass(frozen=True, slots=True)
class MeterDefinition:
    """One profile-declared HP/resource meter.

    Measurement details map directly to `vision.resource_bar.ResourceBarSpec`.
    `min_confidence` is the profile-level acceptance threshold used by later
    meter conditions/live wiring.
    """

    name: str
    roi: tuple[int, int, int, int]
    hsv_ranges: tuple[HSVRange, ...]
    direction: Direction = "left_to_right"
    min_slice_coverage: float = 0.50
    max_gap_slices: int = 1
    min_confidence: float = 0.80

    def spec(self) -> ResourceBarSpec:
        return ResourceBarSpec(
            name=self.name,
            roi=self.roi,
            hsv_ranges=self.hsv_ranges,
            direction=self.direction,
            min_slice_coverage=self.min_slice_coverage,
            max_gap_slices=self.max_gap_slices,
        )


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    """A detector/meter rule that fires a skill, plus its initial enabled state."""

    rule: Rule
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class GameProfile:
    name: str
    directory: Path
    permissions: SkillPermissions
    detectors: tuple[DetectorDefinition, ...]
    skills: tuple[Skill, ...]
    rules: tuple[RuleDefinition, ...]
    planner: PlannerConfig
    # Skill name -> observed effect it should have (v1.0). Observation only:
    # nothing on the input path reads it.
    expectations: Mapping[str, ExpectationLike] = field(
        default_factory=lambda: MappingProxyType({})
    )
    meters: tuple[MeterDefinition, ...] = ()

    def skill_book(self) -> SkillBook:
        """A fresh `SkillBook`; skills start with their profile `enabled` flag."""

        return SkillBook(self.skills, self.permissions)

    def rule_engine(self) -> RuleEngine:
        engine = RuleEngine([definition.rule for definition in self.rules])
        for definition in self.rules:
            if not definition.enabled:
                engine.disable_rule(definition.rule.name)
        return engine

    def template_path(self, detector: DetectorDefinition) -> Path:
        return self.directory / detector.template


# --------------------------------------------------------------------------- load


def load_profile(
    directory: str | Path, registry: DetectorRegistry | None = None
) -> GameProfile:
    """Read and validate `directory/profile.json`.

    With `registry`, every detector is registered from its template only after
    the whole profile validated. If a registration fails, the detectors this
    call already registered are removed again, so no partial profile remains.
    """

    folder = Path(directory)
    path = folder / PROFILE_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProfileError(f"No {PROFILE_FILENAME} in {folder}.") from None
    except OSError as error:
        raise ProfileError(f"Could not read {path}: {error}") from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProfileError(f"{path} is not valid JSON: {error}") from error

    profile = parse_profile(data, folder)
    if registry is not None:
        _register_detectors(profile, registry)
    return profile


def parse_profile(data: object, directory: str | Path) -> GameProfile:
    """Validate an already-decoded profile mapping located in `directory`."""

    folder = Path(directory)
    top = _require_object(data, "profile")
    _reject_unknown(top, _TOP_FIELDS, "profile")

    version = top.get("format_version")
    if type(version) is not int or version != FORMAT_VERSION:
        raise ProfileError(
            f"Unsupported format_version {version!r}; this version reads {FORMAT_VERSION}."
        )
    name = top.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("Profile 'name' must be a non-empty string.")

    permissions = _parse_permissions(top.get("permissions", {}))
    detectors = _parse_detectors(_require_list(top.get("detectors", []), "detectors"), folder)
    detector_names = {detector.name for detector in detectors}
    meters = _parse_meters(_require_list(top.get("meters", []), "meters"))
    meter_names = {meter.name for meter in meters}
    collisions = detector_names & meter_names
    if collisions:
        joined = ", ".join(sorted(collisions))
        raise ProfileError(
            f"Detector and meter names share one observation namespace; duplicate name(s): {joined}."
        )
    skills, expectations = _parse_skills(
        _require_list(top.get("skills", []), "skills"),
        detector_names,
        meter_names,
    )
    try:
        SkillBook(skills, permissions)
    except SkillError as error:
        raise ProfileError(str(error)) from error
    rules = _parse_rules(
        _require_list(top.get("rules", []), "rules"),
        detector_names,
        meter_names,
        {skill.name for skill in skills},
    )
    try:
        planner = load_planner_config(top)
        check_goal_detector(planner.stop_when, detector_names, meter_names)
    except (TypeError, ValueError) as error:
        raise ProfileError(f"Invalid planner block: {error}") from error

    return GameProfile(
        name=name,
        directory=folder,
        permissions=permissions,
        detectors=detectors,
        skills=skills,
        rules=rules,
        planner=planner,
        expectations=MappingProxyType(expectations),
        meters=meters,
    )


def _parse_permissions(value: object) -> SkillPermissions:
    block = _require_object(value, "permissions")
    _reject_unknown(block, _PERMISSION_FIELDS, "permissions")
    keys = _require_list(block.get("allowed_keys", []), "permissions.allowed_keys")
    if any(not isinstance(key, str) for key in keys):
        raise ProfileError("permissions.allowed_keys must be a list of key names.")
    if len(set(keys)) != len(keys):
        raise ProfileError("permissions.allowed_keys contains duplicates.")
    options: dict[str, Any] = {"allowed_keys": frozenset(keys)}
    for field in ("max_hold_seconds", "max_actions_per_second"):
        if field in block:
            options[field] = block[field]
    try:
        return SkillPermissions(**options)
    except (SkillError, TypeError) as error:
        raise ProfileError(f"Invalid permissions: {error}") from error


def _parse_detectors(items: list[object], folder: Path) -> tuple[DetectorDefinition, ...]:
    detectors: list[DetectorDefinition] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"detectors[{index}]"
        block = _require_object(item, label)
        _reject_unknown(block, _DETECTOR_FIELDS, label)
        name = _require_name(block.get("name"), f"{label}.name")
        label = f"detector {name!r}"
        if name in seen:
            raise ProfileError(f"Duplicate detector name: {name}")
        seen.add(name)

        template = _check_template_path(block.get("template"), folder, label)
        threshold = _require_unit_interval(block.get("threshold", 0.82), f"{label} threshold")
        roi = _parse_roi(block.get("roi"), label)
        detectors.append(DetectorDefinition(name, template, threshold, roi))
    return tuple(detectors)



def _parse_meters(items: list[object]) -> tuple[MeterDefinition, ...]:
    meters: list[MeterDefinition] = []
    seen: set[str] = set()

    for index, item in enumerate(items):
        label = f"meters[{index}]"
        block = _require_object(item, label)
        _reject_unknown(block, _METER_FIELDS, label)

        name = _require_name(block.get("name"), f"{label}.name")
        if name in seen:
            raise ProfileError(f"Duplicate meter name: {name}")
        seen.add(name)
        meter_label = f"meter {name!r}"

        roi = _parse_roi(block.get("roi"), meter_label)
        if roi is None:
            raise ProfileError(f"{meter_label}: 'roi' is required.")

        raw_ranges = _require_list(block.get("hsv_ranges"), f"{meter_label}.hsv_ranges")
        if not raw_ranges:
            raise ProfileError(f"{meter_label}: hsv_ranges must contain at least one range.")
        hsv_ranges: list[HSVRange] = []
        for range_index, raw_range in enumerate(raw_ranges):
            range_label = f"{meter_label}.hsv_ranges[{range_index}]"
            range_block = _require_object(raw_range, range_label)
            _reject_unknown(range_block, _HSV_RANGE_FIELDS, range_label)
            lower = _parse_hsv_triplet(range_block.get("lower"), f"{range_label}.lower")
            upper = _parse_hsv_triplet(range_block.get("upper"), f"{range_label}.upper")
            try:
                hsv_ranges.append(HSVRange(lower=lower, upper=upper))
            except ValueError as error:
                raise ProfileError(f"{range_label}: {error}") from error

        direction = block.get("direction", "left_to_right")
        if not isinstance(direction, str):
            raise ProfileError(f"{meter_label}: direction must be a string.")

        min_slice_coverage = _require_number(
            block.get("min_slice_coverage", 0.50),
            f"{meter_label} min_slice_coverage",
        )
        if not 0.0 < min_slice_coverage <= 1.0:
            raise ProfileError(
                f"{meter_label} min_slice_coverage must be greater than 0.0 and at most 1.0."
            )

        max_gap_slices = block.get("max_gap_slices", 1)
        if (
            isinstance(max_gap_slices, bool)
            or not isinstance(max_gap_slices, int)
            or max_gap_slices < 0
        ):
            raise ProfileError(f"{meter_label} max_gap_slices must be a non-negative integer.")

        min_confidence = _require_unit_interval(
            block.get("min_confidence", 0.80),
            f"{meter_label} min_confidence",
        )

        try:
            meter = MeterDefinition(
                name=name,
                roi=roi,
                hsv_ranges=tuple(hsv_ranges),
                direction=direction,  # type: ignore[arg-type]
                min_slice_coverage=min_slice_coverage,
                max_gap_slices=max_gap_slices,
                min_confidence=min_confidence,
            )
            meter.spec()
        except ValueError as error:
            raise ProfileError(f"{meter_label}: {error}") from error
        meters.append(meter)

    return tuple(meters)


def _parse_hsv_triplet(value: object, label: str) -> tuple[int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(isinstance(part, bool) or not isinstance(part, int) for part in value)
    ):
        raise ProfileError(f"{label} must be [H, S, V] integers.")

    hue, saturation, brightness = value
    if not 0 <= hue <= 179:
        raise ProfileError(f"{label} hue must be between 0 and 179.")
    for channel_name, channel in (("saturation", saturation), ("value", brightness)):
        if not 0 <= channel <= 255:
            raise ProfileError(f"{label} {channel_name} must be between 0 and 255.")
    return (hue, saturation, brightness)


def _check_template_path(value: object, folder: Path, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{label}: 'template' must be a relative path such as 'templates/x.png'.")
    posix = PurePosixPath(value.replace("\\", "/"))
    if (
        posix.is_absolute()
        or PureWindowsPath(value).drive
        or PureWindowsPath(value).is_absolute()
        or ".." in posix.parts
    ):
        raise ProfileError(f"{label}: template path {value!r} must stay inside the profile folder.")
    if posix.suffix.lower() != ".png":
        raise ProfileError(f"{label}: template {value!r} must be a .png file.")
    resolved = (folder / posix).resolve()
    if not resolved.is_relative_to(folder.resolve()):
        raise ProfileError(f"{label}: template path {value!r} must stay inside the profile folder.")
    if not resolved.is_file():
        raise ProfileError(f"{label}: missing template file {folder / posix}.")
    return posix.as_posix()


def _parse_roi(value: object, label: str) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(part, bool) or not isinstance(part, int) for part in value)
    ):
        raise ProfileError(f"{label}: 'roi' must be null or [x, y, width, height] integers.")
    x, y, width, height = value
    if width <= 0 or height <= 0:
        raise ProfileError(f"{label}: roi width and height must be positive.")
    return (x, y, width, height)


def _parse_skills(
    items: list[object],
    detector_names: set[str],
    meter_names: set[str] | None = None,
) -> tuple[tuple[Skill, ...], dict[str, ExpectationLike]]:
    """The skills, plus each skill's optional `expect` parsed separately."""

    skills: list[Skill] = []
    expectations: dict[str, ExpectationLike] = {}
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"skills[{index}]"
        block = _require_object(item, label)
        skill_type = block.get("type")
        if not isinstance(skill_type, str) or skill_type not in _SKILL_FIELDS:
            raise ProfileError(
                f"{label}: 'type' must be one of {sorted(_SKILL_FIELDS)}, got {skill_type!r}."
            )
        _reject_unknown(block, _SKILL_FIELDS[skill_type], label)
        name = _require_name(block.get("name"), f"{label}.name")
        if name in seen:
            raise ProfileError(f"Duplicate skill name: {name}")
        seen.add(name)

        if "expect" in block:
            try:
                expectations[name] = parse_expectation(
                    block["expect"],
                    detector_names,
                    meter_names or set(),
                )
            except ExpectationError as error:
                raise ProfileError(f"Skill {name!r}: {error}") from error
        options = {key: block[key] for key in block if key not in ("type", "expect")}
        if skill_type == ClickSkill.TYPE:
            detector = block.get("detector")
            if not isinstance(detector, str) or detector not in detector_names:
                raise ProfileError(
                    f"Skill {name!r} references unknown detector {detector!r}."
                )
        try:
            skills.append(_SKILL_CLASSES[skill_type](**options))
        except TypeError as error:
            raise ProfileError(f"Skill {name!r}: {error}") from error
        except SkillError as error:
            raise ProfileError(str(error)) from error
    return tuple(skills), expectations


_SKILL_CLASSES: dict[str, type] = {
    ClickSkill.TYPE: ClickSkill,
    PressSkill.TYPE: PressSkill,
    HoldSkill.TYPE: HoldSkill,
}


def _parse_rules(
    items: list[object],
    detector_names: set[str],
    meter_names: set[str],
    skill_names: set[str],
) -> tuple[RuleDefinition, ...]:
    rules: list[RuleDefinition] = []
    seen: set[str] = set()

    for index, item in enumerate(items):
        label = f"rules[{index}]"
        block = _require_object(item, label)
        name = _require_name(block.get("name"), f"{label}.name")
        if name in seen:
            raise ProfileError(f"Duplicate rule name: {name}")
        seen.add(name)

        has_detector = "detector" in block
        has_meter = "meter" in block
        if not has_detector and not has_meter:
            raise ProfileError(
                f"Rule {name!r} must reference exactly one detector or meter."
            )
        if has_detector and has_meter:
            raise ProfileError(
                f"Rule {name!r} cannot reference both a detector and a meter."
            )

        skill = block.get("skill")
        if not isinstance(skill, str) or skill not in skill_names:
            raise ProfileError(f"Rule {name!r} references unknown skill {skill!r}.")
        enabled = block.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ProfileError(f"Rule {name!r}: 'enabled' must be true or false.")

        common: dict[str, float] = {}
        for field in ("max_observation_age_seconds", "cooldown_seconds"):
            if field in block:
                common[field] = _require_non_negative(
                    block[field], f"Rule {name!r} {field}"
                )

        if has_detector:
            _reject_unknown(block, _DETECTOR_RULE_FIELDS, label)
            detector = block.get("detector")
            if not isinstance(detector, str) or detector not in detector_names:
                raise ProfileError(
                    f"Rule {name!r} references unknown detector {detector!r}."
                )
            options = dict(common)
            if "min_confidence" in block:
                options["min_confidence"] = _require_unit_interval(
                    block["min_confidence"],
                    f"Rule {name!r} min_confidence",
                )
            try:
                rule: Rule = VisibilityRule(
                    name=name,
                    detector_name=detector,
                    action=SKILL_RULE_ACTION,
                    skill=skill,
                    **options,
                )
            except ValueError as error:
                raise ProfileError(f"Rule {name!r}: {error}") from error
        else:
            _reject_unknown(block, _METER_RULE_FIELDS, label)
            condition_block = {
                key: block[key]
                for key in ("meter", "below", "above", "rises", "falls", "min_confidence")
                if key in block
            }
            try:
                condition = parse_meter_condition(
                    condition_block,
                    meter_names,
                    label=f"Rule {name!r}",
                )
                rule = MeterRule(
                    name=name,
                    condition=condition,
                    skill=skill,
                    **common,
                )
            except (MeterConditionError, ValueError) as error:
                raise ProfileError(f"Rule {name!r}: {error}") from error

        rules.append(RuleDefinition(rule, enabled))

    return tuple(rules)


def read_templates(profile: GameProfile) -> dict[str, np.ndarray]:
    """Each detector's template image (BGR), keyed by detector name.

    Lets the UI register detectors from arrays and keep those arrays, so the
    profile can be saved again later.
    """

    images: dict[str, np.ndarray] = {}
    for detector in profile.detectors:
        path = profile.template_path(detector)
        try:
            data = np.fromfile(path, dtype=np.uint8)
        except OSError as error:
            raise ProfileError(f"Could not read template {path}: {error}") from error
        image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if image is None:
            raise ProfileError(f"Template {path} is not a readable image.")
        images[detector.name] = image
    return images


def _register_detectors(profile: GameProfile, registry: DetectorRegistry) -> None:
    registered: list[str] = []
    try:
        for detector in profile.detectors:
            registry.register_file(detector.spec(), profile.template_path(detector))
            registered.append(detector.name)
    except (OSError, RuntimeError, ValueError) as error:
        for name in registered:
            registry.unregister(name)
        raise ProfileError(f"Could not register detectors: {error}") from error


# --------------------------------------------------------------------------- save


def profile_slug(name: str) -> str:
    """A safe folder name for profile `name`: lowercase `[a-z0-9_-]`, max 64."""

    slug = _SLUG_INVALID.sub("_", name.strip().lower()).strip("_-")[:64].strip("_-")
    if not slug:
        raise ProfileError(f"Profile name {name!r} has no usable characters for a folder name.")
    return slug


def save_profile(
    root: str | Path,
    name: str,
    *,
    detectors: Iterable[tuple[DetectorDefinition, np.ndarray]] = (),
    meters: Iterable[MeterDefinition] = (),
    skills: Iterable[Skill] = (),
    rules: Iterable[RuleDefinition] = (),
    permissions: SkillPermissions | None = None,
    planner: PlannerConfig | None = None,
    expectations: Mapping[str, ExpectationLike] | None = None,
    overwrite: bool = False,
) -> Path:
    """Write a new profile folder under `root` and return its path.

    Each detector's template array is written to `templates/<detector>.png`
    (its `template` field is replaced by that path). `expectations` (skill
    name -> `Expectation`) is written as those skills' `expect` blocks. An
    existing profile folder is only written into when `overwrite` is True;
    files are replaced, never deleted. Everything is validated before the
    first file is written.
    """

    if not isinstance(name, str) or not name.strip():
        raise ProfileError("Profile name cannot be empty.")
    folder = Path(root) / profile_slug(name)
    if folder.exists() and not overwrite:
        raise ProfileError(f"Profile folder {folder} already exists; not overwriting it.")

    permissions = permissions if permissions is not None else SkillPermissions()
    templates: list[tuple[str, bytes]] = []
    detector_blocks: list[dict[str, object]] = []
    for definition, template_bgr in detectors:
        _require_name(definition.name, "detector name")
        relative = f"{TEMPLATES_DIRNAME}/{definition.name}.png"
        detector_blocks.append(
            {
                "name": definition.name,
                "template": relative,
                "threshold": definition.threshold,
                "roi": list(definition.roi) if definition.roi is not None else None,
            }
        )
        templates.append((relative, _encode_png(template_bgr, definition.name)))

    meter_blocks = [_meter_block(meter) for meter in meters]

    data: dict[str, object] = {
        "format_version": FORMAT_VERSION,
        "name": name.strip(),
        "permissions": {
            "allowed_keys": sorted(permissions.allowed_keys),
            "max_hold_seconds": permissions.max_hold_seconds,
            "max_actions_per_second": permissions.max_actions_per_second,
        },
        "detectors": detector_blocks,
        "meters": meter_blocks,
        "skills": [_skill_block(skill, expectations) for skill in skills],
        "rules": [_rule_block(definition) for definition in rules],
        "planner": _planner_block(planner if planner is not None else PlannerConfig()),
    }
    _validate_before_write(data, detector_blocks, meter_blocks)

    try:
        (folder / TEMPLATES_DIRNAME).mkdir(parents=True, exist_ok=True)
        for relative, png in templates:
            (folder / relative).write_bytes(png)
        (folder / PROFILE_FILENAME).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as error:
        raise ProfileError(f"Could not write profile to {folder}: {error}") from error
    return folder


def list_profiles(root: str | Path) -> list[str]:
    """Sorted folder names under `root` that contain a profile.json."""

    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(
        entry.name
        for entry in base.iterdir()
        if entry.is_dir() and (entry / PROFILE_FILENAME).is_file()
    )


def _validate_before_write(
    data: dict[str, object],
    detector_blocks: list[dict],
    meter_blocks: list[dict[str, object]],
) -> None:
    """Run the loader's checks on `data` without touching the real folder.

    Template files do not exist yet, so the detector entries are checked
    without them and the rest of the profile against their names.
    """

    for block in detector_blocks:
        _parse_roi(block["roi"], f"detector {block['name']!r}")
        _require_unit_interval(block["threshold"], f"detector {block['name']!r} threshold")
    names = [block["name"] for block in detector_blocks]
    if len(set(names)) != len(names):
        raise ProfileError("Duplicate detector names.")

    meters = _parse_meters(list(meter_blocks))
    meter_names = {meter.name for meter in meters}
    collisions = set(names) & meter_names
    if collisions:
        joined = ", ".join(sorted(collisions))
        raise ProfileError(
            f"Detector and meter names share one observation namespace; duplicate name(s): {joined}."
        )

    permissions = _parse_permissions(data["permissions"])
    skills, _ = _parse_skills(
        list(data["skills"]),  # type: ignore[arg-type]
        set(names),
        meter_names,
    )
    try:
        SkillBook(skills, permissions)
    except SkillError as error:
        raise ProfileError(str(error)) from error
    _parse_rules(
        list(data["rules"]),  # type: ignore[arg-type]
        set(names),
        meter_names,
        {skill.name for skill in skills},
    )
    try:
        check_goal_detector(
            load_planner_config(data).stop_when,
            set(names),
            meter_names,
        )
    except (TypeError, ValueError) as error:
        raise ProfileError(f"Invalid planner block: {error}") from error


def _encode_png(template_bgr: np.ndarray, name: str) -> bytes:
    if not isinstance(template_bgr, np.ndarray) or template_bgr.size == 0:
        raise ProfileError(f"Detector {name!r} template image is empty.")
    ok, encoded = cv2.imencode(".png", template_bgr)
    if not ok:
        raise ProfileError(f"Could not encode the template for detector {name!r}.")
    return encoded.tobytes()



def _meter_block(meter: MeterDefinition) -> dict[str, object]:
    _require_name(meter.name, "meter name")
    try:
        spec = meter.spec()
    except ValueError as error:
        raise ProfileError(f"Meter {meter.name!r}: {error}") from error
    _require_unit_interval(meter.min_confidence, f"Meter {meter.name!r} min_confidence")
    return {
        "name": spec.name,
        "roi": list(spec.roi),
        "hsv_ranges": [
            {"lower": list(hsv_range.lower), "upper": list(hsv_range.upper)}
            for hsv_range in spec.hsv_ranges
        ],
        "direction": spec.direction,
        "min_slice_coverage": spec.min_slice_coverage,
        "max_gap_slices": spec.max_gap_slices,
        "min_confidence": meter.min_confidence,
    }


def _skill_block(
    skill: Skill, expectations: Mapping[str, ExpectationLike] | None = None
) -> dict[str, object]:
    block: dict[str, object]
    if isinstance(skill, ClickSkill):
        block = {
            "name": skill.name,
            "type": skill.TYPE,
            "detector": skill.detector,
            "min_confidence": skill.min_confidence,
            "max_observation_age_seconds": skill.max_observation_age_seconds,
            "enabled": skill.enabled,
        }
    elif isinstance(skill, PressSkill):
        block = {"name": skill.name, "type": skill.TYPE, "key": skill.key, "enabled": skill.enabled}
    elif isinstance(skill, HoldSkill):
        block = {
            "name": skill.name,
            "type": skill.TYPE,
            "key": skill.key,
            "seconds": skill.seconds,
            "enabled": skill.enabled,
        }
    else:
        raise ProfileError(f"Unsupported skill {skill!r}.")
    expectation = (expectations or {}).get(skill.name)
    if expectation is not None:
        block["expect"] = expectation.to_block()
    return block


def _rule_block(definition: RuleDefinition) -> dict[str, object]:
    rule = definition.rule
    if isinstance(rule, MeterRule):
        block: dict[str, object] = {
            "name": rule.name,
            "skill": rule.skill,
            "max_observation_age_seconds": rule.max_observation_age_seconds,
            "cooldown_seconds": rule.cooldown_seconds,
            "enabled": definition.enabled,
        }
        block.update(rule.condition.to_block())
        return block

    if rule.action != SKILL_RULE_ACTION or rule.skill is None:
        raise ProfileError(
            f"Rule {rule.name!r} must fire a skill to be saved in a profile."
        )
    return {
        "name": rule.name,
        "detector": rule.detector_name,
        "skill": rule.skill,
        "min_confidence": rule.min_confidence,
        "max_observation_age_seconds": rule.max_observation_age_seconds,
        "cooldown_seconds": rule.cooldown_seconds,
        "enabled": definition.enabled,
    }


def _planner_block(planner: PlannerConfig) -> dict[str, object]:
    block: dict[str, object] = {
        "enabled": planner.enabled,
        "interval_seconds": planner.interval_seconds,
        "goal": planner.goal,
        "auto_max_steps": planner.auto_max_steps,
        "llm_notes": planner.llm_notes,
        "max_run_minutes": planner.max_run_minutes,
    }
    if planner.stop_when is not None:
        block["stop_when"] = planner.stop_when.to_block()
    if planner.ollama is not None:
        block.update(
            model=planner.ollama.model,
            host=planner.ollama.host,
            port=planner.ollama.port,
            timeout_seconds=planner.ollama.timeout_seconds,
        )
    return block


# --------------------------------------------------------------------------- helpers


def _require_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be a JSON object.")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProfileError(f"{label} must be a JSON list.")
    return value


def _reject_unknown(block: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = set(block) - allowed
    if unknown:
        raise ProfileError(f"{label}: unknown field(s) {sorted(unknown)!r}.")


def _require_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value):
        raise ProfileError(
            f"{label} {value!r} must be 1-64 characters from A-Z, a-z, 0-9, '_', '.', '-'."
        )
    return value


def _require_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ProfileError(f"{label} must be a finite number.")
    return float(value)


def _require_unit_interval(value: object, label: str) -> float:
    number = _require_number(value, label)
    if not 0.0 <= number <= 1.0:
        raise ProfileError(f"{label} must be between 0.0 and 1.0.")
    return number


def _require_non_negative(value: object, label: str) -> float:
    number = _require_number(value, label)
    if number < 0:
        raise ProfileError(f"{label} cannot be negative.")
    return number
