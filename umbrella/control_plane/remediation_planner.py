"""Umbrella-owned remediation plan preparation for Ouroboros runs."""

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def synthesise_verification_remediation_plan(
    *,
    drive_root: Path,
    task_id: str,
    workspace_id: str,
    remediation_attempt: int,
    failure_kind: str = "verification",
) -> str | None:
    """Create the single-subtask plan used by verification remediation.

    Umbrella owns this because remediation is a control-plane phase decision.
    Ouroboros should only see a prebuilt plan id and execute it.
    """

    try:
        from ouroboros.task_planner import TaskPlanStore

        store = TaskPlanStore(drive_root)
        kind = str(failure_kind or "verification").strip().lower()
        if kind == "hygiene":
            objective = (
                f"External hygiene remediation cycle #{remediation_attempt}. "
                "Address only the final_sweep cleanup targets from the "
                "remediation prompt above."
            )
        else:
            objective = (
                f"External verification remediation cycle #{remediation_attempt}. "
                "Address only the failing verification checks from the "
                "remediation prompt above."
            )
        plan = store.create_from_steps(
            task_id=task_id,
            workspace_id=workspace_id or "",
            objective_digest=objective,
            steps=[_remediation_step(remediation_attempt, failure_kind=kind)],
        )
        log.info(
            "Umbrella remediation planner created prebuilt plan %s for attempt %d.",
            plan.task_id,
            remediation_attempt,
        )
        return plan.task_id
    except Exception:
        log.warning("Failed to synthesise Umbrella remediation plan", exc_info=True)
        return None


def _remediation_step(
    remediation_attempt: int, *, failure_kind: str = "verification"
) -> dict[str, Any]:
    kind = str(failure_kind or "verification").strip().lower()
    if kind == "hygiene":
        return {
            "title": f"Fix final_sweep hygiene failures (remediation #{remediation_attempt})",
            "description": (
                "Re-read the remediation prompt and structured failure context. "
                "Verification may already be green; the blocking gate is workspace "
                "hygiene. Remove or move only the listed cleanup targets (use "
                "delete_workspace_file for removable noise) and self-verify with "
                "`run_workspace_verify` before closing."
            ),
            "success_check": (
                "run_workspace_verify reports passed=true and final_sweep has no "
                "blocking cleanup targets."
            ),
        }
    return {
        "title": f"Fix failing verification checks (remediation #{remediation_attempt})",
        "description": (
            "Re-read the remediation prompt the harness fed in. Read the "
            "structured failure context artifact it points at, diagnose each "
            "failing required check, fix only those files (use "
            "delete_workspace_file for noise/cleanup), and self-verify with "
            "`run_workspace_verify` before closing. Touch the minimum set of "
            "files needed."
        ),
        "success_check": (
            "run_workspace_verify reports passed=true with 0 failed required "
            "steps; every check that was previously failing is now green."
        ),
    }
