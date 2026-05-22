"""AST checks for Python test tampering and weak proof patterns."""

from __future__ import annotations

import ast

from umbrella.analysis.models import StaticAnalysisIssue


def _call_name(call: ast.Call) -> str:
    node: ast.AST = call.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _snippet(source_lines: list[str], node: ast.AST) -> str:
    line_no = int(getattr(node, "lineno", 0) or 0)
    if 1 <= line_no <= len(source_lines):
        return source_lines[line_no - 1].strip()
    return ""


def _constant_truth(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is True or node.value == 1
    if isinstance(node, ast.NameConstant):
        return node.value is True
    return False


def _weak_none_compare(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
        return False
    return bool(node.comparators) and isinstance(node.comparators[0], ast.Constant) and node.comparators[0].value is None


class PyTestTamperVisitor(ast.NodeVisitor):
    def __init__(self, *, path: str, source: str) -> None:
        self.path = path
        self.source_lines = source.splitlines()
        self.issues: list[StaticAnalysisIssue] = []
        self._test_stack: list[str] = []

    @property
    def in_test(self) -> bool:
        return bool(self._test_stack)

    def _add(self, code: str, node: ast.AST, message: str) -> None:
        self.issues.append(
            StaticAnalysisIssue(
                code=code,
                path=self.path,
                line=int(getattr(node, "lineno", 0) or 0),
                snippet=_snippet(self.source_lines, node),
                message=message,
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("test_"):
            self._test_stack.append(node.name)
            self.generic_visit(node)
            self._test_stack.pop()
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name.startswith("test_"):
            self._test_stack.append(node.name)
            self.generic_visit(node)
            self._test_stack.pop()
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        leaf = name.rsplit(".", 1)[-1] if name else ""
        if name in {"pytest.skip", "pytest.xfail"} or name.endswith(".skip") or name.endswith(".xfail"):
            self._add(
                "pytest_skip_or_xfail",
                node,
                "Tests must not be skipped/xfail'ed as proof of completion.",
            )
        if leaf in {"MagicMock", "Mock", "AsyncMock"} or name.endswith(".patch") or name in {"patch", "unittest.mock.patch"}:
            self._add(
                "target_behavior_mock",
                node,
                "Mocking target behavior is not acceptable proof.",
            )
        if leaf in {"monkeypatch", "setattr", "setenv"} or "mocker" in name.split("."):
            self._add(
                "target_behavior_mock",
                node,
                "Monkeypatching target behavior is not acceptable proof.",
            )
        if name.endswith("subprocess.run"):
            for keyword in node.keywords:
                if keyword.arg == "check" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False:
                    self._add(
                        "subprocess_check_false",
                        node,
                        "Verification subprocesses must fail loudly instead of using check=False.",
                    )
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if _constant_truth(node.test):
            self._add(
                "assert_true",
                node,
                "Trivial constant assertions do not prove behavior.",
            )
        elif _weak_none_compare(node.test):
            self._add(
                "weak_not_none_assertion",
                node,
                "`is None`/`is not None` assertions are too weak for behavioral proof.",
            )
        elif isinstance(node.test, ast.Name):
            self._add(
                "weak_truthy_assertion",
                node,
                "Bare truthiness assertions are too weak for behavioral proof.",
            )
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.in_test:
            self._add(
                "early_return_in_test",
                node,
                "Early returns in tests can bypass assertions.",
            )
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            catches_broad = handler.type is None
            if isinstance(handler.type, ast.Name):
                catches_broad = handler.type.id in {"Exception", "BaseException"}
            elif isinstance(handler.type, ast.Tuple):
                catches_broad = any(
                    isinstance(elt, ast.Name) and elt.id in {"Exception", "BaseException"}
                    for elt in handler.type.elts
                )
            if not catches_broad:
                continue
            fails_loudly = any(
                isinstance(inner, (ast.Raise, ast.Assert))
                or (
                    isinstance(inner, ast.Call)
                    and _call_name(inner).split(".")[-1] in {"fail", "xfail"}
                )
                for inner in ast.walk(handler)
            )
            if not fails_loudly:
                self._add(
                    "broad_try_except_pass",
                    handler,
                    "Broad exception swallowing in tests/probes hides failures.",
                )
        self.generic_visit(node)


def analyze_python_test_source(source: str, *, path: str = "") -> list[StaticAnalysisIssue]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            StaticAnalysisIssue(
                code="python_test_syntax_error",
                path=path,
                line=exc.lineno or 0,
                message=f"Python test file cannot be parsed: {exc.msg}",
            )
        ]
    visitor = PyTestTamperVisitor(path=path, source=source)
    visitor.visit(tree)
    return visitor.issues
