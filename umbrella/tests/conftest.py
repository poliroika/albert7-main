"""Shared pytest fixtures for Umbrella tests."""

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACES_TEST_SRC = REPO_ROOT / "workspaces" / "test"


def _ensure_manager_core(repo_root: Path) -> None:
    from umbrella.evals.memory_scenarios.fixtures import ensure_manager_core

    ensure_manager_core(repo_root)


@pytest.fixture(autouse=True)
def _allow_volatile_palace_stub(monkeypatch):
    """MemPalace tests run without chromadb via in-memory stub."""
    from umbrella.memory.palace.stores import _NullChromaCollection

    _NullChromaCollection._GLOBAL_ITEMS.clear()
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    yield
    _NullChromaCollection._GLOBAL_ITEMS.clear()


@pytest.fixture
def test_workspace_copy(tmp_path):
    """Copy committed workspaces/test into an isolated repo root for writes."""
    assert WORKSPACES_TEST_SRC.is_dir(), (
        f"missing fixture workspace: {WORKSPACES_TEST_SRC}"
    )
    (tmp_path / "umbrella").mkdir(parents=True, exist_ok=True)
    dst = tmp_path / "workspaces" / "test"
    shutil.copytree(WORKSPACES_TEST_SRC, dst)
    _ensure_manager_core(tmp_path)
    from umbrella.evals.memory_scenarios.fixtures import apply_default_workspace_memory

    apply_default_workspace_memory(tmp_path, "test")
    return tmp_path, "test"
