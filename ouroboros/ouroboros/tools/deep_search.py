"""Intent-aware deep web search tool.

This tool replaces the old ``web_search`` for callers that want the agent
to think before calling the network.  It is gated by a small intent
whitelist so the model cannot use it as a generic chatbot, and it pipes
results into the workspace knowledge base so subsequent rounds have them
in context without re-querying.

Implementation notes:

- The heavy lifting tries to delegate to GMAS's :class:`WebSearchTool`,
  which already supports caching, dedup, multi-provider routing and an
  optional Playwright/Selenium "deep" mode for dynamic pages.  When GMAS
  is unavailable the tool falls back to the existing
  :func:`ouroboros.tools.search._web_search` (DuckDuckGo + LLM summary).
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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

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
) -> list[dict[str, str]] | None:
    """Try the GMAS WebSearchTool when available.  Returns None on any failure."""
    try:
        from gmas.tools.web_search import WebSearchTool
    except Exception:
        return None
    try:
        kwargs: dict[str, Any] = {
            "max_results": max_results,
            "fetch_content": fetch_content,
            "deduplicate": True,
            "cache": True,
        }
        if deep:
            kwargs["deep_search"] = "playwright"
        # GMAS auto-routes by env keys (SERPER, TAVILY, BRAVE, ...).  If the
        # caller pinned a provider via OUROBOROS_DEEP_SEARCH_PROVIDER we let
        # GMAS pick that provider explicitly.
        provider_hint = _provider_name()
        if provider_hint:
            kwargs["provider"] = provider_hint
        tool = WebSearchTool(**kwargs)
        result = tool.execute(query=query, fetch_content=fetch_content)
        if not getattr(result, "success", False):
            return None
        # WebSearchTool formats text; we still need raw items.  When
        # `structured_output` is present prefer it, otherwise parse the
        # formatted string.
        structured = getattr(result, "structured_output", None) or {}
        items = structured.get("results") if isinstance(structured, dict) else None
        if isinstance(items, list):
            return _normalize_results(items)
        text = getattr(result, "output", "") or ""
        return _parse_formatted_results(text)
    except Exception:
        log.warning("GMAS WebSearchTool failed", exc_info=True)
        return None


def _normalize_results(items: list[Any]) -> list[dict[str, str]]:
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


def _parse_formatted_results(text: str) -> list[dict[str, str]]:
    """Best-effort extraction of (title, url, snippet) blocks from formatted text."""
    if not text:
        return []
    out: list[dict[str, str]] = []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0]
        url = ""
        snippet_parts: list[str] = []
        for line in lines[1:]:
            if line.lower().startswith(("url:", "https://", "http://")):
                url = (
                    line.split(":", 1)[-1].strip()
                    if line.lower().startswith("url:")
                    else line.strip()
                )
            else:
                snippet_parts.append(line)
        out.append(
            {
                "title": title.lstrip("0123456789. ").strip(),
                "url": url,
                "snippet": " ".join(snippet_parts)[:600],
                "content": "",
            }
        )
    return out


def _fallback_search(query: str, *, max_results: int) -> list[dict[str, str]]:
    try:
        from ouroboros.tools.search import _web_search_via_duckduckgo
    except Exception:
        return []
    try:
        payload = _web_search_via_duckduckgo(query, max_results=max_results)
    except Exception:
        return []
    sources = payload.get("sources") if isinstance(payload, dict) else []
    if not isinstance(sources, list):
        return []
    return _normalize_results(sources)


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
    return knowledge_path, ideas_path


def _deep_search(
    ctx: ToolContext,
    query: str = "",
    intent: str = "",
    max_results: int = 5,
    fetch_content: bool = False,
    deep: bool = False,
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
    results = _gmas_search(
        query_norm,
        max_results=max_results,
        fetch_content=bool(fetch_content),
        deep=bool(deep),
    )
    used_provider = "gmas"
    if not results:
        results = _fallback_search(query_norm, max_results=max_results)
        used_provider = "duckduckgo_fallback"
    if not results:
        return json.dumps(
            {
                "status": "no_results",
                "reason": "no provider returned results for this query",
                "query": query_norm,
                "intent": intent_norm,
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
        "provider": used_provider,
        "budget_used": used,
        "budget_limit": limit,
        "knowledge_md": knowledge_path,
        "ideas_jsonl": ideas_path,
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
                    "Intent-aware web search.  Call ONLY when you genuinely need "
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
                            "default": False,
                            "description": "When true, fetch each result page and include its content.",
                        },
                        "deep": {
                            "type": "boolean",
                            "default": False,
                            "description": "Use Playwright for dynamic pages (slow; only for JS-heavy sites).",
                        },
                    },
                    "required": ["query", "intent"],
                },
            },
            _deep_search,
        ),
    ]
