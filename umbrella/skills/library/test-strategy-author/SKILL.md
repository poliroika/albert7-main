---
name: test-strategy-author
status: active
domains: ["planning", "testing"]
phases: ["plan"]
when_to_use: "When planning how the implementation will prove correctness."
---

## Test Strategy

Plan tests according to risk.

Include:
- Unit tests for deterministic domain logic.
- Integration tests for API and persistence boundaries.
- Build or type checks for frontend code.
- E2E or HTTP/browser checks for localhost apps.
- Negative tests for expected failure modes where useful.

Do not accept tests that only import modules for user-facing deliverables.
