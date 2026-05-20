"""Workspace metrics, logs, git, and background-job helpers."""

from umbrella.deep_agent_tools.workspace_common import *
from umbrella.deep_agent_tools.workspace_commands import (
    _strip_posix_timeout_wrapper,
    _strip_workspace_cd_argv,
    _try_normalize_command,
)

def _git_commit_disabled_payload(tool_name: str, workspace_id: str = "") -> str:
    if str(os.environ.get("OUROBOROS_ALLOW_GIT_COMMIT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return ""
    return _json(
        {
            "status": "blocked",
            "reason": "git_commit_disabled_by_policy",
            "tool": tool_name,
            "workspace_id": workspace_id,
            "next_step": (
                "Leave changes in the working tree. A human can inspect and commit them, "
                "or set OUROBOROS_ALLOW_GIT_COMMIT=1 to re-enable local commits."
            ),
        }
    )


def commit_workspace_changes(
    ctx: Any,
    workspace_id: str,
    commit_message: str,
    paths: list[str] | None = None,
    include_data: bool = False,
) -> str:
    """Commit workspace changes in the host repository. Never pushes."""
    try:
        disabled = _git_commit_disabled_payload(
            "commit_workspace_changes", workspace_id
        )
        if disabled:
            return disabled
        if not commit_message.strip():
            return "ERROR: commit_message must be non-empty."

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="commit_workspace_changes", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not _workspace_verification_passed(ctx, workspace_id):
            return _json(
                {
                    "status": "blocked",
                    "reason": "verification_required_before_commit",
                    "workspace_id": workspace_id,
                    "next_step": (
                        "Run `run_workspace_verify` and fix any failures before "
                        "calling `commit_workspace_changes`. Local commits are only "
                        "allowed after a passing verification report."
                    ),
                }
            )
        workspace_prefix = workspace_root.relative_to(repo_root).as_posix()
        stagable = _collect_filtered_workspace_paths(
            repo_root,
            workspace_root,
            workspace_prefix,
            paths,
            include_data=include_data,
        )
        _enc = dict(encoding="utf-8", errors="replace")
        if not stagable:
            return _json(
                {
                    "status": "nothing_to_commit",
                    "workspace_id": workspace_id,
                    "reason": "no_stagable_paths_after_filter",
                    "filtered_out": (
                        ".memory/, __pycache__/, *.pyc, and (unless include_data=true) data/"
                    ),
                }
            )
        subprocess.run(
            ["git", "add", "--", *stagable],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            **_enc,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *stagable],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            **_enc,
        )
        if not status.stdout.strip():
            return _json(
                {
                    "status": "nothing_to_commit",
                    "workspace_id": workspace_id,
                    "reason": "git_status_empty_after_add",
                }
            )
        commit = subprocess.run(
            ["git", "commit", "-m", commit_message, "--", *stagable],
            cwd=repo_root,
            capture_output=True,
            text=True,
            **_enc,
        )
        if commit.returncode != 0:
            return f"GIT_ERROR (commit): {commit.stderr or commit.stdout}"
        return _json(
            {
                "status": "committed_locally",
                "workspace_id": workspace_id,
                "commit_message": commit_message,
                "paths_committed": stagable,
                "push": "disabled_by_umbrella_policy",
                "stdout": commit.stdout.strip(),
            }
        )
    except Exception as e:
        return f"WARNING: workspace commit error: {e}"


def _excluded_workspace_rel(ws_rel: str, *, include_data: bool) -> bool:
    """ws_rel is path under workspaces/<id>/ (posix, no leading slash)."""
    try:
        from umbrella.verification.workspace_path_policy import (
            BUILTIN_SKIP_PATH_GLOBS,
            glob_matches_any,
        )

        if glob_matches_any(ws_rel, BUILTIN_SKIP_PATH_GLOBS):
            return True
    except Exception:
        pass
    norm = ws_rel.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p]
    if ".memory" in parts:
        return True
    if ".umbrella_scratch" in parts:
        # Temp scripts created by run_python_code -- never commit them.
        return True
    if "__pycache__" in parts:
        return True
    if norm.endswith(".pyc") or norm.endswith(".pyo"):
        return True
    if not include_data and parts and parts[0] == "data":
        return True
    if ".venv" in parts or "node_modules" in parts or "vendor" in parts:
        return True
    return False


def _collect_filtered_workspace_paths(
    repo_root: Path,
    workspace_root: Path,
    workspace_prefix: str,
    paths: list[str] | None,
    *,
    include_data: bool,
) -> list[str]:
    """Repo-relative posix paths under workspace_prefix safe to `git add`."""
    candidates: list[str] = []
    if paths:
        for rel in paths:
            target = _workspace_path(workspace_root, rel)
            if target.is_file():
                candidates.append(target.relative_to(repo_root).as_posix())
            elif target.is_dir():
                # enumerate files under this subdir
                for f in target.rglob("*"):
                    if f.is_file():
                        candidates.append(f.relative_to(repo_root).as_posix())
    else:
        if not workspace_root.exists():
            return []
        for f in workspace_root.rglob("*"):
            if f.is_file():
                candidates.append(f.relative_to(repo_root).as_posix())

    seen: set[str] = set()
    out: list[str] = []
    for repo_rel in candidates:
        posix = repo_rel.replace("\\", "/")
        if not posix.startswith(workspace_prefix + "/") and posix != workspace_prefix:
            continue
        under = posix[len(workspace_prefix) :].lstrip("/")
        if _excluded_workspace_rel(under, include_data=include_data):
            continue
        if posix not in seen:
            seen.add(posix)
            out.append(posix)
    return sorted(out)


def _workspace_add_paths(
    repo_root: Path, workspace_root: Path, paths: list[str] | None
) -> list[str]:
    if not paths:
        return [workspace_root.relative_to(repo_root).as_posix()]
    result = []
    for rel in paths:
        target = _workspace_path(workspace_root, rel)
        result.append(target.relative_to(repo_root).as_posix())
    return result


def get_workspace_metrics(ctx: Any, workspace_id: str = "") -> str:
    try:
        from umbrella.telemetry import get_metrics_registry
        from umbrella.workspace_registry import WorkspaceRegistry

        repo_root = _resolve_umbrella_repo_root(ctx)
        metrics_registry = get_metrics_registry()
        registry = WorkspaceRegistry(root=repo_root)
        ws_ids = (
            [workspace_id] if workspace_id else registry.get_all_workspace_ids()[:10]
        )
        metrics = {}
        for ws_id in ws_ids:
            run_metrics = metrics_registry.get_run_metrics(ws_id)
            metrics[ws_id] = {
                "total_runs": run_metrics.total_runs,
                "successful_runs": run_metrics.successful_runs,
                "failed_runs": run_metrics.failed_runs,
                "partial_tasks": run_metrics.partial_tasks,
                "total_cost_usd": run_metrics.total_cost_usd,
                "avg_score": run_metrics.average_score,
            }
        return _json(metrics)
    except Exception as e:
        log.error("Metrics fetch failed: %s", e, exc_info=True)
        return f"WARNING: metrics error: {e}"


def get_workspace_logs(
    ctx: Any, workspace_id: str, run_id: str = "", tail: int = 100
) -> str:
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        instances_dir = repo_root / "workspaces" / workspace_id / "instances"
        if not instances_dir.exists():
            return f"No instances found for {workspace_id}"
        latest_log = _find_workspace_log(instances_dir, run_id=run_id)
        if not latest_log:
            return f"No logs found for {workspace_id}"
        lines = latest_log.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, min(int(tail), 2000)) :])
    except Exception as e:
        return f"WARNING: error reading logs: {e}"


def _find_workspace_log(instances_dir: Path, *, run_id: str = "") -> Path | None:
    latest_log = None
    latest_time = 0.0
    for instance_dir in instances_dir.iterdir():
        if run_id:
            log_file = instance_dir / "runs" / run_id / "agent.log"
            if log_file.exists():
                return log_file
        runs_dir = instance_dir / "runs"
        if not runs_dir.exists():
            continue
        for run_dir in runs_dir.iterdir():
            log_file = run_dir / "agent.log"
            if log_file.exists() and log_file.stat().st_mtime > latest_time:
                latest_time = log_file.stat().st_mtime
                latest_log = log_file
    return latest_log


def update_workspace_from_instance(
    ctx: Any,
    workspace_id: str,
    instance_name: str,
    files_to_copy: list[str],
) -> str:
    try:
        from umbrella.control_plane.workspace_code_update import (
            update_seed_workspace_from_instance,
        )
        from umbrella.workspace_runtime.models import WorkspaceInstance

        repo_root = _resolve_umbrella_repo_root(ctx)
        instance_path = (
            repo_root / "workspaces" / workspace_id / "instances" / instance_name
        )
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="update_workspace_from_instance", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not instance_path.exists():
            return f"Instance not found: {instance_name}"
        instance = WorkspaceInstance(
            path=instance_path,
            workspace_id=workspace_id,
            seed_workspace_id=workspace_id,
        )
        result = update_seed_workspace_from_instance(
            instance=instance,
            files_to_update=files_to_copy,
            seed_path=seed_path,
            create_backup=True,
        )
        if not result.applied:
            return f"Copy failed: {result.error or 'no files copied'}"
        return "Copied files:\n" + "\n".join(
            f"- {file}" for file in result.updated_files
        )
    except Exception as e:
        log.error("Instance-to-seed update failed: %s", e, exc_info=True)
        return f"WARNING: instance promotion error: {e}"


def probe_input_file(ctx: Any, path: str, workspace_id: str = "") -> str:
    """Probe an input file's actual format vs its extension.

    Tier 5.1 — read-only tool that returns the result of
    :func:`umbrella.utils.file_probe.probe_file` as JSON. The path is
    resolved against the active workspace and is **not** allowed to
    escape it. Use this before picking a parser for any input file
    mentioned in TASK_MAIN — a ``.docx`` that's actually a UTF-8 text
    dump is the classic failure mode this catches.
    """

    try:
        from umbrella.utils.file_probe import probe_file
    except Exception as exc:
        log.error("probe_input_file: probe_file import failed: %s", exc, exc_info=True)
        return f"WARNING: probe_input_file unavailable: {exc}"
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        ws_id = (workspace_id or "").strip() or (
            str(getattr(ctx, "active_workspace_id", "") or "")
        )
        if ws_id:
            workspace_root = _workspace_root(repo_root, ws_id, ctx)
            stripped = _strip_workspace_prefix(ws_id, path)
            target = _workspace_path(workspace_root, stripped)
        else:
            raw = str(path or "").strip()
            if not raw:
                return "WARNING: probe_input_file requires a path."
            target = Path(raw).resolve()
        result = probe_file(target)
        return _json(result.to_dict())
    except ValueError as exc:
        return f"WARNING: probe_input_file rejected path: {exc}"
    except Exception as exc:
        log.error("probe_input_file failed: %s", exc, exc_info=True)
        return f"WARNING: probe_input_file error: {exc}"


def _resolve_drive_root(ctx: Any) -> Path:
    drive_root = getattr(ctx, "drive_root", None)
    if drive_root:
        return Path(drive_root)
    repo_root = _resolve_umbrella_repo_root(ctx)
    return Path(repo_root) / ".umbrella" / "ouroboros_drive"


def bg_start(
    ctx: Any,
    workspace_id: str,
    argv: list[str] | str | None = None,
    command: list[str] | str | None = None,
    subdir: str = "",
    label: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Spawn a long-running command (e.g. uvicorn) detached from this tool call.

    Returns ``job_id`` immediately. Use ``bg_status`` / ``bg_tail`` to observe
    and ``bg_kill`` to stop. Logs land in ``<drive>/logs/bg/<job_id>.log``.
    """
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        subdir = _strip_workspace_prefix(workspace_id, subdir)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="bg_start", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        raw_command = argv if argv is not None else command
        if raw_command is None:
            return _json(
                {"status": "invalid_command", "hint": "Pass `argv` or `command`."}
            )
        cmd, norm_err = _try_normalize_command(raw_command)
        if norm_err:
            return _json({"status": "invalid_command", "hint": norm_err})
        cmd, subdir = _strip_workspace_cd_argv(cmd, workspace_id, subdir)
        cmd = _strip_posix_timeout_wrapper(cmd)
        cwd = _workspace_path(workspace_root, subdir)

        drive_root = _resolve_drive_root(ctx)
        job = _bg_jobs.start_background(
            drive_root,
            argv=cmd,
            cwd=cwd,
            label=label or (cmd[0] if cmd else ""),
            env_overrides=dict(env or {}),
        )
        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="bg_start",
                summary=f"bg job {job.job_id} pid={job.pid}: {' '.join(cmd)[:160]}",
                details=f"label={job.label}\ncwd={job.cwd}\nlog={job.log_path}",
                severity="info",
                tags="background,server,terminal",
            )
        except Exception:
            log.debug("bg_start memory log failed", exc_info=True)
        return _json(
            {
                "status": "started",
                "job_id": job.job_id,
                "pid": job.pid,
                "log_path": job.log_path,
                "cwd": job.cwd,
                "argv": job.argv,
                "next_step": (
                    "Wait ~1-3s, then call bg_status / bg_tail to confirm the process "
                    "actually came up. If it crashed, the log will show the traceback."
                ),
            }
        )
    except Exception as e:
        log.error("bg_start failed: %s", e, exc_info=True)
        return f"WARNING: bg_start error: {e}"


def bg_status(ctx: Any, job_id: str) -> str:
    try:
        return _json(_bg_jobs.status(_resolve_drive_root(ctx), job_id))
    except Exception as e:
        return f"WARNING: bg_status error: {e}"


def bg_tail(ctx: Any, job_id: str, lines: int = 200) -> str:
    try:
        return _json(_bg_jobs.tail(_resolve_drive_root(ctx), job_id, lines=int(lines)))
    except Exception as e:
        return f"WARNING: bg_tail error: {e}"


def bg_list(ctx: Any) -> str:
    try:
        jobs = _bg_jobs.list_jobs(_resolve_drive_root(ctx))
        return _json([{**j.to_dict(), "alive": _bg_jobs.is_alive(j.pid)} for j in jobs])
    except Exception as e:
        return f"WARNING: bg_list error: {e}"


def bg_kill(ctx: Any, job_id: str) -> str:
    try:
        result = _bg_jobs.kill(_resolve_drive_root(ctx), job_id)
        try:
            record_workspace_event(
                ctx,
                workspace_id="_bg",
                event_type="bg_kill",
                summary=f"bg_kill {job_id} pid={result.get('pid')}",
                details=_json(result),
                severity="info",
                tags="background,server,terminal",
            )
        except Exception:
            log.debug("bg_kill memory log failed", exc_info=True)
        return _json(result)
    except Exception as e:
        return f"WARNING: bg_kill error: {e}"


__all__ = [
    '_git_commit_disabled_payload',
    'commit_workspace_changes',
    '_excluded_workspace_rel',
    '_collect_filtered_workspace_paths',
    '_workspace_add_paths',
    'get_workspace_metrics',
    'get_workspace_logs',
    '_find_workspace_log',
    'update_workspace_from_instance',
    'probe_input_file',
    '_resolve_drive_root',
    'bg_start',
    'bg_status',
    'bg_tail',
    'bg_list',
    'bg_kill',
]
