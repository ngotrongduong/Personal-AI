"""Event and session schema for demonstration recordings.

Every event carries ``t``: seconds since the session's monotonic ``t0``.
``event_to_dict``/``session_to_dict`` produce JSON-native dicts;
``event_from_dict``/``session_from_dict`` are strict and raise ``SchemaError``
on anything unexpected (unknown type, missing or extra field, wrong type).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
import math
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from agent.game_state import Observation


FORMAT_VERSION = 1

KEY_ACTIONS = ("down", "up")
FOCUS_ACTIONS = ("gained", "lost")
MARKER_ACTIONS = ("start", "stop")


class SchemaError(ValueError):
    """Raised when an event or session record does not match the schema."""


def _require_str(value: object, name: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{name} must be a string, got {type(value).__name__}.")
    if non_empty and not value.strip():
        raise SchemaError(f"{name} cannot be empty.")
    return value


def _require_optional_str(value: object, name: str) -> str | None:
    return None if value is None else _require_str(value, name)


def _require_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{name} must be an integer, got {type(value).__name__}.")
    if minimum is not None and value < minimum:
        raise SchemaError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{name} must be a boolean, got {type(value).__name__}.")
    return value


def _require_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{name} must be a number, got {type(value).__name__}.")
    number = float(value)
    if not math.isfinite(number):
        raise SchemaError(f"{name} must be finite, got {value}.")
    return number


def _require_choice(value: object, name: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise SchemaError(f"{name} must be one of {choices}, got {value!r}.")
    return value  # type: ignore[return-value]


def _require_iso_timestamp(value: object, name: str) -> str:
    text = _require_str(value, name, non_empty=True)
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(f"{name} must be an ISO-8601 timestamp, got {text!r}.") from exc
    return text


def _set(obj: object, name: str, value: object) -> None:
    # Frozen dataclasses normalise their own fields (e.g. int -> float).
    object.__setattr__(obj, name, value)


def _init_t(obj: object) -> None:
    t = _require_float(getattr(obj, "t"), "t")
    if t < 0.0:
        raise SchemaError(f"t must be >= 0, got {t}.")
    _set(obj, "t", t)


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """A serialisable copy of ``agent.game_state.Observation``."""

    name: str
    visible: bool
    confidence: float
    bbox: tuple[int, int, int, int] | None
    value: float | int | str | bool | None
    source: str
    observed_t: float

    def __post_init__(self) -> None:
        _require_str(self.name, "name", non_empty=True)
        _require_bool(self.visible, "visible")
        confidence = _require_float(self.confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise SchemaError(f"confidence must be between 0.0 and 1.0, got {confidence}.")
        _set(self, "confidence", confidence)
        if self.bbox is not None:
            if not isinstance(self.bbox, (tuple, list)) or len(self.bbox) != 4:
                raise SchemaError("bbox must be null or a list of 4 integers.")
            _set(self, "bbox", tuple(_require_int(v, "bbox item") for v in self.bbox))
        value = self.value
        if value is not None and not isinstance(value, (bool, int, str)):
            _require_float(value, "value")
        _require_str(self.source, "source")
        _set(self, "observed_t", _require_float(self.observed_t, "observed_t"))


@dataclass(frozen=True, slots=True)
class FrameEvent:
    t: float
    index: int
    file: str

    def __post_init__(self) -> None:
        _init_t(self)
        _require_int(self.index, "index", minimum=1)
        _require_str(self.file, "file", non_empty=True)


@dataclass(frozen=True, slots=True)
class StateEvent:
    t: float
    observations: tuple[ObservationRecord, ...]

    def __post_init__(self) -> None:
        _init_t(self)
        if not isinstance(self.observations, (tuple, list)):
            raise SchemaError("observations must be a list.")
        for record in self.observations:
            if not isinstance(record, ObservationRecord):
                raise SchemaError("observations must contain ObservationRecord items.")
        _set(self, "observations", tuple(self.observations))


@dataclass(frozen=True, slots=True)
class KeyEvent:
    t: float
    key: str
    action: str

    def __post_init__(self) -> None:
        _init_t(self)
        _require_str(self.key, "key", non_empty=True)
        _require_choice(self.action, "action", KEY_ACTIONS)


@dataclass(frozen=True, slots=True)
class MouseButtonEvent:
    t: float
    button: str
    action: str
    x: int
    y: int

    def __post_init__(self) -> None:
        _init_t(self)
        _require_str(self.button, "button", non_empty=True)
        _require_choice(self.action, "action", KEY_ACTIONS)
        _require_int(self.x, "x")
        _require_int(self.y, "y")


@dataclass(frozen=True, slots=True)
class MouseMoveEvent:
    t: float
    x: int
    y: int

    def __post_init__(self) -> None:
        _init_t(self)
        _require_int(self.x, "x")
        _require_int(self.y, "y")


@dataclass(frozen=True, slots=True)
class ScrollEvent:
    t: float
    dx: int
    dy: int
    x: int
    y: int

    def __post_init__(self) -> None:
        _init_t(self)
        for name in ("dx", "dy", "x", "y"):
            _require_int(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class FocusEvent:
    t: float
    action: str

    def __post_init__(self) -> None:
        _init_t(self)
        _require_choice(self.action, "action", FOCUS_ACTIONS)


@dataclass(frozen=True, slots=True)
class MarkerEvent:
    t: float
    action: str
    reason: str = ""

    def __post_init__(self) -> None:
        _init_t(self)
        _require_choice(self.action, "action", MARKER_ACTIONS)
        _require_str(self.reason, "reason")


Event: TypeAlias = (
    FrameEvent
    | StateEvent
    | KeyEvent
    | MouseButtonEvent
    | MouseMoveEvent
    | ScrollEvent
    | FocusEvent
    | MarkerEvent
)

EVENT_TYPES: dict[str, type] = {
    "frame": FrameEvent,
    "state": StateEvent,
    "key": KeyEvent,
    "mouse_button": MouseButtonEvent,
    "mouse_move": MouseMoveEvent,
    "scroll": ScrollEvent,
    "focus": FocusEvent,
    "marker": MarkerEvent,
}
_TYPE_NAMES: dict[type, str] = {cls: name for name, cls in EVENT_TYPES.items()}


def _check_keys(data: object, expected: set[str], what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError(f"{what} must be an object, got {type(data).__name__}.")
    keys = set(data)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected, key=str)
    if missing or extra:
        raise SchemaError(f"{what} fields mismatch: missing={missing}, extra={extra}.")
    return data


def observation_to_record(observation: Observation, t0: float) -> ObservationRecord:
    value = observation.value
    if isinstance(value, float) and not math.isfinite(value):
        # JSON has no NaN/Infinity; a non-finite sensor value is recorded as unknown.
        value = None
    return ObservationRecord(
        name=observation.name,
        visible=observation.visible,
        confidence=observation.confidence,
        bbox=None if observation.bbox is None else tuple(observation.bbox),
        value=value,
        source=observation.source,
        observed_t=observation.observed_at - t0,
    )


def _record_to_dict(record: ObservationRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "visible": record.visible,
        "confidence": record.confidence,
        "bbox": None if record.bbox is None else list(record.bbox),
        "value": record.value,
        "source": record.source,
        "observed_t": record.observed_t,
    }


_RECORD_FIELDS = {f.name for f in fields(ObservationRecord)}


def _record_from_dict(data: object) -> ObservationRecord:
    checked = _check_keys(data, _RECORD_FIELDS, "observation")
    bbox = checked["bbox"]
    if bbox is not None and not isinstance(bbox, list):
        raise SchemaError("bbox must be null or a list of 4 integers.")
    return ObservationRecord(**checked)


def event_to_dict(event: Event) -> dict[str, Any]:
    type_name = _TYPE_NAMES.get(type(event))
    if type_name is None:
        raise SchemaError(f"Unsupported event type: {type(event).__name__}.")
    data: dict[str, Any] = {"type": type_name}
    for field in fields(event):
        data[field.name] = getattr(event, field.name)
    if isinstance(event, StateEvent):
        data["observations"] = [_record_to_dict(record) for record in event.observations]
    return data


def event_from_dict(data: object) -> Event:
    if not isinstance(data, dict):
        raise SchemaError(f"event must be an object, got {type(data).__name__}.")
    type_name = data.get("type")
    cls = EVENT_TYPES.get(type_name) if isinstance(type_name, str) else None
    if cls is None:
        raise SchemaError(f"Unknown event type: {type_name!r}.")
    expected = {"type"} | {f.name for f in fields(cls)}
    checked = dict(_check_keys(data, expected, f"{type_name} event"))
    del checked["type"]
    if cls is StateEvent:
        observations = checked["observations"]
        if not isinstance(observations, list):
            raise SchemaError("observations must be a list.")
        checked["observations"] = tuple(_record_from_dict(item) for item in observations)
    return cls(**checked)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Contents of a session's ``session.json``."""

    format_version: int
    app_version: str
    window_title: str
    client_width: int
    client_height: int
    record_fps: float
    started_at: str
    ended_at: str | None
    frames: int
    dropped_frames: int
    events: int
    stop_reason: str | None

    def __post_init__(self) -> None:
        version = _require_int(self.format_version, "format_version")
        if version != FORMAT_VERSION:
            raise SchemaError(f"Unsupported format_version {version}; expected {FORMAT_VERSION}.")
        _require_str(self.app_version, "app_version")
        _require_str(self.window_title, "window_title")
        _require_int(self.client_width, "client_width", minimum=1)
        _require_int(self.client_height, "client_height", minimum=1)
        fps = _require_float(self.record_fps, "record_fps")
        if fps <= 0.0:
            raise SchemaError(f"record_fps must be positive, got {fps}.")
        _set(self, "record_fps", fps)
        _require_iso_timestamp(self.started_at, "started_at")
        if self.ended_at is not None:
            _require_iso_timestamp(self.ended_at, "ended_at")
        for name in ("frames", "dropped_frames", "events"):
            _require_int(getattr(self, name), name, minimum=0)
        _require_optional_str(self.stop_reason, "stop_reason")


_SESSION_FIELDS = {f.name for f in fields(SessionInfo)}


def session_to_dict(info: SessionInfo) -> dict[str, Any]:
    if not isinstance(info, SessionInfo):
        raise SchemaError(f"Expected SessionInfo, got {type(info).__name__}.")
    return {f.name: getattr(info, f.name) for f in fields(info)}


def session_from_dict(data: object) -> SessionInfo:
    return SessionInfo(**_check_keys(data, _SESSION_FIELDS, "session"))
