from __future__ import annotations

import unittest

from model_runtime.ollama_provider import OllamaProvider
from model_runtime.types import ChatMessage


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, payload, timeout):
        self.calls.append((method, url, payload, timeout))
        return self.responses.pop(0)


class OllamaProviderTests(unittest.TestCase):
    def test_list_models_reads_name_and_model_fields(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "models": [
                        {"name": "qwen3.5:9b"},
                        {"model": "qwen3-embedding:0.6b"},
                    ]
                }
            ]
        )
        provider = OllamaProvider(transport=transport)

        self.assertEqual(
            provider.list_models(),
            {"qwen3.5:9b", "qwen3-embedding:0.6b"},
        )
        self.assertTrue(transport.calls[0][1].endswith("/api/tags"))

    def test_chat_serializes_images_and_options(self) -> None:
        transport = RecordingTransport(
            [{"model": "qwen3.5:9b", "message": {"content": "seen"}}]
        )
        provider = OllamaProvider(transport=transport)

        result = provider.chat(
            "qwen3.5:9b",
            [ChatMessage("user", "describe", ("abc123",))],
            options={"num_ctx": 8192},
        )

        self.assertEqual(result.content, "seen")
        _method, _url, payload, _timeout = transport.calls[0]
        self.assertEqual(payload["model"], "qwen3.5:9b")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"][0]["images"], ["abc123"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)

    def test_embed_converts_numeric_rows(self) -> None:
        transport = RecordingTransport(
            [{"model": "qwen3-embedding:0.6b", "embeddings": [[1, 2.5], [3, 4]]}]
        )
        provider = OllamaProvider(transport=transport)

        result = provider.embed("qwen3-embedding:0.6b", ["a", "b"])

        self.assertEqual(result.embeddings, ((1.0, 2.5), (3.0, 4.0)))


if __name__ == "__main__":
    unittest.main()
