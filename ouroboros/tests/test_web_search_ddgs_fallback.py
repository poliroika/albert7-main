"""ddgs fallback when GMAS web search is unavailable."""

import json
import sys
from unittest.mock import MagicMock, patch

from ouroboros.tools import search as search_mod
from ouroboros.tools import web_search_adapter as wsa


def _fake_ddgs_module(rows: list[dict]) -> MagicMock:
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value = fake_ddgs
    fake_ddgs.text.return_value = rows
    fake_mod = MagicMock()
    fake_mod.DDGS.return_value = fake_ddgs
    return fake_mod


def test_web_search_via_ddgs_maps_results() -> None:
    fake_rows = [
        {
            "title": "Example",
            "href": "https://example.com",
            "body": "snippet text",
        }
    ]
    with patch.dict(sys.modules, {"ddgs": _fake_ddgs_module(fake_rows)}):
        payload = wsa.web_search_via_ddgs("test query", max_results=3)
    assert payload["status"] == "ok"
    assert payload["provider"] == "ddgs_fallback"
    assert payload["sources"][0]["url"] == "https://example.com"


def test_web_search_tool_falls_back_when_gmas_import_fails() -> None:
    fake_rows = [
        {"title": "A", "href": "https://a.test", "body": "one"},
    ]
    with (
        patch.object(
            wsa,
            "create_gmas_web_search_tool",
            side_effect=ImportError("no gmas"),
        ),
        patch.dict(sys.modules, {"ddgs": _fake_ddgs_module(fake_rows)}),
    ):
        raw = search_mod._web_search(None, query="fallback test")
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["provider"] == "ddgs_fallback"
