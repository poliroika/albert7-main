"""Shared agent guidance for deliberate external prior-art reuse."""

from typing import Any


def external_adoption_playbook(
    *,
    source_kind: str,
    source_handle: str,
    memory_paths: list[str] | None = None,
    licence_permissive: bool | None = None,
) -> dict[str, Any]:
    """Describe how the agent should record and later apply external evidence."""
    paths = [str(p) for p in (memory_paths or []) if str(p).strip()]
    return {
        "decide_intent": [
            "idea_only — architecture/approach only; cite in palace, reimplement yourself",
            "pattern_adapt — read stored snippets/docs; rewrite into workspace types (preferred)",
            "codeptr — record pointer + target workspace path (plan codeptr_refs / palace codeptr)",
            "dependency_import — add upstream package to pyproject only when it is a real library",
            "mcp_register — register MCP via mcp_install in plan (disabled until user enables)",
        ],
        "source_kind": source_kind,
        "source_handle": source_handle,
        "memory_paths": paths,
        "licence_permissive": licence_permissive,
        "palace_actions": [
            (
                "palace_add(kind=research_finding, source_id="
                f"{source_handle}, content=what you learned and intended reuse)"
            ),
            (
                "palace_add(kind=codeptr, source_id="
                f"{source_handle}/<path>, content=summary + intended workspace path)"
            ),
        ],
        "plan_actions": [
            "Attach codeptr_refs / notes on subtasks that will adapt this prior art",
            "State in subtask goal: idea_only | pattern_adapt | codeptr | dependency_import",
        ],
        "execute_actions": [
            "read_file on each memory_paths entry before apply_workspace_patch",
            "Do not paste non-permissive bodies; use idea_only when licence_permissive=false",
        ],
    }
