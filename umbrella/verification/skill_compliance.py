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
* Reports ``skill_compliance`` as ``passed`` when at least one application
  file imports ``gmas``.
* Adds a runtime import check for the selected verification interpreter so
  a shadow package named ``gmas`` cannot satisfy compliance accidentally.
* Returns ``failed`` with a list of inspected files when nothing
  matches.

Pure stdlib so it can be imported from :mod:`umbrella.verification.runner`
without bringing in heavy optional deps.
"""

import logging
import json
import re
import subprocess
import time
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
        "compliance-only skill import",
        re.compile(
            r"\b(?:import\s+gmas|from\s+gmas|gmas|llm|multi[_-]?agent)\b"
            r"[\s\S]{0,180}\b(?:satisfy|pass|appease|silence)\b"
            r"[\s\S]{0,120}\b(?:skill|compliance|import[_-]?check|quality|check|requirement)\b"
            r"|\b(?:satisfy|pass|appease|silence)\b"
            r"[\s\S]{0,120}\b(?:skill|compliance|import[_-]?check|quality|check|requirement)\b"
            r"[\s\S]{0,180}\b(?:gmas|llm|multi[_-]?agent)\b",
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
    (
        "llm decision fallback",
        re.compile(
            r"\b(?:fallback|fall[-\s]?back)\b[\s\S]{0,300}"
            r"\b(?:positive|negative)[_\s-]*(?:words?|count|sentiment)\b|"
            r"\b(?:positive_count|negative_count|positive_words|negative_words)\b"
            r"[\s\S]{0,500}\b(?:return\s+(?:True|False)|accept|reject|decision)\b",
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

_GMAS_RUNTIME_IMPORT_SOURCE = r"""
from pathlib import Path
import importlib.metadata as metadata
import sys

import gmas
from gmas.builder import GraphBuilder
from gmas.execution import MACPRunner

gmas_file = Path(getattr(gmas, "__file__", "")).resolve()
expected_source = Path(sys.argv[1]).resolve()

if expected_source.exists():
    try:
        gmas_file.relative_to(expected_source)
    except ValueError:
        raise SystemExit(
            f"wrong_gmas_package: imported {gmas_file}, expected local source under {expected_source}"
        )
else:
    dists = set(metadata.packages_distributions().get("gmas", []))
    if "frontier-ai-gmas" not in dists:
        raise SystemExit(
            "wrong_gmas_distribution: import namespace 'gmas' is not provided by frontier-ai-gmas"
        )

print(f"gmas runtime ok: {gmas_file}")
print(f"GraphBuilder={GraphBuilder.__module__}.{GraphBuilder.__name__}")
print(f"MACPRunner={MACPRunner.__module__}.{MACPRunner.__name__}")
""".strip()

_GMAS_APP_IMPORT_SOURCE = r"""
from pathlib import Path
import importlib
import importlib.util
import json
import os
import sys

workspace = Path(sys.argv[1]).resolve()
files = json.loads(os.environ.get("UMBRELLA_GMAS_IMPORT_FILES", "[]"))
sys.path.insert(0, str(workspace))
src_root = workspace / "src"
if src_root.exists():
    sys.path.insert(0, str(src_root))


def dotted_module_name(root, raw_parts):
    if not raw_parts:
        return None
    module_parts = raw_parts[:-1] if raw_parts[-1] == "__init__" else raw_parts
    if not module_parts:
        return None
    if len(module_parts) == 1:
        return module_parts[0]
    cursor = root
    package_parts = []
    for part in module_parts[:-1]:
        cursor = cursor / part
        if not (cursor / "__init__.py").exists():
            return None
        package_parts.append(part)
    return ".".join([*package_parts, module_parts[-1]])


def module_name_for(rel_path):
    raw_parts = Path(rel_path).with_suffix("").parts
    if not raw_parts:
        return None
    candidates = []
    if len(raw_parts) > 1 and raw_parts[0] == "src" and src_root.exists():
        candidates.append((src_root, raw_parts[1:]))
    candidates.append((workspace, raw_parts))
    for root, parts in candidates:
        module_name = dotted_module_name(root, parts)
        if module_name:
            return module_name
    return None


failures = []
for idx, rel in enumerate(files):
    path = (workspace / rel).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        failures.append(f"{rel}: path escapes workspace")
        continue
    if not path.exists():
        failures.append(f"{rel}: file is missing")
        continue
    try:
        module_name = module_name_for(rel)
        if module_name:
            importlib.import_module(module_name)
        else:
            spec = importlib.util.spec_from_file_location(
                f"_umbrella_gmas_import_check_{idx}", path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot build import spec for {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except BaseException as exc:
        failures.append(f"{rel}: {type(exc).__name__}: {exc}")

if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)

print("gmas application imports ok: " + ", ".join(files))
""".strip()


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


def _repo_gmas_source_dir(workspace_path: Path) -> Path:
    """Return the local ``gmas`` source package when this is an Umbrella repo."""

    resolved = workspace_path.resolve()
    for parent in (resolved, *resolved.parents):
        candidate = parent / "gmas" / "src" / "gmas"
        if candidate.exists():
            return candidate.resolve()
    return Path()


def evaluate_gmas_runtime_import(
    workspace_path: Path,
    python_cmd: list[str],
    env: dict[str, str] | None = None,
) -> VerificationStepResult:
    """Verify the selected interpreter imports the real GMAS runtime."""

    step = VerificationStep(
        kind=VerificationStepKind.IMPORT_CHECK,
        name="skill_runtime:multi_agent_gmas_importable",
        optional=False,
    )
    started = time.time()
    expected_source = _repo_gmas_source_dir(workspace_path)
    cmd = [
        *python_cmd,
        "-c",
        _GMAS_RUNTIME_IMPORT_SOURCE,
        str(expected_source),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace_path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.ERROR,
            duration_seconds=time.time() - started,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            summary="Timed out while importing GMAS runtime with the workspace interpreter.",
            error="gmas_runtime_import_timeout",
        )
    except Exception as exc:  # noqa: BLE001 - defensive verification boundary
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.ERROR,
            duration_seconds=time.time() - started,
            summary=f"GMAS runtime import check crashed: {type(exc).__name__}",
            error=f"{type(exc).__name__}: {exc}",
        )

    passed = proc.returncode == 0
    if passed:
        summary = "Selected workspace interpreter imports frontier-ai-gmas successfully."
    else:
        summary = (
            "Selected workspace interpreter cannot import the required "
            "`frontier-ai-gmas` runtime (`gmas.builder.GraphBuilder` and "
            "`gmas.execution.MACPRunner`)."
        )
    return VerificationStepResult(
        step=step,
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        exit_code=proc.returncode,
        duration_seconds=time.time() - started,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        summary=summary,
        error="" if passed else "gmas_runtime_import_failed",
    )


def evaluate_gmas_application_imports(
    workspace_path: Path,
    evidence_files: list[str],
    python_cmd: list[str],
    env: dict[str, str] | None = None,
) -> VerificationStepResult:
    """Import the application modules that claim GMAS usage."""

    step = VerificationStep(
        kind=VerificationStepKind.IMPORT_CHECK,
        name="skill_runtime:multi_agent_gmas_app_imports",
        optional=False,
    )
    started = time.time()
    step_env = dict(env or {})
    step_env["UMBRELLA_GMAS_IMPORT_FILES"] = json.dumps(evidence_files)
    cmd = [*python_cmd, "-c", _GMAS_APP_IMPORT_SOURCE, str(workspace_path.resolve())]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace_path),
            env=step_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.ERROR,
            duration_seconds=time.time() - started,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            summary="Timed out while importing GMAS application modules.",
            error="gmas_application_import_timeout",
        )
    except Exception as exc:  # noqa: BLE001 - defensive verification boundary
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.ERROR,
            duration_seconds=time.time() - started,
            summary=f"GMAS application import check crashed: {type(exc).__name__}",
            error=f"{type(exc).__name__}: {exc}",
        )

    sample = ", ".join(evidence_files[:5])
    more = "" if len(evidence_files) <= 5 else f" (+{len(evidence_files) - 5} more)"
    passed = proc.returncode == 0
    return VerificationStepResult(
        step=step,
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        exit_code=proc.returncode,
        duration_seconds=time.time() - started,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        summary=(
            f"GMAS application modules import successfully: {sample}{more}"
            if passed
            else "At least one application module that imports GMAS cannot be "
            f"imported with the workspace interpreter: {sample}{more}"
        ),
        error="" if passed else "gmas_application_import_failed",
    )


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
    """Fail when GMAS-active app code can silently degrade to stubs or heuristics."""

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
        f"non-GMAS stubs or heuristic LLM decisions: {sample}{more}. Use real "
        "`gmas.*` APIs and structured LLM outputs, or fail loudly with an "
        "explicit blocker.",
    )


def build_skill_compliance_results(
    workspace_path: Path,
    detected_domains: set[str],
    *,
    python_cmd: list[str] | None = None,
    env: dict[str, str] | None = None,
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

    if python_cmd:
        results.append(evaluate_gmas_runtime_import(workspace_path, python_cmd, env))
        if passed and evidence_files:
            results.append(
                evaluate_gmas_application_imports(
                    workspace_path,
                    evidence_files,
                    python_cmd,
                    env,
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
