from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One model/runtime entry in the versioned local catalog."""

    id: str
    display_name: str
    provider: str
    provider_model: str
    roles: tuple[str, ...]
    capabilities: frozenset[str]
    enabled_by_default: bool
    source_repo: str
    license: str
    notes: str = ""
    download_gb: float | None = None
    max_context_tokens: int | None = None
    default_context_tokens: int | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("id", self.id),
            ("display_name", self.display_name),
            ("provider", self.provider),
            ("provider_model", self.provider_model),
        ):
            if not value.strip():
                raise ValueError(f"Model {label} cannot be empty.")

        if not self.roles:
            raise ValueError(f"Model {self.id!r} must have at least one role.")
        if not self.capabilities:
            raise ValueError(f"Model {self.id!r} must have at least one capability.")

        if self.download_gb is not None and self.download_gb <= 0:
            raise ValueError("download_gb must be positive when provided.")
        if self.max_context_tokens is not None and self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive when provided.")
        if self.default_context_tokens is not None:
            if self.default_context_tokens <= 0:
                raise ValueError("default_context_tokens must be positive when provided.")
            if (
                self.max_context_tokens is not None
                and self.default_context_tokens > self.max_context_tokens
            ):
                raise ValueError(
                    "default_context_tokens cannot exceed max_context_tokens."
                )

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ModelSpec":
        return cls(
            id=str(data["id"]),
            display_name=str(data["display_name"]),
            provider=str(data["provider"]),
            provider_model=str(data["provider_model"]),
            roles=tuple(str(value) for value in data["roles"]),
            capabilities=frozenset(str(value) for value in data["capabilities"]),
            enabled_by_default=bool(data.get("enabled_by_default", True)),
            source_repo=str(data.get("source_repo", "")),
            license=str(data.get("license", "")),
            notes=str(data.get("notes", "")),
            download_gb=(
                None if data.get("download_gb") is None else float(data["download_gb"])
            ),
            max_context_tokens=(
                None
                if data.get("max_context_tokens") is None
                else int(data["max_context_tokens"])
            ),
            default_context_tokens=(
                None
                if data.get("default_context_tokens") is None
                else int(data["default_context_tokens"])
            ),
        )


class ModelCatalog:
    """Versioned role -> model preference list with validation."""

    def __init__(
        self,
        *,
        schema_version: int,
        models: list[ModelSpec],
        roles: dict[str, tuple[str, ...]],
    ) -> None:
        if schema_version <= 0:
            raise ValueError("schema_version must be positive.")
        self.schema_version = schema_version

        by_id: dict[str, ModelSpec] = {}
        for model in models:
            if model.id in by_id:
                raise ValueError(f"Duplicate model id: {model.id}")
            by_id[model.id] = model
        self._models = by_id

        normalized_roles: dict[str, tuple[str, ...]] = {}
        for role, candidate_ids in roles.items():
            if not role.strip():
                raise ValueError("Role name cannot be empty.")
            if not candidate_ids:
                raise ValueError(f"Role {role!r} must have at least one candidate.")
            for model_id in candidate_ids:
                if model_id not in by_id:
                    raise ValueError(
                        f"Role {role!r} references unknown model {model_id!r}."
                    )
                if role not in by_id[model_id].roles:
                    raise ValueError(
                        f"Model {model_id!r} does not declare role {role!r}."
                    )
            normalized_roles[role] = tuple(candidate_ids)
        self._roles = normalized_roles

    @classmethod
    def from_file(cls, path: str | Path) -> "ModelCatalog":
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Model catalog root must be an object.")

        raw_models = data.get("models")
        raw_roles = data.get("roles")
        if not isinstance(raw_models, list):
            raise ValueError("Model catalog 'models' must be a list.")
        if not isinstance(raw_roles, dict):
            raise ValueError("Model catalog 'roles' must be an object.")

        models = [ModelSpec.from_mapping(item) for item in raw_models]
        roles = {
            str(role): tuple(str(model_id) for model_id in candidate_ids)
            for role, candidate_ids in raw_roles.items()
        }
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            models=models,
            roles=roles,
        )

    @property
    def models(self) -> tuple[ModelSpec, ...]:
        return tuple(self._models.values())

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self._roles)

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self._models[model_id]
        except KeyError as error:
            raise KeyError(f"Unknown model id: {model_id}") from error

    def candidates(
        self,
        role: str,
        *,
        include_disabled: bool = False,
    ) -> tuple[ModelSpec, ...]:
        ids = self._roles.get(role)
        if ids is None:
            raise KeyError(f"Unknown model role: {role}")

        candidates = tuple(self._models[model_id] for model_id in ids)
        if include_disabled:
            return candidates
        return tuple(model for model in candidates if model.enabled_by_default)
