"""MCP discovery and install tools (Ouroboros-side).

These are exposed as Ouroboros tools so the planner/subtask phases can
look up MCP servers via GitHub topic search and add them to the
registry.  Actual installation of arbitrary stdio commands is gated by
:func:`request_user_input` / permission requests on the Web Bridge
side; this module only edits ``.umbrella/mcp/registry.json``.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, List

from umbrella.mcp.registry import McpRegistry, default_registry_path

try:
    from ouroboros.tools.registry import ToolContext, ToolEntry
except (
    Exception
):  # pragma: no cover - keeps module importable in tests without ouroboros
    ToolContext = Any  # type: ignore[assignment]
    ToolEntry = Any  # type: ignore[assignment]

log = logging.getLogger(__name__)

__all__ = ["get_tools", "discover_servers"]


def _retry_after_seconds(headers: dict[str, str], *, default: float = 2.5) -> float:
    raw = (headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if not raw:
        return default
    try:
        return max(1.0, min(float(raw), 10.0))
    except ValueError:
        return default


def _http_get(url: str) -> tuple[int, bytes]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "umbrella-mcp-discovery",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    last_status = 0
    last_body = b""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            last_status = int(exc.code or 0)
            last_body = exc.read() or b""
            if attempt == 0 and last_status in {403, 429}:
                time.sleep(
                    _retry_after_seconds(dict(exc.headers or {}))
                )
                continue
            return last_status, last_body
        except urllib.error.URLError as exc:
            raise RuntimeError(f"github request failed: {exc}") from exc
    return last_status, last_body


def _parse_repo_items(payload: dict[str, Any], *, max_results: int) -> list[dict[str, Any]]:
    items = payload.get("items") or []
    out: list[dict[str, Any]] = []
    for repo in items[:max_results]:
        if not isinstance(repo, dict):
            continue
        license_obj = repo.get("license")
        license_id = (
            (license_obj or {}).get("spdx_id") if isinstance(license_obj, dict) else ""
        )
        out.append(
            {
                "name": str(repo.get("full_name") or repo.get("name") or ""),
                "url": str(repo.get("html_url") or ""),
                "description": str(repo.get("description") or ""),
                "stars": int(repo.get("stargazers_count") or 0),
                "license": str(license_id or ""),
                "topics": list(repo.get("topics") or []),
                "install_hint_npx": (
                    f"npx -y @{repo.get('full_name')}"  # heuristic, may not be valid
                ),
            }
        )
    return out


def _search_repositories(q: str, *, max_results: int) -> tuple[list[dict[str, Any]], str | None]:
    encoded = urllib.parse.quote(q)
    url = (
        f"https://api.github.com/search/repositories?q={encoded}"
        f"&sort=stars&per_page={max_results}"
    )
    status, body = _http_get(url)
    if status != 200:
        if status in {403, 429}:
            return [], "rate_limited"
        return [], "error"
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return [], "error"
    if not isinstance(payload, dict):
        return [], "error"
    return _parse_repo_items(payload, max_results=max_results), None


def discover_servers(query: str, *, max_results: int = 5) -> dict[str, Any]:
    """Search GitHub for MCP servers; works without GITHUB_TOKEN (anonymous API limits)."""
    query_norm = query.strip()
    if not query_norm:
        return {
            "results": [],
            "warnings": [],
            "search_queries": [],
            "status": "error",
        }
    limit = max(1, min(int(max_results), 10))
    warnings: list[str] = []
    search_queries: list[str] = []
    results: list[dict[str, Any]] = []
    status = "ok"

    primary_q = f"topic:mcp-server {query_norm}"
    search_queries.append(primary_q)
    primary, err = _search_repositories(primary_q, max_results=limit)
    if err:
        warnings.append(f"github_search_failed:{err}:topic_query")
        status = err
    results.extend(primary)

    if not results:
        fallback_q = f"mcp {query_norm} in:name,description"
        search_queries.append(fallback_q)
        fallback, err2 = _search_repositories(fallback_q, max_results=limit)
        if err2:
            warnings.append(f"github_search_failed:{err2}:fallback_query")
            status = err2 if status == "ok" else status
        results.extend(fallback)

    # Deduplicate by repo name
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in results:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(item)
    return {
        "results": deduped[:limit],
        "warnings": warnings,
        "search_queries": search_queries,
        "status": status if deduped else status,
    }


def _resolve_repo_root(ctx: Any) -> Path:
    host = getattr(ctx, "host_repo_root", None)
    if host:
        return Path(host).resolve()
    repo = getattr(ctx, "repo_dir", None)
    if repo:
        return Path(repo).resolve()
    return Path.cwd()


def _mcp_discover(
    ctx: Any, query: str = "", max_results: int = 5, intent: str = ""
) -> str:
    try:
        from ouroboros.tools.umbrella_tools import _record_subtask_discovery_tool_call

        _record_subtask_discovery_tool_call(ctx, "mcp_discover")
    except Exception:
        pass
    query_norm = (query or "").strip()
    if not query_norm:
        return json.dumps(
            {"status": "error", "reason": "query is required"}, ensure_ascii=False
        )
    discovery = discover_servers(
        query_norm, max_results=max(1, min(int(max_results or 5), 10))
    )
    results = list(discovery.get("results") or [])
    warnings = list(discovery.get("warnings") or [])
    search_queries = list(discovery.get("search_queries") or [])
    discovery_status = str(discovery.get("status") or "ok")
    response_status = "ok"
    if not results and discovery_status in {"rate_limited", "error"}:
        response_status = discovery_status
    elif not results and warnings:
        response_status = "error"
    # Mirror each discovered MCP server to workspace memory (JSONL +
    # semantic palace). Previously ``mcp_discover`` was completely
    # write-through: results only existed in the tool reply and were
    # lost as soon as the agent moved on, so subsequent
    # ``get_umbrella_memory`` queries (e.g. on the next remediation
    # cycle) never knew which MCP servers had been explored.
    mirrored = 0
    if results:
        try:
            from umbrella.memory.external_findings import (
                mirror_external_finding_to_memory,
            )

            for item in results:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("full_name") or "").strip()
                if not name:
                    continue
                body = (
                    f"{name} — {str(item.get('description') or '(no description)')[:600]}\n"
                    f"url: {item.get('url') or item.get('html_url') or ''}\n"
                    f"licence: {item.get('license') or item.get('licence') or 'unknown'}\n"
                    f"stars: {item.get('stars') or 0}\n"
                    f"search_query: {query_norm}"
                )
                res = mirror_external_finding_to_memory(
                    ctx,
                    kind="mcp_server",
                    title=f"mcp:{name}",
                    body=body,
                    tags=["mcp", "discovery", "external_research"],
                    palace_room="mcp_discovery",
                    palace_subpath=f"mcp/{name}",
                    metadata_extra={
                        "url": item.get("url") or item.get("html_url"),
                        "stars": item.get("stars"),
                        "licence": item.get("license") or item.get("licence"),
                    },
                )
                if res.get("mirrored"):
                    mirrored += 1
        except Exception:
            pass
    return json.dumps(
        {
            "status": response_status,
            "query": query_norm,
            "intent": str(intent or ""),
            "results": results,
            "warnings": warnings,
            "search_queries": search_queries,
            "memory_mirrored_count": mirrored,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "next_step": (
                "If a result looks useful, register it via mcp_install (the user "
                "will be asked to approve before stdio commands are launched). "
                "All results are also mirrored to workspace memory so future "
                "`get_umbrella_memory` recall can surface them."
            ),
        },
        ensure_ascii=False,
    )


def _mcp_install(
    ctx: Any,
    name: str = "",
    transport: str = "stdio",
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    env: dict[str, str] | None = None,
    description: str = "",
) -> str:
    if not name.strip():
        return json.dumps(
            {"status": "error", "reason": "name is required"}, ensure_ascii=False
        )
    repo_root = _resolve_repo_root(ctx)
    registry = McpRegistry(repo_root)
    try:
        spec = registry.add_new(
            name=name.strip(),
            transport=transport,
            command=command,
            args=list(args or []),
            url=url,
            env=dict(env or {}),
            source="discovered",
            description=description,
            status="disabled",  # always disabled until user approves in UI
        )
    except ValueError as exc:
        return json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {
            "status": "ok",
            "registered": True,
            "spec": spec.to_dict(),
            "registry_path": str(default_registry_path(repo_root)),
            "next_step": (
                "Server registered as 'disabled'. The user must enable it in the "
                "MCP Registry UI before its tools become available."
            ),
        },
        ensure_ascii=False,
    )


def get_tools() -> list[Any]:
    return [
        ToolEntry(
            "mcp_discover",
            {
                "name": "mcp_discover",
                "description": (
                    "Search GitHub for MCP servers (topic:mcp-server) related to your task.  "
                    "Returns name, url, description, license, stars. If a result "
                    "is plausibly useful for the current task, follow up with "
                    "mcp_install to register a disabled candidate for user approval."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {
                            "type": "integer",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "intent": {
                            "type": "string",
                            "description": (
                                "Optional caller metadata such as mcp_discovery "
                                "or planner_research; ignored by server search."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
            _mcp_discover,
        ),
        ToolEntry(
            "mcp_install",
            {
                "name": "mcp_install",
                "description": (
                    "Register a new MCP server in the workspace registry.  The "
                    "server is added as 'disabled' and only becomes usable after "
                    "the user explicitly enables it in the MCP Registry UI. Use "
                    "this after mcp_discover when the server could materially help "
                    "the task; do not register random low-confidence results."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "transport": {
                            "type": "string",
                            "enum": ["stdio", "http", "sse"],
                            "default": "stdio",
                        },
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "url": {"type": "string"},
                        "env": {"type": "object"},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "transport"],
                },
            },
            _mcp_install,
        ),
    ]
