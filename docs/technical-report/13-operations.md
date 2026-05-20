# Part 13: Operations

Typical operational scenarios, log locations, and common failure modes.

## Typical Scenarios

### Running a single workspace task

```powershell
# Build frontend (first time or after changes)
cd web && yarn install && yarn build && cd ..

# Run
uv run python umbrella/app_ouroboros.py workspaces/<workspace_id> --live --verbose --max-verify-retries 3
```

The PhaseRunner will:
1. Run preflight (env check).
2. Research the task.
3. Plan subtasks.
4. Execute each subtask.
5. Verify results.
6. Build FinalReport.

### Running via Web UI

```powershell
# Terminal: start bridge
uv run bridge

# Browser: http://127.0.0.1:8765
# Navigate to Chat, select workspace, start run
```

The UI shows:
- PhasePlan timeline (current phase, completed phases).
- Worker messages and tool calls.
- Watcher incidents.
- Agent requests (user input, permission escalation).
- Final report with evidence links.

### Debugging a failed run

1. Check `FinalReport` at `/api/runs/<id>/report`.
2. Look for `unresolved_risks` and `watcher_incidents`.
3. Check `phase_timeline` for which phase failed.
4. Read `verification_reports` for test failure details.
5. Search `palace.transient` for terminal output and error events.

### System self-improvement (separate mode)

Normal workspace runs **must not** edit `umbrella/`, `ouroboros/`, or `gmas/` — those paths stay under strict `PermissionEnvelope` rules.

System self-improvement is wired through `umbrella/orchestrator/self_improvement_runner.py::run_self_improvement`, which loads the relaxed rules from `umbrella/permissions/self_improvement.yaml` (override path with `UMBRELLA_SELF_IMPROVEMENT_ENVELOPE`). Integrate it from your own operator script or internal tooling; there is **no** root-level `run_ouroboros_self_improve.py` wrapper in this repository.

## Log Locations

| Log | Location | Format |
|-----|----------|--------|
| Events | `.umbrella/ouroboros_drive/logs/events.jsonl` | JSONL |
| Tool calls | `.umbrella/ouroboros_drive/logs/tools.jsonl` | JSONL |
| Round I/O | `.umbrella/ouroboros_drive/logs/round_io/` | Per-round JSON |
| State | `.umbrella/ouroboros_drive/state/state.json` | JSON |
| PhasePlan | `.umbrella/ouroboros_drive/state/phase_plan.json` | JSON |
| Watcher signals | `.umbrella/ouroboros_drive/state/watcher_signal.json` | JSON |
| Processed signals | `.umbrella/ouroboros_drive/state/watcher_signals.processed.jsonl` | JSONL |
| Verification | `workspaces/<ws>/instances/<inst>/runs/<run>/verification_report.json` | JSON |
| Memory (legacy) | `.umbrella/memory/*.jsonl` | JSONL |
| Memory (MemPalace) | `.umbrella/palace/` | Chroma + SQLite |

## Common Failure Modes

### `TOOL_DENIED_BY_ENVELOPE`

**Cause**: Worker tried to call a tool not in the phase's `allowed_tools` or blocked by `global.yaml`.

**Fix**: Check the phase manifest. If the tool is legitimately needed, update the manifest's `allowed_tools` list.

### Verification failure loop

**Cause**: Worker makes changes that don't pass `pytest` or other verification steps.

**Fix**: Check `verification_report.json` for specific failures. The Reflexion mechanism should generate feedback for the next attempt. Increase `--max-verify-retries` if needed.

### Watcher abort

**Cause**: Watcher detected stall, repeated error, or budget overrun.

**Fix**: Check `watcher_signal.json` for the trigger type. Investigate why the Worker was stuck (terminal output, repeated tool calls).

### Budget exceeded

**Cause**: Phase or run exceeded token/second/tool-call budget.

**Fix**: Increase `budgets.max_tokens` or `budgets.max_seconds` in the phase manifest. Check for infinite loops in Worker tool calls.

### Palace unavailable

**Cause**: MemPalace Chroma backend not initialized or SQLite locked.

**Fix**: Check `.umbrella/palace/` directory permissions. Run `palace.health()` to diagnose.

### Stale sandbox stashes

**Cause**: Sandbox self-edit left orphaned git stashes.

**Fix**: Run `git stash list` and manually apply or drop stashes with `git stash apply stash@{N}` / `git stash drop stash@{N}`.

## Health Checks

```powershell
# Bridge health
curl http://127.0.0.1:8765/api/health

# MemPalace health
uv run python -c "
from umbrella.memory.palace.facade import MemPalace
p = MemPalace(repo_root='.', workspace_id='test')
print(p.health())
"
```

---

Next: [Part 14 — Testing and Docs](14-testing-and-docs.md)
