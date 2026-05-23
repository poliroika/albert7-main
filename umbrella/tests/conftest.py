"""Shared pytest fixtures for Umbrella tests."""

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACES_TEST_SRC = REPO_ROOT / "workspaces" / "test"


def _ensure_manager_core(repo_root: Path) -> None:
    core = repo_root / ".umbrella" / "memory" / "core"
    core.mkdir(parents=True, exist_ok=True)
    (core / "00_identity.md").write_text(
        "# Identity (test harness)\nAlways verify durable memory.\n",
        encoding="utf-8",
    )
    (core / "10_operating_principles.md").write_text(
        "# Principles\nUse typed evidence refs.\n",
        encoding="utf-8",
    )
    (core / "bkb.yaml").write_text(
        "rules:\n"
        "  - id: manager_test_rule\n"
        "    title: Manager verified rule for harness\n"
        "    scope: manager\n"
        "    type: behavior\n"
        "    status: active\n"
        "    trust: verified\n"
        "    rule:\n"
        "      behavior: cite ledger events for durable promotion\n",
        encoding="utf-8",
    )


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
    (tmp_path / "umbrella").mkdir()
    dst = tmp_path / "workspaces" / "test"
    shutil.copytree(WORKSPACES_TEST_SRC, dst)
    _ensure_manager_core(tmp_path)
    return tmp_path, "test"
