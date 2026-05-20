---
name: Strict_Verification_Sync
status: candidate
domains: ["workflow_orchestration", "debugging"]
phases: ["execute", "verify", "subtask_review"]
when_to_use: "Immediately after executing shell commands or test suites that determine task completion."
params: [{"name": "success_exit_codes", "description": "A list of integers (e.g., [0]) that define a successful execution state."}]
created_by: reflection
source_run_id: sync_improve_c6ffdfcf
---

## Steps
1. Execute the verification or test command.
2. Capture the raw exit code of the process.
3. Compare the exit code against the configured success_exit_codes list.
4. If a match is found, override any heuristic beliefs and immediately set the internal state to 'Success/Pass', stopping further iteration.
5. If no match, proceed with error analysis and remediation.
