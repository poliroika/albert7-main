---
name: env-readiness
status: active
domains: ["preflight", "environment"]
phases: ["preflight"]
when_to_use: "At the start of a run to decide whether execution may proceed."
---

## Readiness Checks

Confirm the task can run with the current repository and credentials.

Required checks:
- LLM model, base URL, and API key availability across supported env families.
- MCP registry health.
- Workspace charter and task file readability.
- Palace memory health.
- Required skills and tools available for the upcoming phases.

Report blockers only when they prevent the run. Warnings should name the risk and let the run continue.
