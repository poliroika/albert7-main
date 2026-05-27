---
name: error-self-repair
status: active
domains: ["debugging", "repair"]
phases: ["execute", "subtask_template"]
when_to_use: "When a command, test, tool call, or runtime path fails."
---

## Repair Loop

Use a tight diagnose, patch, verify cycle.

Steps:
- Read the exact failing output.
- Identify the smallest systemic cause.
- Patch the source of truth, not generated output.
- Remove obsolete paths made wrong by the patch.
- Re-run the failing command first, then broader verification.

Do not hide failures with bypasses or weaker checks.

When a tool returns `allowed_next_tools`, only call tools from that list until the gate clears.
