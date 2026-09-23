"""Application lifecycle wrapper for the optional LLM planner scheduler."""

from __future__ import annotations

from collections.abc import Callable
import threading

from .game_state import GameState
from .llm_planner import LlmPlanner, PlannerCancelledError
from .ollama_client import OllamaClient
from .planner_config import PlannerConfig
from .planner_scheduler import PlannerScheduler
from .rule_engine import RuleEngine

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

    @property
    def is_running(self) -> bool:
        """Whether this controller currently owns a running scheduler."""

        return self._scheduler is not None and self._scheduler.is_running

    def start(self, rule_engine: RuleEngine, config: PlannerConfig) -> bool:
        """Start a configured planner scheduler, replacing any prior scheduler."""

        if not config.enabled or config.ollama is None:
            return False

        self.stop()
        client = self._client_factory(config.ollama)
        rule_control = _CancellableRuleControl(rule_engine)
        planner = LlmPlanner(client, rule_control)
        scheduler = self._scheduler_factory(
            planner,
            self._state,
            interval_seconds=config.interval_seconds,
        )
        scheduler.start()
        self._scheduler = scheduler
        self._rule_control = rule_control
        return True

    def stop(self) -> None:
        """Stop and discard the current scheduler, if any."""

        if self._rule_control is not None:
            self._rule_control.cancel()
            self._rule_control = None

        if self._scheduler is None:
            return

        self._scheduler.stop(join_timeout=0.0)
        self._scheduler = None
