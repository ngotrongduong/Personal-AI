"""Named profile skills (v0.6) and the permissions that bound them.

A skill is the only unit of action a profile (and, from v0.7, a planner) can
request by name. Skills never send input: `SkillBook.build_intent` turns an
enabled, permitted skill into an `ActionIntent`, and `ActionDispatcher` stays
the only bridge to `InputController`, re-checking the same permissions at
dispatch time. Keys, hold times and click targets always come from the
profile's skill definitions, never from the caller.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math
import re
import threading
import time
from typing import ClassVar, TypeAlias

from .game_state import GameState
from .rule_engine import ActionIntent


# Reserved in code whatever a profile says: F8 is the emergency stop, and the
# Windows/menu keys leave the game. Combos are rejected by the key pattern.
FORBIDDEN_KEYS = frozenset({"f8", "win", "winleft", "winright", "lwin", "rwin", "apps"})
HARD_MAX_HOLD_SECONDS = 5.0
HARD_MAX_ACTIONS_PER_SECOND = 20.0
PRESS_SECONDS = 0.08
DEFAULT_MAX_HOLD_SECONDS = 1.0
DEFAULT_MAX_ACTIONS_PER_SECOND = 5.0
DEFAULT_CLICK_MIN_CONFIDENCE = 0.82
DEFAULT_CLICK_MAX_OBSERVATION_AGE_SECONDS = 0.75

# One lowercase key name (pydirectinput spelling, e.g. "x", "space", "f1") or a
# single punctuation key. No "+", spaces or uppercase, so no combos.
_KEY_PATTERN = re.compile(r"[a-z0-9]{1,16}|[`\-=\[\]\\;',./]")
_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class SkillError(ValueError):
    """A skill, key or permission is invalid or not allowed."""


def validate_key(key: object) -> str:
    """Return `key` if it is one canonical, non-reserved key name, else raise."""

    if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
        raise SkillError(
            f"Invalid key {key!r}: use one lowercase key name such as 'x' or "
            "'space' (no combos)."
        )
    if key in FORBIDDEN_KEYS:
        raise SkillError(f"Key {key!r} is reserved and can never be used by a skill.")
    return key


def validate_skill_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise SkillError(
            f"Invalid skill name {name!r}: 1-64 characters from A-Z, a-z, 0-9, '_', '.', '-'."
        )
    return name


def _require_positive_number(value: object, label: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SkillError(f"{label} must be a number.")
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise SkillError(f"{label} must be greater than 0 and at most {maximum:g}.")
    return float(value)


def _require_confidence(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SkillError(f"{label} must be a number.")
    if not 0.0 <= value <= 1.0:
        raise SkillError(f"{label} must be between 0.0 and 1.0.")


@dataclass(frozen=True, slots=True)
class SkillPermissions:
    """What a profile's key skills may do. Checked on load and at dispatch."""

    allowed_keys: frozenset[str] = frozenset()
    max_hold_seconds: float = DEFAULT_MAX_HOLD_SECONDS
    max_actions_per_second: float = DEFAULT_MAX_ACTIONS_PER_SECOND

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_keys, frozenset):
            raise SkillError("allowed_keys must be a frozenset.")
        for key in self.allowed_keys:
            validate_key(key)
        _require_positive_number(
            self.max_hold_seconds, "max_hold_seconds", HARD_MAX_HOLD_SECONDS
        )
        _require_positive_number(
            self.max_actions_per_second, "max_actions_per_second", HARD_MAX_ACTIONS_PER_SECOND
        )

    def key_denial(self, key: object) -> str | None:
        """Why `key` may not be sent, or None if it may."""

        try:
            validate_key(key)
        except SkillError as error:
            return str(error)
        if key not in self.allowed_keys:
            return f"key {key!r} is not in the profile's allowed_keys"
        return None

    def hold_denial(self, seconds: object) -> str | None:
        """Why a hold of `seconds` may not run, or None if it may."""

        try:
            _require_positive_number(seconds, "hold seconds", HARD_MAX_HOLD_SECONDS)
        except SkillError as error:
            return str(error)
        if seconds > self.max_hold_seconds:
            return (
                f"hold of {seconds:g}s exceeds the profile's max_hold_seconds "
                f"({self.max_hold_seconds:g}s)"
            )
        return None


@dataclass(frozen=True, slots=True)
class ClickSkill:
    """Click the centre of a fresh, visible detection of `detector`."""

    TYPE: ClassVar[str] = "click"

    name: str
    detector: str
    min_confidence: float = DEFAULT_CLICK_MIN_CONFIDENCE
    max_observation_age_seconds: float = DEFAULT_CLICK_MAX_OBSERVATION_AGE_SECONDS
    enabled: bool = False

    def __post_init__(self) -> None:
        validate_skill_name(self.name)
        if not isinstance(self.detector, str) or not self.detector.strip():
            raise SkillError(f"Skill {self.name!r}: detector cannot be empty.")
        _require_confidence(self.min_confidence, f"Skill {self.name!r} min_confidence")
        _require_positive_number(
            self.max_observation_age_seconds,
            f"Skill {self.name!r} max_observation_age_seconds",
            60.0,
        )
        _require_bool(self.enabled, self.name)


@dataclass(frozen=True, slots=True)
class PressSkill:
    """Tap one key for `PRESS_SECONDS`."""

    TYPE: ClassVar[str] = "press"

    name: str
    key: str
    enabled: bool = False

    def __post_init__(self) -> None:
        validate_skill_name(self.name)
        validate_key(self.key)
        _require_bool(self.enabled, self.name)


@dataclass(frozen=True, slots=True)
class HoldSkill:
    """Hold one key down for `seconds`, then release it."""

    TYPE: ClassVar[str] = "hold"

    name: str
    key: str
    seconds: float
    enabled: bool = False

    def __post_init__(self) -> None:
        validate_skill_name(self.name)
        validate_key(self.key)
        _require_positive_number(
            self.seconds, f"Skill {self.name!r} seconds", HARD_MAX_HOLD_SECONDS
        )
        _require_bool(self.enabled, self.name)


def _require_bool(value: object, skill_name: str) -> None:
    if not isinstance(value, bool):
        raise SkillError(f"Skill {skill_name!r}: enabled must be true or false.")


Skill: TypeAlias = ClickSkill | PressSkill | HoldSkill


def permission_denial(skill: Skill, permissions: SkillPermissions) -> str | None:
    """Why the profile's permissions forbid `skill`, or None if they allow it."""

    if isinstance(skill, PressSkill):
        return permissions.key_denial(skill.key)
    if isinstance(skill, HoldSkill):
        return permissions.key_denial(skill.key) or permissions.hold_denial(skill.seconds)
    return None


@dataclass(frozen=True, slots=True)
class SkillIntentResult:
    skill_name: str
    intent: ActionIntent | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.intent is not None


class SkillBook:
    """
    The loaded profile's skills, their permissions, and which are enabled.

    Skills start with their profile `enabled` flag (default off) and can be
    toggled at runtime. A disabled or unpermitted skill never yields an intent.
    """

    def __init__(
        self,
        skills: Iterable[Skill] = (),
        permissions: SkillPermissions | None = None,
    ) -> None:
        self._permissions = permissions if permissions is not None else SkillPermissions()
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            if skill.name in self._skills:
                raise SkillError(f"Duplicate skill name: {skill.name}")
            denial = permission_denial(skill, self._permissions)
            if denial is not None:
                raise SkillError(f"Skill {skill.name!r} is not permitted: {denial}")
            self._skills[skill.name] = skill
        self._enabled = {skill.name for skill in self._skills.values() if skill.enabled}
        self._lock = threading.RLock()

    @property
    def permissions(self) -> SkillPermissions:
        return self._permissions

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            self._require_known(name)
            return name in self._enabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            self._require_known(name)
            if enabled:
                self._enabled.add(name)
            else:
                self._enabled.discard(name)

    def build_intent(
        self,
        name: str,
        state: GameState,
        *,
        source: str,
        now: float | None = None,
        reason: str | None = None,
    ) -> SkillIntentResult:
        """Turn skill `name` into an intent, or explain why it cannot run now.

        `source` names who asked (a rule name, "manual", later the planner) and
        becomes the intent's `rule_name` for logging.
        """

        if not source.strip():
            raise ValueError("source cannot be empty.")
        current = time.monotonic() if now is None else now
        skill = self._skills.get(name)
        if skill is None:
            return SkillIntentResult(name, None, f"unknown skill {name!r}")
        with self._lock:
            enabled = name in self._enabled
        if not enabled:
            return SkillIntentResult(name, None, f"skill {name!r} is disabled")
        denial = permission_denial(skill, self._permissions)
        if denial is not None:
            return SkillIntentResult(name, None, f"skill {name!r} not permitted: {denial}")

        if isinstance(skill, ClickSkill):
            return _click_intent(skill, state, source, current, reason)
        if isinstance(skill, PressSkill):
            intent = ActionIntent(
                rule_name=source,
                action=PressSkill.TYPE,
                detector_name="",
                confidence=0.0,
                target_bbox=None,
                created_at=current,
                reason=reason or f"skill {name}: press {skill.key}",
                skill_name=name,
                key=skill.key,
            )
            return SkillIntentResult(name, intent, "ok")
        intent = ActionIntent(
            rule_name=source,
            action=HoldSkill.TYPE,
            detector_name="",
            confidence=0.0,
            target_bbox=None,
            created_at=current,
            reason=reason or f"skill {name}: hold {skill.key} for {skill.seconds:g}s",
            skill_name=name,
            key=skill.key,
            hold_seconds=skill.seconds,
        )
        return SkillIntentResult(name, intent, "ok")

    def _require_known(self, name: str) -> None:
        if name not in self._skills:
            raise SkillError(f"Unknown skill name: {name}")


def _click_intent(
    skill: ClickSkill,
    state: GameState,
    source: str,
    now: float,
    reason: str | None,
) -> SkillIntentResult:
    observation = state.get(skill.detector)
    if observation is None:
        return SkillIntentResult(skill.name, None, f"detector {skill.detector!r} not observed")
    if not observation.visible:
        return SkillIntentResult(skill.name, None, f"detector {skill.detector!r} not visible")
    if observation.confidence < skill.min_confidence:
        return SkillIntentResult(
            skill.name,
            None,
            f"detector {skill.detector!r} confidence {observation.confidence:.3f} "
            f"below {skill.min_confidence:.3f}",
        )
    if observation.is_stale(skill.max_observation_age_seconds, now=now):
        return SkillIntentResult(
            skill.name, None, f"detector {skill.detector!r} observation is stale"
        )
    if observation.bbox is None:
        return SkillIntentResult(skill.name, None, f"detector {skill.detector!r} has no bbox")
    intent = ActionIntent(
        rule_name=source,
        action=ClickSkill.TYPE,
        detector_name=skill.detector,
        confidence=observation.confidence,
        target_bbox=observation.bbox,
        created_at=now,
        reason=reason
        or (
            f"skill {skill.name}: {skill.detector} visible with confidence "
            f"{observation.confidence:.3f}"
        ),
        skill_name=skill.name,
    )
    return SkillIntentResult(skill.name, intent, "ok")
