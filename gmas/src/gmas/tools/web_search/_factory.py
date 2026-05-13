"""Factory functions for creating WebSearchTool from config."""

import contextlib
import os
from typing import Any

from gmas.config.logging import logger

from ._providers import (
    PROVIDER_REGISTRY,
    ApiKeySearchProvider,
    DuckDuckGoProvider,
    GoogleProvider,
    SearchProvider,
    SearXNGProvider,
    get_provider_class,
)
from ._router import SearchRouter
from ._tool import WebSearchTool


def _resolve_provider(
    provider_name: str,
    *,
    api_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> SearchProvider:
    """Instantiate a :class:`SearchProvider` by its short name."""
    cls = get_provider_class(provider_name)
    if cls is None:
        msg = f"Unknown search provider {provider_name!r}.  Available: {', '.join(sorted(PROVIDER_REGISTRY))}"
        raise ValueError(msg)

    extra = dict(extra) if extra else {}
    _trust_env = extra.pop("trust_env", False)

    if cls is DuckDuckGoProvider:
        return DuckDuckGoProvider(
            timeout=extra.pop("timeout", 12),
            trust_env=_trust_env,
        )

    if cls is SearXNGProvider:
        return SearXNGProvider(
            instance_url=extra.pop("instance_url", extra.pop("url", "https://searx.be")),
            timeout=extra.pop("timeout", 12),
            trust_env=_trust_env,
        )

    _env_map: dict[str, str] = {
        "serper": "SERPER_API_KEY",
        "tavily": "TAVILY_API_KEY",
        "brave": "BRAVE_API_KEY",
        "exa": "EXA_API_KEY",
        "bocha": "BOCHA_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    resolved_key = (
        api_key
        or os.environ.get(
            _env_map.get(provider_name.lower(), ""),
            "",
        )
        or None
    )

    if resolved_key is None:
        env_var = _env_map.get(provider_name.lower(), f"{provider_name.upper()}_API_KEY")
        msg = (
            f"No API key for provider {provider_name!r}.  "
            f"Set the {env_var} environment variable or pass api_key explicitly."
        )
        raise ValueError(msg)

    if cls is GoogleProvider:
        cse_id = extra.pop("cse_id", None) or os.environ.get("GOOGLE_CSE_ID", "")
        if not cse_id:
            msg = (
                "Google provider requires cse_id (Custom Search Engine ID).  Set GOOGLE_CSE_ID env-var or pass cse_id."
            )
            raise ValueError(msg)
        return GoogleProvider(
            api_key=resolved_key,
            cse_id=cse_id,
            timeout=extra.pop("timeout", 10),
        )

    if not issubclass(cls, ApiKeySearchProvider):
        msg = f"Provider {cls.__name__!r} does not support instantiation with an API key."
        raise TypeError(msg)

    return cls(api_key=resolved_key, **extra)


def _create_web_search_tool(**kwargs: Any) -> WebSearchTool:
    """Create a :class:`WebSearchTool` from config parameters."""
    provider_cfg = kwargs.pop("provider", None)
    api_key = kwargs.pop("api_key", None)
    auto_route = kwargs.pop("auto_route", False)
    providers_list: list[dict[str, Any]] | None = kwargs.pop("providers", None)
    trust_env: bool = kwargs.get("trust_env", False)

    if auto_route or providers_list:
        available: dict[str, SearchProvider] = {}

        if providers_list:
            for pcfg in providers_list:
                pname = pcfg.get("provider", pcfg.get("name", ""))
                pkey = pcfg.get("api_key")
                pextra = {k: v for k, v in pcfg.items() if k not in ("provider", "name", "api_key")}
                pextra.setdefault("trust_env", trust_env)
                try:
                    available[pname.lower()] = _resolve_provider(pname, api_key=pkey, extra=pextra)
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping provider {!r}: {}", pname, exc)
        else:
            _env_providers = {
                "serper": "SERPER_API_KEY",
                "tavily": "TAVILY_API_KEY",
                "brave": "BRAVE_API_KEY",
                "exa": "EXA_API_KEY",
                "bocha": "BOCHA_API_KEY",
            }
            for pname, env_var in _env_providers.items():
                if os.environ.get(env_var):
                    with contextlib.suppress(ValueError, TypeError):
                        available[pname] = _resolve_provider(pname)
            available.setdefault("duckduckgo", DuckDuckGoProvider(trust_env=trust_env))

        router = SearchRouter(available_providers=available)

        default_names = router.route("")
        default_provider: SearchProvider = (
            available.get(default_names[0], DuckDuckGoProvider(trust_env=trust_env))
            if default_names
            else DuckDuckGoProvider(trust_env=trust_env)
        )

        tool = WebSearchTool(provider=default_provider, **kwargs)
        tool.set_router(router, available)
        return tool

    _provider_keys = {
        "instance_url",
        "url",
        "cse_id",
        "search_depth",
        "include_answer",
        "backend_order",
        "max_backend_attempts",
        "ddgs_backend",
    }

    provider: SearchProvider | None = None
    if isinstance(provider_cfg, SearchProvider):
        provider = provider_cfg
    elif isinstance(provider_cfg, str):
        if api_key is None:
            api_key = kwargs.pop(f"{provider_cfg.lower()}_api_key", None)
        extra = {k: kwargs.pop(k) for k in list(kwargs) if k in _provider_keys}
        extra.setdefault("trust_env", trust_env)
        try:
            provider = _resolve_provider(provider_cfg, api_key=api_key, extra=extra)
        except (ValueError, TypeError) as exc:
            logger.warning("Cannot create provider {!r} ({}), falling back to DuckDuckGo", provider_cfg, exc)
            provider = DuckDuckGoProvider(trust_env=trust_env)

    return WebSearchTool(provider=provider, **kwargs)
