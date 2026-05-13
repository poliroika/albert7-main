"""Umbrella-owned context overlays passed into Ouroboros tasks."""

from pathlib import Path


def build_prompt_governance_overlay(repo_root: Path) -> str:
    """Render Umbrella's prompt-governance contract for the agent context."""

    catalog: list[tuple[str, str]] = [
        ("ouroboros_system_prompt", "ouroboros/prompts/SYSTEM.md"),
        ("ouroboros_bible", "ouroboros/BIBLE.md"),
        ("ouroboros_context_assembly", "ouroboros/ouroboros/context.py"),
        ("ouroboros_task_planner_prompts", "ouroboros/ouroboros/task_planner.py"),
        ("umbrella_delivery_critic", "umbrella/control_plane/critic.py"),
        (
            "umbrella_workspace_task_wrapper",
            "umbrella/prompts/ouroboros_workspace_task.md",
        ),
        ("umbrella_prompt_policy", "umbrella/control_plane/prompt_policy.py"),
        ("umbrella_human_gate_policy", "umbrella/control_plane/human_checkpoints.py"),
    ]
    root = Path(repo_root)
    lines = [
        "## Prompt Stack Governance",
        "",
        "Rewrite prompt surfaces only through approved tooling (`list_prompt_surfaces`, "
        "`propose_prompt_patch`, human checkpoints). Prefer small, test-backed diffs.",
        "",
        "Formal surfaces:",
        "",
    ]
    for surface_id, rel in catalog:
        tag = "[tracked]" if (root / rel).is_file() else "[missing in checkout]"
        lines.append(f"- `{surface_id}` — `{rel}` {tag}")
    return "\n".join(lines) + "\n"


def build_context_overlays(repo_root: Path) -> dict[str, str]:
    return {
        "prompt_governance": build_prompt_governance_overlay(repo_root),
    }
