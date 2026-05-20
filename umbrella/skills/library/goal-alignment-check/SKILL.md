---
name: goal-alignment-check
status: active
domains: ["review", "acceptance"]
phases: ["final_review"]
when_to_use: "When deciding whether the produced workspace satisfies the original user task."
---

## Alignment Check

Compare delivered behavior against the original task.

Required questions:
- Was every explicit user requirement implemented?
- Is the app or tool usable through the requested interface?
- Are tests and verification strong enough for the requested risk?
- Did implementation stay within the workspace and avoid unrelated root artifacts?
- Are placeholders, mock-only paths, or fake success signals still present?

If any answer is weak, request revision instead of approving.
