from __future__ import annotations

import json
import socket
import unittest
from urllib.error import HTTPError, URLError

from agent.ollama_client import OllamaClient, OllamaClientConfig, OllamaErrorKind


class OllamaClientTests(unittest.TestCase):
    def test_generate_posts_configured_json_request_and_returns_response_text(self) -> None:
        requests = []

        def transport(request: object, timeout: float) -> bytes:
            requests.append((request, timeout))
            return b'{"response":"{\\\"type\\\": \\\"noop\\\"}"}'

        client = OllamaClient(
            OllamaClientConfig(model="test-model", host="planner.local", port=22334, timeout_seconds=4),
            transport=transport,
        )

        result = client.generate("Choose a directive.")

        self.assertTrue(result.successful)
        self.assertEqual(result.text, '{"type": "noop"}')
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "http://planner.local:22334/api/generate")
        self.assertEqual(timeout, 4.0)
        self.assertEqual(json.loads(request.data), {
            "model": "test-model",
            "prompt": "Choose a directive.",
            "stream": False,
            "format": "json",
        })

    def test_timeout_returns_structured_error(self) -> None:
        def transport(_request: object, _timeout: float) -> bytes:
            raise TimeoutError("request timed out")

        result = OllamaClient(OllamaClientConfig(model="test"), transport=transport).generate("prompt")

        self.assertFalse(result.successful)
        self.assertIsNone(result.text)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.TIMEOUT)

    def test_connection_failure_returns_structured_error(self) -> None:
        def transport(_request: object, _timeout: float) -> bytes:
            raise URLError(socket.gaierror("name lookup failed"))

        result = OllamaClient(OllamaClientConfig(model="test"), transport=transport).generate("prompt")

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.CONNECTION)

    def test_http_error_returns_status_without_raising(self) -> None:
        def transport(request: object, _timeout: float) -> bytes:
            raise HTTPError(request.full_url, 503, "Unavailable", None, None)

        result = OllamaClient(OllamaClientConfig(model="test"), transport=transport).generate("prompt")

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.HTTP_STATUS)
        self.assertEqual(result.error.status_code, 503)

    def test_malformed_response_returns_structured_error(self) -> None:
        result = OllamaClient(
            OllamaClientConfig(model="test"), transport=lambda _request, _timeout: b"not json"
        ).generate("prompt")

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.RESPONSE_FORMAT)

    def test_response_without_a_string_response_field_returns_format_error(self) -> None:
        for body in (b"{}", b'{"response": 42}', b'{"response": null}', b"[]"):
            with self.subTest(body=body):
                result = OllamaClient(
                    OllamaClientConfig(model="test"),
                    transport=lambda _request, _timeout, body=body: body,
                ).generate("prompt")

                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.kind, OllamaErrorKind.RESPONSE_FORMAT)

    def test_non_utf8_response_returns_format_error(self) -> None:
        result = OllamaClient(
            OllamaClientConfig(model="test"),
            transport=lambda _request, _timeout: b"\xff\xfe\x00",
        ).generate("prompt")

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.RESPONSE_FORMAT)

    def test_url_timeout_returns_timeout_error(self) -> None:
        for reason in (TimeoutError("timed out"), "timed out"):
            with self.subTest(reason=reason):
                def transport(_request: object, _timeout: float, reason: object = reason) -> bytes:
                    raise URLError(reason)

                result = OllamaClient(OllamaClientConfig(model="test"), transport=transport).generate(
                    "prompt"
                )

                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.kind, OllamaErrorKind.TIMEOUT)

    def test_direct_connection_refusal_returns_connection_error(self) -> None:
        def transport(_request: object, _timeout: float) -> bytes:
            raise ConnectionRefusedError("Ollama is not running")

        result = OllamaClient(OllamaClientConfig(model="test"), transport=transport).generate("prompt")

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.CONNECTION)

    def test_config_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            OllamaClientConfig(model="")
        with self.assertRaises(ValueError):
            OllamaClientConfig(model="test", port=0)
        with self.assertRaises(ValueError):
            OllamaClientConfig(model="test", timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
