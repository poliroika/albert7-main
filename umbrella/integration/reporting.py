"""
Manager reporting module - generates human-readable reports from manager runs.

This module provides functions to render comprehensive reports that summarize
manager activity, decisions, evidence, and outcomes.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from umbrella.integration.runner import ManagerRunResult

log = logging.getLogger(__name__)


def render_manager_report(
    result: ManagerRunResult,
    repo_root: Path | None = None,
) -> str:
    """Generate a comprehensive manager report.

    This report summarizes:
    - chosen seed workspace
    - loaded TASK_MAIN.md
    - retrieved GMAS evidence
    - workspace changes attempted
    - inspected run summaries and artifacts
    - human checkpoint status
    - evaluation result
    - lessons recorded
    - whether self-improvement was considered

    Args:
        result: Manager run result
        repo_root: Repository root for file references

    Returns:
        Formatted markdown report
    """
    lines = []
    add_section(lines, "# Umbrella Manager Run Report", level=1)

    # Header
    lines.extend(
        [
            f"**Task ID**: {result.task_id}",
            f"**Status**: {result.status.upper()}",
            f"**Task Success**: {result.task_success.upper()}",
            f"**Duration**: {result.duration_str}",
            f"**Completed**: {_format_timestamp(result.completed_at)}",
            "",
        ]
    )

    # Task Summary
    add_section(lines, "## Task Summary", level=2)
    lines.extend(
        [
            f"**Workspace**: {result.workspace_id or 'None selected'}",
            f"**Iterations**: {result.iterations}",
            f"**Final Outcome**: {result.task_success}",
            "",
        ]
    )

    # Workspace Selection
    if result.workspace_id:
        add_section(lines, "## Workspace Selection", level=2)
        lines.extend(
            [
                f"Selected seed workspace: `{result.workspace_id}`",
                "",
                "This workspace was chosen based on task classification and capability matching.",
                "",
            ]
        )

    # Phases Visited
    if result.phases_visited:
        add_section(lines, "## Execution Phases", level=2)
        lines.extend(
            [
                f"The manager progressed through {len(result.phases_visited)} phases:",
                "",
            ]
        )
        for i, phase in enumerate(result.phases_visited, 1):
            lines.append(f"{i}. `{phase}`")
        lines.append("")

    # Actions Taken
    if result.actions_taken:
        add_section(lines, "## Manager Actions", level=2)
        lines.extend(
            [
                f"The manager took {len(result.actions_taken)} actions:",
                "",
            ]
        )
        for action in result.actions_taken:
            lines.append(f"- **{action}**")
        lines.append("")

    # Workspace Changes
    if result.workspace_changes:
        add_section(lines, "## Workspace Modifications", level=2)
        lines.extend(
            [
                f"The manager made {len(result.workspace_changes)} modifications to the workspace:",
                "",
            ]
        )
        for change in result.workspace_changes:
            lines.append(f"- {change}")
        lines.append("")

    # Evidence
    if result.evidence:
        add_section(lines, "## Evidence & Observations", level=2)
        lines.extend(
            [
                f"Collected {len(result.evidence)} evidence items:",
                "",
            ]
        )
        for evidence in result.evidence[:20]:  # Limit evidence for readability
            lines.append(f"- {evidence}")
        if len(result.evidence) > 20:
            lines.append(f"- ... and {len(result.evidence) - 20} more items")
        lines.append("")

    # Evaluation Results
    add_section(lines, "## Evaluation", level=2)
    lines.extend(
        [
            f"**Lessons Recorded**: {result.lessons_recorded}",
            f"**Final Artifact**: {result.final_artifact_path or 'None'}",
            "",
        ]
    )

    # Self-Improvement
    if result.self_improvement_considered:
        add_section(lines, "## Self-Improvement", level=2)
        lines.extend(
            [
                "**Considered**: Yes",
                f"**Applied**: {'Yes' if result.self_improvement_applied else 'No'}",
                "",
                "The manager considered self-improvement as an option.",
                "",
            ]
        )

    # Human Interaction
    if result.human_checkpoints_requested > 0:
        add_section(lines, "## Human Interaction", level=2)
        lines.extend(
            [
                f"**Checkpoints Requested**: {result.human_checkpoints_requested}",
                f"**Checkpoints Approved**: {result.human_checkpoints_approved}",
                "",
                "Human approval was requested at key decision points.",
                "",
            ]
        )

    # Workspace-First Behavior
    add_section(lines, "## Workspace-First Behavior", level=2)
    workspace_first = len(result.workspace_changes) > 0 or result.task_success in (
        "complete",
        "partial",
    )
    if workspace_first:
        lines.extend(
            [
                "✓ **Workspace-first behavior confirmed**: The manager focused on improving ",
                "the workspace rather than directly solving the task or rewriting itself.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "⚠ **Workspace-first behavior unclear**: Limited workspace modifications detected.",
                "",
            ]
        )

    # Footer
    add_section(lines, "## Report Metadata", level=2)
    lines.extend(
        [
            f"**Generated**: {_format_timestamp(datetime.now(timezone.utc).timestamp())}",
            f"**Repository**: {repo_root or 'N/A'}",
            "",
        ]
    )

    return "\n".join(lines)


def render_promotion_report(
    result: ManagerRunResult,
) -> str | None:
    """Generate a promotion-focused report.

    Args:
        result: Manager run result

    Returns:
        Promotion report section or None if no promotion activity
    """
    if not result.evidence:
        return None

    promotion_evidence = [
        e
        for e in result.evidence
        if "promotion" in e.lower() or "candidate" in e.lower()
    ]

    if not promotion_evidence:
        return None

    lines = []
    add_section(lines, "## Promotion Consideration", level=2)

    lines.extend(
        [
            "The following promotion-related activity occurred:",
            "",
        ]
    )
    for evidence in promotion_evidence:
        lines.append(f"- {e}")
    lines.append("")

    return "\n".join(lines)


def add_section(lines: list[str], title: str, level: int = 2) -> None:
    """Add a section header to the report."""
    prefix = "#" * level
    lines.append(f"{prefix} {title}")
    lines.append("")


def _format_timestamp(ts: float | None) -> str:
    """Format a timestamp for display."""
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def save_report(
    result: ManagerRunResult,
    output_path: Path,
    repo_root: Path | None = None,
) -> None:
    """Save a manager report to file.

    Args:
        result: Manager run result
        output_path: Where to save the report
        repo_root: Repository root
    """
    report_content = render_manager_report(result, repo_root)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_content, encoding="utf-8")

    log.info(f"Saved manager report to {output_path}")
