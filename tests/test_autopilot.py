from __future__ import annotations

import unittest

from agent.autopilot import (
    HARD_MAX_AUTO_STEPS,
    Autopilot,
    OfferResult,
    validate_auto_max_steps,
)
from agent.proposal_mailbox import SkillProposal


def _p(name: str = "type_x") -> SkillProposal:
    return SkillProposal(name, "reason", created_at=0.0, generation=1)


class ApproveModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pilot = Autopilot(ttl_seconds=10.0)

    def test_defaults_to_approve_mode(self) -> None:
        self.assertEqual(self.pilot.mode, "approve")
        self.assertFalse(self.pilot.busy)

    def test_offer_waits_for_approval(self) -> None:
        proposal = _p()

        self.assertEqual(self.pilot.offer(proposal, now=0.0), OfferResult.AWAIT)
        self.assertEqual(self.pilot.pending, proposal)
        self.assertIsNone(self.pilot.running)
        self.assertEqual(self.pilot.seconds_left(4.0), 6.0)

    def test_approve_hands_out_the_proposal_once(self) -> None:
        proposal = _p()
        self.pilot.offer(proposal, now=0.0)

        self.assertEqual(self.pilot.approve(now=9.0), proposal)
        self.assertIsNone(self.pilot.pending)
        self.assertEqual(self.pilot.running, proposal)
        self.assertIsNone(self.pilot.approve(now=9.0))

    def test_approve_after_ttl_is_refused_and_expire_drops(self) -> None:
        proposal = _p()
        self.pilot.offer(proposal, now=0.0)

        self.assertIsNone(self.pilot.expire(now=9.9))
        self.assertIsNone(self.pilot.approve(now=10.0))
        self.assertEqual(self.pilot.expire(now=10.0), proposal)
        self.assertFalse(self.pilot.busy)

    def test_reject(self) -> None:
        proposal = _p()
        self.pilot.offer(proposal, now=0.0)

        self.assertEqual(self.pilot.reject(), proposal)
        self.assertFalse(self.pilot.busy)
        self.assertIsNone(self.pilot.reject())

    def test_offer_while_pending_or_running_is_dropped(self) -> None:
        self.pilot.offer(_p("a"), now=0.0)
        self.assertEqual(self.pilot.offer(_p("b"), now=1.0), OfferResult.DROPPED)
        self.assertEqual(self.pilot.pending.skill_name, "a")

        self.pilot.approve(now=1.0)
        self.assertEqual(self.pilot.offer(_p("c"), now=2.0), OfferResult.DROPPED)

        self.assertIsNone(self.pilot.record_result(True))
        self.assertEqual(self.pilot.offer(_p("d"), now=3.0), OfferResult.AWAIT)

    def test_failures_never_disarm_in_approve_mode(self) -> None:
        for _ in range(5):
            self.pilot.offer(_p(), now=0.0)
            self.pilot.approve(now=0.0)
            self.assertIsNone(self.pilot.record_result(False))
        self.assertEqual(self.pilot.mode, "approve")

    def test_rejects_bad_construction(self) -> None:
        with self.assertRaises(ValueError):
            Autopilot(ttl_seconds=0)
        with self.assertRaises(ValueError):
            Autopilot(max_consecutive_failures=0)


class AutoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pilot = Autopilot()

    def _step(self, ok: bool = True) -> str | None:
        self.assertEqual(self.pilot.offer(_p(), now=0.0), OfferResult.EXECUTE)
        return self.pilot.record_result(ok)

    def test_auto_executes_and_counts_steps(self) -> None:
        self.pilot.arm_auto(5)

        self.assertIsNone(self._step())
        self.assertEqual(self.pilot.steps_taken, 1)
        self.assertIsNone(self.pilot.pending)

    def test_step_cap_disarms_after_the_last_step(self) -> None:
        self.pilot.arm_auto(3)

        self.assertIsNone(self._step())
        self.assertIsNone(self._step())
        self.assertEqual(self._step(), "reached the 3-step limit")
        self.assertEqual(self.pilot.mode, "approve")
        self.assertEqual(self.pilot.offer(_p(), now=0.0), OfferResult.AWAIT)

    def test_three_failures_in_a_row_disarm(self) -> None:
        self.pilot.arm_auto(20)

        self.assertIsNone(self._step(False))
        self.assertIsNone(self._step(False))
        self.assertIsNone(self._step(True))  # resets the streak
        self.assertIsNone(self._step(False))
        self.assertIsNone(self._step(False))
        self.assertEqual(self._step(False), "3 failed steps in a row")
        self.assertEqual(self.pilot.mode, "approve")

    def test_failure_streak_from_approve_mode_is_reset_on_arm(self) -> None:
        for _ in range(2):
            self.pilot.offer(_p(), now=0.0)
            self.pilot.approve(now=0.0)
            self.pilot.record_result(False)
        self.pilot.arm_auto(10)

        self.assertIsNone(self._step(False))
        self.assertEqual(self.pilot.mode, "auto")

    def test_rearm_resets_the_counter(self) -> None:
        self.pilot.arm_auto(2)
        self._step()
        self.pilot.arm_auto(2)

        self.assertEqual(self.pilot.steps_taken, 0)
        self.assertIsNone(self._step())

    def test_disarm_reports_only_when_auto_was_on(self) -> None:
        self.assertIsNone(self.pilot.disarm("F8"))
        self.pilot.arm_auto(5)

        self.assertEqual(self.pilot.disarm("F8"), "F8")
        self.assertEqual(self.pilot.mode, "approve")

    def test_disarm_keeps_the_running_step_until_its_result(self) -> None:
        self.pilot.arm_auto(5)
        self.pilot.offer(_p(), now=0.0)
        self.pilot.disarm("input control turned off")

        self.assertIsNotNone(self.pilot.running)
        self.assertIsNone(self.pilot.record_result(False))
        self.assertFalse(self.pilot.busy)

    def test_reset_forgets_everything(self) -> None:
        self.pilot.offer(_p("a"), now=0.0)
        self.pilot.arm_auto(5)

        self.assertEqual(self.pilot.reset().skill_name, "a")
        self.assertEqual(self.pilot.mode, "approve")
        self.assertFalse(self.pilot.busy)

    def test_arm_validates_max_steps(self) -> None:
        for bad in (0, -1, HARD_MAX_AUTO_STEPS + 1, True, 2.0, "5", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.pilot.arm_auto(bad)  # type: ignore[arg-type]
                self.assertEqual(self.pilot.mode, "approve")
        self.pilot.arm_auto(HARD_MAX_AUTO_STEPS)
        self.assertEqual(self.pilot.max_steps, HARD_MAX_AUTO_STEPS)

    def test_validate_auto_max_steps(self) -> None:
        self.assertEqual(validate_auto_max_steps(1), 1)
        self.assertEqual(validate_auto_max_steps(100), 100)
        with self.assertRaises(ValueError):
            validate_auto_max_steps(False)


if __name__ == "__main__":
    unittest.main()
