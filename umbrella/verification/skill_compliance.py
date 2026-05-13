"""Verification gate that enforces detected-skill compliance.

When Umbrella's skill detector marks a workspace as ``multi_agent_gmas``,
the implementation is expected to actually use the in-repo ``gmas``
library. This module runs after the user-declared verification steps
and adds a synthetic ``skill_compliance`` step that fails the run if
the workspace contains no ``gmas`` imports despite the skill being
active.

The check is intentionally conservative:

* Skips if no skills are detected for the workspace.
* Skips for skills that have no compliance contract.
* Reports ``skill_compliance`` as ``passed`` when at least one
  ``import gmas`` / ``from gmas`` statement is found in the workspace.
* Returns ``failed`` with a list of inspected files when nothing
  matches.

Pure stdlib so it can be imported from :mod:`umbrella.verification.runner`
without bringing in heavy optional deps.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from umbrella.verification.models import (
    VerificationStatus,
    VerificationStep,
    VerificationStepKind,
    VerificationStepResult,
)

log = logging.getLogger(__name__)

_GMAS_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+gmas(?:\.[a-zA-Z0-9_\.]+)?\s+import\b|import\s+gmas(?:\b|\.))",
    re.MULTILINE,
)
_MOCK_SCAFFOLD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mock helper", re.compile(r"\bdef\s+mock_[a-z0-9_]+\s*\(", re.IGNORECASE)),
    (
        "mock content function",
        re.compile(r"\bdef\s+_?create_mock_[a-z0-9_]*\s*\(", re.IGNORECASE),
    ),
    ("mocked response", re.compile(r"mocked response", re.IGNORECASE)),
    ("example thesis", re.compile(r"example thesis", re.IGNORECASE)),
    ("thesis placeholder", re.compile(r"\bThesis\s+(?:\d+|\{idx\})\b", re.IGNORECASE)),
    # Tighter than "in a real scenario": catches the canonical mock hedge
    # ("in a real X, you/we/this would...") without flagging honest design
    # comments like "in a real scenario the Assembler ensures this".
    (
        "real-scenario placeholder",
        re.compile(
            r"in\s+a\s+real\s+(?:scenario|app|implementation|system|production|world|setup)[\s,]+"
            r"(?:you|we|this|it|the\s+system)\s+would",
            re.IGNORECASE,
        ),
    ),
    (
        "placeholder return",
        re.compile(
            r"#\s*placeholder[:\s].{0,80}?(?:actual|real|api|llm|implementation)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "production placeholder",
        re.compile(
            r"\bplaceholder\b[\s\S]{0,160}\b(?:production|llm|actual|real|replace|replaced|implementation)\b"
            r"|\b(?:production|llm|actual|real|replace|replaced|implementation)\b[\s\S]{0,160}\bplaceholder\b",
            re.IGNORECASE,
        ),
    ),
    (
        "llm replacement placeholder",
        re.compile(
            r"(?:would|will)\s+(?:be\s+)?(?:use|replaced\s+with|call|integrate)\s+(?:an?\s+)?LLM",
            re.IGNORECASE,
        ),
    ),
    (
        "stub content",
        re.compile(
            r"\b(?:create|created|generate|generated|build|built)\s+\w*\s*stub\s+(?:card|cards|content|response)"
            r"|\bstub\s+(?:card|cards|content|response)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pending placeholder output",
        re.compile(r"Key impact analysis pending|No content provided", re.IGNORECASE),
    ),
    (
        "hardcoded demo card",
        re.compile(r"#\s*(?:dummy|fake|stub)\s+(?:data|card|response)", re.IGNORECASE),
    ),
    (
        "exception fallback cards",
        re.compile(
            r"except\s+Exception[\s\S]{0,600}?return\s*\{[^}]*['\"]cards['\"]\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        "json-parse fallback",
        re.compile(r"JSON parse error|Pipeline failed", re.IGNORECASE),
    ),
    (
        "hardcoded news card text",
        re.compile(r"AI News|Market Trends|Future Outlook", re.IGNORECASE),
    ),
    (
        "simulated e2e sleep terminate",
        re.compile(
            r"time\.sleep\(\s*\d+\s*\)[\s\S]{0,300}?terminate\(\)", re.IGNORECASE
        ),
    ),
)
_GMAS_FALLBACK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gmas availability flag", re.compile(r"\bGMAS_AVAILABLE\s*=\s*False\b")),
    ("object fallback", re.compile(r"\bBaseTool\s*=\s*object\b")),
    (
        "silent gmas fallback",
        re.compile(r"GMAS\s+not\s+available|fallback\s+BaseTool", re.IGNORECASE),
    ),
    (
        "optional gmas import",
        re.compile(
            r"try:\s*(?:\n|\r\n)[\s\S]{0,400}?(?:from\s+gmas|import\s+gmas)"
            r"[\s\S]{0,400}?except\s+ImportError",
            re.IGNORECASE,
        ),
    ),
)

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".memory",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "data",
    }
)

_DIAGNOSTIC_OR_META_FILE_RE = re.compile(
    r"^(?:analyze|check|debug|diagnose|extract|find|inspect|probe|scan|scratch|"
    r"search|verify|validate|fix|real_test|test_minimal)_.+\.py$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SkillCheck:
    """Single skill->compliance contract."""

    domain_id: str
    summary_label: str

    def make_step(self) -> VerificationStep:
        return VerificationStep(
            kind=VerificationStepKind.IMPORT_CHECK,
            name=f"skill_compliance:{self.domain_id}",
            optional=False,
        )


_SKILL_CHECKS: dict[str, SkillCheck] = {
    "multi_agent_gmas": SkillCheck(
        domain_id="multi_agent_gmas",
        summary_label="GMAS import",
    ),
}


def _iter_python_files(workspace_path: Path) -> list[Path]:
    files: list[Path] = []
    if not workspace_path.exists():
        return files
    for child in workspace_path.rglob("*.py"):
        rel_parts = child.relative_to(workspace_path).parts
        if any(part in _SKIP_DIR_NAMES for part in rel_parts):
            continue
        files.append(child)
    return files


def _file_imports_gmas(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_GMAS_IMPORT_RE.search(text))


def _iter_app_python_files(workspace_path: Path) -> list[Path]:
    files: list[Path] = []
    for path in _iter_python_files(workspace_path):
        rel = path.relative_to(workspace_path).as_posix().lower()
        name = path.name.lower()
        if (
            "/tests/" in f"/{rel}/"
            or name.startswith("test_")
            or name.endswith("_test.py")
        ):
            continue
        if _DIAGNOSTIC_OR_META_FILE_RE.match(name):
            continue
        if rel.startswith("docs/") or rel.startswith("doc/"):
            continue
        files.append(path)
    return files


def evaluate_gmas_compliance(workspace_path: Path) -> tuple[bool, str, list[str]]:
    """Return ``(passed, summary, evidence_files)`` for the GMAS import check.

    ``evidence_files`` is the list of paths (relative to ``workspace_path``)
    that satisfied the contract on a pass, or the list of inspected files
    on a failure.
    """
    py_files = _iter_python_files(workspace_path)
    if not py_files:
        return (
            False,
            "No Python files in workspace; multi_agent_gmas requires a "
            "gmas-based implementation.",
            [],
        )
    app_files = _iter_app_python_files(workspace_path)
    if not app_files:
        inspected = [p.relative_to(workspace_path).as_posix() for p in py_files]
        sample = ", ".join(inspected[:8])
        more = "" if len(inspected) <= 8 else f" (+{len(inspected) - 8} more)"
        return (
            False,
            "No application Python files eligible for GMAS compliance; "
            "diagnostic scripts, docs and tests do not satisfy "
            "multi_agent_gmas. Inspected: "
            f"{sample}{more}.",
            inspected,
        )
    matches: list[str] = []
    for path in app_files:
        if _file_imports_gmas(path):
            matches.append(path.relative_to(workspace_path).as_posix())
    if matches:
        sample = ", ".join(matches[:5])
        more = "" if len(matches) <= 5 else f" (+{len(matches) - 5} more)"
        summary = f"Found gmas imports in {len(matches)} file(s): {sample}{more}"
        return True, summary, matches
    inspected = [p.relative_to(workspace_path).as_posix() for p in app_files]
    sample = ", ".join(inspected[:8])
    more = "" if len(inspected) <= 8 else f" (+{len(inspected) - 8} more)"
    summary = (
        "Skill 'multi_agent_gmas' is active but no `import gmas` / "
        "`from gmas import ...` was found in the workspace. Inspected: "
        f"{sample}{more}. Either build the implementation on the in-repo "
        "`gmas/` library (use `get_gmas_context` for the API), or record "
        "an explicit blocker that prevents using GMAS for this task."
    )
    return False, summary, inspected


def evaluate_no_mock_scaffold(workspace_path: Path) -> tuple[bool, str]:
    """Fail when app code still contains obvious mock/stub scaffolding."""

    hits: list[str] = []
    for path in _iter_app_python_files(workspace_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pattern in _MOCK_SCAFFOLD_PATTERNS:
            if pattern.search(text):
                rel = path.relative_to(workspace_path).as_posix()
                hits.append(f"{rel} ({label})")
                break

    if not hits:
        return True, "No obvious mock/scaffold markers found in application code."

    sample = ", ".join(hits[:6])
    more = "" if len(hits) <= 6 else f" (+{len(hits) - 6} more)"
    return (
        False,
        "Application code still contains mock/scaffold markers that are not "
        f"acceptable for a verified GMAS delivery: {sample}{more}",
    )


def evaluate_no_gmas_fallback(workspace_path: Path) -> tuple[bool, str]:
    """Fail when GMAS-active app code can silently degrade to non-GMAS stubs."""

    hits: list[str] = []
    for path in _iter_app_python_files(workspace_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _GMAS_IMPORT_RE.search(text):
            continue
        for label, pattern in _GMAS_FALLBACK_PATTERNS:
            if pattern.search(text):
                rel = path.relative_to(workspace_path).as_posix()
                hits.append(f"{rel} ({label})")
                break

    if not hits:
        return True, "No silent GMAS fallback paths found in application code."

    sample = ", ".join(hits[:6])
    more = "" if len(hits) <= 6 else f" (+{len(hits) - 6} more)"
    return (
        False,
        "GMAS is active, but application code can silently fall back to "
        f"non-GMAS stubs: {sample}{more}. Use real `gmas.*` APIs or fail "
        "loudly with an explicit blocker.",
    )


def build_skill_compliance_results(
    workspace_path: Path, detected_domains: set[str]
) -> list[VerificationStepResult]:
    """Build synthetic verification results for active skill contracts."""

    results: list[VerificationStepResult] = []
    if "multi_agent_gmas" not in detected_domains:
        return results

    check = _SKILL_CHECKS["multi_agent_gmas"]
    passed, summary, evidence_files = evaluate_gmas_compliance(workspace_path)
    results.append(
        VerificationStepResult(
            step=check.make_step(),
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            summary=summary,
            stdout="\n".join(evidence_files),
            error="" if passed else "missing_gmas_import",
        )
    )

    no_fallback_passed, no_fallback_summary = evaluate_no_gmas_fallback(workspace_path)
    results.append(
        VerificationStepResult(
            step=VerificationStep(
                kind=VerificationStepKind.IMPORT_CHECK,
                name="skill_quality:multi_agent_gmas_no_fallback",
                optional=False,
            ),
            status=VerificationStatus.PASSED
            if no_fallback_passed
            else VerificationStatus.FAILED,
            summary=no_fallback_summary,
            error="" if no_fallback_passed else "gmas_fallback_stub_present",
        )
    )

    no_mock_passed, no_mock_summary = evaluate_no_mock_scaffold(workspace_path)
    results.append(
        VerificationStepResult(
            step=VerificationStep(
                kind=VerificationStepKind.IMPORT_CHECK,
                name="skill_quality:multi_agent_gmas_no_mock_scaffold",
                optional=False,
            ),
            status=VerificationStatus.PASSED
            if no_mock_passed
            else VerificationStatus.FAILED,
            summary=no_mock_summary,
            error="" if no_mock_passed else "mock_scaffold_present",
        )
    )
    return results
