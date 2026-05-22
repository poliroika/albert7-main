"""Render and persist LLM input bundles."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from umbrella.context.models import LLMInputBundle


def _item_dict(item: Any) -> dict[str, Any]:
    data = asdict(item)
    source = data.get("source")
    if source is not None and not isinstance(source, dict):
        data["source"] = asdict(source)
    return data


def bundle_to_overlay_dict(bundle: LLMInputBundle) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": bundle.schema_version,
        "run_id": bundle.run_id,
        "workspace_id": bundle.workspace_id,
        "task_id": bundle.task_id,
        "phase_id": bundle.phase_id,
        "manifest_id": bundle.manifest_id,
        "input_hash": bundle.input_hash,
        "system_sections": [_item_dict(item) for item in bundle.system_sections],
        "user_sections": [_item_dict(item) for item in bundle.user_sections],
        "memory_items": [asdict(item) for item in bundle.memory_items],
        "active_subtask": bundle.active_subtask,
        "workspace_inventory": (
            asdict(bundle.workspace_inventory)
            if bundle.workspace_inventory is not None
            else None
        ),
        "missing_declared_files": (
            list(bundle.workspace_inventory.missing_declared_files)
            if bundle.workspace_inventory is not None
            else []
        ),
        "allowed_tools": list(bundle.tool_contract.allowed_tools) if bundle.tool_contract else [],
        "forbidden_tools": list(bundle.tool_contract.forbidden_tools) if bundle.tool_contract else [],
        "capability_envelope": (
            {
                "phase": bundle.capability_contract.phase,
                "workspace_write": bundle.capability_contract.workspace_write,
                "shell": bundle.capability_contract.shell,
                "memory_write": bundle.capability_contract.memory_write,
                "verification": bundle.capability_contract.verification,
            }
            if bundle.capability_contract
            else {}
        ),
        "rendered_system_preview": "\n".join(
            item.title for item in bundle.system_sections[:6]
        ),
        "rendered_user_preview": "\n".join(item.title for item in bundle.user_sections[:8]),
    }
    return payload


def persist_llm_input_bundle(bundle: LLMInputBundle, drive_root: Path) -> Path:
    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = bundle_to_overlay_dict(bundle)
    latest = state_dir / "llm_input_bundle_latest.json"
    per_phase = state_dir / f"llm_input_bundle_{bundle.phase_id}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    latest.write_text(text, encoding="utf-8")
    per_phase.write_text(text, encoding="utf-8")
    return per_phase
