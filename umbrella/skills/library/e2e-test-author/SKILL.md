---
name: e2e-test-author
status: active
domains: ["verification", "e2e", "browser"]
phases: ["final_review", "verify"]
when_to_use: "When acceptance requires localhost, HTTP, browser, or multi-process proof."
---

## E2E Proof

For web or integration work, require evidence that the real app was started and exercised.

Checklist:
- Start the declared backend/frontend entrypoints.
- Use localhost health and behavioral requests.
- For UI work, use a browser path that clicks the primary workflow.
- Capture terminal/log evidence for failures.
- Report exact commands, URLs, and failing assertions.

Import, compile, or build checks are supporting evidence only.
