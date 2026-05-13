"""WebSearchTool — the main tool class."""

import contextlib
import dataclasses
import hashlib
import json
import time as _time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, ClassVar, Self
from uuid import uuid4

from gmas.config.logging import logger

from ..base import BaseTool, ToolResult
from ._cache import SearchCache
from ._fetchers import BrowserFetcher, PlaywrightFetcher, SeleniumFetcher, URLFetcher
from ._policy import WebSearchPolicy
from ._providers import DuckDuckGoProvider, SearchError, SearchProvider
from ._router import SearchRouter
from ._utils import deduplicate_results

if TYPE_CHECKING:
    from collections.abc import Callable

_CALLBACK_RESOLVER_UNSET = object()


def _infer_backend_name(fetcher: BrowserFetcher) -> str:
    if isinstance(fetcher, PlaywrightFetcher):
        return "playwright"
    if isinstance(fetcher, SeleniumFetcher):
        return "selenium"
    return type(fetcher).__name__.lower()


class WebSearchTool(BaseTool):
    """Tool for searching the web and interacting with web pages."""

    _DEEP_SEARCH_BACKENDS: ClassVar[dict[str, type[BrowserFetcher]]] = {
        "selenium": SeleniumFetcher,
        "playwright": PlaywrightFetcher,
    }

    def __init__(
        self,
        provider: SearchProvider | None = None,
        max_results: int = 5,
        max_content_length: int = 4000,
        *,
        fetch_content: bool = False,
        max_fetch_pages: int | None = None,
        timeout: int = 15,
        deep_search: str | None = None,
        browser_config: dict[str, Any] | None = None,
        browser_fetcher: BrowserFetcher | None = None,
        callback_manager: Any | None = None,
        cache: SearchCache | bool | None = None,
        cache_ttl: float = 300.0,
        cache_max_entries: int = 256,
        deduplicate: bool = True,
        trust_env: bool = False,
        policy: WebSearchPolicy | None = None,
    ):
        self._provider = provider or DuckDuckGoProvider(timeout=timeout, trust_env=trust_env)
        self._max_results = max_results
        self._max_content_length = max_content_length
        self._fetch_content = fetch_content
        self._timeout = timeout
        self._trust_env = trust_env
        self._fetcher = URLFetcher(timeout=timeout, max_content_length=500_000, trust_env=trust_env)
        self._callback_manager = callback_manager
        self._callback_resolver: Any = _CALLBACK_RESOLVER_UNSET
        self._deduplicate = deduplicate
        self._policy = policy or WebSearchPolicy()

        if isinstance(cache, SearchCache):
            self._cache: SearchCache | None = cache
        elif cache is True:
            self._cache = SearchCache(max_entries=cache_max_entries, ttl=cache_ttl)
        else:
            self._cache = None

        self._router: SearchRouter | None = None
        self._available_providers: dict[str, SearchProvider] | None = None
        self._cache_ns: str | None = None

        self._browser_fetcher: BrowserFetcher | None = None
        self._deep_search: str | None = None

        if browser_fetcher is not None:
            self._browser_fetcher = browser_fetcher
            self._deep_search = _infer_backend_name(browser_fetcher)
        elif deep_search is not None:
            ds = deep_search.lower()
            if ds not in self._DEEP_SEARCH_BACKENDS:
                msg = (
                    f"Unknown deep_search backend {deep_search!r}. "
                    f"Supported: {', '.join(sorted(self._DEEP_SEARCH_BACKENDS))}"
                )
                raise ValueError(msg)
            config = browser_config or {}
            self._browser_fetcher = self._DEEP_SEARCH_BACKENDS[ds](**config)
            self._deep_search = ds

        if max_fetch_pages is not None:
            self._max_fetch_pages = max_fetch_pages
        elif self._browser_fetcher is not None:
            self._max_fetch_pages = self._policy.default_browser_fetch_pages
        else:
            self._max_fetch_pages = self._policy.default_http_fetch_pages

        self._warm_up_started = False

    # ------------------------------------------------------------------
    # Cache namespace
    # ------------------------------------------------------------------

    _PROVIDER_IDENTITY_ATTRS: ClassVar[tuple[str, ...]] = (
        "_base_url",
        "_instance_url",
        "_cse_id",
    )

    @classmethod
    def _provider_fingerprint(cls, provider: SearchProvider) -> str:
        parts = [type(provider).__name__]
        for attr in cls._PROVIDER_IDENTITY_ATTRS:
            val = getattr(provider, attr, None)
            if val:
                parts.append(f"{attr}={val}")
        return ":".join(parts)

    def _get_cache_ns(self) -> str:
        if self._cache_ns is None:
            parts: list[str] = []
            parts.append(self._provider_fingerprint(self._provider))
            if self._available_providers:
                parts.extend(
                    f"{name}={self._provider_fingerprint(self._available_providers[name])}"
                    for name in sorted(self._available_providers)
                )
            raw = "|".join(parts)
            self._cache_ns = hashlib.sha256(raw.encode()).hexdigest()[:12]
        return self._cache_ns

    def set_router(
        self,
        router: SearchRouter,
        available_providers: dict[str, SearchProvider],
    ) -> None:
        """Attach a router and provider map (used by the factory)."""
        self._router = router
        self._available_providers = available_providers

    # ------------------------------------------------------------------

    def warm_up(self) -> None:
        """Pre-initialise the browser in the background (idempotent)."""
        if self._browser_fetcher is not None and not self._warm_up_started:
            self._warm_up_started = True
            import threading

            threading.Thread(target=self._browser_fetcher.warm_up, daemon=True).start()

    def _get_callback_manager(self) -> Any | None:
        if self._callback_manager is not None:
            return self._callback_manager
        if self._callback_resolver is _CALLBACK_RESOLVER_UNSET:
            try:
                from gmas.callbacks.context import get_callback_manager
            except ImportError:
                self._callback_resolver = None
            else:
                self._callback_resolver = get_callback_manager

        if self._callback_resolver is None:
            return None

        try:
            return self._callback_resolver()
        except (LookupError, RuntimeError):
            return None

    def _emit_tool_start(self, action: str, arguments: dict[str, Any] | None = None) -> None:
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_start(
                    uuid4(),
                    tool_name=self.name,
                    action=action,
                    arguments=arguments or {},
                )

    def _emit_tool_end(
        self,
        action: str,
        *,
        success: bool = True,
        output_size: int = 0,
        duration_ms: float = 0.0,
        result_summary: str = "",
    ) -> None:
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_end(
                    uuid4(),
                    tool_name=self.name,
                    action=action,
                    success=success,
                    output_size=output_size,
                    duration_ms=duration_ms,
                    result_summary=result_summary,
                )

    def _emit_tool_error(self, action: str, error: Exception) -> None:
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_error(
                    uuid4(),
                    tool_name=self.name,
                    action=action,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        base = (
            "Search the web and interact with web pages. "
            "Use 'query' to search for information. "
            "Use 'url' to read a specific web page. "
            "Use action='search_images' with 'query' to search for images. "
            "Set 'fetch_content=true' to automatically read the content of search results. "
            "Returns search results with titles, URLs, snippets, and optionally full page content.\n"
            "Per-call overrides: 'provider' forces a specific search backend, "
            "'intent' overrides auto-detected routing intent, "
            "'no_cache' bypasses the cache, "
            "'deduplicate' controls result deduplication."
        )
        if self._browser_fetcher is not None:
            backend = self._deep_search or "browser"
            base += (
                f"\n\nThis tool uses a real browser ({backend}) and supports advanced actions:\n"
                "- action='click': Click an element by CSS selector.\n"
                "- action='fill': Fill an input field and optionally submit.\n"
                "- action='extract_links': Extract all links from the current/specified page.\n"
                "- action='execute_js': Execute JavaScript code on the current page.\n"
                "- action='crawl': Recursively crawl a website collecting content.\n"
                "- action='get_content': Get text content of the current page.\n"
                "Use 'wait_for_selector' to wait for a specific element before extracting content."
            )
        if self._supports_advanced_session(self._browser_fetcher):
            base += (
                "\n\nPlaywright session actions:\n"
                "- action='open_tab' / 'list_tabs' / 'switch_tab' / 'close_tab': manage tabs.\n"
                "- action='screenshot': save a screenshot of the current page or element.\n"
                "- action='download': click an element and save the downloaded file.\n"
                "- action='list_frames': inspect page frames.\n"
                "- action='get_cookies' / 'add_cookies' / 'storage_state': manage session state.\n"
                "- action='start_tracing' / 'stop_tracing': capture a Playwright trace.\n"
                "- action='network_events': inspect captured request/response events.\n"
                "Constructor-level Playwright options also support persisted session restore "
                "(`storage_state_path` / `storage_state`) and HAR recording (`har_path`)."
            )
        return base

    @property
    def parameters_schema(self) -> dict[str, Any]:
        action_enum = ["search", "search_images", "fetch"]
        action_description = "Action to perform. Default: auto-detected from query/url."
        advanced_browser = self._supports_advanced_session(self._browser_fetcher)

        if self._browser_fetcher is not None:
            action_enum = [
                "search",
                "search_images",
                "fetch",
                "click",
                "fill",
                "extract_links",
                "execute_js",
                "crawl",
                "get_content",
            ]
            if advanced_browser:
                action_enum.extend(
                    [
                        "open_tab",
                        "list_tabs",
                        "switch_tab",
                        "close_tab",
                        "screenshot",
                        "download",
                        "list_frames",
                        "get_cookies",
                        "add_cookies",
                        "storage_state",
                        "start_tracing",
                        "stop_tracing",
                        "network_events",
                    ]
                )
            action_description = (
                "Browser action to perform. Default: auto-detected from query/url. "
                "Use 'click' to click elements, 'fill' to fill forms, "
                "'extract_links' to get all links, 'execute_js' to run JavaScript, "
                "'crawl' to recursively browse a site, 'get_content' to read current page."
            )

        provider_names: list[str] = []
        if self._available_providers:
            provider_names = sorted(self._available_providers.keys())
        elif self._provider is not None:
            pname = getattr(self._provider, "name", None)
            if pname:
                provider_names = [pname]
        _well_known = ["duckduckgo", "brave", "tavily", "serper", "searxng"]
        for wk in _well_known:
            if wk not in provider_names:
                provider_names.append(wk)

        intent_names = [*sorted(SearchRouter.INTENT_SIGNALS.keys()), "general"]

        properties: dict[str, Any] = {
            "query": {"type": "string", "description": "Search query."},
            "url": {"type": "string", "description": "URL of a specific web page to read/open."},
            "fetch_content": {
                "type": "boolean",
                "description": "If true, automatically fetch full content of found pages.",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum number of search results (1–10). Default: {self._max_results}.",
            },
            "action": {"type": "string", "enum": action_enum, "description": action_description},
            "provider": {
                "type": "string",
                "enum": provider_names,
                "description": "Override the search provider for this call.",
            },
            "intent": {
                "type": "string",
                "enum": intent_names,
                "description": "Override the auto-detected query intent for routing.",
            },
            "no_cache": {"type": "boolean", "description": "Bypass cache for this call."},
            "deduplicate": {"type": "boolean", "description": "Override deduplication setting."},
        }

        if self._browser_fetcher is not None:
            properties.update(
                {
                    "selector": {"type": "string", "description": "CSS selector for click/fill/extract_links."},
                    "value": {"type": "string", "description": "Value for fill action."},
                    "submit": {"type": "boolean", "description": "Press Enter after fill."},
                    "js_code": {"type": "string", "description": "JavaScript code for execute_js."},
                    "wait_for_selector": {"type": "string", "description": "CSS selector to wait for."},
                    "wait_timeout": {"type": "integer", "description": "Timeout for click/fill element wait."},
                    "max_depth": {"type": "integer", "description": "Max crawl depth. Default: 2."},
                    "max_pages": {"type": "integer", "description": "Max pages to crawl. Default: 10."},
                    "max_links": {"type": "integer", "description": "Max links to return. Default: 50."},
                    "url_filter": {"type": "string", "description": "URL prefix filter for crawl/extract_links."},
                }
            )
            if advanced_browser:
                properties.update(
                    {
                        "tab_index": {"type": "integer", "description": "Tab index for switch_tab/close_tab."},
                        "background": {"type": "boolean", "description": "Open a new tab in the background."},
                        "path": {
                            "type": "string",
                            "description": "Output path for screenshot/download/storage_state/stop_tracing.",
                        },
                        "full_page": {"type": "boolean", "description": "Capture the full page in screenshot action."},
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional URLs filter for get_cookies.",
                        },
                        "cookies": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Cookies to add with add_cookies action.",
                        },
                        "limit": {"type": "integer", "description": "Maximum number of items to return."},
                        "clear": {
                            "type": "boolean",
                            "description": "Clear captured network events after reading them.",
                        },
                        "trace_screenshots": {
                            "type": "boolean",
                            "description": "Include screenshots when starting tracing.",
                        },
                        "trace_snapshots": {
                            "type": "boolean",
                            "description": "Include snapshots when starting tracing.",
                        },
                        "trace_sources": {
                            "type": "boolean",
                            "description": "Include sources when starting tracing.",
                        },
                    }
                )

        return {"type": "object", "properties": properties, "required": []}

    def _format_search_results(
        self,
        results: list[dict[str, str]],
        *,
        with_content: bool = False,
    ) -> str:
        if not results:
            return "No results found for the query."

        lines = [f"Found {len(results)} result(s):\n"]

        for i, result in enumerate(results, 1):
            title = result.get("title", "Untitled")
            url = result.get("url", "")
            snippet = result.get("snippet", "")
            content = result.get("content", "")

            lines.append(f"[{i}] {title}")
            if url:
                lines.append(f"    URL: {url}")

            if content and with_content:
                truncated = content[: self._max_content_length]
                if len(content) > self._max_content_length:
                    truncated += "\n    ... (content truncated)"
                lines.append(f"\n    --- Page Content ---\n    {truncated}\n")
            elif snippet:
                lines.append(f"    {snippet}")

            lines.append("")

        return "\n".join(lines).strip()

    @staticmethod
    def _split_content_sections(content: str) -> list[str]:
        return WebSearchPolicy.split_content_sections(content)

    def _extract_query_focused_excerpt(self, content: str, query_terms: list[str], max_chars: int) -> str:
        return self._policy.extract_query_focused_excerpt(content, query_terms, max_chars)

    def _prepare_results_for_output(
        self,
        results: list[dict[str, str]],
        *,
        query: str,
        with_content: bool,
    ) -> list[dict[str, str]]:
        return self._policy.prepare_results_for_output(
            results,
            query=query,
            with_content=with_content,
            max_content_length=self._max_content_length,
        )

    def _get_active_fetcher(self) -> URLFetcher | BrowserFetcher:
        if self._browser_fetcher is not None:
            return self._browser_fetcher
        return self._fetcher

    def _fetch_url(
        self,
        url: str,
        wait_for_selector: str | None = None,
        no_cache: bool = False,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("fetch", {"url": url, "wait_for_selector": wait_for_selector})

        try:
            cache = self._cache if not no_cache else None
            result: dict[str, Any] | None = None
            cache_hit = False

            fetcher = self._get_active_fetcher()
            is_browser = isinstance(fetcher, BrowserFetcher)

            if cache is not None:
                cached = cache.get_fetch(url, use_browser=is_browser, wait_for_selector=wait_for_selector)
                if cached is not None:
                    logger.debug("Cache hit for fetch {}", url)
                    result = cached
                    cache_hit = True

            if result is None:
                if isinstance(fetcher, BrowserFetcher) and wait_for_selector:
                    result = fetcher.fetch_with_wait(url, wait_for_selector=wait_for_selector)
                else:
                    result = fetcher.fetch(url)

            elapsed_ms = (_time.monotonic() - start) * 1000

            if not result["success"]:
                self._emit_tool_end(
                    "fetch", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Failed to fetch URL: {result['error']}")

            if cache is not None and not cache_hit:
                cache.put_fetch(url, result, use_browser=is_browser, wait_for_selector=wait_for_selector)

            output_lines = []
            if result["title"]:
                output_lines.append(f"Title: {result['title']}")
            output_lines.append(f"URL: {url}")
            if self._browser_fetcher is not None and not cache_hit:
                backend = self._deep_search or "browser"
                output_lines.append(f"(Rendered with {backend})")
            elif cache_hit:
                output_lines.append("(from cache)")
            output_lines.append("")
            output_lines.append("--- Page Content ---")
            output_lines.append(result["content"])
            output = "\n".join(output_lines)

            self._emit_tool_end(
                "fetch",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=(f"Fetched {url} ({len(result['content'])} chars)" + (" (cached)" if cache_hit else "")),
            )

            return ToolResult(tool_name=self.name, success=True, output=output)

        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as e:
            self._emit_tool_error("fetch", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _fetch_browser_content(
        self,
        fetcher: BrowserFetcher,
        page_url: str,
        wait_for_selector: str | None,
        cache: Any,
    ) -> dict[str, Any] | None:
        if cache is not None:
            cached = cache.get_fetch(page_url, use_browser=True, wait_for_selector=wait_for_selector)
            if cached is not None:
                logger.debug("Cache hit (browser) for fetch {}", page_url)
                return cached

        if wait_for_selector:
            fetched = fetcher.fetch_with_wait(page_url, wait_for_selector=wait_for_selector)
        else:
            fetched = fetcher.fetch(page_url, quick=True)

        if not fetched["success"]:
            return None
        if cache is not None:
            cache.put_fetch(page_url, fetched, use_browser=True, wait_for_selector=wait_for_selector)
        return fetched

    def _fetch_page_content(
        self,
        page_url: str,
        wait_for_selector: str | None = None,
        *,
        no_cache: bool = False,
    ) -> dict[str, Any] | None:
        fetcher = self._get_active_fetcher()
        cache = self._cache if not no_cache else None

        if isinstance(fetcher, BrowserFetcher):
            return self._fetch_browser_content(fetcher, page_url, wait_for_selector, cache)

        if cache is not None:
            cached = cache.get_fetch(page_url, use_browser=False, wait_for_selector=None)
            if cached is not None:
                logger.debug("Cache hit (HTTP) for fetch {}", page_url)
                return cached

        fetched = fetcher.fetch(page_url, timeout=self._policy.bulk_fetch_timeout)
        if fetched["success"]:
            if cache is not None:
                cache.put_fetch(page_url, fetched, use_browser=False, wait_for_selector=None)
            return fetched
        return None

    def _fetch_http_page_content(self, page_url: str, *, no_cache: bool) -> dict[str, Any] | None:
        cache = self._cache if not no_cache else None
        if cache is not None:
            cached = cache.get_fetch(page_url, use_browser=False, wait_for_selector=None)
            if cached is not None:
                logger.debug("Cache hit (HTTP) for fetch {}", page_url)
                return cached

        fetched = self._fetcher.fetch(page_url, timeout=self._policy.bulk_fetch_timeout)
        if fetched["success"]:
            if cache is not None:
                cache.put_fetch(page_url, fetched, use_browser=False, wait_for_selector=None)
            return fetched
        return None

    # ================================================================
    # Browser Actions
    # ================================================================

    @staticmethod
    def _supports_advanced_session(fetcher: BrowserFetcher | None) -> bool:
        if fetcher is None:
            return False
        method = getattr(fetcher, "supports_advanced_session", None)
        if not callable(method):
            return False
        try:
            return method() is True
        except (RuntimeError, AttributeError, TypeError, ValueError):
            return False

    def _require_browser(self, action: str) -> BrowserFetcher:
        if self._browser_fetcher is None:
            msg = (
                f"Action '{action}' requires a browser backend. "
                "Initialize WebSearchTool with deep_search='playwright' or "
                "deep_search='selenium', or provide a browser_fetcher."
            )
            raise RuntimeError(msg)
        return self._browser_fetcher

    def _require_advanced_browser(self, action: str) -> BrowserFetcher:
        fetcher = self._require_browser(action)
        if not self._supports_advanced_session(fetcher):
            msg = f"Action '{action}' currently requires the Playwright backend."
            raise RuntimeError(msg)
        return fetcher

    def _execute_click(self, selector: str, wait_timeout: int | None = None) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("click", {"selector": selector})
        try:
            fetcher = self._require_browser("click")
            result = fetcher.click_element(selector, wait_timeout=wait_timeout)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "click", success=False, duration_ms=elapsed_ms, result_summary=f"Click failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Click failed: {result['error']}")
            output_parts = [
                f"Clicked element: '{selector}'",
                f"Element text: {result['clicked_text'][:200]}" if result["clicked_text"] else "",
                f"Current URL: {result['url']}",
                f"Page title: {result['title']}",
            ]
            output = "\n".join(p for p in output_parts if p)
            self._emit_tool_end(
                "click",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Clicked '{selector}' -> {result['url']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("click", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_fill(
        self,
        selector: str,
        value: str,
        *,
        submit: bool = False,
        wait_timeout: int | None = None,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("fill", {"selector": selector, "value": value, "submit": submit})
        try:
            fetcher = self._require_browser("fill")
            result = fetcher.fill_input(selector, value, submit=submit, wait_timeout=wait_timeout)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "fill", success=False, duration_ms=elapsed_ms, result_summary=f"Fill failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Fill failed: {result['error']}")
            output_parts = [
                f"Filled '{selector}' with value: '{value}'",
                f"Submitted: {submit}",
                f"Current URL: {result['url']}",
                f"Page title: {result['title']}",
            ]
            output = "\n".join(output_parts)
            self._emit_tool_end(
                "fill",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Filled '{selector}' with '{value[:50]}'",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("fill", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_extract_links(
        self,
        url: str | None = None,
        *,
        selector: str = "a[href]",
        url_filter: str | None = None,
        max_links: int = 50,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("extract_links", {"url": url, "selector": selector})
        try:
            fetcher = self._require_browser("extract_links")
            if url:
                fetch_result = fetcher.fetch(url)
                if not fetch_result["success"]:
                    self._emit_tool_end("extract_links", success=False, result_summary=f"Failed to open {url}")
                    return ToolResult(
                        tool_name=self.name, success=False, error=f"Failed to open URL: {fetch_result['error']}"
                    )
            result = fetcher.extract_links(selector=selector, base_url_filter=url_filter, max_links=max_links)
            if not result["success"] and url:
                _time.sleep(0.5)
                result = fetcher.extract_links(selector=selector, base_url_filter=url_filter, max_links=max_links)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "extract_links",
                    success=False,
                    duration_ms=elapsed_ms,
                    result_summary=f"Extract failed: {result['error']}",
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Extract links failed: {result['error']}")
            lines = [f"Found {result['count']} link(s) on {result['url']}:\n"]
            for i, link in enumerate(result["links"], 1):
                text = link.get("text", "").strip() or "(no text)"
                lines.append(f"[{i}] {text}")
                lines.append(f"    URL: {link['url']}")
                if link.get("title"):
                    lines.append(f"    Title: {link['title']}")
                lines.append("")
            output = "\n".join(lines).strip()
            self._emit_tool_end(
                "extract_links",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Extracted {result['count']} links from {result['url']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("extract_links", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_js(self, js_code: str) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("execute_js", {"js_code": js_code[:200]})
        try:
            fetcher = self._require_browser("execute_js")
            result = fetcher.execute_js(js_code)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "execute_js", success=False, duration_ms=elapsed_ms, result_summary=f"JS error: {result['error']}"
                )
                return ToolResult(
                    tool_name=self.name, success=False, error=f"JavaScript execution failed: {result['error']}"
                )
            output_parts = [f"JavaScript executed on: {result['url']}"]
            if result["return_value"] is not None:
                output_parts.append(f"Return value: {result['return_value']}")
            else:
                output_parts.append("(no return value)")
            output = "\n".join(output_parts)
            self._emit_tool_end(
                "execute_js",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary="JS executed successfully",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("execute_js", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_crawl(
        self,
        start_url: str,
        *,
        max_pages: int = 10,
        max_depth: int = 2,
        url_filter: str | None = None,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start(
            "crawl",
            {
                "url": start_url,
                "max_pages": max_pages,
                "max_depth": max_depth,
                "url_filter": url_filter,
            },
        )
        try:
            fetcher = self._require_browser("crawl")
            result = fetcher.crawl(
                start_url, max_pages=max_pages, max_depth=max_depth, url_filter=url_filter, extract_content=True
            )
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"] and not result["pages"]:
                self._emit_tool_end(
                    "crawl", success=False, duration_ms=elapsed_ms, result_summary=f"Crawl failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Crawl failed: {result['error']}")
            lines = [f"Crawled {result['total_pages']} page(s) starting from {start_url}:\n"]
            for i, page in enumerate(result["pages"], 1):
                lines.append(f"[{i}] {page.get('title', 'Untitled')}")
                lines.append(f"    URL: {page['url']}")
                lines.append(f"    Depth: {page['depth']}, Links found: {page.get('links_found', 0)}")
                content = page.get("content", "")
                if content:
                    truncated = content[: self._max_content_length]
                    if len(content) > self._max_content_length:
                        truncated += "\n    ... (content truncated)"
                    lines.append(f"\n    --- Page Content ---\n    {truncated}\n")
                lines.append("")
            if result.get("error"):
                lines.append(f"Note: Crawl completed with warning: {result['error']}")
            output = "\n".join(lines).strip()
            self._emit_tool_end(
                "crawl",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Crawled {result['total_pages']} pages from {start_url}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("crawl", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_get_content(self) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("get_content", {})
        try:
            fetcher = self._require_browser("get_content")
            result = fetcher.get_page_content()
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "get_content", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Get content failed: {result['error']}")
            output_lines = []
            if result["title"]:
                output_lines.append(f"Title: {result['title']}")
            output_lines.append(f"URL: {result['url']}")
            output_lines.append("")
            output_lines.append("--- Page Content ---")
            output_lines.append(result["content"])
            output = "\n".join(output_lines)
            self._emit_tool_end(
                "get_content",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Got content from {result['url']} ({len(result['content'])} chars)",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("get_content", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_list_tabs(self) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("list_tabs", {})
        try:
            fetcher = self._require_advanced_browser("list_tabs")
            result = fetcher.list_tabs()
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "list_tabs", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"List tabs failed: {result['error']}")

            lines = [f"Open tabs: {result['count']}\n"]
            for tab in result["tabs"]:
                marker = " (active)" if tab.get("active") else ""
                lines.append(f"[{tab['index']}] {tab.get('title') or '(untitled)'}{marker}")
                lines.append(f"    URL: {tab.get('url', '')}")
            output = "\n".join(lines).strip()
            self._emit_tool_end(
                "list_tabs",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Listed {result['count']} tab(s)",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("list_tabs", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_open_tab(
        self,
        url: str = "",
        *,
        wait_for_selector: str | None = None,
        background: bool = False,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("open_tab", {"url": url, "background": background})
        try:
            fetcher = self._require_advanced_browser("open_tab")
            result = fetcher.open_tab(url, wait_for_selector=wait_for_selector, background=background)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "open_tab", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Open tab failed: {result['error']}")

            output_lines = [
                f"Opened tab [{result['index']}]",
                f"Active: {result.get('active', False)}",
            ]
            if result.get("title"):
                output_lines.append(f"Title: {result['title']}")
            if result.get("url"):
                output_lines.append(f"URL: {result['url']}")
            output = "\n".join(output_lines)
            self._emit_tool_end(
                "open_tab",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Opened tab {result['index']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("open_tab", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_switch_tab(self, tab_index: int) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("switch_tab", {"tab_index": tab_index})
        try:
            fetcher = self._require_advanced_browser("switch_tab")
            result = fetcher.switch_tab(tab_index)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "switch_tab", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Switch tab failed: {result['error']}")

            output = "\n".join(
                [
                    f"Switched to tab [{result['index']}]",
                    f"Title: {result.get('title', '')}",
                    f"URL: {result.get('url', '')}",
                ]
            ).strip()
            self._emit_tool_end(
                "switch_tab",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Switched to tab {result['index']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("switch_tab", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_close_tab(self, tab_index: int | None = None) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("close_tab", {"tab_index": tab_index})
        try:
            fetcher = self._require_advanced_browser("close_tab")
            result = fetcher.close_tab(tab_index)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "close_tab", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Close tab failed: {result['error']}")

            output = "\n".join(
                [
                    f"Closed tab: {result.get('closed_index')}",
                    f"Active tab: [{result['index']}]",
                    f"Title: {result.get('title', '')}",
                    f"URL: {result.get('url', '')}",
                ]
            ).strip()
            self._emit_tool_end(
                "close_tab",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Closed tab {result.get('closed_index')}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("close_tab", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_screenshot(
        self,
        *,
        path: str = "",
        selector: str = "",
        full_page: bool = False,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("screenshot", {"path": path, "selector": selector, "full_page": full_page})
        try:
            fetcher = self._require_advanced_browser("screenshot")
            result = fetcher.screenshot(path=path, selector=selector or None, full_page=full_page)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "screenshot", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Screenshot failed: {result['error']}")

            output_lines = [f"Saved screenshot: {result['path']}"]
            if result.get("title"):
                output_lines.append(f"Title: {result['title']}")
            if result.get("url"):
                output_lines.append(f"URL: {result['url']}")
            output = "\n".join(output_lines)
            self._emit_tool_end(
                "screenshot",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Saved screenshot to {result['path']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("screenshot", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_download(self, selector: str, *, path: str = "", wait_timeout: int | None = None) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("download", {"selector": selector, "path": path})
        try:
            fetcher = self._require_advanced_browser("download")
            result = fetcher.download(selector, path=path, wait_timeout=wait_timeout)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "download", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Download failed: {result['error']}")

            output = "\n".join(
                [
                    f"Downloaded file: {result['path']}",
                    f"Suggested filename: {result.get('suggested_filename', '')}",
                    f"Page URL: {result.get('url', '')}",
                ]
            ).strip()
            self._emit_tool_end(
                "download",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Downloaded file to {result['path']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("download", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_list_frames(self) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("list_frames", {})
        try:
            fetcher = self._require_advanced_browser("list_frames")
            result = fetcher.list_frames()
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "list_frames", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"List frames failed: {result['error']}")

            lines = [f"Frames: {result['count']}\n"]
            for frame in result["frames"]:
                label = frame.get("name") or "(unnamed)"
                lines.append(f"[{frame['index']}] {label}")
                lines.append(f"    URL: {frame.get('url', '')}")
            output = "\n".join(lines).strip()
            self._emit_tool_end(
                "list_frames",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Listed {result['count']} frame(s)",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("list_frames", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_get_cookies(self, urls: list[str] | None = None) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("get_cookies", {"urls": urls or []})
        try:
            fetcher = self._require_advanced_browser("get_cookies")
            result = fetcher.get_cookies(urls=urls)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "get_cookies", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Get cookies failed: {result['error']}")

            payload = json.dumps(result["cookies"], ensure_ascii=True, indent=2)
            if len(payload) > self._max_content_length:
                payload = payload[: self._max_content_length] + "\n... (cookies truncated)"
            output = f"Cookies: {result['count']}\n{payload}"
            self._emit_tool_end(
                "get_cookies",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Returned {result['count']} cookie(s)",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as e:
            self._emit_tool_error("get_cookies", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_add_cookies(self, cookies: list[dict[str, Any]] | None) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("add_cookies", {"count": len(cookies or [])})
        try:
            fetcher = self._require_advanced_browser("add_cookies")
            if not cookies:
                return ToolResult(tool_name=self.name, success=False, error="Action 'add_cookies' requires cookies.")
            result = fetcher.add_cookies(cookies)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "add_cookies", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Add cookies failed: {result['error']}")

            output = f"Added cookies: {result['count']}"
            self._emit_tool_end(
                "add_cookies",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Added {result['count']} cookie(s)",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("add_cookies", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_storage_state(self, path: str = "") -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("storage_state", {"path": path})
        try:
            fetcher = self._require_advanced_browser("storage_state")
            result = fetcher.storage_state(path=path)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "storage_state", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Storage state failed: {result['error']}")

            payload = json.dumps(result["state"], ensure_ascii=True, indent=2)
            if len(payload) > self._max_content_length:
                payload = payload[: self._max_content_length] + "\n... (state truncated)"
            lines = []
            if result.get("path"):
                lines.append(f"Saved storage state: {result['path']}")
            lines.append(payload)
            output = "\n".join(lines)
            self._emit_tool_end(
                "storage_state",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary="Captured storage state",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as e:
            self._emit_tool_error("storage_state", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_start_tracing(
        self,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = True,
    ) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start(
            "start_tracing",
            {"screenshots": screenshots, "snapshots": snapshots, "sources": sources},
        )
        try:
            fetcher = self._require_advanced_browser("start_tracing")
            result = fetcher.start_tracing(screenshots=screenshots, snapshots=snapshots, sources=sources)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "start_tracing", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Start tracing failed: {result['error']}")

            output = "Tracing started."
            self._emit_tool_end(
                "start_tracing",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary="Tracing started",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("start_tracing", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_stop_tracing(self, path: str = "") -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("stop_tracing", {"path": path})
        try:
            fetcher = self._require_advanced_browser("stop_tracing")
            result = fetcher.stop_tracing(path=path)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "stop_tracing", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Stop tracing failed: {result['error']}")

            output = f"Saved trace: {result['path']}"
            self._emit_tool_end(
                "stop_tracing",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Saved trace to {result['path']}",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError) as e:
            self._emit_tool_error("stop_tracing", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    def _execute_network_events(self, *, limit: int = 100, clear: bool = False) -> ToolResult:
        start = _time.monotonic()
        self._emit_tool_start("network_events", {"limit": limit, "clear": clear})
        try:
            fetcher = self._require_advanced_browser("network_events")
            result = fetcher.get_network_events(limit=limit, clear=clear)
            elapsed_ms = (_time.monotonic() - start) * 1000
            if not result["success"]:
                self._emit_tool_end(
                    "network_events", success=False, duration_ms=elapsed_ms, result_summary=f"Failed: {result['error']}"
                )
                return ToolResult(tool_name=self.name, success=False, error=f"Network events failed: {result['error']}")

            payload = json.dumps(result["events"], ensure_ascii=True, indent=2)
            if len(payload) > self._max_content_length:
                payload = payload[: self._max_content_length] + "\n... (events truncated)"
            output = f"Network events: {result['count']}\n{payload}"
            self._emit_tool_end(
                "network_events",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Returned {result['count']} network event(s)",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as e:
            self._emit_tool_error("network_events", e)
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    # ================================================================
    # Main execute
    # ================================================================

    @staticmethod
    def _infer_action(query: str, url: str, selector: str, js_code: str) -> str:
        if query:
            return "search"
        if url:
            return "fetch"
        if selector:
            return "click"
        if js_code:
            return "execute_js"
        return ""

    def _require_param(self, param_value: Any, action: str, param_name: str) -> ToolResult | None:
        missing = param_value is None or (isinstance(param_value, str) and not param_value)
        if missing:
            return ToolResult(
                tool_name=self.name, success=False, error=f"Action '{action}' requires '{param_name}' parameter."
            )
        return None

    def execute(  # noqa: PLR0913
        self,
        query: str = "",
        url: str = "",
        *,
        action: str = "",
        fetch_content: bool | None = None,
        max_results: int | None = None,
        wait_for_selector: str | None = None,
        selector: str = "",
        value: str = "",
        submit: bool = False,
        js_code: str = "",
        max_depth: int = 2,
        max_pages: int = 10,
        max_links: int = 50,
        url_filter: str | None = None,
        tab_index: int | None = None,
        background: bool = False,
        path: str = "",
        full_page: bool = False,
        urls: list[str] | None = None,
        cookies: list[dict[str, Any]] | None = None,
        limit: int = 100,
        clear: bool = False,
        trace_screenshots: bool = True,
        trace_snapshots: bool = True,
        trace_sources: bool = True,
        provider: str | None = None,
        intent: str | None = None,
        wait_timeout: int | None = None,
        no_cache: bool = False,
        deduplicate: bool | None = None,
        **_kwargs: Any,
    ) -> ToolResult:
        if not action:
            action = self._infer_action(query, url, selector, js_code)
            if not action:
                return ToolResult(
                    tool_name=self.name, success=False, error="No action, query, url, selector, or js_code provided."
                )

        def switch_tab_handler() -> ToolResult:
            if tab_index is None:
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="Action 'switch_tab' requires 'tab_index' parameter.",
                )
            return self._execute_switch_tab(tab_index)

        dispatch: dict[str, tuple[object, str, Callable[[], ToolResult]]] = {
            "click": (selector, "selector", lambda: self._execute_click(selector, wait_timeout=wait_timeout)),
            "fill": (
                selector,
                "selector",
                lambda: self._execute_fill(selector, value, submit=submit, wait_timeout=wait_timeout),
            ),
            "extract_links": (
                "_skip_",
                "",
                lambda: self._execute_extract_links(
                    url or None, selector=selector or "a[href]", url_filter=url_filter, max_links=max_links
                ),
            ),
            "execute_js": (js_code, "js_code", lambda: self._execute_js(js_code)),
            "crawl": (
                url,
                "url",
                lambda: self._execute_crawl(url, max_pages=max_pages, max_depth=max_depth, url_filter=url_filter),
            ),
            "get_content": ("_skip_", "", self._execute_get_content),
            "open_tab": (
                "_skip_",
                "",
                lambda: self._execute_open_tab(url, wait_for_selector=wait_for_selector, background=background),
            ),
            "list_tabs": ("_skip_", "", self._execute_list_tabs),
            "switch_tab": (tab_index, "tab_index", switch_tab_handler),
            "close_tab": ("_skip_", "", lambda: self._execute_close_tab(tab_index)),
            "screenshot": (
                "_skip_",
                "",
                lambda: self._execute_screenshot(path=path, selector=selector, full_page=full_page),
            ),
            "download": (
                selector,
                "selector",
                lambda: self._execute_download(selector, path=path, wait_timeout=wait_timeout),
            ),
            "list_frames": ("_skip_", "", self._execute_list_frames),
            "get_cookies": ("_skip_", "", lambda: self._execute_get_cookies(urls)),
            "add_cookies": ("_skip_", "", lambda: self._execute_add_cookies(cookies)),
            "storage_state": ("_skip_", "", lambda: self._execute_storage_state(path)),
            "start_tracing": (
                "_skip_",
                "",
                lambda: self._execute_start_tracing(
                    screenshots=trace_screenshots,
                    snapshots=trace_snapshots,
                    sources=trace_sources,
                ),
            ),
            "stop_tracing": ("_skip_", "", lambda: self._execute_stop_tracing(path)),
            "network_events": (
                "_skip_",
                "",
                lambda: self._execute_network_events(limit=limit, clear=clear),
            ),
            "fetch": (url, "url", lambda: self._fetch_url(url, wait_for_selector=wait_for_selector, no_cache=no_cache)),
            "search": (
                query,
                "query",
                lambda: self._execute_search(
                    query,
                    fetch_content=fetch_content,
                    max_results=max_results,
                    wait_for_selector=wait_for_selector,
                    provider=provider,
                    intent=intent,
                    no_cache=no_cache,
                    deduplicate=deduplicate,
                ),
            ),
            "search_images": (
                query,
                "query",
                lambda: self._execute_search_images(
                    query,
                    max_results=max_results,
                    provider=provider,
                    no_cache=no_cache,
                ),
            ),
        }

        entry = dispatch.get(action)
        if entry is None:
            return ToolResult(tool_name=self.name, success=False, error=f"Unknown action: '{action}'.")

        required_value, param_name, handler = entry
        if required_value != "_skip_":
            err = self._require_param(required_value, action, param_name)
            if err is not None:
                return err

        try:
            return handler()
        except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as exc:
            self._emit_tool_error(action, exc)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Unexpected error in '{action}' ({type(exc).__name__}): {exc}",
            )

    # ------------------------------------------------------------------
    # Provider selection helpers
    # ------------------------------------------------------------------

    def _select_providers(
        self,
        query: str,
        *,
        provider: str | None = None,
        intent: str | None = None,
    ) -> list[SearchProvider]:
        if provider is not None and self._available_providers:
            override_prov = self._available_providers.get(provider)
            if override_prov is not None:
                chain: list[SearchProvider] = [override_prov]
                if self._router is not None:
                    routed = self._router.route_providers(query)
                    chain.extend(p for p in routed if p is not override_prov)
                elif self._provider is not override_prov:
                    chain.append(self._provider)
                return chain
            logger.warning(
                "Provider override {!r} not found in available providers — falling back to normal routing",
                provider,
            )

        if self._router is not None and self._available_providers:
            providers = self._router.route_providers(query, intent=intent)
            if providers:
                return providers
            logger.debug("Router returned empty list for {!r} — using default provider", query)
        return [self._provider]

    @dataclasses.dataclass
    class _SearchAttempt:
        provider: str
        status: str  # "success", "no_results", "error"
        error: SearchError | None = None
        result_count: int = 0

    def _search_with_fallback(
        self,
        query: str,
        num_results: int,
        *,
        provider: str | None = None,
        intent: str | None = None,
        no_cache: bool = False,
        deduplicate: bool | None = None,
    ) -> tuple[list[dict[str, str]], list["WebSearchTool._SearchAttempt"]]:
        should_dedup = deduplicate if deduplicate is not None else self._deduplicate

        cache = self._cache if not no_cache else None
        ns = self._get_cache_ns()

        providers = self._select_providers(query, provider=provider, intent=intent)
        chain_tag = ",".join(type(p).__name__ for p in providers)

        if cache is not None:
            cached = cache.get_search(
                query,
                num_results,
                provider=provider,
                intent=intent,
                deduplicate=should_dedup,
                namespace=ns,
                source_provider=chain_tag,
            )
            if cached is not None:
                logger.debug("Cache hit for search {!r} (max_results={})", query, num_results)
                attempts = [
                    self._SearchAttempt(
                        provider="cache",
                        status="success",
                        result_count=len(cached),
                    )
                ]
                return cached, attempts

        attempts: list[WebSearchTool._SearchAttempt] = []

        for prov in providers:
            pname = type(prov).__name__
            try:
                results = prov.search(query, num_results)
                if results:
                    if should_dedup:
                        before = len(results)
                        results = deduplicate_results(results)
                        if len(results) < before:
                            logger.debug("Dedup removed {} duplicate(s) from {} results", before - len(results), pname)
                    logger.debug("Provider {} returned {} results for {!r}", pname, len(results), query)
                    attempts.append(
                        self._SearchAttempt(
                            provider=pname,
                            status="success",
                            result_count=len(results),
                        )
                    )
                    if cache is not None:
                        cache.put_search(
                            query,
                            num_results,
                            results,
                            provider=provider,
                            intent=intent,
                            deduplicate=should_dedup,
                            namespace=ns,
                            source_provider=chain_tag,
                        )
                    return results, attempts
                logger.debug("Provider {} returned 0 results for {!r} — trying next", pname, query)
                attempts.append(self._SearchAttempt(provider=pname, status="no_results"))
            except SearchError as exc:
                logger.warning(
                    "Provider {} failed for {!r} ({}, status={}) — trying next",
                    pname,
                    query,
                    exc.reason,
                    exc.status_code,
                )
                attempts.append(self._SearchAttempt(provider=pname, status="error", error=exc))
            except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as exc:
                wrapped = SearchError(str(exc), reason="unexpected", provider=pname)
                logger.warning(
                    "Provider {} raised unexpected {} for {!r}: {} — trying next",
                    pname,
                    type(exc).__name__,
                    query,
                    exc,
                )
                attempts.append(self._SearchAttempt(provider=pname, status="error", error=wrapped))

        if any(a.status == "error" for a in attempts):
            logger.warning(
                "All providers exhausted for {!r}; {} failed, {} returned no results",
                query,
                sum(1 for a in attempts if a.status == "error"),
                sum(1 for a in attempts if a.status == "no_results"),
            )
        return [], attempts

    @staticmethod
    def _format_attempts_debug(attempts: list["WebSearchTool._SearchAttempt"]) -> str:
        parts: list[str] = []
        for a in attempts:
            if a.status == "success":
                parts.append(f"{a.provider}: ok ({a.result_count} results)")
            elif a.status == "no_results":
                parts.append(f"{a.provider}: no_results")
            elif a.error is not None:
                detail = a.error.reason
                if a.error.status_code is not None:
                    detail += f" (HTTP {a.error.status_code})"
                err_msg = str(a.error)
                if err_msg:
                    detail += f": {err_msg}"
                parts.append(f"{a.provider}: {detail}")
            else:
                parts.append(f"{a.provider}: {a.status}")
        return " → ".join(parts)

    def _build_fetch_candidates(self, results: list[dict[str, Any]]) -> list[tuple[int, str]]:
        candidates: list[tuple[int, str]] = []
        for idx, result in enumerate(results):
            if len(candidates) >= self._max_fetch_pages + 2:
                break
            page_url = result.get("url", "")
            if page_url:
                candidates.append((idx, page_url))
        return candidates

    def _has_sufficient_content(self, fetched: dict[str, Any] | None) -> bool:
        return bool(
            fetched and fetched.get("success") and len(fetched.get("content", "")) >= self._policy.min_fallback_content
        )

    def _extract_query_terms(self, query: str) -> list[str]:
        return self._policy.extract_query_terms(query)

    def _content_quality_score(
        self,
        query_terms: list[str],
        result: dict[str, Any],
        fetched: dict[str, Any] | None,
    ) -> float:
        return self._policy.content_quality_score(query_terms, result, fetched)

    def _snippet_quality_score(self, query_terms: list[str], result: dict[str, Any]) -> float:
        return self._policy.snippet_quality_score(query_terms, result)

    def _results_need_content_fetch(self, query: str, results: list[dict[str, str]]) -> bool:
        return self._policy.results_need_content_fetch(query, results)

    def _should_browser_enrich_candidate(
        self,
        query_terms: list[str],
        idx: int,
        result: dict[str, Any],
        fetched: dict[str, Any] | None,
    ) -> bool:
        if not self._has_sufficient_content(fetched):
            return True
        return self._policy.should_browser_enrich_candidate(query_terms, idx, result, fetched)

    def _fetch_http_candidates_parallel(
        self,
        candidates: list[tuple[int, str]],
        *,
        no_cache: bool,
    ) -> dict[int, dict[str, Any] | None]:
        if not candidates:
            return {}

        fetch_map: dict[int, dict[str, Any] | None] = {}
        with ThreadPoolExecutor(max_workers=min(len(candidates), self._policy.http_enrich_concurrency)) as pool:
            futures = {
                pool.submit(self._fetch_http_page_content, url, no_cache=no_cache): idx for idx, url in candidates
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    fetch_map[idx] = future.result()
                except (RuntimeError, OSError, TimeoutError, ValueError, AttributeError):
                    fetch_map[idx] = None
        return fetch_map

    def _fetch_browser_candidates(
        self,
        fetcher: BrowserFetcher,
        candidates: list[tuple[int, str]],
        wait_for_selector: str | None,
        *,
        no_cache: bool,
        quick: bool = True,
    ) -> dict[int, dict[str, Any] | None]:
        fetch_map: dict[int, dict[str, Any] | None] = {}
        cache = self._cache if not no_cache else None
        for idx, page_url in candidates:
            if cache is not None and wait_for_selector is None:
                cached = cache.get_fetch(page_url, use_browser=True, wait_for_selector=None)
                if cached is not None:
                    fetch_map[idx] = cached
                    continue

            if wait_for_selector:
                fetched = fetcher.fetch_with_wait(page_url, wait_for_selector=wait_for_selector)
            else:
                fetched = fetcher.fetch(page_url, quick=quick)

            if fetched["success"]:
                if cache is not None:
                    cache.put_fetch(page_url, fetched, use_browser=True, wait_for_selector=wait_for_selector)
                fetch_map[idx] = fetched
            else:
                fetch_map[idx] = None
        return fetch_map

    def _apply_fetched_content(
        self,
        results: list[dict[str, Any]],
        candidates: list[tuple[int, str]],
        fetch_map: dict[int, dict[str, Any] | None],
    ) -> None:
        fetched_count = 0
        for idx, _url in candidates:
            if fetched_count >= self._max_fetch_pages:
                break
            fetched = fetch_map.get(idx)
            if fetched is not None:
                results[idx]["content"] = fetched["content"]
                if fetched["title"] and not results[idx].get("title"):
                    results[idx]["title"] = fetched["title"]
                fetched_count += 1

    def _fetch_content_for_results(
        self,
        results: list[dict[str, Any]],
        wait_for_selector: str | None,
        *,
        query: str = "",
        no_cache: bool,
    ) -> None:
        candidates = self._build_fetch_candidates(results)
        fetcher = self._get_active_fetcher()
        if not isinstance(fetcher, BrowserFetcher):
            fetch_map = self._fetch_http_candidates_parallel(candidates, no_cache=no_cache)
            self._apply_fetched_content(results, candidates, fetch_map)
            return

        if wait_for_selector:
            browser_fetch_map = self._fetch_browser_candidates(
                fetcher,
                candidates,
                wait_for_selector,
                no_cache=no_cache,
            )
            self._apply_fetched_content(results, candidates, browser_fetch_map)
            return

        query_terms = self._extract_query_terms(query)
        http_fetch_map = self._fetch_http_candidates_parallel(candidates, no_cache=no_cache)
        browser_candidates = [
            candidate
            for candidate in candidates
            if self._should_browser_enrich_candidate(
                query_terms,
                candidate[0],
                results[candidate[0]],
                http_fetch_map.get(candidate[0]),
            )
        ][: self._max_fetch_pages]

        browser_fetch_map = self._fetch_browser_candidates(
            fetcher,
            browser_candidates,
            None,
            no_cache=no_cache,
            quick=True,
        )

        combined_fetch_map = dict(http_fetch_map)
        for idx, browser_fetched in browser_fetch_map.items():
            current = combined_fetch_map.get(idx)
            if browser_fetched is None:
                continue
            if current is None or len(browser_fetched.get("content", "")) > len(current.get("content", "")):
                combined_fetch_map[idx] = browser_fetched

        rescue_candidates = [
            candidate
            for candidate in browser_candidates
            if candidate[0] == 0
            and self._content_quality_score(query_terms, results[candidate[0]], combined_fetch_map.get(candidate[0]))
            < self._policy.full_browser_rescue_threshold
        ][: self._policy.full_browser_rescue_pages]
        if rescue_candidates:
            rescue_fetch_map = self._fetch_browser_candidates(
                fetcher,
                rescue_candidates,
                None,
                no_cache=no_cache,
                quick=False,
            )
            combined_fetch_map.update(
                {
                    idx: browser_fetched
                    for idx, browser_fetched in rescue_fetch_map.items()
                    if browser_fetched is not None
                }
            )

        self._apply_fetched_content(results, candidates, combined_fetch_map)

    def _format_image_results(self, results: list[dict[str, str]]) -> str:
        if not results:
            return "No image results found for the query."

        lines = [f"Found {len(results)} image result(s):\n"]
        for i, result in enumerate(results, 1):
            title = result.get("title", "Untitled")
            url = result.get("url", "")
            image_url = result.get("image_url", "")
            snippet = result.get("snippet", "")

            lines.append(f"[{i}] {title}")
            if image_url:
                lines.append(f"    Image: {image_url}")
            if url:
                lines.append(f"    Source: {url}")
            if snippet:
                lines.append(f"    {snippet}")
            lines.append("")

        return "\n".join(lines).strip()

    def _execute_search_images(
        self,
        query: str,
        *,
        max_results: int | None = None,
        provider: str | None = None,
        no_cache: bool = False,
    ) -> ToolResult:
        start = _time.monotonic()
        num_results = max(1, min(max_results or self._max_results, 10))
        self._emit_tool_start("search_images", {"query": query, "max_results": num_results})

        cache = self._cache if not no_cache else None

        if cache is not None:
            cached = cache.get_image_search(query, num_results, provider=provider)
            if cached is not None:
                logger.debug("Cache hit for image search {!r} (max_results={})", query, num_results)
                output = self._format_image_results(cached)
                elapsed_ms = (_time.monotonic() - start) * 1000
                self._emit_tool_end(
                    "search_images",
                    success=True,
                    output_size=len(output),
                    duration_ms=elapsed_ms,
                    result_summary=f"Found {len(cached)} images for '{query}' (cached)",
                )
                return ToolResult(tool_name=self.name, success=True, output=output)

        try:
            active_provider = self._provider
            if provider and self._available_providers:
                active_provider = self._available_providers.get(provider, self._provider)

            results = active_provider.search_images(query, num_results)

            if cache is not None:
                cache.put_image_search(query, num_results, results, provider=provider)

            output = self._format_image_results(results)
            elapsed_ms = (_time.monotonic() - start) * 1000
            self._emit_tool_end(
                "search_images",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=f"Found {len(results)} images for '{query}'",
            )
            return ToolResult(tool_name=self.name, success=True, output=output)

        except SearchError as exc:
            self._emit_tool_error("search_images", exc)
            return ToolResult(tool_name=self.name, success=False, error=str(exc))
        except (TimeoutError, urllib.error.URLError, ValueError, KeyError, OSError, RuntimeError) as exc:
            self._emit_tool_error("search_images", exc)
            return ToolResult(tool_name=self.name, success=False, error=f"Image search error: {exc}")

    def _build_empty_search_result(
        self,
        query: str,
        attempts: list[Any],
        attempts_debug: str,
        start: float,
    ) -> ToolResult:
        failed = [a for a in attempts if a.status == "error"]
        elapsed_ms = (_time.monotonic() - start) * 1000
        if failed:
            last_err = failed[-1].error
            error_msg = f"All search providers failed for '{query}'. Providers tried: {attempts_debug}"
            if last_err is not None:
                self._emit_tool_error("search", last_err)
            self._emit_tool_end(
                "search",
                success=False,
                output_size=0,
                duration_ms=elapsed_ms,
                result_summary=(
                    f"provider_failed | reason={last_err.reason if last_err else 'unknown'} | attempts={attempts_debug}"
                ),
            )
            return ToolResult(tool_name=self.name, success=False, error=error_msg)

        output = f"No results found for query: '{query}'. Try rephrasing your search or using different keywords."
        self._emit_tool_end(
            "search",
            success=True,
            output_size=len(output),
            duration_ms=elapsed_ms,
            result_summary=f"no_results | attempts={attempts_debug}",
        )
        return ToolResult(tool_name=self.name, success=True, output=output)

    def _execute_search(
        self,
        query: str,
        *,
        fetch_content: bool | None = None,
        max_results: int | None = None,
        wait_for_selector: str | None = None,
        provider: str | None = None,
        intent: str | None = None,
        no_cache: bool = False,
        deduplicate: bool | None = None,
    ) -> ToolResult:
        start = _time.monotonic()
        num_results = max_results if max_results is not None else self._max_results
        num_results = max(1, min(num_results, 10))
        should_fetch = fetch_content if fetch_content is not None else self._fetch_content

        self._emit_tool_start("search", {"query": query, "max_results": num_results, "fetch_content": should_fetch})

        try:
            results, attempts = self._search_with_fallback(
                query,
                num_results,
                provider=provider,
                intent=intent,
                no_cache=no_cache,
                deduplicate=deduplicate,
            )
            attempts_debug = self._format_attempts_debug(attempts)

            if not results:
                return self._build_empty_search_result(query, attempts, attempts_debug, start)

            if should_fetch:
                if self._results_need_content_fetch(query, results):
                    self._fetch_content_for_results(results, wait_for_selector, query=query, no_cache=no_cache)
                else:
                    should_fetch = False

            output_results = self._prepare_results_for_output(results, query=query, with_content=should_fetch)
            output = self._format_search_results(output_results, with_content=should_fetch)
            elapsed_ms = (_time.monotonic() - start) * 1000

            self._emit_tool_end(
                "search",
                success=True,
                output_size=len(output),
                duration_ms=elapsed_ms,
                result_summary=(f"Found {len(results)} results for '{query}' | attempts={attempts_debug}"),
            )

            return ToolResult(tool_name=self.name, success=True, output=output)

        except TimeoutError as e:
            self._emit_tool_error("search", e)
            error_msg = f"Search timed out after {self._timeout} seconds"
        except urllib.error.URLError as e:
            self._emit_tool_error("search", e)
            error_msg = f"Network error: {e.reason}"
        except (ValueError, KeyError, OSError) as e:
            self._emit_tool_error("search", e)
            error_msg = f"Search error: {e}"
        except (RuntimeError, AttributeError, TypeError, ImportError) as e:
            self._emit_tool_error("search", e)
            error_msg = f"Unexpected search error ({type(e).__name__}): {e}"

        return ToolResult(tool_name=self.name, success=False, error=error_msg)

    def close(self) -> None:
        closed_ids: set[int] = set()

        def _close_once(obj: Any) -> None:
            oid = id(obj)
            if oid in closed_ids:
                return
            closed_ids.add(oid)
            if hasattr(obj, "close"):
                with contextlib.suppress(Exception):
                    obj.close()

        fetcher = getattr(self, "_browser_fetcher", None)
        if fetcher is not None:
            _close_once(fetcher)
        if hasattr(self, "_fetcher"):
            _close_once(self._fetcher)
        if hasattr(self, "_provider"):
            _close_once(self._provider)
        for prov in (getattr(self, "_available_providers", None) or {}).values():
            _close_once(prov)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
