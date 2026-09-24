from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from agent.action_dispatcher import ActionDispatcher, DispatchResult
from agent.rule_engine import ActionIntent
from agent.skill_executor import SkillExecutor
from agent.skills import SkillPermissions
from tests.test_action_dispatcher_keys import HWND, KNOWN_KEYS, FakeInput, _intent


class FakeDispatcher:
    """Blocks in dispatch until released, like a hold would."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_calls = 0
        self.events: list[threading.Event | None] = []
        self.hwnds: list[int | None] = []
        self.error: Exception | None = None

    def dispatch(self, intent: ActionIntent, *, hwnd, now=None, cancel_event=None):
        self.events.append(cancel_event)
        self.hwnds.append(hwnd)
        self.started.set()
        if self.error is not None:
            raise self.error
        # Wait for the test, or for a cancel, like a hold does.
        while not self.release.is_set():
            if cancel_event is not None and cancel_event.wait(0.01):
                return DispatchResult(intent, True, "cancelled")
        return DispatchResult(intent, True, "done")

    def cancel(self) -> None:
        self.cancel_calls += 1


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class SkillExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = FakeDispatcher()
        self.executor = SkillExecutor(self.dispatcher)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.dispatcher.release.set()
        self.executor.shutdown(join_timeout=2.0)

    def test_submit_does_not_block_and_reports_via_drain(self) -> None:
        started = time.monotonic()
        self.assertIsNone(self.executor.submit(_intent("press", key="x"), hwnd=HWND))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(self.dispatcher.started.wait(2.0))
        self.assertTrue(self.executor.busy)
        self.assertEqual(self.executor.drain(), [])

        self.dispatcher.release.set()
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        runs = self.executor.drain()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].source, "manual")
        self.assertEqual(runs[0].result.reason, "done")
        self.assertEqual(self.dispatcher.hwnds, [HWND])
        self.assertEqual(self.executor.drain(), [])

    def test_busy_rejects_instead_of_queueing(self) -> None:
        self.assertIsNone(self.executor.submit(_intent("hold", key="space", hold=1.0), hwnd=HWND))
        self.assertTrue(self.dispatcher.started.wait(2.0))
        refusal = self.executor.submit(_intent("press", key="x"), hwnd=HWND, source="rule")
        self.assertIsNotNone(refusal)
        self.assertIn("Busy", refusal)
        self.dispatcher.release.set()
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        self.assertEqual(len(self.dispatcher.events), 1)
        # Free again once the first skill finishes.
        self.assertIsNone(self.executor.submit(_intent("press", key="x"), hwnd=HWND))

    def test_cancel_ends_running_skill(self) -> None:
        self.executor.submit(_intent("hold", key="space", hold=1.0), hwnd=HWND)
        self.assertTrue(self.dispatcher.started.wait(2.0))
        self.executor.cancel()
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        self.assertEqual(self.dispatcher.cancel_calls, 1)
        [run] = self.executor.drain()
        self.assertEqual(run.result.reason, "cancelled")

    def test_each_submit_gets_a_fresh_cancel_event(self) -> None:
        self.dispatcher.release.set()
        self.executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        self.executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        first, second = self.dispatcher.events
        self.assertIsNotNone(first)
        self.assertIsNot(first, second)
        self.assertFalse(second.is_set())

    def test_cancel_when_idle_is_harmless(self) -> None:
        self.executor.cancel()
        self.dispatcher.release.set()
        self.assertIsNone(self.executor.submit(_intent("press", key="x"), hwnd=HWND))
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        [run] = self.executor.drain()
        self.assertTrue(run.result.dispatched)

    def test_dispatch_exception_becomes_a_rejection(self) -> None:
        self.dispatcher.error = ValueError("boom")
        self.executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        [run] = self.executor.drain()
        self.assertFalse(run.result.dispatched)
        self.assertIn("boom", run.result.reason)

    def test_shutdown_refuses_new_skills(self) -> None:
        self.executor.submit(_intent("hold", key="space", hold=1.0), hwnd=HWND)
        self.assertTrue(self.dispatcher.started.wait(2.0))
        self.executor.shutdown(join_timeout=2.0)
        self.assertFalse(self.executor.busy)
        refusal = self.executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertIn("shut down", refusal)

    def test_shutdown_rejects_negative_timeout(self) -> None:
        with self.assertRaises(ValueError):
            self.executor.shutdown(join_timeout=-1)

    def test_shutdown_when_idle(self) -> None:
        self.executor.shutdown(join_timeout=0.1)
        self.assertFalse(self.executor.busy)
        self.assertEqual(self.dispatcher.cancel_calls, 1)
        self.assertIn("shut down", self.executor.submit(_intent("press", key="x"), hwnd=HWND))

    def test_shutdown_timeout_leaves_state_consistent(self) -> None:
        stubborn = FakeDispatcher()
        executor = SkillExecutor(stubborn)
        stubborn_run = threading.Event()

        def ignore_cancel(intent, *, hwnd, now=None, cancel_event=None):
            stubborn_run.set()
            stubborn.release.wait(5.0)
            return DispatchResult(intent, True, "done")

        stubborn.dispatch = ignore_cancel  # type: ignore[method-assign]
        executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(stubborn_run.wait(2.0))
        started = time.monotonic()
        executor.shutdown(join_timeout=0.05)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(executor.busy)
        self.assertIn("shut down", executor.submit(_intent("press", key="x"), hwnd=HWND))

        stubborn.release.set()
        self.assertTrue(_wait_until(lambda: not executor.busy))
        [run] = executor.drain()
        self.assertEqual(run.result.reason, "done")

    def test_shutdown_from_the_worker_does_not_deadlock(self) -> None:
        def shut_down_inside(intent, *, hwnd, now=None, cancel_event=None):
            self.executor.shutdown(join_timeout=2.0)
            return DispatchResult(intent, False, "stopped")

        self.dispatcher.dispatch = shut_down_inside  # type: ignore[method-assign]
        self.executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        [run] = self.executor.drain()
        self.assertEqual(run.result.reason, "stopped")

    def test_concurrent_submits_start_exactly_one(self) -> None:
        barrier = threading.Barrier(8)
        refusals: list[str | None] = []
        lock = threading.Lock()

        def submit() -> None:
            barrier.wait()
            refusal = self.executor.submit(_intent("press", key="x"), hwnd=HWND)
            with lock:
                refusals.append(refusal)

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        self.assertEqual(refusals.count(None), 1)
        self.dispatcher.release.set()
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        self.assertEqual(len(self.dispatcher.events), 1)

    def test_thread_start_failure_does_not_stick_busy(self) -> None:
        with mock.patch.object(
            threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
        ):
            refusal = self.executor.submit(_intent("press", key="x"), hwnd=HWND)
        self.assertIn("Could not start skill", refusal)
        self.assertFalse(self.executor.busy)
        self.dispatcher.release.set()
        self.assertIsNone(self.executor.submit(_intent("press", key="x"), hwnd=HWND))
        self.assertTrue(_wait_until(lambda: not self.executor.busy))


class SkillExecutorWithDispatcherTests(unittest.TestCase):
    """The executor driving the real ActionDispatcher over fake input."""

    def setUp(self) -> None:
        self.input = FakeInput()
        permissions = SkillPermissions(allowed_keys=frozenset({"x", "space"}), max_hold_seconds=5.0)
        self.dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            region_resolver=lambda _hwnd: (100, 200, 900, 800),
            permissions_provider=lambda: permissions,
            foreground_checker=lambda _hwnd: True,
            known_keys=KNOWN_KEYS,
        )
        self.executor = SkillExecutor(self.dispatcher)
        self.addCleanup(self.executor.shutdown, join_timeout=2.0)

    def _gated_executor(self) -> tuple[SkillExecutor, threading.Event]:
        """An executor whose worker waits for `go` before reaching the dispatcher."""

        go = threading.Event()
        real = self.dispatcher

        class Gated:
            def dispatch(self, intent, *, hwnd, now=None, cancel_event=None):
                go.wait(2.0)
                return real.dispatch(intent, hwnd=hwnd, now=now, cancel_event=cancel_event)

            def cancel(self) -> None:
                real.cancel()

        executor = SkillExecutor(Gated())
        self.addCleanup(executor.shutdown, join_timeout=2.0)
        self.addCleanup(go.set)
        return executor, go

    def _assert_cancelled_before_input(self, intent: ActionIntent) -> None:
        executor, go = self._gated_executor()
        self.assertIsNone(executor.submit(intent, hwnd=HWND))
        executor.cancel()  # input still enabled: only the cancel protects us
        go.set()
        self.assertTrue(_wait_until(lambda: not executor.busy))
        [run] = executor.drain()
        self.assertFalse(run.result.dispatched)
        self.assertIn("Cancelled", run.result.reason)
        self.assertEqual(self.input.calls, [])

    def test_cancel_before_dispatch_sends_no_key(self) -> None:
        self._assert_cancelled_before_input(_intent("hold", key="space", hold=1.0))

    def test_cancel_before_dispatch_sends_no_press(self) -> None:
        self._assert_cancelled_before_input(_intent("press", key="x"))

    def test_cancel_before_dispatch_sends_no_click(self) -> None:
        self._assert_cancelled_before_input(_intent("click", bbox=(10, 20, 30, 40)))

    def test_f8_order_releases_held_key_quickly(self) -> None:
        self.executor.submit(_intent("hold", key="space", hold=5.0), hwnd=HWND)
        self.assertTrue(self.input.key_down_started.wait(2.0))
        started = time.monotonic()
        self.input.set_enabled(False)  # F8 step 1
        self.executor.cancel()  # F8 step 2
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.input.down, set())
        [run] = self.executor.drain()
        self.assertTrue(run.result.dispatched)

    def test_cancel_during_hold_releases_key(self) -> None:
        self.executor.submit(_intent("hold", key="space", hold=5.0), hwnd=HWND)
        self.assertTrue(self.input.key_down_started.wait(2.0))
        self.executor.cancel()
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        [run] = self.executor.drain()
        self.assertIn("cancelled", run.result.reason)
        self.assertEqual(self.input.calls, [("down", "space"), ("up", "space")])

    def test_press_runs_on_worker(self) -> None:
        self.executor.submit(_intent("press", key="x"), hwnd=HWND, source="rule")
        self.assertTrue(_wait_until(lambda: not self.executor.busy))
        [run] = self.executor.drain()
        self.assertTrue(run.result.dispatched, run.result.reason)
        self.assertEqual(run.source, "rule")


if __name__ == "__main__":
    unittest.main()
