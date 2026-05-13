"""
Optional workspace context skills for GMAS-backed workspaces.

These helpers keep retrieval/search available as on-demand tools instead of
forcing context into every run upfront.
"""

import fnmatch
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from umbrella.file_preview import read_file_preview
from umbrella.retrieval.models import RetrievalCard, RetrievalHit
from umbrella.retrieval.service import query_gmas

_MAX_SEARCH_RESULTS = 20
_MAX_MATCHES_PER_FILE = 50
_MAX_DISPLAY_MATCHES = 12
_MAX_LINE_LENGTH = 220
_MAX_FILE_SIZE = 100_000
_MAX_READ_SIZE = 12_000
_SKIP_DIR_NAMES = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def _clip(text: str, limit: int = _MAX_LINE_LENGTH) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _format_hit(repo_root: Path, hit: RetrievalHit) -> str:
    label = hit.title or hit.symbol_name or hit.source_id or "match"
    location = ""
    if hit.path:
        location = _safe_relative(repo_root, hit.path)
        if hit.line_number:
            location = f"{location}:{hit.line_number}"
        location = f" [{location}]"
    excerpt = hit.excerpt or hit.content or ""
    excerpt = _clip(excerpt, 240) if excerpt else "No excerpt available."
    return f"- {label}{location}: {excerpt}"


def format_retrieval_card(
    repo_root: Path, card: RetrievalCard, *, max_hits: int = 4
) -> str:
    """Format a RetrievalCard into a compact tool-friendly text block."""
    lines: list[str] = [f"GMAS knowledge query: {card.query}"]

    if card.recommended_pattern:
        lines.append(f"Recommended pattern: {card.recommended_pattern}")
    if card.key_symbols:
        lines.append("Key symbols: " + ", ".join(card.key_symbols[:5]))
    if card.key_files:
        lines.append(
            "Key files: "
            + ", ".join(
                _safe_relative(repo_root, Path(path)) for path in card.key_files[:5]
            )
        )
    if card.example_usage:
        lines.append(
            "Example usage: "
            + " | ".join(_clip(item, 140) for item in card.example_usage[:2])
        )
    if card.anti_patterns:
        lines.append(
            "Avoid: " + " | ".join(_clip(item, 140) for item in card.anti_patterns[:2])
        )
    if card.hits:
        lines.append("Top hits:")
        lines.extend(_format_hit(repo_root, hit) for hit in card.hits[:max_hits])
    else:
        lines.append("Top hits: none")

    return "\n".join(lines)


def search_gmas_knowledge(repo_root: Path, query: str, *, max_results: int = 6) -> str:
    """
    Query GMAS docs/source/examples on demand.

    Intended for tool-style use inside a workspace when the model decides it
    needs deeper GMAS implementation context.
    """
    normalized = query.strip()
    if not normalized:
        return "error: query must be a non-empty string"

    clamped_results = max(1, min(int(max_results), 10))
    try:
        card = query_gmas(repo_root, normalized, max_results=clamped_results)
    except Exception as exc:
        return f"error: GMAS knowledge search failed: {exc}"

    return format_retrieval_card(repo_root, card, max_hits=min(clamped_results, 4))


def _resolve_inside_root(root: Path, relative_path: str) -> Path | None:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _iter_workspace_files(
    root: Path, directory: Path, pattern: str, *, max_depth: int
) -> list[Path]:
    results: list[Path] = []
    stack: list[tuple[Path, int]] = [(directory, 0)]

    while stack and len(results) < _MAX_SEARCH_RESULTS:
        current, depth = stack.pop()
        try:
            entries = sorted(
                current.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
            )
        except OSError:
            continue

        for entry in entries:
            if entry.name.startswith(".") or entry.name in _SKIP_DIR_NAMES:
                continue
            if entry.is_dir():
                if depth < max_depth:
                    stack.append((entry, depth + 1))
                continue
            rel = _safe_relative(root, entry).replace("\\", "/")
            if fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(rel, pattern):
                results.append(entry)
                if len(results) >= _MAX_SEARCH_RESULTS:
                    break

    return results


def _read_workspace_file(root: Path, path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"error: could not stat {_safe_relative(root, path)}: {exc}"

    try:
        content, truncated, kind = read_file_preview(path, max_chars=_MAX_READ_SIZE)
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        return f"error: could not read {_safe_relative(root, path)}: {exc}"

    rel = _safe_relative(root, path)
    header = f"=== {rel}"
    if kind != "text":
        header += f" [{kind}]"
    header += " ==="
    if truncated or size > _MAX_READ_SIZE:
        content += (
            f"\n\n... (truncated to first {_MAX_READ_SIZE} bytes of {size} total bytes)"
        )
    return f"{header}\n{content}"


def _search_file_for_query(
    path: Path, query: str, *, regex: bool
) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    try:
        if path.stat().st_size > _MAX_FILE_SIZE:
            return matches
    except OSError:
        return matches

    pattern = re.compile(query, re.IGNORECASE) if regex else None

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.rstrip()
                matched = (
                    bool(pattern.search(line))
                    if pattern
                    else query.lower() in line.lower()
                )
                if matched:
                    matches.append((line_number, _clip(line)))
                if len(matches) >= _MAX_MATCHES_PER_FILE:
                    break
    except OSError:
        return []

    return matches


def search_workspace_context(
    workspace_root: Path,
    *,
    pattern: str = "*",
    query: str = "",
    read_file: str = "",
    directory: str = "",
    regex: bool = False,
    max_depth: int = 8,
) -> str:
    """
    Search the current workspace instance on demand.

    Supports:
    - reading a specific file
    - listing files by glob
    - searching text/regex across files
    """
    root = workspace_root.resolve()
    target_dir = root

    if directory:
        resolved_dir = _resolve_inside_root(root, directory)
        if resolved_dir is None:
            return "error: directory must stay inside the current workspace"
        if not resolved_dir.exists() or not resolved_dir.is_dir():
            return f"error: directory not found: {directory}"
        target_dir = resolved_dir

    if read_file:
        resolved_file = _resolve_inside_root(root, read_file)
        if resolved_file is None:
            return "error: read_file must stay inside the current workspace"
        if not resolved_file.exists() or not resolved_file.is_file():
            return f"error: file not found: {read_file}"
        return _read_workspace_file(root, resolved_file)

    try:
        if regex:
            re.compile(query)
    except re.error as exc:
        return f"error: invalid regex: {exc}"

    files = _iter_workspace_files(root, target_dir, pattern or "*", max_depth=max_depth)
    if not files:
        return "No workspace files matched the request."

    if not query.strip():
        lines = [f"Workspace root: {root}", f"Matched files ({len(files)}):"]
        lines.extend(
            f"- {_safe_relative(root, path)}" for path in files[:_MAX_DISPLAY_MATCHES]
        )
        if len(files) > _MAX_DISPLAY_MATCHES:
            lines.append(f"... and {len(files) - _MAX_DISPLAY_MATCHES} more")
        return "\n".join(lines)

    output_lines = [f"Workspace search results for: {query.strip()}"]
    total_matches = 0

    for path in files:
        matches = _search_file_for_query(path, query, regex=regex)
        if not matches:
            continue
        rel = _safe_relative(root, path)
        for line_number, snippet in matches[:3]:
            output_lines.append(f"- {rel}:{line_number}: {snippet}")
            total_matches += 1
            if total_matches >= _MAX_DISPLAY_MATCHES:
                break
        if total_matches >= _MAX_DISPLAY_MATCHES:
            break

    if total_matches == 0:
        return "No workspace matches found."

    return "\n".join(output_lines)
