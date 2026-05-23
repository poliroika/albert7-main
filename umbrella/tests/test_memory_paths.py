"""Regression: memory paths must not double-nest ``workspaces/<id>/workspaces/<id>``."""

from pathlib import Path

import pytest

from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.paths import (
    normalize_workspace_id,
    parse_palace_path_hint,
    palace_path_for,
    workspace_memory_root,
)


def test_normalize_workspace_id_strips_repo_prefix() -> None:
    assert normalize_workspace_id("demo") == "demo"
    assert normalize_workspace_id("workspaces/demo") == "demo"
    assert normalize_workspace_id("/workspaces/demo/") == "demo"
    assert normalize_workspace_id("workspaces/demo/workspaces/demo") == "demo"


def test_parse_palace_path_hint_strips_workspaces_and_memory() -> None:
    ws, event, room = parse_palace_path_hint(
        "workspaces/demo/research/plan",
        workspace_id="demo",
    )
    assert ws == "demo"
    assert event == "research"
    assert room == "research/plan"

    ws2, event2, room2 = parse_palace_path_hint(
        "workspaces/demo/.memory/ideas/hypothesis",
        workspace_id="demo",
    )
    assert ws2 == "demo"
    assert event2 == "ideas"
    assert room2 == "ideas/hypothesis"


def test_mem_palace_does_not_create_nested_workspaces_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    palace = MemPalace(tmp_path, "workspaces/nested_ws")
    try:
        expected = tmp_path / "workspaces" / "nested_ws" / ".memory" / "palace"
        assert expected.exists()
        assert not (tmp_path / "workspaces" / "workspaces").exists()
    finally:
        palace.close()


def test_workspace_memory_root_with_prefixed_id(tmp_path: Path) -> None:
    root = workspace_memory_root(tmp_path, "workspaces/demo")
    assert root == (tmp_path / "workspaces" / "demo" / ".memory").resolve()
    assert palace_path_for(tmp_path, "workspaces/demo") == root / "palace"
