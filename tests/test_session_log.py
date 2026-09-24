from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent.session_log import (
    EVENT_TYPES,
    MAX_TEXT_CHARS,
    SessionLogWriter,
    read_session,
    record_problems,
    validate_session,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def fixed_wall() -> datetime:
    return datetime(2026, 9, 25, 10, 0, 0)


START = {
    "app_version": "0.8.0",
    "profile": "Notepad demo",
    "model": "qwen3.5:9b",
    "goal": "Type x",
    "auto_max_steps": 20,
    "llm_notes": False,
}


class SessionLogWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "sessions" / "20260925-100000.jsonl"
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def writer(self, **kwargs) -> SessionLogWriter:
        return SessionLogWriter(self.path, clock=self.clock, wall=fixed_wall, **kwargs)

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_every_record_type_round_trips(self) -> None:
        log = self.writer()
        self.assertTrue(log.write("session_start", **START))
        self.clock.now += 1.2345
        self.assertTrue(log.write("cycle", status="ok", message="noop", latency_s=2.5))
        self.assertTrue(log.write("cycle", status="error", message="timeout", latency_s=None))
        self.assertTrue(
            log.write("step", skill="type_x", reason="goal", decision="approved", outcome="DISPATCHED", ok=True)
        )
        self.assertTrue(log.write("step", skill="type_x", reason="r", decision="expired", outcome="", ok=None))
        self.assertTrue(log.write("auto", on=True, reason="confirmed", max_steps=3))
        self.assertTrue(log.write("auto", on=False, reason="step cap", max_steps=None))
        self.assertTrue(log.write("note", action="add", source="llm", text="x works"))
        self.assertTrue(log.close("planner off"))

        records = read_session(self.path)
        self.assertEqual(
            [r["type"] for r in records],
            ["session_start", "cycle", "cycle", "step", "step", "auto", "auto", "note", "session_end"],
        )
        self.assertEqual(records[0]["t"], 0.0)
        self.assertEqual(records[1]["t"], 1.234)
        self.assertEqual(records[0]["wall"], "2026-09-25T10:00:00")
        self.assertEqual(records[0]["v"], 1)
        self.assertEqual(validate_session(self.path), [])
        self.assertEqual(EVENT_TYPES, {r["type"] for r in records} | {"truncated"})

    def test_close_writes_one_session_end_and_then_nothing(self) -> None:
        log = self.writer()
        log.write("session_start", **START)
        self.assertTrue(log.close("f8"))
        self.assertFalse(log.close("again"))
        self.assertFalse(log.write("cycle", status="ok", message="late", latency_s=1.0))
        self.assertTrue(log.closed)
        self.assertEqual([r["type"] for r in self.lines()], ["session_start", "session_end"])

    def test_invalid_records_are_programming_errors(self) -> None:
        log = self.writer()
        with self.assertRaises(ValueError):
            log.write("keypress", key="x")
        with self.assertRaises(ValueError):
            log.write("cycle", status="ok", message="m")  # missing latency_s
        with self.assertRaises(ValueError):
            log.write("cycle", status="ok", message="m", latency_s=1.0, extra=1)
        with self.assertRaises(ValueError):
            log.write("step", skill="a", reason="r", decision="maybe", outcome="o", ok=None)
        with self.assertRaises(ValueError):
            log.write("auto", on="yes", reason="r", max_steps=None)
        with self.assertRaises(ValueError):
            log.write("note", action="add", source="system", text="t")
        with self.assertRaises(ValueError):
            log.write("session_start", **{**START, "auto_max_steps": True})
        self.assertFalse(self.path.exists())

    def test_text_is_cleaned_and_cut(self) -> None:
        log = self.writer()
        log.write("session_start", **{**START, "goal": "a\nb\x00c\t" + "z" * 1000})
        goal = self.lines()[0]["goal"]
        self.assertTrue(goal.startswith("a b c"))
        self.assertEqual(len(goal), MAX_TEXT_CHARS)
        self.assertTrue(all(ch.isprintable() for ch in goal))

    def test_size_cap_writes_one_truncated_record(self) -> None:
        log = self.writer(max_bytes=600)
        log.write("session_start", **START)
        written = 0
        for _ in range(20):
            if log.write("cycle", status="ok", message="noop " * 5, latency_s=1.0):
                written += 1
        self.assertTrue(log.truncated)
        self.assertFalse(log.close("planner off"))
        types = [r["type"] for r in self.lines()]
        self.assertEqual(types.count("truncated"), 1)
        self.assertEqual(types[-1], "truncated")
        self.assertEqual(types.count("cycle"), written)
        self.assertEqual(validate_session(self.path), [])

    def test_io_error_turns_the_writer_off_without_raising(self) -> None:
        blocker = self.dir / "blocked"
        blocker.write_text("a file, not a folder", encoding="utf-8")
        log = SessionLogWriter(blocker / "s.jsonl", clock=self.clock, wall=fixed_wall)
        self.assertFalse(log.write("session_start", **START))
        self.assertTrue(log.failed)
        self.assertIsNotNone(log.error)
        self.assertFalse(log.write("cycle", status="ok", message="m", latency_s=None))
        self.assertFalse(log.close("x"))

    def test_appends_never_truncate_existing_content(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("", encoding="utf-8")
        log = self.writer()
        log.write("session_start", **START)
        log.write("cycle", status="ok", message="a", latency_s=1.0)
        self.assertEqual(len(self.lines()), 2)

    def test_concurrent_writes_keep_whole_lines(self) -> None:
        log = self.writer()
        log.write("session_start", **START)

        def spam() -> None:
            for index in range(50):
                log.write("cycle", status="ok", message=f"m{index}", latency_s=0.1)

        threads = [threading.Thread(target=spam) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        log.close("done")
        self.assertEqual(len(read_session(self.path)), 202)


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "s.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_lines(self, *records: object) -> None:
        self.path.write_text(
            "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records),
            encoding="utf-8",
        )

    @staticmethod
    def rec(event_type: str, **fields) -> dict:
        return {"v": 1, "type": event_type, "t": 0.0, "wall": "2026-09-25T10:00:00", **fields}

    def test_valid_file_without_end_is_accepted(self) -> None:
        self.write_lines(self.rec("session_start", **START))
        self.assertEqual(validate_session(self.path), [])

    def test_rejects_bad_order_and_duplicates(self) -> None:
        start = self.rec("session_start", **START)
        end = self.rec("session_end", reason="x")
        cycle = self.rec("cycle", status="ok", message="m", latency_s=None)
        cases = {
            "first record": [cycle, start],
            "more than one session_start": [start, start],
            "more than one session_end": [start, end, end],
            "after session_end": [start, end, cycle],
        }
        for expected, records in cases.items():
            with self.subTest(expected):
                self.write_lines(*records)
                problems = validate_session(self.path)
                self.assertTrue(any(expected in p for p in problems), problems)
                with self.assertRaises(ValueError):
                    read_session(self.path)

    def test_rejects_bad_lines(self) -> None:
        start = self.rec("session_start", **START)
        self.write_lines(
            start,
            "not json",
            "",
            self.rec("mystery"),
            {**self.rec("cycle", status="ok", message="m", latency_s=1), "sneaky": 1},
            {**self.rec("cycle", status="ok", message="m", latency_s=1), "v": 2},
            {**self.rec("cycle", status="ok", message="m", latency_s=1), "t": -1},
            [1, 2],
        )
        problems = validate_session(self.path)
        for expected in ("invalid JSON", "blank line", "unknown record type", "unknown field",
                         "unsupported version", "non-negative", "not a JSON object"):
            self.assertTrue(any(expected in p for p in problems), (expected, problems))

    def test_missing_or_empty_file(self) -> None:
        self.assertTrue(validate_session(self.path)[0].startswith("cannot read"))
        self.path.write_text("", encoding="utf-8")
        self.assertIn("empty session", validate_session(self.path))

    def test_record_problems_on_valid_record(self) -> None:
        self.assertEqual(record_problems(self.rec("truncated", limit_bytes=10)), [])


if __name__ == "__main__":
    unittest.main()
