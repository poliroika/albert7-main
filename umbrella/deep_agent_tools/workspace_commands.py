"""Workspace command, Python execution, and terminal helpers."""

from contextlib import contextmanager

from umbrella.deep_agent_tools.domain_policy import public_workspace_llm_env_bridge
from umbrella.deep_agent_tools.workspace_common import *
from umbrella.deep_agent_tools.workspace_ops import _phase_subtask_retry_escalation_block
from umbrella.enforcement import (
    append_supervisor_ledger_event,
    blocked_payload,
    check_post_tool_diff,
    diff_snapshots,
    phase_from_context,
    restore_snapshot_changes,
    snapshot_workspace,
)
from umbrella.enforcement.ledger import supervisor_ledger_ref


@contextmanager
def _workspace_public_llm_env(extra_env: dict[str, str] | None = None):
    """Expose Umbrella host LLM env to generated projects as public LLM_*."""

    effective = dict(os.environ)
    if extra_env:
        effective.update({str(k): str(v) for k, v in extra_env.items()})
    updates = public_workspace_llm_env_bridge(effective)
    updates.setdefault("PYTHONIOENCODING", "utf-8")
    updates.setdefault("PYTHONUTF8", "1")
    if extra_env:
        updates.update({str(k): str(v) for k, v in extra_env.items()})
    saved = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def _scrollback_path(ctx: Any) -> Path | None:
    """Locate ``<drive_root>/memory/terminal_scrollback.md`` for ``ctx``.

    Returns ``None`` if the drive root is unknown (e.g. unit-test contexts
    that don't set it).
    """
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return None
    try:
        path = Path(drive_root) / _SCROLLBACK_REL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        log.debug("scrollback path resolution failed", exc_info=True)
        return None


def _maybe_rotate_scrollback(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    except Exception:
        log.debug("scrollback stat failed", exc_info=True)
        return
    if size <= _SCROLLBACK_MAX_BYTES:
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        keep_from = int(len(data) * _SCROLLBACK_TRIM_FRACTION)
        new_text = (
            "<!-- scrollback rotated, oldest 25% dropped -->\n" + data[keep_from:]
        )
        path.write_text(new_text, encoding="utf-8")
    except Exception:
        log.debug("scrollback rotation failed", exc_info=True)


def _append_scrollback(
    ctx: Any,
    *,
    workspace_id: str,
    command: list[str] | str,
    result: RunResult,
    cwd: str,
) -> None:
    """Append a fenced block to ``terminal_scrollback.md`` for the LLM to re-read."""
    path = _scrollback_path(ctx)
    if path is None:
        return
    if isinstance(command, list):
        cmd_repr = shlex.join(command)
    else:
        cmd_repr = str(command)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    full_output = result.raw_output or result.output
    task_id = str(getattr(ctx, "task_id", "") or "")
    run_id = _run_id_from_task_id(task_id)
    body = (
        f"\n## ws={workspace_id} task={task_id or '-'} run={run_id or '-'} "
        f"ts={ts} exit={result.exit_code} backend={getattr(result, 'marker', '')[:6]}\n"
        f"cwd: {cwd}\n"
        f"$ {cmd_repr}\n"
        "```\n"
        f"{full_output}\n"
        "```\n"
    )
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        log.debug("scrollback append failed", exc_info=True)
        return
    _maybe_rotate_scrollback(path)


def _active_subtask_harness_profile(ctx: Any) -> str:
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return ""
    try:
        plan_path = Path(drive_root) / "state" / "phase_plan.json"
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        for node in payload.get("nodes") or []:
            if not isinstance(node, dict) or str(node.get("id") or "") != "execute":
                continue
            for subtask in node.get("subtasks") or []:
                if not isinstance(subtask, dict):
                    continue
                if str(subtask.get("status") or "").lower() == "done":
                    continue
                proof = subtask.get("proof")
                if isinstance(proof, dict):
                    return str(proof.get("harness_profile") or "")
            break
    except Exception:
        log.debug("active harness profile lookup failed", exc_info=True)
    return ""


def _secret_path_marker_from_token(value: str) -> str:
    token = str(value or "").strip().strip("'\"")
    if not token:
        return ""
    normalised = token.replace("\\", "/").split("::", 1)[0].strip()
    if _SECRET_ENV_PATH_RE.search(normalised):
        return ".env"

    looks_path_like = (
        "/" in normalised
        or normalised.startswith(".")
        or bool(re.search(r"\.[A-Za-z0-9_-]{1,12}$", normalised))
    )
    if not looks_path_like:
        return ""
    for part in (p for p in normalised.lower().split("/") if p):
        stem = part.split(".", 1)[0]
        if stem in _SECRET_PATH_COMPONENTS or stem in _SECRET_FILE_STEMS:
            return stem
    return ""


def _command_secret_read_reason(cmd: list[str]) -> str:
    for part in cmd:
        marker = _secret_path_marker_from_token(str(part))
        if marker:
            return (
                f"command references secret-like path/token `{marker}`; "
                "use a dedicated config reader that redacts values instead of shelling it"
            )
    return ""


def _command_program_basename(part: str) -> str:
    text = str(part or "").strip().strip('"').strip("'")
    # Logs and model retries often contain Windows interpreter paths even when
    # tests run on a non-Windows host. Normalize separators before Path.name.
    return Path(text.replace("\\", "/")).name.lower()


def _command_source_control_mutation_reason(cmd: list[str]) -> str:
    """Block rollback-style git operations through shell checks."""

    lowered = [str(part).strip().lower() for part in cmd]
    if not lowered:
        return ""
    program = _command_program_basename(lowered[0])
    if program in {"git", "git.exe"} and len(lowered) >= 2:
        if lowered[1] in _SOURCE_CONTROL_ROLLBACK_COMMANDS:
            return (
                f"`git {lowered[1]}` is blocked in workspace shell commands; "
                "fix files through sanctioned write tools instead of rolling back"
            )
    if program in _PYTHON_COMMAND_NAMES and len(lowered) >= 4:
        if lowered[1] == "-m" and lowered[2] == "git":
            operation = lowered[3]
            if operation in _SOURCE_CONTROL_ROLLBACK_COMMANDS:
                return (
                    f"`python -m git {operation}` is blocked in workspace shell commands; "
                    "fix files through sanctioned write tools instead of rolling back"
                )
    joined = " ".join(lowered)
    match = re.search(
        r"(?<![A-Za-z0-9_.-])git(?:\.exe)?\s+"
        r"(checkout|reset|restore|clean|stash)(?![A-Za-z0-9_-])",
        joined,
    )
    if match:
        return (
            f"`git {match.group(1)}` is blocked in workspace shell commands; "
            "fix files through sanctioned write tools instead of rolling back"
        )
    return ""


def _command_workspace_mutation_reason(cmd: list[str]) -> str:
    """Detect common shell-write patterns that bypass code-write guards."""
    lowered = [str(part).strip().lower() for part in cmd]
    joined = " ".join(lowered)

    if not lowered:
        return ""

    source_control_reason = _command_source_control_mutation_reason(cmd)
    if source_control_reason:
        return source_control_reason

    program = _command_program_basename(str(cmd[0]))
    if program in _DIRECT_WORKSPACE_MUTATION_COMMANDS:
        return (
            f"`{program}` is blocked in workspace shell commands; use "
            "apply_workspace_patch or delete_workspace_file "
            "for workspace file/directory changes"
        )

    if program in {"powershell", "pwsh", "powershell.exe", "pwsh.exe"}:
        write_verbs = (
            "set-content",
            "add-content",
            "out-file",
            "remove-item",
            "copy-item",
            "move-item",
            "new-item",
            "clear-content",
        )
        if any(verb in joined for verb in write_verbs):
            return "PowerShell file mutation is blocked; next use apply_workspace_patch for edits or delete_workspace_file for cleanup-only removal"

    if program in {"cmd", "cmd.exe"}:
        cmdline = " ".join(
            lowered[2:] if len(lowered) >= 2 and lowered[1] == "/c" else lowered[1:]
        )
        mutating = ("del ", "erase ", "copy ", "move ", "ren ", "rename ", "echo ")
        if ">" in cmdline or any(token in f" {cmdline} " for token in mutating):
            return "cmd.exe file mutation is blocked; next use apply_workspace_patch for edits or delete_workspace_file for cleanup-only removal"

    if program in _PYTHON_COMMAND_NAMES and len(lowered) >= 3 and lowered[1] == "-c":
        reason = _python_c_workspace_mutation_reason(str(cmd[2]))
        if reason:
            return reason
        if len(cmd) > 3:
            combined_code = " ".join(str(part) for part in cmd[2:])
            reason = _python_c_workspace_mutation_reason(combined_code)
            if reason:
                return reason

    return ""


def _command_nonportable_probe_reason(cmd: list[str]) -> str:
    """Reject shell-only source inspection when workspace tools are available."""

    if not cmd:
        return ""
    program = _command_program_basename(str(cmd[0]))
    if program in _NONPORTABLE_WORKSPACE_PROBE_COMMANDS:
        return (
            f"`{program}` is not a portable workspace inspection command in "
            "Umbrella runs. Use `read_file`/`repo_read` for file contents, "
            "`list_files`/`repo_list` for trees, or a checked-in test/script for "
            "behavioral diagnostics."
        )
    return ""


def _command_argv_portability_issue(cmd: list[str]) -> tuple[str, str]:
    """Reject argv shapes that ask the host to infer a shell or interpreter."""

    if not cmd:
        return "", ""
    first = str(cmd[0] or "").strip()
    if first.startswith("-"):
        return (
            "invalid_command_argv",
            (
                "command argv starts with an option instead of an executable. "
                "Include the interpreter/program explicitly, for example "
                '`["python", "-c", "..."]` or `["python", "-m", "pytest", ...]`.'
            ),
        )
    program = _command_program_basename(first)
    if program in {"bash", "bash.exe", "sh", "sh.exe"}:
        return (
            "nonportable_shell_interpreter_guard",
            (
                f"`{program}` is not a portable workspace command wrapper. "
                "Use direct argv such as `python -m pytest ...`, `npm run ...`, "
                "a checked-in Python/Node verification script, or the workspace "
                "read/write tools instead of POSIX shell scripts."
            ),
        )
    return "", ""


def _pytest_command_start_index(cmd: list[str]) -> int | None:
    lowered = [str(part).strip().lower() for part in cmd]
    if not lowered:
        return None
    program = _command_program_basename(lowered[0])
    if program in {"pytest", "pytest.exe"}:
        return 1
    if (
        program in _PYTHON_COMMAND_NAMES
        and len(lowered) >= 3
        and lowered[1] == "-m"
        and lowered[2] == "pytest"
    ):
        return 3
    return None


def _pytest_argv_shape_reason(cmd: list[str]) -> str:
    start = _pytest_command_start_index(cmd)
    if start is None:
        return ""
    for part in cmd[start:]:
        text = str(part)
        if not text.strip():
            continue
        # A common model slip is passing
        # "tests/test_x.py::test_name -v" as one argv element. Pytest treats
        # it as a single node id and reports "not found", wasting repair loops.
        if "::" in text and re.search(r"\s-{1,2}[A-Za-z]", text):
            return (
                "pytest node ids and flags must be separate argv elements; "
                f"split `{text}` into the test target and flag arguments."
            )
    return ""


def _python_c_workspace_mutation_reason(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    mutating_attrs = {
        "write",
        "writelines",
        "write_text",
        "write_bytes",
        "unlink",
        "remove",
        "rmdir",
        "rename",
        "replace",
        "mkdir",
        "touch",
    }
    mutating_modules = {"shutil", "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "open":
                if _open_call_is_mutating(node):
                    return "python -c file mutation is blocked; next use apply_workspace_patch for edits, delete_workspace_file for cleanup-only removal, or read_file/repo_read for reads"
            if isinstance(fn, ast.Attribute):
                if fn.attr in mutating_attrs:
                    return "python -c file mutation is blocked; use apply_workspace_patch for edits or delete_workspace_file for cleanup-only removal"
                if isinstance(fn.value, ast.Name) and fn.value.id in mutating_modules:
                    return "python -c subprocess/shutil is blocked; use sanctioned workspace tools instead"
    return ""


def _open_call_is_mutating(node: ast.Call) -> bool:
    mode = ""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value or "")
    for kw in node.keywords or []:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value or "")
    if not mode:
        return False
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _strip_redundant_workspace_cd_script(script: str, workspace_id: str) -> str:
    """Remove ``cd workspaces/<id>`` when cwd is already the workspace root."""

    wid = workspace_id.strip()
    if not wid:
        return script
    esc = re.escape(wid)
    pat = re.compile(rf"(?is)^\s*cd\s+((?:\./)?)workspaces/{esc}(?:/)?\s*(&&|;)\s*")
    new_script, n = pat.subn("", script, count=1)
    return new_script if n else script


def _rewrite_pip_install_script(script: str) -> str:
    """Prefer ``python -m pip install`` so the same interpreter sees packages."""

    s = script.strip()
    m = re.match(r"(?is)^(?P<prefix>\s*)(?P<pip>pip3?)\s+(?P<rest>install\b.*)$", s)
    if not m:
        return script
    return f"{m.group('prefix')}python -m pip {m.group('rest')}"


def _normalize_workspace_shell_script(script: str, workspace_id: str) -> str:
    inner = _strip_redundant_workspace_cd_script(script, workspace_id)
    inner = _rewrite_pip_install_script(inner)
    return inner


def _maybe_rewrite_workspace_command(cmd: list[str], workspace_id: str) -> list[str]:
    if len(cmd) < 3:
        return cmd
    prog, flag = cmd[0].lower(), cmd[1].lower()
    if prog not in {"bash", "sh"} or flag not in {"-c", "-lc"}:
        return cmd
    inner = cmd[2]
    new_inner = _normalize_workspace_shell_script(inner, workspace_id)
    if new_inner == inner:
        return cmd
    return [cmd[0], cmd[1], new_inner]


def _strip_workspace_cd_argv(
    cmd: list[str], workspace_id: str, subdir: str
) -> tuple[list[str], str]:
    """Turn ``cd workspaces/<id> && cmd`` argv into a scoped workspace cwd."""

    if len(cmd) >= 4 and cmd[0].strip().lower() == "cd" and cmd[2] in {"&&", ";"}:
        cd_target = _strip_workspace_prefix(workspace_id, cmd[1])
        parts = [part for part in (subdir, cd_target) if str(part or "").strip()]
        new_subdir = "/".join(str(part).strip().strip("/\\") for part in parts)
        return cmd[3:], new_subdir
    return cmd, subdir


def _strip_posix_timeout_wrapper(cmd: list[str]) -> list[str]:
    """Remove POSIX ``timeout N`` wrappers that are not portable to Windows."""

    if len(cmd) >= 3 and cmd[0].strip().lower() == "timeout":
        seconds = cmd[1].strip().lower().rstrip("s")
        try:
            float(seconds)
        except ValueError:
            return cmd
        return cmd[2:]
    return cmd


def _has_command_chain(cmd: list[str]) -> bool:
    return any(str(part).strip() in _COMMAND_CHAIN_TOKENS for part in cmd)


def _join_compound_command(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([str(part) for part in cmd])
    return " ".join(
        str(part) if str(part).strip() in _COMMAND_CHAIN_TOKENS else shlex.quote(str(part))
        for part in cmd
    )


def _wrap_compound_command_for_host(cmd: list[str]) -> list[str]:
    if not _has_command_chain(cmd):
        return cmd
    script = _join_compound_command(cmd)
    if os.name == "nt":
        return ["cmd", "/d", "/s", "/c", script]
    return ["bash", "-lc", script]


def run_workspace_command(
    ctx: Any,
    workspace_id: str,
    argv: list[str] | str | None = None,
    command: list[str] | str | None = None,
    subdir: str = "",
    timeout_seconds: int = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
    allow_dependency_install: bool = False,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run non-interactive checks/tests inside a host-repo workspace."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        subdir = _strip_workspace_prefix(workspace_id, subdir)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="run_workspace_command", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        raw_command = argv if argv is not None else command
        if raw_command is None:
            return _json(
                {
                    "status": "invalid_command",
                    "workspace_id": workspace_id,
                    "hint": "Missing command payload. Pass either `argv` or `command`.",
                    "next_step": (
                        "Use any command shape that fits the task: "
                        "`argv` as a JSON array of strings is preferred, but "
                        "`command` is also accepted for backward compatibility."
                    ),
                }
            )
        cmd, norm_err = _try_normalize_command(raw_command)
        if norm_err:
            return _json(
                {
                    "status": "invalid_command",
                    "workspace_id": workspace_id,
                    "hint": norm_err,
                    "next_step": (
                        "Pass either `argv` as a JSON array of strings or `command` "
                        "as a string/list. `argv` is preferred because it is easier "
                        "for the repair layer to preserve, but the tool intentionally "
                        "does not restrict what program or flags you can run."
                    ),
                }
            )
        cmd, subdir = _strip_workspace_cd_argv(cmd, workspace_id, subdir)
        cmd = _strip_posix_timeout_wrapper(cmd)
        cwd = _workspace_path(workspace_root, subdir)
        if retry_block := _phase_subtask_retry_escalation_block(
            ctx, tool_name="run_workspace_command"
        ):
            return _json(retry_block)
        cmd = _maybe_rewrite_workspace_command(cmd, workspace_id)
        portability_reason, portability_hint = _command_argv_portability_issue(cmd)
        if portability_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": portability_reason,
                    "command": cmd,
                    "hint": portability_hint,
                    "next_step": (
                        "Re-run the check with an explicit portable argv vector. "
                        "For source inspection, use `read_file`/`list_files`; for "
                        "multi-line analysis, use `run_python_code`."
                    ),
                }
            )
        secret_reason = _command_secret_read_reason(cmd)
        if secret_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": "secret_path_guard",
                    "command": cmd,
                    "hint": secret_reason,
                    "next_step": (
                        "Do not read secret/config files through shell. If a test needs "
                        "environment variables, run the test directly and rely on the "
                        "process environment instead of printing secret files."
                    ),
                }
            )
        probe_reason = _command_nonportable_probe_reason(cmd)
        if probe_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": "nonportable_shell_probe_guard",
                    "command": cmd,
                    "hint": probe_reason,
                    "next_step": (
                        "Use the workspace-aware tools instead of shell file "
                        "utilities: `read_file` for a known file, `list_files` "
                        "for directories, or `repo_read`/`repo_list` for repo "
                        "paths outside the active workspace. Then retry the "
                        "test or patch with evidence from that authoritative read."
                    ),
                }
            )
        pytest_reason = _pytest_argv_shape_reason(cmd)
        if pytest_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": "invalid_pytest_argv",
                    "command": cmd,
                    "hint": pytest_reason,
                    "next_step": (
                        "Re-run pytest with a real argv array, for example "
                        "`[\"python\", \"-m\", \"pytest\", "
                        "\"tests/test_file.py::test_case\", \"-v\"]`."
                    ),
                }
            )
        mutation_reason = _command_workspace_mutation_reason(cmd)
        if mutation_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_mutation_guard",
                    "command": cmd,
                    "hint": mutation_reason,
                    "next_step": (
                        "Use `apply_workspace_patch` for workspace file edits so the "
                        "harness can validate syntax, create backups, and record changes. "
                        "Use `run_workspace_command` only for read-only checks/tests."
                    ),
                }
            )
        if _looks_like_dependency_install(cmd):
            return _json(
                {
                    "status": "blocked",
                    "reason": "dependency_install_guard",
                    "command": cmd,
                    "allow_dependency_install_requested": bool(allow_dependency_install),
                    "next_step": (
                        "Dependency/environment provisioning is not proof. Use "
                        "`provision_workspace_environment` for sanctioned "
                        "environment setup, then rerun read-only verification."
                    ),
                }
            )
        is_server, server_hint = _looks_like_blocking_server(
            cmd, cwd=cwd, workspace_root=workspace_root
        )
        if is_server:
            return _json(
                {
                    "status": "blocked",
                    "reason": "blocking_server_in_foreground",
                    "command": cmd,
                    "matched": server_hint,
                    "hint": (
                        f"This command looks like a long-running server "
                        f"(matched `{server_hint}`). run_workspace_command is FOREGROUND -- "
                        "it would block until the per-call timeout fires and possibly "
                        "leak a port-bound process."
                    ),
                    "next_step": (
                        "Use the workspace verification/http_boot path or a background "
                        "server-aware tool. Do not run this server in the foreground."
                    ),
                }
            )
        is_interactive_launch, launch_hint = _looks_like_interactive_app_launch(cmd)
        if is_interactive_launch:
            runtime_profile = _active_subtask_harness_profile(ctx)
            if runtime_profile == "desktop_gui_runtime":
                next_step = (
                    "The active subtask is a desktop_gui_runtime proof. Use "
                    "`run_subtask_proof` so Umbrella launches the app through "
                    "the managed runtime lifecycle, waits for readiness, "
                    "collects evidence, and cleans up. For exploratory "
                    "runtime work use bg_start/bg_status/bg_tail/bg_kill, not "
                    "foreground run_workspace_command."
                )
            else:
                next_step = (
                    "Use non-interactive checks only: pytest/smoke commands, CLI test mode, "
                    "or import checks like `python -c \"import main; print('ok')\"`."
                )
            return _json(
                {
                    "status": "blocked",
                    "reason": "interactive_app_launch_guard",
                    "command": cmd,
                    "matched": launch_hint,
                    "active_harness_profile": runtime_profile,
                    "hint": (
                        "Interactive local app launches are blocked in run_workspace_command "
                        "to avoid hanging/broken foreground sessions."
                    ),
                    "next_step": next_step,
                }
            )
        py_c_problem = _python_c_compound_problem(cmd)
        if py_c_problem:
            return _json(
                {
                    "status": "blocked",
                    "reason": "python_c_compound_statement",
                    "command": cmd,
                    "hint": py_c_problem,
                    "next_step": (
                        "Call `run_python_code` with `code` set to the multi-line script. "
                        "Don't try to cram `def`/`async def`/`for`/`if` into a single "
                        "`python -c` argument."
                    ),
                }
            )
        # Always clamp to the per-call hard cap, regardless of what the LLM
        # asked for. This prevents one bad interactive command from burning
        # the entire task wall-clock budget.
        try:
            requested = int(timeout_seconds)
        except (TypeError, ValueError):
            requested = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S
        timeout = max(1, min(requested, _RUN_WORKSPACE_MAX_TIMEOUT_S))
        cmd = _rewrite_python_command_for_workspace(
            cmd, repo_root=repo_root, workspace_root=workspace_root
        )
        cmd = _wrap_compound_command_for_host(cmd)
        phase = phase_from_context(ctx)
        before_snapshot = snapshot_workspace(workspace_root, capture_content=True)
        with _workspace_public_llm_env(extra_env):
            session = get_or_create_session(ctx, workspace_id)
            result = session.run(cmd, cwd=str(cwd), timeout=timeout)
        changes = diff_snapshots(before_snapshot, snapshot_workspace(workspace_root))
        enforcement_issues = check_post_tool_diff(
            "run_workspace_command",
            phase,
            changes,
        )
        if enforcement_issues:
            touched_files = [change.path for change in changes]
            rollback = restore_snapshot_changes(before_snapshot, changes)
            try:
                record_workspace_event(
                    ctx,
                    workspace_id=workspace_id,
                    event_type="command_blocked",
                    summary="run_workspace_command mutated protected workspace paths",
                    details="\n".join(touched_files[:100]),
                    severity="error",
                    tags="command,enforcement_kernel,blocked",
                )
            except Exception:
                log.debug("record enforcement event failed", exc_info=True)
            try:
                blocked_ledger = append_supervisor_ledger_event(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    actor="agent",
                    phase=phase,
                    tool="run_workspace_command",
                    args={"command": cmd, "cwd": str(cwd)},
                    result={
                        "status": "blocked",
                        "exit_code": result.exit_code,
                        "issue_codes": [issue.code for issue in enforcement_issues],
                        "rollback": rollback,
                    },
                    touched_files=touched_files,
                )
            except Exception:
                log.debug(
                    "supervisor ledger append failed for blocked command",
                    exc_info=True,
                )
                blocked_ledger = None
            payload = blocked_payload(
                enforcement_issues,
                tool_name="run_workspace_command",
                phase=phase,
                touched_files=touched_files,
            )
            payload.update(
                {
                    "workspace_id": workspace_id,
                    "cwd": str(cwd),
                    "command": cmd,
                    "exit_code": result.exit_code,
                    "output_tail": (result.output or "")[-4000:],
                    "rollback": rollback,
                }
            )
            if blocked_ledger is not None:
                payload.update(supervisor_ledger_ref(blocked_ledger))
            return _json(payload)

        output = result.output
        # Scrollback gets the *full* slice (head/tail-truncated only as a
        # last resort), so the model can re-read prior terminal state in
        # the next round even after `_maybe_compact_history` drops the raw
        # tool message.
        try:
            _append_scrollback(
                ctx,
                workspace_id=workspace_id,
                command=cmd,
                result=result,
                cwd=str(cwd),
            )
        except Exception:
            log.debug("scrollback hook failed", exc_info=True)

        severity = "info" if result.exit_code == 0 else "error"
        if result.timed_out:
            severity = "error"
        event_tags = "command,validation,terminal,session"
        if result.session_recovered:
            event_tags += ",terminal_session_recovered"
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="command",
            summary=f"{' '.join(cmd)} -> exit {result.exit_code}",
            details=(output[:4000] if output else ""),
            severity=severity,
            tags=event_tags,
        )
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "cwd": str(cwd),
            "command": cmd,
            "exit_code": result.exit_code,
            "output": output,
            "backend": session.backend_name,
            "duration_seconds": round(result.duration_seconds, 3),
        }
        if result.timed_out:
            payload["timed_out"] = True
        if result.session_recovered:
            payload["terminal_session_recovered"] = True
        if result.truncated_head or result.truncated_tail:
            payload["truncated"] = True
        try:
            command_ledger = append_supervisor_ledger_event(
                repo_root=repo_root,
                workspace_id=workspace_id,
                actor="agent",
                phase=phase,
                tool="run_workspace_command",
                args={"command": cmd, "cwd": str(cwd)},
                result={
                    "exit_code": result.exit_code,
                    "timed_out": bool(result.timed_out),
                    "duration_seconds": round(result.duration_seconds, 3),
                },
                touched_files=[change.path for change in changes],
            )
            payload.update(supervisor_ledger_ref(command_ledger))
            payload["ledger_ref"] = {
                "ref_type": "ledger_event",
                "ref_id": command_ledger.event_id,
                "hash": command_ledger.event_hash,
                "produced_by": "agent",
                "phase": phase,
            }
        except Exception:
            log.debug("supervisor ledger append failed for command", exc_info=True)
        return _json(payload)
    except Exception as e:
        return f"WARNING: workspace command error: {e}"


def terminal_view(
    ctx: Any,
    workspace_id: str,
    last_lines: int = 200,
    grep: str = "",
) -> str:
    """Return the recent scrollback of the persistent shell for ``workspace_id``.

    Read-only. Use this to re-read what an earlier ``run_workspace_command``
    printed when the raw tool message was already compacted out of history.
    """
    try:
        get_or_create_session  # ensure import is alive even if session import fails
        session = get_or_create_session(ctx, workspace_id)
        try:
            requested = int(last_lines)
        except (TypeError, ValueError):
            requested = 200
        capped = max(1, min(requested, 4000))
        text = session.view(last_lines=capped, grep=(grep.strip() or None))
        # Hard cap so a noisy session can't blow past the tool-result limit.
        if len(text) > 60000:
            text = text[:30000] + "\n...(truncated)...\n" + text[-30000:]
        return _json(
            {
                "workspace_id": workspace_id,
                "backend": session.backend_name,
                "last_lines": capped,
                "grep": grep or None,
                "scrollback": text,
            }
        )
    except Exception as e:
        return f"WARNING: terminal_view error: {e}"


def run_python_code(
    ctx: Any,
    workspace_id: str,
    code: str,
    args: list[str] | None = None,
    subdir: str = "",
    timeout_seconds: int = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
    use_uv: bool = True,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run a multi-line Python script inside a workspace via a temp file.

    This is the ergonomic way to execute scripts that contain ``def``,
    ``async def``, ``class``, loops, ``with`` blocks, ``try/except``, etc.
    Don't try to cram those into ``python -c "...; ...; ..."`` -- CPython
    parses the ``-c`` body as a single simple statement and SyntaxErrors
    on any compound block keyword joined with ``;``.

    The script is written to ``<workspace>/.umbrella_scratch/run_<id>.py``,
    then executed (``uv run python <file> [args]`` if ``use_uv`` else
    ``python <file> [args]``). Stdout+stderr are returned exactly as
    ``run_workspace_command`` would. The temp file is left in place for
    debugging; the directory is gitignored via ``.umbrella_scratch/``.
    """
    try:
        if not isinstance(code, str) or not code.strip():
            return _json(
                {
                    "status": "blocked",
                    "reason": "empty_code",
                    "hint": "Pass `code` as a non-empty string with the Python source to run.",
                }
            )
        # Validate the script *before* we spawn an interpreter -- the
        # SyntaxError shape mirrors what update_workspace_seed already
        # returns, so the model can fix the script in-place.
        import ast as _ast

        try:
            _ast.parse(code)
        except SyntaxError as syn:
            line_no = int(syn.lineno or 0)
            snippet = ""
            try:
                if line_no:
                    lines = code.splitlines()
                    snippet = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            except Exception:
                snippet = ""
            return _json(
                {
                    "status": "blocked",
                    "reason": "python_syntax_error",
                    "error": f"{syn.msg} (line {syn.lineno}, col {syn.offset})",
                    "offending_line": snippet,
                    "next_step": "Fix the script and re-call run_python_code.",
                }
            )

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        cwd = _workspace_path(workspace_root, subdir)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="run_python_code", workspace_id=workspace_id
        ):
            return _json(stop_payload)

        scratch_dir = workspace_root / ".umbrella_scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        # Keep .umbrella_scratch out of git.
        gi = scratch_dir / ".gitignore"
        if not gi.exists():
            try:
                gi.write_text("*\n", encoding="utf-8")
            except Exception:
                pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        # Short hash to make filenames unique across rapid calls.
        import hashlib

        digest = hashlib.sha1(code.encode("utf-8", errors="replace")).hexdigest()[:8]
        script_path = scratch_dir / f"run_{ts}_{digest}.py"
        # Always write with a trailing newline so the interpreter is happy
        # even if the LLM omitted one.
        body = code if code.endswith("\n") else code + "\n"
        script_path.write_text(body, encoding="utf-8")

        argv: list[str] = []
        if use_uv:
            argv = ["uv", "run", "python", str(script_path)]
        else:
            argv = ["python", str(script_path)]
        if args:
            argv.extend(str(a) for a in args)

        try:
            requested = int(timeout_seconds)
        except (TypeError, ValueError):
            requested = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S
        timeout = max(1, min(requested, _RUN_WORKSPACE_MAX_TIMEOUT_S))

        env_overrides = dict(extra_env or {})
        with _workspace_public_llm_env(env_overrides):
            session = get_or_create_session(ctx, workspace_id)
            result = session.run(argv, cwd=str(cwd), timeout=timeout)

        try:
            _append_scrollback(
                ctx,
                workspace_id=workspace_id,
                command=argv,
                result=result,
                cwd=str(cwd),
            )
        except Exception:
            log.debug("scrollback hook failed", exc_info=True)

        severity = "info" if result.exit_code == 0 else "error"
        if result.timed_out:
            severity = "error"
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="command",
            summary=f"run_python_code {script_path.name} -> exit {result.exit_code}",
            details=(result.output[:4000] if result.output else ""),
            severity=severity,
            tags="command,validation,terminal,session,python_script",
        )
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "cwd": str(cwd),
            "script_path": str(script_path.relative_to(workspace_root)).replace(
                "\\", "/"
            ),
            "argv": argv,
            "exit_code": result.exit_code,
            "output": result.output,
            "duration_seconds": round(result.duration_seconds, 3),
            "backend": session.backend_name,
        }
        if result.timed_out:
            payload["timed_out"] = True
        return _json(payload)
    except Exception as e:
        log.error("run_python_code failed: %s", e, exc_info=True)
        return f"WARNING: run_python_code error: {e}"


def terminal_reset(
    ctx: Any,
    workspace_id: str,
    reason: str = "",
) -> str:
    """Kill the persistent shell for ``workspace_id`` and start a fresh one.

    All state (cwd, env vars, background jobs) is dropped. The agent must
    pass ``reason`` so the reset is recorded as an explicit decision.
    """
    try:
        if not str(reason).strip():
            return _json(
                {
                    "status": "blocked",
                    "reason": "missing_reason",
                    "hint": (
                        "terminal_reset destroys all in-shell state. Pass `reason` "
                        "explaining why a reset is justified before calling again."
                    ),
                }
            )
        session = get_or_create_session(ctx, workspace_id)
        old_backend = session.backend_name
        session.reset()
        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="terminal_reset",
                summary=f"terminal_reset: {reason.strip()[:160]}",
                details=(
                    f"Backend before reset: {old_backend}\n"
                    f"Backend after reset:  {session.backend_name}\n"
                    f"Reason: {reason.strip()}"
                ),
                severity="warning",
                tags="terminal,session,reset",
            )
        except Exception:
            log.debug("terminal_reset event log failed", exc_info=True)
        return _json(
            {
                "status": "reset",
                "workspace_id": workspace_id,
                "backend": session.backend_name,
                "reason": reason.strip(),
            }
        )
    except Exception as e:
        return f"WARNING: terminal_reset error: {e}"


def _try_normalize_command(command: list[str] | str) -> tuple[list[str] | None, str]:
    """Return (argv, error_message). error_message empty on success."""
    try:
        cmd = _normalize_command(command)
    except ValueError as e:
        return None, str(e)
    for part in cmd:
        if "\n" in part or "\r" in part:
            return (
                None,
                "command argv contains embedded newlines; use a list of strings or a one-line shell command.",
            )
    if not cmd:
        return None, "command parsed to an empty argv."
    return cmd, ""


def _strip_balanced_outer_quotes(value: str) -> str:
    stripped = value.strip()
    if (
        len(stripped) < 2
        or stripped[0] != stripped[-1]
        or stripped[0] not in {'"', "'"}
    ):
        return value
    inner = stripped[1:-1]
    if stripped[0] == '"':
        return inner.replace('\\"', '"')
    return inner.replace("\\'", "'")


def _repair_interpreter_payload_quotes(argv: list[str]) -> list[str]:
    normalized = [str(part) for part in argv]

    def _unwrap_at(index: int) -> None:
        if index < len(normalized):
            normalized[index] = _strip_balanced_outer_quotes(normalized[index])

    lowered = [part.lower() for part in normalized[:4]]
    if (
        len(normalized) >= 3
        and lowered[:2]
        and lowered[0] in {"python", "python3", "py"}
        and lowered[1] == "-c"
    ):
        _unwrap_at(2)
    elif (
        len(normalized) >= 5
        and lowered[:4]
        and lowered[0] == "uv"
        and lowered[1] == "run"
        and lowered[2] in {"python", "python3", "py"}
        and lowered[3] == "-c"
    ):
        _unwrap_at(4)
    elif (
        len(normalized) >= 3
        and lowered[:2]
        and lowered[0] in {"powershell", "pwsh"}
        and lowered[1] == "-command"
    ):
        _unwrap_at(2)
    elif (
        len(normalized) >= 3
        and lowered[:2]
        and lowered[0] in {"bash", "sh"}
        and lowered[1] in {"-c", "-lc"}
    ):
        _unwrap_at(2)
    return normalized


def _normalize_command(command: list[str] | str) -> list[str]:
    def _strip_balanced_outer_quotes(value: str) -> str:
        stripped = value.strip()
        if (
            len(stripped) < 2
            or stripped[0] != stripped[-1]
            or stripped[0] not in {'"', "'"}
        ):
            return value
        inner = stripped[1:-1]
        if stripped[0] == '"':
            return inner.replace('\\"', '"')
        return inner.replace("\\'", "'")

    def _repair_interpreter_payload_quotes(argv: list[str]) -> list[str]:
        normalized = [str(part) for part in argv]

        def _unwrap_at(index: int) -> None:
            if index < len(normalized):
                normalized[index] = _strip_balanced_outer_quotes(normalized[index])

        lowered = [part.lower() for part in normalized[:4]]
        if (
            len(normalized) >= 3
            and lowered[:2]
            and lowered[0] in {"python", "python3", "py"}
            and lowered[1] == "-c"
        ):
            _unwrap_at(2)
        elif (
            len(normalized) >= 5
            and lowered[:4]
            and lowered[0] == "uv"
            and lowered[1] == "run"
            and lowered[2] in {"python", "python3", "py"}
            and lowered[3] == "-c"
        ):
            _unwrap_at(4)
        elif (
            len(normalized) >= 3
            and lowered[:2]
            and lowered[0] in {"powershell", "pwsh"}
            and lowered[1] == "-command"
        ):
            _unwrap_at(2)
        elif (
            len(normalized) >= 3
            and lowered[:2]
            and lowered[0] in {"bash", "sh"}
            and lowered[1] in {"-c", "-lc"}
        ):
            _unwrap_at(2)
        return normalized

    if isinstance(command, str):
        stripped = command.strip()
        if "\n" in command and not stripped.startswith("["):
            raise ValueError(
                "command string contains raw newlines; use a JSON array of strings instead."
            )
        looks_like_json_argv = stripped.startswith("[") or stripped.startswith("['")
        try:
            parsed = json.loads(command)
            if isinstance(parsed, list):
                return _repair_interpreter_payload_quotes(
                    [str(part) for part in parsed]
                )
            if isinstance(parsed, str):
                return _repair_interpreter_payload_quotes(
                    shlex.split(parsed, posix=os.name != "nt")
                )
        except json.JSONDecodeError as e:
            if looks_like_json_argv:
                raise ValueError(
                    "command string looks like a JSON argv array but is invalid; "
                    "pass `argv` as an actual JSON array of strings, not a "
                    "stringified or line-broken array"
                ) from e
        try:
            return _repair_interpreter_payload_quotes(
                shlex.split(command, posix=os.name != "nt")
            )
        except ValueError as e:
            raise ValueError(f"cannot parse command as shell: {e}") from e
    if isinstance(command, list):
        return _repair_interpreter_payload_quotes([str(part) for part in command])
    raise ValueError("command must be a list of strings or a shell-like string")


def _looks_like_dependency_install(cmd: list[str]) -> bool:
    lowered = [part.lower() for part in cmd]
    if not lowered:
        return False
    package_managers = {"pip", "pip3", "uv", "poetry", "npm", "pnpm", "yarn"}
    if lowered[0] in package_managers and any(
        part in {"install", "add", "sync"} for part in lowered[1:]
    ):
        return True
    if len(lowered) >= 4 and lowered[1:3] == ["-m", "pip"] and "install" in lowered[3:]:
        return True
    return False


def _looks_like_blocking_server(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    workspace_root: Path | None = None,
) -> tuple[bool, str]:
    """Return (looks_like_server, matched_hint).

    The check is deliberately conservative: we want to refuse the *common*
    footguns (uvicorn, gunicorn, dev servers) without blocking legitimate
    short-lived commands that happen to mention these names.
    """
    if not cmd:
        return False, ""
    lowered = [str(part).lower() for part in cmd]

    # Python inline checks like:
    #   python -c "import fastapi; print('ok')"
    # must be allowed (they are short-lived import probes, not servers).
    def _python_c_body(parts: list[str]) -> str:
        if (
            len(parts) >= 3
            and parts[0] in {"python", "python3", "py"}
            and parts[1] == "-c"
        ):
            return parts[2]
        if (
            len(parts) >= 5
            and parts[0] == "uv"
            and parts[1] == "run"
            and parts[2] in {"python", "python3", "py"}
            and parts[3] == "-c"
        ):
            return parts[4]
        return ""

    body = _python_c_body(lowered)
    if body:
        if "uvicorn.run(" in body or "app.run(" in body or ".serve(" in body:
            return True, "python -c with .run("
        return False, ""

    # Direct server launchers by executable name.
    for pat in _SERVER_TOKEN_PATTERNS:
        if lowered[0] == pat:
            return True, pat
        if (
            len(lowered) >= 3
            and lowered[0] == "uv"
            and lowered[1] == "run"
            and lowered[2] == pat
        ):
            return True, pat

    # Common multi-token foreground server launches.
    if len(lowered) >= 2 and lowered[0] == "flask" and lowered[1] == "run":
        return True, "flask run"
    if len(lowered) >= 2 and lowered[0] == "next" and lowered[1] in {"dev", "start"}:
        return True, f"next {lowered[1]}"
    if len(lowered) >= 2 and lowered[0] == "rails" and lowered[1] == "server":
        return True, "rails server"
    if len(lowered) >= 2 and lowered[0] == "manage.py" and lowered[1] == "runserver":
        return True, "manage.py runserver"
    if len(lowered) >= 2 and lowered[0] == "ray" and lowered[1] == "start":
        return True, "ray start"
    if len(lowered) >= 2 and lowered[0] == "celery" and lowered[1] == "worker":
        return True, "celery worker"
    if len(lowered) >= 2 and lowered[0] == "rq" and lowered[1] == "worker":
        return True, "rq worker"
    if len(lowered) >= 2 and lowered[0] == "ollama" and lowered[1] in {"serve", "run"}:
        return True, f"ollama {lowered[1]}"

    # Generic token check still useful for obvious "runserver" binaries.
    for tok in lowered:
        for sub in _SERVER_TOKEN_SUBSTRINGS:
            if sub in tok:
                return True, sub
    script = _python_script_target(cmd)
    if script:
        if workspace_root is not None and _workspace_declares_http_boot_for_script(
            workspace_root, script
        ):
            return True, f"workspace verification declares server entry {script}"
        if cwd is not None and _script_contains_server_entry(cwd / script):
            return True, f"{script} contains server entrypoint"
    return False, ""


def _python_module_target(cmd: list[str]) -> str:
    """Return module target for `python -m package.module` commands."""
    if not cmd:
        return ""
    lowered = [part.lower() for part in cmd]
    start = 0
    if len(lowered) >= 3 and lowered[0] == "uv" and lowered[1] == "run":
        start = 2
        while start < len(lowered) and lowered[start].startswith("-"):
            start += 1
            if start < len(lowered) and lowered[start - 1] in {"--python", "-p"}:
                start += 1
    if start >= len(lowered) or lowered[start] not in {"python", "python3", "py"}:
        return ""
    idx = start + 1
    while idx < len(lowered):
        token = lowered[idx]
        if token == "-m" and idx + 1 < len(cmd):
            return str(cmd[idx + 1]).strip()
        if token == "-c":
            return ""
        idx += 1
    return ""


def _looks_like_interactive_app_launch(cmd: list[str]) -> tuple[bool, str]:
    """Detect likely interactive app entrypoint launches (game/UI loops)."""
    script = _python_script_target(cmd)
    if script:
        script_name = Path(script).name.lower()
        if script_name in _INTERACTIVE_APP_ENTRY_NAMES:
            return True, f"python script entrypoint `{script_name}`"
    module = _python_module_target(cmd)
    if module:
        tail = module.split(".")[-1].lower()
        if tail in _INTERACTIVE_APP_MODULE_NAMES:
            return True, f"python -m `{module}`"
    return False, ""


def _python_script_target(cmd: list[str]) -> str:
    """Return the Python script target for `python main.py`-style commands."""
    if not cmd:
        return ""
    lowered = [part.lower() for part in cmd]
    start = 0
    if len(lowered) >= 3 and lowered[0] == "uv" and lowered[1] == "run":
        start = 2
        while start < len(lowered) and lowered[start].startswith("-"):
            start += 1
            if start < len(lowered) and lowered[start - 1] in {"--python", "-p"}:
                start += 1
    if start >= len(lowered) or lowered[start] not in {"python", "python3", "py"}:
        return ""
    idx = start + 1
    while idx < len(cmd):
        token = cmd[idx]
        low = lowered[idx]
        if low == "-c" or low == "-m":
            return ""
        if low.startswith("-"):
            idx += 1
            continue
        if token.endswith(".py"):
            return token.replace("\\", "/")
        return ""
    return ""


def _script_contains_server_entry(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size > 512_000:
            return False
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in text for marker in _SERVER_SOURCE_MARKERS)


def _workspace_declares_http_boot_for_script(workspace_root: Path, script: str) -> bool:
    config_path = workspace_root / "workspace.toml"
    if not config_path.exists():
        return False
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    script_name = Path(script).name.lower()
    server = data.get("server")
    if isinstance(server, dict):
        candidates = [
            server.get("entry"),
            server.get("entrypoint"),
            server.get("command"),
        ]
        if any(
            _command_mentions_script(candidate, script_name) for candidate in candidates
        ):
            return True
    verification = data.get("verification")
    steps = verification.get("steps") if isinstance(verification, dict) else None
    if isinstance(steps, list):
        for step in steps:
            if (
                isinstance(step, dict)
                and str(step.get("kind") or "").lower() == "http_boot"
            ):
                if _command_mentions_script(step.get("command"), script_name):
                    return True
    return False


def _command_mentions_script(command: Any, script_name: str) -> bool:
    if command is None:
        return False
    if isinstance(command, list):
        return any(
            Path(str(part).replace("\\", "/")).name.lower() == script_name
            for part in command
        )
    text = str(command).replace("\\", "/").lower()
    return (
        f"/{script_name}" in text
        or text.split()[-1:] == [script_name]
        or script_name in text.split()
    )


def _python_c_compound_problem(cmd: list[str]) -> str:
    """Detect the classic `python -c "import x; def foo(): ..."` footgun.

    `python -c` parses its body as a single ``simple_stmt``-style line and
    chokes on compound statements joined with ``;`` (``def``, ``async def``,
    ``class``, ``for``, ``while``, ``if``, ``try``). This function returns a
    short human-readable problem description, or an empty string if no
    issue was detected.

    Implementation notes:
      * We delegate to ``compile(body, '<string>', 'exec')`` so legitimate
        expressions that *contain* the keyword (list / set / dict
        comprehensions, generator expressions, conditional expressions) are
        not falsely rejected. The previous substring heuristic blocked e.g.
        ``python -c "from x import Y; doc=Y(p); print('\\n'.join(t.text for t in doc.paragraphs))"``
        because it spotted the ``for `` substring inside a generator
        expression.
      * Only ``SyntaxError`` is treated as a structural problem; other
        compile errors (NameError etc. don't happen at compile time) are
        ignored — we want to let the actual runtime decide.
    """
    body: str | None = None
    if (
        len(cmd) >= 3
        and cmd[0].lower() in {"python", "python3", "py"}
        and cmd[1] == "-c"
    ):
        body = cmd[2]
    elif (
        len(cmd) >= 5
        and cmd[0].lower() == "uv"
        and cmd[1].lower() == "run"
        and cmd[2].lower() in {"python", "python3", "py"}
        and cmd[3] == "-c"
    ):
        body = cmd[4]
    if not body or "\n" in body:
        return ""
    if ";" not in body:
        # No statement separator => python -c handles single-statement
        # bodies fine. Even ``python -c "for i in range(3): print(i)"``
        # parses as one compound statement on a single line.
        return ""
    try:
        compile(body, "<python -c>", "exec")
    except SyntaxError:
        return (
            "`python -c` cannot parse compound statements (def/async def/class/for/while/if/try) "
            "joined with `;` -- it parses the body as a single simple statement and raises "
            "SyntaxError on the first block keyword. Use `run_python_code` instead, which "
            "writes your code to a temp file and runs it."
        )
    except Exception:
        # Any non-syntax compile failure is not our concern; let the real
        # interpreter surface it.
        return ""
    return ""


__all__ = [
    '_scrollback_path',
    '_maybe_rotate_scrollback',
    '_append_scrollback',
    '_secret_path_marker_from_token',
    '_command_secret_read_reason',
    '_command_program_basename',
    '_command_source_control_mutation_reason',
    '_command_workspace_mutation_reason',
    '_command_nonportable_probe_reason',
    '_pytest_command_start_index',
    '_pytest_argv_shape_reason',
    '_python_c_workspace_mutation_reason',
    '_open_call_is_mutating',
    '_strip_redundant_workspace_cd_script',
    '_rewrite_pip_install_script',
    '_normalize_workspace_shell_script',
    '_maybe_rewrite_workspace_command',
    '_strip_workspace_cd_argv',
    '_strip_posix_timeout_wrapper',
    '_has_command_chain',
    '_join_compound_command',
    '_wrap_compound_command_for_host',
    'run_workspace_command',
    'terminal_view',
    'run_python_code',
    'terminal_reset',
    '_try_normalize_command',
    '_strip_balanced_outer_quotes',
    '_repair_interpreter_payload_quotes',
    '_normalize_command',
    '_looks_like_dependency_install',
    '_looks_like_blocking_server',
    '_python_module_target',
    '_looks_like_interactive_app_launch',
    '_python_script_target',
    '_script_contains_server_entry',
    '_workspace_declares_http_boot_for_script',
    '_command_mentions_script',
    '_python_c_compound_problem',
]
