"""Final workspace sweep: noise cleanup + required-file check.

Severity model
--------------
Tier 4.1 — every noise pattern now carries a severity:

- ``block``: clear ad-hoc debug / debris that violates the layout rules
  in ``ouroboros_workspace_task.md §8``. Presence of a single ``block``
  item flips the overall sweep status to ``FAILED``, which the harness
  surfaces as a verification failure so the agent must clean it up
  during remediation instead of "passing" with junk in the repo.
- ``warn``: noise that's plausibly intentional in some workspaces
  (e.g. ``.bak`` backups, ``.tmp``). Cleaned up automatically when
  ``auto_clean=True`` but does not fail verification on its own.

The classification is universal — no per-workspace allowlists. Patterns
that genuinely belong in a workspace (a legitimate ``run_pipeline.py``
entrypoint kept in the package, not the root) move into ``src/`` and
fall out of scope automatically.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections.abc import Iterable

from umbrella.verification.workspace_path_policy import WorkspacePathPolicy

log = logging.getLogger(__name__)


class SweepSeverity(str, Enum):
    """Severity of an individual noise hit."""

    BLOCK = "block"
    WARN = "warn"


class SweepStatus(str, Enum):
    """Overall outcome of ``run_workspace_sweep``."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


# Workspace-wide noise: legacy "always remove" patterns. These can sit
# anywhere in the tree (subject to the excluded-dir filter). Severity is
# ``warn`` — they're cleaned but don't fail the sweep by themselves.
_NOISE_GLOBS_WARN: tuple[str, ...] = (
    "debug_*.py",
    "scratch_*.py",
    "*.bak",
    "*.orig",
    "*.tmp",
    "EMPTY_MARKER.txt",
    ".py",
    ".verification_status.txt",
    "all_files_verified.txt",
    "listing_verification.txt",
    "get-pip.py",
    "subtask*_*.txt",
    "*_installed_dependencies.txt",
)

# Diagnostic / one-off scripts in the workspace root. These are the
# clearest signal of "agent dumped scratch work where production code
# lives" — block-level so verification fails until they're moved or
# removed.
_ROOT_DIAGNOSTIC_GLOBS: tuple[str, ...] = (
    "analyze_*.py",
    "check_*.py",
    "debug_*.py",
    "diagnose_*.py",
    "find_*.py",
    "inspect_*.py",
    "probe_*.py",
    "scan_*.py",
    "scratch_*.py",
    "search_*.py",
    "verify_*.py",
    "run_verification.py",
    "run_dry_run.py",
    "run_checks.py",
    "run_check.py",
    "test_minimal_*.py",
    # Tier 4.1 additions:
    "extract_*.py",
    "fix_*.py",
    "real_test_*.py",
    "test_*_output.py",
    "run_manual_*.py",
    "run_news_*.py",
)

# Output / extraction artifacts that the agent leaves in the root after
# debugging. Block-level — these contaminate the deliverable.
_ROOT_ARTIFACT_GLOBS: tuple[str, ...] = (
    "result.txt",
    "*.pptx",
    "*_test_output.*",
    "*_raw_extracted.*",
    "test_output*.*",
    "test_output_new.*",
    "real_test_output.*",
    "docx_content.txt",
    "template_analysis_raw.*",
    "extracted_*.json",
)

# Raw-extracted artefacts caught anywhere in the tree (under ``docs/``
# is the typical offender: ``docs/requirements_raw.txt``). Block-level
# because the agent should produce a clean version and discard these.
_ANY_DEPTH_ARTIFACT_GLOBS: tuple[str, ...] = (
    "*_raw.txt",
    "*_raw.md",
    "*_raw.json",
    "*_raw.csv",
    "*_raw.tsv",
    "*_raw_extracted.*",
    "*_extracted.json",
)

# Diagnostic scripts caught wherever they appear under ``docs/`` or
# ``src/scripts/`` (or any subdirectory of them). These mirror the
# ``_ROOT_DIAGNOSTIC_GLOBS`` set and are the user's explicit complaint
# about persistent workspace pollution.
_DIAGNOSTIC_SCRIPT_GLOBS_RECURSIVE: tuple[str, ...] = (
    "analyze_*.py",
    "check_*.py",
    "debug_*.py",
    "diagnose_*.py",
    "find_*.py",
    "inspect_*.py",
    "probe_*.py",
    "read_*.py",
    "scan_*.py",
    "scratch_*.py",
    "search_*.py",
    "verify_*.py",
    "validate_*.py",
    "extract_*.py",
    "fix_*.py",
    "real_test_*.py",
    "test_*_output.py",
)

_SRC_PACKAGE_DIAGNOSTIC_GLOBS: tuple[str, ...] = (
    "analyze_*.py",
    "debug_*.py",
    "diagnose_*.py",
    "extract_*.py",
    "inspect_*.py",
    "probe_*.py",
    "scan_*.py",
    "scratch_*.py",
    "test_minimal_*.py",
    "real_test_*.py",
)

# Handoff / architecture / topology markdown that should live in
# ``docs/`` rather than the root. Block-level so the agent learns to
# move them out before final aggregation.
_ROOT_DOC_GLOBS: tuple[str, ...] = (
    "handoff*.md",
    "agent_topology*.md",
    "architecture.md",
    "*_handoff.md",
    "*_topology.md",
    "agent_*.md",
)

_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".memory",
        ".umbrella",
        "node_modules",
        "backups",
        "instances",
        "dist",
        "build",
    }
)

_FILE_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,8}")

_FILE_TOKEN_BLOCKLIST: frozenset[str] = frozenset(
    {
        "pip.install",
        "uv.pip",
    }
)

_RUNTIME_OPTIONAL_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".json",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".cache",
    }
)

_ALWAYS_REQUIRED_FILENAMES: frozenset[str] = frozenset(
    {
        "readme.md",
        "requirements.txt",
    }
)


@dataclass(slots=True)
class NoiseHit:
    """A single noise file matched by the sweep, with its severity."""

    path: str
    severity: SweepSeverity
    category: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "severity": self.severity.value,
            "category": self.category,
        }


@dataclass(slots=True)
class SweepReport:
    workspace_path: str
    removed: list[str] = field(default_factory=list)
    leftover_noise: list[str] = field(default_factory=list)
    blocking_noise: list[NoiseHit] = field(default_factory=list)
    warning_noise: list[NoiseHit] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    auto_clean: bool = True

    @property
    def status(self) -> SweepStatus:
        if self.blocking_noise or self.missing_required:
            return SweepStatus.FAILED
        if self.warning_noise or self.leftover_noise:
            return SweepStatus.WARNING
        return SweepStatus.PASSED

    @property
    def passed(self) -> bool:
        return self.status == SweepStatus.PASSED

    def to_dict(self) -> dict:
        return {
            "workspace_path": self.workspace_path,
            "removed": list(self.removed),
            "leftover_noise": list(self.leftover_noise),
            "blocking_noise": [h.to_dict() for h in self.blocking_noise],
            "warning_noise": [h.to_dict() for h in self.warning_noise],
            "expected_files": list(self.expected_files),
            "missing_required": list(self.missing_required),
            "auto_clean": self.auto_clean,
            "passed": self.passed,
            "status": self.status.value,
            "summary": self.render_summary(),
        }

    def render_summary(self) -> str:
        parts: list[str] = []
        if self.removed:
            parts.append(f"removed noise: {', '.join(self.removed)}")
        if self.blocking_noise:
            blocking_paths = ", ".join(h.path for h in self.blocking_noise)
            parts.append(
                f"BLOCKING noise ({len(self.blocking_noise)}): {blocking_paths} — "
                "move ad-hoc scripts into src/scripts/ or delete; "
                "artifacts/docs belong under build/ or docs/"
            )
        if self.leftover_noise:
            parts.append(f"leftover noise: {', '.join(self.leftover_noise)}")
        if self.missing_required:
            parts.append(f"missing required files: {', '.join(self.missing_required)}")
        if not parts:
            return "Workspace clean and all required files present."
        return "; ".join(parts)


def _iter_workspace_files(
    root: Path, policy: WorkspacePathPolicy | None = None
) -> Iterable[Path]:
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        cur_path = Path(current)
        if policy is not None:
            filtered: list[str] = []
            for dirname in dirnames:
                try:
                    rel = (cur_path / dirname).relative_to(root).as_posix()
                except ValueError:
                    rel = dirname
                if not policy.is_dependency_or_runtime(rel):
                    filtered.append(dirname)
            dirnames[:] = filtered
        for fname in filenames:
            path = cur_path / fname
            if policy is not None:
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    rel = path.as_posix()
                if policy.is_dependency_or_runtime(rel):
                    continue
            yield path


def _classify_path(path: Path, root: Path) -> tuple[SweepSeverity, str] | None:
    """Return (severity, category) for a noise path or ``None`` if clean."""

    if path.parent == root:
        for pattern in _ROOT_DIAGNOSTIC_GLOBS:
            if path.match(pattern):
                return SweepSeverity.BLOCK, "noise.scripts"
        for pattern in _ROOT_ARTIFACT_GLOBS:
            if path.match(pattern):
                return SweepSeverity.BLOCK, "noise.artifacts"
        for pattern in _ROOT_DOC_GLOBS:
            if path.match(pattern):
                # README.md is intentionally not matched by any glob.
                # Architecture/handoff docs at root are block-level only
                # if a ``docs/`` directory exists — otherwise the agent
                # had nowhere to put them and we just warn.
                if (root / "docs").is_dir():
                    return SweepSeverity.BLOCK, "noise.docs"
                return SweepSeverity.WARN, "noise.docs"

    # Path components relative to the workspace root (already filtered
    # by ``_iter_workspace_files`` to skip ``.git`` etc.).
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    rel_lower = [p.lower() for p in rel_parts]
    top = rel_lower[0] if rel_lower else ""

    if path.suffix.lower() == ".pyc" or "__pycache__" in rel_lower:
        return SweepSeverity.BLOCK, "noise.bytecode"

    # Diagnostic scripts under ``docs/`` are never legitimate (docs is
    # for Markdown/spec content, not code). Diagnostic scripts under
    # ``src/scripts/`` are also block-level: real CLIs keep their
    # functional name, ``check_*/probe_*/read_*`` etc. are leftover
    # debugging debris regardless of depth.
    if top in {"docs", "doc"} and path.suffix.lower() == ".py":
        return SweepSeverity.BLOCK, "noise.scripts"
    if len(rel_lower) >= 3 and top == "src" and rel_lower[1] == "scripts":
        for pattern in _DIAGNOSTIC_SCRIPT_GLOBS_RECURSIVE:
            if path.match(pattern):
                return SweepSeverity.BLOCK, "noise.scripts"
    if top == "src" and path.suffix.lower() == ".py":
        for pattern in _SRC_PACKAGE_DIAGNOSTIC_GLOBS:
            if path.match(pattern):
                return SweepSeverity.BLOCK, "noise.scripts"

    # Raw-extracted artefacts anywhere except memory/umbrella scratch.
    if top not in {".memory", ".umbrella", ".umbrella_scratch"}:
        for pattern in _ANY_DEPTH_ARTIFACT_GLOBS:
            if path.match(pattern):
                return SweepSeverity.BLOCK, "noise.artifacts"

    for pattern in _NOISE_GLOBS_WARN:
        if path.match(pattern):
            return SweepSeverity.WARN, "noise.legacy"
    return None


def _find_noise_files(
    root: Path, policy: WorkspacePathPolicy | None = None
) -> list[tuple[Path, SweepSeverity, str]]:
    matched: list[tuple[Path, SweepSeverity, str]] = []
    for path in _iter_workspace_files(root, policy=policy):
        classification = _classify_path(path, root)
        if classification is not None:
            sev, cat = classification
            matched.append((path, sev, cat))
    return matched


def _extract_section(task_md_text: str, header: str) -> str:
    match = re.search(
        rf"^##+\s*{re.escape(header)}\b.*?$(.*?)(?=^##+\s|\Z)",
        task_md_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _candidate_files_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for raw in _FILE_TOKEN_RE.findall(text):
        token = raw.strip().strip(".")
        if not token or token.startswith("/"):
            continue
        if token in _FILE_TOKEN_BLOCKLIST:
            continue
        if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", token):
            continue
        if token not in seen:
            seen.add(token)
            candidates.append(token)
    return candidates


def _strip_layout_inline_notes(line: str) -> str:
    text = re.split(r"\s+#", line, maxsplit=1)[0]
    while True:
        cleaned = re.sub(r"\([^()]*\)", "", text)
        if cleaned == text:
            break
        text = cleaned
    return text


def _candidate_files_from_project_layout(text: str) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for line in text.splitlines():
        cleaned = _strip_layout_inline_notes(line.strip())
        if not cleaned:
            continue
        for raw in _FILE_TOKEN_RE.findall(cleaned):
            token = raw.strip().strip(".")
            if not token or token.startswith("/"):
                continue
            if token in _FILE_TOKEN_BLOCKLIST:
                continue
            if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", token):
                continue
            if token not in seen:
                seen.add(token)
                candidates.append(token)
    return candidates


def is_runtime_optional_required_path(rel_path: str) -> bool:
    p = Path(str(rel_path).strip())
    if not p.name:
        return False
    if p.name.lower() in _ALWAYS_REQUIRED_FILENAMES:
        return False
    return p.suffix.lower() in _RUNTIME_OPTIONAL_EXTENSIONS


def parse_required_files(task_md_path: Path) -> list[str]:
    """Return files that ``TASK_MAIN.md`` explicitly lists as required."""
    if not task_md_path.exists() or not task_md_path.is_file():
        return []
    try:
        text = task_md_path.read_text(encoding="utf-8")
    except OSError:
        return []

    candidates = _candidate_files_from_project_layout(
        _extract_section(text, "Project Layout")
    )
    if not candidates:
        candidates = _candidate_files_from_text(
            _extract_section(text, "Definition of Done")
        )

    return [
        cand
        for cand in candidates
        if cand.count("/") <= 4 and not is_runtime_optional_required_path(cand)
    ]


def cleanup_noise_files(
    workspace_path: Path,
    *,
    auto_remove: bool = False,
) -> tuple[list[str], list[str]]:
    """Find noise files and (optionally) remove them. Returns (removed, leftover).

    Legacy two-tuple shape preserved for backwards-compat. Callers that
    need severity should use :func:`scan_noise_files` instead.
    """
    removed, leftover, _block, _warn = scan_noise_files(
        workspace_path, auto_remove=auto_remove
    )
    return removed, leftover


def scan_noise_files(
    workspace_path: Path,
    *,
    auto_remove: bool = False,
) -> tuple[list[str], list[str], list[NoiseHit], list[NoiseHit]]:
    """Return ``(removed, leftover, blocking_hits, warning_hits)``.

    ``blocking_hits``/``warning_hits`` describe everything the sweep
    found, regardless of whether ``auto_remove`` deleted it. This lets
    the caller surface a structured FAIL/WARN status that's separate
    from "was the file actually deleted from disk".
    """

    workspace_path = workspace_path.resolve()
    policy = WorkspacePathPolicy.load(workspace_path)
    removed: list[str] = []
    leftover: list[str] = []
    blocking_hits: list[NoiseHit] = []
    warning_hits: list[NoiseHit] = []
    for path, severity, category in _find_noise_files(workspace_path, policy=policy):
        try:
            rel = path.relative_to(workspace_path).as_posix()
        except ValueError:
            rel = path.as_posix()
        hit = NoiseHit(path=rel, severity=severity, category=category)
        if severity == SweepSeverity.BLOCK:
            blocking_hits.append(hit)
        else:
            warning_hits.append(hit)
        if not auto_remove:
            leftover.append(rel)
            continue
        try:
            path.unlink()
            removed.append(rel)
            log.info(
                "Final sweep removed noise file: %s (severity=%s, category=%s)",
                rel,
                severity.value,
                category,
            )
        except OSError as exc:
            log.warning("Failed to remove noise file %s: %s", rel, exc)
            leftover.append(rel)
    return removed, leftover, blocking_hits, warning_hits


def find_nested_workspace_duplicate(workspace_path: Path) -> Path | None:
    """Detect a self-nested ``<ws>/workspaces/<ws_name>/`` directory.

    The agent occasionally double-prefixes a path (``workspaces/<id>/foo``
    while already inside ``workspaces/<id>/``), creating a confusing
    duplicate tree.  Returns the duplicate path if it exists, else ``None``.
    """
    workspace_path = workspace_path.resolve()
    candidate = workspace_path / "workspaces" / workspace_path.name
    if candidate.is_dir():
        return candidate
    return None


def cleanup_nested_workspace_duplicate(
    workspace_path: Path,
    *,
    auto_remove: bool = False,
) -> str | None:
    """Remove a self-nested ``<ws>/workspaces/<ws>/`` tree if present.

    Returns the relative POSIX path of the removed/leftover duplicate, or
    ``None`` if no duplicate was found.
    """
    duplicate = find_nested_workspace_duplicate(workspace_path)
    if duplicate is None:
        return None
    try:
        rel = duplicate.relative_to(workspace_path.resolve()).as_posix()
    except ValueError:
        rel = duplicate.as_posix()
    if not auto_remove:
        return rel
    import shutil

    try:
        shutil.rmtree(duplicate)
        log.info("Final sweep removed nested workspace duplicate: %s", rel)
    except OSError as exc:
        log.warning("Failed to remove nested workspace duplicate %s: %s", rel, exc)
        return rel
    parent = duplicate.parent
    try:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return rel


def run_workspace_sweep(
    workspace_path: Path,
    *,
    auto_clean: bool = False,
    task_md_name: str = "TASK_MAIN.md",
) -> SweepReport:
    workspace_path = workspace_path.resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        return SweepReport(workspace_path=str(workspace_path), auto_clean=auto_clean)

    removed, leftover, blocking_hits, warning_hits = scan_noise_files(
        workspace_path, auto_remove=auto_clean
    )

    duplicate_rel = cleanup_nested_workspace_duplicate(
        workspace_path, auto_remove=auto_clean
    )
    if duplicate_rel:
        # Nested workspace duplicates are always block-level: they
        # silently confuse every downstream resolution path.
        blocking_hits.append(
            NoiseHit(
                path=duplicate_rel,
                severity=SweepSeverity.BLOCK,
                category="noise.duplicate_root",
            )
        )
        if auto_clean:
            removed.append(duplicate_rel)
        else:
            leftover.append(duplicate_rel)

    expected = parse_required_files(workspace_path / task_md_name)
    missing = [rel for rel in expected if not (workspace_path / rel).exists()]

    return SweepReport(
        workspace_path=str(workspace_path),
        removed=removed,
        leftover_noise=leftover,
        blocking_noise=blocking_hits,
        warning_noise=warning_hits,
        expected_files=expected,
        missing_required=missing,
        auto_clean=auto_clean,
    )


__all__ = [
    "NoiseHit",
    "SweepReport",
    "SweepSeverity",
    "SweepStatus",
    "cleanup_nested_workspace_duplicate",
    "cleanup_noise_files",
    "find_nested_workspace_duplicate",
    "is_runtime_optional_required_path",
    "parse_required_files",
    "run_workspace_sweep",
    "scan_noise_files",
]
