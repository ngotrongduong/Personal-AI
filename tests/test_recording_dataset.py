from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from recording.dataset import (
    DATASET_FILE,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_INVALID,
    DatasetError,
    event_source,
    export_dataset,
    iter_dataset_rows,
    list_sessions,
    load_session,
    validate_session,
)
from recording.schema import (
    FocusEvent,
    KeyEvent,
    MarkerEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    ObservationRecord,
    ScrollEvent,
    StateEvent,
)
from recording.session_writer import SessionWriter


def _state(t: float) -> StateEvent:
    return StateEvent(
        t=t,
        observations=(
            ObservationRecord(
                name="hp",
                visible=True,
                confidence=0.9,
                bbox=(1, 2, 10, 5),
                value=50,
                source="ocr",
                observed_t=t - 0.01,
            ),
        ),
    )


def _frame(value: int = 0) -> np.ndarray:
    return np.full((48, 64, 3), value, dtype=np.uint8)


def make_session(root: Path, name: str = "20260924_120000") -> Path:
    """Record a small session the way RecordingController orders its writes.

    The click at t=0.19 is queued after the frame at t=0.2: input and sampler
    events may interleave out of order, which is valid.
    """
    writer = SessionWriter(
        root,
        app_version="0.5.0",
        window_title="Test Game",
        client_size=(64, 48),
        record_fps=10.0,
        session_name=name,
        clock=lambda: 100.0,
    )
    writer.write_event(MarkerEvent(t=0.0, action="start"))
    writer.write_event(FocusEvent(t=0.05, action="gained"))
    writer.write_frame(_frame(10), 0.1, _state(0.1))
    writer.write_event(KeyEvent(t=0.12, key="w", action="down"))
    writer.write_event(MouseMoveEvent(t=0.15, x=10, y=20))
    writer.write_frame(_frame(20), 0.2, _state(0.2))
    writer.write_event(MouseButtonEvent(t=0.19, button="left", action="down", x=30, y=40))
    writer.write_event(KeyEvent(t=0.25, key="w", action="up"))
    writer.write_frame(_frame(30), 0.3, _state(0.3))
    writer.write_event(ScrollEvent(t=0.35, dx=0, dy=-1, x=5, y=6))
    writer.write_event(MarkerEvent(t=0.4, action="stop", reason="user"))
    writer.close("user", join_timeout=5.0)
    assert writer.wait_finished(5.0)
    return writer.session_dir


def read_lines(session_dir: Path) -> list[dict]:
    text = (session_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def write_lines(session_dir: Path, lines: list[dict]) -> None:
    text = "".join(json.dumps(line) + "\n" for line in lines)
    (session_dir / "events.jsonl").write_text(text, encoding="utf-8", newline="\n")


def edit_session_json(session_dir: Path, **changes: object) -> None:
    path = session_dir / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


class DatasetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session_dir = make_session(self.root)


class ValidateTests(DatasetTestCase):
    def test_recorded_session_is_valid(self) -> None:
        report = validate_session(self.session_dir)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.lines, 14)
        self.assertEqual(report.frames, 3)
        self.assertEqual(
            report.counts_by_type,
            {
                "marker": 2,
                "focus": 1,
                "frame": 3,
                "state": 3,
                "key": 2,
                "mouse_move": 1,
                "mouse_button": 1,
                "scroll": 1,
            },
        )

    def test_event_source_splits_sampler_and_input(self) -> None:
        self.assertEqual(event_source(_state(0.1)), "sampler")
        self.assertEqual(event_source(MarkerEvent(t=0.0, action="start")), "sampler")
        self.assertEqual(event_source(KeyEvent(t=0.0, key="a", action="up")), "input")
        self.assertEqual(event_source(FocusEvent(t=0.0, action="lost")), "input")
        with self.assertRaises(TypeError):
            event_source(object())  # type: ignore[arg-type]

    def test_time_going_backwards_within_a_source_is_an_error(self) -> None:
        lines = read_lines(self.session_dir)
        key_up = next(i for i, line in enumerate(lines) if line.get("action") == "up")
        lines[key_up]["t"] = 0.11  # before the key down at 0.12
        write_lines(self.session_dir, lines)

        report = validate_session(self.session_dir)

        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("goes backwards", report.errors[0])
        self.assertIn("input events", report.errors[0])

    def test_missing_frame_file_is_an_error(self) -> None:
        (self.session_dir / "frames" / "000002.jpg").unlink()

        report = validate_session(self.session_dir)

        self.assertFalse(report.ok)
        self.assertTrue(any("000002.jpg is missing" in e for e in report.errors), report.errors)

    def test_unexpected_frame_path_is_rejected_without_following_it(self) -> None:
        lines = read_lines(self.session_dir)
        frame = next(line for line in lines if line["type"] == "frame")
        frame["file"] = "../../outside.jpg"
        write_lines(self.session_dir, lines)

        report = validate_session(self.session_dir)

        self.assertFalse(report.ok)
        self.assertTrue(any("should be 'frames/000001.jpg'" in e for e in report.errors))
        # The real 000001.jpg is now unreferenced.
        self.assertTrue(any("not referenced" in w for w in report.warnings))

    def test_frame_index_gap_is_an_error(self) -> None:
        lines = [line for line in read_lines(self.session_dir) if line.get("index") != 2]
        write_lines(self.session_dir, lines)
        edit_session_json(self.session_dir, frames=2, events=13)

        report = validate_session(self.session_dir)

        self.assertTrue(any("frame index 3, expected 2" in e for e in report.errors))

    def test_counts_must_match_session_json(self) -> None:
        edit_session_json(self.session_dir, frames=4, events=20)

        report = validate_session(self.session_dir)

        self.assertEqual(len(report.errors), 2)
        self.assertIn("3 frame events, but session.json says 4", report.errors[0])
        self.assertIn("14 lines in events.jsonl, but session.json says 20", report.errors[1])

    def test_extra_frame_file_is_a_warning(self) -> None:
        (self.session_dir / "frames" / "000009.jpg").write_bytes(b"x")

        report = validate_session(self.session_dir)

        self.assertTrue(report.ok)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("1 frame file(s)", report.warnings[0])

    def test_bad_lines_are_errors_and_capped(self) -> None:
        text = (self.session_dir / "events.jsonl").read_text(encoding="utf-8")
        garbage = "".join('{"type": "teleport", "t": 1}\n' for _ in range(15))
        (self.session_dir / "events.jsonl").write_text(text + garbage + "not json\n")
        edit_session_json(self.session_dir, events=30)

        report = validate_session(self.session_dir, max_issues_per_kind=5)

        self.assertFalse(report.ok)
        self.assertEqual(report.lines, 30)
        self.assertEqual(len(report.errors), 6)
        self.assertIn("Unknown event type", report.errors[0])
        self.assertEqual(report.errors[-1], "... and 11 more event problem(s).")

    def test_unfinished_session_tolerates_a_truncated_last_line(self) -> None:
        edit_session_json(self.session_dir, ended_at=None, stop_reason=None)
        with open(self.session_dir / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write('{"type": "key", "t": 0.5')

        report = validate_session(self.session_dir)

        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("last line is incomplete" in w for w in report.warnings))
        self.assertTrue(any("ended_at is null" in w for w in report.warnings))

    def test_finished_session_with_a_truncated_line_is_an_error(self) -> None:
        with open(self.session_dir / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write('{"type": "key", "t": 0.5')

        report = validate_session(self.session_dir)

        self.assertFalse(report.ok)

    def test_hostile_json_is_reported_not_raised(self) -> None:
        text = (self.session_dir / "events.jsonl").read_text(encoding="utf-8")
        huge_int = '{"type": "key", "t": ' + "1" * 5000 + ', "key": "w", "action": "down"}\n'
        deep = "[" * 200_000 + "\n"
        (self.session_dir / "events.jsonl").write_text(text + huge_int + deep, encoding="utf-8")

        report = validate_session(self.session_dir)

        self.assertFalse(report.ok)
        self.assertEqual(report.lines, 16)
        self.assertEqual(sum("line 15" in e or "line 16" in e for e in report.errors), 2)

        (self.session_dir / "session.json").write_text('{"frames": ' + "9" * 5000 + "}")
        report = validate_session(self.session_dir)
        self.assertTrue(any("session.json" in e for e in report.errors))

    def test_frame_symlink_out_of_the_session_is_an_error(self) -> None:
        outside = self.root / "outside.jpg"
        frame = self.session_dir / "frames" / "000002.jpg"
        outside.write_bytes(frame.read_bytes())
        frame.unlink()
        try:
            frame.symlink_to(outside)
        except OSError:
            self.skipTest("creating symlinks needs Developer Mode or admin on Windows")

        report = validate_session(self.session_dir)

        self.assertFalse(report.ok)
        self.assertTrue(any("links outside the session" in e for e in report.errors))

    def test_missing_files(self) -> None:
        (self.session_dir / "session.json").unlink()
        report = validate_session(self.session_dir)
        self.assertIn("session.json is missing.", report.errors)

        (self.session_dir / "events.jsonl").unlink()
        report = validate_session(self.session_dir)
        self.assertIn("events.jsonl is missing.", report.errors)

        report = validate_session(self.root / "nope")
        self.assertFalse(report.ok)

    def test_stop_marker_reason_mismatch_is_a_warning(self) -> None:
        edit_session_json(self.session_dir, stop_reason="f8")

        report = validate_session(self.session_dir)

        self.assertTrue(report.ok)
        self.assertTrue(any("differs" in w for w in report.warnings))


class ListSessionsTests(DatasetTestCase):
    def test_lists_complete_incomplete_and_invalid_folders(self) -> None:
        crashed = make_session(self.root, "20260924_130000")
        edit_session_json(crashed, ended_at=None, stop_reason=None)
        (self.root / "20260924_140000").mkdir()
        (self.root / ".gitkeep").write_text("")

        sessions = list_sessions(self.root)

        self.assertEqual(
            [(s.name, s.status) for s in sessions],
            [
                ("20260924_120000", STATUS_COMPLETE),
                ("20260924_130000", STATUS_INCOMPLETE),
                ("20260924_140000", STATUS_INVALID),
            ],
        )
        self.assertIsNotNone(sessions[0].duration_seconds)
        self.assertIsNone(sessions[1].duration_seconds)
        self.assertEqual(sessions[2].error, "session.json is missing.")

    def test_missing_root_lists_nothing(self) -> None:
        self.assertEqual(list_sessions(self.root / "missing"), [])

    def test_duration_from_wall_clock(self) -> None:
        edit_session_json(
            self.session_dir,
            started_at="2026-09-24T12:00:00+07:00",
            ended_at="2026-09-24T12:01:05+07:00",
        )
        self.assertEqual(list_sessions(self.root)[0].duration_seconds, 65.0)


class ExportTests(DatasetTestCase):
    def test_rows_align_frames_state_and_actions(self) -> None:
        rows = list(iter_dataset_rows(load_session(self.session_dir)))

        self.assertEqual([row["index"] for row in rows], [1, 2, 3])
        self.assertEqual([row["t"] for row in rows], [0.1, 0.2, 0.3])
        self.assertEqual([row["next_t"] for row in rows], [0.2, 0.3, None])
        self.assertEqual(rows[0]["file"], "frames/000001.jpg")
        self.assertEqual(rows[0]["image"], "frames/000001.jpg")
        self.assertEqual([row["focused"] for row in rows], [True, True, True])
        self.assertEqual([row["state_t"] for row in rows], [0.1, 0.2, 0.3])
        self.assertEqual(rows[1]["state"][0]["name"], "hp")
        self.assertEqual(rows[1]["state"][0]["bbox"], [1, 2, 10, 5])
        self.assertEqual(
            [[(a["type"], a["t"]) for a in row["actions"]] for row in rows],
            [
                [("key", 0.12), ("mouse_move", 0.15), ("mouse_button", 0.19)],
                [("key", 0.25)],
                [("scroll", 0.35)],
            ],
        )

    def test_export_writes_dataset_next_to_the_session(self) -> None:
        result = export_dataset(self.session_dir)

        self.assertEqual(result.out_path, self.session_dir / DATASET_FILE)
        self.assertEqual((result.rows, result.actions, result.unassigned_actions), (3, 5, 1))
        text = result.out_path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["actions"][0]["type"], "scroll")
        self.assertFalse(list(self.session_dir.glob("*.tmp")))
        # Exporting leaves the session valid.
        self.assertTrue(validate_session(self.session_dir).ok)

    def test_export_elsewhere_uses_relative_image_paths(self) -> None:
        out = self.root / "exports" / "run1.jsonl"

        export_dataset(self.session_dir, out)

        row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["image"], "../20260924_120000/frames/000001.jpg")
        self.assertTrue((out.parent / row["image"]).is_file())

    def test_existing_output_needs_overwrite(self) -> None:
        export_dataset(self.session_dir)
        with self.assertRaises(DatasetError):
            export_dataset(self.session_dir)
        self.assertEqual(export_dataset(self.session_dir, overwrite=True).rows, 3)

    def test_never_writes_over_recording_files(self) -> None:
        events_before = (self.session_dir / "events.jsonl").read_bytes()
        session_before = (self.session_dir / "session.json").read_bytes()
        for out in (
            self.session_dir / "events.jsonl",
            self.session_dir / "session.json",
            self.session_dir / "frames" / "000001.jpg",
            self.session_dir / "frames",
            self.root / "other" / "events.jsonl",
        ):
            with self.subTest(out=out), self.assertRaises(DatasetError):
                export_dataset(self.session_dir, out, overwrite=True)
        self.assertEqual((self.session_dir / "events.jsonl").read_bytes(), events_before)
        self.assertEqual((self.session_dir / "session.json").read_bytes(), session_before)

    def test_existing_temp_file_is_never_touched(self) -> None:
        # A user's own "<out>.tmp" is neither truncated nor removed.
        notes = self.root / "notes.tmp"
        notes.write_text("keep me", encoding="utf-8")
        with self.assertRaises(DatasetError):
            export_dataset(self.session_dir, self.root / "notes")
        self.assertEqual(notes.read_text(encoding="utf-8"), "keep me")

        # A hardlink named like the temp file cannot be used to write events.jsonl.
        events = self.session_dir / "events.jsonl"
        before = events.read_bytes()
        (self.session_dir / (DATASET_FILE + ".tmp")).hardlink_to(events)
        with self.assertRaises(DatasetError):
            export_dataset(self.session_dir)
        self.assertEqual(events.read_bytes(), before)
        self.assertFalse((self.session_dir / DATASET_FILE).exists())

    def test_invalid_session_is_not_exported(self) -> None:
        (self.session_dir / "frames" / "000001.jpg").unlink()

        with self.assertRaises(DatasetError):
            export_dataset(self.session_dir)
        self.assertFalse((self.session_dir / DATASET_FILE).exists())

    def test_focus_before_first_event_is_unknown(self) -> None:
        lines = [line for line in read_lines(self.session_dir) if line["type"] != "focus"]
        lines.insert(8, {"type": "focus", "t": 0.21, "action": "lost"})
        write_lines(self.session_dir, lines)
        edit_session_json(self.session_dir, events=len(lines))

        rows = list(iter_dataset_rows(load_session(self.session_dir)))

        self.assertEqual([row["focused"] for row in rows], [None, None, False])
        self.assertEqual(rows[1]["actions"][0]["type"], "focus")


if __name__ == "__main__":
    unittest.main()
