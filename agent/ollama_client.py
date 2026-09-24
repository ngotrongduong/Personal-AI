"""Small, dependency-free client for Ollama's non-streaming generate endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from http.client import HTTPException
import json
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OllamaErrorKind(str, Enum):
    """Categories callers can use to decide how to handle an unsuccessful request."""

    CONNECTION = "connection"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    RESPONSE_FORMAT = "response_format"


@dataclass(frozen=True, slots=True)
class OllamaError:
    """A request failure that is safe for a planner loop to inspect and log."""

    kind: OllamaErrorKind
    message: str
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class OllamaResult:
    """The text produced by Ollama, or an error describing why none was available."""

    text: str | None = None
    error: OllamaError | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.error is None):
            raise ValueError("OllamaResult must contain exactly one of text or error.")

    @property
    def successful(self) -> bool:
        """Whether this request produced usable generated text."""

        return self.error is None


@dataclass(frozen=True, slots=True)
class OllamaClientConfig:
    """Connection settings for one local Ollama service."""

    model: str
    host: str = "localhost"
    port: int = 11434
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Ollama model must be a non-empty string.")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("Ollama host must be a non-empty string.")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ValueError("Ollama port must be an integer from 1 through 65535.")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Ollama timeout_seconds must be positive.")


Transport = Callable[[Request, float], bytes]


class OllamaClient:
    """Calls Ollama without letting HTTP failures escape into a caller's loop."""

    def __init__(
        self,
        config: OllamaClientConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or _default_transport

    def generate(self, prompt: str) -> OllamaResult:
        """Generate one JSON-formatted response for *prompt* without streaming."""

        if not isinstance(prompt, str):
            raise TypeError("Ollama prompt must be a string.")

        payload = json.dumps(
            {
                "model": self._config.model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "think": False,
            }
        ).encode("utf-8")
        request = Request(
            self._endpoint_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            body = self._transport(request, float(self._config.timeout_seconds))
        except HTTPError as error:
            return OllamaResult(
                error=OllamaError(
                    OllamaErrorKind.HTTP_STATUS,
                    f"Ollama returned HTTP {error.code}.",
                    status_code=error.code,
                )
            )
        except (TimeoutError, URLError, OSError, HTTPException, ValueError) as error:
            return OllamaResult(error=_network_error(error))

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return OllamaResult(
                error=OllamaError(
                    OllamaErrorKind.RESPONSE_FORMAT,
                    f"Ollama returned invalid JSON: {error}.",
                )
            )

        if not isinstance(decoded, dict) or not isinstance(decoded.get("response"), str):
            return OllamaResult(
                error=OllamaError(
                    OllamaErrorKind.RESPONSE_FORMAT,
                    "Ollama response JSON did not contain a string 'response' field.",
                )
            )
        return OllamaResult(text=decoded["response"])

    @property
    def _endpoint_url(self) -> str:
        return f"http://{self._config.host}:{self._config.port}/api/generate"


def _default_transport(request: Request, timeout_seconds: float) -> bytes:
    with urlopen(request, timeout=timeout_seconds) as response:
        status_code = getattr(response, "status", response.getcode())
        if status_code != 200:
            raise HTTPError(request.full_url, status_code, "Unexpected HTTP status", None, None)
        return response.read()


def _network_error(error: TimeoutError | URLError | OSError | HTTPException | ValueError) -> OllamaError:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
        kind = OllamaErrorKind.TIMEOUT
    else:
        kind = OllamaErrorKind.CONNECTION
    return OllamaError(kind, f"Could not reach Ollama: {reason}.")
