"""Compile auditable LLM input bundles per phase."""

import json
from pathlib import Path
from typing import Any

from umbrella.contracts.hashing import hash_value, workspace_hash
from umbrella.context.models import (
    CapabilityContractView,
    ContextSourceRef,
    HarnessContractView,
    CurrentPhaseEnvelope,
    LLMContextItem,
    LLMInputBundle,
    MemorySelection,
    ToolContractView,
    WorkspaceFileDigest,
    WorkspaceInventorySnapshot,
)
from umbrella.contracts.harness_profiles import (
    build_harness_contract_payload,
    render_harness_contract_markdown,
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


def _build_current_phase_envelope(
    *,
    phase_id: str,
    active_subtask: dict | None,
    drive_root: Path | None,
) -> CurrentPhaseEnvelope | None:
    if phase_id != "execute" or not active_subtask:
        return None
    allowed: list[str] = []
    for key in ("files_to_create", "files_to_change", "files_affected"):
        raw = active_subtask.get(key)
        if isinstance(raw, str) and raw.strip():
            allowed.append(raw.strip().replace("\\", "/").lstrip("/"))
        elif isinstance(raw, (list, tuple)):
            allowed.extend(
                str(item).strip().replace("\\", "/").lstrip("/")
                for item in raw
                if str(item).strip()
            )
    last_failure = ""
    if drive_root is not None:
        tools_path = drive_root / "logs" / "tools.jsonl"
        if tools_path.is_file():
            try:
                lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            for line in reversed(lines[-40:]):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw = str(row.get("result_preview") or row.get("result") or "")
                if raw.strip().lower().startswith("error:") or '"status": "blocked"' in raw:
                    last_failure = raw[:400]
                    break
    return CurrentPhaseEnvelope(
        goal=str(active_subtask.get("goal") or active_subtask.get("title") or ""),
        active_subtask=str(active_subtask.get("id") or ""),
        allowed_files=tuple(sorted(set(allowed))),
        last_failure=last_failure,
        open_issues=(),
        forbidden_repeats=(),
    )


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
    subtask_memory_chunks: list[dict[str, Any]] | None = None,
) -> LLMInputBundle:
    del memory_client
    phase_id = str(getattr(phase_node, "id", "") or "")
    manifest_id = str(getattr(manifest, "id", "") or "")
    system_sections: list[LLMContextItem] = []
    user_sections: list[LLMContextItem] = []
    contract_items: list[LLMContextItem] = []
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

    active_subtask_id = (
        str(active_subtask.get("id") or "").strip()
        if isinstance(active_subtask, dict)
        else ""
    )
    if phase_id == "execute" and active_subtask:
        envelope = _build_current_phase_envelope(
            phase_id=phase_id,
            active_subtask=active_subtask,
            drive_root=drive_root,
        )
        scope_text = json.dumps(
            {
                "goal": envelope.goal if envelope else "",
                "active_subtask": envelope.active_subtask if envelope else active_subtask.get("id"),
                "allowed_files": list(envelope.allowed_files) if envelope else [],
                "last_failure": envelope.last_failure if envelope else "",
                "open_issues": list(envelope.open_issues) if envelope else [],
                "forbidden_repeats": list(envelope.forbidden_repeats) if envelope else [],
            },
            ensure_ascii=False,
            indent=2,
        )
        ref = ContextSourceRef(kind="phase_envelope", path="current_phase_envelope", phase=phase_id, run_id=run_id)
        user_sections.append(
            LLMContextItem(
                id="current_phase_envelope",
                role="phase_envelope",
                title="Current phase envelope",
                text=scope_text,
                source=ref,
                freshness="current_run",
                trust="system",
            )
        )
        source_refs.append(ref)

    if phase_id == "execute" and subtask_memory_chunks:
        for idx, chunk in enumerate(subtask_memory_chunks):
            if not isinstance(chunk, dict):
                continue
            ref = str(chunk.get("ref") or "")
            kind = str(chunk.get("kind") or "asset")
            inject_mode = str(chunk.get("inject_mode") or "on_demand")
            if inject_mode == "search_only" or not chunk.get("loaded"):
                continue
            text = str(chunk.get("text") or chunk.get("preview") or "")[:4000]
            if not text.strip():
                continue
            item_ref = ContextSourceRef(
                kind="subtask_memory_asset",
                path=ref,
                phase=phase_id,
                run_id=run_id,
            )
            user_sections.append(
                LLMContextItem(
                    id=f"subtask_memory.{idx}",
                    role="subtask_memory_asset",
                    title=f"{kind}: {ref}",
                    text=text,
                    source=item_ref,
                    freshness="current_run",
                    trust="supervisor",
                    include_reason=inject_mode,
                )
            )
            source_refs.append(item_ref)

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
        fixed_capability_keys = {
            "phase",
            "workspace_write",
            "shell",
            "memory_write",
            "verification",
        }
        capability_contract = CapabilityContractView(
            phase=str(capability_envelope.get("phase") or phase_id),
            workspace_write=dict(capability_envelope.get("workspace_write") or {}),
            shell=dict(capability_envelope.get("shell") or {}),
            memory_write=dict(capability_envelope.get("memory_write") or {}),
            verification=dict(capability_envelope.get("verification") or {}),
            source=ContextSourceRef(kind="capability_policy", phase=phase_id, run_id=run_id),
            extra={
                str(key): value
                for key, value in capability_envelope.items()
                if str(key) not in fixed_capability_keys
            },
        )

    harness_payload = build_harness_contract_payload(
        phase_id=phase_id,
        active_subtask=active_subtask,
        capability_envelope=capability_envelope,
    )
    harness_contract = None
    if harness_payload.get("mode") != "none":
        payload_text = json.dumps(harness_payload, sort_keys=True, ensure_ascii=False)
        ref = ContextSourceRef(
            kind="harness_contract",
            path="umbrella/contracts/harness_profiles.py",
            phase=phase_id,
            run_id=run_id,
            hash=hash_value(payload_text),
        )
        harness_contract = HarnessContractView(
            schema_version=str(harness_payload.get("schema_version") or "1"),
            mode=str(harness_payload.get("mode") or ""),
            selected_ids=list(harness_payload.get("selected_ids") or []),
            reason=str(harness_payload.get("reason") or ""),
            profiles=list(harness_payload.get("profiles") or []),
            source=ref,
        )
        rendered_harness = render_harness_contract_markdown(harness_payload)
        if rendered_harness.strip():
            contract_items.append(
                LLMContextItem(
                    id="harness_contract",
                    role="harness_contract",
                    title=(
                        "Harness profile catalog"
                        if harness_contract.mode == "catalog"
                        else "Active harness contract"
                    ),
                    text=rendered_harness[:5000],
                    source=ref,
                    freshness="current_run",
                    trust="system",
                    include_reason=harness_contract.reason,
                )
            )
            source_refs.append(ref)

    inventory = _build_workspace_inventory(
        workspace_root, active_subtask=active_subtask
    )
    memory_items = _memory_items_from_recall(recall_bundle)
    proactive_items = _memory_items_from_proactive(proactive_memory)
    if proactive_items and phase_id != "execute":
        memory_items = proactive_items + memory_items
    elif proactive_items and phase_id == "execute":
        memory_items = proactive_items[:2] + memory_items
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
        "active_subtask_id": active_subtask_id,
        "active_subtask": active_subtask,
        "tool_filter": tool_filter,
        "inventory": inventory.missing_declared_files if inventory else [],
        "contract_items": [item.text[:300] for item in contract_items],
        "harness_contract": harness_payload,
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
        contract_items=contract_items,
        active_subtask_id=active_subtask_id or None,
        active_subtask=active_subtask,
        workspace_inventory=inventory,
        tool_contract=tool_contract,
        capability_contract=capability_contract,
        harness_contract=harness_contract,
        source_refs=source_refs,
        input_hash=hash_value(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False)),
    )
