"""Single canonical write path for MemoryEvent → MemPalace."""

import os
from pathlib import Path
from typing import Any

from umbrella.contracts import build_workspace_context
from umbrella.contracts.evidence import EvidenceResolver
from umbrella.deep_agent_tools.memory import memory_write_policy_issues
from umbrella.memory.kernel.models import (
    MemoryEvent,
    MemoryWriteResult,
    memory_event_to_palace_kwargs,
    normalize_memory_event,
    validate_memory_event_for_write,
)
from umbrella.memory.kernel.telemetry import record_memory_event
from umbrella.memory.paths import workspace_root as workspace_project_root


def write_memory_event(
    repo_root: Path,
    event: MemoryEvent | dict[str, Any],
    *,
    workspace_id: str = "",
    drive_root: Path | None = None,
) -> MemoryWriteResult:
    """Write one memory event to canonical MemPalace (optional legacy mirror)."""
    norm = normalize_memory_event(event)
    ws = workspace_id or norm.workspace_id
    issues = validate_memory_event_for_write(norm)
    if issues:
        record_memory_event(
            repo_root,
            event_type="memory_promotion_blocked",
            workspace_id=ws,
            run_id=norm.run_id,
            phase_id=norm.phase_id,
            status="blocked",
            error="; ".join(issues),
            drive_root=drive_root,
        )
        return MemoryWriteResult(saved=False, error="memory_event_validation", warnings=tuple(issues))

    tag_list = list(norm.tags)
    metadata = {
        "trust_level": norm.trust_level,
        "evidence_refs": [
            {
                "ref_type": r.ref_type,
                "ref_id": r.ref_id,
                "produced_by": r.produced_by,
                "hash": r.hash,
            }
            for r in norm.evidence_refs
        ],
        "scope": norm.scope,
        "tier": norm.tier,
        "phase": norm.phase_id,
        "verified": norm.verified,
    }
    policy_issues = memory_write_policy_issues(
        kind=norm.memory_kind,
        tags=tag_list,
        metadata=metadata,
    )
    if policy_issues:
        record_memory_event(
            repo_root,
            event_type="memory_promotion_blocked",
            workspace_id=ws,
            run_id=norm.run_id,
            phase_id=norm.phase_id,
            status="blocked",
            error="; ".join(policy_issues),
            drive_root=drive_root,
        )
        return MemoryWriteResult(
            saved=False,
            error="evidence_bound_memory",
            warnings=tuple(policy_issues),
        )

    if norm.evidence_refs and ws:
        ws_proj = workspace_project_root(repo_root, ws)
        ctx = build_workspace_context(
            repo_root=str(repo_root.resolve()),
            workspace_root=str(ws_proj.resolve()),
            workspace_id=ws,
        )
        resolver_issues = EvidenceResolver(ctx).validate_refs(
            norm.evidence_refs,
            phase=norm.phase_id,
        )
        if resolver_issues:
            msgs = [i.message for i in resolver_issues]
            record_memory_event(
                repo_root,
                event_type="memory_promotion_blocked",
                workspace_id=ws,
                status="blocked",
                error=msgs[0] if msgs else "invalid_evidence_refs",
                drive_root=drive_root,
            )
            return MemoryWriteResult(
                saved=False,
                error="invalid_evidence_refs",
                warnings=tuple(msgs),
            )

    kwargs = memory_event_to_palace_kwargs(norm)
    store = str(kwargs["store"])
    volatile = os.environ.get("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB") == "1"
    try:
        from umbrella.memory.palace.facade import MemPalace

        palace = MemPalace(repo_root, ws or None)
        try:
            node_id = palace.add(**kwargs)
        finally:
            palace.close()
    except Exception as exc:
        record_memory_event(
            repo_root,
            event_type="memory_write_failed",
            workspace_id=ws,
            run_id=norm.run_id,
            phase_id=norm.phase_id,
            backend="canonical_mempalace",
            status="failed",
            error=str(exc),
            drive_root=drive_root,
        )
        return MemoryWriteResult(
            saved=False,
            error="canonical_memory_unavailable",
            volatile=volatile,
        )

    if not node_id:
        record_memory_event(
            repo_root,
            event_type="memory_write_failed",
            workspace_id=ws,
            backend="canonical_mempalace",
            status="failed",
            error="empty canonical_id",
            drive_root=drive_root,
        )
        return MemoryWriteResult(saved=False, error="empty_canonical_id", volatile=volatile)

    record_memory_event(
        repo_root,
        event_type="memory_write_succeeded",
        workspace_id=ws,
        run_id=norm.run_id,
        phase_id=norm.phase_id,
        backend="canonical_mempalace",
        status="ok",
        data={"canonical_id": node_id, "store": store},
        drive_root=drive_root,
    )
    return MemoryWriteResult(
        saved=True,
        canonical_id=node_id,
        store=store,
        volatile=volatile,
    )
