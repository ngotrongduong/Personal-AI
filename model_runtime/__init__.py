"""Local model catalog, providers, and role-based routing for Personal Game AI."""

from .catalog import ModelCatalog, ModelSpec
from .router import ModelNotAvailableError, ModelRouter
from .types import ChatMessage, ChatResponse, EmbeddingResponse

__all__ = [
    "ChatMessage",
    "ChatResponse",
    "EmbeddingResponse",
    "ModelCatalog",
    "ModelNotAvailableError",
    "ModelRouter",
    "ModelSpec",
]
