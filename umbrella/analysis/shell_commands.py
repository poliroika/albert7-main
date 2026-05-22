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
                    "Inline python proof commands must be syntactically valid "
                    "Python so Umbrella can execute and statically inspect them."
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
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
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
    if exe in _SHELL_EXES and lowered_args & _SHELL_EVAL_FLAGS:
        issues.append(
            StaticAnalysisIssue(
                code="shell_process_control_forbidden",
                path=path,
                snippet=" ".join(command[:3]),
                message="Proof command uses shell eval flags instead of direct argv.",
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
                        "Proof command argv must not include shell chaining "
                        "operators; split setup and verification into direct "
                        "commands or use a checked-in script."
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
                    message="Proof command masks failures.",
                )
            )
    issues.extend(_validate_python_c_code(_python_c_code(command), path=path))
    return issues
