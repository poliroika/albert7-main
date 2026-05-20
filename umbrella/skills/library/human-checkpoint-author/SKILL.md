---
name: human-checkpoint-author
status: active
domains: ["verification", "handoff"]
phases: ["verify"]
when_to_use: "When a run needs user confirmation for a real-world choice, permission, or acceptance risk."
---

## Human Checkpoints

Ask for a checkpoint only when the run cannot safely infer the answer.

A checkpoint must include:
- The concrete decision needed.
- The current evidence.
- The risk of proceeding without the user.
- The recommended default if the user does not respond.

Do not use checkpoints to avoid normal debugging or verification.
