"""Tests for the MCP-related Web Bridge endpoints."""

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from umbrella.web_bridge.app import WebBridgeApp
from umbrella.web_bridge.handler import build_handler


@pytest.fixture
def httpd(tmp_path: Path):
    app = WebBridgeApp(tmp_path)
    handler = build_handler(app)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port, app
    server.shutdown()
    t.join(timeout=2)


def _request(
    port: int, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    raw_body = resp.read().decode("utf-8")
    conn.close()
    payload = json.loads(raw_body) if raw_body else {}
    return resp.status, payload


def test_mcp_servers_lifecycle_via_http(httpd) -> None:
    port, _app = httpd
    status, items = _request(port, "GET", "/api/mcp/servers")
    assert status == 200
    assert items == []

    status, created = _request(
        port,
        "POST",
        "/api/mcp/servers",
        {
            "name": "memory-bank",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "description": "test mcp",
        },
    )
    assert status == 201
    assert created["name"] == "memory-bank"
    server_id = created["id"]

    status, listed = _request(port, "GET", "/api/mcp/servers")
    assert status == 200
    assert len(listed) == 1
    assert listed[0]["status"] == "disabled"

    status, updated = _request(
        port, "PATCH", f"/api/mcp/servers/{server_id}", {"status": "enabled"}
    )
    assert status == 200
    assert updated["status"] == "enabled"

    status, deleted = _request(port, "DELETE", f"/api/mcp/servers/{server_id}")
    assert status == 200
    assert deleted["ok"] is True

    status, listed = _request(port, "GET", "/api/mcp/servers")
    assert listed == []


def test_mcp_discover_endpoint_returns_results(httpd) -> None:
    port, _app = httpd
    fake = [
        {
            "name": "alice/mcp-foo",
            "url": "https://github.com/alice/mcp-foo",
            "description": "foo",
            "stars": 12,
            "license": "MIT",
            "topics": ["mcp-server"],
        }
    ]
    with patch("umbrella.mcp.discovery.discover_servers", return_value=fake):
        status, payload = _request(
            port, "POST", "/api/mcp/discover", {"query": "foo", "max_results": 4}
        )
    assert status == 200
    assert payload["ok"] is True
    assert payload["results"] == fake


def test_mcp_servers_validation_error_returns_4xx(httpd) -> None:
    port, _app = httpd
    status, body = _request(
        port, "POST", "/api/mcp/servers", {"name": "bad", "transport": "stdio"}
    )
    assert status == 400
    assert "command" in body.get("error", "")
