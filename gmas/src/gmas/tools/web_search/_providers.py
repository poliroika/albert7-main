"""Search providers and provider registry."""

import contextlib
import functools
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from gmas.config.logging import logger

_HTTP_RATE_LIMIT = 429

_urlopen = urllib.request.urlopen
_Request = urllib.request.Request


# ============================================================
# Base & exception
# ============================================================


class SearchProvider(ABC):
    """Abstract base class for search providers."""

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """
        Perform a search and return results.

        Returns:
            List of dicts with keys ``title``, ``url``, ``snippet``.

        Raises:
            SearchError: On provider failure (network, auth, rate-limit …).

        """
        ...

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """
        Search for images and return results.

        Returns:
            List of dicts with keys ``title``, ``url``, ``image_url``, ``snippet``.

        Raises:
            SearchError: If the provider does not support image search or fails.

        """
        msg = f"{type(self).__name__} does not support image search (query={query!r}, max_results={max_results})"
        raise SearchError(
            msg,
            reason="not_supported",
            provider=type(self).__name__,
        )


class ApiKeySearchProvider(SearchProvider, ABC):
    """Base class for search providers that authenticate via an API key."""

    def __init__(self, api_key: str, **_kwargs: Any) -> None:
        self._api_key = api_key


class SearchError(Exception):
    """
    Structured error raised by search providers.

    Carries machine-readable metadata so the framework can distinguish
    "no results found" from "provider failed".
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "api_error",
        status_code: int | None = None,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.provider = provider

    def __repr__(self) -> str:
        parts = [f"reason={self.reason!r}"]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.provider:
            parts.append(f"provider={self.provider!r}")
        return f"SearchError({', '.join(parts)}: {self})"


def _http_status_reason(status: int) -> str:
    """Map HTTP status code to a reason string."""
    if status == _HTTP_RATE_LIMIT:
        return "rate_limit"
    if status in (401, 403):
        return "auth_error"
    if status in (502, 503, 504):
        return "unavailable"
    return "http_error"


def _classify_urllib_error(
    exc: Exception,
    *,
    provider: str,
) -> SearchError:
    """Convert a stdlib/httpx exception into a :class:`SearchError`."""
    status: int | None = None
    reason: str = "api_error"

    try:
        import httpx
    except ImportError:
        pass
    else:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            reason = _http_status_reason(status)
            return SearchError(str(exc), reason=reason, status_code=status, provider=provider)
        if isinstance(exc, httpx.TimeoutException):
            return SearchError(str(exc), reason="timeout", provider=provider)
        if isinstance(exc, (httpx.ConnectError, httpx.TooManyRedirects, httpx.HTTPError)):
            return SearchError(str(exc), reason="network_error", provider=provider)

    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        reason = _http_status_reason(status)
    elif isinstance(exc, urllib.error.URLError):
        reason = "network_error"
    elif isinstance(exc, TimeoutError):
        reason = "timeout"
    elif isinstance(exc, (json.JSONDecodeError, KeyError, ValueError)):
        reason = "parse_error"
    elif isinstance(exc, OSError):
        reason = "network_error"

    return SearchError(
        str(exc),
        reason=reason,
        status_code=status,
        provider=provider,
    )


# ============================================================
# Concrete providers
# ============================================================


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo search — no API key required."""

    _DEFAULT_BACKEND_ORDER: ClassVar[tuple[str, ...]] = ("ddgs", "httpx_html", "urllib_html")
    _STAGE_ALIASES: ClassVar[dict[str, str]] = {
        "ddgs_html": "ddgs",
        "ddgs_lite": "ddgs",
    }
    _SUPPORTED_BACKENDS: ClassVar[frozenset[str]] = frozenset({"ddgs", "httpx_html", "urllib_html"})

    _HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://html.duckduckgo.com/",
    }

    def __init__(
        self,
        timeout: int = 12,
        *,
        trust_env: bool = False,
        backend_order: tuple[str, ...] | list[str] | None = None,
        max_backend_attempts: int | None = None,
        ddgs_backend: str | None = None,
    ):
        self._timeout = timeout
        self._trust_env = trust_env
        self._search_url = "https://html.duckduckgo.com/html/"
        self._httpx_client: Any = None
        self._ddgs_backend = self._resolve_ddgs_backend(ddgs_backend or "duckduckgo")
        raw_order = tuple(backend_order or self._DEFAULT_BACKEND_ORDER)
        normalized_order: list[str] = []
        seen_stages: set[str] = set()
        for raw_stage in raw_order:
            stage = self._normalize_backend_stage(raw_stage)
            if stage is None or stage in seen_stages:
                continue
            seen_stages.add(stage)
            normalized_order.append(stage)
        self._backend_order = tuple(normalized_order) or self._DEFAULT_BACKEND_ORDER
        self._max_backend_attempts = max(
            1, min(max_backend_attempts or len(self._backend_order), len(self._backend_order))
        )

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _get_ddgs_text_backends() -> frozenset[str]:
        try:
            from ddgs.engines import ENGINES
        except ImportError:
            return frozenset()

        text_engines = ENGINES.get("text", {})
        if not isinstance(text_engines, dict):
            return frozenset()
        return frozenset(str(name).lower() for name in text_engines)

    @classmethod
    def _normalize_backend_stage(cls, stage: str) -> str | None:
        normalized = cls._STAGE_ALIASES.get((stage or "").strip().lower(), (stage or "").strip().lower())
        return normalized if normalized in cls._SUPPORTED_BACKENDS else None

    @classmethod
    def _resolve_ddgs_backend(cls, backend: str) -> str:
        requested = (backend or "duckduckgo").strip().lower()
        legacy_aliases = {
            "html": "duckduckgo",
            "lite": "duckduckgo",
        }
        normalized = legacy_aliases.get(requested, requested)
        available = cls._get_ddgs_text_backends()
        if not available:
            return normalized
        if normalized in available:
            return normalized
        fallback = "duckduckgo" if "duckduckgo" in available else "auto"
        logger.warning(
            "DDGS backend {!r} is unavailable; falling back to {!r}. Available: {}",
            backend,
            fallback,
            ", ".join(sorted(available)),
        )
        return fallback

    def _get_httpx_client(self) -> Any:
        if self._httpx_client is None:
            import httpx

            self._httpx_client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers=self._HEADERS,
                trust_env=self._trust_env,
            )
        return self._httpx_client

    def close(self) -> None:
        if self._httpx_client is not None:
            with contextlib.suppress(Exception):
                self._httpx_client.close()
            self._httpx_client = None

    def _search_ddgs(self, query: str, max_results: int) -> list[dict[str, str]]:
        return self._search_ddgs_backend(query, max_results, self._ddgs_backend)

    def _search_ddgs_backend(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
        from ddgs import DDGS

        resolved_backend = self._resolve_ddgs_backend(backend)
        results: list[dict[str, str]] = []
        with DDGS(timeout=self._timeout) as ddgs:
            results.extend(
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("link", "")),
                    "snippet": r.get("body", r.get("snippet", "")),
                }
                for r in ddgs.text(query, max_results=max_results, backend=resolved_backend)
            )
        return results[:max_results]

    @staticmethod
    def _extract_real_url(href: str) -> str:
        if not href:
            return ""
        if "uddg=" in href:
            match = re.search(r"uddg=([^&]+)", href)
            if match:
                return urllib.parse.unquote(match.group(1))
        if href.startswith("http"):
            return href
        return ""

    def _parse_html_results(self, html_body: str, max_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []

        title_pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )

        titles = title_pattern.findall(html_body)
        snippets = snippet_pattern.findall(html_body)

        for i, (raw_href, raw_title) in enumerate(titles):
            if len(results) >= max_results:
                break

            url = self._extract_real_url(raw_href)
            if not url:
                continue

            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            title = html.unescape(title)

            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
                snippet = html.unescape(snippet)

            results.append({"title": title, "url": url, "snippet": snippet})

        return results

    def _search_html_httpx(self, query: str, max_results: int) -> list[dict[str, str]]:
        client = self._get_httpx_client()
        resp = client.post(self._search_url, data={"q": query})
        resp.raise_for_status()
        return self._parse_html_results(resp.text, max_results)

    def _search_html_urllib(self, query: str, max_results: int) -> list[dict[str, str]]:
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
        request = _Request(
            self._search_url,
            data=data,
            headers=self._HEADERS,
            method="POST",
        )
        with _urlopen(request, timeout=self._timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        return self._parse_html_results(body, max_results)

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        last_exc: Exception | None = None

        for stage in self._backend_order[: self._max_backend_attempts]:
            try:
                if stage == "ddgs":
                    results = self._search_ddgs(query, max_results)
                elif stage == "httpx_html":
                    results = self._search_html_httpx(query, max_results)
                else:
                    results = self._search_html_urllib(query, max_results)

                if results:
                    return results
            except ImportError as exc:
                last_exc = exc
                logger.debug("DuckDuckGo backend={} unavailable: {}", stage, exc)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.debug("DuckDuckGo backend={} failed: {}", stage, exc)

        if last_exc is not None:
            raise _classify_urllib_error(last_exc, provider="duckduckgo") from last_exc
        return []

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            from ddgs import DDGS

            with DDGS(timeout=self._timeout) as ddgs:
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", r.get("source", "")),
                        "image_url": r.get("image", r.get("thumbnail", "")),
                        "snippet": r.get("source", ""),
                    }
                    for r in ddgs.images(query, max_results=max_results)
                ][:max_results]
        except ImportError:
            pass
        except Exception as exc:
            raise _classify_urllib_error(exc, provider="duckduckgo") from exc
        return []


class SerperProvider(ApiKeySearchProvider):
    """Serper API (Google Search). Requires API key."""

    _ENDPOINTS: ClassVar[dict[str, str]] = {
        "search": "https://google.serper.dev/search",
        "images": "https://google.serper.dev/images",
    }

    def __init__(self, api_key: str, timeout: int = 10):
        super().__init__(api_key=api_key)
        self._timeout = timeout
        self._base_url = self._ENDPOINTS["search"]

    def _request(self, url: str, query: str, max_results: int) -> dict[str, Any]:
        payload = json.dumps({"q": query, "num": max_results}).encode("utf-8")
        request = _Request(
            url,
            data=payload,
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with _urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(self._base_url, query, max_results)

            results: list[dict[str, str]] = []
            results.extend(
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in data.get("organic", [])[:max_results]
            )

            if data.get("answerBox") and len(results) < max_results:
                answer = data["answerBox"]
                results.insert(
                    0,
                    {
                        "title": answer.get("title", "Featured Answer"),
                        "url": answer.get("link", ""),
                        "snippet": answer.get("snippet", answer.get("answer", "")),
                    },
                )

            return results[:max_results]

        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="serper") from exc

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(self._ENDPOINTS["images"], query, max_results)
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "image_url": item.get("imageUrl", ""),
                    "snippet": item.get("source", ""),
                }
                for item in data.get("images", [])[:max_results]
            ]
        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="serper") from exc


class TavilyProvider(ApiKeySearchProvider):
    """Tavily API. Requires API key."""

    def __init__(
        self,
        api_key: str,
        timeout: int = 30,
        *,
        include_answer: bool = True,
        search_depth: str = "basic",
    ):
        super().__init__(api_key=api_key)
        self._timeout = timeout
        self._include_answer = include_answer
        self._search_depth = search_depth
        self._base_url = "https://api.tavily.com/search"

    def _request(self, query: str, max_results: int, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            **extra,
        }
        payload = json.dumps(body).encode("utf-8")
        request = _Request(
            self._base_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(
                query,
                max_results,
                include_answer=self._include_answer,
                search_depth=self._search_depth,
            )

            results: list[dict[str, str]] = []
            if data.get("answer"):
                results.append({"title": "Tavily AI Answer", "url": "", "snippet": data["answer"]})

            results.extend(
                {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")}
                for item in data.get("results", [])[:max_results]
            )
            return results[:max_results]

        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="tavily") from exc

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(query, max_results, search_depth="basic", topic="images")
            return [
                {
                    "title": item.get("title", item.get("description", "")),
                    "url": item.get("url", ""),
                    "image_url": item.get("url", ""),
                    "snippet": item.get("description", ""),
                }
                for item in data.get("images", data.get("results", []))[:max_results]
            ]
        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="tavily") from exc


class BraveProvider(ApiKeySearchProvider):
    """Brave Search API. Requires API key."""

    _ENDPOINTS: ClassVar[dict[str, str]] = {
        "web": "https://api.search.brave.com/res/v1/web/search",
        "images": "https://api.search.brave.com/res/v1/images/search",
    }

    def __init__(self, api_key: str, timeout: int = 10):
        super().__init__(api_key=api_key)
        self._timeout = timeout
        self._base_url = self._ENDPOINTS["web"]

    def _request(self, url: str, query: str, max_results: int) -> dict[str, Any]:
        params = urllib.parse.urlencode({"q": query, "count": max_results})
        request = _Request(
            f"{url}?{params}",
            headers={"Accept": "application/json", "X-Subscription-Token": self._api_key},
            method="GET",
        )
        with _urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(self._base_url, query, max_results)
            return [
                {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("description", "")}
                for item in data.get("web", {}).get("results", [])[:max_results]
            ]
        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="brave") from exc

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(self._ENDPOINTS["images"], query, max_results)
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "image_url": item.get("properties", {}).get("url", item.get("thumbnail", {}).get("src", "")),
                    "snippet": item.get("source", ""),
                }
                for item in data.get("results", [])[:max_results]
            ]
        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="brave") from exc


class SearXNGProvider(SearchProvider):
    """SearXNG meta-search (self-hosted). No API key required."""

    _HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(
        self,
        instance_url: str = "https://searx.be",
        timeout: int = 12,
        *,
        trust_env: bool = False,
    ):
        self._instance_url = instance_url.rstrip("/")
        self._timeout = timeout
        self._trust_env = trust_env
        self._httpx_client: Any = None

    def _get_httpx_client(self) -> Any:
        if self._httpx_client is None:
            import httpx

            self._httpx_client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers=self._HEADERS,
                trust_env=self._trust_env,
            )
        return self._httpx_client

    def close(self) -> None:
        if self._httpx_client is not None:
            with contextlib.suppress(Exception):
                self._httpx_client.close()
            self._httpx_client = None

    def _fetch_json(self, query: str, **extra_params: str) -> dict[str, Any]:
        """Fetch JSON from SearXNG, trying httpx first then urllib."""
        params: dict[str, str] = {"q": query, "format": "json", "pageno": "1", **extra_params}
        last_exc: Exception | None = None

        try:
            client = self._get_httpx_client()
            resp = client.get(f"{self._instance_url}/search", params=params)
            resp.raise_for_status()
            return resp.json()
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.debug("SearXNG httpx request failed: {}", exc)

        try:
            qs = urllib.parse.urlencode(params)
            request = _Request(f"{self._instance_url}/search?{qs}", headers=self._HEADERS, method="GET")
            with _urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, KeyError, OSError) as exc:
            last_exc = exc

        if last_exc is not None:
            raise _classify_urllib_error(last_exc, provider="searxng") from last_exc
        return {"results": []}

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        data = self._fetch_json(query)
        return [
            {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")}
            for item in data.get("results", [])[:max_results]
        ]

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        data = self._fetch_json(query, categories="images")
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "image_url": item.get("img_src", item.get("thumbnail_src", "")),
                "snippet": item.get("content", item.get("source", "")),
            }
            for item in data.get("results", [])[:max_results]
        ]


class ExaProvider(ApiKeySearchProvider):
    """Exa neural/semantic search. Requires API key."""

    def __init__(self, api_key: str, timeout: int = 15):
        super().__init__(api_key=api_key)
        self._timeout = timeout
        self._base_url = "https://api.exa.ai/search"

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            payload = json.dumps(
                {
                    "query": query,
                    "numResults": max_results,
                    "useAutoprompt": True,
                }
            ).encode("utf-8")

            request = _Request(
                self._base_url,
                data=payload,
                headers={
                    "x-api-key": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )

            with _urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))

            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("text", item.get("highlight", "")),
                }
                for item in data.get("results", [])[:max_results]
            ]

        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="exa") from exc


class BochaProvider(ApiKeySearchProvider):
    """Bocha (博查) Chinese search. Requires API key."""

    def __init__(self, api_key: str, timeout: int = 15):
        super().__init__(api_key=api_key)
        self._timeout = timeout
        self._base_url = "https://api.bochaai.com/v1/web-search"

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            payload = json.dumps(
                {
                    "query": query,
                    "count": max_results,
                    "freshness": "noLimit",
                }
            ).encode("utf-8")

            request = _Request(
                self._base_url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )

            with _urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))

            web_pages = data.get("data", {}).get("webPages", {}).get("value", [])
            return [
                {
                    "title": item.get("name", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in web_pages[:max_results]
            ]

        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="bocha") from exc


class GoogleProvider(SearchProvider):
    """Google Custom Search JSON API. Requires API key and CSE ID."""

    def __init__(self, api_key: str, cse_id: str, timeout: int = 10):
        self._api_key = api_key
        self._cse_id = cse_id
        self._timeout = timeout
        self._base_url = "https://www.googleapis.com/customsearch/v1"

    def _request(self, query: str, max_results: int, **extra: str) -> dict[str, Any]:
        params = urllib.parse.urlencode(
            {"key": self._api_key, "cx": self._cse_id, "q": query, "num": min(max_results, 10), **extra}
        )
        request = _Request(f"{self._base_url}?{params}", headers={"Accept": "application/json"}, method="GET")
        with _urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(query, max_results)
            return [
                {"title": item.get("title", ""), "url": item.get("link", ""), "snippet": item.get("snippet", "")}
                for item in data.get("items", [])[:max_results]
            ]
        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="google") from exc

    def search_images(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        try:
            data = self._request(query, max_results, searchType="image")
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("image", {}).get("contextLink", item.get("link", "")),
                    "image_url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in data.get("items", [])[:max_results]
            ]
        except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
            raise _classify_urllib_error(exc, provider="google") from exc


# ============================================================
# Provider registry
# ============================================================

PROVIDER_REGISTRY: dict[str, type[SearchProvider]] = {
    "duckduckgo": DuckDuckGoProvider,
    "ddg": DuckDuckGoProvider,
    "serper": SerperProvider,
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "searxng": SearXNGProvider,
    "exa": ExaProvider,
    "bocha": BochaProvider,
    "google": GoogleProvider,
}


def get_provider_class(name: str) -> type[SearchProvider] | None:
    """Look up a provider class by its short name (case-insensitive)."""
    return PROVIDER_REGISTRY.get(name.lower())


def register_provider(name: str, cls: type[SearchProvider]) -> None:
    """Register a custom provider class so it can be referenced by name."""
    PROVIDER_REGISTRY[name.lower()] = cls
