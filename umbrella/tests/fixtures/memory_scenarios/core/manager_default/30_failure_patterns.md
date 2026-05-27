# Anti-patterns

Do not repeat known failure modes without mitigation.

## Phase exit evidence

- False `diff_hash_mismatch` on phase exit when subtask proof already passed: phase-exit validation must use completion `changed_files`, not an empty diff context.

## Loop-back routing

- Stale `loop_back_to(plan)` during execute after `mark_subtask_complete` or `mutate_phase_plan` repaired the active subtask: contract loop-back targets win; older loop-back signals are superseded.

## Greenfield Python setup

- Bare `import <package>` proof on `src/<package>/` layouts without `sys.path.insert(0,'src')` or pytest: fails from workspace root; `pip install -e .` often conflicts with enforcement (egg-info).
