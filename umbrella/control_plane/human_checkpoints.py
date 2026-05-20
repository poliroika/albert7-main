"""
Human checkpoint workflow for risky manager prompt rewrites.

The control plane should reuse the existing Ouroboros owner-contact path
instead of inventing a second approval channel. This module packages the
request/decision/resume state so the runtime can surface it through
`send_owner_message`, normal task messages, or escalation records.
"""

import json
import time
from pathlib import Path

from umbrella.control_plane.models import (
    CheckpointResumeResult,
    HumanCheckpointDecision,
    HumanCheckpointRequest,
    HumanCheckpointStatus,
    ManagerPhase,
    PromptPatchProposal,
    generate_human_checkpoint_id,
)
def resume_from_checkpoint(checkpoint_id: str, checkpoint_dir: Path) -> None:
    """No-op placeholder: legacy self-improvement governance removed in PhaseRunner refactor.

    System self-improvement now flows through ``umbrella.orchestrator.self_improvement_runner``
    with the relaxed PermissionEnvelope from ``umbrella/permissions/self_improvement.yaml``.
    """
    return None


def _request_path(checkpoint_dir: Path, checkpoint_id: str) -> Path:
    return checkpoint_dir / f"{checkpoint_id}.json"


def build_owner_checkpoint_message(
    request: HumanCheckpointRequest,
    proposal: PromptPatchProposal,
) -> str:
    """Render the human-facing summary that can be sent via owner messaging."""
    evidence_lines = (
        "\n".join(f"- {item}" for item in proposal.evidence[:5])
        or "- No evidence attached"
    )
    return (
        "Human checkpoint required for manager prompt rewrite.\n"
        f"Surface: {proposal.surface.label}\n"
        f"Risk: {proposal.risk_level.value}\n"
        f"Expected effect: {proposal.expected_behavioral_effect}\n"
        f"Rollback: {proposal.rollback_reference or 'n/a'}\n"
        "Evidence:\n"
        f"{evidence_lines}\n"
        f"Proposal ID: {proposal.id}\n"
        f"Checkpoint ID: {request.id}"
    )


def save_human_checkpoint_request(
    request: HumanCheckpointRequest, checkpoint_dir: Path
) -> Path:
    """Persist a human checkpoint request."""
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = _request_path(checkpoint_dir, request.id)
    path.write_text(
        json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_human_checkpoint_request(
    checkpoint_id: str,
    checkpoint_dir: Path,
) -> HumanCheckpointRequest | None:
    """Load a human checkpoint request from disk."""
    path = _request_path(checkpoint_dir, checkpoint_id)
    if not path.exists():
        return None
    return HumanCheckpointRequest.model_validate_json(path.read_text(encoding="utf-8"))


def create_human_checkpoint_request(
    *,
    task_id: str,
    proposal: PromptPatchProposal,
    checkpoint_dir: Path,
    manager_checkpoint_id: str | None = None,
    description: str | None = None,
    checkpoint_id: str | None = None,
) -> HumanCheckpointRequest:
    """Create and persist a new human checkpoint request."""
    request = HumanCheckpointRequest(
        id=checkpoint_id or generate_human_checkpoint_id(),
        task_id=task_id,
        checkpoint_type="prompt_rewrite",
        description=description
        or f"Approve prompt rewrite for {proposal.surface.label}",
        proposal_id=proposal.id,
        manager_checkpoint_id=manager_checkpoint_id,
        metadata={
            "surface_id": proposal.surface.id,
            "surface_path": str(proposal.surface.path),
            "risk_level": proposal.risk_level.value,
            "rollback_reference": proposal.rollback_reference,
            "diff_text": proposal.diff_text,
        },
    )
    request.notification_message = build_owner_checkpoint_message(request, proposal)
    save_human_checkpoint_request(request, checkpoint_dir)
    return request


def record_human_checkpoint_decision(
    checkpoint_id: str,
    *,
    checkpoint_dir: Path,
    approved: bool,
    response: str,
    decided_by: str = "human",
) -> HumanCheckpointDecision:
    """Record a human approval or rejection for a checkpoint request."""
    request = load_human_checkpoint_request(checkpoint_id, checkpoint_dir)
    if request is None:
        raise FileNotFoundError(f"Human checkpoint {checkpoint_id} not found")

    normalized_decider = (
        decided_by if decided_by in {"human", "auto", "policy"} else "human"
    )
    decision = HumanCheckpointDecision(
        checkpoint_id=checkpoint_id,
        approved=approved,
        response=response,
        decided_by=normalized_decider,
    )
    request.status = (
        HumanCheckpointStatus.APPROVED if approved else HumanCheckpointStatus.REJECTED
    )
    request.resolved_at = time.time()
    request.metadata["decision"] = decision.model_dump(mode="json")
    save_human_checkpoint_request(request, checkpoint_dir)
    return decision


def resume_after_human_checkpoint(
    checkpoint_id: str,
    *,
    checkpoint_dir: Path,
    manager_checkpoint_dir: Path,
) -> CheckpointResumeResult:
    """Resume a task after a human checkpoint has been resolved."""
    request = load_human_checkpoint_request(checkpoint_id, checkpoint_dir)
    if request is None:
        raise FileNotFoundError(f"Human checkpoint {checkpoint_id} not found")

    decision_payload = request.metadata.get("decision")
    decision = (
        HumanCheckpointDecision.model_validate(decision_payload)
        if isinstance(decision_payload, dict)
        else None
    )

    if request.status == HumanCheckpointStatus.PENDING:
        return CheckpointResumeResult(
            checkpoint_id=request.id,
            task_id=request.task_id,
            resumed=False,
            resume_reason="Human checkpoint is still pending",
            manager_checkpoint_id=request.manager_checkpoint_id,
            human_decision=decision,
        )

    if request.status == HumanCheckpointStatus.REJECTED:
        return CheckpointResumeResult(
            checkpoint_id=request.id,
            task_id=request.task_id,
            resumed=False,
            resume_reason=decision.response
            if decision
            else "Human checkpoint rejected",
            manager_checkpoint_id=request.manager_checkpoint_id,
            human_decision=decision,
        )

    if request.manager_checkpoint_id:
        resume_from_checkpoint(request.manager_checkpoint_id, manager_checkpoint_dir)

    return CheckpointResumeResult(
        checkpoint_id=request.id,
        task_id=request.task_id,
        resumed=True,
        next_phase=ManagerPhase.SELF_IMPROVEMENT_APPROVED,
        resume_reason=decision.response if decision else "Human checkpoint approved",
        manager_checkpoint_id=request.manager_checkpoint_id,
        human_decision=decision,
        metadata={"proposal_id": request.proposal_id},
    )
