"""Compile auditable LLM input bundles per phase."""

import json
from pathlib import Path
from typing import Any

from umbrella.contracts.hashing import hash_value, workspace_hash
from umbrella.context.models import (
    CapabilityContractView,
    ContextSourceRef,
    LLMContextItem,
    LLMInputBundle,
    MemorySelection,
    ToolContractView,
    WorkspaceFileDigest,
    WorkspaceInventorySnapshot,
)


def _file_kind(path: str) -> str:
    norm = path.replace("\\", "/").lower()
    if norm.startswith("tests/") or "/test_" in norm:
        return "test"
    if norm.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg")):
        return "config"
    if norm.startswith("docs/") or norm.endswith(".md"):
        return "docs"
    if norm.startswith("frontend/"):
        return "frontend"
    if norm.endswith(".py"):
        return "source"
    return "other"


def _build_workspace_inventory(
    workspace_root: Path,
    *,
    active_subtask: dict | None,
) -> WorkspaceInventorySnapshot | None:
    if not workspace_root.is_dir():
        return None
    declared: list[str] = []
    if active_subtask:
        for key in ("files_to_create", "files_to_change", "files_affected"):
            raw = active_subtask.get(key)
            if isinstance(raw, str) and raw.strip():
                declared.append(raw.strip().replace("\\", "/").lstrip("/"))
            elif isinstance(raw, (list, tuple)):
                for item in raw:
                    text = str(item or "").strip().replace("\\", "/").lstrip("/")
                    if text:
                        declared.append(text)
    digests: list[WorkspaceFileDigest] = []
    missing: list[str] = []
    for rel in declared:
        target = workspace_root / rel
        if not target.is_file():
            missing.append(rel)
            digests.append(
                WorkspaceFileDigest(
                    path=rel,
                    exists=False,
                    size_bytes=0,
                    line_count=0,
                    sha256="",
                    kind=_file_kind(rel),
                )
            )
            continue
        try:
            content = target.read_bytes()
            text = content.decode("utf-8", errors="replace")
            line_count = len(text.splitlines()) if text else 0
            size_bytes = len(content)
        except OSError:
            missing.append(rel)
            continue
        digests.append(
            WorkspaceFileDigest(
                path=rel,
                exists=True,
                size_bytes=size_bytes,
                line_count=line_count,
                sha256=hash_value(content),
                kind=_file_kind(rel),
            )
        )
    source_count = 0
    test_count = 0
    config_count = 0
    file_count = 0
    try:
        for path in workspace_root.rglob("*"):
            if not path.is_file():
                continue
            file_count += 1
            rel = str(path.relative_to(workspace_root)).replace("\\", "/")
            kind = _file_kind(rel)
            if kind == "source":
                source_count += 1
            elif kind == "test":
                test_count += 1
            elif kind == "config":
                config_count += 1
    except OSError:
        file_count = 0
        source_count = 0
        test_count = 0
        config_count = 0
    return WorkspaceInventorySnapshot(
        workspace_hash=workspace_hash(workspace_root),
        file_count=file_count,
        source_count=source_count,
        test_count=test_count,
        config_count=config_count,
        active_declared_files=digests,
        recently_changed_files=digests[:12],
        missing_declared_files=missing,
    )


def _memory_items_from_proactive(proactive_memory: dict[str, Any] | None) -> list[MemorySelection]:
    if not isinstance(proactive_memory, dict):
        return []
    sections = proactive_memory.get("sections")
    if not isinstance(sections, list):
        return []
    items: list[MemorySelection] = []
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("name") or f"proactive.{idx}")
        text = str(section.get("content") or "")[:4000]
        refs = section.get("source_refs") if isinstance(section.get("source_refs"), list) else []
        items.append(
            MemorySelection(
                id=section_id,
                kind="proactive_core",
                tier="always_on",
                trust=str(section.get("trust") or "curated"),
                scope="core",
                phase=str(proactive_memory.get("phase_id") or ""),
                run_id=str(proactive_memory.get("run_id") or ""),
                evidence_refs=refs,
                selected_reason="proactive memory overlay",
                freshness="core",
                text=text,
                surface="directive",
                directive=True,
            )
        )
    return items


def _memory_items_from_recall(recall_bundle: dict[str, Any] | None) -> list[MemorySelection]:
    if not isinstance(recall_bundle, dict):
        return []
    items: list[MemorySelection] = []
    for tier, nodes in (
        ("always_on", recall_bundle.get("always_on")),
        ("hot", recall_bundle.get("hot")),
        ("warm", recall_bundle.get("warm")),
        ("graph", recall_bundle.get("graph_neighbours")),
    ):
        if not isinstance(nodes, list):
            continue
        for idx, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            memory_id = str(node.get("id") or f"{tier}.{idx}")
            text = str(node.get("content") or node.get("text") or "")[:4000]
            surface = "archive_hint" if tier == "graph" and not text.strip() else "supplemental_evidence"
            items.append(
                MemorySelection(
                    id=memory_id,
                    kind=str(node.get("kind") or tier),
                    tier=str(tier),
                    trust=str(node.get("trust") or "memory"),
                    scope=str(node.get("scope") or ""),
                    phase=str(node.get("phase") or ""),
                    run_id=str(node.get("run_id") or ""),
                    evidence_refs=list(node.get("evidence_refs") or []),
                    selected_reason=f"recall tier {tier}",
                    freshness=str(node.get("freshness") or "palace_recall"),
                    text=text,
                    surface=surface,
                    directive=False,
                )
            )
    return items


def _artifact_role_for_phase(phase_id: str, drive_root: Path | None) -> str:
    if drive_root is None:
        return "unknown"
    submitted = drive_root / "state" / "phase_plan_submitted_latest.json"
    proposal = drive_root / "state" / "phase_plan_proposal_latest.json"
    if phase_id == "plan_review":
        if submitted.is_file():
            return "submitted_plan"
        if proposal.is_file():
            return "proposal_for_review"
    if phase_id == "execute" and submitted.is_file():
        return "submitted_plan"
    return "phase_plan_json"


def compile_phase_context(
    *,
    workspace_root: Path,
    workspace_id: str,
    run_id: str,
    task_id: str,
    manifest: Any,
    phase_node: Any,
    domain_policy: dict | None = None,
    contract_bundle: Any | None = None,
    memory_client: Any | None = None,
    tool_filter: dict | None = None,
    capability_envelope: dict | None = None,
    active_subtask: dict | None = None,
    phase_prompt_sections: list[dict[str, str]] | None = None,
    authoritative_artifacts: list[dict[str, str]] | None = None,
    recall_bundle: dict[str, Any] | None = None,
    proactive_memory: dict[str, Any] | None = None,
    drive_root: Path | None = None,
) -> LLMInputBundle:
    del memory_client
    phase_id = str(getattr(phase_node, "id", "") or "")
    manifest_id = str(getattr(manifest, "id", "") or "")
    system_sections: list[LLMContextItem] = []
    user_sections: list[LLMContextItem] = []
    source_refs: list[ContextSourceRef] = []

    for idx, section in enumerate(phase_prompt_sections or []):
        path = str(section.get("path") or "")
        ref = ContextSourceRef(kind="prompt_file", path=path, phase=phase_id, run_id=run_id)
        system_sections.append(
            LLMContextItem(
                id=f"prompt.{idx}",
                role="phase_instruction",
                title=path or "phase prompt",
                text=str(section.get("text") or section.get("content") or "")[:8000],
                source=ref,
                trust="system",
            )
        )
        source_refs.append(ref)

    for idx, artifact in enumerate(authoritative_artifacts or []):
        path = str(artifact.get("path") or "")
        role = _artifact_role_for_phase(phase_id, drive_root)
        if "proposal" in path:
            role = "proposal_for_review"
        ref = ContextSourceRef(kind="authoritative_artifact", path=path, phase=phase_id, run_id=run_id)
        user_sections.append(
            LLMContextItem(
                id=f"artifact.{idx}",
                role="authoritative_artifact",
                title=path,
                text=str(artifact.get("text") or artifact.get("content") or "")[:4000],
                source=ref,
                freshness="current_run",
                trust="supervisor",
                include_reason=role,
            )
        )
        source_refs.append(ref)

    if phase_id == "execute" and active_subtask:
        scope_text = json.dumps(
            {
                "id": active_subtask.get("id"),
                "files_to_create": active_subtask.get("files_to_create"),
                "files_to_change": active_subtask.get("files_to_change"),
                "files_affected": active_subtask.get("files_affected"),
                "proof": active_subtask.get("proof"),
            },
            ensure_ascii=False,
            indent=2,
        )
        ref = ContextSourceRef(kind="manifest", path="phase_plan.json", phase=phase_id, run_id=run_id)
        user_sections.append(
            LLMContextItem(
                id="active_subtask",
                role="active_subtask",
                title=str(active_subtask.get("id") or "active"),
                text=scope_text,
                source=ref,
                freshness="current_run",
                trust="system",
            )
        )
        source_refs.append(ref)

    tool_contract = None
    if tool_filter:
        payload = json.dumps(tool_filter, sort_keys=True, ensure_ascii=False)
        tool_contract = ToolContractView(
            allowed_tools=list(tool_filter.get("allow") or []),
            forbidden_tools=list(tool_filter.get("deny") or []),
            required_calls=list(tool_filter.get("required") or []),
            tool_filter_hash=hash_value(payload),
        )

    capability_contract = None
    if capability_envelope:
        capability_contract = CapabilityContractView(
            phase=str(capability_envelope.get("phase") or phase_id),
            workspace_write=dict(capability_envelope.get("workspace_write") or {}),
            shell=dict(capability_envelope.get("shell") or {}),
            memory_write=dict(capability_envelope.get("memory_write") or {}),
            verification=dict(capability_envelope.get("verification") or {}),
            source=ContextSourceRef(kind="capability_policy", phase=phase_id, run_id=run_id),
        )

    inventory = _build_workspace_inventory(
        workspace_root, active_subtask=active_subtask
    )
    memory_items = _memory_items_from_recall(recall_bundle)
    proactive_items = _memory_items_from_proactive(proactive_memory)
    if proactive_items:
        memory_items = proactive_items + memory_items
    proactive_section_ids = [
        str(section.get("name") or "")
        for section in (proactive_memory or {}).get("sections", [])
        if isinstance(section, dict)
    ]
    hash_payload = {
        "phase_id": phase_id,
        "run_id": run_id,
        "system": [item.text[:200] for item in system_sections],
        "user": [item.text[:200] for item in user_sections],
        "active_subtask": active_subtask,
        "tool_filter": tool_filter,
        "inventory": inventory.missing_declared_files if inventory else [],
        "proactive_memory_hash": hash_value(
            json.dumps(proactive_memory or {}, sort_keys=True, ensure_ascii=False)
        ),
        "proactive_section_ids": proactive_section_ids,
    }
    return LLMInputBundle(
        schema_version="1",
        run_id=run_id,
        workspace_id=workspace_id,
        task_id=task_id,
        phase_id=phase_id,
        manifest_id=manifest_id,
        system_sections=system_sections,
        user_sections=user_sections,
        memory_items=memory_items,
        authoritative_artifacts=[item for item in user_sections if item.role == "authoritative_artifact"],
        contract_items=[],
        active_subtask=active_subtask,
        workspace_inventory=inventory,
        tool_contract=tool_contract,
        capability_contract=capability_contract,
        source_refs=source_refs,
        input_hash=hash_value(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False)),
    )
