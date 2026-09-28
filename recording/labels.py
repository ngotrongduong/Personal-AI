"""Pure, read-only labeling of recorded input with profile skills.

This module deliberately duck-types skills by their ``TYPE`` attribute.  It
never imports the skill or input path, and labeling never sends input or edits
a recording/profile.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from agent.meter_conditions import METER_SOURCE
from recording.schema import KeyEvent, MouseButtonEvent, StateEvent


@dataclass(frozen=True, slots=True)
class Label:
    t: float
    line: int | None
    skill: str | None
    kind: str
    input: str
    x: float | None
    y: float | None
    detail: str


def label_to_dict(label: Label) -> dict[str, object]:
    return {
        "t": label.t,
        "line": label.line,
        "skill": label.skill,
        "kind": label.kind,
        "input": label.input,
        "x": label.x,
        "y": label.y,
        "detail": label.detail,
    }


@dataclass(frozen=True, slots=True)
class LabelResult:
    labels: tuple[Label, ...]
    counts: dict[str, int]
    total_inputs: int


@dataclass(frozen=True, slots=True)
class _TimedEvent:
    event: Any
    line: int | None


def label_events(
    events: object,
    skills: object,
    *,
    client_width: int,
    client_height: int,
    radius: float = 0.03,
    max_state_age: float = 1.0,
) -> LabelResult:
    """Label input events using profile-order skill matching.

    ``events`` may contain schema events directly or dataset ``EventLine``
    objects. Events are processed in stable timestamp order because the
    recorder's sampler and input streams can be slightly interleaved on disk.
    """

    _validate_options(client_width, client_height, radius, max_state_age)
    ordered = sorted((_unwrap(item) for item in events), key=lambda item: item.event.t)  # type: ignore[union-attr]
    profile_skills = tuple(skills)  # type: ignore[arg-type]
    durations, repeats = _key_durations(ordered)

    labels: list[Label] = []
    latest_state: StateEvent | None = None
    for index, item in enumerate(ordered):
        event = item.event
        if isinstance(event, StateEvent):
            latest_state = event
        elif isinstance(event, MouseButtonEvent) and event.action == "down":
            labels.append(
                _label_mouse(
                    event,
                    item.line,
                    profile_skills,
                    latest_state,
                    client_width,
                    client_height,
                    radius,
                    max_state_age,
                )
            )
        elif (
            isinstance(event, KeyEvent)
            and event.action == "down"
            and index not in repeats
        ):
            labels.append(
                _label_key(event, item.line, profile_skills, durations.get(index))
            )

    matched = {label.skill for label in labels if label.skill is not None}
    counts = {
        skill.name: sum(label.skill == skill.name for label in labels)
        for skill in profile_skills
        if getattr(skill, "name", None) in matched
    }
    counts["unlabeled"] = sum(label.skill is None for label in labels)
    return LabelResult(tuple(labels), counts, len(labels))


def _validate_options(width: object, height: object, radius: object, age: object) -> None:
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise ValueError("client_width must be a positive integer.")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise ValueError("client_height must be a positive integer.")
    if (
        isinstance(radius, bool)
        or not isinstance(radius, int | float)
        or not math.isfinite(radius)
        or not 0.0 < radius <= 0.5
    ):
        raise ValueError("radius must be greater than 0 and at most 0.5.")
    if (
        isinstance(age, bool)
        or not isinstance(age, int | float)
        or not math.isfinite(age)
        or age <= 0.0
    ):
        raise ValueError("max_state_age must be greater than 0.")


def _unwrap(item: Any) -> _TimedEvent:
    event = getattr(item, "event", None)
    if event is None:
        return _TimedEvent(item, None)
    line = getattr(item, "line", None)
    return _TimedEvent(event, line if isinstance(line, int) and not isinstance(line, bool) else None)


def _key_durations(
    ordered: list[_TimedEvent],
) -> tuple[dict[int, float], set[int]]:
    held: dict[str, int] = {}
    durations: dict[int, float] = {}
    repeats: set[int] = set()
    for index, item in enumerate(ordered):
        event = item.event
        if not isinstance(event, KeyEvent):
            continue
        if event.action == "down":
            if event.key in held:
                repeats.add(index)
            else:
                held[event.key] = index
        else:
            start = held.pop(event.key, None)
            if start is not None:
                durations[start] = event.t - ordered[start].event.t
    return durations, repeats


def _label_mouse(
    event: MouseButtonEvent,
    line: int | None,
    skills: tuple[Any, ...],
    state: StateEvent | None,
    width: int,
    height: int,
    radius: float,
    max_state_age: float,
) -> Label:
    normalized_x = round(min(1.0, max(0.0, event.x / width)), 4)
    normalized_y = round(min(1.0, max(0.0, event.y / height)), 4)
    input_name = f"mouse:{event.button}"
    if event.button != "left":
        return Label(
            event.t, line, None, "unlabeled", input_name,
            normalized_x, normalized_y, "unsupported mouse button",
        )

    click = _matching_click(event, skills, state, max_state_age)
    if click is not None:
        skill, observation = click
        return Label(
            event.t,
            line,
            skill.name,
            "click",
            input_name,
            normalized_x,
            normalized_y,
            f"inside {skill.detector} (conf {observation.confidence:.2f})",
        )

    tap = _matching_tap(event, skills, width, height, radius)
    if tap is not None:
        skill, distance = tap
        return Label(
            event.t,
            line,
            skill.name,
            "tap",
            input_name,
            normalized_x,
            normalized_y,
            f"near {skill.name} (distance {distance:.1f}px)",
        )
    return Label(
        event.t, line, None, "unlabeled", input_name,
        normalized_x, normalized_y, "no matching click or tap skill",
    )


def _matching_click(
    event: MouseButtonEvent,
    skills: tuple[Any, ...],
    state: StateEvent | None,
    max_state_age: float,
) -> tuple[Any, Any] | None:
    if state is None:
        return None
    observations = {observation.name: observation for observation in state.observations}
    candidates: list[tuple[int, int, Any, Any]] = []
    for order, skill in enumerate(skills):
        if getattr(skill, "TYPE", None) != "click":
            continue
        observation = observations.get(getattr(skill, "detector", None))
        if (
            observation is None
            or not observation.visible
            or observation.confidence < skill.min_confidence
            or observation.source == METER_SOURCE
            or observation.bbox is None
            or abs(event.t - observation.observed_t) > max_state_age
        ):
            continue
        x, y, width, height = observation.bbox
        if x <= event.x < x + width and y <= event.y < y + height:
            candidates.append((width * height, order, skill, observation))
    if not candidates:
        return None
    _, _, skill, observation = min(candidates, key=lambda candidate: candidate[:2])
    return skill, observation


def _matching_tap(
    event: MouseButtonEvent,
    skills: tuple[Any, ...],
    width: int,
    height: int,
    radius: float,
) -> tuple[Any, float] | None:
    limit = radius * math.hypot(width, height)
    candidates: list[tuple[float, int, Any]] = []
    for order, skill in enumerate(skills):
        if getattr(skill, "TYPE", None) != "tap":
            continue
        fx, fy = skill.at
        distance = math.hypot(event.x - fx * width, event.y - fy * height)
        if distance <= limit:
            candidates.append((distance, order, skill))
    if not candidates:
        return None
    distance, _, skill = min(candidates, key=lambda candidate: candidate[:2])
    return skill, distance


def _label_key(
    event: KeyEvent,
    line: int | None,
    skills: tuple[Any, ...],
    duration: float | None,
) -> Label:
    holds = [
        skill
        for skill in skills
        if getattr(skill, "TYPE", None) == "hold" and getattr(skill, "key", None) == event.key
    ]
    presses = [
        skill
        for skill in skills
        if getattr(skill, "TYPE", None) == "press" and getattr(skill, "key", None) == event.key
    ]
    qualifying = next(
        (skill for skill in holds if duration is not None and duration >= 0.5 * skill.seconds),
        None,
    )
    skill = qualifying or (presses[0] if presses else None) or (holds[0] if holds else None)
    kind = getattr(skill, "TYPE", "unlabeled")
    if duration is None:
        detail = "key was not released"
    else:
        detail = f"held {duration:.3f}s"
    return Label(
        event.t,
        line,
        getattr(skill, "name", None),
        kind,
        f"key:{event.key}",
        None,
        None,
        detail if skill is not None else f"{detail}; no matching key skill",
    )
