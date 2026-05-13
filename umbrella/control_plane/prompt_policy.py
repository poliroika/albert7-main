"""
Formal prompt-stack policy for manager self-improvement.

This module makes the manager prompt stack explicit instead of treating it as
an invisible implementation detail.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

from umbrella.control_plane.models import (
    PromptPatchProposal,
    PromptRiskLevel,
    PromptSurface,
    PromptSurfaceKind,
    generate_prompt_proposal_id,
)
from umbrella.control_plane.prompt_diff import render_prompt_diff
from umbrella.control_plane.prompt_versioning import PromptVersionStore

_SURFACE_CATALOG: tuple[PromptSurface, ...] = (
    PromptSurface(
        id="ouroboros_system_prompt",
        path=Path("ouroboros/prompts/SYSTEM.md"),
        kind=PromptSurfaceKind.SYSTEM_PROMPT,
        label="Ouroboros system prompt",
        description="Primary manager instruction stack for Ouroboros.",
        foundational=True,
        human_checkpoint_required=True,
    ),
    PromptSurface(
        id="ouroboros_bible",
        path=Path("ouroboros/BIBLE.md"),
        kind=PromptSurfaceKind.CONSTITUTION,
        label="Ouroboros constitution",
        description="Foundational behavioral and identity contract for Ouroboros.",
        foundational=True,
        human_checkpoint_required=True,
    ),
    PromptSurface(
        id="ouroboros_context_assembly",
        path=Path("ouroboros/ouroboros/context.py"),
        kind=PromptSurfaceKind.CONTEXT_ASSEMBLY,
        label="Manager context assembly",
        description="Runtime rules that assemble the manager's effective prompt context.",
    ),
    PromptSurface(
        id="ouroboros_task_planner_prompts",
        path=Path("ouroboros/ouroboros/task_planner.py"),
        kind=PromptSurfaceKind.POLICY_FRAGMENT,
        label="Task planner phase prompts",
        description="Planner, subtask focus, and review-phase strings injected into the loop.",
    ),
    PromptSurface(
        id="umbrella_delivery_critic",
        path=Path("umbrella/control_plane/critic.py"),
        kind=PromptSurfaceKind.POLICY_FRAGMENT,
        label="Delivery critic LLM gate",
        description="Post-run critic system prompt and heuristic gate for workspace delivery.",
    ),
    PromptSurface(
        id="umbrella_workspace_task_wrapper",
        path=Path("umbrella/prompts/ouroboros_workspace_task.md"),
        kind=PromptSurfaceKind.POLICY_FRAGMENT,
        label="Umbrella workspace mission wrapper",
        description="Default task wrapper text for Ouroboros workspace runs.",
    ),
    PromptSurface(
        id="umbrella_prompt_policy",
        path=Path("umbrella/control_plane/prompt_policy.py"),
        kind=PromptSurfaceKind.POLICY_FRAGMENT,
        label="Umbrella prompt governance policy",
        description="Structured prompt-governance rules for the manager control plane.",
    ),
    PromptSurface(
        id="umbrella_human_gate_policy",
        path=Path("umbrella/control_plane/human_checkpoints.py"),
        kind=PromptSurfaceKind.HUMAN_GATE_POLICY,
        label="Human checkpoint policy",
        description="Approval and resume policy for risky manager prompt rewrites.",
        human_checkpoint_required=True,
    ),
)

_HUMAN_GATE_KEYWORDS = (
    "human checkpoint",
    "approval",
    "send_owner_message",
    "owner message",
    "escalat",
    "creator",
    "human gate",
)

_FOUNDATIONAL_KEYWORDS = (
    "identity",
    "constitution",
    "core principle",
    "always",
    "never",
    "global behavior",
    "owner",
)


def identify_prompt_surfaces(repo_root: Path | None = None) -> list[PromptSurface]:
    """Return the formal prompt surfaces for manager-side self-improvement."""
    if repo_root is None:
        return list(_SURFACE_CATALOG)

    surfaces: list[PromptSurface] = []
    for surface in _SURFACE_CATALOG:
        if (repo_root / surface.path).exists():
            surfaces.append(surface)
    return surfaces


def get_prompt_surface(
    *,
    surface_id: str | None = None,
    surface_path: Path | None = None,
    repo_root: Path | None = None,
) -> PromptSurface:
    """Resolve a prompt surface by ID or path."""
    for surface in identify_prompt_surfaces(repo_root):
        if surface_id is not None and surface.id == surface_id:
            return surface
        if surface_path is not None and surface.path == surface_path:
            return surface
    requested = surface_id or str(surface_path)
    raise KeyError(f"Unknown prompt surface: {requested}")


def _read_surface_text(surface: PromptSurface, repo_root: Path | None) -> str:
    path = (
        (repo_root / surface.path).resolve() if repo_root is not None else surface.path
    )
    return path.read_text(encoding="utf-8")


def _changed_line_count(diff_text: str) -> int:
    return sum(
        1
        for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _touches_human_gate_policy(
    surface: PromptSurface,
    rationale: str,
    expected_behavioral_effect: str,
    diff_text: str,
) -> bool:
    if surface.kind == PromptSurfaceKind.HUMAN_GATE_POLICY:
        return True

    haystack = "\n".join((rationale, expected_behavioral_effect, diff_text)).lower()
    return any(keyword in haystack for keyword in _HUMAN_GATE_KEYWORDS)


def classify_prompt_risk(proposal: PromptPatchProposal) -> PromptRiskLevel:
    """Classify a prompt proposal into safe / medium / high risk."""
    surface = proposal.surface
    if surface.foundational or surface.kind in {
        PromptSurfaceKind.SYSTEM_PROMPT,
        PromptSurfaceKind.CONSTITUTION,
        PromptSurfaceKind.HUMAN_GATE_POLICY,
    }:
        return PromptRiskLevel.HIGH_FOUNDATIONAL_CHANGE

    if proposal.touches_human_gate_policy:
        return PromptRiskLevel.HIGH_FOUNDATIONAL_CHANGE

    combined_text = "\n".join(
        (proposal.rationale, proposal.expected_behavioral_effect, proposal.diff_text)
    ).lower()
    if any(keyword in combined_text for keyword in _FOUNDATIONAL_KEYWORDS):
        return PromptRiskLevel.HIGH_FOUNDATIONAL_CHANGE

    if surface.kind in {
        PromptSurfaceKind.CONTEXT_ASSEMBLY,
        PromptSurfaceKind.POLICY_FRAGMENT,
    }:
        return PromptRiskLevel.MEDIUM_POLICY_CHANGE

    if proposal.changed_lines <= 12:
        return PromptRiskLevel.SAFE_LOCAL_TUNING

    return PromptRiskLevel.MEDIUM_POLICY_CHANGE


def requires_human_checkpoint(proposal: PromptPatchProposal) -> bool:
    """Return whether a prompt proposal requires human approval."""
    return (
        proposal.surface.human_checkpoint_required
        or proposal.touches_human_gate_policy
        or proposal.risk_level != PromptRiskLevel.SAFE_LOCAL_TUNING
    )


def propose_prompt_patch(
    surface: PromptSurface,
    *,
    repo_root: Path,
    version_store_dir: Path,
    task_id: str,
    rationale: str,
    expected_behavioral_effect: str,
    evidence: list[str] | None = None,
    proposed_content: str | None = None,
    rollback_reference: str | None = None,
) -> PromptPatchProposal:
    """Create a formal, auditable prompt patch proposal."""
    before_text = _read_surface_text(surface, repo_root)
    after_text = proposed_content if proposed_content is not None else before_text
    version_store = PromptVersionStore(version_store_dir, repo_root=repo_root)
    before_record = version_store.record(
        surface,
        task_id=task_id,
        label="before",
        content=before_text,
    )
    candidate_record = version_store.record(
        surface,
        task_id=task_id,
        label="candidate",
        content=after_text,
    )

    diff_text = render_prompt_diff(before_text, after_text, surface.label)
    proposal = PromptPatchProposal(
        id=generate_prompt_proposal_id(),
        task_id=task_id,
        surface=surface,
        rationale=rationale,
        expected_behavioral_effect=expected_behavioral_effect,
        rollback_reference=rollback_reference or before_record.id,
        evidence=evidence or [],
        proposed_content=proposed_content,
        diff_text=diff_text,
        base_version_id=before_record.id,
        candidate_version_id=candidate_record.id,
        changed_lines=_changed_line_count(diff_text),
    )
    proposal.touches_human_gate_policy = _touches_human_gate_policy(
        surface,
        rationale,
        expected_behavioral_effect,
        diff_text,
    )
    proposal.risk_level = classify_prompt_risk(proposal)
    proposal.requires_human_checkpoint = requires_human_checkpoint(proposal)
    return proposal


def save_prompt_patch_proposal(
    proposal: PromptPatchProposal, proposal_dir: Path
) -> Path:
    """Persist a prompt patch proposal for audit/review."""
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposal_dir / f"{proposal.id}.json"
    proposal_path.write_text(
        json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return proposal_path


def load_prompt_patch_proposal(
    proposal_id: str, proposal_dir: Path
) -> PromptPatchProposal:
    """Load a saved prompt patch proposal."""
    proposal_path = proposal_dir / f"{proposal_id}.json"
    if not proposal_path.exists():
        raise FileNotFoundError(
            f"Prompt proposal {proposal_id} not found in {proposal_dir}"
        )
    return PromptPatchProposal.model_validate_json(
        proposal_path.read_text(encoding="utf-8")
    )


def apply_prompt_patch(
    proposal: PromptPatchProposal,
    repo_root: Path,
    version_store_dir: Path,
) -> "PromptVersionRecord":
    """Apply an approved prompt patch to the target surface.

    This writes the proposed content to the prompt file and creates a version record.

    Args:
        proposal: The approved prompt patch proposal
        repo_root: Repository root for resolving file paths
        version_store_dir: Directory for version records

    Returns:
        Version record for the applied patch
    """
    if proposal.proposed_content is None:
        raise ValueError(f"Cannot apply proposal {proposal.id}: no proposed content")

    # Resolve the target file path
    target_path = (repo_root / proposal.surface.path).resolve()

    if not target_path.exists():
        raise FileNotFoundError(f"Target prompt file not found: {target_path}")

    # Write the proposed content
    target_path.write_text(proposal.proposed_content, encoding="utf-8")
    log.info(f"Applied prompt patch {proposal.id} to {proposal.surface.path}")

    # Record the new version
    from umbrella.control_plane.prompt_versioning import record_prompt_version

    version_record = record_prompt_version(
        proposal.surface,
        version_store_dir,
        repo_root=repo_root,
        task_id=proposal.task_id,
        label="applied",
        content=proposal.proposed_content,
    )

    return version_record
