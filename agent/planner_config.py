"""Planner settings parsed from an optional game-profile configuration block."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .autopilot import DEFAULT_AUTO_MAX_STEPS, validate_auto_max_steps
from .llm_planner import MAX_GOAL_LENGTH
from .ollama_client import OllamaClientConfig


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Optional local-planner settings, disabled unless explicitly enabled.

    ``goal`` is the free-text objective shown to the planner (v0.7).
    ``auto_max_steps`` is the default step cap offered when the user arms auto
    mode; auto mode itself is never stored.
    """

    enabled: bool = False
    ollama: OllamaClientConfig | None = None
    interval_seconds: float = 5.0
    goal: str = ""
    auto_max_steps: int = DEFAULT_AUTO_MAX_STEPS

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str):
            raise ValueError("goal must be a string.")
        if len(self.goal) > MAX_GOAL_LENGTH:
            raise ValueError(f"goal must be at most {MAX_GOAL_LENGTH} characters.")
        validate_auto_max_steps(self.auto_max_steps)
        if (
            not isinstance(self.interval_seconds, (int, float))
            or isinstance(self.interval_seconds, bool)
            or self.interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be a positive real number.")
        if self.enabled is True and self.ollama is None:
            raise ValueError("An enabled planner requires an Ollama configuration.")


def load_planner_config(profile: Mapping[str, object]) -> PlannerConfig:
    """Load the optional strict ``planner`` block from a game-profile mapping."""

    if "planner" not in profile:
        return PlannerConfig()

    planner = profile["planner"]
    if not isinstance(planner, dict):
        raise ValueError("Game profile 'planner' must be an object.")

    recognized_fields = {
        "enabled",
        "model",
        "host",
        "port",
        "timeout_seconds",
        "interval_seconds",
        "goal",
        "auto_max_steps",
    }
    unknown_fields = set(planner) - recognized_fields
    if unknown_fields:
        raise ValueError(f"Unrecognized planner field(s): {sorted(unknown_fields)!r}.")

    enabled = planner.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("Planner 'enabled' must be a boolean.")
    if enabled and "model" not in planner:
        raise ValueError("An enabled planner requires a 'model'.")

    interval_seconds = planner.get("interval_seconds", 5.0)
    client_fields = {"model", "host", "port", "timeout_seconds"}
    client_options = {field: planner[field] for field in client_fields if field in planner}
    if client_options and "model" not in client_options:
        raise ValueError("Planner Ollama settings require a 'model'.")

    ollama = OllamaClientConfig(**client_options) if client_options else None
    return PlannerConfig(
        enabled=enabled,
        ollama=ollama,
        interval_seconds=interval_seconds,
        goal=planner.get("goal", ""),
        auto_max_steps=planner.get("auto_max_steps", DEFAULT_AUTO_MAX_STEPS),
    )
