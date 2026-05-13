"""Ouroboros-first Umbrella entrypoint.

Umbrella builds the workspace mission and launches Ouroboros. Ouroboros does
the actual work through Umbrella tools: GMAS retrieval, workspace access,
memory, tests/E2E, and local-only commits.

Operator UI: ``uv run bridge`` or ``uv run python -m umbrella.web_bridge`` (React + ``/api/*``; build UI first: ``yarn build`` in ``web/``).
"""

import argparse
import io
import logging
import sys
from pathlib import Path
from typing import Any

_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from umbrella.config import load_runtime_config
from umbrella.config import (
    DEFAULT_DASHBOARD_QUALITY_THRESHOLD,
    DEFAULT_DASHBOARD_TIMEOUT_HOURS,
    DEFAULT_DASHBOARD_WORKSPACE_ID,
)
from umbrella.control_plane.ouroboros_integration import run_ouroboros_improvement_sync
from umbrella.control_plane.sandbox_self_edit import recover_orphan_sandbox_stashes
from umbrella.env import get_llm_env_config, load_env
from umbrella.orchestration.ouroboros_task import (
    polymarket_e2e_task,
    render_retry_prompt,
    render_workspace_prompt,
)
from umbrella.orchestration.task_input import TaskInputResolution, resolve_task_text
from umbrella.orchestration.status import write_status


def _windows_utf8_stream(stream):
    if sys.platform != "win32":
        return stream
    if type(stream).__module__.startswith("_pytest."):
        return stream
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return stream
    encoding = str(getattr(stream, "encoding", "") or "").replace("-", "").lower()
    if encoding == "utf8":
        return stream
    return io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")


sys.stdout = _windows_utf8_stream(sys.stdout)
sys.stderr = _windows_utf8_stream(sys.stderr)


def _ensure_log_dir() -> None:
    Path(".umbrella").mkdir(parents=True, exist_ok=True)


_ensure_log_dir()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(".umbrella/app_ouroboros.log", encoding="utf-8"),
    ],
)

log = logging.getLogger(__name__)


def _failed_verification_signature(result: dict[str, Any]) -> str:
    report = result.get("verification_report") if isinstance(result, dict) else None
    sweep = result.get("sweep_report") if isinstance(result, dict) else None
    failed_required: list[str] = []
    if isinstance(report, dict):
        for item in report.get("results") or []:
            if not isinstance(item, dict):
                continue
            if item.get("optional"):
                continue
            if str(item.get("status") or "") != "passed":
                failed_required.append(str(item.get("name") or ""))
    failed_required.sort()
    cleanup_targets: list[str] = []
    if isinstance(sweep, dict):
        for item in sweep.get("blocking_noise") or []:
            if isinstance(item, dict) and item.get("path"):
                cleanup_targets.append(str(item.get("path")))
        for item in sweep.get("missing_required") or []:
            cleanup_targets.append(str(item))
        cleanup_targets.sort()
    return "|".join(
        [
            str(result.get("status") or ""),
            str(result.get("workspace_write_tool_calls") or 0),
            str(result.get("llm_tool_invocations") or 0),
            ",".join(failed_required),
            ",".join(cleanup_targets),
        ]
    )


def _verification_failure_is_repairable_config(result: dict[str, Any]) -> bool:
    report = result.get("verification_report") if isinstance(result, dict) else None
    if not isinstance(report, dict):
        return False
    if report.get("repairable") or report.get("spec_error"):
        return True
    summary = str(report.get("summary") or "").lower()
    return (
        "verification spec is invalid" in summary
        or "no verification steps declared or auto-detected" in summary
    )


def _resolve_app_live_mode(
    repo_root: Path,
    *,
    prefer_live: bool = False,
    force_mock: bool = False,
) -> tuple[bool, str]:
    load_env(repo_root=repo_root)
    _llm_model, llm_api_key, _llm_base_url = get_llm_env_config()

    if force_mock:
        return False, "forced by --mock"
    if llm_api_key:
        return (
            (True, "enabled by --live")
            if prefer_live
            else (True, "auto-enabled from .env")
        )
    if prefer_live:
        log.warning(
            "Live mode requested but no LLM credentials found; starting in degraded mode"
        )
    return False, "no live credentials found"


def _apply_max_rounds_env(max_rounds: int | None) -> None:
    """Translate the Umbrella ``--max-rounds`` flag into ``OUROBOROS_MAX_ROUNDS``.

    Semantics:

    * ``max_rounds is None`` — caller did not pass the flag; we preserve
      whatever ``OUROBOROS_MAX_ROUNDS`` already says (default 200 inside the
      Ouroboros loop).
    * ``max_rounds <= 0`` — unlimited rounds. We export ``OUROBOROS_MAX_ROUNDS=0``
      and the Ouroboros loop interprets the non-positive value as "no cap".
    * ``max_rounds > 0`` — explicit ceiling, exported as-is.

    This is the single point where Umbrella's "no limits" CLI stance gets
    aligned with the Ouroboros internal ``MAX_ROUNDS`` gate, so that
    ``--timeout-hours 0 --max-budget`` (effectively unbounded) is no longer
    silently capped at 200 LLM rounds.
    """
    import os as _os

    if max_rounds is None:
        existing = _os.environ.get("OUROBOROS_MAX_ROUNDS")
        if existing is not None:
            log.info("Ouroboros max_rounds: inherited %s from environment", existing)
        return

    if max_rounds <= 0:
        _os.environ["OUROBOROS_MAX_ROUNDS"] = "0"
        log.info("Ouroboros max_rounds: UNLIMITED (--max-rounds=%d)", max_rounds)
        return

    _os.environ["OUROBOROS_MAX_ROUNDS"] = str(int(max_rounds))
    log.info("Ouroboros max_rounds: capped at %d", int(max_rounds))


def _clear_stop_requests(repo_root: Path) -> None:
    """Remove stale operator stop requests before a fresh app run starts."""
    for path in (
        repo_root / ".umbrella" / "launcher" / "stop_requested.json",
        repo_root / ".umbrella" / "ouroboros_drive" / "state" / "stop_requested.json",
    ):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            log.debug("Failed to clear stale stop request %s", path, exc_info=True)


def _resolve_workspace(repo_root: Path, workspace_arg: str) -> Path:
    path = Path(workspace_arg)
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Workspace path not found: {path}")
    return path


def _resolve_launch_task(
    args: argparse.Namespace, workspace_path: Path
) -> TaskInputResolution:
    task_parts = []
    if args.polymarket_e2e:
        task_parts.append(polymarket_e2e_task())
    if args.task_file:
        task_parts.append(Path(args.task_file).read_text(encoding="utf-8"))
    if args.task:
        task_parts.append(args.task)
    explicit = "\n\n".join(part.strip() for part in task_parts if part.strip())
    return resolve_task_text(workspace_path, explicit_task_text=explicit or None)


def _task_terminal(result: dict[str, Any], *, status: str) -> dict[str, Any]:
    critic = result.get("critic_review")
    critic_verdict = critic.get("verdict") if isinstance(critic, dict) else ""
    return {
        "task_id": result.get("task_id"),
        "final_status": status,
        "critic_verdict": critic_verdict,
        "verified": status == "verified",
        "finished_at": __import__("time").time(),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch Ouroboros through Umbrella tools against a workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "workspace", nargs="?", default=f"workspaces/{DEFAULT_DASHBOARD_WORKSPACE_ID}"
    )
    parser.add_argument(
        "--task", help="Task text. Defaults to TASK_MAIN.md from the workspace."
    )
    parser.add_argument("--task-file", type=Path, help="Read task text from this file.")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--quality-threshold", type=float, default=DEFAULT_DASHBOARD_QUALITY_THRESHOLD
    )
    parser.add_argument(
        "--timeout-hours", type=float, default=DEFAULT_DASHBOARD_TIMEOUT_HOURS
    )
    parser.add_argument("--max-budget", type=float, default=None)
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help=(
            "Hard cap on LLM rounds per Ouroboros attempt. Sets "
            "OUROBOROS_MAX_ROUNDS for the child loop. Use 0 (or any "
            "non-positive value) for unlimited. When omitted, the Ouroboros "
            "default of 200 (or whatever OUROBOROS_MAX_ROUNDS is in the "
            "environment) is preserved."
        ),
    )
    parser.add_argument("--live", action="store_true", help="Prefer live LLM mode")
    parser.add_argument("--mock", action="store_true", help="Force degraded/mock mode")
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Deprecated: legacy HTTP dashboard removed; flag is ignored.",
    )
    parser.add_argument(
        "--open-legacy-dashboard",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--polymarket-e2e",
        action="store_true",
        help="Append the Polymarket E2E validation task",
    )
    parser.add_argument(
        "--max-verify-retries",
        type=int,
        default=20,
        help=(
            "Number of extra Ouroboros attempts when runtime verification fails. "
            "0 disables retrying (one attempt total). Default: 20."
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip runtime verification entirely (legacy behavior).",
    )
    parser.add_argument(
        "--verification-timeout-seconds",
        type=int,
        default=None,
        help="Overall ceiling (seconds) for the verification pass.",
    )
    parser.add_argument(
        "--require-instance",
        dest="require_instance",
        action="store_true",
        default=True,
        help="Require a task instance before running (default).",
    )
    parser.add_argument(
        "--allow-seed-writes",
        dest="require_instance",
        action="store_false",
        help="Allow Ouroboros to operate directly on the seed workspace if an instance cannot be created.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    repo_root = (args.repo_root or Path.cwd()).resolve()

    _apply_max_rounds_env(args.max_rounds)
    _clear_stop_requests(repo_root)

    try:
        recovered = recover_orphan_sandbox_stashes(repo_root)
    except Exception:
        log.debug("Orphan sandbox-stash recovery failed (non-fatal)", exc_info=True)
        recovered = []
    if recovered:
        log.warning(
            "Recovered %d orphan sandbox stash(es) from a previous run: %s. "
            "Inspect `git status` and drop them with `git stash drop` once verified.",
            len(recovered),
            ", ".join(recovered),
        )

    workspace_path = _resolve_workspace(repo_root, args.workspace)
    workspace_id = workspace_path.name
    resolved_live, live_reason = _resolve_app_live_mode(
        repo_root,
        prefer_live=args.live,
        force_mock=args.mock,
    )

    runtime_config = load_runtime_config(
        overrides={
            "max_budget_usd": args.max_budget,
            "quality_completion_threshold": args.quality_threshold,
            "human_review_stages": [],
            "human_review_timeout_seconds": 0,
            "max_duration_seconds": None,
            "max_iterations": None,
        }
    )
    task_resolution = _resolve_launch_task(args, workspace_path)
    task_text = task_resolution.task_text
    timeout_seconds = None if args.timeout_hours <= 0 else args.timeout_hours * 3600

    verify_enabled = not args.no_verify
    max_verify_retries = max(0, int(args.max_verify_retries))
    max_attempts = 1 if not verify_enabled else max_verify_retries + 1

    if args.open_legacy_dashboard:
        log.warning("--open-legacy-dashboard is ignored (legacy dashboard removed).")

    write_status(
        repo_root,
        active=True,
        status="running",
        workspace_id=workspace_id,
        workspace_path=str(workspace_path),
        live_llm=resolved_live,
        live_reason=live_reason,
        dashboard_url="",
        task_preview=task_text[:1200],
        timeout_seconds=timeout_seconds,
        verify_enabled=verify_enabled,
        max_verify_retries=max_verify_retries,
    )

    if task_resolution.task_missing:
        result = {
            "status": task_resolution.missing_status or "missing_task_main",
            "task_id": "",
            "workspace_id": workspace_id,
            "final_message": task_resolution.error,
            "task_source": task_resolution.task_source,
            "task_hash": task_resolution.task_hash,
            "task_missing": True,
        }
        write_status(
            repo_root,
            active=False,
            status=result["status"],
            workspace_id=workspace_id,
            workspace_path=str(workspace_path),
            task_preview="",
            result=result,
        )
        log.error("%s", task_resolution.error)
        return 1

    log.info("=" * 70)
    log.info("UMBRELLA TOOL LAYER + OUROBOROS")
    log.info("Workspace: %s", workspace_id)
    log.info("Mode: %s (%s)", "LIVE LLM" if resolved_live else "degraded", live_reason)
    log.info(
        "Timeout: %s", "none" if timeout_seconds is None else f"{timeout_seconds:.0f}s"
    )
    log.info("Quality threshold: %.2f", runtime_config.quality_completion_threshold)
    log.info(
        "Verification: %s (max %d attempts)",
        "enabled" if verify_enabled else "disabled",
        max_attempts,
    )
    log.info("=" * 70)

    last_result: dict[str, Any] | None = None
    previous_status = ""
    previous_verification_report: dict[str, Any] | None = None
    previous_final_message = ""
    previous_failed_signature = ""
    repeated_failed_signature_count = 0

    try:
        for attempt in range(1, max_attempts + 1):
            retry_context = render_retry_prompt(
                attempt=attempt,
                max_attempts=max_attempts,
                previous_status=previous_status,
                verification_report=previous_verification_report,
                previous_final_message=previous_final_message,
            )

            # Pre-warm skill detection BEFORE rendering the prompt so the
            # `### Detected skills` block (and the matching skill artifact)
            # is in place for the very first attempt. Without this, the
            # launcher would only sync skills *inside* the task run, after
            # the prompt was already shipped to the model -- which is why
            # multi_agent_gmas previously never made it into Prior knowledge.
            try:
                from umbrella.integration.ouroboros_bridge import (
                    prepare_active_skills_for_workspace,
                )

                prepare_active_skills_for_workspace(
                    repo_root,
                    workspace_id,
                    task_input=task_text,
                )
            except Exception:
                log.debug(
                    "prepare_active_skills_for_workspace failed (non-fatal)",
                    exc_info=True,
                )

            task_prompt = render_workspace_prompt(
                repo_root=repo_root,
                workspace_id=workspace_id,
                task_text=task_text,
                quality_threshold=runtime_config.quality_completion_threshold,
                retry_context=retry_context,
            )
            task_prompt = (
                f"<!-- task_source={task_resolution.task_source} "
                f"task_hash={task_resolution.task_hash} "
                f"task_missing={str(task_resolution.task_missing).lower()} -->\n"
                f"{task_prompt}"
            )

            log.info("Ouroboros attempt %d/%d", attempt, max_attempts)
            write_status(
                repo_root,
                active=True,
                status=f"running_attempt_{attempt}",
                workspace_id=workspace_id,
                attempt=attempt,
                max_attempts=max_attempts,
            )

            result = run_ouroboros_improvement_sync(
                repo_root=repo_root,
                task_description=task_prompt,
                workspace_id=workspace_id,
                use_live_llm=resolved_live,
                timeout_seconds=timeout_seconds,
                promote=True,
                verify=verify_enabled,
                verification_timeout_seconds=args.verification_timeout_seconds,
                require_instance=args.require_instance,
                task_input_metadata=task_resolution.metadata(),
            )
            last_result = result
            status = str(result.get("status") or "unknown")
            log.info(
                "Attempt %d/%d finished with status=%s", attempt, max_attempts, status
            )

            if status == "verified":
                break
            if status == "complete" and not verify_enabled:
                break
            if status in ("error", "incomplete") and attempt == max_attempts:
                break
            if (
                status in {"failed_verification", "failed_hygiene"}
                and attempt < max_attempts
            ):
                signature = _failed_verification_signature(result)
                if signature and signature == previous_failed_signature:
                    repeated_failed_signature_count += 1
                else:
                    previous_failed_signature = signature
                    repeated_failed_signature_count = 1
                previous_status = status
                previous_verification_report = result.get("verification_report")
                previous_final_message = str(result.get("final_message") or "")
                if (
                    repeated_failed_signature_count >= 2
                    and not _verification_failure_is_repairable_config(result)
                ):
                    log.warning(
                        "Verification failure signature repeated (%d/%d); "
                        "stopping retries early to avoid churn.",
                        repeated_failed_signature_count,
                        max_attempts,
                    )
                    break
                log.warning(
                    "Verification failed on attempt %d/%d; retrying with failure report injected",
                    attempt,
                    max_attempts,
                )
                continue
            if status in ("error", "incomplete") and attempt < max_attempts:
                previous_status = status
                previous_verification_report = result.get("verification_report")
                previous_final_message = str(
                    result.get("final_message") or result.get("error") or ""
                )
                log.warning(
                    "Attempt %d/%d ended with status=%s; retrying",
                    attempt,
                    max_attempts,
                    status,
                )
                continue
            break

        result = last_result or {"status": "error", "error": "no attempts executed"}
        status = str(result.get("status") or "unknown")

        write_status(
            repo_root,
            active=False,
            status=status,
            workspace_id=workspace_id,
            result=_summarize_result(result),
            task_id=result.get("task_id"),
            task_terminal=_task_terminal(result, status=status),
        )
        print(_format_result(result), flush=True)

        if verify_enabled:
            if status == "verified":
                return 0
            return 1
        return 0 if status == "complete" else 1
    except KeyboardInterrupt:
        write_status(
            repo_root,
            active=False,
            status="interrupted",
            workspace_id=workspace_id,
            task_terminal={
                "final_status": "interrupted",
                "verified": False,
                "finished_at": __import__("time").time(),
            },
        )
        log.info("Interrupted by user")
        return 130
    except Exception as exc:
        write_status(
            repo_root,
            active=False,
            status="error",
            workspace_id=workspace_id,
            error=str(exc),
            task_terminal={
                "final_status": "error",
                "verified": False,
                "finished_at": __import__("time").time(),
            },
        )
        log.error("Ouroboros run failed: %s", exc, exc_info=True)
        return 1


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    verification = result.get("verification_report") or {}
    verification_summary: dict[str, Any] = {}
    if verification:
        verification_summary = {
            "passed": verification.get("passed"),
            "pass_rate": verification.get("pass_rate"),
            "skipped": verification.get("skipped"),
            "results": [
                {
                    "name": r.get("name"),
                    "kind": r.get("kind"),
                    "status": r.get("status"),
                    "exit_code": r.get("exit_code"),
                    "optional": r.get("optional"),
                }
                for r in (verification.get("results") or [])
            ],
        }

    return {
        "status": result.get("status"),
        "task_id": result.get("task_id"),
        "workspace_write_tool_calls": result.get("workspace_write_tool_calls"),
        "llm_tool_invocations": result.get("llm_tool_invocations"),
        "events_count": result.get("events_count"),
        "changes_made": result.get("changes_made", []),
        "promoted_files": result.get("promoted_files", []),
        "cost_usd": result.get("cost_usd", 0),
        "instance_path": result.get("instance_path"),
        "final_message": _truncate_summary_text(result.get("final_message")),
        "error": result.get("error"),
        "verification": verification_summary,
        "critic_review": result.get("critic_review"),
        "quality_telemetry": result.get("quality_telemetry"),
        "promotion_blocked_reason": result.get("promotion_blocked_reason"),
    }


def _persist_verification_artifact(repo_root: Path, result: dict[str, Any]) -> None:
    """Compatibility helper for tests/tools that still call it directly."""
    report = result.get("verification_report")
    task_id = result.get("task_id")
    if not report or not task_id:
        return
    try:
        import json
        from umbrella.integration.ouroboros_launcher import resolve_drive_root

        out_dir = (
            resolve_drive_root(
                repo_root, str(result.get("workspace_id") or "")
            ).resolve()
            / "task_results"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{task_id}.verification.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        log.debug("Failed to persist verification artifact", exc_info=True)


def _persist_critic_artifact(repo_root: Path, result: dict[str, Any]) -> None:
    """Compatibility helper for tests/tools that still call it directly."""
    report = result.get("critic_review")
    task_id = result.get("task_id")
    if not report or not task_id:
        return
    try:
        import json
        from umbrella.integration.ouroboros_launcher import resolve_drive_root

        out_dir = (
            resolve_drive_root(
                repo_root, str(result.get("workspace_id") or "")
            ).resolve()
            / "task_results"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{task_id}.critic.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        log.debug("Failed to persist critic artifact", exc_info=True)


def _truncate_summary_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated]"


def _format_result(result: dict[str, Any]) -> str:
    summary = _summarize_result(result)
    return "\nOuroboros result:\n" + json_dumps(summary)


def json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    sys.exit(main())
