"""Shared provider-independent web search helpers for Ouroboros tools."""

from __future__ import annotations

import contextlib
from typing import Any


def fallback_answer_from_results(results: list[dict[str, str]]) -> str:
    lines = []
    for idx, item in enumerate(results[:5], start=1):
        title = (item.get("title") or "").strip() or f"Result {idx}"
        url = (item.get("url") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        parts = [f"{idx}. {title}"]
        if snippet:
            parts.append(snippet)
        if url:
            parts.append(url)
        lines.append("\n".join(parts))
    return "\n\n".join(lines) if lines else "(no answer)"


def create_gmas_web_search_tool(
    *,
    max_results: int,
    fetch_content: bool = False,
    deep_search: str | None = None,
    max_fetch_pages: int | None = None,
) -> Any:
    from gmas.tools.web_search import _create_web_search_tool

    kwargs: dict[str, Any] = {
        "auto_route": True,
        "max_results": max_results,
        "fetch_content": fetch_content,
        "trust_env": False,
    }
    if deep_search:
        kwargs["deep_search"] = deep_search
    if max_fetch_pages is not None:
        kwargs["max_fetch_pages"] = max_fetch_pages
    return _create_web_search_tool(**kwargs)


def normalize_results(items: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or ""),
                "content": str(item.get("content") or "")[:4000],
            }
        )
    return out


def source_rows(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "title": (item.get("title") or "").strip(),
            "url": (item.get("url") or "").strip(),
            "snippet": (item.get("snippet") or "").strip(),
        }
        for item in results
        if item.get("url")
    ]


def attempt_rows(attempts: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for attempt in attempts:
        error = getattr(attempt, "error", None)
        row: dict[str, Any] = {
            "provider": str(getattr(attempt, "provider", "")),
            "status": str(getattr(attempt, "status", "")),
            "result_count": int(getattr(attempt, "result_count", 0) or 0),
        }
        if error is not None:
            row["reason"] = str(getattr(error, "reason", "") or "")
            status_code = getattr(error, "status_code", None)
            if status_code is not None:
                row["status_code"] = status_code
            row["error"] = str(error)
        rows.append(row)
    return rows


def status_from_attempts(results: list[dict[str, Any]], attempts: list[Any]) -> str:
    if results:
        return "ok"
    if any(str(getattr(attempt, "status", "")) == "error" for attempt in attempts):
        return "provider_error"
    return "no_results"


def web_search_via_gmas(
    query: str,
    *,
    max_results: int = 5,
    intent: str = "",
    provider: str = "",
    fetch_content: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(max_results), 10))
    tool = create_gmas_web_search_tool(
        max_results=limit,
        fetch_content=fetch_content,
    )
    try:
        results, attempts = tool._search_with_fallback(
            query,
            limit,
            provider=(provider or None),
            intent=(intent or None),
        )
        if fetch_content and results:
            with contextlib.suppress(Exception):
                tool._fetch_content_for_results(
                    results,
                    None,
                    query=query,
                    no_cache=False,
                )
        sources = source_rows(results)
        with contextlib.suppress(Exception):
            prepared = tool._prepare_results_for_output(
                results,
                query=query,
                with_content=fetch_content,
            )
            answer = tool._format_search_results(
                prepared,
                with_content=fetch_content,
            )
            if answer:
                sources = source_rows(prepared)
                return {
                    "status": status_from_attempts(results, attempts),
                    "provider": "gmas_web_search",
                    "answer": answer,
                    "sources": sources,
                    "attempts": attempt_rows(attempts),
                    "max_results": limit,
                }
        return {
            "status": status_from_attempts(results, attempts),
            "provider": "gmas_web_search",
            "answer": fallback_answer_from_results(sources) if sources else "(no results)",
            "sources": sources,
            "attempts": attempt_rows(attempts),
            "max_results": limit,
        }
    finally:
        close = getattr(tool, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
