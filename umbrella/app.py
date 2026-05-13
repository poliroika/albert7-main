"""
Umbrella Manager - Main Entry Point (DEPRECATED)

This was the old Umbrella-manager entrypoint that used ControlPlaneEngine /
run_manager_task.  The Ouroboros-first architecture replaces this with:

  - umbrella/app_ouroboros.py   (primary entrypoint)
  - run_ouroboros_self_improve.py  (continuous loop)

This file is kept for backward-compatibility of imports used by tests and
other internal modules.  New code should NOT use this entrypoint.
"""

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from umbrella.env import get_llm_env_config, load_env
from umbrella.integration import (
    create_demo_runner,
    DemoScenario,
)
from umbrella.workspace_registry.discovery import load_workspace_config
from umbrella.integration.reporting import (
    save_report,
)
from umbrella.control_plane.engine import HumanCheckpoint
from umbrella.control_plane.human_checkpoints import (
    load_human_checkpoint_request,
)
from umbrella.control_plane.task_updates import queue_runtime_task_update
from umbrella.orchestration.task_input import resolve_task_text

Path(".umbrella").mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(".umbrella/app.log"),
    ],
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedTaskRequest:
    """Resolved CLI task source."""

    task_input: str
    workspace_id: str | None
    workspace_path: Path | None
    task_file: Path | None
    source: str


@dataclass(frozen=True)
class PendingCheckpointRecord:
    """Human-review checkpoint visible from CLI or dashboard."""

    checkpoint_id: str
    task_id: str
    checkpoint_type: str
    description: str
    source: str
    status: str
    created_at: float
    task_summary: str | None = None
    metadata: dict[str, Any] | None = None


def _resolve_app_live_mode(
    repo_root: Path,
    *,
    prefer_live: bool = False,
    force_mock: bool = False,
) -> tuple[bool, str]:
    """Resolve CLI live/degraded mode from flags plus .env-backed environment."""
    load_env(repo_root=repo_root)
    _llm_model, llm_api_key, _llm_base_url = get_llm_env_config()

    if force_mock:
        return False, "forced by --mock"

    if llm_api_key:
        if prefer_live:
            return True, "enabled by --live"
        return True, "auto-enabled from .env"

    if prefer_live:
        log.warning(
            "Live mode requested but no LLM credentials were found; starting in degraded mode"
        )
        return False, "no live credentials found"

    return False, "no live credentials found"


def _normalize_runtime_limit(limit: int | float) -> int | float | None:
    """Interpret zero-or-negative CLI limits as unlimited."""
    return None if limit <= 0 else limit


def _format_limit_for_log(limit: int | float | None, unit: str = "") -> str:
    """Render runtime limits in logs without leaking None semantics to users."""
    if limit is None:
        return "unlimited"
    return f"{limit}{unit}"


def _iter_path_candidates(raw_value: str, repo_root: Path) -> list[Path]:
    """Return likely filesystem interpretations for a user-provided CLI value."""
    given = Path(raw_value).expanduser()
    candidates = []
    if given.is_absolute():
        candidates.append(given.resolve())
    else:
        candidates.append((Path.cwd() / given).resolve())
        candidates.append((repo_root / given).resolve())

    ordered: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def _select_task_file(workspace_root: Path) -> tuple[Path | None, str | None]:
    """Find the canonical task contract file for a workspace root."""
    task_main_name = None
    workspace_toml = workspace_root / "workspace.toml"
    if workspace_toml.exists():
        ref = load_workspace_config(workspace_toml)
        if ref is not None:
            task_main_name = ref.task_main_file

    candidates = []
    if task_main_name:
        candidates.append(workspace_root / task_main_name)
    candidates.append(workspace_root / "TASK_MAIN.md")

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved, task_main_name
    return None, task_main_name


def _resolve_task_request(
    raw_value: str, repo_root: Path, workspace_arg: str | None = None
) -> ResolvedTaskRequest:
    """Resolve a CLI value into either inline task text or a workspace-backed task file."""
    if not raw_value.strip():
        raise ValueError("Task description or workspace path is required")

    for candidate in _iter_path_candidates(raw_value, repo_root):
        if not candidate.exists():
            continue

        if candidate.is_file():
            workspace_root = candidate.parent
            workspace_id = workspace_arg
            workspace_toml = workspace_root / "workspace.toml"
            if workspace_toml.exists():
                ref = load_workspace_config(workspace_toml)
                if ref is not None:
                    workspace_id = ref.workspace_id

            resolution = resolve_task_text(
                candidate.parent,
                explicit_task_text=candidate.read_text(encoding="utf-8"),
            )
            content = resolution.task_text
            return ResolvedTaskRequest(
                task_input=content,
                workspace_id=workspace_id,
                workspace_path=workspace_root,
                task_file=candidate,
                source="task_file",
            )

        if candidate.is_dir():
            task_file, declared_name = _select_task_file(candidate)
            if task_file is None:
                expected = declared_name or "TASK_MAIN.md"
                raise FileNotFoundError(
                    f"Workspace path {candidate} does not contain {expected}"
                )

            workspace_id = workspace_arg
            workspace_toml = candidate / "workspace.toml"
            if workspace_toml.exists():
                ref = load_workspace_config(workspace_toml)
                if ref is not None:
                    workspace_id = ref.workspace_id

            resolution = resolve_task_text(candidate, task_file_name=task_file.name)
            if resolution.task_missing:
                raise FileNotFoundError(resolution.error)
            content = resolution.task_text
            return ResolvedTaskRequest(
                task_input=content,
                workspace_id=workspace_id,
                workspace_path=candidate,
                task_file=task_file,
                source="workspace_path",
            )

    return ResolvedTaskRequest(
        task_input=raw_value,
        workspace_id=workspace_arg,
        workspace_path=None,
        task_file=None,
        source="inline_task",
    )


def _exit_code_for_status(status: str, *, demo_mode: bool = False) -> int:
    """Map manager status to a CLI exit code.

    Returns 0 only for fully completed tasks; partial and failed always non-zero.
    """
    _ = demo_mode  # Backward-compatible keyword for older callers/tests.
    if status in ("complete", "success"):
        return 0
    if status == "partial":
        return 2
    return 1


def _resolve_live_task_id(
    control_state_dir: Path, requested_task_id: str | None = None
) -> str:
    """Resolve an explicit task id or the most recent active task checkpoint."""
    raw = (requested_task_id or "current").strip()
    if raw and raw.lower() not in {"current", "latest"}:
        return raw

    checkpoint_dir = Path(control_state_dir) / "checkpoints"
    if not checkpoint_dir.exists():
        raise ValueError("No task checkpoints found; cannot resolve current task")

    candidates: list[tuple[float, str]] = []
    for path in checkpoint_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = str(payload.get("status") or "")
        state = payload.get("state") or {}
        phase = str(state.get("phase") or "")
        if status in {"complete", "failed", "blocked"}:
            continue
        if phase in {"task_complete", "task_failed", "task_blocked"}:
            continue
        updated_at = float(
            state.get("updated_at")
            or payload.get("started_at")
            or payload.get("created_at")
            or 0.0
        )
        task_id = str(payload.get("id") or state.get("task_id") or "").strip()
        if task_id:
            candidates.append((updated_at, task_id))

    if not candidates:
        raise ValueError("No active manager task found; pass --task-id explicitly")

    candidates.sort()
    return candidates[-1][1]


def _load_task_summary(control_state_dir: Path, task_id: str) -> str | None:
    """Load a short task summary from the persisted task checkpoint."""
    checkpoint_path = Path(control_state_dir) / "checkpoints" / f"{task_id}.json"
    if not checkpoint_path.exists():
        return None

    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    brief = payload.get("brief") or {}
    if isinstance(brief, dict):
        summary = str(brief.get("summary") or brief.get("original_input") or "").strip()
        if summary:
            return summary
    return None


def list_pending_checkpoints(
    *,
    repo_root: Path | None = None,
    control_state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return pending human-review checkpoints across all supported stores."""
    effective_repo_root = (repo_root or Path.cwd()).resolve()
    effective_state_dir = (
        control_state_dir or effective_repo_root / ".umbrella"
    ).resolve()

    records: list[PendingCheckpointRecord] = []

    human_checkpoint_dir = effective_state_dir / "human_checkpoints"
    if human_checkpoint_dir.exists():
        for path in human_checkpoint_dir.glob("*.json"):
            request = load_human_checkpoint_request(path.stem, human_checkpoint_dir)
            if request is None or str(request.status.value) != "pending":
                continue

            metadata = dict(request.metadata)
            records.append(
                PendingCheckpointRecord(
                    checkpoint_id=request.id,
                    task_id=request.task_id,
                    checkpoint_type=request.checkpoint_type,
                    description=request.description,
                    source="prompt_request",
                    status=request.status.value,
                    created_at=request.created_at,
                    task_summary=_load_task_summary(
                        effective_state_dir, request.task_id
                    ),
                    metadata=metadata,
                )
            )

    checkpoint_dir = effective_state_dir / "checkpoints"
    if checkpoint_dir.exists():
        for path in checkpoint_dir.glob("*.json"):
            checkpoint = HumanCheckpoint.load(path.stem, checkpoint_dir)
            if checkpoint is None or checkpoint.status != "pending":
                continue

            records.append(
                PendingCheckpointRecord(
                    checkpoint_id=checkpoint.id,
                    task_id=checkpoint.task_id,
                    checkpoint_type=checkpoint.checkpoint_type,
                    description=checkpoint.description,
                    source="engine_checkpoint",
                    status=checkpoint.status,
                    created_at=float(checkpoint.created_at or 0.0),
                    task_summary=_load_task_summary(
                        effective_state_dir, checkpoint.task_id
                    ),
                    metadata=dict(checkpoint.proposed_change or {}),
                )
            )

    records.sort(key=lambda item: (item.created_at, item.checkpoint_id), reverse=True)
    return [
        {
            "checkpoint_id": item.checkpoint_id,
            "task_id": item.task_id,
            "checkpoint_type": item.checkpoint_type,
            "description": item.description,
            "source": item.source,
            "status": item.status,
            "created_at": item.created_at,
            "task_summary": item.task_summary,
            "metadata": item.metadata or {},
        }
        for item in records
    ]


def inject_runtime_instruction(
    *,
    instruction: str,
    repo_root: Path | None = None,
    control_state_dir: Path | None = None,
    task_id: str | None = None,
    source: str = "terminal",
) -> dict[str, str]:
    """Queue a live human instruction for an active manager task."""
    effective_repo_root = (repo_root or Path.cwd()).resolve()
    effective_state_dir = (
        control_state_dir or effective_repo_root / ".umbrella"
    ).resolve()
    resolved_task_id = _resolve_live_task_id(effective_state_dir, task_id)
    update = queue_runtime_task_update(
        effective_state_dir,
        resolved_task_id,
        instruction,
        source=source,
    )
    return {
        "task_id": resolved_task_id,
        "update_id": update.id,
        "status": "queued",
    }


def main() -> int:
    """Main entrypoint for Umbrella manager CLI.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    from umbrella.app_ouroboros import main as ouroboros_main

    log.warning(
        "umbrella.app is now a compatibility alias; routing CLI execution to umbrella.app_ouroboros"
    )
    return ouroboros_main(sys.argv[1:])


def run_demo(
    demo_scenario: str,
    workspace_id: str,
    repo_root: Path | None,
    control_state_dir: Path | None,
    workspaces_root: Path | None,
    max_iterations: int | None,
    max_duration_seconds: float | None,
    use_live_llm: bool,
    output_path: Path | None,
    heartbeat_interval_seconds: float = 30.0,
    progress_reporter: Any = None,
) -> dict:
    """Run a demo scenario."""
    scenario = DemoScenario(demo_scenario)

    log.info(f"Running demo scenario: {scenario.value}")
    log.info(f"Workspace: {workspace_id}")
    log.info(f"Live LLM: {use_live_llm}")
    log.info(f"Max iterations: {_format_limit_for_log(max_iterations)}")
    log.info(f"Max duration: {_format_limit_for_log(max_duration_seconds, 's')}")

    runner = create_demo_runner()

    # Handle workspace improvement cycle as special case
    if scenario == DemoScenario.WORKSPACE_IMPROVEMENT_CYCLE:
        results = runner.run_workspace_improvement_cycle(
            repo_root=repo_root,
            control_state_dir=control_state_dir,
            workspaces_root=workspaces_root,
            use_live_llm=use_live_llm,
        )

        # Return aggregated results
        baseline = results["baseline"]
        improved = results["improved"]

        if not results.get("changed_files"):
            overall_status = "failed"
        elif improved.status == "complete":
            overall_status = "complete"
        elif improved.status == "partial" or baseline.status == "partial":
            overall_status = "partial"
        else:
            overall_status = "failed"

        return {
            "scenario": "workspace_improvement_cycle",
            "status": overall_status,
            "baseline_status": baseline.status,
            "improved_status": improved.status,
            "baseline_duration": baseline.duration_seconds,
            "improved_duration": improved.duration_seconds,
            "changed_files": results.get("changed_files", []),
            "instance_path": results.get("instance_path"),
        }

    result = runner.run_scenario(
        scenario,
        repo_root=repo_root,
        control_state_dir=control_state_dir,
        workspaces_root=workspaces_root,
        max_iterations=max_iterations,
        max_duration_seconds=max_duration_seconds,
        use_live_llm=use_live_llm,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        progress_reporter=progress_reporter,
    )

    # Print summary
    runner.print_result_summary(result)

    # Save report if requested
    if output_path:
        save_report(result, output_path, repo_root)
        log.info(f"Report saved to {output_path}")

    # Print promotion report if relevant
    from umbrella.integration.reporting import render_promotion_report

    promo_report = render_promotion_report(result)
    if promo_report:
        print("\n" + promo_report)

    return {
        "status": result.status,
        "task_success": result.task_success,
        "iterations": result.iterations,
        "duration_seconds": result.duration_seconds,
    }


def run_custom_task(
    task_input: str,
    workspace_id: str,
    repo_root: Path | None,
    control_state_dir: Path | None,
    workspaces_root: Path | None,
    max_iterations: int | None,
    max_duration_seconds: float | None,
    use_live_llm: bool,
    output_path: Path | None,
    heartbeat_interval_seconds: float = 30.0,
    runtime_config: object | None = None,
    progress_reporter: Any = None,
) -> dict:
    """Run a custom task through Ouroboros-first Umbrella compatibility path."""
    del (
        control_state_dir,
        workspaces_root,
        max_iterations,
        heartbeat_interval_seconds,
        runtime_config,
        progress_reporter,
    )

    from umbrella.control_plane.ouroboros_integration import (
        run_ouroboros_improvement_sync,
    )

    effective_repo_root = (repo_root or Path.cwd()).resolve()
    log.info(
        "Running custom task through Ouroboros: workspace=%s live=%s",
        workspace_id,
        use_live_llm,
    )
    result = run_ouroboros_improvement_sync(
        repo_root=effective_repo_root,
        task_description=task_input,
        workspace_id=workspace_id,
        use_live_llm=use_live_llm,
        timeout_seconds=max_duration_seconds,
    )

    if output_path:
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        log.info("Report saved to %s", output_path)

    status = str(result.get("status") or "error")
    return {
        "status": status,
        "task_success": "complete" if status == "complete" else status,
        "iterations": int(result.get("llm_tool_invocations") or 0),
        "duration_seconds": 0.0,
    }


if __name__ == "__main__":
    sys.exit(main())
