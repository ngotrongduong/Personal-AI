from __future__ import annotations

import unittest

from agent.action_dispatcher import ActionDispatcher
from agent.rule_engine import ActionIntent
from core.input_controller import InputController


def _intent(
    *,
    action: str = "click",
    target_bbox=(10, 20, 30, 40),
    created_at: float = 100.0,
) -> ActionIntent:
    return ActionIntent(
        rule_name="test_rule",
        action=action,
        detector_name="button",
        confidence=0.95,
        target_bbox=target_bbox,
        created_at=created_at,
        reason="button visible",
    )


def _fake_region(_hwnd: int) -> tuple[int, int, int, int]:
    return (1000, 500, 1400, 900)


class ActionDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.input = InputController()
        self.clicks: list[tuple[int, int]] = []
        self.input.click = self._record_click  # type: ignore[method-assign]
        self.dispatcher = ActionDispatcher(
            self.input, region_resolver=_fake_region
        )

    def _record_click(self, x=None, y=None, button="left") -> None:
        if not self.input.enabled:
            raise RuntimeError("Input control is disabled.")
        self.clicks.append((x, y))

    def test_rejects_when_input_disabled(self) -> None:
        self.input.set_enabled(False)
        result = self.dispatcher.dispatch(_intent(), hwnd=123, now=100.1)

        self.assertFalse(result.dispatched)
        self.assertIn("disabled", result.reason)
        self.assertEqual(self.clicks, [])

    def test_dispatches_click_at_bbox_center_translated_to_screen(self) -> None:
        self.input.set_enabled(True)
        result = self.dispatcher.dispatch(_intent(), hwnd=123, now=100.1)

        self.assertTrue(result.dispatched)
        # bbox (10, 20, 30, 40) center offset -> (10+15, 20+20) = (25, 40)
        # client region origin (1000, 500) -> screen (1025, 540)
        self.assertEqual(self.clicks, [(1025, 540)])

    def test_rejects_unsupported_action(self) -> None:
        self.input.set_enabled(True)
        result = self.dispatcher.dispatch(
            _intent(action="grind_forever"), hwnd=123, now=100.1
        )

        self.assertFalse(result.dispatched)
        self.assertIn("Unsupported action", result.reason)
        self.assertEqual(self.clicks, [])

    def test_rejects_stale_intent(self) -> None:
        self.input.set_enabled(True)
        result = self.dispatcher.dispatch(
            _intent(created_at=100.0), hwnd=123, now=101.0
        )

        self.assertFalse(result.dispatched)
        self.assertIn("stale", result.reason)
        self.assertEqual(self.clicks, [])

    def test_rejects_missing_target_bbox(self) -> None:
        self.input.set_enabled(True)
        result = self.dispatcher.dispatch(
            _intent(target_bbox=None), hwnd=123, now=100.1
        )

        self.assertFalse(result.dispatched)
        self.assertIn("target location", result.reason)
        self.assertEqual(self.clicks, [])

    def test_rejects_missing_hwnd(self) -> None:
        self.input.set_enabled(True)
        result = self.dispatcher.dispatch(_intent(), hwnd=None, now=100.1)

        self.assertFalse(result.dispatched)
        self.assertIn("No target window", result.reason)
        self.assertEqual(self.clicks, [])

    def test_rejects_when_region_resolver_raises(self) -> None:
        def _broken_region(_hwnd: int) -> tuple[int, int, int, int]:
            raise OSError("window closed")

        self.input.set_enabled(True)
        dispatcher = ActionDispatcher(self.input, region_resolver=_broken_region)
        result = dispatcher.dispatch(_intent(), hwnd=123, now=100.1)

        self.assertFalse(result.dispatched)
        self.assertIn("unavailable", result.reason)
        self.assertEqual(self.clicks, [])

    def test_race_where_input_disabled_between_gate_check_and_click(self) -> None:
        # Simulate F8 firing after the initial `enabled` gate check but before
        # InputController.click() actually runs -- the dispatcher must treat
        # the resulting RuntimeError as a normal rejection, not a crash.
        self.input.set_enabled(True)

        real_click = self._record_click

        def _click_then_disable(x=None, y=None, button="left"):
            self.input.enabled = False
            return real_click(x=x, y=y, button=button)

        self.input.click = _click_then_disable  # type: ignore[method-assign]
        result = self.dispatcher.dispatch(_intent(), hwnd=123, now=100.1)

        self.assertFalse(result.dispatched)
        self.assertIn("disabled", result.reason)

    def test_max_intent_age_seconds_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            ActionDispatcher(self.input, max_intent_age_seconds=0.0)


if __name__ == "__main__":
    unittest.main()
