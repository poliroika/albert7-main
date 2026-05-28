"""Build unified phase platform context from capability_declaration."""

from pathlib import Path
from typing import Any

from umbrella.contracts.capability_declaration import (
    CapabilityDeclaration,
    load_capability_declaration,
    proof_required_capabilities,
)
from umbrella.contracts.models import PlanIR
from umbrella.contracts.runtime_probes import effective_runtime_capabilities
from umbrella.workspace_registry.charter import load_workspace_charter


def overlay_hints_from_declaration(
    drive_root: Path | None,
    workspace_root: Path,
) -> dict[str, Any]:
    declaration = load_capability_declaration(drive_root)
    charter = load_workspace_charter(workspace_root)
    recommended_skills: list[str] = []
    detected_domains: list[str] = []
    if declaration is not None:
        recommended_skills = list(declaration.recommended_skills)
        llm_entry = declaration.capabilities.get("llm_api")
        if llm_entry is not None and llm_entry.available:
            detected_domains.append("multi_agent_gmas")
    policies = charter.get("policies") if isinstance(charter.get("policies"), dict) else {}
    if policies.get("multi_agent_gmas") is True:
        detected_domains.append("multi_agent_gmas")
    return {
        "recommended_skills": recommended_skills,
        "detected_domains": sorted(set(detected_domains)),
        "charter_policies": policies,
    }


def build_platform_context_envelope(
    *,
    drive_root: Path | None,
    workspace_root: Path,
    plan: PlanIR | None = None,
) -> dict[str, Any]:
    declaration = load_capability_declaration(drive_root)
    effective = effective_runtime_capabilities(drive_root)
    matrix: dict[str, list[str]] = {}
    if plan is not None:
        for subtask in plan.subtasks:
            if subtask.proof is not None:
                matrix[subtask.id] = sorted(proof_required_capabilities(subtask.proof))
    payload: dict[str, Any] = {
        "effective_capabilities": effective,
        "proof_capability_matrix": matrix,
    }
    if declaration is not None:
        payload["capability_declaration"] = declaration.to_dict()
    payload.update(overlay_hints_from_declaration(drive_root, workspace_root))
    return payload


def capability_gate_recovery_hint(issues: list[Any]) -> str | None:
    codes = {str(getattr(item, "code", "") or "") for item in issues}
    if "missing_capability_declaration" in codes:
        return "loop_back_to(research) and submit_capability_declaration before plan."
    if "capability_probe_failed" in codes:
        return "loop_back_to(research); update capability_declaration probes or proof.required_capabilities."
    return None
