---
name: review-checklist
status: active
domains: ["review", "quality_gate"]
phases: ["research_review", "plan_review", "subtask_review"]
when_to_use: "At mini-review gates before allowing the next phase to proceed."
---

## Review Checklist → Coverage Keys

Every `submit_micro_review` must set all coverage keys to `true` and batch all blockers in one `issues` array.

| Coverage key | What to verify |
|--------------|----------------|
| `policy_conflicts` | Charter, permissions, forbidden paths, verifier policy |
| `oracle_compatibility` | Oracle types match task domain and runtime capabilities |
| `proof_strength` | Machine oracles, required_properties, anti-gaming |
| `scope_validity` | Files, sequencing, subtask ownership |
| `runtime_capabilities` | Proofs only require capabilities marked available in `capability_declaration.json` |
| `test_validity` | Tests, commands, scope overlap, no tampering risk |

Do not submit revise with a single issue when multiple dimensions fail — collect all blockers first.
