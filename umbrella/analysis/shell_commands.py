"""Argv-level validation for proof commands."""

from __future__ import annotations

import ast

from umbrella.analysis.models import StaticAnalysisIssue


_SHELL_EXES = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}
_SHELL_EVAL_FLAGS = {"-c", "-lc", "/c", "-command", "-encodedcommand"}
_FAILURE_MASKING_TOKENS = {
    "|| true",
    "|| :",
    "exit 0",
    "set +e",
}
_SHELL_CHAIN_TOKENS = {"&&", "||", "|", ";", "&"}
_AGENT_TOOL_PSEUDO_COMMANDS = {
    "apply_workspace_patch",
    "delete_workspace_file",
    "mark_subtask_complete",
    "mutate_phase_plan",
    "propose_phase_plan",
    "request_watcher_review",
    "run_workspace_command",
    "run_workspace_verify",
    "shell",
    "submit_micro_review",
    "submit_phase_plan",
}
_PYTHON_EXES = {
    "python",
    "python.exe",
    "python3",
    "python3.exe",
    "py",
    "py.exe",
}
_SUBPROCESS_CALLS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "Popen",
}


def _exe_name(command: tuple[str, ...]) -> str:
    if not command:
        return ""
    return command[0].lower().replace("\\", "/").rsplit("/", 1)[-1]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _constant_bool(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _python_c_is_import_only(tree: ast.AST) -> bool:
    if not isinstance(tree, ast.Module) or not tree.body:
        return False
    saw_import = False
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            saw_import = True
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "print"
        ):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return saw_import


def _python_c_code(command: tuple[str, ...]) -> str:
    if len(command) < 3 or _exe_name(command) not in _PYTHON_EXES:
        return ""
    for idx, part in enumerate(command[1:-1], start=1):
        if part == "-c":
            return command[idx + 1]
    return ""


def _validate_python_c_code(code: str, *, path: str = "") -> list[StaticAnalysisIssue]:
    issues: list[StaticAnalysisIssue] = []
    if not code:
        return issues
    try:
        tree = ast.parse(code)
    except SyntaxError:
        issues.append(
            StaticAnalysisIssue(
                code="invalid_python_c_proof",
                path=path,
                snippet=code[:160],
                message=(
                    "Inline proof has invalid `python -c` code; it must be "
                    "syntactically valid Python so Umbrella can execute and "
                    "statically inspect it."
                ),
            )
        )
        if "shell=true" in code.replace(" ", "").lower():
            issues.append(
                StaticAnalysisIssue(
                    code="python_subprocess_shell_forbidden",
                    path=path,
                    snippet="shell=True",
                    message=(
                        "Inline python proof must not hide shell=True inside "
                        "subprocess calls."
                    ),
                )
            )
        return issues
    has_import = any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
    has_assert = any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    has_checked_subprocess = False
    complex_inline = False
    workspace_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and isinstance(node.module, str)
        and node.module.startswith(("src.", "backend.", "frontend."))
    ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in {
            "subprocess.Popen",
            "subprocess.run",
            "time.sleep",
            "requests.get",
            "requests.post",
        }:
            complex_inline = True
        if name in _SUBPROCESS_CALLS:
            has_checked_subprocess = True
        if name not in _SUBPROCESS_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg == "shell" and _constant_bool(keyword.value) is True:
                issues.append(
                    StaticAnalysisIssue(
                        code="python_subprocess_shell_forbidden",
                        path=path,
                        snippet=f"{name}(shell=True)",
                        message=(
                            "Proof commands cannot bypass argv policy by using "
                            "subprocess shell=True inside python -c."
                        ),
                    )
                )
            if keyword.arg == "check" and _constant_bool(keyword.value) is False:
                issues.append(
                    StaticAnalysisIssue(
                        code="python_subprocess_check_false",
                        path=path,
                        snippet=f"{name}(check=False)",
                        message=(
                            "Verification subprocesses must fail loudly instead "
                            "of using check=False."
                        ),
                )
            )
    if complex_inline or len(code) > 220 or "\n" in code:
        issues.append(
            StaticAnalysisIssue(
                code="complex_python_inline_proof",
                path=path,
                snippet=code[:160],
                message=(
                    "Inline python proof is too complex for a plan command; "
                    "move orchestration, service startup, sleeps, HTTP checks, "
                    "or multi-line logic into a checked-in verifier script or "
                    "managed harness."
                ),
            )
        )
    if workspace_imports:
        issues.append(
            StaticAnalysisIssue(
                code="workspace_import_inline_proof",
                path=path,
                snippet=", ".join(workspace_imports[:4]),
                message=(
                    "Inline python proof imports workspace/application modules; "
                    "put behavioral assertions in pytest or a checked-in "
                    "verifier so imports, fixtures, and package paths are "
                    "owned by the workspace."
                ),
            )
        )
    if (
        has_import
        and not has_assert
        and not has_checked_subprocess
        and _python_c_is_import_only(tree)
    ):
        issues.append(
            StaticAnalysisIssue(
                code="import_only_proof",
                path=path,
                snippet=code[:160],
                message=(
                    "Inline python proof only imports modules; add a real "
                    "assertion, execute a checked verifier script, or use "
                    "pytest for behavioral proof."
                ),
            )
        )
    return issues


def validate_argv(
    command: tuple[str, ...],
    *,
    shell: bool = False,
    path: str = "",
) -> list[StaticAnalysisIssue]:
    issues: list[StaticAnalysisIssue] = []
    if shell:
        issues.append(
            StaticAnalysisIssue(
                code="shell_proof_forbidden",
                path=path,
                message="Proof execution must use argv with shell=false.",
            )
        )
    if not command:
        issues.append(
            StaticAnalysisIssue(
                code="empty_proof_command",
                path=path,
                message="Proof command argv is required.",
            )
        )
        return issues
    exe = _exe_name(command)
    if exe in _AGENT_TOOL_PSEUDO_COMMANDS:
        issues.append(
            StaticAnalysisIssue(
                code="unavailable_proof_target",
                path=path,
                snippet=exe,
                message=(
                    f"Proof command `{exe}` is an Umbrella tool, not a "
                    "workspace executable. Use a concrete test/build/HTTP "
                    "command inside the workspace; Umbrella invokes supervisor "
                    "tools separately."
                ),
            )
        )
    lowered_args = {part.lower() for part in command[1:3]}
    if exe in _PYTHON_EXES and len(command) >= 2:
        target = str(command[1] or "")
        if "::" in target and (target.endswith(".py") or ".py::" in target):
            issues.append(
                StaticAnalysisIssue(
                    code="python_pytest_node_without_pytest",
                    path=path,
                    snippet=target,
                    message=(
                        "Python proof points at a pytest node directly; use "
                        "`python -m pytest <target>` so pytest executes the "
                        "test contract."
                    ),
                )
            )
    if exe in _SHELL_EXES and lowered_args & _SHELL_EVAL_FLAGS:
        issues.append(
            StaticAnalysisIssue(
                code="shell_process_control_forbidden",
                path=path,
                snippet=" ".join(command[:3]),
                message=(
                    "Proof command uses non-portable or unmanaged "
                    "shell/process-control instead of direct argv."
                ),
            )
        )
    for part in command:
        token = str(part or "").strip()
        if token in _SHELL_CHAIN_TOKENS:
            issues.append(
                StaticAnalysisIssue(
                    code="shell_operator_in_argv",
                    path=path,
                    snippet=token,
                    message=(
                        "Proof command argv must not include non-portable or "
                        "unmanaged shell/process-control operators; split "
                        "setup and verification into direct commands or use a "
                        "checked-in script."
                    ),
                )
            )
    joined = " ".join(command).lower()
    for token in _FAILURE_MASKING_TOKENS:
        if token in joined:
            issues.append(
                StaticAnalysisIssue(
                    code="shell_failure_masking",
                    path=path,
                    snippet=token,
                    message="Proof command masks command failure status.",
                )
            )
    issues.extend(_validate_python_c_code(_python_c_code(command), path=path))
    return issues
