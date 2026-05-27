"""Single write path for canonical memory events."""

from dataclasses import replace
from pathlib import Path
from typing import Any

from umbrella.contracts.models import json_ready
from umbrella.memory.kernel.models import (
    MemoryEvent,
    MemoryWriteResult,
    memory_event_to_palace_kwargs,
    validate_memory_event_for_write,
)
from umbrella.memory.kernel.policy import memory_write_policy_issues
from umbrella.memory.kernel.telemetry import record_memory_event
from umbrella.memory.paths import normalize_workspace_id
from umbrella.memory.palace.facade import MemPalace as _DefaultMemPalace

MemPalace = _DefaultMemPalace


def _mem_palace_class() -> Any:
    from umbrella.memory.palace import facade

    current = getattr(facade, "MemPalace", _DefaultMemPalace)
    if current is not _DefaultMemPalace:
        return current
    return MemPalace


def _preset_canonical_id(event: MemoryEvent) -> str:
    return str(event.metadata.get("canonical_id") or "").strip()


def existing_canonical_node(
    repo_root: str | Path,
    *,
    workspace_id: str,
    node_id: str,
    store: str,
) -> bool:
    palace = _mem_palace_class()(repo_root, normalize_workspace_id(workspace_id))
    try:
        return palace.get(node_id, stores=[store]) is not None
    finally:
        palace.close()


def mirror_legacy_write(
    repo_root: str | Path,
    *,
    workspace_id: str,
    store: str,
    node_id: str,
    content: str,
    title: str,
    tags: list[str],
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    from umbrella.deep_agent_tools.memory import _legacy_palace_available, _palace_backend

    if not _legacy_palace_available():
        return None
    backend = _palace_backend(Path(repo_root), workspace_id)
    if backend is None:
        return None
    kind = str(extra.get("type") or extra.get("kind") or "observation")
    return backend.add(
        workspace_id=workspace_id,
        event_type=kind,
        room=str(extra.get("room") or ""),
        title=title,
        content=content,
        kind=kind,
        tags=tags or None,
        metadata_extra={**extra, "canonical_id": node_id, "store": store},
    )


def write_memory_event(
    repo_root: str | Path,
    event: MemoryEvent,
    *,
    workspace_id: str | None = None,
    skip_if_exists: bool = False,
    mirror_legacy: bool = False,
) -> MemoryWriteResult:
    """Validate policy, write to MemPalace, emit telemetry."""
    ws = normalize_workspace_id(workspace_id or event.workspace_id)
    event = replace(event, workspace_id=ws)
    preset_id = _preset_canonical_id(event)
    issues = tuple(
        validate_memory_event_for_write(event)
        + memory_write_policy_issues(
            kind=event.memory_kind,
            tags=list(event.tags),
            metadata={
                "scope": event.scope,
                "tier": event.tier,
                "trust_level": event.trust_level,
                "verified": event.verified,
                "evidence_refs": [json_ready(ref) for ref in event.evidence_refs],
                "verify_run_id": event.metadata.get("verify_run_id"),
            },
        )
    )
    if issues:
        record_memory_event(
            Path(repo_root),
            event_type="memory_write_rejected",
            workspace_id=ws,
            run_id=event.run_id,
            phase_id=event.phase_id,
            status="blocked",
            data={
                "canonical_id": preset_id,
                "memory_kind": event.memory_kind,
                "issues": list(issues),
            },
        )
        return MemoryWriteResult(
            saved=False,
            canonical_id=preset_id,
            store="",
            policy_issues=issues,
        )

    kwargs = memory_event_to_palace_kwargs(event)
    store = str(kwargs["store"])
    node_id = preset_id

    if skip_if_exists and node_id and existing_canonical_node(
        repo_root, workspace_id=ws, node_id=node_id, store=store
    ):
        record_memory_event(
            Path(repo_root),
            event_type="memory_write_skipped_duplicate",
            workspace_id=ws,
            run_id=event.run_id,
            phase_id=event.phase_id,
            data={"canonical_id": node_id, "store": store},
        )
        return MemoryWriteResult(
            saved=False,
            canonical_id=node_id,
            store=store,
            skipped_duplicate=True,
        )

    try:
        palace = _mem_palace_class()(repo_root, ws)
        try:
            if node_id:
                committed_id = palace.add(**kwargs, node_id=node_id)
            else:
                committed_id = palace.add(**kwargs)
        finally:
            palace.close()
    except Exception as exc:
        record_memory_event(
            Path(repo_root),
            event_type="memory_write_failed",
            workspace_id=ws,
            run_id=event.run_id,
            phase_id=event.phase_id,
            status="failed",
            error=str(exc),
            data={
                "canonical_id": preset_id,
                "store": store,
                "memory_kind": event.memory_kind,
                "surface": event.surface,
            },
        )
        return MemoryWriteResult(
            saved=False,
            canonical_id=preset_id,
            store=store,
            error=str(exc),
            policy_issues=(),
        )

    record_memory_event(
        Path(repo_root),
        event_type="memory_write_committed",
        workspace_id=ws,
        run_id=event.run_id,
        phase_id=event.phase_id,
        data={
            "canonical_id": committed_id,
            "store": store,
            "memory_kind": event.memory_kind,
            "surface": event.surface,
        },
    )

    if mirror_legacy:
        mirror_legacy_write(
            repo_root,
            workspace_id=ws,
            store=store,
            node_id=committed_id,
            content=event.content,
            title=event.title,
            tags=list(event.tags),
            extra=dict(kwargs.get("extra") or {}),
        )

    return MemoryWriteResult(
        saved=True,
        canonical_id=committed_id,
        store=store,
    )
