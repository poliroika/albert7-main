"""GitHub project discovery and snippet extraction.

This module gives the agent a way to look up similar open-source
projects when starting on something new and to pull a small number of
code snippets for inspiration.  The goal is *not* to copy other
people's code wholesale — there are licence rules baked in here so the
agent only ever stores raw code from permissive licences (MIT, Apache,
BSD, ISC, Unlicence, MPL).  For everything else we keep a description
and a link only.

Persistence layout::

    workspaces/<ws>/.memory/drive/memory/knowledge/inspiration/
        <owner>/<repo>/index.md          <-- summary card per repo
        <owner>/<repo>/<safe_path>.md    <-- one file per extracted snippet
"""

import json
import logging
import os
import fnmatch
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ouroboros.limits import DISCOVERY_CONTENT_CHARS
from ouroboros.tools.registry import ToolContext, ToolEntry
from umbrella.discovery.adoption_playbook import external_adoption_playbook
from umbrella.discovery.external_catalog import mirror_preview_body, register_card
from umbrella.discovery.web_page_chunks import preview_text

log = logging.getLogger(__name__)

__all__ = [
    "PERMISSIVE_LICENCES",
    "DEFAULT_BUDGET",
    "get_tools",
]

PERMISSIVE_LICENCES = {
    "mit",
    "apache-2.0",
    "bsd-3-clause",
    "bsd-2-clause",
    "isc",
    "unlicense",
    "mpl-2.0",
    "0bsd",
    "cc0-1.0",
}


def _github_extract_suggestions(
    *, licence_permissive: bool, query: str
) -> dict[str, Any]:
    paths = ["README.md"]
    if licence_permissive:
        paths.extend(["src/", "examples/", "app/"])
    return {
        "paths": paths,
        "queries": [query.strip(), "agent", "main loop"] if query.strip() else ["agent"],
        "tool": "github_extract_snippets",
    }

DEFAULT_BUDGET = 10
DEFAULT_EXTRACT_BUDGET = 12
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".m",
    ".mm",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".ps1",
}
DOC_NAMES = {"readme.md", "readme.rst", "readme.txt"}


def _budget_search() -> int:
    raw = (os.environ.get("OUROBOROS_GITHUB_DISCOVERY_BUDGET") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_BUDGET
    except ValueError:
        return DEFAULT_BUDGET


def _budget_extract() -> int:
    raw = (os.environ.get("OUROBOROS_GITHUB_EXTRACT_BUDGET") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_EXTRACT_BUDGET
    except ValueError:
        return DEFAULT_EXTRACT_BUDGET


_USED_SEARCH: dict[str, int] = {}
_USED_EXTRACT: dict[str, int] = {}


def reset_budget(task_id: str | None) -> None:
    if not task_id:
        return
    _USED_SEARCH.pop(str(task_id), None)
    _USED_EXTRACT.pop(str(task_id), None)


def _consume(
    counter: dict[str, int], task_id: str | None, limit: int
) -> tuple[bool, int]:
    key = str(task_id or "_global")
    used = counter.get(key, 0)
    if used >= limit:
        return False, used
    counter[key] = used + 1
    return True, used + 1


def _retry_after_seconds(headers: dict[str, str], *, default: float = 2.5) -> float:
    raw = (headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if not raw:
        return default
    try:
        return max(1.0, min(float(raw), 10.0))
    except ValueError:
        return default


def _http_get(
    url: str, *, timeout: float = 15.0, allow_retry: bool = True
) -> tuple[int, bytes, dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "umbrella-github-discovery",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    last_status = 0
    last_body = b""
    last_headers: dict[str, str] = {}
    attempts = 2 if allow_retry else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            last_status = int(exc.code or 0)
            last_body = exc.read() or b""
            last_headers = dict(exc.headers or {})
            if (
                allow_retry
                and attempt == 0
                and last_status in {403, 429}
            ):
                time.sleep(_retry_after_seconds(last_headers))
                continue
            return last_status, last_body, last_headers
        except urllib.error.URLError as exc:
            raise RuntimeError(f"github request failed: {exc}") from exc
    return last_status, last_body, last_headers


def _normalize_licence(licence: Any) -> str:
    if isinstance(licence, dict):
        spdx = licence.get("spdx_id") or licence.get("key") or ""
        return str(spdx).strip().lower()
    if isinstance(licence, str):
        return licence.strip().lower()
    return ""


def _safe_path_segment(value: str) -> str:
    cleaned = []
    for ch in value:
        if ch.isalnum() or ch in "-_./":
            cleaned.append(ch)
        else:
            cleaned.append("_")
    out = "".join(cleaned).strip("/")
    return out or "untitled"


def _knowledge_root(ctx: ToolContext) -> Path:
    drive_root = Path(getattr(ctx, "drive_root", "") or "")
    if drive_root.name == "drive" and drive_root.parent.name == ".memory":
        target = drive_root / "memory" / "knowledge" / "inspiration"
    else:
        base = Path(getattr(ctx, "repo_dir", "."))
        target = base / ".memory" / "drive" / "memory" / "knowledge" / "inspiration"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _knowledge_rel_base(ctx: ToolContext) -> Path:
    host_root = Path(getattr(ctx, "host_repo_root", "") or "")
    if host_root:
        return host_root.resolve()
    drive_root = Path(getattr(ctx, "drive_root", "") or "")
    if drive_root.name == "drive" and drive_root.parent.name == ".memory":
        return drive_root.parent.parent.resolve()
    return Path(getattr(ctx, "repo_dir", ".")).resolve()


def _write_repo_index(
    ctx: ToolContext,
    *,
    repo: dict,
    licence_norm: str,
) -> str:
    full_name = str(repo.get("full_name") or "").strip()
    if not full_name:
        return ""
    target = _knowledge_root(ctx) / _safe_path_segment(full_name) / "index.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    block = [
        f"# {full_name}",
        f"_url: {repo.get('html_url') or ''}_",
        f"_licence: {licence_norm or 'unknown'}_",
        f"_stars: {repo.get('stargazers_count') or 0}, forks: {repo.get('forks_count') or 0}_",
        "",
        str(repo.get("description") or "(no description)"),
        "",
        "## Topics",
        ", ".join(str(t) for t in (repo.get("topics") or [])) or "(none)",
        "",
        f"_recorded_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}_",
        "",
    ]
    target.write_text("\n".join(block), encoding="utf-8")
    try:
        return str(target.relative_to(_knowledge_rel_base(ctx)))
    except (OSError, ValueError):
        return str(target)


def _github_search_repositories(
    query: str, max_repos: int
) -> tuple[list[dict], str | None]:
    """Return (items, error_status). error_status is rate_limited on GitHub throttle."""
    query = query.strip()
    if not query:
        return [], None
    encoded = urllib.parse.quote(query)
    url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&per_page={max_repos}"
    status, body, _ = _http_get(url)
    if status != 200:
        log.warning("github search returned %s for %s", status, url)
        if status in {403, 429}:
            return [], "rate_limited"
        return [], "error"
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return [], "error"
    return list(payload.get("items") or [])[:max_repos], None


def _github_search_code_in_repo(repo_full_name: str, query: str) -> list[dict]:
    if not repo_full_name or not query.strip():
        return []
    encoded = urllib.parse.quote(f"{query} repo:{repo_full_name}")
    url = f"https://api.github.com/search/code?q={encoded}&per_page=10"
    status, body, _ = _http_get(url)
    if status != 200:
        log.warning("github code search returned %s for %s", status, url)
        return []
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    return list(payload.get("items") or [])


def _github_contents(repo_full_name: str, path: str = "") -> list[dict]:
    encoded_path = urllib.parse.quote(path.strip("/"))
    url = (
        f"https://api.github.com/repos/{repo_full_name}/contents/{encoded_path}"
        if encoded_path
        else f"https://api.github.com/repos/{repo_full_name}/contents"
    )
    status, body, _ = _http_get(url)
    if status != 200:
        return []
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _github_tree_paths(repo_full_name: str) -> list[str]:
    url = f"https://api.github.com/repos/{repo_full_name}/git/trees/HEAD?recursive=1"
    status, body, _ = _http_get(url)
    if status != 200:
        return []
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    tree = payload.get("tree") if isinstance(payload, dict) else None
    if not isinstance(tree, list):
        return []
    paths: list[str] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _is_code_path(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in CODE_EXTENSIONS


def _snippet_path_score(path: str) -> tuple[int, str]:
    norm = path.lower()
    name = norm.rsplit("/", 1)[-1]
    if _is_code_path(norm):
        if any(
            part in norm
            for part in ("example", "examples", "demo", "sample", "template")
        ):
            return (0, norm)
        if any(part in norm for part in ("src/", "lib/", "pptemp/", "package/")):
            return (1, norm)
        return (2, norm)
    if name in DOC_NAMES:
        return (5, norm)
    return (9, norm)


def _expand_requested_paths(
    repo_full_name: str, requested_paths: list[str]
) -> list[str]:
    expanded: list[str] = []
    all_tree_paths: list[str] | None = None
    for raw in requested_paths:
        item = str(raw or "").strip().lstrip("/")
        if not item:
            continue
        if any(ch in item for ch in "*?["):
            if all_tree_paths is None:
                all_tree_paths = _github_tree_paths(repo_full_name)
            matches = [
                path
                for path in all_tree_paths
                if fnmatch.fnmatch(path, item)
                or fnmatch.fnmatch(path.rsplit("/", 1)[-1], item)
            ]
            expanded.extend(matches)
            continue
        if item.endswith("/"):
            contents = _github_contents(repo_full_name, item.rstrip("/"))
            files = [
                str(entry.get("path") or "").strip()
                for entry in contents
                if str(entry.get("type") or "") == "file"
            ]
            expanded.extend(path for path in files if path)
            continue
        expanded.append(item)
    deduped = list(dict.fromkeys(expanded))
    return sorted(deduped, key=_snippet_path_score)


def _fetch_file_raw(repo_full_name: str, path: str, ref: str = "HEAD") -> str:
    """Return the raw text of a file in a GitHub repo via raw.githubusercontent."""
    if not repo_full_name or not path:
        return ""
    encoded_path = urllib.parse.quote(path)
    url = f"https://raw.githubusercontent.com/{repo_full_name}/{ref}/{encoded_path}"
    try:
        status, body, _ = _http_get(url, timeout=20.0)
    except RuntimeError:
        return ""
    if status != 200:
        return ""
    try:
        return body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return ""


def _fetch_repo_meta(repo_full_name: str) -> dict:
    url = f"https://api.github.com/repos/{repo_full_name}"
    status, body, _ = _http_get(url)
    if status != 200:
        return {}
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}


def _github_project_search(
    ctx: ToolContext,
    query: str = "",
    language: str | None = None,
    max_repos: int = 5,
    max_results: int | None = None,
) -> str:
    try:
        from ouroboros.tools.umbrella_tools import _record_subtask_discovery_tool_call

        _record_subtask_discovery_tool_call(ctx, "github_project_search")
    except Exception:
        pass
    query_norm = (query or "").strip()
    if not query_norm:
        return json.dumps(
            {"status": "error", "reason": "query is required"}, ensure_ascii=False
        )
    ok, used = _consume(_USED_SEARCH, getattr(ctx, "task_id", None), _budget_search())
    if not ok:
        return json.dumps(
            {
                "status": "BUDGET_EXHAUSTED",
                "used": used,
                "limit": _budget_search(),
                "reason": "github_project_search budget exhausted",
            },
            ensure_ascii=False,
        )
    qualifier = query_norm
    if language:
        qualifier = f"{qualifier} language:{language}"
    if max_results is not None:
        max_repos = max_results
    try:
        max_repos = max(1, min(int(max_repos or 5), 10))
    except (TypeError, ValueError):
        max_repos = 5
    repos, search_error = _github_search_repositories(qualifier, max_repos=max_repos)
    if search_error:
        return json.dumps(
            {
                "status": search_error,
                "query": query_norm,
                "language": language,
                "results": [],
                "reason": f"github repository search returned {search_error}",
                "budget_used": used,
                "budget_limit": _budget_search(),
                "next_step": (
                    "Retry later or narrow the query. When search succeeds, use "
                    "github_extract_snippets on permissive repos (README.md, "
                    "src/, examples/) to study patterns before writing new code."
                ),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            ensure_ascii=False,
        )
    items: list[dict] = []
    mirrored_count = 0
    for repo in repos:
        licence_norm = _normalize_licence(repo.get("license"))
        index_path = _write_repo_index(ctx, repo=repo, licence_norm=licence_norm)
        # Mirror to memory so future ``get_umbrella_memory`` recall (both
        # JSONL hierarchical and semantic palace) surfaces this finding.
        # Previously results only landed as markdown in
        # ``knowledge/inspiration/`` and were invisible to recall.
        full_name = str(repo.get("full_name") or "").strip()
        desc = str(repo.get("description") or "").strip()
        if full_name:
            try:
                from umbrella.memory.external_findings import (
                    mirror_external_finding_to_memory,
                )

                body = mirror_preview_body(
                    source_id=f"github:{full_name}",
                    url=str(repo.get("html_url") or ""),
                    preview=desc,
                    storage_ref=index_path,
                )
                tags = ["github", "inspiration", "external_research"]
                if repo.get("language"):
                    tags.append(f"lang:{str(repo['language']).lower()}")
                mirror = mirror_external_finding_to_memory(
                    ctx,
                    kind="github_repo",
                    title=f"github:{full_name}",
                    body=body,
                    tags=tags,
                    palace_room="github_discovery",
                    palace_subpath=f"github/{full_name}",
                    metadata_extra={
                        "url": repo.get("html_url"),
                        "stars": repo.get("stargazers_count"),
                        "licence": licence_norm,
                    },
                )
                if mirror.get("mirrored"):
                    mirrored_count += 1
            except Exception:
                log.debug("github_project_search memory mirror skipped", exc_info=True)
        licence_permissive = licence_norm in PERMISSIVE_LICENCES
        source_id = f"github:{full_name}"
        catalog_id = register_card(
            ctx,
            kind="github_repo",
            source_id=source_id,
            storage_ref=index_path,
            preview=desc,
            tags=["github", "inspiration"] + [f"lang:{repo['language']}".lower()]
            if repo.get("language")
            else ["github", "inspiration"],
            licence=licence_norm,
            url=str(repo.get("html_url") or ""),
            title=full_name,
            palace_room="github_discovery",
        )
        items.append(
            {
                "name": repo.get("name"),
                "full_name": full_name,
                "html_url": repo.get("html_url"),
                "preview": preview_text(desc),
                "stars": repo.get("stargazers_count"),
                "language": repo.get("language"),
                "license": licence_norm,
                "license_permissive": licence_permissive,
                "index_md": index_path,
                "catalog_id": catalog_id,
                "source_id": source_id,
                "suggested_extract": _github_extract_suggestions(
                    licence_permissive=licence_permissive,
                    query=query_norm,
                ),
            }
        )
    catalog_ids = [str(i.get("catalog_id") or "") for i in items if i.get("catalog_id")]
    return json.dumps(
        {
            "status": "ok",
            "query": query_norm,
            "language": language,
            "results": items,
            "catalog_ids": catalog_ids,
            "memory_mirrored_count": mirrored_count,
            "budget_used": used,
            "budget_limit": _budget_search(),
            "adoption_playbook": external_adoption_playbook(
                source_kind="github_repo",
                source_handle=f"github_project_search:{query_norm}",
            ),
            "next_step": (
                "Pick 1–2 relevant repos. For each, call github_extract_snippets "
                "using suggested_extract.paths/queries, then decide intent "
                "(idea_only | pattern_adapt | codeptr | dependency_import) and "
                "record it via palace_add with a matching source_id. Read "
                "knowledge_md paths before adapting code in execute."
            ),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        ensure_ascii=False,
    )


def _write_snippet(
    ctx: ToolContext,
    *,
    repo_full_name: str,
    path: str,
    body: str,
    licence_norm: str,
    licence_permissive: bool,
    queries: list[str],
) -> tuple[str, bool, str]:
    """Write a snippet file under inspiration/.  Returns (rel_path, included_full_body, blocked_reason)."""
    target_dir = _knowledge_root(ctx) / _safe_path_segment(repo_full_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_target = target_dir / f"{_safe_path_segment(path)}.md"
    file_target.parent.mkdir(parents=True, exist_ok=True)
    raw_url = f"https://github.com/{repo_full_name}/blob/HEAD/{path}"
    parts: list[str] = [
        f"# {repo_full_name}/{path}",
        f"_url: {raw_url}_",
        f"_licence: {licence_norm or 'unknown'} (permissive={licence_permissive})_",
        f"_recorded_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}_",
        f"_queries: {', '.join(queries) or '(none)'}_",
        "",
    ]
    blocked_reason = ""
    if not licence_permissive:
        parts.append(
            "## Body suppressed (non-permissive licence)\n"
            f"This repository's licence ({licence_norm or 'unknown'}) is not in the "
            "permissive whitelist, so we keep only the link and a short summary."
        )
        if body:
            parts.append("")
            parts.append("Summary (first 1500 chars):")
            parts.append("```")
            parts.append(body[:1500])
            parts.append("```")
        blocked_reason = "non_permissive_licence"
    else:
        parts.append("## Snippet")
        parts.append("```")
        parts.append(body[:8000])
        parts.append("```")
    file_target.write_text("\n".join(parts), encoding="utf-8")
    rel = ""
    try:
        rel = str(file_target.relative_to(_knowledge_rel_base(ctx)))
    except (OSError, ValueError):
        rel = str(file_target)
    return rel, licence_permissive, blocked_reason


def _github_extract_snippets(
    ctx: ToolContext,
    repo_full_name: str = "",
    paths: list[str] | None = None,
    queries: list[str] | None = None,
    query: str = "",
    intent: str = "",
    max_results: int | None = None,
) -> str:
    try:
        from ouroboros.tools.umbrella_tools import _record_subtask_discovery_tool_call

        _record_subtask_discovery_tool_call(ctx, "github_extract_snippets")
    except Exception:
        pass
    repo_full_name = (repo_full_name or "").strip()
    if not repo_full_name or "/" not in repo_full_name:
        return json.dumps(
            {
                "status": "error",
                "reason": "repo_full_name like 'owner/repo' is required",
            },
            ensure_ascii=False,
        )
    paths = list(paths or [])
    queries = [q for q in (queries or []) if str(q or "").strip()]
    query = str(query or "").strip()
    if query:
        queries.append(query)
    if not paths and not queries:
        return json.dumps(
            {
                "status": "error",
                "reason": "either paths or queries must be provided",
            },
            ensure_ascii=False,
        )
    ok, used = _consume(_USED_EXTRACT, getattr(ctx, "task_id", None), _budget_extract())
    if not ok:
        return json.dumps(
            {
                "status": "BUDGET_EXHAUSTED",
                "used": used,
                "limit": _budget_extract(),
                "reason": "github_extract_snippets budget exhausted",
            },
            ensure_ascii=False,
        )

    meta = _fetch_repo_meta(repo_full_name)
    licence_norm = _normalize_licence(meta.get("license"))
    licence_permissive = licence_norm in PERMISSIVE_LICENCES

    discovered_paths: list[str] = _expand_requested_paths(repo_full_name, paths)
    if queries:
        for q in queries:
            for item in _github_search_code_in_repo(repo_full_name, q):
                p = str(item.get("path") or "")
                if p and p not in discovered_paths:
                    discovered_paths.append(p)

    try:
        path_limit = max(1, min(int(max_results or 12), 12))
    except (TypeError, ValueError):
        path_limit = 12
    discovered_paths = sorted(
        list(dict.fromkeys(discovered_paths)), key=_snippet_path_score
    )[:path_limit]

    extracted: list[dict] = []
    mirrored_count = 0
    for path in discovered_paths:
        body = _fetch_file_raw(repo_full_name, path)
        if not body:
            continue
        rel, full_body_included, blocked = _write_snippet(
            ctx,
            repo_full_name=repo_full_name,
            path=path,
            body=body,
            licence_norm=licence_norm,
            licence_permissive=licence_permissive,
            queries=queries,
        )
        snippet_text = body if licence_permissive else body[:1500]
        # Mirror to memory (JSONL + semantic palace) so recall surfaces
        # the snippet later. For non-permissive licences we keep a short
        # summary only — same policy as the disk write.
        try:
            from umbrella.memory.external_findings import (
                mirror_external_finding_to_memory,
            )

            mem_body = mirror_preview_body(
                source_id=f"github:{repo_full_name}/{path}",
                url=f"https://github.com/{repo_full_name}/blob/HEAD/{path}",
                preview=snippet_text[:DISCOVERY_CONTENT_CHARS],
                storage_ref=rel,
            )
            mirror = mirror_external_finding_to_memory(
                ctx,
                kind="github_snippet",
                title=f"github:{repo_full_name}/{path}",
                body=mem_body,
                tags=[
                    "github",
                    "snippet",
                    "implementation" if _is_code_path(path) else "documentation",
                    "code_pattern" if _is_code_path(path) else "reference",
                    "external_research",
                    f"repo:{repo_full_name}",
                ],
                palace_room="github_snippets",
                palace_subpath=f"github/{repo_full_name}/snippets",
                metadata_extra={
                    "repo": repo_full_name,
                    "path": path,
                    "licence": licence_norm,
                    "permissive": licence_permissive,
                },
            )
            if mirror.get("mirrored"):
                mirrored_count += 1
        except Exception:
            log.debug("github_extract_snippets memory mirror skipped", exc_info=True)

        source_id = f"github:{repo_full_name}/{path}"
        catalog_id = register_card(
            ctx,
            kind="github_snippet",
            source_id=source_id,
            storage_ref=rel,
            preview=snippet_text[:400] if licence_permissive else body[:400],
            tags=[
                "github",
                "snippet",
                f"repo:{repo_full_name}",
            ],
            licence=licence_norm,
            size_bytes=len(body),
            palace_room="github_snippets",
        )
        extracted.append(
            {
                "path": path,
                "knowledge_md": rel,
                "size": len(body),
                "preview": preview_text(snippet_text),
                "catalog_id": catalog_id,
                "source_id": source_id,
                "research_source_id": f"github:{repo_full_name}",
                "full_body_included": full_body_included,
                "licence_blocked_reason": blocked,
            }
        )

    knowledge_paths = [
        str(item.get("knowledge_md") or "")
        for item in extracted
        if str(item.get("knowledge_md") or "").strip()
    ]
    return json.dumps(
        {
            "status": "ok",
            "repo": repo_full_name,
            "repo_source_id": f"github:{repo_full_name}",
            "intent": str(intent or "").strip(),
            "license": licence_norm,
            "license_permissive": licence_permissive,
            "extracted": extracted,
            "catalog_ids": [
                str(item.get("catalog_id") or "")
                for item in extracted
                if item.get("catalog_id")
            ],
            "memory_mirrored_count": mirrored_count,
            "budget_used": used,
            "budget_limit": _budget_extract(),
            "adoption_playbook": external_adoption_playbook(
                source_kind="github_snippet",
                source_handle=f"github:{repo_full_name}",
                memory_paths=knowledge_paths,
                licence_permissive=licence_permissive,
            ),
            "next_step": (
                "For each extracted knowledge_md file, choose reuse intent "
                "(idea_only | pattern_adapt | codeptr | dependency_import). "
                "Save research_finding records with "
                f"source_id=github:{repo_full_name}. Use the path-level "
                "extracted[].source_id only for codeptr/path-specific notes. "
                "Wire plan subtask codeptr_refs to the workspace paths you "
                "will implement. read_file each snippet before "
                "apply_workspace_patch in execute."
            ),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        ensure_ascii=False,
    )


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "github_project_search",
            {
                "name": "github_project_search",
                "description": (
                    "Search GitHub for repositories related to the task. Returns "
                    "metadata, per-repo index.md under "
                    "`.memory/drive/memory/knowledge/inspiration/`, "
                    "suggested_extract hints, and an adoption_playbook describing "
                    "how to record idea vs pattern vs codeptr vs dependency reuse."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "language": {
                            "type": "string",
                            "description": "Optional language filter (python, typescript, ...).",
                        },
                        "max_repos": {
                            "type": "integer",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 10,
                            "description": "Alias for max_repos; accepted for consistency with other discovery tools.",
                        },
                    },
                    "required": ["query"],
                },
            },
            _github_project_search,
        ),
        ToolEntry(
            "github_extract_snippets",
            {
                "name": "github_extract_snippets",
                "description": (
                    "Pull files from a GitHub repo into inspiration snippets "
                    "(`knowledge/inspiration/<owner>/<repo>/`). Returns "
                    "knowledge_md paths, licence policy, and adoption_playbook "
                    "for deliberate reuse (idea / adapt / codeptr / dependency). "
                    "Call after github_project_search on 1–2 relevant repos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo_full_name": {
                            "type": "string",
                            "description": "owner/repo, e.g. 'pallets/flask'",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files, directories (ending in /), or simple globs (e.g. examples/, src/*.py, **/*.py).",
                        },
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional code-search queries to discover paths.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Alias for a single entry in `queries`.",
                        },
                        "intent": {
                            "type": "string",
                            "default": "",
                            "description": "Optional audit metadata describing why snippets are needed.",
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 12,
                            "minimum": 1,
                            "maximum": 12,
                            "description": "Maximum number of discovered paths to extract.",
                        },
                    },
                    "required": ["repo_full_name"],
                },
            },
            _github_extract_snippets,
        ),
    ]
