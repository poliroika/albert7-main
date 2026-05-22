"""Intent-aware deep web search tool.

This tool replaces the old ``web_search`` for callers that want the agent
to think before calling the network.  It is gated by a small intent
whitelist so the model cannot use it as a generic chatbot, and it pipes
results into the workspace knowledge base so subsequent rounds have them
in context without re-querying.

Implementation notes:

- The heavy lifting delegates to GMAS's :class:`WebSearchTool`, which
  already supports caching, dedup, multi-provider routing and a
  Playwright/Selenium page-reading mode for dynamic pages. DuckDuckGo is
  GMAS's no-key default provider. This tool must not make generic internet
  access depend on any model-provider key.
- A per-run budget (env ``OUROBOROS_DEEP_SEARCH_BUDGET``, default 6)
  keeps the model from spamming the network: once exhausted the tool
  returns a structured ``BUDGET_EXHAUSTED`` payload.
- Successful results are persisted twice:

  1. As an append-only Markdown block in
     ``workspaces/<ws>/.memory/drive/memory/knowledge/web_research.md``
     (one section per call, with intent + URLs).
  2. As a JSONL event row alongside ``ideas.jsonl`` so retrieval picks
     it up.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.web_search_adapter import (
    attempt_rows as _attempt_rows,
    create_gmas_web_search_tool as _create_gmas_web_search_tool,
    fallback_answer_from_results as _fallback_answer_from_results,
    normalize_results as _normalize_results,
    source_rows as _source_rows,
    status_from_attempts as _status_from_attempts,
)

log = logging.getLogger(__name__)

__all__ = ["get_tools", "INTENT_WHITELIST", "DEEP_SEARCH_BUDGET_DEFAULT"]


INTENT_WHITELIST = {
    "planner_research",
    "subtask_evidence",
    "github_discovery",
    "mcp_discovery",
    "verification_repair",
}

DEEP_SEARCH_BUDGET_DEFAULT = 6
KNOWLEDGE_FILENAME = "web_research.md"

# Per-task in-process counters; we deliberately store them in module-level
# state because the tool registry is reused across tool calls within a
# single Ouroboros run.
_BUDGET_LOCK = threading.Lock()
_BUDGET_USED: dict[str, int] = {}


def _budget_for_run() -> int:
    raw = (os.environ.get("OUROBOROS_DEEP_SEARCH_BUDGET") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEEP_SEARCH_BUDGET_DEFAULT
    except ValueError:
        return DEEP_SEARCH_BUDGET_DEFAULT


def _enabled() -> bool:
    raw = (os.environ.get("OUROBOROS_DEEP_SEARCH_ENABLED") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _provider_name() -> str:
    return (os.environ.get("OUROBOROS_DEEP_SEARCH_PROVIDER") or "").strip().lower()


def _engine_name(engine: str = "") -> str:
    return (engine or os.environ.get("OUROBOROS_DEEP_SEARCH_ENGINE") or "").strip().lower()


def _available_external_engine(engine: str = "") -> str:
    requested = _engine_name(engine)
    if requested in {"firecrawl", "jina", "gmas"}:
        return requested
    if os.environ.get("FIRECRAWL_API_KEY", "").strip():
        return "firecrawl"
    if os.environ.get("JINA_API_KEY", "").strip():
        return "jina"
    return "gmas"


def reset_budget_for_task(task_id: str | None) -> None:
    """Used by tests / callers that want a clean budget per run."""
    if not task_id:
        return
    with _BUDGET_LOCK:
        _BUDGET_USED.pop(str(task_id), None)


def _consume_budget(task_id: str | None) -> tuple[bool, int, int]:
    key = str(task_id or "_global")
    limit = _budget_for_run()
    with _BUDGET_LOCK:
        used = _BUDGET_USED.get(key, 0)
        if used >= limit:
            return False, used, limit
        _BUDGET_USED[key] = used + 1
        return True, used + 1, limit


def _gmas_search(
    query: str,
    *,
    max_results: int,
    fetch_content: bool,
    deep: bool,
    provider: str = "",
    intent: str = "",
) -> dict[str, Any]:
    """Run GMAS WebSearchTool, using Playwright for deep page reads when asked.

    ``deep_search`` should be provider-independent. DuckDuckGo is the default
    GMAS provider, so there is no API-key preflight. If the browser backend is
    unavailable, retry once with GMAS HTTP page fetch so discovery can still
    produce honest evidence instead of reporting a false provider-missing state.
    """
    payload = _gmas_search_once(
        query,
        max_results=max_results,
        fetch_content=fetch_content,
        deep=deep,
        provider=provider,
        intent=intent,
    )
    if deep and payload.get("status") == "provider_error":
        fallback = _gmas_search_once(
            query,
            max_results=max_results,
            fetch_content=True,
            deep=False,
            provider=provider,
            intent=intent,
        )
        fallback["browser_fallback_from"] = payload.get("error", "")
        fallback["browser_attempts_before_fallback"] = payload.get("attempts", [])
        if fallback.get("browser_backend") == "http_fetch":
            fallback["browser_backend"] = "http_fetch_fallback"
        return fallback
    return payload


def _external_deep_search(
    query: str,
    *,
    max_results: int,
    fetch_content: bool,
    engine: str,
) -> dict[str, Any]:
    if engine == "firecrawl":
        return _firecrawl_search(
            query,
            max_results=max_results,
            fetch_content=fetch_content,
        )
    if engine == "jina":
        return _jina_search(
            query,
            max_results=max_results,
        )
    return {
        "status": "provider_error",
        "provider": f"{engine}_deep_search",
        "browser_backend": engine,
        "answer": "",
        "results": [],
        "sources": [],
        "attempts": [
            {
                "provider": engine,
                "status": "error",
                "reason": "unknown_deep_search_engine",
            }
        ],
        "error": f"unknown deep_search engine {engine!r}",
        "retryable": False,
    }


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: int = 60,
) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **headers,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(5_000_000)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _get_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain, text/markdown, application/json;q=0.8",
            "User-Agent": "OuroborosDeepSearch/1.0",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(5_000_000)
    return raw.decode("utf-8", errors="replace")


def _firecrawl_search(
    query: str,
    *,
    max_results: int,
    fetch_content: bool,
) -> dict[str, Any]:
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "provider_error",
            "provider": "firecrawl_deep_search",
            "browser_backend": "firecrawl",
            "answer": "",
            "results": [],
            "sources": [],
            "attempts": [
                {
                    "provider": "firecrawl",
                    "status": "error",
                    "reason": "missing_FIRECRAWL_API_KEY",
                }
            ],
            "error": "FIRECRAWL_API_KEY is required for engine='firecrawl'",
            "retryable": False,
        }
    body: dict[str, Any] = {
        "query": query,
        "limit": max(1, min(max_results, 10)),
        "sources": ["web"],
        "timeout": 60000,
    }
    if fetch_content:
        body["scrapeOptions"] = {"formats": [{"type": "markdown"}]}
    try:
        payload = _post_json(
            "https://api.firecrawl.dev/v2/search",
            body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=70,
        )
        rows = ((payload.get("data") or {}).get("web") or []) if isinstance(payload, dict) else []
        results = _normalize_results(
            [
                {
                    "title": row.get("title") or (row.get("metadata") or {}).get("title") or "",
                    "url": row.get("url") or (row.get("metadata") or {}).get("url") or "",
                    "snippet": row.get("description")
                    or (row.get("metadata") or {}).get("description")
                    or "",
                    "content": row.get("markdown") or row.get("html") or row.get("rawHtml") or "",
                }
                for row in rows
                if isinstance(row, dict)
            ]
        )
        return {
            "status": "ok" if results else "no_results",
            "provider": "firecrawl_deep_search",
            "browser_backend": "firecrawl_search_scrape",
            "answer": _fallback_answer_from_results(results) if results else "(no results)",
            "results": results,
            "sources": _source_rows(results),
            "attempts": [
                {
                    "provider": "firecrawl",
                    "status": "success" if results else "no_results",
                    "result_count": len(results),
                    "credits_used": payload.get("creditsUsed") if isinstance(payload, dict) else None,
                }
            ],
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "provider_error",
            "provider": "firecrawl_deep_search",
            "browser_backend": "firecrawl_search_scrape",
            "answer": "",
            "results": [],
            "sources": [],
            "attempts": [
                {
                    "provider": "firecrawl",
                    "status": "error",
                    "reason": type(exc).__name__,
                    "error": str(exc),
                }
            ],
            "error": repr(exc),
            "retryable": True,
        }


def _jina_search(
    query: str,
    *,
    max_results: int,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = "https://s.jina.ai/" + urllib.parse.quote(query)
    try:
        text = _get_text(url, headers=headers, timeout=45)
        urls = []
        seen: set[str] = set()
        for match in re.finditer(r"https?://[^\s)>\]\"']+", text):
            candidate = match.group(0).rstrip(".,;:")
            if candidate.startswith("https://s.jina.ai/") or candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
            if len(urls) >= max_results:
                break
        results = [
            {
                "title": f"Jina Reader result {idx}",
                "url": item_url,
                "snippet": "",
                "content": text[:4000] if idx == 1 else "",
            }
            for idx, item_url in enumerate(urls, start=1)
        ]
        if not results and text.strip():
            results = [
                {
                    "title": "Jina Search result",
                    "url": url,
                    "snippet": text[:280].replace("\n", " "),
                    "content": text[:4000],
                }
            ]
        normalized = _normalize_results(results)
        return {
            "status": "ok" if normalized else "no_results",
            "provider": "jina_reader_search",
            "browser_backend": "jina_reader",
            "answer": text[:4000] if text else "(no results)",
            "results": normalized,
            "sources": _source_rows(normalized),
            "attempts": [
                {
                    "provider": "jina",
                    "status": "success" if normalized else "no_results",
                    "result_count": len(normalized),
                    "api_key_present": bool(api_key),
                }
            ],
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "status": "provider_error",
            "provider": "jina_reader_search",
            "browser_backend": "jina_reader",
            "answer": "",
            "results": [],
            "sources": [],
            "attempts": [
                {
                    "provider": "jina",
                    "status": "error",
                    "reason": type(exc).__name__,
                    "error": str(exc),
                    "api_key_present": bool(api_key),
                }
            ],
            "error": repr(exc),
            "retryable": True,
        }


def _gmas_search_once(
    query: str,
    *,
    max_results: int,
    fetch_content: bool,
    deep: bool,
    provider: str = "",
    intent: str = "",
) -> dict[str, Any]:
    tool: Any | None = None
    backend = "playwright" if deep else ("http_fetch" if fetch_content else "search_only")
    try:
        tool = _create_gmas_web_search_tool(
            max_results=max_results,
            fetch_content=fetch_content,
            deep_search="playwright" if deep else None,
            max_fetch_pages=min(max_results, 5) if fetch_content else None,
        )
        results, attempts = tool._search_with_fallback(
            query,
            max_results,
            provider=(provider or None),
            intent=(intent or None),
        )
        if fetch_content and results:
            tool._fetch_content_for_results(
                results,
                None,
                query=query,
                no_cache=False,
            )
        prepared = results
        answer = ""
        with_context = bool(fetch_content)
        try:
            prepared = tool._prepare_results_for_output(
                results,
                query=query,
                with_content=with_context,
            )
            answer = tool._format_search_results(
                prepared,
                with_content=with_context,
            )
        except Exception:
            log.debug("GMAS deep_search output formatting failed", exc_info=True)
        normalized = _normalize_results(prepared)
        sources = _source_rows(normalized)
        if not answer:
            answer = _fallback_answer_from_results(sources)
        return {
            "status": _status_from_attempts(normalized, attempts),
            "provider": "gmas_deep_search",
            "browser_backend": backend,
            "answer": answer if normalized else "(no results)",
            "results": normalized,
            "sources": sources,
            "attempts": _attempt_rows(attempts),
        }
    except Exception as exc:
        log.warning("GMAS deep_search failed", exc_info=True)
        return {
            "status": "provider_error",
            "provider": "gmas_deep_search",
            "browser_backend": backend,
            "answer": "",
            "results": [],
            "sources": [],
            "attempts": [],
            "error": repr(exc),
            "retryable": True,
        }
    finally:
        close = getattr(tool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.debug("GMAS deep_search close failed", exc_info=True)


def _workspace_memory_paths(ctx: ToolContext) -> tuple[Path, Path, Path, Path]:
    """Return ``(host_root, workspace_root, memory_root, drive_root)``."""
    repo_dir = Path(getattr(ctx, "repo_dir", ".")).resolve()
    host_raw = getattr(ctx, "host_repo_root", None)
    host_root = Path(host_raw).resolve() if host_raw else repo_dir
    drive_raw = getattr(ctx, "drive_root", None)
    drive_root = (
        Path(drive_raw).resolve() if drive_raw else repo_dir / ".memory" / "drive"
    )
    if drive_root.name == "drive" and drive_root.parent.name == ".memory":
        memory_root = drive_root.parent
        workspace_root = memory_root.parent
    else:
        memory_root = repo_dir / ".memory"
        drive_root = memory_root / "drive"
        workspace_root = repo_dir
    return host_root, workspace_root, memory_root, drive_root


def _rel_or_abs(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except (OSError, ValueError):
        return str(path)


def _persist(
    ctx: ToolContext,
    *,
    query: str,
    intent: str,
    results: list[dict[str, str]],
) -> tuple[str, str]:
    """Persist results to the workspace knowledge directory.

    Returns ``(knowledge_md_path, ideas_jsonl_path)`` (relative-to-repo
    strings or empty).
    """
    knowledge_path = ""
    ideas_path = ""
    host_root, workspace_root, memory_root, drive_root = _workspace_memory_paths(ctx)
    try:
        knowledge_dir = drive_root / "memory" / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        md_path = knowledge_dir / KNOWLEDGE_FILENAME
        block_lines: list[str] = []
        block_lines.append(f"## {intent} — {query}")
        block_lines.append(
            f"_ts: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}_"
        )
        block_lines.append("")
        for idx, item in enumerate(results, start=1):
            title = item.get("title") or "Untitled"
            url = item.get("url") or ""
            snippet = (item.get("snippet") or "").replace("\n", " ").strip()
            block_lines.append(f"{idx}. [{title}]({url}) — {snippet[:280]}")
        block_lines.append("")
        with md_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(block_lines) + "\n")
        knowledge_path = _rel_or_abs(md_path, host_root)
    except OSError:
        log.warning("deep_search persist to knowledge md failed", exc_info=True)

    try:
        ideas_target = memory_root / "ideas.jsonl"
        ideas_target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "id": f"web_{int(time.time() * 1000)}",
            "kind": "web_research",
            "intent": intent,
            "title": f"deep_search: {query[:120]}",
            "content": "; ".join(
                f"{item.get('title')} ({item.get('url')})"
                for item in results[:5]
                if item.get("url")
            ),
            "tags": ["web", "deep_search", intent],
            "task_id": getattr(ctx, "task_id", None),
            "workspace_id": workspace_root.name,
            "palace_path": f"Research/{intent}/{query[:80]}",
            "created_at": time.time(),
        }
        with ideas_target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        ideas_path = _rel_or_abs(ideas_target, host_root)
    except OSError:
        log.warning("deep_search persist to ideas.jsonl failed", exc_info=True)
    try:
        from umbrella.memory.palace.facade import MemPalace
        import pathlib as _pl

        _repo = (
            _pl.Path(ctx.repo_dir)
            if hasattr(ctx, "repo_dir")
            else _pl.Path(".")
        )
        _ws = getattr(ctx, "workspace_id", "") or ""
        _palace = MemPalace(_repo, _ws)
        finding_text = "; ".join(
            f"{item.get('title')} ({item.get('url')})"
            for item in results[:5]
            if item.get("url")
        )
        _palace.add(
            store="palace.idea",
            content=finding_text,
            tier="warm",
            scope="cross_run_durable",
            tags=["finding", "research", "deep_search"],
            verified=False,
            phase="research",
        )
    except Exception:
        pass
    return knowledge_path, ideas_path


def _deep_search(
    ctx: ToolContext,
    query: str = "",
    intent: str = "",
    max_results: int = 5,
    fetch_content: bool = True,
    deep: bool = True,
    provider: str = "",
    engine: str = "",
) -> str:
    try:
        from ouroboros.tools.umbrella_tools import _record_subtask_discovery_tool_call

        _record_subtask_discovery_tool_call(ctx, "deep_search")
    except Exception:
        pass
    if not _enabled():
        return json.dumps(
            {
                "status": "disabled",
                "reason": "OUROBOROS_DEEP_SEARCH_ENABLED=0; skip search and proceed with what you know",
            },
            ensure_ascii=False,
        )

    query_norm = (query or "").strip()
    intent_norm = (intent or "").strip()
    if not query_norm:
        return json.dumps(
            {"status": "error", "reason": "query is required"}, ensure_ascii=False
        )
    if not intent_norm:
        return json.dumps(
            {
                "status": "error",
                "reason": (
                    "intent is required. Specify why you need this search: one of "
                    + ", ".join(sorted(INTENT_WHITELIST))
                ),
            },
            ensure_ascii=False,
        )
    if intent_norm not in INTENT_WHITELIST:
        return json.dumps(
            {
                "status": "error",
                "reason": f"unknown intent {intent_norm!r}. Allowed: {sorted(INTENT_WHITELIST)}",
            },
            ensure_ascii=False,
        )

    ok, used, limit = _consume_budget(getattr(ctx, "task_id", None))
    if not ok:
        return json.dumps(
            {
                "status": "BUDGET_EXHAUSTED",
                "used": used,
                "limit": limit,
                "reason": (
                    "deep_search budget for this run is exhausted. Use what you "
                    "have already found in workspace knowledge (see "
                    "drive/memory/knowledge/web_research.md) instead of new searches."
                ),
            },
            ensure_ascii=False,
        )

    max_results = max(1, min(int(max_results or 5), 15))
    provider_norm = (provider or _provider_name()).strip().lower()
    engine_norm = _available_external_engine(engine)
    if engine_norm in {"firecrawl", "jina"}:
        search_payload = _external_deep_search(
            query_norm,
            max_results=max_results,
            fetch_content=bool(fetch_content),
            engine=engine_norm,
        )
        if search_payload.get("status") == "provider_error" and not _engine_name(engine):
            fallback_payload = _gmas_search(
                query_norm,
                max_results=max_results,
                fetch_content=bool(fetch_content),
                deep=bool(deep),
                provider=provider_norm,
                intent=intent_norm,
            )
            fallback_payload["external_engine_fallback_from"] = search_payload.get("provider", engine_norm)
            fallback_payload["external_engine_error"] = search_payload.get("error", "")
            search_payload = fallback_payload
    else:
        search_payload = _gmas_search(
            query_norm,
            max_results=max_results,
            fetch_content=bool(fetch_content),
            deep=bool(deep),
            provider=provider_norm,
            intent=intent_norm,
        )
    results = _normalize_results(search_payload.get("results", []))
    if not results:
        status = str(search_payload.get("status") or "no_results")
        return json.dumps(
            {
                "status": status,
                "reason": "no provider returned results for this query",
                "query": query_norm,
                "intent": intent_norm,
                "provider": search_payload.get("provider", "gmas_deep_search"),
                "browser_backend": search_payload.get("browser_backend", ""),
                "attempts": search_payload.get("attempts", []),
                "error": search_payload.get("error", ""),
                "retryable": bool(search_payload.get("retryable", status == "provider_error")),
            },
            ensure_ascii=False,
        )

    knowledge_path, ideas_path = _persist(
        ctx, query=query_norm, intent=intent_norm, results=results
    )
    payload = {
        "status": "ok",
        "query": query_norm,
        "intent": intent_norm,
        "provider": search_payload.get("provider", "gmas_deep_search"),
        "browser_backend": search_payload.get("browser_backend", ""),
        "engine": engine_norm,
        "requested_provider": provider_norm,
        "budget_used": used,
        "budget_limit": limit,
        "knowledge_md": knowledge_path,
        "ideas_jsonl": ideas_path,
        "answer": search_payload.get("answer", ""),
        "sources": search_payload.get("sources", []),
        "attempts": search_payload.get("attempts", []),
        "browser_fallback_from": search_payload.get("browser_fallback_from", ""),
        "external_engine_fallback_from": search_payload.get("external_engine_fallback_from", ""),
        "results": results,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return json.dumps(payload, ensure_ascii=False)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "deep_search",
            {
                "name": "deep_search",
                "description": (
                    "Intent-aware deep web search with pluggable engines. "
                    "Default engine is GMAS WebSearchTool: DuckDuckGo no-key "
                    "search plus Playwright-backed page reading, with an HTTP "
                    "content-fetch fallback if the browser backend is not "
                    "available. Optional stronger engines are `firecrawl` "
                    "(requires FIRECRAWL_API_KEY) and `jina` (uses Jina Reader "
                    "Search, optionally with JINA_API_KEY). Call ONLY when you genuinely need "
                    "external evidence (unknown library/API, fresh standards, "
                    "recent error message, similar-project research).  You MUST "
                    "pass a non-empty `intent` from the whitelist: planner_research, "
                    "subtask_evidence, github_discovery, mcp_discovery, "
                    "verification_repair.  Each run has a small budget; results "
                    "are persisted into workspace knowledge so you do not need "
                    "to re-query the same thing."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Concrete search query.",
                        },
                        "intent": {
                            "type": "string",
                            "description": "Why you need this search; must be one of the whitelist.",
                            "enum": sorted(INTENT_WHITELIST),
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 15,
                        },
                        "fetch_content": {
                            "type": "boolean",
                            "default": True,
                            "description": "Fetch result pages and include query-focused page content.",
                        },
                        "deep": {
                            "type": "boolean",
                            "default": True,
                            "description": "Use GMAS Playwright page reading for dynamic pages; falls back to HTTP content fetch if the browser backend fails.",
                        },
                        "provider": {
                            "type": "string",
                            "description": "Optional GMAS provider override such as duckduckgo, brave, serper, tavily, exa, searxng, bocha, or google.",
                        },
                        "engine": {
                            "type": "string",
                            "default": "auto",
                            "enum": ["auto", "gmas", "firecrawl", "jina"],
                            "description": "Deep-search engine. auto uses Firecrawl or Jina only when their keys are configured, otherwise GMAS.",
                        },
                    },
                    "required": ["query", "intent"],
                },
            },
            _deep_search,
        ),
    ]
