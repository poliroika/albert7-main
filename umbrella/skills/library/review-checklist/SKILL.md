---
name: review-checklist
status: active
domains: ["review", "quality_gate"]
phases: ["research_review", "plan_review", "subtask_review"]
when_to_use: "At mini-review gates before allowing the next phase to proceed."
---

## Review Checklist

Approve only when the previous phase produced enough evidence.

Research review:
- At least one architecture direction.
- Relevant prior art or explicit reason none was useful.
- Tool and skill availability checked.

Plan review:
- Concrete files, tests, and run commands.
- No detached artifacts outside the workspace.
- Verification path matches the user-facing goal.

Subtask review:
- Changes are scoped.
- Tests or inspection prove the subtask behavior.
- Follow-up failures are routed back to execute.
