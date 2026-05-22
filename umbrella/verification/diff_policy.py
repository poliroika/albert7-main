"""Diff-aware anti-gaming checks for candidate workspace verification.

This module is the static, cheap gate.  It is intentionally separate from the
older source-policy scanner: source policy looks for mock scaffolds in source
files, while this gate focuses on evaluator/test tampering and weak proof
patterns that can make green tests meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from umbrella.analysis import analyze_jsts_test_source, analyze_python_test_source
from umbrella.verification.models import (
    VerificationStatus,
    VerificationStep,
    VerificationStepKind,
    VerificationStepResult,
)


@dataclass(frozen=True)
class DiffPolicyIssue:
    code: str
    message: str
    path: str = ""
    line: str = ""


_POLICY_PATHS = {
    "workspace.toml",
    "verification.toml",
    "verify.sh",
}
_POLICY_PREFIXES = ("policies/", ".policies/", "verification/", ".verification/")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_[^/]+\.py$)")
_ADDED_TAMPER_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "pytest_skip_or_xfail",
        re.compile(r"\bpytest\.(?:skip|xfail)\b|@pytest\.mark\.xfail\b"),
        "Tests must not be skipped/xfail'ed as proof of completion.",
    ),
    (
        "assert_true",
        re.compile(r"\bassert\s+(?:True|1)\b"),
        "Trivial constant assertions do not prove behavior.",
    ),
    (
        "weak_not_none_assertion",
        re.compile(r"\bassert\s+[^#\n]+(?:is\s+not\s+None|is\s+None)\b"),
        "`is None`/`is not None` assertions are too weak for behavioral proof.",
    ),
    (
        "early_return_in_test",
        re.compile(r"^\s*return(?:\s|$)"),
        "Early returns in tests can bypass assertions.",
    ),
    (
        "broad_try_except_pass",
        re.compile(r"except\s+(?:Exception|BaseException)?[^:\n]*:\s*(?:pass)?$"),
        "Broad exception swallowing in tests/probes hides failures.",
    ),
    (
        "target_behavior_mock",
        re.compile(r"\b(?:monkeypatch|mock|MagicMock|patch\(|mocker\.)\b"),
        "Mocking/monkeypatching target behavior is not acceptable proof.",
    ),
    (
        "snapshot_rewrite",
        re.compile(r"\b(?:update_snapshot|snapshot\.assert_match|--snapshot-update)\b"),
        "Snapshot rewrites need semantic assertions, not blind acceptance.",
    ),
)
_COMMAND_BYPASS_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "shell_always_true",
        re.compile(r"(\|\|\s*true\b|\bexit\s+0\b|\bset\s+\+e\b)"),
        "Shell success bypasses are forbidden in verification paths.",
    ),
    (
        "subprocess_check_false",
        re.compile(r"\bsubprocess\.run\([^)\n]*check\s*=\s*False"),
        "Verification subprocesses must fail loudly instead of using check=False.",
    ),
)
_ENV_BRANCH_RE = re.compile(
    r"\b(?:os\.getenv|os\.environ|process\.env)\b[^\n]*(?:CI|PYTEST_CURRENT_TEST|SKIP|MOCK|FAKE)",
    re.I,
)
_HARDCODED_RETURN_RE = re.compile(r"\breturn\s+(['\"])(?:ok|pass|success|done|expected|stub)\1", re.I)


def _norm(path: str | Path) -> str:
    value = str(path or "").replace("\\", "/").strip().strip("\"'`")
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(_norm(path).lower()))


def _is_js_test_path(path: str) -> bool:
    rel = _norm(path).lower()
    return rel.endswith((".test.js", ".test.jsx", ".test.ts", ".test.tsx", ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx"))


def _is_policy_path(path: str) -> bool:
    rel = _norm(path).lower()
    return rel in _POLICY_PATHS or any(rel.startswith(prefix) for prefix in _POLICY_PREFIXES)


def scan_unified_diff(
    diff_text: str,
    *,
    approved_policy_edits: bool = False,
) -> list[DiffPolicyIssue]:
    issues: list[DiffPolicyIssue] = []
    current_path = ""
    old_path_for_header = ""
    removed_asserts = 0
    added_asserts = 0
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("--- "):
            old_path_for_header = _norm(raw[4:].removeprefix("a/"))
            if _is_policy_path(old_path_for_header) and not approved_policy_edits:
                issues.append(
                    DiffPolicyIssue(
                        code="verifier_policy_changed",
                        path=old_path_for_header,
                        message=(
                            f"`{old_path_for_header}` controls verification/policy "
                            "and requires supervisor approval."
                        ),
                    )
                )
            continue
        if raw.startswith("+++ "):
            new_path = _norm(raw[4:].removeprefix("b/"))
            if raw.strip() == "+++ /dev/null" and _is_test_path(old_path_for_header):
                issues.append(
                    DiffPolicyIssue(
                        code="test_deleted",
                        path=old_path_for_header,
                        message=f"Test/probe file `{old_path_for_header}` was deleted.",
                    )
                )
            current_path = new_path
            if raw.strip() == "+++ /dev/null":
                current_path = ""
            if current_path and _is_policy_path(current_path) and not approved_policy_edits:
                issues.append(
                    DiffPolicyIssue(
                        code="verifier_policy_changed",
                        path=current_path,
                        message=(
                            f"`{current_path}` controls verification/policy and "
                            "requires supervisor approval."
                        ),
                    )
                )
            continue
        if not raw or raw.startswith("@@"):
            continue
        sign = raw[:1]
        line = raw[1:] if sign in {"+", "-"} else raw
        if sign == "-" and "assert" in line:
            removed_asserts += 1
        if sign == "+" and "assert" in line:
            added_asserts += 1
        if sign != "+":
            continue
        rel = current_path
        if _is_test_path(rel):
            for code, pattern, message in _ADDED_TAMPER_PATTERNS:
                if pattern.search(line):
                    issues.append(
                        DiffPolicyIssue(
                            code=code,
                            path=rel,
                            line=line.strip(),
                            message=message,
                        )
                    )
        for code, pattern, message in _COMMAND_BYPASS_PATTERNS:
            if pattern.search(line):
                issues.append(
                    DiffPolicyIssue(
                        code=code,
                        path=rel,
                        line=line.strip(),
                        message=message,
                    )
                )
        if _ENV_BRANCH_RE.search(line):
            issues.append(
                DiffPolicyIssue(
                    code="env_branch_bypass",
                    path=rel,
                    line=line.strip(),
                    message="Environment branches for CI/test/mock flags can hide failures.",
                )
            )
        if _HARDCODED_RETURN_RE.search(line):
            issues.append(
                DiffPolicyIssue(
                    code="hardcoded_stub_output",
                    path=rel,
                    line=line.strip(),
                    message="Hardcoded success/stub returns are suspicious proof paths.",
                )
            )
    if removed_asserts > added_asserts:
        issues.append(
            DiffPolicyIssue(
                code="assertions_weakened",
                message=(
                    f"Diff removed more assertions than it added "
                    f"({removed_asserts} removed, {added_asserts} added)."
                ),
            )
        )
    return _dedupe(issues)


def scan_workspace_files(
    workspace_path: str | Path,
    *,
    changed_files: Iterable[str] = (),
    approved_policy_edits: bool = False,
) -> list[DiffPolicyIssue]:
    root = Path(workspace_path).resolve()
    paths = [_norm(p) for p in changed_files if _norm(p)]
    if not paths:
        paths = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and ".memory" not in p.parts and ".git" not in p.parts
        ]
    issues: list[DiffPolicyIssue] = []
    for rel in paths:
        if _is_policy_path(rel) and not approved_policy_edits:
            issues.append(
                DiffPolicyIssue(
                    code="verifier_policy_changed",
                    path=rel,
                    message=(
                        f"`{rel}` controls verification/policy and requires "
                        "supervisor approval."
                    ),
                )
            )
        path = root / rel
        if not path.is_file() or path.stat().st_size > 512_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _is_test_path(rel) and rel.endswith(".py"):
            issues.extend(
                DiffPolicyIssue(
                    code=item.code,
                    path=item.path or rel,
                    line=item.snippet,
                    message=item.message,
                )
                for item in analyze_python_test_source(text, path=rel)
            )
        elif _is_js_test_path(rel):
            issues.extend(
                DiffPolicyIssue(
                    code=item.code,
                    path=item.path or rel,
                    line=item.snippet,
                    message=item.message,
                )
                for item in analyze_jsts_test_source(text, path=rel)
            )
        for line in text.splitlines():
            for code, pattern, message in _COMMAND_BYPASS_PATTERNS:
                if pattern.search(line):
                    issues.append(
                        DiffPolicyIssue(
                            code=code,
                            path=rel,
                            line=line.strip(),
                            message=message,
                        )
                    )
            if _ENV_BRANCH_RE.search(line):
                issues.append(
                    DiffPolicyIssue(
                        code="env_branch_bypass",
                        path=rel,
                        line=line.strip(),
                        message="Environment branches for CI/test/mock flags can hide failures.",
                    )
                )
            if _HARDCODED_RETURN_RE.search(line):
                issues.append(
                    DiffPolicyIssue(
                        code="hardcoded_stub_output",
                        path=rel,
                        line=line.strip(),
                        message="Hardcoded success/stub returns are suspicious proof paths.",
                    )
                )
    return _dedupe(issues)


def _dedupe(issues: Iterable[DiffPolicyIssue]) -> list[DiffPolicyIssue]:
    seen: set[tuple[str, str, str]] = set()
    out: list[DiffPolicyIssue] = []
    for issue in issues:
        key = (issue.code, issue.path, issue.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def run_diff_policy_guard(
    workspace_path: str | Path,
    *,
    changed_files: Iterable[str] = (),
    approved_policy_edits: bool = False,
) -> VerificationStepResult:
    step = VerificationStep(
        kind=VerificationStepKind.SOURCE_POLICY,
        name="diff_policy:anti_tamper",
        optional=False,
    )
    issues = scan_workspace_files(
        workspace_path,
        changed_files=changed_files,
        approved_policy_edits=approved_policy_edits,
    )
    if not issues:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="diff_policy: no test/verifier tampering patterns detected",
        )
    summary = "\n".join(
        f"{issue.code}: {issue.path or '-'} {issue.message}" for issue in issues[:20]
    )
    return VerificationStepResult(
        step=step,
        status=VerificationStatus.FAILED,
        summary=summary,
        error="diff_policy_anti_tamper_failed",
    )


__all__ = [
    "DiffPolicyIssue",
    "run_diff_policy_guard",
    "scan_unified_diff",
    "scan_workspace_files",
]
