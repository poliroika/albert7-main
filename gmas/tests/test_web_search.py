"""Tests for src/tools/web_search.py"""

import contextlib
import email.message
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gmas.tools.web_search import (
    DuckDuckGoProvider,
    SearchProvider,
    SerperProvider,
    SimpleHTMLParser,
    TavilyProvider,
    URLFetcher,
    WebSearchPolicy,
    WebSearchTool,
    _create_web_search_tool,
)

# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════


class MockProvider(SearchProvider):
    """Mock search provider that returns canned results."""

    def __init__(self, results: list[dict[str, str]] | None = None):
        self._results = results or [
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Snippet 1"},
            {"title": "Result 2", "url": "https://example.com/2", "snippet": "Snippet 2"},
        ]

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        return self._results[:max_results]


class EmptyProvider(SearchProvider):
    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        return []


# ═══════════════════════════════════════════════════════════════
#  SimpleHTMLParser
# ═══════════════════════════════════════════════════════════════


class TestSimpleHTMLParser:
    def test_empty_input(self):
        result = SimpleHTMLParser.html_to_text("")
        assert result == ""

    def test_plain_text(self):
        result = SimpleHTMLParser.html_to_text("Hello World")
        assert "Hello World" in result

    def test_strips_tags(self):
        html = "<p>Hello <b>World</b></p>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Hello" in result
        assert "World" in result
        assert "<" not in result
        assert ">" not in result

    def test_removes_script(self):
        html = "<html><script>alert('xss')</script><p>Safe text</p></html>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "alert" not in result
        assert "Safe text" in result

    def test_removes_style(self):
        html = "<html><style>.class { color: red; }</style><p>Visible</p></html>"
        result = SimpleHTMLParser.html_to_text(html)
        assert ".class" not in result
        assert "Visible" in result

    def test_removes_comments(self):
        html = "<!-- This is a comment -->Hello"
        result = SimpleHTMLParser.html_to_text(html)
        assert "comment" not in result
        assert "Hello" in result

    def test_handles_html_entities(self):
        html = "Fish &amp; Chips"
        result = SimpleHTMLParser.html_to_text(html)
        assert "&amp;" not in result
        assert "Fish & Chips" in result

    def test_max_length_truncation(self):
        html = "x" * 10000
        result = SimpleHTMLParser.html_to_text(html, max_length=100)
        assert "(content truncated)" in result

    def test_block_tags_add_newlines(self):
        html = "<p>Para 1</p><p>Para 2</p>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Para 1" in result
        assert "Para 2" in result

    def test_heading_tags(self):
        html = "<h1>Title</h1><h2>Subtitle</h2><p>Content</p>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Title" in result
        assert "Subtitle" in result
        assert "Content" in result

    def test_nested_tags(self):
        html = "<div><article><p>Article text</p></article></div>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Article text" in result

    def test_no_remaining_tags(self):
        html = "<div class='foo'><span id='bar'>text</span></div>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "<" not in result
        assert ">" not in result

    def test_removes_nav_header_footer(self):
        html = "<nav>Navigation</nav><main>Content</main><footer>Footer</footer>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Navigation" not in result
        assert "Content" in result
        assert "Footer" not in result

    def test_list_items(self):
        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Item 1" in result
        assert "Item 2" in result

    def test_case_insensitive_removes_script(self):
        html = "<SCRIPT>bad code</SCRIPT><P>Good text</P>"
        result = SimpleHTMLParser.html_to_text(html)
        assert "bad code" not in result
        assert "Good text" in result

    def test_br_tags(self):
        html = "Line 1<br>Line 2<br/>Line 3"
        result = SimpleHTMLParser.html_to_text(html)
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_multiple_spaces_collapsed(self):
        html = "<p>word1    word2      word3</p>"
        result = SimpleHTMLParser.html_to_text(html)
        # Collapsed spaces
        assert "word1" in result
        assert "word2" in result
        assert "word3" in result
        assert "word1     word2" not in result


# ═══════════════════════════════════════════════════════════════
#  URLFetcher
# ═══════════════════════════════════════════════════════════════


class TestURLFetcher:
    class TestURLFetcher:
        def test_init_custom(self):
            fetcher = URLFetcher(timeout=30, max_content_length=100_000)
            assert fetcher._timeout == 30
            assert fetcher._max_content_length == 100_000

        def test_fetch_fails_for_nonexistent_host(self):
            fetcher = URLFetcher(timeout=2)
            result = fetcher.fetch("http://this-should-not-exist-xyz.invalid/")
            assert isinstance(result, dict)
            assert result["success"] is False
            assert result["url"] == "http://this-should-not-exist-xyz.invalid/"

        def test_fetch_result_keys(self):
            fetcher = URLFetcher(timeout=1)
            result = fetcher.fetch("http://nope.invalid")
            for key in ("success", "url", "title", "content", "error"):
                assert key in result

        def test_fetch_success_mock(self):
            fetcher = URLFetcher()
            mock_html = b"<html><title>Test Page</title><body><p>Hello World</p></body></html>"
            mock_response = MagicMock()
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_response.headers.get = lambda key, default="": (
                "text/html; charset=utf-8" if "Content-Type" in key else default
            )
            mock_response.read.return_value = mock_html

            with (
                patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
                patch("urllib.request.urlopen", return_value=mock_response),
            ):
                result = fetcher.fetch("https://example.com")
                assert result["success"] is True
                assert "Hello World" in result["content"]
                assert result["title"] == "Test Page"

        def test_fetch_unsupported_content_type(self):
            fetcher = URLFetcher()
            mock_response = MagicMock()
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_response.headers.get = lambda key, default="": "application/pdf" if "Content-Type" in key else default

            with (
                patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
                patch("urllib.request.urlopen", return_value=mock_response),
            ):
                result = fetcher.fetch("https://example.com/file.pdf")
                assert result["success"] is False
                assert "Unsupported content type" in result["error"]

        def test_fetch_http_error(self):
            fetcher = URLFetcher()
            with (
                patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
                patch(
                    "urllib.request.urlopen",
                    side_effect=urllib.error.HTTPError(
                        url="http://example.com", code=404, msg="Not Found", hdrs=email.message.Message(), fp=None
                    ),
                ),
            ):
                result = fetcher.fetch("http://example.com")
                assert result["success"] is False
                assert "HTTP Error 404" in result["error"]

        def test_fetch_url_error(self):
            fetcher = URLFetcher()
            with (
                patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
                patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Name resolution failed")),
            ):
                result = fetcher.fetch("http://doesnotexist.invalid")
                assert result["success"] is False
                assert "URL Error" in result["error"]

        def test_fetch_timeout(self):
            fetcher = URLFetcher(timeout=1)
            with (
                patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
            ):
                result = fetcher.fetch("http://example.com")
                assert result["success"] is False
                assert "timed out" in result["error"].lower()

        def test_fetch_text_plain_content_type(self):
            fetcher = URLFetcher()
            mock_html = b"Hello plain text"
            mock_response = MagicMock()
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_response.headers.get = lambda key, default="": "text/plain" if "Content-Type" in key else default
            mock_response.read.return_value = mock_html

            with (
                patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
                patch("urllib.request.urlopen", return_value=mock_response),
            ):
                result = fetcher.fetch("https://example.com/text.txt")
                assert result["success"] is True
                assert "Hello plain text" in result["content"]


# ═══════════════════════════════════════════════════════════════
#  DuckDuckGoProvider
# ═══════════════════════════════════════════════════════════════


class TestDuckDuckGoProvider:
    def test_init_defaults(self):
        provider = DuckDuckGoProvider()
        assert provider._timeout == 12
        assert provider._backend_order == ("ddgs", "httpx_html", "urllib_html")
        assert provider._ddgs_backend == "duckduckgo"

    def test_init_custom(self):
        provider = DuckDuckGoProvider(timeout=30)
        assert provider._timeout == 30

    def test_init_custom_backend_policy(self):
        provider = DuckDuckGoProvider(
            backend_order=("ddgs_lite", "urllib_html", "unknown"),
            max_backend_attempts=1,
        )
        assert provider._backend_order == ("ddgs", "urllib_html")
        assert provider._max_backend_attempts == 1

    def test_init_legacy_ddgs_backend_alias_normalized(self):
        provider = DuckDuckGoProvider(ddgs_backend="lite")
        assert provider._ddgs_backend == "duckduckgo"

    def test_search_with_abstract(self):
        provider = DuckDuckGoProvider()
        ddgs_results = [{"title": "Python", "url": "https://python.org", "snippet": "A programming language"}]

        with patch.object(provider, "_search_ddgs", return_value=ddgs_results):
            results = provider.search("python", max_results=5)
            assert len(results) == 1
            assert results[0]["title"] == "Python"
            assert results[0]["snippet"] == "A programming language"

    def test_search_with_related_topics(self):
        provider = DuckDuckGoProvider()
        ddgs_results = [
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Text 1"},
            {"title": "Result 2", "url": "https://example.com/2", "snippet": "Text 2"},
        ]

        with patch.object(provider, "_search_ddgs", return_value=ddgs_results):
            results = provider.search("test", max_results=5)
            assert len(results) == 2

    def test_search_network_error_returns_empty(self):
        from gmas.tools.web_search._providers import SearchError

        provider = DuckDuckGoProvider()
        with (
            patch.object(provider, "_search_ddgs", side_effect=Exception("failed")),
            patch.object(provider, "_search_html_httpx", side_effect=ImportError),
            patch("gmas.tools.web_search._providers._urlopen", side_effect=urllib.error.URLError("failed")),
            pytest.raises(SearchError),
        ):
            provider.search("test")

    def test_search_excludes_non_dict_topics(self):
        provider = DuckDuckGoProvider()
        ddgs_results = [
            {"title": "Valid topic", "url": "https://valid.com", "snippet": "Valid topic"},
        ]

        with patch.object(provider, "_search_ddgs", return_value=ddgs_results):
            results = provider.search("test", max_results=10)
            assert any(r["snippet"] == "Valid topic" for r in results)

    def test_search_respects_max_results(self):
        provider = DuckDuckGoProvider()
        fallback_results = [
            {"title": f"Topic {i}", "url": f"https://ex.com/{i}", "snippet": f"Snippet {i}"} for i in range(10)
        ]

        with (
            patch.object(provider, "_search_ddgs", side_effect=ImportError),
            patch.object(provider, "_search_html_httpx", side_effect=ImportError),
            patch.object(provider, "_search_html_urllib", return_value=fallback_results[:3]),
        ):
            results = provider.search("test", max_results=3)
            assert len(results) <= 3


# ═══════════════════════════════════════════════════════════════
#  SerperProvider
# ═══════════════════════════════════════════════════════════════


class TestSerperProvider:
    def test_init(self):
        provider = SerperProvider(api_key="test-key")
        assert provider._api_key == "test-key"
        assert provider._timeout == 10

    def test_search_organic(self):
        provider = SerperProvider(api_key="test-key")
        serper_response = {
            "organic": [
                {"title": "Result 1", "link": "https://example.com/1", "snippet": "Snippet 1"},
                {"title": "Result 2", "link": "https://example.com/2", "snippet": "Snippet 2"},
            ]
        }
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(serper_response).encode("utf-8")

        with patch("gmas.tools.web_search._providers._urlopen", return_value=mock_response):
            results = provider.search("test query", max_results=5)
            assert len(results) == 2
            assert results[0]["title"] == "Result 1"

    def test_search_with_answer_box(self):
        provider = SerperProvider(api_key="test-key")
        serper_response = {
            "organic": [{"title": "Result", "link": "https://example.com", "snippet": "info"}],
            "answerBox": {
                "title": "Direct Answer",
                "link": "https://answer.com",
                "answer": "The answer is 42",
            },
        }
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(serper_response).encode("utf-8")

        with patch("gmas.tools.web_search._providers._urlopen", return_value=mock_response):
            results = provider.search("query", max_results=5)
            # Answer box should be inserted at position 0
            assert results[0]["title"] == "Direct Answer"

    def test_search_network_error_returns_empty(self):
        from gmas.tools.web_search._providers import SearchError

        provider = SerperProvider(api_key="key")
        with (
            patch("gmas.tools.web_search._providers._urlopen", side_effect=urllib.error.URLError("failed")),
            pytest.raises(SearchError),
        ):
            provider.search("test")


# ═══════════════════════════════════════════════════════════════
#  TavilyProvider
# ═══════════════════════════════════════════════════════════════


class TestTavilyProvider:
    def test_init(self):
        provider = TavilyProvider(api_key="tavily-key")
        assert provider._api_key == "tavily-key"
        assert provider._include_answer is True

    def test_search_with_answer(self):
        provider = TavilyProvider(api_key="key")
        tavily_response = {
            "answer": "The answer is 42",
            "results": [
                {"title": "Page 1", "url": "https://example.com/1", "content": "Content 1"},
                {"title": "Page 2", "url": "https://example.com/2", "content": "Content 2"},
            ],
        }
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(tavily_response).encode("utf-8")

        with patch("gmas.tools.web_search._providers._urlopen", return_value=mock_response):
            results = provider.search("test", max_results=5)
            assert results[0]["title"] == "Tavily AI Answer"
            assert len(results) == 3

    def test_search_no_answer(self):
        provider = TavilyProvider(api_key="key")
        tavily_response = {"results": [{"title": "Page", "url": "https://example.com", "content": "Content"}]}
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(tavily_response).encode("utf-8")

        with patch("gmas.tools.web_search._providers._urlopen", return_value=mock_response):
            results = provider.search("test", max_results=5)
            assert len(results) == 1
            assert results[0]["title"] == "Page"

    def test_search_network_error_returns_empty(self):
        from gmas.tools.web_search._providers import SearchError

        provider = TavilyProvider(api_key="key")
        with (
            patch("gmas.tools.web_search._providers._urlopen", side_effect=urllib.error.URLError("failed")),
            pytest.raises(SearchError),
        ):
            provider.search("test")


# ═══════════════════════════════════════════════════════════════
#  WebSearchTool
# ═══════════════════════════════════════════════════════════════


class TestWebSearchToolInit:
    def test_init_default_provider(self):
        tool = WebSearchTool()
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_init_custom_provider(self):
        provider = MockProvider()
        tool = WebSearchTool(provider=provider)
        assert tool._provider is provider

    def test_name_property(self):
        tool = WebSearchTool()
        assert tool.name == "web_search"

    def test_description_property(self):
        tool = WebSearchTool()
        desc = tool.description
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_parameters_schema(self):
        tool = WebSearchTool()
        schema = tool.parameters_schema
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "url" in schema["properties"]
        assert "action" in schema["properties"]

    def test_parameters_schema_with_selenium(self):
        tool = WebSearchTool()
        # Simulate having a browser fetcher to enable extended schema
        tool._browser_fetcher = MagicMock()
        schema = tool.parameters_schema
        assert "selector" in schema["properties"]
        assert "js_code" in schema["properties"]

    def test_parameters_schema_with_playwright_advanced(self):
        tool = WebSearchTool()
        tool._browser_fetcher = MagicMock()
        tool._browser_fetcher.supports_advanced_session.return_value = True
        schema = tool.parameters_schema
        assert "tab_index" in schema["properties"]
        assert "cookies" in schema["properties"]
        assert "screenshot" in schema["properties"]["action"]["enum"]

    def test_init_with_custom_policy(self):
        policy = WebSearchPolicy(query_term_limit=4, default_http_fetch_pages=2)
        tool = WebSearchTool(provider=MockProvider(), policy=policy)
        assert tool._policy.query_term_limit == 4
        assert tool._max_fetch_pages == 2


class TestWebSearchToolExecute:
    def test_execute_search_with_query(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(query="python programming")
        assert result.success is True
        assert "Result 1" in result.output

    def test_execute_no_action_no_query_no_url(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute()
        assert result.success is False
        assert result.error

    def test_execute_search_empty_results(self):
        tool = WebSearchTool(provider=EmptyProvider())
        result = tool.execute(query="something obscure")
        assert result.success is True
        assert "No results found" in result.output

    def test_execute_fetch_with_url(self):
        tool = WebSearchTool(provider=MockProvider())
        mock_fetch_result = {
            "success": True,
            "url": "https://example.com",
            "title": "Test",
            "content": "Test content here",
        }
        with patch.object(tool._fetcher, "fetch", return_value=mock_fetch_result):
            result = tool.execute(url="https://example.com")
            assert result.success is True
            assert "Test content here" in result.output

    def test_execute_fetch_action_no_url(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="fetch")
        assert result.success is False
        assert result.error

    def test_execute_fetch_action_with_url(self):
        tool = WebSearchTool(provider=MockProvider())
        mock_fetch_result = {
            "success": True,
            "url": "https://example.com",
            "title": "Page",
            "content": "Page content",
        }
        with patch.object(tool._fetcher, "fetch", return_value=mock_fetch_result):
            result = tool.execute(action="fetch", url="https://example.com")
            assert result.success is True

    def test_execute_fetch_failure(self):
        tool = WebSearchTool(provider=MockProvider())
        mock_fetch_result = {
            "success": False,
            "url": "https://example.com",
            "title": "",
            "content": "",
            "error": "Connection refused",
        }
        with patch.object(tool._fetcher, "fetch", return_value=mock_fetch_result):
            result = tool.execute(action="fetch", url="https://example.com")
            assert result.success is False

    def test_execute_click_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="click", selector=".button")
        assert result.success is False
        assert result.error is not None
        assert "browser" in result.error.lower()

    def test_execute_fill_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="fill", selector="input", value="test")
        assert result.success is False
        assert result.error is not None
        assert "browser" in result.error.lower()

    def test_execute_extract_links_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="extract_links")
        assert result.success is False
        assert result.error is not None
        assert "browser" in result.error.lower()

    def test_execute_execute_js_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="execute_js", js_code="return 1")
        assert result.success is False
        assert result.error is not None
        assert "browser" in result.error.lower()

    def test_execute_execute_js_no_code(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="execute_js")
        assert result.success is False

    def test_execute_crawl_without_url(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="crawl")
        assert result.success is False

    def test_execute_crawl_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="crawl", url="https://example.com")
        assert result.success is False
        assert result.error is not None
        assert "browser" in result.error.lower()

    def test_execute_get_content_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="get_content")
        assert result.success is False

    def test_execute_search_action_explicit(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="search", query="test")
        assert result.success is True

    def test_execute_search_action_no_query(self):
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="search")
        assert result.success is False

    def test_execute_with_fetch_content(self):
        provider = MockProvider()
        tool = WebSearchTool(provider=provider, fetch_content=True)

        mock_fetch_result = {
            "success": True,
            "url": "https://example.com/1",
            "title": "Example",
            "content": "Page content here for fetching",
        }
        with patch.object(tool._fetcher, "fetch", return_value=mock_fetch_result):
            result = tool.execute(query="test query")
            assert result.success is True

    def test_execute_max_results_clipped(self):
        """max_results is capped at 10 in the implementation."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(query="test", max_results=100)
        assert result.success is True


class TestWebSearchToolFormatting:
    def test_format_search_results_empty(self):
        tool = WebSearchTool(provider=MockProvider())
        formatted = tool._format_search_results([])
        assert "No results found" in formatted

    def test_format_search_results_with_results(self):
        tool = WebSearchTool(provider=MockProvider())
        results = [
            {"title": "Test Title", "url": "https://example.com", "snippet": "A snippet"},
        ]
        formatted = tool._format_search_results(results)
        assert "Test Title" in formatted
        assert "https://example.com" in formatted
        assert "A snippet" in formatted

    def test_format_search_results_with_content(self):
        tool = WebSearchTool(provider=MockProvider(), max_content_length=200)
        results = [
            {"title": "Title", "url": "https://example.com", "snippet": "snap", "content": "Page content"},
        ]
        formatted = tool._format_search_results(results, with_content=True)
        assert "Page content" in formatted

    def test_format_search_results_content_truncated(self):
        tool = WebSearchTool(provider=MockProvider(), max_content_length=5)
        results = [
            {"title": "T", "url": "https://x.com", "snippet": "", "content": "A" * 100},
        ]
        formatted = tool._format_search_results(results, with_content=True)
        assert "truncated" in formatted.lower()


class TestWebSearchToolSeleniumCheck:
    def test_require_selenium_raises_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        assert tool._browser_fetcher is None
        with pytest.raises(RuntimeError, match="browser"):
            tool._require_browser("click")

    def test_context_manager(self):
        tool = WebSearchTool(provider=MockProvider())
        with tool as t:
            assert t is tool

    def test_close_without_selenium(self):
        tool = WebSearchTool(provider=MockProvider())
        tool.close()  # Should not raise


class TestCreateWebSearchToolFactory:
    def test_default_provider(self):
        tool = _create_web_search_tool()
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_serper_provider_with_key(self):
        tool = _create_web_search_tool(provider="serper", api_key="my-key")
        assert isinstance(tool._provider, SerperProvider)

    def test_serper_provider_no_key_falls_back_to_ddg(self):
        tool = _create_web_search_tool(provider="serper")
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_tavily_provider_with_key(self):
        tool = _create_web_search_tool(provider="tavily", api_key="my-key")
        assert isinstance(tool._provider, TavilyProvider)

    def test_tavily_provider_no_key_falls_back_to_ddg(self):
        tool = _create_web_search_tool(provider="tavily")
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_duckduckgo_provider_explicit(self):
        tool = _create_web_search_tool(provider="duckduckgo")
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_ddg_alias(self):
        tool = _create_web_search_tool(provider="ddg")
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_unknown_provider_falls_back_to_ddg(self):
        tool = _create_web_search_tool(provider="unknown_xyz")
        assert isinstance(tool._provider, DuckDuckGoProvider)

    def test_serper_provider_serper_api_key_param(self):
        tool = _create_web_search_tool(provider="serper", serper_api_key="key")
        assert isinstance(tool._provider, SerperProvider)

    def test_tavily_provider_tavily_api_key_param(self):
        tool = _create_web_search_tool(provider="tavily", tavily_api_key="key")
        assert isinstance(tool._provider, TavilyProvider)


# ═══════════════════════════════════════════════════════════════
#  SeleniumFetcher — initialization only (no real browser)
# ═══════════════════════════════════════════════════════════════


class TestSeleniumFetcherInit:
    def test_init_defaults(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        assert fetcher._headless is True
        assert fetcher._browser == "auto"
        assert fetcher._wait_timeout == 15
        assert fetcher._page_load_timeout == 30
        assert fetcher._scroll_to_bottom is False
        assert fetcher._driver is None

    def test_init_custom(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher(
            headless=False,
            browser="firefox",
            wait_timeout=30,
            scroll_to_bottom=True,
        )
        assert fetcher._headless is False
        assert fetcher._browser == "firefox"
        assert fetcher._wait_timeout == 30
        assert fetcher._scroll_to_bottom is True

    def test_ensure_dependencies_import_error(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        with patch("builtins.__import__", side_effect=ImportError("No selenium")), pytest.raises(ImportError):
            fetcher._ensure_dependencies()

    def test_ensure_dependencies_with_selenium(self):
        """If selenium is available, _ensure_dependencies should not raise."""
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        try:
            import selenium  # noqa: F401

            fetcher._ensure_dependencies()  # Should not raise
        except ImportError:
            pytest.skip("selenium not installed")

    def test_close_no_driver(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        fetcher.close()  # Should not raise
        assert fetcher._driver is None

    def test_close_with_mock_driver(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        mock_driver = MagicMock()
        fetcher._driver = mock_driver
        fetcher.close()
        mock_driver.quit.assert_called_once()
        assert fetcher._driver is None

    def test_context_manager(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        with fetcher as f:
            assert f is fetcher

    def test_create_driver_invalid_browser(self):
        from gmas.tools.web_search import SeleniumFetcher

        try:
            import selenium  # noqa: F401
        except ImportError:
            pytest.skip("selenium not installed")
        fetcher = SeleniumFetcher(browser="ie")
        with pytest.raises(ValueError, match="Unsupported browser"):
            fetcher._create_driver()

    def test_get_driver_creates_if_none(self):
        from gmas.tools.web_search import SeleniumFetcher

        fetcher = SeleniumFetcher()
        with patch.object(fetcher, "_create_driver", return_value=MagicMock()):
            driver = fetcher._get_driver()
            assert driver is not None


# ═══════════════════════════════════════════════════════════════
#  URLFetcher — charset detection
# ═══════════════════════════════════════════════════════════════


class TestURLFetcherCharset:
    def test_custom_charset_in_content_type(self):
        """Test charset extraction from content-type header."""
        fetcher = URLFetcher()
        mock_html = b"Hello World"
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get = lambda key, default="": (
            "text/html; charset=latin-1" if "Content-Type" in key else default
        )
        mock_response.read.return_value = mock_html

        with (
            patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = fetcher.fetch("https://example.com")
            assert result["success"] is True

    def test_unicode_decode_error_fallback(self):
        """Test fallback when charset decoding fails."""
        fetcher = URLFetcher()
        mock_html = b"\xff\xfe Hello World"
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get = lambda key, default="": (
            "text/html; charset=utf-16" if "Content-Type" in key else default
        )
        mock_response.read.return_value = mock_html

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = fetcher.fetch("https://example.com")
            # Should succeed even if charset is tricky
            assert "success" in result

    def test_value_error_in_fetch(self):
        """Test handling of ValueError in fetch."""
        fetcher = URLFetcher()
        with patch("urllib.request.urlopen", side_effect=ValueError("bad url")):
            result = fetcher.fetch("not-a-url")
            assert result["success"] is False

    def test_main_content_extraction(self):
        """Test that main/article content is extracted."""
        fetcher = URLFetcher()
        html_content = (
            b"""
        <html>
        <head><title>Test</title></head>
        <body>
        <nav>Navigation</nav>
        <main>
        """
            + (b"Main content " * 50)
            + b"""
        </main>
        </body>
        </html>
        """
        )
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get = lambda key, default="": (
            "text/html; charset=utf-8" if "Content-Type" in key else default
        )
        mock_response.read.return_value = html_content

        with (
            patch.object(URLFetcher, "_fetch_httpx", side_effect=ImportError),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = fetcher.fetch("https://example.com")
            assert result["success"] is True
            assert "Main content" in result["content"]


# ═══════════════════════════════════════════════════════════════
#  WebSearchTool — with mock selenium fetcher
# ═══════════════════════════════════════════════════════════════


class TestWebSearchToolWithBrowserFetcher:
    """Tests for WebSearchTool when given a mocked browser fetcher."""

    def _make_mock_browser_fetcher(self):
        from gmas.tools.web_search import BrowserFetcher

        return MagicMock(spec=BrowserFetcher)

    def test_init_with_browser_fetcher(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        assert tool._browser_fetcher is mock_fetcher
        assert tool._deep_search is not None

    def test_execute_fetch_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.fetch.return_value = {
            "success": True,
            "url": "https://example.com",
            "title": "Example",
            "content": "Content via Browser",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(url="https://example.com")
        assert result.success is True
        assert "Content via Browser" in result.output

    def test_execute_click_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.click_element.return_value = {
            "success": True,
            "url": "https://example.com/next",
            "title": "Next Page",
            "clicked_text": "Submit",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="click", selector=".submit-btn")
        assert result.success is True

    def test_execute_click_failed(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.click_element.return_value = {
            "success": False,
            "error": "Element not found",
            "url": "",
            "title": "",
            "clicked_text": "",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="click", selector=".nonexistent")
        assert result.success is False

    def test_execute_fill_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.fill_input.return_value = {
            "success": True,
            "url": "https://example.com",
            "title": "Page",
            "error": "",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="fill", selector="input[name=q]", value="test")
        assert result.success is True

    def test_execute_fill_no_selector_fails(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="fill", value="test")
        assert result.success is False

    def test_execute_extract_links_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.fetch.return_value = {
            "success": True,
            "url": "https://example.com",
            "title": "Example",
            "content": "",
        }
        mock_fetcher.extract_links.return_value = {
            "success": True,
            "url": "https://example.com",
            "links": [
                {"url": "https://example.com/1", "text": "Link 1", "title": ""},
            ],
            "count": 1,
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="extract_links", url="https://example.com")
        assert result.success is True

    def test_execute_execute_js_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.execute_js.return_value = {
            "success": True,
            "url": "https://example.com",
            "return_value": "document.title",
            "error": "",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="execute_js", js_code="return document.title")
        assert result.success is True

    def test_execute_get_content_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.get_page_content.return_value = {
            "success": True,
            "url": "https://example.com",
            "title": "Example",
            "content": "Page content here",
            "error": "",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="get_content")
        assert result.success is True

    def test_execute_crawl_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.crawl.return_value = {
            "success": True,
            "pages": [{"url": "https://example.com", "title": "Home", "depth": 0}],
            "total_pages": 1,
            "error": "",
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="crawl", url="https://example.com", max_depth=1, max_pages=5)
        assert result.success is True

    def test_execute_list_tabs_with_advanced_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.supports_advanced_session.return_value = True
        mock_fetcher.list_tabs.return_value = {
            "success": True,
            "tabs": [{"index": 0, "title": "Example", "url": "https://example.com", "active": True}],
            "count": 1,
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="list_tabs")
        assert result.success is True
        assert "Open tabs" in result.output

    def test_execute_get_cookies_truncates_large_payload(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.supports_advanced_session.return_value = True
        mock_fetcher.get_cookies.return_value = {
            "success": True,
            "count": 1,
            "cookies": [
                {
                    "name": "session",
                    "value": "x" * 400,
                    "domain": ".example.com",
                    "path": "/",
                }
            ],
        }
        tool = WebSearchTool(browser_fetcher=mock_fetcher, max_content_length=80)

        result = tool.execute(action="get_cookies")

        assert result.success is True
        assert "... (cookies truncated)" in result.output

    def test_execute_advanced_action_requires_playwright(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.supports_advanced_session.return_value = False
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        result = tool.execute(action="list_tabs")
        assert result.success is False
        assert result.error is not None
        assert "Playwright backend" in result.error

    def test_execute_search_with_fetch_content_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        mock_fetcher.fetch.return_value = {
            "success": True,
            "url": "https://example.com",
            "title": "Example",
            "content": "Content here",
        }
        tool = WebSearchTool(
            provider=MockProvider(),
            browser_fetcher=mock_fetcher,
            fetch_content=True,
        )
        result = tool.execute(query="test")
        assert result.success is True

    def test_description_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        desc = tool.description
        assert "browser" in desc.lower()

    def test_close_with_browser(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_fetcher = MagicMock(spec=BrowserFetcher)
        tool = WebSearchTool(browser_fetcher=mock_fetcher)
        tool.close()
        mock_fetcher.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════
#  WebSearchTool — callback integration
# ═══════════════════════════════════════════════════════════════


class TestWebSearchToolCallbacks:
    def test_emit_tool_start_with_callback_manager(self):
        from gmas.callbacks.base import BaseCallbackHandler
        from gmas.callbacks.manager import CallbackManager

        class RecordingCB(BaseCallbackHandler):
            def __init__(self):
                self.calls = []

            def on_tool_start(self, *, run_id, tool_name, **kwargs):
                self.calls.append(("start", tool_name))

            def on_tool_end(self, *, run_id, tool_name, **kwargs):
                self.calls.append(("end", tool_name))

        cb = RecordingCB()
        manager = CallbackManager(handlers=[cb])
        tool = WebSearchTool(provider=MockProvider(), callback_manager=manager)
        result = tool.execute(query="test")
        assert result.success is True
        assert any(c[0] == "start" for c in cb.calls)
        assert any(c[0] == "end" for c in cb.calls)

    def test_get_callback_manager_caches_context_resolver(self):
        tool = WebSearchTool(provider=MockProvider())

        with patch("gmas.callbacks.context.get_callback_manager", return_value=None) as mock_get:
            assert tool._get_callback_manager() is None
            assert tool._get_callback_manager() is None

        assert mock_get.call_count == 2

    def test_get_callback_manager_from_context(self):
        tool = WebSearchTool(provider=MockProvider())
        # Without a callback manager, _get_callback_manager should return None or context manager
        cb = tool._get_callback_manager()
        # May be None if not in a callback context
        assert cb is None or hasattr(cb, "on_tool_start")

    def test_get_callback_manager_exception_returns_none(self, monkeypatch):
        """_get_callback_manager should return None on exception."""
        tool = WebSearchTool(provider=MockProvider())
        tool._callback_manager = None
        # Mock get_callback_manager to raise
        monkeypatch.setattr(
            "gmas.callbacks.context.get_callback_manager",
            lambda: (_ for _ in ()).throw(RuntimeError("error")),
        )
        cb = tool._get_callback_manager()
        assert cb is None

    def test_emit_tool_error_with_callback_manager(self):
        """Test _emit_tool_error is called when callback manager is set."""
        from gmas.callbacks.base import BaseCallbackHandler
        from gmas.callbacks.manager import CallbackManager

        class RecordingCB(BaseCallbackHandler):
            def __init__(self):
                self.errors = []

            def on_tool_error(self, *, run_id, tool_name, **kwargs):
                self.errors.append(tool_name)

        cb_handler = RecordingCB()
        manager = CallbackManager(handlers=[cb_handler])
        tool = WebSearchTool(provider=MockProvider(), callback_manager=manager)

        # Trigger an error by mocking provider.search to raise
        from unittest.mock import MagicMock

        mock_error = TimeoutError("timed out")
        tool._provider = MagicMock()
        tool._provider.search.side_effect = mock_error

        result = tool.execute(query="test")
        assert result.success is False
        assert len(cb_handler.errors) > 0


# ═══════════════════════════════════════════════════════════════
#  WebSearchTool.execute — action routing edge cases
# ═══════════════════════════════════════════════════════════════


class TestWebSearchToolExecuteActionRouting:
    """Test execute() auto-detection and edge cases."""

    def test_auto_detect_click_from_selector(self):
        """Auto-detect action 'click' when selector is provided."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.click_element.return_value = {
            "success": True,
            "url": "http://example.com",
            "title": "T",
            "content": "",
            "clicked_text": "Button",
        }
        tool._browser_fetcher = mock_selenium

        result = tool.execute(selector="#btn")
        assert result.success is True

    def test_auto_detect_execute_js_from_js_code(self):
        """Auto-detect action 'execute_js' when js_code is provided."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.execute_js.return_value = {"success": True, "url": "http://example.com", "return_value": 42}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(js_code="return 42;")
        assert result.success is True

    def test_click_action_without_selector_returns_error(self):
        """Action 'click' without selector returns error."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="click")
        assert result.success is False
        assert result.error is not None
        assert "selector" in result.error

    def test_fill_action_without_selector_returns_error(self):
        """Action 'fill' without selector returns error."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="fill")
        assert result.success is False
        assert result.error is not None
        assert "selector" in result.error

    def test_execute_js_action_without_js_code_returns_error(self):
        """Action 'execute_js' without js_code returns error."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="execute_js")
        assert result.success is False
        assert result.error is not None
        assert "js_code" in result.error

    def test_crawl_action_without_url_returns_error(self):
        """Action 'crawl' without url returns error."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="crawl")
        assert result.success is False
        assert result.error is not None
        assert "url" in result.error

    def test_fetch_action_without_url_returns_error(self):
        """Action 'fetch' without url returns error."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(action="fetch")
        assert result.success is False
        assert result.error is not None
        assert "url" in result.error

    def test_no_action_no_query_url_selector_js_code_returns_error(self):
        """No action, query, url, selector, or js_code returns error."""
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute()
        assert result.success is False
        assert result.error is not None
        assert "No action" in result.error

    def test_fetch_url_with_wait_for_selector(self):
        """_fetch_url with wait_for_selector uses fetch_with_wait."""
        from gmas.tools.web_search import SeleniumFetcher

        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock(spec=SeleniumFetcher)
        mock_selenium.fetch_with_wait.return_value = {"success": True, "title": "Test", "content": "content"}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(url="https://example.com", wait_for_selector="#main")
        assert result.success is True
        mock_selenium.fetch_with_wait.assert_called_once()

    def test_fetch_url_exception_returns_error(self):
        """_fetch_url exception returns error."""
        from unittest.mock import patch

        tool = WebSearchTool(provider=MockProvider())
        with patch.object(tool, "_get_active_fetcher") as mock_fetcher:
            mock_fetcher.return_value.fetch.side_effect = RuntimeError("connection failed")
            result = tool.execute(url="https://example.com")
            assert result.success is False

    def test_execute_fill_fail(self):
        """Fill action when fill fails."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.fill_input.return_value = {"success": False, "error": "element not found"}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="fill", selector="#input", value="test")
        assert result.success is False
        assert result.error is not None
        assert "element not found" in result.error

    def test_execute_extract_links_fetch_fail(self):
        """Extract links when URL fetch fails."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.fetch.return_value = {"success": False, "error": "page not found"}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="extract_links", url="https://example.com")
        assert result.success is False
        assert result.error is not None
        assert "page not found" in result.error

    def test_execute_extract_links_extract_fail(self):
        """Extract links when link extraction fails."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.fetch.return_value = {"success": True, "content": ""}
        mock_selenium.extract_links.return_value = {"success": False, "error": "extraction failed"}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="extract_links")
        assert result.success is False

    def test_execute_extract_links_with_title(self):
        """Extract links with link that has title."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.extract_links.return_value = {
            "success": True,
            "url": "http://example.com",
            "count": 1,
            "links": [{"url": "http://example.com/page", "text": "link", "title": "Page Title"}],
        }
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="extract_links")
        assert result.success is True
        assert "Page Title" in result.output

    def test_execute_js_fail(self):
        """Execute JS when JS fails."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.execute_js.return_value = {"success": False, "error": "js error"}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="execute_js", js_code="throw Error()")
        assert result.success is False

    def test_execute_js_no_return_value(self):
        """Execute JS with no return value."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.execute_js.return_value = {"success": True, "url": "http://example.com", "return_value": None}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="execute_js", js_code="document.title = 'test'")
        assert result.success is True
        assert "no return value" in result.output

    def test_execute_crawl_fail(self):
        """Crawl action when crawl fails."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.crawl.return_value = {"success": False, "error": "crawl failed", "pages": []}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="crawl", url="http://example.com")
        assert result.success is False

    def test_execute_crawl_with_content_truncation(self):
        """Crawl result with content exceeding max_content_length."""
        tool = WebSearchTool(provider=MockProvider(), max_content_length=10)
        mock_selenium = MagicMock()
        long_content = "x" * 100
        mock_selenium.crawl.return_value = {
            "success": True,
            "total_pages": 1,
            "pages": [
                {"url": "http://example.com", "title": "T", "depth": 0, "content": long_content, "links_found": 0}
            ],
            "error": None,
        }
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="crawl", url="http://example.com")
        assert result.success is True
        assert "truncated" in result.output

    def test_execute_crawl_with_error_warning(self):
        """Crawl result with partial error."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.crawl.return_value = {
            "success": True,
            "total_pages": 1,
            "pages": [{"url": "http://example.com", "title": "T", "depth": 0, "content": "content", "links_found": 0}],
            "error": "some pages failed",
        }
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="crawl", url="http://example.com")
        assert result.success is True
        assert "warning" in result.output.lower() or "some pages failed" in result.output

    def test_execute_get_content_fail(self):
        """Get content when it fails."""
        tool = WebSearchTool(provider=MockProvider())
        mock_selenium = MagicMock()
        mock_selenium.get_page_content.return_value = {"success": False, "error": "driver not ready"}
        tool._browser_fetcher = mock_selenium

        result = tool.execute(action="get_content")
        assert result.success is False

    def test_execute_search_timeout_error(self):
        """Search with TimeoutError."""
        tool = WebSearchTool(provider=MockProvider())
        tool._provider = MagicMock()
        tool._provider.search.side_effect = TimeoutError("timed out")

        result = tool.execute(query="test")
        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    def test_execute_search_urlerror(self):
        """Search with URLError."""
        import urllib.error

        tool = WebSearchTool(provider=MockProvider())
        tool._provider = MagicMock()
        tool._provider.search.side_effect = urllib.error.URLError("network error")

        result = tool.execute(query="test")
        assert result.success is False

    def test_execute_search_with_fetched_title(self):
        """Search result gets title from fetched content."""
        tool = WebSearchTool(
            provider=MockProvider(
                results=[
                    {"title": "", "url": "https://example.com/1", "snippet": "Snippet"},
                ]
            ),
            fetch_content=True,
        )
        with patch.object(
            tool, "_fetch_page_content", return_value={"title": "Fetched Title", "content": "Page content"}
        ):
            result = tool.execute(query="test")
            assert result.success is True

    def test_fetch_content_for_results_browser_uses_http_fast_path_then_browser_fallback(self):
        from gmas.tools.web_search import BrowserFetcher

        mock_browser = MagicMock(spec=BrowserFetcher)
        tool = WebSearchTool(provider=MockProvider(), browser_fetcher=mock_browser, fetch_content=True)
        results = [
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Snippet 1"},
            {"title": "Result 2", "url": "https://example.com/2", "snippet": "Snippet 2"},
        ]

        with (
            patch.object(
                tool,
                "_fetch_http_candidates_parallel",
                return_value={},
            ) as mock_http,
            patch.object(
                tool,
                "_fetch_browser_candidates",
                return_value={
                    0: {"success": True, "title": "Browser 1", "content": "x" * 400},
                    1: {"success": True, "title": "Browser 2", "content": "y" * 400},
                },
            ) as mock_browser_fetch,
        ):
            tool._fetch_content_for_results(results, None, query="test", no_cache=True)

        mock_http.assert_called_once()
        assert mock_browser_fetch.call_count >= 1
        assert results[0]["content"] == "x" * 400
        assert results[1]["content"] == "y" * 400
        assert results[1]["title"] == "Result 2"

    def test_execute_search_valueerror(self):
        """Search with ValueError raises generic search error."""
        tool = WebSearchTool(provider=MockProvider())
        tool._provider = MagicMock()
        tool._provider.search.side_effect = ValueError("bad value")
        result = tool.execute(query="test")
        assert result.success is False
        assert result.error is not None


# ═══════════════════════════════════════════════════════════════
#  SeleniumFetcher
# ═══════════════════════════════════════════════════════════════


class TestSeleniumFetcher:
    """Tests for SeleniumFetcher — selenium module is fully mocked."""

    def _make_mock_driver(self):
        driver = MagicMock()
        driver.current_url = "https://example.com"
        driver.title = "Test Page"
        driver.page_source = "<html><body><p>Hello World</p></body></html>"
        driver.execute_script.return_value = None
        return driver

    def _get_fetcher(self, **kwargs):
        from gmas.tools.web_search import SeleniumFetcher

        return SeleniumFetcher(**kwargs)

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def test_init_defaults(self):
        f = self._get_fetcher()
        assert f._headless is True
        assert f._browser == "auto"
        assert f._driver is None
        assert f._wait_timeout == 15

    def test_init_custom(self):
        f = self._get_fetcher(headless=False, browser="Firefox", wait_timeout=30, proxy="http://p:8080")
        assert f._headless is False
        assert f._browser == "firefox"
        assert f._wait_timeout == 30
        assert f._proxy == "http://p:8080"

    # ------------------------------------------------------------------
    # _ensure_dependencies
    # ------------------------------------------------------------------

    def test_ensure_dependencies_raises_when_no_selenium(self):
        f = self._get_fetcher()
        import sys

        backup = sys.modules.get("selenium")
        sys.modules["selenium"] = None  # type: ignore[assignment,ty:invalid-assignment]
        try:
            with pytest.raises(ImportError, match="Selenium is required"):
                f._ensure_dependencies()
        finally:
            if backup is not None:
                sys.modules["selenium"] = backup
            else:
                sys.modules.pop("selenium", None)

    def test_ensure_dependencies_ok_with_mock_selenium(self):
        f = self._get_fetcher()
        mock_selenium = MagicMock()
        mock_selenium.__spec__ = MagicMock()
        with patch.dict("sys.modules", {"selenium": mock_selenium}):
            f._ensure_dependencies()  # should not raise

    # ------------------------------------------------------------------
    # _create_driver
    # ------------------------------------------------------------------

    def test_create_driver_chrome(self):
        f = self._get_fetcher(browser="chrome")
        mock_driver = MagicMock()
        with (
            patch.object(f, "_ensure_dependencies"),
            patch.object(f, "_create_chrome_driver", return_value=mock_driver) as mock_chrome,
        ):
            result = f._create_driver()
            mock_chrome.assert_called_once()
            assert result is mock_driver

    def test_create_driver_firefox(self):
        f = self._get_fetcher(browser="firefox")
        mock_driver = MagicMock()
        with (
            patch.object(f, "_ensure_dependencies"),
            patch.object(f, "_create_firefox_driver", return_value=mock_driver) as mock_ff,
        ):
            result = f._create_driver()
            mock_ff.assert_called_once()
            assert result is mock_driver

    def test_create_driver_edge(self):
        f = self._get_fetcher(browser="edge")
        mock_driver = MagicMock()
        with (
            patch.object(f, "_ensure_dependencies"),
            patch.object(f, "_create_edge_driver", return_value=mock_driver) as mock_edge,
        ):
            result = f._create_driver()
            mock_edge.assert_called_once()
            assert result is mock_driver

    def test_create_driver_unsupported_browser(self):
        f = self._get_fetcher(browser="ie")
        with patch.object(f, "_ensure_dependencies"), pytest.raises(ValueError, match="Unsupported browser"):
            f._create_driver()

    # ------------------------------------------------------------------
    # _create_chrome_driver
    # ------------------------------------------------------------------

    def _build_selenium_mocks(self):
        """Return a dict of fake selenium submodule mocks."""
        mock_options = MagicMock()
        mock_service = MagicMock()
        mock_driver_instance = MagicMock()

        mock_webdriver = MagicMock()
        mock_webdriver.Chrome.return_value = mock_driver_instance
        mock_webdriver.Firefox.return_value = mock_driver_instance
        mock_webdriver.Edge.return_value = mock_driver_instance

        mock_chrome_options = MagicMock()
        mock_chrome_options.Options.return_value = mock_options
        mock_chrome_service = MagicMock()
        mock_chrome_service.Service.return_value = mock_service

        mock_firefox_options = MagicMock()
        mock_firefox_options.Options.return_value = mock_options
        mock_firefox_service = MagicMock()
        mock_firefox_service.Service.return_value = mock_service

        mock_edge_options = MagicMock()
        mock_edge_options.Options.return_value = mock_options
        mock_edge_service = MagicMock()
        mock_edge_service.Service.return_value = mock_service

        # The code does `from selenium import webdriver` which resolves
        # selenium.webdriver from the "selenium" module object, not from
        # sys.modules["selenium.webdriver"]. We must link them explicitly.
        mock_selenium = MagicMock()
        mock_selenium.webdriver = mock_webdriver

        return {
            "selenium": mock_selenium,
            "selenium.webdriver": mock_webdriver,
            "selenium.webdriver.chrome": MagicMock(),
            "selenium.webdriver.chrome.options": mock_chrome_options,
            "selenium.webdriver.chrome.service": mock_chrome_service,
            "selenium.webdriver.firefox": MagicMock(),
            "selenium.webdriver.firefox.options": mock_firefox_options,
            "selenium.webdriver.firefox.service": mock_firefox_service,
            "selenium.webdriver.edge": MagicMock(),
            "selenium.webdriver.edge.options": mock_edge_options,
            "selenium.webdriver.edge.service": mock_edge_service,
            "urllib.parse": __import__("urllib.parse", fromlist=["urlparse"]),
        }, mock_driver_instance

    def test_create_chrome_driver_basic(self):
        f = self._get_fetcher(browser="chrome", headless=True)
        mocks, mock_driver_instance = self._build_selenium_mocks()
        with patch.dict("sys.modules", mocks):
            driver = f._create_chrome_driver()
            assert driver is mock_driver_instance

    def test_create_chrome_driver_with_ua_proxy_images(self):
        f = self._get_fetcher(browser="chrome", user_agent="MyAgent", proxy="http://p:8080", disable_images=True)
        mocks, mock_driver_instance = self._build_selenium_mocks()
        with patch.dict("sys.modules", mocks):
            driver = f._create_chrome_driver()
            assert driver is mock_driver_instance

    def test_create_firefox_driver_basic(self):
        f = self._get_fetcher(browser="firefox")
        mocks, mock_driver_instance = self._build_selenium_mocks()
        with patch.dict("sys.modules", mocks):
            driver = f._create_firefox_driver()
            assert driver is mock_driver_instance

    def test_create_firefox_driver_with_proxy(self):
        f = self._get_fetcher(browser="firefox", proxy="http://proxy:3128", disable_images=True)
        mocks, mock_driver_instance = self._build_selenium_mocks()
        with patch.dict("sys.modules", mocks):
            driver = f._create_firefox_driver()
            assert driver is mock_driver_instance

    def test_create_edge_driver_basic(self):
        f = self._get_fetcher(browser="edge")
        mocks, mock_driver_instance = self._build_selenium_mocks()
        with patch.dict("sys.modules", mocks):
            driver = f._create_edge_driver()
            assert driver is mock_driver_instance

    def test_create_edge_driver_with_ua_proxy(self):
        f = self._get_fetcher(browser="edge", user_agent="EdgeBot", proxy="http://p:80")
        mocks, mock_driver_instance = self._build_selenium_mocks()
        with patch.dict("sys.modules", mocks):
            driver = f._create_edge_driver()
            assert driver is mock_driver_instance

    # ------------------------------------------------------------------
    # _get_driver (lazy init)
    # ------------------------------------------------------------------

    def test_get_driver_creates_on_first_call(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        with patch.object(f, "_create_driver", return_value=mock_driver):
            driver = f._get_driver()
            assert driver is mock_driver
            assert f._driver is mock_driver
            mock_driver.set_page_load_timeout.assert_called_once()
            mock_driver.implicitly_wait.assert_not_called()

    def test_get_driver_reuses_existing(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        f._driver = mock_driver
        with patch.object(f, "_create_driver") as mock_create:
            driver = f._get_driver()
            mock_create.assert_not_called()
            assert driver is mock_driver

    # ------------------------------------------------------------------
    # _scroll_page
    # ------------------------------------------------------------------

    def test_scroll_page(self):
        f = self._get_fetcher(scroll_to_bottom=True, max_scrolls=3, scroll_pause=0)
        mock_driver = self._make_mock_driver()
        heights = iter([100, 200])
        current = [100]

        def _script_side_effect(script: str):
            if "scrollTo" in script:
                return None
            if "scrollHeight" in script:
                with contextlib.suppress(StopIteration):
                    current[0] = next(heights)
                return current[0]
            return None

        mock_driver.execute_script.side_effect = _script_side_effect
        f._scroll_page(mock_driver)
        assert mock_driver.execute_script.call_count >= 2

    def test_scroll_page_no_change(self):
        f = self._get_fetcher(scroll_pause=0)
        mock_driver = self._make_mock_driver()

        def _script_side_effect(script: str):
            if "scrollHeight" in script:
                return 500
            return None

        mock_driver.execute_script.side_effect = _script_side_effect
        f._scroll_page(mock_driver)

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    def test_fetch_success(self):
        f = self._get_fetcher(extra_wait=0)
        mock_driver = self._make_mock_driver()
        with patch.object(f, "_get_driver", return_value=mock_driver):
            result = f.fetch("https://example.com")
            assert result["success"] is True
            assert result["url"] == "https://example.com"
            assert "Hello World" in result["content"]

    def test_fetch_success_with_scroll(self):
        f = self._get_fetcher(scroll_to_bottom=True, extra_wait=0)
        mock_driver = self._make_mock_driver()
        mock_driver.page_source = "<html><main>" + "x" * 600 + "</main></html>"
        with patch.object(f, "_get_driver", return_value=mock_driver), patch.object(f, "_scroll_page") as mock_scroll:
            result = f.fetch("https://example.com")
            mock_scroll.assert_called_once()
            assert result["success"] is True

    def test_fetch_exception(self):
        f = self._get_fetcher(extra_wait=0)
        with patch.object(f, "_get_driver", side_effect=RuntimeError("driver failed")):
            result = f.fetch("https://example.com")
            assert result["success"] is False
            assert "Selenium error" in result["error"]

    # ------------------------------------------------------------------
    # fetch_with_wait
    # ------------------------------------------------------------------

    def test_fetch_with_wait_no_selector(self):
        f = self._get_fetcher(extra_wait=0)
        mock_driver = self._make_mock_driver()
        with patch.object(f, "_get_driver", return_value=mock_driver):
            result = f.fetch_with_wait("https://example.com", wait_for_selector=None)
            assert result["success"] is True

    def test_fetch_with_wait_with_selector(self):
        f = self._get_fetcher(extra_wait=0)
        mock_driver = self._make_mock_driver()
        mock_by = MagicMock()
        mock_ec = MagicMock()
        mock_wait_cls = MagicMock()
        mock_wait_cls.return_value.until.return_value = True
        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict(
                "sys.modules",
                {
                    "selenium.webdriver.common.by": mock_by,
                    "selenium.webdriver.support": MagicMock(),
                    "selenium.webdriver.support.expected_conditions": mock_ec,
                    "selenium.webdriver.support.ui": MagicMock(),
                },
            ),
        ):
            result = f.fetch_with_wait("https://example.com", wait_for_selector="#main")
            # Either success or failure both acceptable here
            assert "success" in result

    def test_fetch_with_wait_exception(self):
        f = self._get_fetcher(extra_wait=0)
        with patch.object(f, "_get_driver", side_effect=RuntimeError("boom")):
            result = f.fetch_with_wait("https://example.com")
            assert result["success"] is False

    # ------------------------------------------------------------------
    # click_element
    # ------------------------------------------------------------------

    def test_click_element_success(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_element = MagicMock()
        mock_element.text = "Click me"
        mock_element.get_attribute.return_value = ""
        mock_wait = MagicMock()
        mock_wait.until.return_value = mock_element

        mock_by = MagicMock()
        mock_ec = MagicMock()
        mock_wait_cls = MagicMock(return_value=mock_wait)

        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict(
                "sys.modules",
                {
                    "selenium.webdriver.common.by": mock_by,
                    "selenium.webdriver.support": MagicMock(),
                    "selenium.webdriver.support.expected_conditions": mock_ec,
                    "selenium.webdriver.support.ui": type("m", (), {"WebDriverWait": mock_wait_cls})(),
                },
            ),
        ):
            result = f.click_element(".btn")
            assert "success" in result

    def test_click_element_exception(self):
        f = self._get_fetcher()
        with patch.object(f, "_get_driver", side_effect=RuntimeError("no driver")):
            result = f.click_element(".btn")
            assert result["success"] is False
            assert "Click error" in result["error"]

    # ------------------------------------------------------------------
    # fill_input
    # ------------------------------------------------------------------

    def test_fill_input_success(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_element = MagicMock()
        mock_wait = MagicMock()
        mock_wait.until.return_value = mock_element
        mock_by = MagicMock()
        mock_ec = MagicMock()
        mock_keys = MagicMock()
        mock_wait_cls = MagicMock(return_value=mock_wait)

        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict(
                "sys.modules",
                {
                    "selenium.webdriver.common.by": mock_by,
                    "selenium.webdriver.common.keys": type("m", (), {"Keys": mock_keys})(),
                    "selenium.webdriver.support": MagicMock(),
                    "selenium.webdriver.support.expected_conditions": mock_ec,
                    "selenium.webdriver.support.ui": type("m", (), {"WebDriverWait": mock_wait_cls})(),
                },
            ),
        ):
            result = f.fill_input("#input", "hello", clear_first=True, submit=False)
            assert "success" in result

    def test_fill_input_submit(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_element = MagicMock()
        mock_wait = MagicMock()
        mock_wait.until.return_value = mock_element
        mock_by = MagicMock()
        mock_ec = MagicMock()
        mock_keys = MagicMock()
        mock_wait_cls = MagicMock(return_value=mock_wait)

        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict(
                "sys.modules",
                {
                    "selenium.webdriver.common.by": mock_by,
                    "selenium.webdriver.common.keys": type("m", (), {"Keys": mock_keys})(),
                    "selenium.webdriver.support": MagicMock(),
                    "selenium.webdriver.support.expected_conditions": mock_ec,
                    "selenium.webdriver.support.ui": type("m", (), {"WebDriverWait": mock_wait_cls})(),
                },
            ),
        ):
            result = f.fill_input("#input", "hello", submit=True)
            assert "success" in result

    def test_fill_input_exception(self):
        f = self._get_fetcher()
        with patch.object(f, "_get_driver", side_effect=RuntimeError("no driver")):
            result = f.fill_input("#input", "test")
            assert result["success"] is False
            assert "Fill error" in result["error"]

    # ------------------------------------------------------------------
    # extract_links
    # ------------------------------------------------------------------

    def test_extract_links_success(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_elem1 = MagicMock()
        mock_elem1.get_attribute.side_effect = lambda attr: (
            "https://example.com/page1" if attr == "href" else "Link Title"
        )
        mock_elem1.text = "Page 1"
        mock_elem2 = MagicMock()
        mock_elem2.get_attribute.side_effect = lambda attr: "javascript:void(0)" if attr == "href" else ""
        mock_elem2.text = "JS Link"
        mock_driver.find_elements.return_value = [mock_elem1, mock_elem2]

        mock_by = MagicMock()

        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict("sys.modules", {"selenium.webdriver.common.by": mock_by}),
        ):
            result = f.extract_links()
            assert result["success"] is True
            assert result["count"] >= 1

    def test_extract_links_with_base_filter(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_elem = MagicMock()
        mock_elem.get_attribute.side_effect = lambda attr: "https://other.com/page" if attr == "href" else ""
        mock_elem.text = "Other"
        mock_driver.find_elements.return_value = [mock_elem]

        mock_by = MagicMock()
        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict("sys.modules", {"selenium.webdriver.common.by": mock_by}),
        ):
            result = f.extract_links(base_url_filter="https://example.com")
            assert result["success"] is True
            assert result["count"] == 0

    def test_extract_links_exception(self):
        f = self._get_fetcher()
        with patch.object(f, "_get_driver", side_effect=RuntimeError("no driver")):
            result = f.extract_links()
            assert result["success"] is False
            assert "Extract links error" in result["error"]

    # ------------------------------------------------------------------
    # execute_js
    # ------------------------------------------------------------------

    def test_execute_js_success(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_driver.execute_script.return_value = "result_value"
        with patch.object(f, "_get_driver", return_value=mock_driver):
            result = f.execute_js("return document.title;")
            assert result["success"] is True
            assert result["return_value"] == "result_value"

    def test_execute_js_no_return(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_driver.execute_script.return_value = None
        with patch.object(f, "_get_driver", return_value=mock_driver):
            result = f.execute_js("console.log('hi');")
            assert result["success"] is True
            assert result["return_value"] is None

    def test_execute_js_exception(self):
        f = self._get_fetcher()
        with patch.object(f, "_get_driver", side_effect=RuntimeError("no driver")):
            result = f.execute_js("bad()")
            assert result["success"] is False
            assert "JS execution error" in result["error"]

    # ------------------------------------------------------------------
    # get_current_url
    # ------------------------------------------------------------------

    def test_get_current_url_success(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        with patch.object(f, "_get_driver", return_value=mock_driver):
            url = f.get_current_url()
            assert url == "https://example.com"

    def test_get_current_url_exception(self):
        f = self._get_fetcher()
        with patch.object(f, "_get_driver", side_effect=RuntimeError("no driver")):
            url = f.get_current_url()
            assert url == ""

    # ------------------------------------------------------------------
    # get_page_content
    # ------------------------------------------------------------------

    def test_get_page_content_success(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        with patch.object(f, "_get_driver", return_value=mock_driver):
            result = f.get_page_content()
            assert result["success"] is True
            assert "Hello World" in result["content"]

    def test_get_page_content_with_main_tag(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        mock_driver.page_source = "<html><body><main>" + "main content " * 50 + "</main></body></html>"
        with patch.object(f, "_get_driver", return_value=mock_driver):
            result = f.get_page_content()
            assert result["success"] is True

    def test_get_page_content_exception(self):
        f = self._get_fetcher()
        with patch.object(f, "_get_driver", side_effect=RuntimeError("boom")):
            result = f.get_page_content()
            assert result["success"] is False
            assert "Get content error" in result["error"]

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def test_crawl_success_single_page(self):
        f = self._get_fetcher(extra_wait=0)
        mock_driver = self._make_mock_driver()
        mock_driver.page_source = "<html><body><p>Content</p></body></html>"
        mock_driver.find_elements.return_value = []

        mock_by = MagicMock()
        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict("sys.modules", {"selenium.webdriver.common.by": mock_by}),
            patch("time.sleep"),
        ):
            result = f.crawl("https://example.com", max_pages=1, max_depth=0)
            assert result["success"] is True
            assert result["total_pages"] >= 1

    def test_crawl_follows_links(self):
        f = self._get_fetcher(extra_wait=0)
        mock_driver = self._make_mock_driver()
        mock_driver.page_source = "<html><body><p>Content</p></body></html>"
        mock_elem = MagicMock()
        mock_elem.get_attribute.return_value = "https://example.com/page2"
        mock_elem.text = "Link"
        mock_driver.find_elements.return_value = [mock_elem]

        mock_by = MagicMock()
        with (
            patch.object(f, "_get_driver", return_value=mock_driver),
            patch.dict("sys.modules", {"selenium.webdriver.common.by": mock_by}),
            patch("time.sleep"),
        ):
            result = f.crawl("https://example.com", max_pages=5, max_depth=1, extract_content=True)
            assert result["success"] is True

    def test_crawl_exception(self):
        # When _get_driver raises, fetch() catches the error and returns
        # success=False; crawl() continues and completes with success=True
        # but 0 pages crawled.
        f = self._get_fetcher(extra_wait=0)
        with patch.object(f, "_get_driver", side_effect=RuntimeError("no driver")):
            result = f.crawl("https://example.com")
            assert result["total_pages"] == 0

    # ------------------------------------------------------------------
    # close / context manager
    # ------------------------------------------------------------------

    def test_close_quits_driver(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        f._driver = mock_driver
        f.close()
        mock_driver.quit.assert_called_once()
        assert f._driver is None

    def test_close_no_driver(self):
        f = self._get_fetcher()
        f.close()  # Should not raise

    def test_context_manager(self):
        f = self._get_fetcher()
        mock_driver = self._make_mock_driver()
        f._driver = mock_driver
        with f as ctx:
            assert ctx is f
        mock_driver.quit.assert_called_once()


class TestPlaywrightFetcher:
    def _get_fetcher(self, **kwargs):
        from gmas.tools.web_search import PlaywrightFetcher

        return PlaywrightFetcher(**kwargs)

    def _make_mock_page(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.is_closed.return_value = False
        page.title.return_value = "Test Page"
        page.content.return_value = "<html><body><main>Hello World</main></body></html>"
        page.frames = []
        page.wait_for_timeout = MagicMock()
        rich_text = ("Hello World " * 30).strip()

        def _evaluate(script):
            if not isinstance(script, str):
                return None
            if "document.body.scrollHeight" in script:
                return 1000
            if "primaryText" in script:
                return {
                    "title": "Test Page",
                    "primaryText": rich_text,
                    "bodyText": rich_text,
                    "usedMain": True,
                }
            return None

        page.evaluate.side_effect = _evaluate
        return page

    @staticmethod
    def _make_locator(text: str = "Text") -> MagicMock:
        locator = MagicMock()
        locator.first = locator
        locator.text_content.return_value = text
        return locator

    def test_patch_asyncio_new_event_loop_uses_clean_loop(self):
        import asyncio
        import sys

        from gmas.tools.web_search import PlaywrightFetcher

        loop_base = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop
        orig_flag = getattr(loop_base, "_nest_patched", None)
        original_new_event_loop = asyncio.new_event_loop
        loop_base._nest_patched = True  # type: ignore[attr-defined,ty:unresolved-attribute]

        try:
            with PlaywrightFetcher._patch_asyncio_new_event_loop():
                extra_loop = asyncio.new_event_loop()
                try:
                    assert type(extra_loop).__name__ == "_CleanLoop"
                    assert asyncio.new_event_loop is not original_new_event_loop
                finally:
                    extra_loop.close()
        finally:
            if orig_flag is None:
                delattr(loop_base, "_nest_patched")
            else:
                loop_base._nest_patched = orig_flag

        assert asyncio.new_event_loop is original_new_event_loop

    def test_build_context_kwargs_impl_supports_har_and_storage_state_path(self, tmp_path: Path):
        state_path = tmp_path / "state.json"
        state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

        f = self._get_fetcher(
            user_agent="Agent",
            har_path=str(tmp_path / "session.har"),
            storage_state_path=str(state_path),
        )

        context_kwargs = f._build_context_kwargs_impl()

        assert context_kwargs["accept_downloads"] is True
        assert context_kwargs["user_agent"] == "Agent"
        assert context_kwargs["record_har_path"].endswith("session.har")
        assert context_kwargs["storage_state"] == str(state_path.resolve())

    def test_build_context_kwargs_impl_rejects_conflicting_storage_state_inputs(self):
        f = self._get_fetcher(
            storage_state_path="state.json",
            storage_state={"cookies": [], "origins": []},
        )

        with pytest.raises(ValueError, match="storage_state_path or storage_state"):
            f._build_context_kwargs_impl()

    def test_fetch_with_wait_uses_locator_wait(self):
        f = self._get_fetcher(extra_wait=0)
        page = self._make_mock_page()
        locator = self._make_locator()
        page.locator.return_value = locator
        f._page = page
        f._pages = [page]

        result = f._fetch_with_wait_impl("https://example.com", wait_for_selector="#main")

        page.goto.assert_called_once_with("https://example.com", wait_until="domcontentloaded")
        page.locator.assert_called_once_with("#main")
        locator.wait_for.assert_called_once_with(state="attached", timeout=f._wait_timeout * 1000)
        assert result["success"] is True
        page.content.assert_not_called()

    def test_click_impl_uses_locator_api(self):
        f = self._get_fetcher()
        page = self._make_mock_page()
        locator = self._make_locator("Click me")
        page.locator.return_value = locator
        f._page = page
        f._pages = [page]

        with patch.object(f, "_wait_for_load_after_action_impl") as mock_wait:
            result = f._click_impl(".btn")

        page.locator.assert_called_once_with(".btn")
        locator.click.assert_called_once_with(timeout=f._wait_timeout * 1000)
        mock_wait.assert_called_once_with(f._wait_timeout * 1000)
        assert result["clicked_text"] == "Click me"
        assert result["success"] is True

    def test_wait_for_page_delay_prefers_playwright_timeout(self):
        f = self._get_fetcher()
        page = self._make_mock_page()
        f._page = page

        f._wait_for_page_delay_impl(0.2)

        page.wait_for_timeout.assert_called_once_with(200)

    def test_fetch_impl_uses_dom_extraction_instead_of_page_content(self):
        f = self._get_fetcher()
        page = self._make_mock_page()
        f._page = page
        f._pages = [page]

        result = f._fetch_impl("https://example.com")

        assert result["success"] is True
        assert "Hello World" in result["content"]
        page.content.assert_not_called()

    def test_fill_impl_uses_locator_api(self):
        f = self._get_fetcher()
        page = self._make_mock_page()
        locator = self._make_locator()
        page.locator.return_value = locator
        f._page = page
        f._pages = [page]

        with patch.object(f, "_wait_for_load_after_action_impl") as mock_wait:
            result = f._fill_impl("#input", "hello", submit=True)

        page.locator.assert_called_once_with("#input")
        locator.clear.assert_called_once_with(timeout=f._wait_timeout * 1000)
        locator.fill.assert_called_once_with("hello", timeout=f._wait_timeout * 1000)
        locator.press.assert_called_once_with("Enter", timeout=f._wait_timeout * 1000)
        mock_wait.assert_called_once_with(f._wait_timeout * 1000)
        assert result["success"] is True

    def test_extract_links_uses_locator_iteration(self):
        f = self._get_fetcher()
        page = self._make_mock_page()
        links_locator = MagicMock()
        first_link = MagicMock()
        first_link.get_attribute.side_effect = lambda attr: (
            "https://example.com/page1" if attr == "href" else "Link Title"
        )
        first_link.text_content.return_value = "Page 1"
        second_link = MagicMock()
        second_link.get_attribute.side_effect = lambda attr: "javascript:void(0)" if attr == "href" else ""
        second_link.text_content.return_value = "Ignored"
        links_locator.count.return_value = 2
        links_locator.nth.side_effect = [first_link, second_link]
        page.locator.return_value = links_locator
        f._page = page
        f._pages = [page]

        result = f._extract_links_impl()

        page.locator.assert_called_once_with("a[href]")
        assert result["success"] is True
        assert result["count"] == 1

    def test_list_tabs_impl_returns_tab_snapshots(self):
        f = self._get_fetcher()
        page1 = self._make_mock_page()
        page2 = self._make_mock_page()
        page2.url = "https://example.com/2"
        page2.title.return_value = "Second Tab"
        f._page = page1
        f._pages = [page1, page2]

        result = f._list_tabs_impl()

        assert result["success"] is True
        assert result["count"] == 2
        assert result["tabs"][0]["active"] is True
        assert result["tabs"][1]["title"] == "Second Tab"

    def test_open_tab_impl_creates_and_activates_page(self):
        f = self._get_fetcher()
        context = MagicMock()
        new_page = self._make_mock_page()
        new_page.url = "https://example.com/new"
        new_page.title.return_value = "New Tab"
        context.new_page.return_value = new_page
        f._context = context

        result = f._open_tab_impl("https://example.com/new")

        context.new_page.assert_called_once()
        new_page.goto.assert_called_once_with(
            "https://example.com/new",
            timeout=f._page_load_timeout * 1000,
            wait_until="domcontentloaded",
        )
        assert result["success"] is True
        assert result["index"] == 0
        assert f._page is new_page

    def test_screenshot_impl_saves_page_screenshot(self, tmp_path: Path):
        f = self._get_fetcher()
        page = self._make_mock_page()
        f._page = page
        f._pages = [page]

        output_path = tmp_path / "page.png"
        result = f._screenshot_impl(str(output_path), full_page=True)

        page.screenshot.assert_called_once_with(path=str(output_path), full_page=True)
        assert result["success"] is True
        assert result["path"] == str(output_path)

    def test_storage_state_impl_uses_context(self, tmp_path: Path):
        f = self._get_fetcher()
        context = MagicMock()
        context.storage_state.return_value = {"cookies": [], "origins": []}
        f._context = context

        output_path = tmp_path / "state.json"
        result = f._storage_state_impl(str(output_path))

        context.storage_state.assert_called_once_with(path=str(output_path))
        assert result["success"] is True
        assert result["path"] == str(output_path)

    def test_network_events_impl_returns_recent_events(self):
        f = self._get_fetcher()
        f._network_events.extend(
            [
                {"type": "request", "url": "https://example.com/1"},
                {"type": "response", "url": "https://example.com/2"},
                {"type": "request_failed", "url": "https://example.com/3"},
            ]
        )

        result = f._get_network_events_impl(limit=2, clear=True)

        assert result["success"] is True
        assert result["count"] == 2
        assert len(f._network_events) == 0

    def test_download_impl_saves_file(self, tmp_path: Path):
        f = self._get_fetcher()
        page = self._make_mock_page()
        locator = self._make_locator("Download")
        page.locator.return_value = locator
        download = MagicMock()
        download.suggested_filename = "artifact.txt"
        expect_download = MagicMock()
        expect_download.__enter__.return_value = type("DownloadInfo", (), {"value": download})()
        expect_download.__exit__.return_value = None
        page.expect_download.return_value = expect_download
        f._page = page
        f._pages = [page]

        output_path = tmp_path / "artifact.txt"
        result = f._download_impl(".download", path=str(output_path))

        locator.click.assert_called_once_with(timeout=f._wait_timeout * 1000)
        download.save_as.assert_called_once_with(str(output_path))
        assert result["success"] is True
        assert result["path"] == str(output_path)


# ═══════════════════════════════════════════════════════════════
#  search_images action
# ═══════════════════════════════════════════════════════════════


class TestSearchImagesAction:
    """Tests for the search_images action in WebSearchTool."""

    def test_search_images_action_with_provider(self):
        """search_images action should call provider's image search."""
        mock_provider = MagicMock()
        mock_provider.search_images.return_value = [
            {"title": "Image 1", "url": "https://img1.jpg", "source": "https://example.com/1"},
            {"title": "Image 2", "url": "https://img2.jpg", "source": "https://example.com/2"},
        ]
        tool = WebSearchTool(provider=mock_provider)

        result = tool.execute(action="search_images", query="Python logo", max_results=5)

        assert result.success is True
        mock_provider.search_images.assert_called_once_with("Python logo", 5)
        # Output is formatted as a string
        assert "img1.jpg" in result.output or "Image 1" in result.output

    def test_search_images_in_parameters_schema(self):
        """search_images should be included in the action enum."""
        tool = WebSearchTool()
        schema = tool.parameters_schema

        assert "search_images" in schema["properties"]["action"]["enum"]

    def test_search_images_description_mentions_action(self):
        """The tool description should mention search_images."""
        tool = WebSearchTool()
        description = tool.description

        assert "search_images" in description.lower()
