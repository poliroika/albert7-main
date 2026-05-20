---
name: gmas-pattern-author
status: active
domains: ["planning", "multi_agent", "gmas"]
phases: ["plan"]
when_to_use: "When planning a task that includes LLM-backed or multi-agent behavior."
---

## GMAS Plan Pattern

A plan using GMAS must name:
- Agent roles and responsibilities.
- Shared state and message schema.
- Tool or action contracts each agent may call.
- Turn or workflow orchestration.
- Tests that distinguish real LLM-backed behavior from static placeholders.

Keep deterministic rules separate from agent reasoning.
