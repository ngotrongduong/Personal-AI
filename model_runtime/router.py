from __future__ import annotations

from collections.abc import Mapping, Sequence

from .catalog import ModelCatalog, ModelSpec
from .provider import ModelProvider
from .types import ChatMessage, ChatResponse, EmbeddingResponse


class ModelNotAvailableError(RuntimeError):
    """No installed/configured model can currently satisfy a requested role."""


class ModelRouter:
    """Resolve model roles to the first installed compatible provider model.

    The router only chooses and invokes models. It does not download models and
    it is intentionally not wired into the frame-by-frame game reaction loop.
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        providers: Mapping[str, ModelProvider],
    ) -> None:
        self._catalog = catalog
        self._providers = dict(providers)

    def available_for_role(
        self,
        role: str,
        *,
        capability: str | None = None,
    ) -> tuple[ModelSpec, ...]:
        installed_cache: dict[str, set[str]] = {}
        available: list[ModelSpec] = []

        for spec in self._catalog.candidates(role):
            if capability is not None and capability not in spec.capabilities:
                continue
            provider = self._providers.get(spec.provider)
            if provider is None:
                continue

            installed = installed_cache.get(spec.provider)
            if installed is None:
                installed = provider.list_models()
                installed_cache[spec.provider] = installed

            if _provider_model_is_installed(spec.provider_model, installed):
                available.append(spec)

        return tuple(available)

    def select(
        self,
        role: str,
        *,
        capability: str | None = None,
    ) -> ModelSpec:
        available = self.available_for_role(role, capability=capability)
        if available:
            return available[0]

        candidates = self._catalog.candidates(role)
        expected = ", ".join(
            f"{spec.provider}:{spec.provider_model}" for spec in candidates
        )
        suffix = "" if capability is None else f" with capability {capability!r}"
        raise ModelNotAvailableError(
            f"No installed model is available for role {role!r}{suffix}. "
            f"Configured candidates: {expected}"
        )

    def chat(
        self,
        role: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, object] | None = None,
    ) -> ChatResponse:
        spec = self.select(role, capability="chat")
        provider = self._providers[spec.provider]

        merged_options: dict[str, object] = {}
        if spec.default_context_tokens is not None:
            merged_options["num_ctx"] = spec.default_context_tokens
        if options:
            merged_options.update(options)

        return provider.chat(
            spec.provider_model,
            messages,
            options=merged_options or None,
        )

    def embed(
        self,
        role: str,
        inputs: Sequence[str],
    ) -> EmbeddingResponse:
        spec = self.select(role, capability="embedding")
        provider = self._providers[spec.provider]
        return provider.embed(spec.provider_model, inputs)


def _provider_model_is_installed(
    configured_name: str,
    installed: set[str],
) -> bool:
    if configured_name in installed:
        return True

    if ":" not in configured_name and f"{configured_name}:latest" in installed:
        return True
    if configured_name.endswith(":latest") and configured_name[:-7] in installed:
        return True
    return False
