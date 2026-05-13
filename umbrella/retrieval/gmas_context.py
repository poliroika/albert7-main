"""Rich GMAS context retrieval for Ouroboros tools."""

import json
import logging
from pathlib import Path
from typing import Any

from umbrella.llm_budget import estimate_tokens, get_gmas_context_tokens
from umbrella.retrieval.gmas_chunk_cache import (
    content_hash_prefix,
    get_cached_summary,
    make_cache_key,
    put_cached_summary,
)
from umbrella.retrieval.gmas_summarizer import summarize_chunk
from umbrella.retrieval.service import RetrievalService

log = logging.getLogger(__name__)


def build_gmas_context(
    repo_root: Path,
    query: str,
    *,
    max_results: int = 6,
    max_chars_per_hit: int | None = None,
    token_budget: int | None = None,
    auto_grow: bool = True,
) -> dict[str, Any]:
    """Return GMAS retrieval hits with enough code/docs for direct use.

    ``token_budget`` defaults to ``OUROBOROS_GMAS_CONTEXT_TOKENS``. When
    ``auto_grow`` is True, starts with 8k chars/hit, then may ramp to 30k
    if the aggregate is still below 60% of the budget. Oversized hits are
    summarized (with Palace cache) to fit ``budget / max_results``.
    """
    budget = (
        int(token_budget) if token_budget is not None else get_gmas_context_tokens()
    )
    budget = max(2000, budget)
    mr = max(1, min(int(max_results), 24))

    # Optional explicit cap (used by search_gmas_knowledge tool).
    fixed_cap = None
    if max_chars_per_hit is not None:
        fixed_cap = max(1000, min(int(max_chars_per_hit), 100_000))

    if fixed_cap is not None:
        results = _collect_results(repo_root, query, mr, fixed_cap)
    else:
        results = _collect_results(repo_root, query, mr, 8000)
        total_t = _estimate_results_tokens(results)
        if auto_grow and total_t < budget * 0.6:
            results = _collect_results(repo_root, query, mr, 30000)

    # Merge retrieval + fallback (same as legacy)
    max_chars_fb = fixed_cap if fixed_cap is not None else 30000
    fallback_results = _fallback_gmas_results(
        repo_root, query, mr, min(max_chars_fb, 30000)
    )
    combined_results = _merge_fallback_results(fallback_results, results, mr)

    combined_results = _maybe_summarize_hits(repo_root, combined_results, budget, mr)

    card_meta = _empty_card_fields()
    try:
        service = RetrievalService(repo_root)
        search_limit = max(mr, mr * 4)
        card = service.search(query, max_results=search_limit)
        card_meta = {
            "recommended_pattern": card.recommended_pattern,
            "confidence": card.confidence,
            "key_symbols": _normalize_import_symbols(card.key_symbols[:12]),
            "key_files": [str(path) for path in card.key_files[:12]],
            "example_usage": card.example_usage[:8],
            "doc_references": card.doc_references[:8],
        }
    except Exception:
        log.debug("GMAS card metadata search failed (non-fatal)", exc_info=True)

    est_final = _estimate_results_tokens(combined_results)

    return {
        "query": query,
        "recommended_pattern": card_meta.get("recommended_pattern"),
        "confidence": card_meta.get("confidence"),
        "key_symbols": card_meta.get("key_symbols") or [],
        "key_files": card_meta.get("key_files") or [],
        "example_usage": card_meta.get("example_usage") or [],
        "doc_references": card_meta.get("doc_references") or [],
        "results": combined_results,
        "estimated_context_tokens": est_final,
        "token_budget": budget,
        "policy_hint": (
            "Use this context before writing GMAS-based agents. Prefer copied "
            "patterns from gmas/examples, gmas/docs, and gmas/src over memory-only code."
        ),
    }


def _normalize_import_symbols(symbols: list[str]) -> list[str]:
    """Strip filesystem-layout prefixes from symbols so the model can paste them directly.

    Retrieval indexes symbols by their on-disk path (``src.gmas.callbacks``),
    but the public, importable name is ``gmas.callbacks``. Without this fix
    the agent reliably produces ``ModuleNotFoundError: No module named
    'src.gmas...'`` (observed in the news_cards_ai run).
    """
    fixed: list[str] = []
    for raw in symbols:
        s = str(raw or "").strip()
        if not s:
            continue
        if s.startswith("src.gmas."):
            s = s[len("src.") :]
        elif s == "src.gmas":
            s = "gmas"
        fixed.append(s)
    return fixed


def _empty_card_fields() -> dict[str, Any]:
    return {
        "recommended_pattern": None,
        "confidence": None,
        "key_symbols": [],
        "key_files": [],
        "example_usage": [],
        "doc_references": [],
    }


def _estimate_results_tokens(results: list[dict[str, Any]]) -> int:
    total = 0
    for item in results:
        total += estimate_tokens(str(item.get("content") or ""))
        total += estimate_tokens(
            json.dumps(item.get("metadata") or {}, default=str)[:500]
        )
    return total


def _collect_results(
    repo_root: Path,
    query: str,
    max_results: int,
    max_chars_per_hit: int,
) -> list[dict[str, Any]]:
    service = RetrievalService(repo_root)
    search_limit = max(max_results, max_results * 4)
    card = service.search(query, max_results=search_limit)
    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    ordered_hits = _prefer_gmas_hits(repo_root, card.hits, max_results)

    for hit in ordered_hits:
        path = _resolve_hit_path(repo_root, hit.path)
        path_key = str(path) if path else str(hit.path or "")
        content, truncated, content_source = _hit_content(path, hit, max_chars_per_hit)
        if path_key in seen_paths and content_source == "file":
            content, truncated, content_source = _snippet_from_hit(
                hit, max_chars_per_hit
            )
        seen_paths.add(path_key)

        results.append(
            {
                "title": hit.title,
                "path": path_key,
                "line_number": hit.line_number,
                "source_type": getattr(hit.source_type, "value", str(hit.source_type)),
                "hit_type": getattr(hit.hit_type, "value", str(hit.hit_type)),
                "score": round(float(hit.score), 3),
                "symbol_name": hit.symbol_name,
                "symbol_type": getattr(hit.symbol_type, "value", str(hit.symbol_type))
                if hit.symbol_type
                else None,
                "content_source": content_source,
                "content_truncated": truncated,
                "content": content,
                "retrieval_excerpt": hit.excerpt,
                "metadata": hit.metadata,
            }
        )
    return results


def _maybe_summarize_hits(
    repo_root: Path,
    results: list[dict[str, Any]],
    token_budget: int,
    max_results: int,
) -> list[dict[str, Any]]:
    per_hit = max(800, token_budget // max(1, max_results))
    out: list[dict[str, Any]] = []
    for item in results:
        text = str(item.get("content") or "")
        if estimate_tokens(text) <= per_hit:
            out.append(item)
            continue
        rel = str(item.get("path") or "unknown").replace("\\", "/")
        try:
            rel_path = Path(rel).relative_to(repo_root)
            rel_display = rel_path.as_posix()
        except Exception:
            rel_display = rel
        h = content_hash_prefix(text)
        ck = make_cache_key(rel_display, h, per_hit)
        cached = get_cached_summary(repo_root, ck)
        if cached:
            new_item = dict(item)
            new_item["content"] = cached
            new_item["content_truncated"] = True
            new_item["gmas_summarization"] = "palace_cache"
            out.append(new_item)
            continue
        summary = summarize_chunk(text, per_hit, Path(rel_display))
        put_cached_summary(repo_root, ck, summary, rel_display)
        new_item = dict(item)
        new_item["content"] = summary
        new_item["content_truncated"] = True
        new_item["gmas_summarization"] = "llm"
        out.append(new_item)
    return out


def _merge_fallback_results(
    fallback_results: list[dict[str, Any]],
    retrieval_results: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in fallback_results + retrieval_results:
        key = str(item.get("path") or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= max_results:
            break
    return merged


def _fallback_gmas_results(
    repo_root: Path,
    query: str,
    max_results: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    gmas_root = repo_root / "gmas"
    if not gmas_root.exists():
        return []
    query_tokens = {token for token in query.lower().split() if len(token) > 2}
    candidates = []
    for path in _iter_gmas_files(gmas_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        haystack = f"{path.name} {text[:20000]}".lower()
        overlap = sum(1 for token in query_tokens if token in haystack)
        if overlap <= 0 and path.suffix != ".md":
            continue
        score = overlap + _path_boost(path)
        candidates.append((score, path, text))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        _format_fallback(path, text, score, max_chars)
        for score, path, text in candidates[:max_results]
    ]


def _iter_gmas_files(gmas_root: Path) -> list[Path]:
    roots = [
        gmas_root / "examples",
        gmas_root / "docs",
        gmas_root / "src" / "gmas",
        gmas_root / "README.md",
        gmas_root / "QUICKSTART.md",
        gmas_root / "DOCUMENTATION.md",
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.exists():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.lower() in {
                    ".py",
                    ".md",
                    ".toml",
                    ".yaml",
                    ".yml",
                }:
                    files.append(path)
    return files


def _path_boost(path: Path) -> float:
    text = str(path).replace("\\", "/").lower()
    if "/examples/" in text:
        return 3.0
    if "/docs/" in text:
        return 2.0
    if text.endswith("quickstart.md"):
        return 2.5
    return 1.0


def _format_fallback(
    path: Path, text: str, score: float, max_chars: int
) -> dict[str, Any]:
    truncated = len(text) > max_chars
    return {
        "title": path.name,
        "path": str(path),
        "line_number": 0,
        "source_type": "gmas_fallback",
        "hit_type": "file_context",
        "score": round(float(score), 3),
        "symbol_name": None,
        "symbol_type": None,
        "content_source": "gmas_file",
        "content_truncated": truncated,
        "content": text[:max_chars] if truncated else text,
        "retrieval_excerpt": "",
        "metadata": {"fallback": True},
    }


def _prefer_gmas_hits(repo_root: Path, hits: list[Any], max_results: int) -> list[Any]:
    gmas_hits = []
    other_hits = []
    for hit in hits:
        path = _resolve_hit_path(repo_root, hit.path)
        path_text = str(path or hit.path or "").replace("\\", "/").lower()
        if "/gmas/" in path_text:
            gmas_hits.append(hit)
        else:
            other_hits.append(hit)
    ordered = gmas_hits + other_hits
    return ordered[: max(1, max_results)]


def _resolve_hit_path(repo_root: Path, hit_path: Path | None) -> Path | None:
    if hit_path is None:
        return None
    path = Path(hit_path)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def _hit_content(path: Path | None, hit: Any, max_chars: int) -> tuple[str, bool, str]:
    if path and path.exists() and path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= max_chars:
            return text, False, "file"
        window = _line_window(text, hit.line_number, max_chars)
        return window, True, "file_window"
    return _snippet_from_hit(hit, max_chars)


def _snippet_from_hit(hit: Any, max_chars: int) -> tuple[str, bool, str]:
    text = str(getattr(hit, "content", "") or getattr(hit, "excerpt", "") or "")
    if len(text) <= max_chars:
        return text, False, "retrieval_chunk"
    return text[:max_chars], True, "retrieval_chunk"


def _line_window(text: str, line_number: int, max_chars: int) -> str:
    lines = text.splitlines()
    if line_number <= 0:
        return text[:max_chars]
    index = max(0, min(line_number - 1, len(lines) - 1))
    start = max(0, index - 80)
    end = min(len(lines), index + 160)
    while start < end:
        window = "\n".join(lines[start:end])
        if len(window) <= max_chars or end - start <= 20:
            prefix = f"# excerpt around line {line_number}\n"
            return prefix + window[:max_chars]
        start += 10
        end -= 10
    return text[:max_chars]
