---
name: architecture-author
status: active
domains: ["architecture", "planning"]
phases: ["research", "plan"]
when_to_use: "When turning research into a concrete project architecture."
---

## Architecture Standard

Produce architecture that can be implemented and verified in the current workspace.

Required content:
- Clear module boundaries and file ownership.
- Runtime entrypoints for backend, frontend, workers, and tests when relevant.
- Data contracts between components.
- Dependency choices with a reason tied to the task.
- Verification hooks that prove real behavior, not import-only checks.

Avoid placeholder modules, detached demo files, and parallel implementations outside the workspace structure.
