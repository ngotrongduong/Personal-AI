from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Provider-neutral chat message.

    images_base64 contains raw base64 image payloads (without a data: prefix).
    Keeping images attached to a message matches Ollama's native chat schema.
    """

    role: str
    content: str
    images_base64: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("Chat message role cannot be empty.")
        if not isinstance(self.content, str):
            raise TypeError("Chat message content must be a string.")


@dataclass(frozen=True, slots=True)
class ChatResponse:
    provider: str
    model: str
    content: str


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    provider: str
    model: str
    embeddings: tuple[tuple[float, ...], ...]
