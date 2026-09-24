from __future__ import annotations

import threading
import time
import unittest

from agent.action_dispatcher import ActionDispatcher
from agent.rule_engine import ActionIntent
from agent.skills import PRESS_SECONDS, SkillPermissions

HWND = 4242
KNOWN_KEYS = frozenset({"x", "space", "f8", "q"})


class FakeInput:
    """Records calls instead of sending input. Mirrors InputController's API."""

    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[tuple] = []
        self.down: set[str] = set()
        self.key_down_started = threading.Event()
        self.fail_click: Exception | None = None
        self.fail_tap: Exception | None = None
        self.fail_key_down: Exception | None = None
        self.fail_key_up: Exception | None = None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            for key in list(self.down):
                self.key_up(key)

    def click(self, x: int, y: int) -> None:
        if not self.enabled:
            raise RuntimeError("Input control is disabled.")
        if self.fail_click is not None:
            raise self.fail_click
        self.calls.append(("click", x, y))

    def tap_key(self, key: str, duration: float) -> None:
        if not self.enabled:
            raise RuntimeError("Input control is disabled.")
        if self.fail_tap is not None:
            raise self.fail_tap
        self.calls.append(("tap", key, duration))

    def key_down(self, key: str) -> None:
        if not self.enabled:
            raise RuntimeError("Input control is disabled.")
        if self.fail_key_down is not None:
            raise self.fail_key_down
        self.calls.append(("down", key))
        self.down.add(key)
        self.key_down_started.set()

    def key_up(self, key: str) -> None:
        self.calls.append(("up", key))
        self.down.discard(key)
        if self.fail_key_up is not None:
            raise self.fail_key_up


def _region(_hwnd: int) -> tuple[int, int, int, int]:
    return 1000, 500, 1400, 900


def _intent(action: str, *, key: str | None = None, hold: float | None = None,
            created_at: float | None = None, bbox=None) -> ActionIntent:
    return ActionIntent(
        rule_name="test",
        action=action,
        detector_name="",
        confidence=1.0,
        target_bbox=bbox,
        created_at=time.monotonic() if created_at is None else created_at,
        reason="test",
        skill_name="skill",
        key=key,
        hold_seconds=hold,
    )


class KeyDispatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.input = FakeInput()
        self.foreground = True
        self.permissions: SkillPermissions | None = SkillPermissions(
            allowed_keys=frozenset({"x", "space"}),
            max_hold_seconds=1.5,
            max_actions_per_second=5,
        )
        self.dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            region_resolver=_region,
            permissions_provider=lambda: self.permissions,
            foreground_checker=lambda _hwnd: self.foreground,
            known_keys=KNOWN_KEYS,
        )

    def _run_in_thread(self, intent: ActionIntent, **kwargs):
        box: dict = {}

        def target() -> None:
            box["result"] = self.dispatcher.dispatch(intent, hwnd=HWND, **kwargs)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, box


class PressTests(KeyDispatchTestCase):
    def test_press_taps_key(self) -> None:
        result = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(result.dispatched, result.reason)
        self.assertEqual(self.input.calls, [("tap", "x", PRESS_SECONDS)])

    def test_disabled_input_blocks(self) -> None:
        self.input.enabled = False
        result = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("disabled", result.reason)
        self.assertEqual(self.input.calls, [])

    def test_skill_action_is_not_dispatchable(self) -> None:
        result = self.dispatcher.dispatch(_intent("skill", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("Unsupported", result.reason)

    def test_no_permissions_blocks_keys(self) -> None:
        self.permissions = None
        result = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("permissions", result.reason)
        self.assertEqual(self.input.calls, [])

    def test_no_permissions_provider_blocks_keys(self) -> None:
        dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            foreground_checker=lambda _hwnd: True,
            known_keys=KNOWN_KEYS,
        )
        result = dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertEqual(self.input.calls, [])

    def test_permissions_provider_error_blocks_keys(self) -> None:
        def broken() -> SkillPermissions:
            raise OSError("boom")

        dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            permissions_provider=broken,
            foreground_checker=lambda _hwnd: True,
            known_keys=KNOWN_KEYS,
        )
        result = dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertEqual(self.input.calls, [])

    def test_key_not_in_allowlist_blocks(self) -> None:
        result = self.dispatcher.dispatch(_intent("press", key="q"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("allowed_keys", result.reason)
        self.assertEqual(self.input.calls, [])

    def test_forbidden_keys_block_even_if_known(self) -> None:
        for key in ("f8", "win", "apps", "ctrl+c", "X", "", None):
            with self.subTest(key=key):
                result = self.dispatcher.dispatch(_intent("press", key=key), hwnd=HWND)
                self.assertFalse(result.dispatched)
                self.assertIn("Key blocked", result.reason)
        self.assertEqual(self.input.calls, [])

    def test_unknown_key_blocks(self) -> None:
        dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            permissions_provider=lambda: self.permissions,
            foreground_checker=lambda _hwnd: True,
            known_keys=frozenset({"space"}),
        )
        result = dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("unknown key", result.reason)

    def test_default_known_keys_come_from_pydirectinput(self) -> None:
        dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            permissions_provider=lambda: self.permissions,
            foreground_checker=lambda _hwnd: True,
        )
        result = dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(result.dispatched, result.reason)

    def test_press_with_hold_seconds_is_rejected(self) -> None:
        result = self.dispatcher.dispatch(_intent("press", key="x", hold=1.0), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertEqual(self.input.calls, [])

    def test_not_foreground_blocks(self) -> None:
        self.foreground = False
        result = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("foreground", result.reason)
        self.assertEqual(self.input.calls, [])

    def test_foreground_checker_error_blocks(self) -> None:
        def broken(_hwnd: int) -> bool:
            raise OSError("boom")

        dispatcher = ActionDispatcher(
            self.input,  # type: ignore[arg-type]
            permissions_provider=lambda: self.permissions,
            foreground_checker=broken,
            known_keys=KNOWN_KEYS,
        )
        result = dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("foreground", result.reason)

    def test_no_window_blocks(self) -> None:
        result = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=None)
        self.assertFalse(result.dispatched)
        self.assertIn("No target window", result.reason)

    def test_stale_intent_blocks(self) -> None:
        now = 100.0
        intent = _intent("press", key="x", created_at=now - 1.0)
        result = self.dispatcher.dispatch(intent, hwnd=HWND, now=now)
        self.assertFalse(result.dispatched)
        self.assertIn("stale", result.reason)

    def test_tap_error_is_a_rejection_and_frees_the_slot(self) -> None:
        self.permissions = SkillPermissions(
            allowed_keys=frozenset({"x"}), max_actions_per_second=1
        )
        self.input.fail_tap = RuntimeError("Input control is disabled.")
        now = 50.0
        result = self.dispatcher.dispatch(
            _intent("press", key="x", created_at=now), hwnd=HWND, now=now
        )
        self.assertFalse(result.dispatched)
        self.input.fail_tap = None
        result = self.dispatcher.dispatch(
            _intent("press", key="x", created_at=now), hwnd=HWND, now=now
        )
        self.assertTrue(result.dispatched, result.reason)


class RateLimitTests(KeyDispatchTestCase):
    def _press(self, now: float):
        return self.dispatcher.dispatch(
            _intent("press", key="x", created_at=now), hwnd=HWND, now=now
        )

    def test_rate_limit_and_recovery(self) -> None:
        base = 10.0
        for i in range(5):
            self.assertTrue(self._press(base + i * 0.01).dispatched)
        blocked = self._press(base + 0.1)
        self.assertFalse(blocked.dispatched)
        self.assertIn("Rate limit", blocked.reason)
        # A rejected attempt does not use up a slot; after the window it recovers.
        self.assertTrue(self._press(base + 1.0).dispatched)
        self.assertEqual(len(self.input.calls), 6)

    def test_fractional_rate(self) -> None:
        self.permissions = SkillPermissions(
            allowed_keys=frozenset({"x"}), max_actions_per_second=0.5
        )
        self.assertTrue(self._press(0.0).dispatched)
        self.assertFalse(self._press(1.9).dispatched)
        self.assertTrue(self._press(2.0).dispatched)

    def test_clicks_share_the_rate_limit_when_permissions_loaded(self) -> None:
        self.permissions = SkillPermissions(
            allowed_keys=frozenset({"x"}), max_actions_per_second=1
        )
        now = 5.0
        click = _intent("click", created_at=now, bbox=(10, 20, 30, 40))
        self.assertTrue(self.dispatcher.dispatch(click, hwnd=HWND, now=now).dispatched)
        self.assertFalse(self._press(now + 0.2).dispatched)

    def test_click_without_permissions_is_unlimited(self) -> None:
        self.permissions = None
        now = 5.0
        for _ in range(30):
            click = _intent("click", created_at=now, bbox=(10, 20, 30, 40))
            self.assertTrue(self.dispatcher.dispatch(click, hwnd=HWND, now=now).dispatched)
        self.assertEqual(self.input.calls[0], ("click", 1025, 540))

    def test_failed_click_frees_the_slot(self) -> None:
        self.permissions = SkillPermissions(
            allowed_keys=frozenset({"x"}), max_actions_per_second=1
        )
        now = 5.0
        self.input.fail_click = RuntimeError("Input control is disabled.")
        click = _intent("click", created_at=now, bbox=(10, 20, 30, 40))
        self.assertFalse(self.dispatcher.dispatch(click, hwnd=HWND, now=now).dispatched)
        self.input.fail_click = None
        self.assertTrue(self.dispatcher.dispatch(click, hwnd=HWND, now=now).dispatched)

    def test_click_does_not_need_foreground_or_keys(self) -> None:
        self.foreground = False
        click = _intent("click", bbox=(10, 20, 30, 40))
        result = self.dispatcher.dispatch(click, hwnd=HWND)
        self.assertTrue(result.dispatched, result.reason)


class HoldTests(KeyDispatchTestCase):
    def test_hold_downs_then_releases(self) -> None:
        started = time.monotonic()
        result = self.dispatcher.dispatch(_intent("hold", key="space", hold=0.15), hwnd=HWND)
        elapsed = time.monotonic() - started
        self.assertTrue(result.dispatched, result.reason)
        self.assertEqual(self.input.calls, [("down", "space"), ("up", "space")])
        self.assertGreaterEqual(elapsed, 0.14)
        self.assertNotIn("cancelled", result.reason)
        self.assertFalse(self.dispatcher.holding)

    def test_hold_over_profile_max_blocks(self) -> None:
        result = self.dispatcher.dispatch(_intent("hold", key="space", hold=2.0), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("max_hold_seconds", result.reason)
        self.assertEqual(self.input.calls, [])

    def test_hold_over_hard_cap_or_invalid_blocks(self) -> None:
        for seconds in (6.0, 0.0, -1.0, None, float("nan")):
            with self.subTest(seconds=seconds):
                result = self.dispatcher.dispatch(
                    _intent("hold", key="space", hold=seconds), hwnd=HWND
                )
                self.assertFalse(result.dispatched)
        self.assertEqual(self.input.calls, [])

    def test_hold_not_foreground_blocks(self) -> None:
        self.foreground = False
        result = self.dispatcher.dispatch(_intent("hold", key="space", hold=0.5), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertEqual(self.input.calls, [])

    def test_cancel_from_another_thread_releases_key(self) -> None:
        thread, box = self._run_in_thread(_intent("hold", key="space", hold=1.5))
        self.assertTrue(self.input.key_down_started.wait(2.0))
        self.assertTrue(self.dispatcher.holding)
        started = time.monotonic()
        self.dispatcher.cancel()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        result = box["result"]
        self.assertTrue(result.dispatched)
        self.assertIn("cancelled", result.reason)
        self.assertEqual(self.input.down, set())
        self.assertEqual(self.input.calls[-1], ("up", "space"))
        self.assertFalse(self.dispatcher.holding)

    def test_caller_cancel_event_ends_hold(self) -> None:
        event = threading.Event()
        thread, box = self._run_in_thread(
            _intent("hold", key="space", hold=1.5), cancel_event=event
        )
        self.assertTrue(self.input.key_down_started.wait(2.0))
        event.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertIn("cancelled", box["result"].reason)
        self.assertEqual(self.input.down, set())

    def test_disabling_input_mid_hold_ends_it(self) -> None:
        thread, box = self._run_in_thread(_intent("hold", key="space", hold=1.5))
        self.assertTrue(self.input.key_down_started.wait(2.0))
        self.input.set_enabled(False)  # what F8 does
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertIn("input disabled", box["result"].reason)
        self.assertEqual(self.input.down, set())
        self.assertFalse(self.dispatcher.holding)

    def test_dispatch_during_hold_is_busy(self) -> None:
        thread, box = self._run_in_thread(_intent("hold", key="space", hold=1.5))
        self.assertTrue(self.input.key_down_started.wait(2.0))
        press = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        click = self.dispatcher.dispatch(_intent("click", bbox=(1, 2, 3, 4)), hwnd=HWND)
        self.dispatcher.cancel()
        thread.join(2.0)
        self.assertFalse(press.dispatched)
        self.assertIn("Busy", press.reason)
        self.assertFalse(click.dispatched)
        self.assertIn("Busy", click.reason)
        self.assertNotIn(("tap", "x", PRESS_SECONDS), self.input.calls)
        # Once the hold is over the dispatcher is free again.
        self.assertTrue(self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND).dispatched)

    def test_cancel_with_no_hold_is_harmless(self) -> None:
        self.dispatcher.cancel()
        result = self.dispatcher.dispatch(_intent("press", key="x"), hwnd=HWND)
        self.assertTrue(result.dispatched)

    def test_key_down_failure_releases_and_rejects(self) -> None:
        self.input.fail_key_down = OSError("driver error")
        result = self.dispatcher.dispatch(_intent("hold", key="space", hold=0.5), hwnd=HWND)
        self.assertFalse(result.dispatched)
        self.assertIn("driver error", result.reason)
        self.assertEqual(self.input.calls, [("up", "space")])
        self.assertFalse(self.dispatcher.holding)

    def test_key_up_failure_is_reported_not_raised(self) -> None:
        self.input.fail_key_up = OSError("stuck")
        result = self.dispatcher.dispatch(_intent("hold", key="space", hold=0.05), hwnd=HWND)
        self.assertTrue(result.dispatched)
        self.assertIn("key_up failed", result.reason)
        self.assertFalse(self.dispatcher.holding)


if __name__ == "__main__":
    unittest.main()
