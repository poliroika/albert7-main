"""Tests for the MCP registry, discovery, and tools-bridge wiring."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from umbrella.mcp.registry import McpRegistry, default_registry_path


def test_mcp_registry_add_list_update_delete(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = McpRegistry(repo)
    assert "list" not in McpRegistry.__dict__
    assert registry.list_servers() == []

    spec = registry.add_new(
        name="memory-bank",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
    )
    assert spec.id
    assert spec.status == "disabled"

    listed = registry.list_servers()
    assert len(listed) == 1
    assert listed[0].name == "memory-bank"

    updated = registry.set_status(spec.id, "enabled")
    assert updated is not None
    assert updated.status == "enabled"
    assert registry.list_servers()[0].status == "enabled"

    assert registry.delete(spec.id) is True
    assert registry.list_servers() == []


def test_mcp_registry_validates_transport(tmp_path: Path) -> None:
    registry = McpRegistry(tmp_path)
    with pytest.raises(ValueError, match="transport"):
        registry.add_new(name="bad", transport="ftp", command="x")
    with pytest.raises(ValueError, match="stdio transport requires a command"):
        registry.add_new(name="bad", transport="stdio")
    with pytest.raises(ValueError, match="http transport requires a url"):
        registry.add_new(name="bad", transport="http")


def test_default_registry_path_under_umbrella(tmp_path: Path) -> None:
    p = default_registry_path(tmp_path)
    assert p == tmp_path / ".umbrella" / "mcp" / "registry.json"


def test_mcp_install_tool_registers_disabled(tmp_path: Path) -> None:
    from umbrella.mcp.discovery import _mcp_install

    class _Ctx:
        host_repo_root = tmp_path
        repo_dir = tmp_path / "ws"
        task_id = "t1"

    ctx = _Ctx()
    out = _mcp_install(
        ctx,
        name="postgres",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres"],
        description="local postgres",
    )
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["spec"]["status"] == "disabled"
    registry = McpRegistry(tmp_path)
    listed = registry.list_servers()
    assert len(listed) == 1
    assert listed[0].name == "postgres"


def test_mcp_discover_tool_uses_github_search(tmp_path: Path) -> None:
    from umbrella.mcp import discovery

    class _Ctx:
        host_repo_root = tmp_path
        repo_dir = tmp_path / "ws"
        task_id = "t1"

    ctx = _Ctx()
    fake = [
        {
            "name": "alice/mcp-foo",
            "url": "https://github.com/alice/mcp-foo",
            "description": "foo",
            "stars": 12,
            "license": "MIT",
            "topics": ["mcp-server"],
        },
    ]
    with patch.object(discovery, "discover_servers", return_value=fake):
        out = discovery._mcp_discover(ctx, query="foo", max_results=5)
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["results"] == fake


def test_mcp_servers_tools_module_returns_empty_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OUROBOROS_MCP_DISABLED", "1")
    from ouroboros.tools import mcp_servers

    monkeypatch.chdir(tmp_path)
    assert mcp_servers.get_tools() == []


def test_mcp_servers_tools_module_skips_when_no_enabled_server(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OUROBOROS_MCP_DISABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".umbrella").mkdir()
    from ouroboros.tools import mcp_servers

    assert mcp_servers.get_tools() == []
