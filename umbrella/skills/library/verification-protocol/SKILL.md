---
name: verification-protocol
status: active
domains: ["verification", "testing"]
phases: ["verify", "final_review"]
when_to_use: "In the verify phase when checking if all tests pass and the task goal is met."
---

## Verification checklist

1. Run `run_workspace_verify` — must pass
2. Run `run_real_e2e` if configured in workspace.toml `[verify.real_e2e]`
3. Check `palace.run` for any open `unresolved_risks`
4. Confirm all subtasks have status=done
5. Treat unresolved runtime errors, user-facing limitations, or "passes but still requires fixes" notes as verify failures.

## On verify pass

Call `promote_to_durable` for the verification report, then
`submit_verification(status="pass", details="...")`.

## On verify fail

1. Call `loop_back_to(phase="execute", reason="<failing test + error>")` when implementation fixes are needed
2. Call `submit_verification(status="fail", details="<failing test + error>")`
3. Runner automatically triggers `reflexion` phase
4. After reflexion, runner loops back to `execute` or `plan` depending on failure severity:
   - Test failure in specific subtask → loop_back to execute with remediation subtask
   - Design flaw found → loop_back to plan

## Evidence requirement

The verify failure message in `details` must be specific:
- Which test failed
- Error message verbatim (first 500 chars)
- Which file/function is broken
