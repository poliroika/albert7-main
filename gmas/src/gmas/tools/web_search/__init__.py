"""Web Search tool — search the internet and interact with web pages."""

from ..base import register_tool_factory
from ._cache import SearchCache
from ._factory import _create_web_search_tool, _resolve_provider
from ._fetchers import (
    BrowserFetcher,
    PlaywrightFetcher,
    SeleniumFetcher,
    URLFetcher,
)
from ._html import SimpleHTMLParser
from ._policy import WebSearchPolicy
from ._providers import (
    PROVIDER_REGISTRY,
    BochaProvider,
    BraveProvider,
    DuckDuckGoProvider,
    ExaProvider,
    GoogleProvider,
    SearchError,
    SearchProvider,
    SearXNGProvider,
    SerperProvider,
    TavilyProvider,
    get_provider_class,
    register_provider,
)
from ._router import IntentClassifier, SearchRouter
from ._tool import WebSearchTool
from ._utils import deduplicate_results, normalize_url

__all__ = [
    "PROVIDER_REGISTRY",
    "BochaProvider",
    "BraveProvider",
    "BrowserFetcher",
    "DuckDuckGoProvider",
    "ExaProvider",
    "GoogleProvider",
    "IntentClassifier",
    "PlaywrightFetcher",
    "SearXNGProvider",
    "SearchCache",
    "SearchError",
    "SearchProvider",
    "SearchRouter",
    "SeleniumFetcher",
    "SerperProvider",
    "SimpleHTMLParser",
    "TavilyProvider",
    "URLFetcher",
    "WebSearchPolicy",
    "WebSearchTool",
    "_create_web_search_tool",
    "_resolve_provider",
    "deduplicate_results",
    "get_provider_class",
    "normalize_url",
    "register_provider",
]

register_tool_factory("web_search", _create_web_search_tool)
