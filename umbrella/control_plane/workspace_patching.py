"""
Real workspace patch application for task instances.

The goal is not open-ended code rewriting. The goal is a bounded, inspectable
set of instance-level mutations that the next run will actually consume.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from umbrella.retrieval.models import RetrievalCard
from umbrella.workspace_runtime.instances import (
    load_instance_metadata,
    update_instance_metadata,
)
from umbrella.workspace_runtime.models import WorkspaceInstance

_AGENT_RESEARCH_GRAPH_PATCH_MARKER = "draft_rewrite_evidence_loop_v1"


@dataclass
class WorkspacePatchResult:
    """Result of applying a bounded workspace patch."""

    applied: bool
    summary: str
    changed_files: list[str] = field(default_factory=list)
    patch_note_path: str | None = None
    snapshot_path: str | None = None
    graph_changed: bool = False
    runtime_overrides: dict[str, Any] = field(default_factory=dict)


class WorkspacePatchStrategy:
    """Strategy for bounded instance patching per workspace family."""

    strategy_id = "generic"

    def build_runtime_overrides(
        self,
        runtime_overrides: dict[str, Any],
        retrieval_summary: str,
    ) -> dict[str, Any]:
        desired_overrides = {
            "max_agent_executions": max(
                int(runtime_overrides.get("max_agent_executions", 0) or 0), 32
            ),
            "query_suffix": (
                "Repository-grounded execution guidance:\n"
                f"{retrieval_summary}\n"
                "Favor concrete, repo-specific outputs over generic phrasing."
            ),
        }
        current_mock = runtime_overrides.get("mock_loops")
        if current_mock is not None:
            desired_overrides["mock_loops"] = current_mock
        return desired_overrides

    def apply_graph_patch(self, topology_path: Path) -> bool:
        return False


class AgentResearchPatchStrategy(WorkspacePatchStrategy):
    """Patch behavior tuned for the article-writing workspace."""

    strategy_id = "agent_research"

    def build_runtime_overrides(
        self,
        runtime_overrides: dict[str, Any],
        retrieval_summary: str,
    ) -> dict[str, Any]:
        desired_overrides = super().build_runtime_overrides(
            runtime_overrides, retrieval_summary
        )
        desired_overrides["query_suffix"] = (
            "Repository-grounded article execution guidance:\n"
            f"{retrieval_summary}\n"
            "Favor concrete, repo-specific article revisions over generic phrasing."
        )
        return desired_overrides

    def apply_graph_patch(self, topology_path: Path) -> bool:
        return _append_agent_research_graph_patch(topology_path)


class WorldPredictionPatchStrategy(WorkspacePatchStrategy):
    """Patch behavior tuned for forecasting-style workspaces."""

    strategy_id = "world_prediction"

    def build_runtime_overrides(
        self,
        runtime_overrides: dict[str, Any],
        retrieval_summary: str,
    ) -> dict[str, Any]:
        desired_overrides = super().build_runtime_overrides(
            runtime_overrides, retrieval_summary
        )
        desired_overrides["query_suffix"] = (
            "Repository-grounded forecasting guidance:\n"
            f"{retrieval_summary}\n"
            "Favor falsifiable, source-backed predictions over generic summaries."
        )
        return desired_overrides


_PATCH_STRATEGIES: dict[str, WorkspacePatchStrategy] = {
    "agent_research": AgentResearchPatchStrategy(),
    "world_prediction": WorldPredictionPatchStrategy(),
}


def _summarize_retrieval(card: RetrievalCard | None) -> str:
    if card is None:
        return "No retrieval guidance was available."

    sections = [f"Recommended pattern: {card.recommended_pattern}"]

    if card.key_symbols:
        sections.append("Key symbols: " + ", ".join(card.key_symbols[:5]))
    if card.key_files:
        sections.append(
            "Key files: " + ", ".join(str(path) for path in card.key_files[:5])
        )
    if card.example_usage:
        sections.append("Example usage: " + " | ".join(card.example_usage[:2]))
    if card.doc_references:
        sections.append("Docs: " + " | ".join(card.doc_references[:2]))

    return "\n".join(sections)


def _append_agent_research_graph_patch(topology_path: Path) -> bool:
    if not topology_path.exists():
        return False

    content = topology_path.read_text(encoding="utf-8")
    if _AGENT_RESEARCH_GRAPH_PATCH_MARKER in content:
        return False

    edge_block = (
        f"\n# manager_patch: {_AGENT_RESEARCH_GRAPH_PATCH_MARKER}\n"
        "[[edges]]\n"
        'source = "draft_reviewer"\n'
        'target = "evidence_web_researcher"\n'
        "weight = 1.0\n"
        "priority = 95\n"
        'condition = "contains:REWRITE_DRAFT"\n'
    )

    if "\n[metadata]" in content:
        content = content.replace("\n[metadata]", edge_block + "\n[metadata]", 1)
    else:
        content = content.rstrip() + edge_block

    topology_path.write_text(content, encoding="utf-8")
    return True


def _workspace_key(instance: WorkspaceInstance) -> str:
    return instance.seed_workspace_id or instance.workspace_id


def _resolve_patch_strategy(instance: WorkspaceInstance) -> WorkspacePatchStrategy:
    return _PATCH_STRATEGIES.get(_workspace_key(instance), WorkspacePatchStrategy())


def apply_workspace_patch(
    instance: WorkspaceInstance,
    *,
    patch_description: str,
    retrieval_card: RetrievalCard | None = None,
    inspection_data: dict[str, Any] | None = None,
    snapshot_path: str | None = None,
) -> WorkspacePatchResult:
    """
    Apply a real, bounded patch to the workspace instance.

    Mutations:
    - update instance runtime overrides so the next run uses a healthier budget
      and a stable mock configuration;
    - inject retrieval-derived guidance into the runtime overrides;
    - evolve the instance graph with an evidence-refresh edge used on draft rewrites;
    - persist an auditable patch note inside the instance.
    """
    changed_files: list[str] = []
    instance_path = instance.path
    metadata = load_instance_metadata(instance_path) or {}
    runtime_overrides = dict(metadata.get("runtime_overrides", {}))
    retrieval_summary = _summarize_retrieval(retrieval_card)
    strategy = _resolve_patch_strategy(instance)

    metadata_changed = False

    desired_overrides = strategy.build_runtime_overrides(
        runtime_overrides, retrieval_summary
    )

    for key, value in desired_overrides.items():
        if runtime_overrides.get(key) != value:
            runtime_overrides[key] = value
            metadata_changed = True

    patch_history = list(metadata.get("manager_patch_history", []))
    patch_entry = {
        "patch_id": f"patch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
        "description": patch_description,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy.strategy_id,
        "runtime_overrides": runtime_overrides,
        "retrieval_confidence": retrieval_card.confidence if retrieval_card else 0.0,
        "snapshot_path": snapshot_path,
    }

    if not patch_history or patch_history[-1] != patch_entry:
        patch_history.append(patch_entry)
        metadata_changed = True

    if metadata_changed:
        update_instance_metadata(
            instance_path,
            {
                "runtime_overrides": runtime_overrides,
                "manager_patch_history": patch_history,
            },
        )
        changed_files.append(str(instance_path / "instance_metadata.json"))

    topology_path = instance_path / "graph" / "topology.toml"
    graph_changed = strategy.apply_graph_patch(topology_path)
    if graph_changed:
        changed_files.append(str(topology_path))

    note_lines = [
        f"# Manager Patch: {patch_description}",
        "",
        f"- Applied at: {datetime.now(timezone.utc).isoformat()}",
        f"- Strategy: {strategy.strategy_id}",
        f"- Snapshot: {snapshot_path or 'not created'}",
        f"- Graph changed: {'yes' if graph_changed else 'no'}",
        "",
        "## Runtime Overrides",
        "",
        json.dumps(runtime_overrides, indent=2, ensure_ascii=False),
        "",
        "## Retrieval Guidance",
        "",
        retrieval_summary,
    ]

    if inspection_data:
        note_lines.extend(
            [
                "",
                "## Inspection Context",
                "",
                json.dumps(
                    {
                        "manifest": inspection_data.get("manifest", {}),
                        "error_signatures": inspection_data.get("error_signatures", []),
                    },
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                ),
            ]
        )

    reports_dir = instance_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    patch_note_path = (
        reports_dir
        / f"manager_patch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.md"
    )
    patch_note_path.write_text("\n".join(note_lines).rstrip() + "\n", encoding="utf-8")
    changed_files.append(str(patch_note_path))

    applied = bool(changed_files)
    summary = (
        f"Applied {strategy.strategy_id} workspace patch touching {len(changed_files)} file(s)"
        if applied
        else "Workspace patch request produced no file changes"
    )

    return WorkspacePatchResult(
        applied=applied,
        summary=summary,
        changed_files=changed_files,
        patch_note_path=str(patch_note_path),
        snapshot_path=snapshot_path,
        graph_changed=graph_changed,
        runtime_overrides=runtime_overrides,
    )
