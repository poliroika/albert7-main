---
name: decision-policy
status: active
domains: ["planning", "workflow_orchestration"]
phases: ["plan", "plan_review"]
when_to_use: "When building or reviewing a PhasePlan and need to decide how to decompose a task or choose between approaches."
---

## Decision framework

### Task decomposition
1. Read `palace.run.architecture_draft` to understand scope
2. Break into subtasks ≤ 200 LOC each where possible
3. Each subtask must have a concrete, runnable success_test
4. Avoid subtasks that can only be verified manually

### Choosing between approaches
- Prefer existing code (palace.codeptr) over writing from scratch
- Prefer MCP tools over custom shell scripts
- Choose the approach with the clearest success test

### Plan quality checklist
- [ ] Every subtask has a `success_test` (cmd or pytest id)
- [ ] Tool/skill list per subtask is minimal (no unused tools)
- [ ] Subtasks ordered by dependency (no circular deps)
- [ ] Final subtask validates the whole feature end-to-end
- [ ] Risky subtasks marked for harness_run if determinism unclear

## Anti-patterns

- Subtasks that are "write tests" in isolation without running them
- Subtasks with no tool constraints (too broad)
- Plans with >16 subtasks without a mid-plan checkpoint
