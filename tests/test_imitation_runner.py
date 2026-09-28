from __future__ import annotations

from pathlib import Path
import threading
import time
import unittest

import numpy as np

from imitation.demo_bank import DemoBank
from imitation.policy import Abstention, Proposal
from imitation.runner import BankLoaded, ImitationRunner, PolicyOutcome, build_bank


def _drain_until(runner: ImitationRunner, count: int = 1) -> list:
    results: list = []
    deadline = time.monotonic() + 5.0
    while len(results) < count:
        if time.monotonic() > deadline:
            raise AssertionError("runner produced no result")
        results.extend(runner.drain())
        time.sleep(0.005)
    return results


PROPOSAL = Proposal(0.5, 0.25, 0.95, 0.9, 2, 1.7, "s1", 3.0, "matched")


class FakePolicy:
    def __init__(self, outcome=PROPOSAL, *, gate: threading.Event | None = None, error=None):
        self.outcome = outcome
        self.gate = gate
        self.error = error
        self.calls: list[tuple] = []

    def propose(self, frame, *, now, recent=()):
        self.calls.append((frame.shape, now, recent))
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.error is not None:
            raise self.error
        return self.outcome


class ImitationRunnerTests(unittest.TestCase):
    def test_load_returns_a_bank_with_its_load_id(self) -> None:
        bank = DemoBank.from_clicks(())
        seen = []

        def builder(root, title, names):
            seen.append((root, title, names))
            return bank, ("a", "b")

        runner = ImitationRunner(bank_builder=builder)
        load_id = runner.load(Path("rec"), "Merchant Guilds", ["a"])

        [result] = _drain_until(runner)
        self.assertEqual(result, BankLoaded(load_id, bank, ("a", "b")))
        self.assertEqual(seen, [(Path("rec"), "Merchant Guilds", ("a",))])

    def test_load_errors_are_reported_not_raised(self) -> None:
        def builder(root, title, names):
            raise OSError("disk gone")

        runner = ImitationRunner(bank_builder=builder)
        runner.load(Path("rec"), "x")

        [result] = _drain_until(runner)
        self.assertIsNone(result.bank)
        self.assertIn("disk gone", result.error)

    def test_one_load_at_a_time(self) -> None:
        gate = threading.Event()

        def builder(root, title, names):
            gate.wait(5.0)
            return DemoBank.from_clicks(()), ()

        runner = ImitationRunner(bank_builder=builder)
        first = runner.load(Path("rec"), "x")
        self.assertIsNotNone(first)
        self.assertTrue(runner.loading)
        self.assertIsNone(runner.load(Path("rec"), "x"))
        gate.set()
        [result] = _drain_until(runner)
        self.assertEqual(result.load_id, first)
        deadline = time.monotonic() + 5.0
        while runner.loading and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertNotEqual(runner.load(Path("rec"), "x"), first)
        gate.set()
        _drain_until(runner)

    def test_propose_passes_frame_time_and_recent_taps(self) -> None:
        runner = ImitationRunner()
        policy = FakePolicy()
        frame = np.zeros((8, 12, 3), dtype=np.uint8)

        self.assertTrue(runner.propose(policy, frame, now=7.0, recent=[(0.1, 0.2, 6.0)]))

        [result] = _drain_until(runner)
        self.assertEqual(result, PolicyOutcome(runner.generation, PROPOSAL, observed_at=7.0))
        self.assertEqual(policy.calls, [((8, 12, 3), 7.0, ((0.1, 0.2, 6.0),))])

    def test_one_policy_call_at_a_time_and_reset_marks_it_stale(self) -> None:
        runner = ImitationRunner()
        gate = threading.Event()
        policy = FakePolicy(Abstention("unknown screen (best 0.10)"), gate=gate)
        frame = np.zeros((4, 4, 3), dtype=np.uint8)

        self.assertTrue(runner.propose(policy, frame, now=1.0))
        self.assertTrue(runner.proposing)
        self.assertFalse(runner.propose(policy, frame, now=2.0))
        started = runner.generation
        self.assertEqual(runner.reset(), started + 1)
        gate.set()

        [result] = _drain_until(runner)
        self.assertEqual(result.generation, started)
        self.assertNotEqual(result.generation, runner.generation)

    def test_policy_errors_are_reported_not_raised(self) -> None:
        runner = ImitationRunner()
        runner.propose(FakePolicy(error=ValueError("bad frame")), np.zeros((4, 4, 3)), now=1.0)

        [result] = _drain_until(runner)
        self.assertIsNone(result.outcome)
        self.assertIn("bad frame", result.error)

    def test_build_bank_on_an_empty_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bank, sessions = build_bank(Path(tmp), "Merchant Guilds", ())
        self.assertEqual(bank.clicks, ())
        self.assertEqual(sessions, ())


if __name__ == "__main__":
    unittest.main()
