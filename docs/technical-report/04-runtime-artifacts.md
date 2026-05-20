# Part 4: Runtime Artifacts

This chapter describes what appears on disk during execution. Runtime data lives under `.umbrella/` by default.

## `.umbrella/` Layout

```
.umbrella/
  ouroboros_drive/                    # Ouroboros filesystem workspace
    logs/                             # Event and tool logs
      events.jsonl                    # Structured event log
      tools.jsonl                     # Tool call log
      round_io/                       # Per-round I/O snapshots
    memory/
      knowledge/                      # Lessons synced from Umbrella memory
    state/
      state.json                      # Current run state (budget, drift)
      phase_plan.json                 # Active PhasePlan
      watcher_signal.json             # Watcher -> Runner control signal (atomic)
      watcher_signals.processed.jsonl # Audit trail of processed signals
    task_results/                     # Completed task outputs
    tmp_tools/                        # Agent-authored temporary tools
      <phase>/<name>.py               # Temporary tool source
      <phase>/<name>_spec.json        # Temporary tool schema

  memory/                             # Legacy memory files (pre-MemPalace)
    lessons.jsonl                     # -> migrated to palace.lesson
    ideas.jsonl                       # -> migrated to palace.idea
    gaps.jsonl                        # -> migrated to palace.idea (tag=gap)
    signals.jsonl                     # -> migrated to palace.idea (tag=signal)
    *.migrated                        # Renamed after successful migration

  palace/                             # MemPalace data stores
    charter/                          # Chroma: project goal, architecture
    lesson/                           # Chroma: verified lessons
    idea/                             # Chroma: hypotheses, findings
    codeptr/                          # Chroma: external code pointers
    skill_index/                      # Chroma: skill library mirror
    run/                              # Chroma: current run state
    phase/                            # Chroma: phase scratchpads
    subtask/                          # Chroma: subtask scratchpads
    transient.sqlite                  # SQLite: events, tool I/O (TTL 24h)
    graph.sqlite                      # SQLite: edge table

  sandbox_sessions/                   # Sandbox self-edit session records
    <id>.json                         # Session metadata, rollback status

  meta_harness/                       # Meta-Harness experiment data
    experiments/
      <experiment_id>/
        manifest.json                 # Experiment configuration
        snapshots/                    # Repository state snapshots
        execution/                    # Execution artifacts
        evaluation/                   # Evaluation results
```

## Workspace Runtime Data

Within a workspace instance (`workspaces/<seed>/instances/<instance>/`):

```
runs/                                 # Run output directories
  <run_id>/
    events.jsonl                      # Run events
    tools.jsonl                       # Tool calls
    result_summary.json               # Structured result
    verification_report.json          # Verification output
snapshots/                            # State snapshots
reports/                              # Generated reports
memory/                               # Per-workspace memory (pre-MemPalace)
logs/                                 # Detailed logs
```

## Key Files

| File | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `drive/state/phase_plan.json` | PhaseRunner, phase_control.py | PhaseRunner, Worker | Current PhasePlan state |
| `drive/state/watcher_signal.json` | Watcher | PhaseRunner | Control signals (atomic rename) |
| `drive/state/state.json` | Ouroboros state manager | Ouroboros loop | Budget, drift, round count |
| `palace/transient.sqlite` | MemPalace facade | Watcher, search | Events, tool I/O, terminal scrollback |
| `palace/graph.sqlite` | MemPalace facade | recall, walk | Edge table linking memory nodes |
| `runs/<id>/verification_report.json` | Verification runner | PhaseRunner, Web UI | Verification pass/fail details |
| `runs/<id>/final_report.json` | FinalReport builder | Web UI, CLI | Evidence-based run report |

## Atomic Writes

Critical files use atomic write patterns:

- `watcher_signal.json`: write to `.tmp` + `os.replace(.tmp, target)`.
- `state.json`: similar atomic rename.
- Lock files: `watcher_signal.lock` (advisory, via `filelock` or `msvcrt.locking` on Windows).

---

Next: [Part 5 — Layers and Data Flows](05-architecture-flows.md)
