"""Static JS/TS test tampering checks.

This v1 analyzer is intentionally dependency-light. It operates on source
tokens and line structure so Umbrella can run without Node/Babel installed;
the public API is ready for a TypeScript compiler or Babel-backed adapter.
"""

from __future__ import annotations

from umbrella.analysis.models import StaticAnalysisIssue


def _add(
    issues: list[StaticAnalysisIssue],
    *,
    code: str,
    path: str,
    line_no: int,
    line: str,
    message: str,
) -> None:
    issues.append(
        StaticAnalysisIssue(
            code=code,
            path=path,
            line=line_no,
            snippet=line.strip(),
            message=message,
        )
    )


def analyze_jsts_test_source(source: str, *, path: str = "") -> list[StaticAnalysisIssue]:
    issues: list[StaticAnalysisIssue] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        compact = "".join(line.split())
        lowered = compact.lower()
        if any(token in lowered for token in ("test.skip(", "it.skip(", "describe.skip(")):
            _add(
                issues,
                code="js_test_skip",
                path=path,
                line_no=line_no,
                line=line,
                message="Skipped JS/TS tests cannot prove completion.",
            )
        if any(token in lowered for token in ("test.todo(", "it.todo(")):
            _add(
                issues,
                code="js_test_todo",
                path=path,
                line_no=line_no,
                line=line,
                message="TODO tests are not executable proof.",
            )
        if "expect(true)" in lowered or "expect(1)" in lowered:
            _add(
                issues,
                code="js_expect_true",
                path=path,
                line_no=line_no,
                line=line,
                message="Trivial constant JS/TS expectations do not prove behavior.",
            )
        if any(token in lowered for token in ("jest.mock(", "vi.mock(")):
            _add(
                issues,
                code="js_target_mock",
                path=path,
                line_no=line_no,
                line=line,
                message="Mocking target modules is not acceptable behavioral proof.",
            )
        if any(token in lowered for token in ("--passwithnotests", "passwithnotests:true")):
            _add(
                issues,
                code="js_pass_with_no_tests",
                path=path,
                line_no=line_no,
                line=line,
                message="Pass-with-no-tests bypasses are forbidden.",
            )
    return issues
