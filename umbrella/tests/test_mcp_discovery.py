"""Tests for umbrella.mcp.discovery."""

import json
from unittest.mock import patch

from umbrella.mcp import discovery as mcp_disc


def test_discover_servers_fallback_query() -> None:
    calls: list[str] = []

    def fake_search(q: str, *, max_results: int):
        calls.append(q)
        if q.startswith("topic:mcp-server"):
            return [], None
        return (
            [
                {
                    "name": "acme/mcp-filesystem",
                    "url": "https://github.com/acme/mcp-filesystem",
                    "description": "Filesystem MCP",
                    "stars": 10,
                    "license": "MIT",
                    "topics": ["mcp-server"],
                    "install_hint_npx": "npx -y @acme/mcp-filesystem",
                }
            ],
            None,
        )

    with patch.object(mcp_disc, "_search_repositories", side_effect=fake_search):
        out = mcp_disc.discover_servers("filesystem", max_results=5)

    assert len(calls) == 2
    assert calls[0].startswith("topic:mcp-server")
    assert "in:name,description" in calls[1]
    assert out["status"] == "ok"
    assert len(out["results"]) == 1
    assert out["results"][0]["name"] == "acme/mcp-filesystem"


def test_discover_servers_rate_limited() -> None:
    with patch.object(
        mcp_disc, "_search_repositories", return_value=([], "rate_limited")
    ):
        out = mcp_disc.discover_servers("test", max_results=3)

    assert out["results"] == []
    assert out["status"] == "rate_limited"
    assert any("rate_limited" in w for w in out["warnings"])


def test_mcp_discover_json_includes_warnings() -> None:
    with patch.object(
        mcp_disc,
        "discover_servers",
        return_value={
            "results": [],
            "warnings": ["github_search_failed:rate_limited:topic_query"],
            "search_queries": ["topic:mcp-server x"],
            "status": "rate_limited",
        },
    ):
        raw = mcp_disc._mcp_discover(None, query="x")
    payload = json.loads(raw)
    assert payload["status"] == "rate_limited"
    assert payload["warnings"]


def test_research_manifest_allows_github_extract() -> None:
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1]
        / "phases"
        / "manifests"
        / "research.yaml"
    ).read_text(encoding="utf-8")
    assert "github_extract_snippets" in text
