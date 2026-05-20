---
name: reflexion-author
status: active
domains: ["debugging", "self_improvement"]
phases: ["reflexion"]
when_to_use: "After a verify(fail) or final_review(loop_back) to generate a verbal reflection with evidence citations."
---

## Reflexion protocol

Reflexion (Shinn et al. 2023) improves future attempts via verbal self-feedback.
**Critical safety rule**: reflections are stored as `verified=false` and only promoted to
`palace.lesson` when a subsequent run that uses this reflection passes verify.

## How to write a reflection

1. Read the verify fail report from `palace.run`
2. Walk edges: `triggered_by_error`, `tests`, `implements` to trace root cause
3. Read recent terminal errors from `palace.transient`
4. Identify the specific failure mode (not a vague "improve X")

## Evidence citation requirement

Every sentence that makes a factual claim MUST cite an event:
`[ev:event_id]` for events, `[art:artifact_id]` for files/artifacts.

**Bad**: "The migration failed because of a type error."
**Good**: "The migration failed [ev:tools_42] due to a type mismatch between str and int [ev:events_38]."

## Reflection content

- What specific assumption was wrong?
- What concrete change would prevent this failure?
- Which phase/subtask needs to change?

Call `submit_reflection(text, applies_to_phase, evidence_refs=[...])` — rejected without citations.
