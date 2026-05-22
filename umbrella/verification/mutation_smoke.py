"""Minimal mutation smoke tests for changed Python production files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import time
from typing import Iterable

from umbrella.verification.models import (
    VerificationStatus,
    VerificationStep,
    VerificationStepKind,
    VerificationStepResult,
)


@dataclass(frozen=True)
class MutationCandidate:
    path: str
    original: str
    mutated: str
    label: str


_MUTATIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("gt_to_gte", re.compile(r"(?<![<>=!])>(?!=)"), ">="),
    ("lt_to_lte", re.compile(r"(?<![<>=!])<(?!=)"), "<="),
    ("and_to_or", re.compile(r"\band\b"), "or"),
    ("or_to_and", re.compile(r"\bor\b"), "and"),
    ("true_to_false", re.compile(r"\bTrue\b"), "False"),
    ("false_to_true", re.compile(r"\bFalse\b"), "True"),
)


def _norm(path: str | Path) -> str:
    value = str(path or "").replace("\\", "/").strip().strip("\"'`")
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/")


def _eligible_files(workspace_path: Path, changed_files: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for raw in changed_files:
        rel = _norm(raw)
        if not rel.endswith(".py"):
            continue
        parts = [part.lower() for part in rel.split("/") if part]
        if not parts or parts[0] in {"tests", "test", "docs", "doc", ".memory"}:
            continue
        path = workspace_path / rel
        if path.is_file() and path.stat().st_size <= 256_000:
            out.append(path)
    return out


def _first_mutation(path: Path, root: Path) -> MutationCandidate | None:
    source = path.read_text(encoding="utf-8", errors="replace")
    for label, pattern, replacement in _MUTATIONS:
        mutated, count = pattern.subn(replacement, source, count=1)
        if count and mutated != source:
            return MutationCandidate(
                path=path.relative_to(root).as_posix(),
                original=source,
                mutated=mutated,
                label=label,
            )
    return None


def run_mutation_smoke_guard(
    workspace_path: str | Path,
    *,
    changed_files: Iterable[str],
    python_cmd: list[str],
    env: dict[str, str],
    timeout_seconds: int = 120,
    max_mutants: int = 3,
) -> VerificationStepResult:
    step = VerificationStep(
        kind=VerificationStepKind.SOURCE_POLICY,
        name="mutation_smoke:changed_python",
        optional=False,
    )
    root = Path(workspace_path).resolve()
    if not (root / "tests").exists():
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="mutation_smoke: no tests/ directory; test_quality_guard owns this failure",
        )
    candidates: list[MutationCandidate] = []
    for path in _eligible_files(root, changed_files):
        candidate = _first_mutation(path, root)
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= max_mutants:
            break
    if not candidates:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="mutation_smoke: no simple Python mutants available",
        )

    survived: list[str] = []
    killed: list[str] = []
    started = time.time()
    for candidate in candidates:
        path = root / candidate.path
        try:
            path.write_text(candidate.mutated, encoding="utf-8")
            proc = subprocess.run(
                [*python_cmd, "-m", "pytest", "-q"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(10, int(timeout_seconds)),
                env=env,
                check=False,
            )
            label = f"{candidate.path}:{candidate.label}"
            if proc.returncode == 0:
                survived.append(label)
            else:
                killed.append(label)
        except Exception as exc:  # noqa: BLE001 - verifier boundary
            killed.append(f"{candidate.path}:{candidate.label}:{type(exc).__name__}")
        finally:
            path.write_text(candidate.original, encoding="utf-8")

    summary = (
        f"mutation_smoke: killed {len(killed)}/{len(candidates)} simple mutant(s); "
        f"survived={survived[:5]}"
    )
    if survived:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.FAILED,
            duration_seconds=time.time() - started,
            summary=summary,
            error=(
                "mutation_survived: tests did not fail after simple logic mutation(s): "
                + ", ".join(survived[:5])
            ),
        )
    return VerificationStepResult(
        step=step,
        status=VerificationStatus.PASSED,
        duration_seconds=time.time() - started,
        summary=summary,
    )


__all__ = ["MutationCandidate", "run_mutation_smoke_guard"]
