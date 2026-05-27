"""
TASK_MAIN.md workspace contract module.

Defines and implements TASK_MAIN.md as a universal workspace contract.
Every workspace should have this file as the canonical statement of
the workspace's primary task.

This module provides:
- Parsing and loading of TASK_MAIN.md files
- Validation of required sections
- Template rendering for new workspaces
- Task instance initialization
- Conversion to TaskBrief for workspace selection
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from umbrella.workspace_registry.models import (
    TaskBrief,
    ValidationIssue,
    ValidationSeverity,
)


class TaskMainSection(str, Enum):
    """Required and optional sections in TASK_MAIN.md."""

    # Required sections
    OBJECTIVE = "Objective"
    FINAL_DELIVERABLE = "Final Deliverable"
    SUCCESS_CRITERIA = "Success Criteria"
    CONSTRAINTS = "Constraints"
    STARTING_POINT = "Starting Point"
    HUMAN_CHECKPOINTS = "Human Checkpoints"
    LONG_RUN_POLICY = "Long-Run Policy"

    # Optional sections
    NOTES = "Notes"


# All core contract sections (Task 02a); Notes is optional.
REQUIRED_SECTIONS = [
    TaskMainSection.OBJECTIVE,
    TaskMainSection.FINAL_DELIVERABLE,
    TaskMainSection.SUCCESS_CRITERIA,
    TaskMainSection.CONSTRAINTS,
    TaskMainSection.STARTING_POINT,
    TaskMainSection.HUMAN_CHECKPOINTS,
    TaskMainSection.LONG_RUN_POLICY,
]


@dataclass
class TaskMainDocument:
    """
    Parsed representation of a TASK_MAIN.md file.

    This is the canonical task contract for a workspace.
    """

    path: Path
    raw_content: str

    # Parsed sections (section_name -> content)
    sections: dict[str, str] = field(default_factory=dict)

    # Title (first # heading)
    title: str = ""

    # Metadata
    has_required_sections: bool = False
    missing_sections: list[str] = field(default_factory=list)

    @property
    def objective(self) -> str:
        """Get the objective section content."""
        return self.sections.get(TaskMainSection.OBJECTIVE.value, "")

    @property
    def final_deliverable(self) -> str:
        """Get the final deliverable section content."""
        return self.sections.get(TaskMainSection.FINAL_DELIVERABLE.value, "")

    @property
    def success_criteria(self) -> str:
        """Get the success criteria section content."""
        return self.sections.get(TaskMainSection.SUCCESS_CRITERIA.value, "")

    @property
    def constraints(self) -> str:
        """Get the constraints section content."""
        return self.sections.get(TaskMainSection.CONSTRAINTS.value, "")

    @property
    def starting_point(self) -> str:
        """Get the starting point section content."""
        return self.sections.get(TaskMainSection.STARTING_POINT.value, "")

    @property
    def human_checkpoints(self) -> str:
        """Get the human checkpoints section content."""
        return self.sections.get(TaskMainSection.HUMAN_CHECKPOINTS.value, "")

    @property
    def long_run_policy(self) -> str:
        """Get the long-run policy section content."""
        return self.sections.get(TaskMainSection.LONG_RUN_POLICY.value, "")

    @property
    def notes(self) -> str:
        """Get the notes section content."""
        return self.sections.get(TaskMainSection.NOTES.value, "")

    def get_section(self, section: TaskMainSection) -> str:
        """Get content for a specific section."""
        return self.sections.get(section.value, "")

    def has_section(self, section: TaskMainSection) -> bool:
        """Check if a section exists."""
        return section.value in self.sections


def load_task_main(path: Path) -> TaskMainDocument | None:
    """
    Load and parse a TASK_MAIN.md file.

    Args:
        path: Path to the TASK_MAIN.md file

    Returns:
        TaskMainDocument if successful, None if file doesn't exist
    """
    if not path.exists():
        return None

    try:
        raw_content = path.read_text(encoding="utf-8")
    except Exception:
        return None

    return parse_task_main(raw_content, path)


def _normalize_section_title(name: str) -> str:
    """Map heading text to canonical TaskMainSection titles when they match."""
    s = name.strip()
    for sec in TaskMainSection:
        if sec.value.lower() == s.lower():
            return sec.value
    return s


def parse_task_main(content: str, path: Path | None = None) -> TaskMainDocument:
    """
    Parse TASK_MAIN.md content into a structured document.

    Uses markdown heading detection to extract sections.

    Args:
        content: Raw markdown content
        path: Optional path to the file

    Returns:
        TaskMainDocument with parsed sections
    """
    if path is None:
        path = Path(".")

    sections: dict[str, str] = {}
    title = ""

    # Extract title (first # heading)
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    # Pattern to match section headings: ## N. Section Name or ## Section Name
    # Use explicit space character instead of \s to avoid matching newlines
    section_pattern = re.compile(
        r"^##\s+(?:\d+\.?\s+)?([A-Za-z][A-Za-z \-]+)$", re.MULTILINE
    )

    # Find all section positions
    section_matches = list(section_pattern.finditer(content))

    for i, match in enumerate(section_matches):
        section_name = _normalize_section_title(match.group(1).strip())
        start_pos = match.end()

        # Find end position (next section or end of file)
        if i + 1 < len(section_matches):
            end_pos = section_matches[i + 1].start()
        else:
            end_pos = len(content)

        section_content = content[start_pos:end_pos].strip()
        sections[section_name] = section_content

    # Check for required sections
    missing = []
    for req_section in REQUIRED_SECTIONS:
        if req_section.value not in sections:
            # Try case-insensitive match
            found = False
            for key in sections:
                if key.lower() == req_section.value.lower():
                    found = True
                    break
            if not found:
                missing.append(req_section.value)

    has_required = len(missing) == 0

    return TaskMainDocument(
        path=path,
        raw_content=content,
        sections=sections,
        title=title,
        has_required_sections=has_required,
        missing_sections=missing,
    )


def validate_task_main_content(document: TaskMainDocument) -> list[ValidationIssue]:
    """
    Validate a TaskMainDocument for completeness and quality.

    Args:
        document: The parsed TASK_MAIN document

    Returns:
        List of validation issues
    """
    issues = []

    # Check for missing required sections
    for missing in document.missing_sections:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message=f"Missing required section: {missing}",
                field=f"section.{missing.lower().replace(' ', '_')}",
                path=document.path,
                suggestion=f"Add a '## {missing}' section to TASK_MAIN.md",
            )
        )

    # Check content quality for required sections
    for req_section in REQUIRED_SECTIONS:
        content = document.sections.get(req_section.value, "")
        if content and len(content.strip()) < 20:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    message=f"Section '{req_section.value}' is very short",
                    field=f"section.{req_section.value.lower().replace(' ', '_')}",
                    path=document.path,
                    suggestion=f"Expand the {req_section.value} section with more detail",
                )
            )

    return issues


def validate_task_main_at_path(path: Path) -> list[ValidationIssue]:
    """
    Validate a TASK_MAIN.md file at a given path.

    Args:
        path: Path to the TASK_MAIN.md file

    Returns:
        List of validation issues
    """
    issues = []

    if not path.exists():
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message="Required file not found: TASK_MAIN.md",
                path=path,
                suggestion="Create TASK_MAIN.md with task description, objectives, and constraints.",
            )
        )
        return issues

    document = load_task_main(path)
    if document is None:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message="Failed to parse TASK_MAIN.md",
                path=path,
            )
        )
        return issues

    issues.extend(validate_task_main_content(document))
    return issues


def validate_task_main_sections(document: TaskMainDocument) -> list[ValidationIssue]:
    """
    Validate that required sections exist in a TaskMainDocument.

    This is a convenience function for use by validation.py.

    Args:
        document: The parsed TASK_MAIN document

    Returns:
        List of validation issues
    """
    return validate_task_main_content(document)


def render_task_main_template(
    title: str = "TASK_MAIN",
    objective: str = "",
    final_deliverable: str = "",
    success_criteria: str = "",
    constraints: str = "",
    starting_point: str = "",
    human_checkpoints: str = "",
    long_run_policy: str = "",
    notes: str = "",
) -> str:
    """
    Render a TASK_MAIN.md template with provided values.

    Args:
        title: Document title
        objective: Objective section content
        final_deliverable: Final deliverable section content
        success_criteria: Success criteria section content
        constraints: Constraints section content
        starting_point: Starting point section content
        human_checkpoints: Human checkpoints section content
        long_run_policy: Long-run policy section content
        notes: Additional notes

    Returns:
        Rendered markdown content
    """
    lines = [f"# {title}", ""]

    def add_section(number: int, name: str, content: str) -> None:
        lines.append(f"## {number}. {name}")
        lines.append("")
        if content:
            lines.append(content)
        else:
            lines.append(f"[TODO: Describe {name.lower()}]")
        lines.append("")

    add_section(1, "Objective", objective)
    add_section(2, "Final Deliverable", final_deliverable)
    add_section(3, "Success Criteria", success_criteria)
    add_section(4, "Constraints", constraints)
    add_section(5, "Starting Point", starting_point)
    add_section(6, "Human Checkpoints", human_checkpoints)
    add_section(7, "Long-Run Policy", long_run_policy)
    add_section(8, "Notes", notes)

    return "\n".join(lines)


def initialize_task_main_for_instance(
    instance_path: Path,
    seed_document: TaskMainDocument | None,
    task_brief: TaskBrief,
    task_id: str | None = None,
) -> Path:
    """
    Create a TASK_MAIN.md for a task instance workspace.

    This either:
    - Copies and adapts the seed's TASK_MAIN.md, or
    - Creates a new one from the task brief

    Args:
        instance_path: Path to the instance workspace directory
        seed_document: Optional seed workspace's TASK_MAIN document
        task_brief: The task brief for this instance
        task_id: Optional task ID for tracking

    Returns:
        Path to the created TASK_MAIN.md file
    """
    task_main_path = instance_path / "TASK_MAIN.md"

    # Build objective from task brief
    objective = task_brief.description

    # Preserve the seed contract when it already defines a concrete deliverable.
    final_deliverable = "[TODO: Define the concrete artifact or behavior expected]"
    if seed_document and seed_document.final_deliverable:
        final_deliverable = seed_document.final_deliverable

    # Build success criteria from task constraints
    success_criteria_parts = []
    if seed_document and seed_document.success_criteria:
        success_criteria_parts.append(seed_document.success_criteria)

    instance_specific_parts = []
    if task_brief.constraints:
        for key, value in task_brief.constraints.items():
            instance_specific_parts.append(f"- {key}: {value}")
    if task_brief.required_capabilities:
        instance_specific_parts.append(
            "- Must demonstrate: " + ", ".join(task_brief.required_capabilities)
        )
    if instance_specific_parts:
        if success_criteria_parts:
            success_criteria_parts.append(
                "Instance-specific expectations:\n\n"
                + "\n".join(instance_specific_parts)
            )
        else:
            success_criteria_parts.extend(instance_specific_parts)
    success_criteria = (
        "\n\n".join(success_criteria_parts)
        if success_criteria_parts
        else "[TODO: Define success criteria]"
    )

    # Start with seed's constraints if available
    constraints = ""
    if seed_document and seed_document.constraints:
        constraints = f"Inherited from seed workspace:\n\n{seed_document.constraints}"
    else:
        constraints = "[TODO: Define constraints]"

    # Starting point
    starting_point = ""
    if seed_document:
        starting_point = f"This instance is derived from a seed workspace with the following starting point:\n\n{seed_document.starting_point}"
    else:
        starting_point = "[TODO: Describe initial state]"

    # Human checkpoints from seed
    human_checkpoints = ""
    if seed_document and seed_document.human_checkpoints:
        human_checkpoints = seed_document.human_checkpoints
    else:
        human_checkpoints = "[TODO: Define human checkpoints]"

    # Long-run policy from seed
    long_run_policy = ""
    if seed_document and seed_document.long_run_policy:
        long_run_policy = seed_document.long_run_policy
    else:
        long_run_policy = "[TODO: Define long-run policy]"

    # Notes with task metadata
    notes_parts = [f"Task ID: {task_id or 'not specified'}"]
    if task_brief.task_class:
        notes_parts.append(f"Task class: {task_brief.task_class}")
    if task_brief.domains:
        notes_parts.append(f"Domains: {', '.join(task_brief.domains)}")
    notes = "\n".join(notes_parts)

    # Render the template
    title = f"TASK_MAIN - {task_brief.description[:50]}"
    if len(task_brief.description) > 50:
        title = title.rstrip(".") + "..."

    content = render_task_main_template(
        title=title,
        objective=objective,
        final_deliverable=final_deliverable,
        success_criteria=success_criteria,
        constraints=constraints,
        starting_point=starting_point,
        human_checkpoints=human_checkpoints,
        long_run_policy=long_run_policy,
        notes=notes,
    )

    # Ensure directory exists
    instance_path.mkdir(parents=True, exist_ok=True)

    # Write the file
    task_main_path.write_text(content, encoding="utf-8")

    return task_main_path


def build_task_brief_from_task_main(
    document: TaskMainDocument,
    task_id: str | None = None,
    preferred_workspace_id: str | None = None,
) -> TaskBrief:
    """
    Build a TaskBrief from a TaskMainDocument.

    This allows the runtime and manager to derive a task brief
    from the canonical TASK_MAIN.md file.

    Args:
        document: The parsed TASK_MAIN document
        task_id: Optional task ID for tracking
        preferred_workspace_id: Optional preferred workspace ID

    Returns:
        TaskBrief suitable for workspace selection
    """
    # Use objective as primary description
    description = document.objective or document.title or "No objective specified"

    workspace_root = document.path.parent
    from umbrella.workspace_registry.charter import charter_capability_slugs, load_workspace_charter

    charter = load_workspace_charter(workspace_root)
    required_capabilities = charter_capability_slugs(charter)
    task_class = _explicit_task_class_from_document(document)
    domains: list[str] = []

    # Build constraints from sections
    constraints: dict[str, Any] = {}
    if document.constraints:
        constraints["has_explicit_constraints"] = True
    if document.human_checkpoints:
        constraints["requires_human_oversight"] = True
    if document.long_run_policy:
        constraints["long_running"] = True

    return TaskBrief(
        description=description,
        task_id=task_id,
        task_class=task_class,
        domains=domains,
        required_capabilities=required_capabilities,
        preferred_workspace_id=preferred_workspace_id,
        constraints=constraints,
        metadata={
            "source": "task_main",
            "title": document.title,
            "has_required_sections": document.has_required_sections,
            "capability_seeds_from_charter": bool(required_capabilities),
        },
    )


def _explicit_task_class_from_document(document: TaskMainDocument) -> str | None:
    """Optional explicit task class from a `## Task Class` section (no keyword inference)."""

    raw = document.sections.get("Task Class", "").strip().lower()
    if not raw:
        return None
    return raw.replace(" ", "_")


def infer_task_class(document: TaskMainDocument) -> str | None:
    """Deprecated: use explicit `## Task Class` section or manager task_class."""

    return _explicit_task_class_from_document(document)


def infer_domains(document: TaskMainDocument) -> list[str]:
    """Deprecated: domains are not inferred; use capability_declaration after research."""

    _ = document
    return []


def infer_capabilities(document: TaskMainDocument) -> list[str]:
    """Deprecated: use workspace.toml [[capabilities]] seeds."""

    from umbrella.workspace_registry.charter import charter_capability_slugs, load_workspace_charter

    return charter_capability_slugs(load_workspace_charter(document.path.parent))
