---
name: harness-strategy
status: active
domains: ["execute", "harness", "parallel"]
phases: ["execute"]
when_to_use: "When multiple implementation candidates or phase candidates are available."
---

## Harness Strategy

Use candidate comparison to improve quality, not to multiply noise.

Selection criteria:
- Meets the task contract.
- Has coherent structure and minimal duplication.
- Provides real tests and run commands.
- Avoids generated artifacts outside the workspace.
- Has the strongest verification evidence.

Reject candidates that only pass shallow checks.
