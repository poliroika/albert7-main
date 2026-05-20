# Part 5: Layers and Data Flows

This chapter traces the data flow from operator input through the phase machine to the final report.

## Phase Execution Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI as CLI / Web Bridge
    participant Runner as PhaseRunner
    participant Palace as MemPalace
    participant Worker as Worker Ouroboros
    participant Watcher as Watcher Ouroboros
    participant Registry as ToolRegistry

    User->>CLI: Task + workspace
    CLI->>Runner: run_phases(workspace, task)
    Runner->>Palace: recall("preflight") -> RecallBundle
    Runner->>Worker: spawn(manifest=preflight, recall, tool_filter)
    Worker->>Registry: execute_tool(env_check)
    Registry->>Registry: PermissionEnvelope.check(preflight, tool)
    Registry-->>Worker: allowed -> execute
    Worker-->>Runner: submit_preflight_report(ready)

    loop For each phase in PhasePlan
        Runner->>Palace: recall(phase_id) -> RecallBundle
        Runner->>Worker: spawn(manifest, recall, tool_filter, budgets)
        Runner->>Watcher: start_monitoring()

        loop Worker rounds
            Worker->>Registry: execute_tool(name, args)
            Registry->>Registry: PermissionEnvelope.check(phase, tool, paths)
            alt allowed
                Registry-->>Worker: tool result
            else denied
                Registry-->>Worker: TOOL_DENIED_BY_ENVELOPE
            end
            Worker->>Palace: add(store, content, tier, scope)

            Watcher->>Watcher: check_triggers()
            alt trigger fired
                Watcher->>Runner: watcher_signal.json
                Runner->>Runner: process_signal()
            end
        end

        Worker-->>Runner: phase result
        Runner->>Palace: expire_scope(phase_scoped)
    end

    Runner->>Runner: build_final_report()
    Runner-->>CLI: FinalReport JSON
    CLI-->>User: Result
```

## Memory Flow

```mermaid
flowchart TB
    subgraph Recall
        AlwaysOn["always_on tier<br/>palace.charter"] --> Hot["hot tier<br/>palace.run (PhasePlan, findings)"]
        Hot --> Warm["warm vector search<br/>palace.lesson, idea, codeptr"]
        Warm --> GraphWalk["1-hop graph walk<br/>derived_from, cites, tests"]
    end

    subgraph Write
        WorkerW["Worker writes"] --> PalaceAdd["palace.add(store, content, tier, scope)"]
        WatcherW["Watcher writes"] --> PalaceAdd
    end

    subgraph Lifecycle
        PhaseExit["Phase exit"] --> ExpirePhase["expire_scope(phase_scoped)"]
        SubtaskDone["Subtask complete"] --> ExpireSubtask["expire_scope(subtask_scoped)"]
        RunDone["Run complete"] --> Promote["promote verified nodes"]
        Promote --> Durable["palace.lesson, palace.idea(verified)"]
        Promote --> Archive["Archive rest"]
    end

    Recall --> WorkerContext["Worker recall_bundle"]
    Write --> Stores["Chroma + SQLite stores"]
    Stores --> Recall
```

## Permission Enforcement Flow

```mermaid
flowchart LR
    LLM["LLM response"] --> ToolCall["Tool call request"]
    ToolCall --> PreHook["ToolRegistry.execute pre-hook"]
    PreHook --> Envelope["PermissionEnvelope.check(phase, tool, paths, commands)"]
    Envelope --> GlobalDeny{"Global deny matches?"}
    GlobalDeny -->|yes| Denied["TOOL_DENIED_BY_ENVELOPE"]
    GlobalDeny -->|no| PhaseRules{"Phase rule matches?"}
    PhaseRules -->|allow| Execute["Execute tool"]
    PhaseRules -->|deny| Denied
    PhaseRules -->|no match| DefaultDeny["Default: deny"]
    Denied --> LLM
    Execute --> LLM
```

## FinalReport Flow

```mermaid
flowchart TB
    VerifyPass["verify(pass)"] --> Collect["Collect evidence"]
    Collect --> Files["changed_files (git diff)"]
    Collect --> Commands["commands_run (transient store)"]
    Collect --> VReports["verification_reports"]
    Collect --> Incidents["watcher_incidents (palace.run)"]
    Collect --> Promotions["memory_promotions"]

    Collect --> BuildLLM["LLM: human_summary_md<br/>with [ev:] / [art:] citations"]
    BuildLLM --> Validate["Validate: every claim cites evidence"]
    Validate -->|ok| Report["FinalReport (status=pass)"]
    Validate -->|fail| Retry["Retry once"]
    Retry -->|fail again| Partial["FinalReport (status=partial)"]
```

## Watcher Signal Protocol

1. Watcher detects trigger condition.
2. Watcher writes `WatcherSignal` JSON to `drive/state/watcher_signal.json` (atomic rename).
3. Runner reads signal on next Worker round boundary or phase boundary.
4. Runner deduplicates by `signal_id` against `watcher_signals.processed.jsonl`.
5. Runner applies signal: abort, restart, mutate plan, force verify, inject lesson.
6. Signal is logged to `palace.run` with edge `flagged_by=watcher`.

---

Next: [Part 6 — Umbrella Subsystems](06-umbrella-subsystems.md)
