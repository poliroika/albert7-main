# Plan Context Overlay

The following workspace-specific context supplements the planning system prompt.

When decomposing tasks, consider:

- The specific acceptance criteria listed in the workspace charter
- Any existing code or structure in the workspace repository that subtasks should build on or refactor
- The preferred testing framework and verification approach specified in the charter
- Any time or complexity constraints that should influence subtask granularity

Prefer subtasks that each touch a single concern (one module, one feature, one test file) over large multi-concern subtasks.
