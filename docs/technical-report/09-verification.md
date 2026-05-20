# Part 9: Verification

Verification is the final gate between agent self-completion and result acceptance. It runs real subprocess commands in the workspace directory to confirm that the changes actually work.

## Verification in the Phase Model

Verification happens in the **verify** phase (manifest: `umbrella/phases/manifests/verify.yaml`):

- Worker runs `run_workspace_verify` and/or `run_real_e2e`.
- Worker calls `submit_verification(pass|fail, details)`.
- On pass: `promote_to_durable` transfers verified nodes from `palace.run` to `palace.lesson`/`palace.idea`/`palace.codeptr`.
- On fail: Reflexion mini-phase triggers, generating evidence-backed feedback.

## Verification Sources

Steps are declared in `workspace.toml` (section `[verification]`) or auto-detected:

| Source | Detection | Command |
|--------|-----------|---------|
| pytest | `test_smoke.py` exists | `pytest test_smoke.py` |
| HTTP health | `web_server.py` exists | Start server, GET `/health` |
| Import check | `main.py` exists | `python -c "import main"` |
| Custom steps | `workspace.toml [verification]` | User-defined commands |

## Verify Phase Manifest (key fields)

```yaml
id: verify
allowed_tools:
  - run_workspace_verify
  - run_real_e2e
  - palace_search
  - submit_verification
  - promote_to_durable
  - request_human_checkpoint
  - loop_back_to
memory:
  always_on:
    - {store: palace.charter}
  hot:
    - {store: palace.run, tags: [all]}
exit_criteria:
  required_calls: [submit_verification]
```

## Retry Loop

The `umbrella/orchestrator/verify_loop.py` manages retries:

1. Run verify phase.
2. If `submit_verification(fail)`:
   - Reflexion phase generates feedback.
   - `loop_back_to(execute)` or `loop_back_to(plan)` with remediation.
3. Repeat up to `max_verify_retries` (configurable via CLI `--max-verify-retries` or env `OUROBOROS_WEB_MAX_VERIFY_RETRIES`).
4. If all retries exhausted: `status=failed_verification`.

## Verification and Promotion

Verified knowledge promotion (`umbrella/orchestrator/promotion.py`):

On `submit_verification(pass)`:
1. Worker calls `promote_to_durable(node_id, target_store)` for key findings.
2. Nodes are copied from `palace.run` to `palace.lesson`/`palace.idea`/`palace.codeptr` with `verified=true`.
3. Reflexions that were in hot-context get promoted: requires `applied_reflection` edge + verify pass.

On `submit_verification(fail)`:
1. A bug-pattern lesson is written to `palace.lesson` with edge `triggered_by_error`.
2. Reflexion mini-phase generates verbal feedback with evidence citations.

## Verification in Web Bridge

The web bridge runs verification as part of the phase lifecycle. The `--max-verify-retries` flag (default 20) controls the retry budget. The `--no-verify` flag disables verification entirely (not recommended for production use).

## Reflexion Promotion Gate

The key safety mechanism for reflexion:

1. Reflexion generates a `Reflection` node in `palace.run` with `verified=false`.
2. On next attempt, the reflection is injected into hot-context.
3. If the Worker applies the reflection (creates `applied_reflection` edge) and the attempt passes verify:
   - `palace.promote(reflection_id, target=palace.lesson, verified=true)`.
4. Without verified evidence, the reflection stays in `palace.idea` with `verified=false` (suppressed in default recall) or dies with the run.

This prevents hallucinated "lessons" from polluting the durable knowledge store.

---

Next: [Part 10 — Web Bridge and UI](10-web-bridge.md)
