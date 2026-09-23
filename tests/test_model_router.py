from __future__ import annotations

from collections.abc import Mapping, Sequence
import unittest

from model_runtime.catalog import ModelCatalog, ModelSpec
from model_runtime.router import ModelNotAvailableError, ModelRouter
from model_runtime.types import ChatMessage, ChatResponse, EmbeddingResponse


class FakeProvider:
    name = "fake"

    def __init__(self, installed: set[str]) -> None:
        self.installed = installed
        self.chat_calls: list[tuple[str, object]] = []
        self.embed_calls: list[tuple[str, tuple[str, ...]]] = []

    def list_models(self) -> set[str]:
        return set(self.installed)

    def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, object] | None = None,
    ) -> ChatResponse:
        self.chat_calls.append((model, dict(options or {})))
        return ChatResponse("fake", model, messages[-1].content)

    def embed(
        self,
        model: str,
        inputs: Sequence[str],
    ) -> EmbeddingResponse:
        self.embed_calls.append((model, tuple(inputs)))
        rows = tuple((float(index),) for index, _ in enumerate(inputs))
        return EmbeddingResponse("fake", model, rows)


def _catalog() -> ModelCatalog:
    return ModelCatalog(
        schema_version=1,
        models=[
            ModelSpec(
                id="big",
                display_name="Big",
                provider="fake",
                provider_model="big:latest",
                roles=("planner",),
                capabilities=frozenset({"chat"}),
                enabled_by_default=True,
                source_repo="example/big",
                license="MIT",
                default_context_tokens=8192,
            ),
            ModelSpec(
                id="small",
                display_name="Small",
                provider="fake",
                provider_model="small",
                roles=("planner",),
                capabilities=frozenset({"chat"}),
                enabled_by_default=True,
                source_repo="example/small",
                license="MIT",
                default_context_tokens=4096,
            ),
            ModelSpec(
                id="embed",
                display_name="Embed",
                provider="fake",
                provider_model="embed",
                roles=("memory_embedding",),
                capabilities=frozenset({"embedding"}),
                enabled_by_default=True,
                source_repo="example/embed",
                license="MIT",
            ),
        ],
        roles={
            "planner": ("big", "small"),
            "memory_embedding": ("embed",),
        },
    )


class ModelRouterTests(unittest.TestCase):
    def test_selects_first_installed_candidate(self) -> None:
        provider = FakeProvider({"small"})
        router = ModelRouter(_catalog(), {"fake": provider})

        self.assertEqual(router.select("planner", capability="chat").id, "small")

    def test_latest_alias_is_considered_installed(self) -> None:
        provider = FakeProvider({"big"})
        router = ModelRouter(_catalog(), {"fake": provider})

        self.assertEqual(router.select("planner").id, "big")

    def test_chat_applies_catalog_context_without_overriding_explicit_option(self) -> None:
        provider = FakeProvider({"big:latest"})
        router = ModelRouter(_catalog(), {"fake": provider})

        router.chat(
            "planner",
            [ChatMessage("user", "hello")],
            options={"num_ctx": 2048, "temperature": 0.2},
        )

        model, options = provider.chat_calls[-1]
        self.assertEqual(model, "big:latest")
        self.assertEqual(options["num_ctx"], 2048)
        self.assertEqual(options["temperature"], 0.2)

    def test_embedding_routes_to_embedding_capability(self) -> None:
        provider = FakeProvider({"embed"})
        router = ModelRouter(_catalog(), {"fake": provider})

        result = router.embed("memory_embedding", ["a", "b"])

        self.assertEqual(len(result.embeddings), 2)
        self.assertEqual(provider.embed_calls, [("embed", ("a", "b"))])

    def test_missing_installed_model_does_not_trigger_download(self) -> None:
        provider = FakeProvider(set())
        router = ModelRouter(_catalog(), {"fake": provider})

        with self.assertRaisesRegex(ModelNotAvailableError, "No installed model"):
            router.select("planner")


if __name__ == "__main__":
    unittest.main()
