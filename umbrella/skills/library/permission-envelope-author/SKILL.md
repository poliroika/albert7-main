---
name: permission-envelope-author
status: active
domains: ["preflight", "permissions"]
phases: ["preflight"]
when_to_use: "When checking the run's allowed tools, forbidden tools, and path permissions."
---

## Permission Envelope

Confirm that each phase has the tools it needs and that risky tools are restricted.

Check:
- Allowed tools cover expected phase work.
- Forbidden tools match the phase boundary.
- Workspace writes are limited to the workspace during implementation.
- Verification can run commands needed by the task.
- Secrets and env files remain protected.

Report missing required tools before execution begins.
