from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from typing import Any
from urllib import error as urlerror
from urllib import request

from .provider import ModelProviderError
from .types import ChatMessage, ChatResponse, EmbeddingResponse


Transport = Callable[[str, str, dict[str, object] | None, float], dict[str, Any]]


def _default_transport(
    method: str,
    url: str,
    payload: dict[str, object] | None,
    timeout: float,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError) as error:
        raise ModelProviderError(f"Ollama request failed: {error}") from error

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelProviderError("Ollama returned an invalid JSON response.") from error

    if not isinstance(parsed, dict):
        raise ModelProviderError("Ollama returned an unexpected response shape.")
    return parsed


class OllamaProvider:
    """Minimal Ollama HTTP client used by the v0.4 model router.

    It never pulls/downloads a model implicitly. Missing models are reported by
    ModelRouter so multi-gigabyte downloads remain an explicit user/admin action.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 60.0,
        transport: Transport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._transport = transport or _default_transport

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return self._transport(
            method,
            f"{self._base_url}{path}",
            payload,
            self._timeout,
        )

    def list_models(self) -> set[str]:
        payload = self._request("GET", "/api/tags")
        items = payload.get("models", [])
        if not isinstance(items, list):
            raise ModelProviderError("Ollama /api/tags response has no models list.")

        names: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("name") or item.get("model")
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
        return names

    def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, object] | None = None,
    ) -> ChatResponse:
        if not messages:
            raise ValueError("At least one chat message is required.")

        serialized_messages: list[dict[str, object]] = []
        for message in messages:
            item: dict[str, object] = {
                "role": message.role,
                "content": message.content,
            }
            if message.images_base64:
                item["images"] = list(message.images_base64)
            serialized_messages.append(item)

        body: dict[str, object] = {
            "model": model,
            "messages": serialized_messages,
            "stream": False,
        }
        if options:
            body["options"] = dict(options)

        payload = self._request("POST", "/api/chat", body)
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ModelProviderError("Ollama chat response is missing message.content.")

        actual_model = payload.get("model")
        return ChatResponse(
            provider=self.name,
            model=actual_model if isinstance(actual_model, str) else model,
            content=message["content"],
        )

    def embed(
        self,
        model: str,
        inputs: Sequence[str],
    ) -> EmbeddingResponse:
        if not inputs:
            raise ValueError("At least one embedding input is required.")

        payload = self._request(
            "POST",
            "/api/embed",
            {"model": model, "input": list(inputs)},
        )
        raw_embeddings = payload.get("embeddings")
        if not isinstance(raw_embeddings, list):
            raise ModelProviderError("Ollama embed response is missing embeddings.")

        embeddings: list[tuple[float, ...]] = []
        for row in raw_embeddings:
            if not isinstance(row, list):
                raise ModelProviderError("Ollama returned an invalid embedding row.")
            try:
                embeddings.append(tuple(float(value) for value in row))
            except (TypeError, ValueError) as error:
                raise ModelProviderError(
                    "Ollama returned a non-numeric embedding value."
                ) from error

        if len(embeddings) != len(inputs):
            raise ModelProviderError(
                "Ollama embedding count does not match the input count."
            )

        actual_model = payload.get("model")
        return EmbeddingResponse(
            provider=self.name,
            model=actual_model if isinstance(actual_model, str) else model,
            embeddings=tuple(embeddings),
        )
