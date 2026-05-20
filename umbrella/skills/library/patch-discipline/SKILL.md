---
name: patch-discipline
status: active
domains: ["implementation", "patching"]
phases: ["execute", "subtask_template"]
when_to_use: "When editing workspace files."
---

## Patch Discipline

Patch the source of truth with minimal, coherent changes.

Rules:
- Keep all task artifacts inside the target workspace unless the task asks otherwise.
- Use established project structure or create a clear src, tests, docs, and entrypoint layout.
- Remove obsolete files when replacing an approach.
- Avoid hardcoded success paths and fake outputs.
- Prefer structured parsers and APIs over fragile string hacks.

After patching, run the nearest relevant verification.
