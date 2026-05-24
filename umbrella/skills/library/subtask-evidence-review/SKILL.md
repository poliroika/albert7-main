---
name: subtask-evidence-review
status: active
domains: ["verification", "review", "workflow_orchestration"]
phases: ["subtask_review"]
when_to_use: "Use in subtask_review to validate a completed subtask's CompletionContract and proof freshness."
---

## Evidence review checklist

1. Read the subtask card, CompletionContract, changed files, and cited proof refs.
2. Confirm each proof ref is ledger-backed and produced by verifier, supervisor, watcher, or harness.
3. Confirm proof was produced after the latest relevant patch and matches the current workspace/diff hashes when present.
4. Confirm `changed_files` overlaps the active subtask scope and any out-of-scope changes are justified by an accepted plan mutation.
5. Run `run_subtask_proof` or `run_workspace_verify` only as a read-only check when cited evidence is missing, stale, or ambiguous.
6. Use typed review issues for blockers; keep style-only suggestions in notes.
