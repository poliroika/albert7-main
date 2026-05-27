"""Test helpers for capability_declaration handoff gates."""

from ouroboros.tools.registry import ToolContext
from umbrella.contracts.capability_declaration import (
    build_declaration_from_probes,
    persist_capability_declaration,
)
from umbrella.contracts.runtime_probes import baseline_runtime_capabilities


def seed_submitted_declaration(
    ctx: ToolContext,
    *,
    notes: str = (
        "Test harness submitted capability declaration with baseline "
        "python/subprocess for phase-control artifact tests."
    ),
    discovery_channels: list[dict[str, str]] | None = None,
    recommended_skills: list[str] | None = None,
) -> None:
    drive = ctx.drive_root
    if drive is None:
        raise ValueError("ToolContext.drive_root is required")
    workspace_id = str(getattr(ctx, "workspace_id", "") or "test")
    run_id = str(getattr(ctx, "run_id", "") or "run-test")
    probed = baseline_runtime_capabilities()
    payload = build_declaration_from_probes(
        run_id=run_id,
        workspace_id=workspace_id,
        probed=probed,
        actor="harness",
        status="submitted",
        notes=notes,
    )
    if discovery_channels:
        payload["discovery_channels"] = discovery_channels
    if recommended_skills:
        payload["recommended_skills"] = recommended_skills
    persist_capability_declaration(drive, payload)
