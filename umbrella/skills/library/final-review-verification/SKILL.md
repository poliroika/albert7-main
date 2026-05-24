---
name: final-review-verification
status: active
domains: ["verification", "review", "testing"]
phases: ["final_review"]
when_to_use: "Use in final_review to check end-to-end readiness without performing durable verification promotion."
---

## Final review checklist

1. Read the submitted plan and completed subtask evidence.
2. Run `run_workspace_verify` and treat nonzero or failing required steps as blockers.
3. Run `run_real_e2e` for integration, browser, localhost, or user-facing runtime claims.
4. Use only read-only diagnostics (`read_file`, logs, terminal scrollback, workspace logs) to explain failures.
5. Compare the implementation against the charter acceptance criteria.
6. If anything required is missing, call `loop_back_to` or `request_extra_subtask` with concrete evidence.

## Boundary

Do not call `submit_verification` or `promote_to_durable` in final review. Those calls belong to the verify phase after the final review passes.
