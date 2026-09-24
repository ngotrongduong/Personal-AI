from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_memory_root(tmp_path, monkeypatch):
    """Keep every app built by a test away from the real ``memory/`` folder.

    The string target imports ``main`` if needed, so the patch always applies.
    Run the suite with pytest; plain ``unittest`` skips this fixture.
    """

    root = tmp_path / "app_root"
    root.mkdir()
    monkeypatch.setattr("main.default_memory_root", lambda: root)
    yield
