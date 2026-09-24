from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest

import numpy as np

from recording.schema import (
    FrameEvent,
    KeyEvent,
    MarkerEvent,
    StateEvent,
    event_from_dict,
    session_from_dict,
)
from recording.session_writer import SessionWriter, encode_jpeg


class FakeEncoder:
    def __init__(self, *, fail_on_calls: tuple[int, ...] = (), gate: threading.Event | None = None):
        self._fail_on_calls = fail_on_calls
        self._gate = gate
        self.calls = 0

    def __call__(self, frame: object, quality: int) -> bytes:
        self.calls += 1
        if self._gate is not None:
            self._gate.wait(timeout=5.0)
        if self.calls in self._fail_on_calls:
            raise RuntimeError(f"planned encoder failure {self.calls}")
        return f"jpeg:{frame}:{quality}".encode()


class SessionWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _writer(self, encoder: FakeEncoder | None = None, **kwargs: object) -> SessionWriter:
        options: dict[str, object] = dict(
            app_version="0.5.0",
            window_title="Test Game",
            client_size=(640, 480),
            record_fps=10.0,
            session_name="session",
            clock=lambda: 100.0,
            encoder=encoder or FakeEncoder(),
        )
        options.update(kwargs)
        writer = SessionWriter(self.root, **options)  # type: ignore[arg-type]
        self.addCleanup(writer.close, "cleanup", join_timeout=5.0)
        return writer

    @staticmethod
    def _events(writer: SessionWriter) -> list[object]:
        text = (writer.session_dir / "events.jsonl").read_text(encoding="utf-8")
        return [event_from_dict(json.loads(line)) for line in text.splitlines()]

    @staticmethod
    def _session(writer: SessionWriter):
        text = (writer.session_dir / "session.json").read_text(encoding="utf-8")
        return session_from_dict(json.loads(text))

    @staticmethod
    def _frame_files(writer: SessionWriter) -> list[str]:
        return sorted(path.name for path in (writer.session_dir / "frames").iterdir())

    @staticmethod
    def _wait_until(predicate, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_writes_layout_events_in_order_and_session_json(self) -> None:
        writer = self._writer(jpeg_quality=70)
        state = StateEvent(t=0.1, observations=())

        self.assertEqual(writer.t0, 100.0)
        self.assertTrue(writer.write_event(MarkerEvent(t=0.0, action="start")))
        self.assertTrue(writer.write_frame("f1", 0.1, state))
        self.assertTrue(writer.write_event(KeyEvent(t=0.15, key="a", action="down")))
        self.assertTrue(writer.write_frame("f2", 0.2))
        self.assertTrue(writer.write_frame("f3", 0.3))
        self.assertTrue(writer.write_event(MarkerEvent(t=0.4, action="stop", reason="user")))
        self.assertTrue(writer.close("user", join_timeout=5.0))

        self.assertEqual(writer.session_dir, self.root / "session")
        self.assertEqual(self._frame_files(writer), ["000001.jpg", "000002.jpg", "000003.jpg"])
        self.assertEqual((writer.session_dir / "frames" / "000002.jpg").read_bytes(), b"jpeg:f2:70")
        self.assertEqual(
            self._events(writer),
            [
                MarkerEvent(t=0.0, action="start"),
                FrameEvent(t=0.1, index=1, file="frames/000001.jpg"),
                state,
                KeyEvent(t=0.15, key="a", action="down"),
                FrameEvent(t=0.2, index=2, file="frames/000002.jpg"),
                FrameEvent(t=0.3, index=3, file="frames/000003.jpg"),
                MarkerEvent(t=0.4, action="stop", reason="user"),
            ],
        )
        info = self._session(writer)
        self.assertEqual((info.frames, info.dropped_frames, info.events), (3, 0, 7))
        self.assertEqual((info.client_width, info.client_height), (640, 480))
        self.assertEqual(info.window_title, "Test Game")
        self.assertEqual(info.stop_reason, "user")
        self.assertIsNotNone(info.ended_at)
        self.assertFalse((writer.session_dir / "session.json.tmp").exists())

    def test_session_json_exists_while_recording(self) -> None:
        writer = self._writer()

        info = self._session(writer)

        self.assertIsNone(info.ended_at)
        self.assertIsNone(info.stop_reason)
        self.assertEqual(info.frames, 0)

    def test_session_names_are_unique_and_validated(self) -> None:
        first = self._writer(session_name="dup")
        second = self._writer(session_name="dup")
        third = self._writer(session_name="dup")
        stamped = self._writer(session_name=None)

        self.assertEqual(first.session_dir.name, "dup")
        self.assertEqual(second.session_dir.name, "dup_2")
        self.assertEqual(third.session_dir.name, "dup_3")
        self.assertRegex(stamped.session_dir.name, re.compile(r"^\d{8}_\d{6}$"))
        for bad in ("../escape", "a/b", ".."):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    self._writer(session_name=bad)

    def test_full_queue_drops_frames_without_consuming_indices(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        writer = self._writer(FakeEncoder(gate=gate), max_queue_frames=2)

        self.assertTrue(writer.write_frame("f1", 0.1))
        self.assertTrue(writer.write_frame("f2", 0.2))
        self.assertFalse(writer.write_frame("f3", 0.3))
        self.assertFalse(writer.write_frame("f4", 0.4))
        self.assertTrue(writer.write_event(KeyEvent(t=0.45, key="a", action="up")))
        self.assertEqual(writer.stats().dropped_frames, 2)

        gate.set()
        self.assertTrue(self._wait_until(lambda: writer.stats().pending == 0))
        self.assertTrue(writer.write_frame("f5", 0.5))
        self.assertTrue(writer.close("user", join_timeout=5.0))

        self.assertEqual(self._frame_files(writer), ["000001.jpg", "000002.jpg", "000003.jpg"])
        self.assertEqual((writer.session_dir / "frames" / "000003.jpg").read_bytes(), b"jpeg:f5:85")
        info = self._session(writer)
        self.assertEqual((info.frames, info.dropped_frames, info.events), (3, 2, 4))

    def test_encoder_failure_is_counted_and_thread_survives(self) -> None:
        writer = self._writer(FakeEncoder(fail_on_calls=(2,)))

        with self.assertLogs("recording.session_writer", level="ERROR") as logs:
            for index, t in enumerate((0.1, 0.2, 0.3), start=1):
                self.assertTrue(writer.write_frame(f"f{index}", t))
            self.assertTrue(writer.close("user", join_timeout=5.0))

        self.assertTrue(any("Failed to write recording frame" in line for line in logs.output))
        self.assertEqual(self._frame_files(writer), ["000001.jpg", "000002.jpg"])
        self.assertEqual((writer.session_dir / "frames" / "000002.jpg").read_bytes(), b"jpeg:f3:85")
        frames = [event for event in self._events(writer) if isinstance(event, FrameEvent)]
        self.assertEqual([(event.index, event.t) for event in frames], [(1, 0.1), (2, 0.3)])
        stats = writer.stats()
        self.assertEqual((stats.frames_written, stats.dropped_frames, stats.errors), (2, 1, 1))
        self.assertEqual(self._session(writer).dropped_frames, 1)

    def test_close_is_idempotent_and_rejects_later_writes(self) -> None:
        writer = self._writer()

        self.assertTrue(writer.close("first", join_timeout=5.0))
        self.assertTrue(writer.close("second", join_timeout=5.0))

        self.assertFalse(writer.write_event(KeyEvent(t=1.0, key="a", action="down")))
        self.assertFalse(writer.write_frame("late", 1.0))
        self.assertEqual(self._session(writer).stop_reason, "first")
        stats = writer.stats()
        self.assertTrue(stats.closed)
        self.assertTrue(stats.finished)
        self.assertEqual(stats.dropped_frames, 0)

    def test_close_does_not_block_by_default(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        writer = self._writer(FakeEncoder(gate=gate))
        self.assertTrue(writer.write_frame("slow", 0.1))

        started = time.monotonic()
        finished = writer.close("f8")

        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(finished)
        self.assertTrue(writer.stats().closed)
        gate.set()
        self.assertTrue(writer.wait_finished(timeout=5.0))
        info = self._session(writer)
        self.assertEqual((info.frames, info.stop_reason), (1, "f8"))

    def test_rejects_invalid_arguments(self) -> None:
        writer = self._writer()

        with self.assertRaises(ValueError):
            writer.write_event(FrameEvent(t=0.0, index=1, file="frames/000001.jpg"))
        with self.assertRaises(ValueError):
            writer.write_event("not an event")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            writer.write_frame("f", 0.0, state_event="state")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            writer.write_frame("f", -1.0)
        with self.assertRaises(ValueError):
            self._writer(max_queue_frames=0)
        with self.assertRaises(ValueError):
            self._writer(client_size=(0, 480))

    def test_default_encoder_produces_jpeg(self) -> None:
        frame = np.zeros((8, 12, 3), dtype=np.uint8)

        data = encode_jpeg(frame, 85)

        self.assertTrue(data.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
