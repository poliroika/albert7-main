"""Source-level verification policy checks for generated workspaces."""

import logging
import re
from pathlib import Path

from umbrella.verification.skill_compliance import _MOCK_SCAFFOLD_PATTERNS
from umbrella.verification.workspace_path_policy import (
    WorkspacePathPolicy,
    glob_matches_any,
    normalize_rel,
)

log = logging.getLogger(__name__)

_EXTRA_MOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("numbered news placeholder", re.compile(r"\bNews\s+\d+\b", re.IGNORECASE)),
    ("numbered point placeholder", re.compile(r"\bPoint\s+\d+\b", re.IGNORECASE)),
    ("placeholder image url", re.compile(r"(?:via\.)?placeholder\.com", re.IGNORECASE)),
    ("lorem ipsum", re.compile(r"lorem\s+ipsum", re.IGNORECASE)),
    (
        "future implementation marker",
        re.compile(
            r"\b(?:will|would|to\s+be)\s+(?:be\s+)?implemented\b"
            r"|\bin\s+a\s+real\s+implementation\b"
            r"|\bnot\s+implemented\s+yet\b",
            re.IGNORECASE,
        ),
    ),
    (
        "phase scaffold marker",
        re.compile(
            r"\bphase\s+\d+(?:\s*-\s*\d+)?\b.{0,100}\bimplement",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

_SKIP_SUFFIXES = {
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
}

# Source-policy path globs that should NEVER be scanned for mock/scaffold
# markers. These are *meta* files that legitimately contain the very
# tokens the scanner looks for — they describe what to look for in
# real source files. The most painful production failure was the
# agent self-flagging ``record_verification_lessons.py`` because its
# lesson body literally explained the ``Point 1\nPoint 2\nPoint 3``
# pattern that triggered ``mock_scaffold_scan``.
#
_SOURCE_POLICY_SKIP_PATH_GLOBS: tuple[str, ...] = (
    # In-workspace memory/artifact roots
    ".memory/**",
    ".umbrella/**",
    ".umbrella_scratch/**",
    "**/.memory/**",
    "**/.umbrella/**",
    # Lesson / verification context meta-files
    "record_verification_lessons.py",
    "**/record_verification_lessons.py",
    "**/*_verification_lessons.py",
    "lessons.md",
    "**/lessons.md",
    "lessons.jsonl",
    "**/lessons.jsonl",
    "verification_failure_context.*",
    "**/verification_failure_context.*",
    # Common dev-tool output dirs that may contain reference text
    ".git/**",
    ".venv/**",
    "vendor/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "node_modules/**",
)


def _normalize_rel(rel: str | Path) -> str:
    return normalize_rel(rel)


def _glob_matches_any(rel: str, patterns: tuple[str, ...]) -> bool:
    return glob_matches_any(rel, patterns)


def load_skip_path_patterns(workspace_path: str | Path) -> tuple[str, ...]:
    """Return the effective skip-path glob list for ``workspace_path``.

    Combines source-policy defaults with whatever the user
    declared under ``[verification]`` in ``workspace.toml``:

    .. code-block:: toml

        [verification]
        skip_paths = [
          "scratch/**",
          "**/*.fixture.py",
        ]

    User patterns win in case of overlap (they are simply appended;
    ``_glob_matches_any`` is OR over the union). Returns an empty
    tuple if the workspace has neither file nor section — never
    raises (this is a hot path in the verification gate).
    """
    policy = WorkspacePathPolicy.load(workspace_path)
    return (
        *_SOURCE_POLICY_SKIP_PATH_GLOBS,
        *policy.skip_patterns,
        *policy.exclude_patterns,
    )


def mock_scaffold_hits(text: str) -> list[str]:
    """Return source/output mock scaffold labels found in ``text``."""
    patterns = (*_MOCK_SCAFFOLD_PATTERNS, *_EXTRA_MOCK_PATTERNS)
    return [label for label, pattern in patterns if pattern.search(str(text or ""))]


def scan_changed_files_for_mock_scaffold(
    *,
    repo_root: Path,
    workspace_path: Path,
    changed_files: list[str],
) -> list[str]:
    """Scan changed source files for mock, placeholder, or scaffold markers.

    Files matching :func:`load_skip_path_patterns` are skipped — this
    avoids self-flagging on lesson/memory meta-files that legitimately
    contain the patterns the scanner is looking for.
    """
    hits: list[str] = []
    repo_root = Path(repo_root).resolve()
    workspace_path = Path(workspace_path).resolve()
    skip_globs = load_skip_path_patterns(workspace_path)

    for rel in changed_files:
        rel_text = _normalize_rel(rel)
        if not rel_text or Path(rel_text).suffix.lower() in _SKIP_SUFFIXES:
            continue
        if _glob_matches_any(rel_text, skip_globs):
            log.debug("source_policy: skipping %s (skip_paths match)", rel_text)
            continue
        candidates = [workspace_path / rel_text, repo_root / rel_text]
        path = next((p for p in candidates if p.exists() and p.is_file()), None)
        if path is None:
            continue
        try:
            path.resolve().relative_to(repo_root)
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for label in mock_scaffold_hits(text):
            hits.append(f"{rel_text}: {label}")
    return hits
