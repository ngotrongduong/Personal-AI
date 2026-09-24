"""v0.7 task 3: the scheduler's should_plan gate and the controller's proposal wiring."""

from __future__ import annotations

import threading
import time
import unittest

from agent.game_state import GameState
from agent.llm_planner import PlannerCancelledError, SkillSummary
from agent.ollama_client import OllamaClientConfig, OllamaResult
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.planner_scheduler import PlannerCycleReport, PlannerScheduler
from agent.proposal_mailbox import ProposalMailbox
from agent.rule_engine import RuleEngine, VisibilityRule
from agent.step_history import StepHistory


class CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0
        self.called = threading.Event()

    def plan_once(self, _state: GameState) -> object:
        self.calls += 1
        self.called.set()
        return "noop"


class Gate:
    def __init__(self, open_: bool = False) -> None:
        self.open = open_
        self.checks = 0
        self.checked = threading.Event()

    def __call__(self) -> bool:
        self.checks += 1
        self.checked.set()
        return self.open


class SchedulerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.reports: list[PlannerCycleReport] = []
        self.schedulers: list[PlannerScheduler] = []

    def tearDown(self) -> None:
        for scheduler in self.schedulers:
            scheduler.stop()

    def _start(self, planner, gate) -> PlannerScheduler:
        scheduler = PlannerScheduler(
            planner,
            self.state,
            interval_seconds=0.02,
            on_cycle=self.reports.append,
            should_plan=gate,
        )
        self.schedulers.append(scheduler)
        scheduler.start()
        return scheduler

    def test_closed_gate_skips_the_planner_and_reports_nothing(self) -> None:
        planner, gate = CountingPlanner(), Gate(open_=False)
        self._start(planner, gate)

        self.assertTrue(gate.checked.wait(1.0))
        time.sleep(0.1)

        self.assertEqual(planner.calls, 0)
        self.assertEqual(self.reports, [])

    def test_gate_opening_lets_cycles_through(self) -> None:
        planner, gate = CountingPlanner(), Gate(open_=False)
        self._start(planner, gate)
        self.assertTrue(gate.checked.wait(1.0))

        gate.open = True

        self.assertTrue(planner.called.wait(2.0))
        deadline = time.monotonic() + 2.0
        while not self.reports and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.reports), 1)

    def test_raising_gate_fails_closed(self) -> None:
        planner = CountingPlanner()
        raised = threading.Event()

        def broken_gate() -> bool:
            raised.set()
            raise RuntimeError("gate bug")

        with self.assertLogs("agent.planner_scheduler", level="ERROR"):
            scheduler = self._start(planner, broken_gate)
            self.assertTrue(raised.wait(1.0))
            time.sleep(0.05)
            scheduler.stop(join_timeout=1.0)

        self.assertEqual(planner.calls, 0)

    def test_stop_interrupts_a_closed_gate_wait(self) -> None:
        scheduler = self._start(CountingPlanner(), Gate(open_=False))

        started = time.monotonic()
        scheduler.stop(join_timeout=1.0)

        self.assertLess(time.monotonic() - started, 0.9)
        self.assertFalse(scheduler.is_running)


class FakeClient:
    """Answers every prompt with a run_skill directive for ``type_x``."""

    def __init__(self, config: OllamaClientConfig) -> None:
        self.config = config

    def generate(self, prompt: str) -> OllamaResult:
        return OllamaResult(text='{"type":"run_skill","skill":"type_x","reason":"test"}')


class FakeCatalog:
    def runnable_skills(self) -> list[SkillSummary]:
        return [SkillSummary("type_x", "press", "presses key x")]


class RecordingScheduler:
    def __init__(self, planner, state, **options) -> None:
        self.planner = planner
        self.state = state
        self.options = options
        self.stopped = False

    @property
    def is_running(self) -> bool:
        return not self.stopped

    def start(self) -> bool:
        return True

    def stop(self, *, join_timeout: float = 1.0) -> None:
        self.stopped = True


class ControllerProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.schedulers: list[RecordingScheduler] = []
        self.controller = PlannerController(
            self.state, client_factory=FakeClient, scheduler_factory=self._make
        )
        self.engine = RuleEngine([VisibilityRule("click_ok", "ok_button", "click")])
        self.config = PlannerConfig(
            enabled=True, ollama=OllamaClientConfig(model="m"), interval_seconds=1.0
        )
        self.mailbox = ProposalMailbox()

    def _make(self, planner, state, **options) -> RecordingScheduler:
        scheduler = RecordingScheduler(planner, state, **options)
        self.schedulers.append(scheduler)
        return scheduler

    def _start(self, **overrides) -> RecordingScheduler:
        options = dict(
            skills=FakeCatalog(),
            history=StepHistory(),
            goal="type x",
            mailbox=self.mailbox,
            clock=lambda: 42.0,
        )
        options.update(overrides)
        self.assertTrue(self.controller.start(self.engine, self.config, **options))
        return self.schedulers[-1]

    def test_planner_proposal_reaches_the_mailbox_with_generation(self) -> None:
        scheduler = self._start()

        outcome = scheduler.planner.plan_once(self.state)

        self.assertEqual(outcome.message, "proposed type_x: test")
        proposal = self.mailbox.take()
        self.assertEqual(
            (proposal.skill_name, proposal.reason, proposal.created_at, proposal.generation),
            ("type_x", "test", 42.0, self.controller.generation),
        )

    def test_generation_increments_per_start(self) -> None:
        self._start()
        first = self.controller.generation
        self._start()

        self.assertEqual(self.controller.generation, first + 1)

    def test_proposal_after_stop_is_cancelled_and_never_posted(self) -> None:
        scheduler = self._start()
        self.controller.stop()

        with self.assertRaises(PlannerCancelledError):
            scheduler.planner.plan_once(self.state)
        self.assertFalse(self.mailbox.occupied)

    def test_stop_clears_a_proposal_posted_before_stop(self) -> None:
        scheduler = self._start()
        scheduler.planner.plan_once(self.state)
        self.assertTrue(self.mailbox.occupied)

        self.controller.stop()

        self.assertFalse(self.mailbox.occupied)
        self.assertIsNone(self.mailbox.take())

    def test_old_start_cannot_post_after_restart(self) -> None:
        old = self._start()
        self._start()

        with self.assertRaises(PlannerCancelledError):
            old.planner.plan_once(self.state)
        self.assertFalse(self.mailbox.occupied)

    def test_without_mailbox_run_skill_is_rejected(self) -> None:
        scheduler = self._start(mailbox=None)

        outcome = scheduler.planner.plan_once(self.state)

        self.assertIn("no change, rejected:", outcome.message)

    def test_should_plan_is_passed_only_when_given(self) -> None:
        def gate() -> bool:
            return True

        self.assertIs(self._start(should_plan=gate).options["should_plan"], gate)
        self.assertNotIn("should_plan", self._start().options)


if __name__ == "__main__":
    unittest.main()
