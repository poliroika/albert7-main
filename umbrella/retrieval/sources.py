"""
Source collection for GMAS documentation and code.

This module collects all relevant source documents for indexing.
"""

import hashlib
import logging
from pathlib import Path
from typing import List, Optional, Set

from umbrella.retrieval.models import (
    SourceDocument,
    SourceType,
    RetrievalConfig,
)

log = logging.getLogger(__name__)


def collect_gmas_sources(
    repo_root: Path,
    config: RetrievalConfig | None = None,
) -> list[SourceDocument]:
    """
    Collect all GMAS source documents for indexing.

    Args:
        repo_root: Path to the repository root
        config: Optional retrieval configuration

    Returns:
        List of SourceDocument objects.
    """
    if config is None:
        config = RetrievalConfig(repo_root=repo_root, gmas_path=repo_root / "gmas")

    sources = []

    # Collect documentation
    for pattern in config.docs_paths:
        docs = _collect_files_by_pattern(
            repo_root,
            pattern,
            SourceType.DOCUMENTATION,
        )
        sources.extend(docs)

    # Collect source code
    for pattern in config.source_paths:
        code_files = _collect_files_by_pattern(
            repo_root,
            pattern,
            SourceType.SOURCE_CODE,
        )
        sources.extend(code_files)

    # Collect examples
    for pattern in config.example_paths:
        examples = _collect_files_by_pattern(
            repo_root,
            pattern,
            SourceType.EXAMPLE,
        )
        sources.extend(examples)

    # Collect tests
    for pattern in config.test_paths:
        tests = _collect_files_by_pattern(
            repo_root,
            pattern,
            SourceType.TEST,
        )
        sources.extend(tests)

    # Collect workspace usage
    for pattern in config.workspace_paths:
        workspace_files = _collect_files_by_pattern(
            repo_root,
            pattern,
            SourceType.WORKSPACE_USAGE,
        )
        sources.extend(workspace_files)

    # Collect README files separately
    readme_files = [
        repo_root / "gmas" / "README.md",
        repo_root / "gmas" / "QUICKSTART.md",
        repo_root / "gmas" / "DOCUMENTATION.md",
    ]
    for readme_path in readme_files:
        if readme_path.exists():
            source = _create_source_document(
                readme_path,
                SourceType.README,
            )
            sources.append(source)

    # Remove duplicates and filter
    seen_paths: set[Path] = set()
    unique_sources = []
    for source in sources:
        if source.path not in seen_paths:
            unique_sources.append(source)
            seen_paths.add(source.path)

    log.info(f"Collected {len(unique_sources)} source documents for indexing")
    return unique_sources


def _collect_files_by_pattern(
    repo_root: Path,
    pattern: str,
    source_type: SourceType,
) -> list[SourceDocument]:
    """Collect files matching a glob pattern."""
    sources = []

    # Handle recursive patterns (strip trailing /** for path resolution)
    if pattern.endswith("/**"):
        search_path = repo_root / pattern.rstrip("/**")
        if search_path.exists() and search_path.is_dir():
            files = search_path.rglob("*")
        else:
            files = []
    elif "*" in pattern:
        # Use glob for patterns with wildcards
        full_pattern = repo_root / pattern
        files = full_pattern.parent.glob(full_pattern.name)
    else:
        # Direct file path
        full_pattern = repo_root / pattern
        files = [full_pattern] if full_pattern.exists() else []

    for file_path in files:
        if file_path.is_file() and _should_index_file(file_path):
            source = _create_source_document(file_path, source_type)
            sources.append(source)

    return sources


def _create_source_document(
    path: Path,
    source_type: SourceType,
) -> SourceDocument:
    """Create a SourceDocument from a file path."""
    rel_path = path.relative_to(path.anchor or Path("/"))

    # Generate source_id
    source_id = _generate_source_id(path)

    # Determine language and category
    language = _get_language(path)
    category = _get_category(path, source_type)

    # Read content
    content = ""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        log.warning(f"Failed to read {path}: {e}")

    # Generate title
    title = _generate_title(path, source_type, content)

    return SourceDocument(
        source_id=source_id,
        path=path,
        source_type=source_type,
        title=title,
        content=content,
        language=language,
        category=category,
        checksum=_compute_checksum(content),
    )


def _should_index_file(path: Path) -> bool:
    """Check if a file should be indexed."""
    # Skip common exclusions
    exclusions = [
        "__pycache__",
        ".git",
        ".pytest_cache",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        "dist",
        "build",
        "*.egg-info",
    ]

    path_str = str(path)
    for exclusion in exclusions:
        if exclusion in path_str:
            return False

    # Check file extension
    indexed_extensions = {
        ".md",
        ".py",
        ".txt",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
    }

    return path.suffix.lower() in indexed_extensions


def _generate_source_id(path: Path) -> str:
    """Generate a unique source ID from path."""
    return str(path).replace("\\", "/").replace("/", "_")


def _compute_checksum(content: str) -> str:
    """Compute checksum of content."""
    return hashlib.md5(content.encode()).hexdigest()[:16]


def _get_language(path: Path) -> str:
    """Determine programming language from file extension."""
    ext_map = {
        ".py": "python",
        ".md": "markdown",
        ".txt": "text",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
    }
    return ext_map.get(path.suffix.lower(), "unknown")


def _get_category(path: Path, source_type: SourceType) -> str:
    """Determine category from path and source type."""
    path_str = str(path).replace("\\", "/")

    if source_type == SourceType.SOURCE_CODE:
        if "/builder/" in path_str:
            return "builder"
        elif "/tools/" in path_str:
            return "tools"
        elif "/execution/" in path_str:
            return "execution"
        elif "/core/" in path_str:
            return "core"
        elif "/utils/" in path_str:
            return "utils"
    elif source_type == SourceType.DOCUMENTATION:
        if "/api/" in path_str:
            return "api"
        elif "/user-guide/" in path_str:
            return "user_guide"
        elif "/getting-started/" in path_str:
            return "getting_started"
        elif "/examples/" in path_str:
            return "examples"
        elif "/contributing/" in path_str:
            return "contributing"

    return source_type.value


def _generate_title(path: Path, source_type: SourceType, content: str) -> str:
    """Generate a title for the source document."""
    # Try to extract first heading from markdown
    if content and path.suffix == ".md":
        for line in content.split("\n")[:20]:
            line = line.strip()
            if line.startswith("#"):
                # Remove # symbols and extra spaces
                title = line.lstrip("#").strip()
                return title

    # Use filename as fallback
    return path.name
