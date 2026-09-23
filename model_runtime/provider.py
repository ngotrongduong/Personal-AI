from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from .types import ChatMessage, ChatResponse, EmbeddingResponse


class ModelProviderError(RuntimeError):
    """A local/remote model runtime could not satisfy a provider request."""


@runtime_checkable
class ModelProvider(Protocol):
    """Small provider surface needed by the role router."""

    name: str

    def list_models(self) -> set[str]:
        """Return provider model names currently available on this runtime."""

    def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, object] | None = None,
    ) -> ChatResponse:
        """Run one non-streaming chat request."""

    def embed(
        self,
        model: str,
        inputs: Sequence[str],
    ) -> EmbeddingResponse:
        """Embed one or more text strings."""
