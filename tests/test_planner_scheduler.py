from __future__ import annotations

import threading
import time
import unittest

from agent.game_state import GameState
from agent.llm_planner import PlannerCancelledError
from agent.planner_scheduler import PlannerScheduler


class FakePlanner:
    def __init__(
        self,
        *,
        fail_first_call: bool = False,
        raise_cancelled_error: bool = False,
        outcome: object = None,
    ) -> None:
        self._fail_first_call = fail_first_call
        self._raise_cancelled_error = raise_cancelled_error
        self._outcome = outcome
        self._lock = threading.Lock()
        self.call_times: list[float] = []
        self.thread_ids: list[int] = []
        self._call_events: dict[int, threading.Event] = {}

    def plan_once(self, _state: GameState) -> object:
        with self._lock:
            self.call_times.append(time.monotonic())
            self.thread_ids.append(threading.get_ident())
            call_count = len(self.call_times)
            for expected_count, event in self._call_events.items():
                if call_count >= expected_count:
                    event.set()

        if self._fail_first_call and call_count == 1:
            raise RuntimeError("planned test failure")
        if self._raise_cancelled_error:
            raise PlannerCancelledError()

        return self._outcome

    def wait_for_calls(self, expected_count: int) -> threading.Event:
        event = threading.Event()
        with self._lock:
            if len(self.call_times) >= expected_count:
                event.set()
            else:
                self._call_events[expected_count] = event
        return event

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.call_times)


class PlannerSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.schedulers: list[PlannerScheduler] = []

    def tearDown(self) -> None:
        for scheduler in self.schedulers:
            scheduler.stop()

    def _scheduler(self, planner: FakePlanner, *, interval_seconds: float = 0.02) -> PlannerScheduler:
        scheduler = PlannerScheduler(planner, self.state, interval_seconds=interval_seconds)
        self.schedulers.append(scheduler)
        return scheduler

    def test_calls_planner_repeatedly_on_its_configured_cadence(self) -> None:
        planner = FakePlanner()
        scheduler = self._scheduler(planner)

        self.assertTrue(scheduler.start())
        self.assertTrue(planner.wait_for_calls(3).wait(timeout=1.0))
        scheduler.stop()

        self.assertGreaterEqual(planner.call_count, 3)
        self.assertTrue(all(thread_id != threading.get_ident() for thread_id in planner.thread_ids))
        intervals = [
            later - earlier for earlier, later in zip(planner.call_times, planner.call_times[1:])
        ]
        self.assertTrue(all(interval >= 0.01 for interval in intervals[:2]))

    def test_stop_interrupts_interval_wait_and_halts_future_cycles(self) -> None:
        planner = FakePlanner()
        scheduler = self._scheduler(planner, interval_seconds=0.05)

        self.assertTrue(scheduler.start())
        self.assertTrue(planner.wait_for_calls(1).wait(timeout=1.0))
        started_stopping_at = time.monotonic()
        scheduler.stop()

        self.assertLess(time.monotonic() - started_stopping_at, 0.04)
        self.assertFalse(scheduler.is_running)
        call_count_after_stop = planner.call_count
        self.assertFalse(planner.wait_for_calls(call_count_after_stop + 1).wait(timeout=0.1))

    def test_exception_in_one_cycle_does_not_stop_later_cycles(self) -> None:
        planner = FakePlanner(fail_first_call=True)
        scheduler = self._scheduler(planner)

        with self.assertLogs("agent.planner_scheduler", level="ERROR") as logs:
            self.assertTrue(scheduler.start())
            self.assertTrue(planner.wait_for_calls(2).wait(timeout=1.0))
            scheduler.stop()

        self.assertGreaterEqual(planner.call_count, 2)
        self.assertTrue(any("Planner scheduler cycle failed" in message for message in logs.output))
        self.assertFalse(scheduler.is_running)

    def test_logs_each_successful_cycle_outcome(self) -> None:
        outcome = "accepted planner directive sentinel"
        planner = FakePlanner(outcome=outcome)
        scheduler = self._scheduler(planner)

        with self.assertLogs("agent.planner_scheduler", level="INFO") as logs:
            self.assertTrue(scheduler.start())
            self.assertTrue(planner.wait_for_calls(1).wait(timeout=1.0))
            scheduler.stop()

        self.assertTrue(any(outcome in message for message in logs.output))

    def test_cancelled_directive_logs_discard_without_cycle_outcome(self) -> None:
        planner = FakePlanner(raise_cancelled_error=True)
        scheduler = self._scheduler(planner)

        with self.assertLogs("agent.planner_scheduler", level="INFO") as logs:
            self.assertTrue(scheduler.start())
            self.assertTrue(planner.wait_for_calls(1).wait(timeout=1.0))
            scheduler.stop()

        self.assertTrue(
            any(
                "Planner directive discarded after stop; rule settings unchanged." in message
                for message in logs.output
            )
        )
        self.assertFalse(any("Planner cycle outcome" in message for message in logs.output))
        self.assertFalse(any("changed=True" in message for message in logs.output))
        self.assertFalse(any("Traceback" in message for message in logs.output))

    def test_start_and_stop_are_safe_and_support_restart(self) -> None:
        planner = FakePlanner()
        scheduler = self._scheduler(planner)

        scheduler.stop()
        scheduler.stop()
        self.assertTrue(scheduler.start())
        self.assertTrue(planner.wait_for_calls(1).wait(timeout=1.0))
        self.assertFalse(scheduler.start())
        scheduler.stop()
        scheduler.stop()
        self.assertFalse(scheduler.is_running)

        self.assertTrue(scheduler.start())
        self.assertTrue(planner.wait_for_calls(2).wait(timeout=1.0))
        scheduler.stop()


if __name__ == "__main__":
    unittest.main()
