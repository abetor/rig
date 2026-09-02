"""Shared pytest fixtures for isolating per-test project state."""
import pytest

from rig.db import convention


@pytest.fixture(autouse=True)
def _isolate_project_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(convention, "registry_path",
                        lambda: str(tmp_path / "projects.jsonl"))
