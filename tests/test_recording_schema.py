from __future__ import annotations

import json
import math
import unittest

from agent.game_state import Observation
from recording.schema import (
    FORMAT_VERSION,
    FocusEvent,
    FrameEvent,
    KeyEvent,
    MarkerEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    ObservationRecord,
    ScrollEvent,
    SchemaError,
    SessionInfo,
    StateEvent,
    event_from_dict,
    event_to_dict,
    observation_to_record,
    session_from_dict,
    session_to_dict,
)


def _record(**overrides: object) -> ObservationRecord:
    values: dict[str, object] = dict(
        name="collect_button",
        visible=True,
        confidence=0.9,
        bbox=(10, 20, 30, 40),
        value=None,
        source="vision",
        observed_t=0.5,
    )
    values.update(overrides)
    return ObservationRecord(**values)  # type: ignore[arg-type]


def _session(**overrides: object) -> SessionInfo:
    values: dict[str, object] = dict(
        format_version=FORMAT_VERSION,
        app_version="0.5.0",
        window_title="Notepad",
        client_width=800,
        client_height=600,
        record_fps=10.0,
        started_at="2026-09-24T10:00:00+07:00",
        ended_at=None,
        frames=0,
        dropped_frames=0,
        events=0,
        stop_reason=None,
    )
    values.update(overrides)
    return SessionInfo(**values)  # type: ignore[arg-type]


ALL_EVENTS = (
    FrameEvent(t=0.1, index=1, file="frames/000001.jpg"),
    StateEvent(t=0.1, observations=(_record(), _record(name="hp", bbox=None, value=42))),
    KeyEvent(t=0.2, key="w", action="down"),
    MouseButtonEvent(t=0.3, button="left", action="up", x=5, y=6),
    MouseMoveEvent(t=0.4, x=-1, y=7),
    ScrollEvent(t=0.5, dx=0, dy=-1, x=3, y=4),
    FocusEvent(t=0.6, action="lost"),
    MarkerEvent(t=0.0, action="start"),
    MarkerEvent(t=9.0, action="stop", reason="user"),
)


class EventRoundTripTests(unittest.TestCase):
    def test_every_event_type_round_trips_through_json(self) -> None:
        for event in ALL_EVENTS:
            with self.subTest(event=type(event).__name__):
                encoded = json.dumps(event_to_dict(event), ensure_ascii=False)
                self.assertEqual(event_from_dict(json.loads(encoded)), event)

    def test_to_dict_adds_type_and_uses_json_lists(self) -> None:
        data = event_to_dict(ALL_EVENTS[1])

        self.assertEqual(data["type"], "state")
        self.assertEqual(data["observations"][0]["bbox"], [10, 20, 30, 40])
        self.assertEqual(event_to_dict(ALL_EVENTS[2])["type"], "key")
        self.assertEqual(event_to_dict(ALL_EVENTS[3])["type"], "mouse_button")

    def test_integer_t_is_normalised_to_float(self) -> None:
        event = event_from_dict({"type": "focus", "t": 2, "action": "gained"})

        self.assertIsInstance(event.t, float)
        self.assertEqual(event.t, 2.0)

    def test_non_ascii_key_survives(self) -> None:
        event = KeyEvent(t=1.0, key="ư", action="down")

        encoded = json.dumps(event_to_dict(event), ensure_ascii=False)

        self.assertIn("ư", encoded)
        self.assertEqual(event_from_dict(json.loads(encoded)), event)


class StrictDecodingTests(unittest.TestCase):
    def _assert_rejected(self, data: object) -> None:
        with self.assertRaises(SchemaError):
            event_from_dict(data)

    def test_rejects_non_object_and_unknown_type(self) -> None:
        self._assert_rejected([1, 2])
        self._assert_rejected({"type": "teleport", "t": 0.0})
        self._assert_rejected({"t": 0.0, "action": "gained"})

    def test_rejects_missing_and_extra_fields(self) -> None:
        self._assert_rejected({"type": "key", "t": 0.0, "key": "a"})
        self._assert_rejected({"type": "key", "t": 0.0, "key": "a", "action": "down", "x": 1})
        self._assert_rejected({"type": "marker", "t": 0.0, "action": "start"})

    def test_rejects_wrong_types(self) -> None:
        self._assert_rejected({"type": "mouse_move", "t": 0.0, "x": True, "y": 1})
        self._assert_rejected({"type": "mouse_move", "t": 0.0, "x": 1.5, "y": 1})
        self._assert_rejected({"type": "frame", "t": "0", "index": 1, "file": "f.jpg"})
        self._assert_rejected({"type": "frame", "t": True, "index": 1, "file": "f.jpg"})
        self._assert_rejected({"type": "key", "t": 0.0, "key": 5, "action": "down"})

    def test_rejects_invalid_values(self) -> None:
        self._assert_rejected({"type": "key", "t": 0.0, "key": "a", "action": "hold"})
        self._assert_rejected({"type": "key", "t": 0.0, "key": " ", "action": "down"})
        self._assert_rejected({"type": "focus", "t": 0.0, "action": "blurred"})
        self._assert_rejected({"type": "frame", "t": 0.0, "index": 0, "file": "f.jpg"})

    def test_rejects_negative_and_non_finite_t(self) -> None:
        self._assert_rejected({"type": "focus", "t": -0.001, "action": "gained"})
        self._assert_rejected({"type": "focus", "t": math.nan, "action": "gained"})
        self._assert_rejected({"type": "focus", "t": math.inf, "action": "gained"})

    def test_rejects_malformed_observations(self) -> None:
        good = event_to_dict(ALL_EVENTS[1])

        not_list = dict(good, observations={"a": 1})
        self._assert_rejected(not_list)

        record = dict(good["observations"][0])
        for bad in (
            dict(record, bbox=[1, 2, 3]),
            dict(record, bbox="1,2,3,4"),
            dict(record, confidence=1.5),
            dict(record, visible=1),
            dict(record, value=[1]),
            dict(record, extra=True),
        ):
            with self.subTest(record=bad):
                self._assert_rejected(dict(good, observations=[bad]))

    def test_constructor_validates_too(self) -> None:
        with self.assertRaises(SchemaError):
            KeyEvent(t=-1.0, key="a", action="down")
        with self.assertRaises(SchemaError):
            StateEvent(t=0.0, observations=("not a record",))  # type: ignore[arg-type]
        with self.assertRaises(SchemaError):
            _record(value=math.inf)
        with self.assertRaises(SchemaError):
            event_to_dict(object())  # type: ignore[arg-type]


class ObservationRecordTests(unittest.TestCase):
    def test_observation_to_record_uses_relative_time(self) -> None:
        observation = Observation(
            name="enemy",
            visible=True,
            confidence=0.75,
            bbox=(1, 2, 3, 4),
            value="boss",
            observed_at=105.25,
            source="template",
        )

        record = observation_to_record(observation, t0=100.0)

        self.assertEqual(record, _record(
            name="enemy",
            confidence=0.75,
            bbox=(1, 2, 3, 4),
            value="boss",
            source="template",
            observed_t=5.25,
        ))

    def test_observation_before_start_has_negative_observed_t(self) -> None:
        observation = Observation(name="hp", visible=False, observed_at=99.0)

        record = observation_to_record(observation, t0=100.0)

        self.assertEqual(record.observed_t, -1.0)
        self.assertIsNone(record.bbox)

    def test_non_finite_value_is_recorded_as_null(self) -> None:
        observation = Observation(name="hp", visible=True, value=math.nan, observed_at=1.0)

        record = observation_to_record(observation, t0=0.0)

        self.assertIsNone(record.value)
        json.dumps(event_to_dict(StateEvent(t=0.0, observations=(record,))), allow_nan=False)


class SessionInfoTests(unittest.TestCase):
    def test_round_trips_through_json(self) -> None:
        info = _session(
            ended_at="2026-09-24T10:01:00+07:00", frames=10, events=25, stop_reason="user"
        )

        encoded = json.dumps(session_to_dict(info), ensure_ascii=False)

        self.assertEqual(session_from_dict(json.loads(encoded)), info)

    def test_rejects_invalid_sessions(self) -> None:
        good = session_to_dict(_session())
        for bad in (
            dict(good, format_version=FORMAT_VERSION + 1),
            dict(good, client_width=0),
            dict(good, record_fps=0),
            dict(good, started_at="yesterday"),
            dict(good, ended_at="later"),
            dict(good, frames=-1),
            dict(good, dropped_frames=True),
            dict(good, stop_reason=3),
            dict(good, extra=1),
            {k: v for k, v in good.items() if k != "events"},
            "not a dict",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(SchemaError):
                    session_from_dict(bad)


if __name__ == "__main__":
    unittest.main()
