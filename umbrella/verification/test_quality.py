"""Static guard against trivial test files in workspaces.

Without this guard an agent under deadline pressure can satisfy the
verification gate by replacing meaningful tests with ``assert True`` or
``assert response is not None`` and call it a day. We walk every
``test_*.py`` in the workspace, classify each top-level ``test_*``
function as either *trivial* or *substantive*, and fail the verification
report when a workspace ships predominantly trivial tests OR when a
web-shaped workspace has zero tests that exercise an HTTP client.

The guard is intentionally conservative — it returns ``PASSED`` when
there is nothing to look at (no tests, no Python files) so that pure-doc
workspaces are not punished.

Knobs (declared on the workspace ``[verification]`` table):

- ``skip_test_quality = true`` opts out entirely.
- ``enforce_test_quality = true`` forces this guard on even if a workspace
  template tried to skip it.
"""

import ast
import logging
from pathlib import Path

from umbrella.verification.models import (
    VerificationStatus,
    VerificationStep,
    VerificationStepKind,
    VerificationStepResult,
)

log = logging.getLogger(__name__)


# A function is "trivial" when it does nothing but a constant assertion or
# a very weak existence check. We err on the side of marking checks as
# substantive so we do not penalise creative test styles.
_TRIVIAL_RESERVED_BOOLS = {"True"}
_HTTP_HINTS = (
    "requests",
    "httpx",
    "urllib",
    "TestClient",
    "test_client",
    "fastapi.testclient",
    "starlette.testclient",
    "client.get",
    "client.post",
    "Client(",
)
_WEB_FILE_HINTS = (
    "web_server.py",
    "app.py",
    "server.py",
    "main.py",
)


def _iter_test_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("test_*.py"):
        # Skip __pycache__, virtualenvs, vendored test fixtures, etc.
        rel = p.relative_to(root).parts
        if any(
            part in {"__pycache__", ".venv", "venv", "node_modules", ".memory", ".git"}
            for part in rel
        ):
            continue
        files.append(p)
    return sorted(files)


def _is_trivial_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value) is True or node.value is None or node.value == ""
    if isinstance(node, ast.Name) and node.id in _TRIVIAL_RESERVED_BOOLS:
        return True
    return False


def _is_trivial_assert(stmt: ast.Assert) -> bool:
    test = stmt.test
    if _is_trivial_constant(test):
        return True
    # ``assert x is not None`` / ``assert x is None`` — extremely weak.
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        comparator = test.comparators[0]
        if (
            isinstance(op, (ast.Is, ast.IsNot))
            and isinstance(comparator, ast.Constant)
            and comparator.value is None
        ):
            return True
    # ``assert response`` (truthiness) without any further check is also
    # mostly noise.
    if isinstance(test, ast.Name):
        return True
    return False


def _call_name(call: ast.Call) -> str:
    node: ast.AST = call.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _call_has_keyword_true(call: ast.Call, keyword_name: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg == keyword_name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value is True
    return False


def _is_assertion_shaped_call(call: ast.Call) -> bool:
    name = _call_name(call)
    if not name:
        return False
    leaf = name.rsplit(".", 1)[-1]
    if name.endswith("pytest.raises") or leaf == "raises":
        return True
    if name.endswith("pytest.fail") or leaf == "fail":
        return True
    if leaf.startswith("assert") or ".assert" in name:
        return True
    if leaf in {"check_call", "check_output"}:
        return True
    if leaf == "run" and _call_has_keyword_true(call, "check"):
        return True
    return False


def _has_assertion_shaped_call(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call) and _is_assertion_shaped_call(node)
        for node in ast.walk(func)
    )


def _is_domain_call(call: ast.Call) -> bool:
    name = _call_name(call)
    if not name:
        return False
    leaf = name.rsplit(".", 1)[-1]
    if _is_assertion_shaped_call(call):
        return True
    if leaf in {
        "print",
        "repr",
        "str",
        "len",
        "bool",
        "isinstance",
        "hasattr",
        "getattr",
    }:
        return False
    return True


def _has_behavioral_evidence(
    func: ast.FunctionDef | ast.AsyncFunctionDef, source: str
) -> bool:
    if _file_uses_http_client(source):
        return True
    saw_domain_call = False
    saw_strong_assert = False
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _is_domain_call(node):
            saw_domain_call = True
        if isinstance(node, ast.Assert) and not _is_trivial_assert(node):
            saw_strong_assert = True
        if isinstance(node, ast.Call) and _is_assertion_shaped_call(node):
            return True
    return saw_domain_call and saw_strong_assert


def _swallows_exception_without_failure(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            has_failure = any(
                isinstance(inner, (ast.Raise, ast.Assert))
                or (isinstance(inner, ast.Call) and _is_assertion_shaped_call(inner))
                for inner in ast.walk(handler)
            )
            if not has_failure:
                return True
    return False


def _function_is_trivial(
    func: ast.FunctionDef | ast.AsyncFunctionDef, source: str
) -> bool:
    """A test is trivial when *every* statement is a no-op or a weak assert."""
    asserts: list[ast.Assert] = []
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assert):
            asserts.append(stmt)

    body = func.body
    if not body:
        # ``def test_x(): ...`` (literal Ellipsis) or empty after pass.
        return True

    only_pass_or_doc = all(
        isinstance(stmt, (ast.Pass,))
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in body
    )
    if only_pass_or_doc:
        return True

    if not asserts:
        # Effective no-assert tests must still fail loudly through
        # pytest.raises, unittest-style assert calls, or subprocess
        # check=True/check_call.
        if _swallows_exception_without_failure(func):
            return True
        return not _has_assertion_shaped_call(func)

    return all(_is_trivial_assert(a) for a in asserts)


def _file_uses_http_client(source: str) -> bool:
    return any(hint in source for hint in _HTTP_HINTS)


def _workspace_is_web_shaped(root: Path) -> bool:
    for hint in _WEB_FILE_HINTS:
        if (root / hint).exists():
            return True
    return False


def _empty_synthetic_step() -> VerificationStep:
    return VerificationStep(
        kind=VerificationStepKind.SHELL,
        name="test_quality_guard",
        command=[],
        timeout_seconds=0,
        optional=False,
    )


def run_test_quality_guard(workspace_path: str | Path) -> VerificationStepResult:
    """Walk ``workspace_path`` and report on test depth.

    Always returns a single :class:`VerificationStepResult`. The guard fails
    only when we find tests AND those tests are dominated by trivial
    asserts.  No tests at all is not considered a failure here because the
    surrounding pytest step would have caught a missing test file.
    """

    root = Path(workspace_path).resolve()
    step = _empty_synthetic_step()

    test_files = _iter_test_files(root)
    if not test_files:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="test_quality_guard: no test_*.py files found, nothing to inspect",
        )

    total_tests = 0
    trivial_tests = 0
    behavioral_tests = 0
    files_with_http: int = 0
    per_file_breakdown: list[str] = []
    misplaced_test_files: list[str] = []

    for path in test_files:
        rel_path = path.relative_to(root)
        if rel_path.parts and rel_path.parts[0] == "src":
            misplaced_test_files.append(rel_path.as_posix())
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("test_quality_guard: cannot read %s: %s", path, exc)
            continue

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            per_file_breakdown.append(
                f"{path.name}: SyntaxError ({exc.msg}) — counted as trivial"
            )
            total_tests += 1
            trivial_tests += 1
            continue

        file_total = 0
        file_trivial = 0
        file_behavioral = 0
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                file_total += 1
                if _function_is_trivial(node, source):
                    file_trivial += 1
                elif _has_behavioral_evidence(node, source):
                    file_behavioral += 1
        total_tests += file_total
        trivial_tests += file_trivial
        behavioral_tests += file_behavioral

        if _file_uses_http_client(source):
            files_with_http += 1

        if file_total:
            per_file_breakdown.append(
                f"{rel_path.as_posix()}: {file_total - file_trivial}/{file_total} substantive, "
                f"{file_behavioral}/{file_total} behavioral"
            )

    if total_tests == 0:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="test_quality_guard: test files contain no `test_*` functions",
        )

    trivial_ratio = trivial_tests / total_tests
    is_web = _workspace_is_web_shaped(root)
    web_needs_http = is_web and files_with_http == 0

    summary_lines: list[str] = [
        f"test_quality_guard: {total_tests - trivial_tests}/{total_tests} substantive tests "
        f"across {len(test_files)} file(s) (trivial ratio {trivial_ratio:.2f}); "
        f"{behavioral_tests}/{total_tests} behavioral workflow/output tests",
    ]
    if is_web:
        summary_lines.append(
            f"web-shaped workspace: {files_with_http}/{len(test_files)} test file(s) use an HTTP client"
        )
    summary_lines.extend(per_file_breakdown)
    summary = "\n".join(summary_lines)

    fail_reasons: list[str] = []
    # Heuristics: 70%+ trivial is too weak; web project with zero HTTP tests
    # is also a hard fail.
    if trivial_ratio >= 0.70:
        fail_reasons.append(
            f"{trivial_tests}/{total_tests} tests are trivial (assert True / assert is not None)"
        )
    if web_needs_http:
        fail_reasons.append(
            "no test file performs an HTTP request — a web workspace must "
            "exercise its endpoints (use TestClient, requests, httpx, urllib)"
        )

    if total_tests - trivial_tests > 0 and behavioral_tests == 0:
        fail_reasons.append(
            "no behavioral workflow/output test found; at least one test must "
            "call real project behavior and assert concrete output, not only "
            "imports or object existence"
        )

    if misplaced_test_files:
        fail_reasons.append(
            "test files are under src/ instead of tests/: "
            + ", ".join(misplaced_test_files[:5])
        )

    if fail_reasons:
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.FAILED,
            summary=summary,
            error="; ".join(fail_reasons),
        )

    return VerificationStepResult(
        step=step,
        status=VerificationStatus.PASSED,
        summary=summary,
    )


__all__ = ["run_test_quality_guard"]
