from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from model_runtime.catalog import ModelCatalog


REPO_ROOT = Path(__file__).resolve().parents[1]


class ModelCatalogTests(unittest.TestCase):
    def test_repo_catalog_loads_and_orders_primary_roles(self) -> None:
        catalog = ModelCatalog.from_file(REPO_ROOT / "configs" / "models.v1.json")

        self.assertEqual(catalog.schema_version, 1)
        self.assertEqual(catalog.candidates("planner")[0].id, "qwen35_9b")
        self.assertEqual(
            catalog.candidates("memory_embedding")[0].id,
            "qwen3_embedding_0_6b",
        )

    def test_disabled_heavy_model_is_excluded_by_default(self) -> None:
        catalog = ModelCatalog.from_file(REPO_ROOT / "configs" / "models.v1.json")

        self.assertEqual(catalog.candidates("heavy_reasoner"), ())
        self.assertEqual(
            catalog.candidates("heavy_reasoner", include_disabled=True)[0].id,
            "gpt_oss_20b",
        )

    def test_unknown_model_reference_is_rejected(self) -> None:
        payload = {
            "schema_version": 1,
            "roles": {"planner": ["missing"]},
            "models": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown model"):
                ModelCatalog.from_file(path)

    def test_role_must_be_declared_by_candidate_model(self) -> None:
        payload = {
            "schema_version": 1,
            "roles": {"planner": ["embed"]},
            "models": [
                {
                    "id": "embed",
                    "display_name": "Embed",
                    "provider": "fake",
                    "provider_model": "embed",
                    "roles": ["memory_embedding"],
                    "capabilities": ["embedding"],
                    "enabled_by_default": True,
                    "source_repo": "example/repo",
                    "license": "MIT"
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not declare role"):
                ModelCatalog.from_file(path)


if __name__ == "__main__":
    unittest.main()
