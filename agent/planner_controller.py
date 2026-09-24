"""Application lifecycle wrapper for the optional LLM planner scheduler."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time

from .game_state import GameState
from .llm_planner import LlmPlanner, PlannerCancelledError, SkillCatalog
from .ollama_client import OllamaClient
from .planner_config import PlannerConfig
from .planner_scheduler import CycleCallback, PlanGate, PlannerScheduler
from .proposal_mailbox import ProposalMailbox, SkillProposal
from .rule_engine import RuleEngine
from .step_history import StepHistory


class _CancellableRuleControl:
    """Prevent an in-flight planner from changing rules after controller shutdown."""

    def __init__(self, rule_engine: RuleEngine) -> None:
        self._rule_engine = rule_engine
        self._lock = threading.Lock()
        self._cancelled = False

    @property
    def rules(self):
        with self._lock:
            return self._rule_engine.rules

    def is_rule_enabled(self, name: str) -> bool:
        with self._lock:
            return self._rule_engine.is_rule_enabled(name)

    def enable_rule(self, name: str) -> None:
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._rule_engine.enable_rule(name)

        if cancelled:
            raise PlannerCancelledError()

    def disable_rule(self, name: str) -> None:
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._rule_engine.disable_rule(name)

        if cancelled:
            raise PlannerCancelledError()

    def cancel(self) -> None:
        """Make all future rule mutations through this control a no-op."""

        with self._lock:
            self._cancelled = True


class _CancellableProposalSink:
    """Post planner proposals to the mailbox until the controller stops.

    Each sink stamps proposals with the generation of the planner start that
    created it, so the Tk thread can drop anything left from an older start.
    """

    def __init__(
        self,
        mailbox: ProposalMailbox,
        generation: int,
        clock: Callable[[], float],
    ) -> None:
        self._mailbox = mailbox
        self._generation = generation
        self._clock = clock
        self._lock = threading.Lock()
        self._cancelled = False

    def propose(self, skill_name: str, reason: str) -> bool:
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                return self._mailbox.post(
                    SkillProposal(skill_name, reason, self._clock(), self._generation)
                )

        raise PlannerCancelledError()

    def cancel(self) -> None:
        """Make every later proposal raise instead of reaching the mailbox."""

        with self._lock:
            self._cancelled = True


class PlannerController:
    """Own the optional planner scheduler without participating in dispatch."""

    def __init__(
        self,
        state: GameState,
        *,
        client_factory: Callable[..., OllamaClient] = OllamaClient,
        scheduler_factory: Callable[..., PlannerScheduler] = PlannerScheduler,
    ) -> None:
        self._state = state
        self._client_factory = client_factory
        self._scheduler_factory = scheduler_factory
        self._scheduler: PlannerScheduler | None = None
        self._rule_control: _CancellableRuleControl | None = None
        self._proposal_sink: _CancellableProposalSink | None = None
        self._mailbox: ProposalMailbox | None = None
        self._generation = 0

    @property
    def is_running(self) -> bool:
        """Whether this controller currently owns a running scheduler."""

        return self._scheduler is not None and self._scheduler.is_running

    @property
    def generation(self) -> int:
        """Increments on every successful start; proposals carry the value."""

        return self._generation

    def start(
        self,
        rule_engine: RuleEngine,
        config: PlannerConfig,
        *,
        on_cycle: CycleCallback | None = None,
        skills: SkillCatalog | None = None,
        history: StepHistory | None = None,
        goal: str | Callable[[], str] = "",
        mailbox: ProposalMailbox | None = None,
        should_plan: PlanGate | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Start a configured planner scheduler, replacing any prior scheduler.

        ``on_cycle`` is an observation-only hook invoked on the scheduler
        thread after every cycle; it receives no handle to rules or input.

        With both ``skills`` and ``mailbox`` the planner may propose skills.
        Proposals only reach ``mailbox``; after :meth:`stop` a late proposal
        raises :class:`PlannerCancelledError` instead. ``should_plan`` gates
        each cycle on the scheduler thread, so it must be thread-safe and must
        not read Tk variables. :meth:`stop` clears ``mailbox``.
        """

        if not config.enabled or config.ollama is None:
            return False

        self.stop()
        self._generation += 1
        client = self._client_factory(config.ollama)
        rule_control = _CancellableRuleControl(rule_engine)
        sink = (
            _CancellableProposalSink(mailbox, self._generation, clock)
            if mailbox is not None and skills is not None
            else None
        )
        planner = LlmPlanner(
            client,
            rule_control,
            skills=skills if sink is not None else None,
            history=history,
            goal=goal,
            proposals=sink,
        )
        scheduler_options: dict[str, object] = {
            "interval_seconds": config.interval_seconds,
            "on_cycle": on_cycle,
        }
        if should_plan is not None:
            scheduler_options["should_plan"] = should_plan
        scheduler = self._scheduler_factory(planner, self._state, **scheduler_options)
        scheduler.start()
        self._scheduler = scheduler
        self._rule_control = rule_control
        self._proposal_sink = sink
        self._mailbox = mailbox if sink is not None else None
        return True

    def stop(self) -> None:
        """Stop and discard the current scheduler, if any."""

        if self._rule_control is not None:
            self._rule_control.cancel()
            self._rule_control = None

        if self._proposal_sink is not None:
            # Cancel first: once cancel() returns no post can follow, so the
            # clear below cannot race a late proposal from this start.
            self._proposal_sink.cancel()
            self._proposal_sink = None
            if self._mailbox is not None:
                self._mailbox.clear()
                self._mailbox = None

        if self._scheduler is None:
            return

        self._scheduler.stop(join_timeout=0.0)
        self._scheduler = None
