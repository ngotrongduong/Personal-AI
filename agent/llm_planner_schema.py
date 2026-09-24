"""Closed directive vocabulary accepted from the local LLM planner."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Collection, TypeAlias


@dataclass(frozen=True, slots=True)
class EnableRuleDirective:
    """Request that a known deterministic rule be enabled."""

    rule_name: str


@dataclass(frozen=True, slots=True)
class DisableRuleDirective:
    """Request that a known deterministic rule be disabled."""

    rule_name: str


@dataclass(frozen=True, slots=True)
class NoopDirective:
    """Explicitly request no rule-configuration change this planner cycle."""


@dataclass(frozen=True, slots=True)
class RunSkillDirective:
    """Propose running one enabled profile skill, by name only (v0.7).

    There is deliberately no field for a key, coordinate, duration or
    detector: those always come from the profile's skill definition.
    ``reason`` is display and prompt text only.
    """

    skill_name: str
    reason: str


PlannerDirective: TypeAlias = (
    EnableRuleDirective | DisableRuleDirective | NoopDirective | RunSkillDirective
)

MAX_REASON_LENGTH = 200


class DirectiveValidationError(ValueError):
    """The raw planner response was not in the reviewed directive vocabulary."""


def parse_directive(
    raw: str,
    known_rule_names: Collection[str],
    runnable_skill_names: Collection[str] = (),
) -> PlannerDirective:
    """Parse one JSON directive and reject every shape outside the closed schema.

    ``run_skill`` is accepted only for a name in ``runnable_skill_names`` (the
    loaded profile's currently enabled skills); with the default empty
    collection every ``run_skill`` is rejected.
    """

    if not isinstance(raw, str):
        raise DirectiveValidationError("Planner directive must be a JSON string.")

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DirectiveValidationError(f"Planner directive is not valid JSON: {error.msg}.") from error

    if not isinstance(value, dict):
        raise DirectiveValidationError("Planner directive JSON must be an object.")

    directive_type = value.get("type")
    if directive_type == "noop":
        _require_exact_fields(value, {"type"})
        return NoopDirective()
    if directive_type == "enable_rule":
        return EnableRuleDirective(_validated_rule_name(value, known_rule_names))
    if directive_type == "disable_rule":
        return DisableRuleDirective(_validated_rule_name(value, known_rule_names))
    if directive_type == "run_skill":
        _require_exact_fields(value, {"type", "skill", "reason"})
        return RunSkillDirective(
            _validated_skill_name(value["skill"], runnable_skill_names),
            _validated_reason(value["reason"]),
        )

    if not isinstance(directive_type, str):
        raise DirectiveValidationError("Planner directive 'type' must be a string.")
    raise DirectiveValidationError(f"Unknown planner directive type: {directive_type!r}.")


def _validated_rule_name(value: dict[object, object], known_rule_names: Collection[str]) -> str:
    _require_exact_fields(value, {"type", "rule_name"})
    rule_name = value["rule_name"]
    if not isinstance(rule_name, str) or not rule_name:
        raise DirectiveValidationError("Planner directive 'rule_name' must be a non-empty string.")
    if rule_name not in known_rule_names:
        raise DirectiveValidationError(f"Planner directive references unknown rule: {rule_name!r}.")
    return rule_name


def _validated_skill_name(skill: object, runnable_skill_names: Collection[str]) -> str:
    if not isinstance(skill, str) or not skill:
        raise DirectiveValidationError("Planner directive 'skill' must be a non-empty string.")
    if skill not in runnable_skill_names:
        raise DirectiveValidationError(
            f"Planner directive references a skill that is unknown or disabled: {skill!r}."
        )
    return skill


def _validated_reason(reason: object) -> str:
    if not isinstance(reason, str):
        raise DirectiveValidationError("Planner directive 'reason' must be a string.")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in reason).strip()
    if not cleaned:
        raise DirectiveValidationError("Planner directive 'reason' cannot be empty.")
    if len(cleaned) > MAX_REASON_LENGTH:
        raise DirectiveValidationError(
            f"Planner directive 'reason' is longer than {MAX_REASON_LENGTH} characters."
        )
    return cleaned


def _require_exact_fields(value: dict[object, object], expected: set[str]) -> None:
    fields = set(value)
    if fields != expected:
        raise DirectiveValidationError(
            f"Planner directive fields must be exactly {sorted(expected)!r}, got {sorted(fields)!r}."
        )
