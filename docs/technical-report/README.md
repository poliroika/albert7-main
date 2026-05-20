# Technical Report

Multi-page description of the repository: module boundaries, data flows, configuration, web bridge, and operations. Written for an engineer who will edit code and maintain runs.

---

## Table of Contents

| Part | File | Contents |
|------|------|----------|
| 0 | [This file](README.md) | Introduction, navigation |
| 1 | [Goals, Audience, Terms](01-scope-terms.md) | Why this report, glossary, out-of-scope |
| 2 | [System Context](02-system-context.md) | Workspace / GMAS / Umbrella / Ouroboros roles in one narrative |
| 3 | [Repository Topology](03-repository-topology.md) | Directories, Python dependencies, entry points |
| 4 | [Runtime Artifacts](04-runtime-artifacts.md) | What appears on disk during execution |
| 5 | [Layers and Data Flows](05-architecture-flows.md) | PhaseRunner -> Worker -> Watcher -> MemPalace -> FinalReport |
| 6 | [Umbrella Subsystems](06-umbrella-subsystems.md) | Phases, orchestrator, MemPalace, permissions, retrieval |
| 7 | [Workspaces and Policy](07-workspaces-and-policy.md) | Seed, instance, file contracts, PermissionEnvelope path rules |
| 8 | [Ouroboros Runtime](08-ouroboros-runtime.md) | LLM loop, phase manifest consumption, tool registry, supervisor |
| 9 | [Verification](09-verification.md) | Spec, runner, retry, promotion gate, reflexion |
| 10 | [Web Bridge and UI](10-web-bridge.md) | HTTP server, API routes, React frontend |
| 11 | [Configuration](11-configuration.md) | `.env`, CLI flags, environment variables |
| 12 | [Harness vs meta-harness](12-meta-harness.md) | `harness_run` tool vs `.umbrella/meta_harness/` artifacts; promotion in `umbrella/evals/` |
| 13 | [Operations](13-operations.md) | Typical scenarios, logs, common failures |
| 14 | [Testing and Docs](14-testing-and-docs.md) | Pytest suites, test descriptions, documentation maintenance |
| 15 | [Planning layers](15-task-planner.md) | PhasePlan (Umbrella) vs adaptive task planner (Ouroboros), completion gates |

---

## Cross-references

- [Architecture overview](../architecture.md)
- [Workspaces](../workspaces.md)
- [Ouroboros](../ouroboros.md)
- [Umbrella layer](../umbrella-layer.md)
- [GMAS](../gmas.md)

If text diverges from code, code and tests take priority; update the corresponding chapter.
