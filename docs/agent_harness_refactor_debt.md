# Agent Harness Refactor Debt

This file tracks cleanup work deliberately left out of the deadline fix.

## Refactor Completed In This Pass

- Umbrella/Ouroboros boundary split:
  - Umbrella now owns remediation plan synthesis in `umbrella/control_plane/remediation_planner.py`.
  - Umbrella now supplies prompt-governance context overlays from `umbrella/orchestration/context_overlays.py`; Ouroboros only consumes `task["context_overlays"]`.
  - External discovery memory mirroring moved to `umbrella/memory/external_findings.py`, with GitHub/MCP discovery callsites importing from Umbrella.
  - Umbrella exposes injectable agent memory hooks through `umbrella/memory/agent_hooks.py`; Ouroboros accepts `make_agent(memory_hooks=...)`.
  - MCP server tool materialization moved to `umbrella/mcp/tool_entries.py`; `ouroboros/tools/mcp_servers.py` and `mcp_discovery.py` are thin shims.
- Cleanup and size reduction:
  - Removed stale `source_policy.py.backup` and `.orig`.
  - Split oversized `build_prior_knowledge_section`, `get_umbrella_memory`, `_execute_single_tool`, `run_ouroboros_improvement_sync`, `_run_llm_phase`, and `_drive_subtask_loop` into smaller helpers.
  - Added explicit Umbrella phase-boundary events for `initial_started`, `verification_started`, `verification_completed`, existing `self_review_started`, and `remediation_started`.
- Real workspace smoke:
  - `workspaces/news_cards_ai` now has a clean `src/`, `docs/`, and `workspace.toml` layout.
  - Real `run_workspace_verify(workspace_id="news_cards_ai")` passed 6/6 required checks, including `source_policy:mock_scaffold_scan`, GMAS checks, and compile check.
  - Pollution check stayed clean for prior throwaway artifacts (`check_format.py`, `extract_requirements.py`, `requirements_raw.txt`, `test_agents.py`, `test_basic.pptx`, probe/read/extract scripts).

## Validation Snapshot

- Focused Phase 1 pack: `187 passed`.
- Focused Phase 2 pack: `156 passed`.
- Hook/loop regression pack: `77 passed`.
- MCP boundary pack: `15 passed`.
- Real `news_cards_ai` verification: `passed=true`, `6/6` required checks.
- Broad `python -m pytest umbrella/tests ouroboros/tests -q`: `1341 passed`, `7 skipped`, `26 failed`.
  - Most broad failures remain in known workspace registry/runtime/e2e baseline areas.
  - Also observed known `test_loop_auto_compact.py::test_sets_moderate_pending_at_86_percent`.
  - `test_record_idea_verified_outcome_mirrors_to_semantic` failed in the full run but passed in isolation, so it is order-dependent and should be stabilized separately.
  - `test_workspace_usage_recursion.py::TestIndexerSurvivesGiantModule::test_deeply_nested_expression_does_not_crash` still hits recursion in the workspace import visitor.

## Diff Snapshot

- Tracked diff stat at report time: 23 files, 4202 insertions, 1620 deletions.
- New Umbrella boundary modules added:
  - `umbrella/control_plane/remediation_planner.py`
  - `umbrella/orchestration/context_overlays.py`
  - `umbrella/memory/external_findings.py`
  - `umbrella/memory/agent_hooks.py`
  - `umbrella/mcp/tool_entries.py`
- New `news_cards_ai` workspace seed artifacts added:
  - `docs/design.md`
  - `docs/requirements.md`
  - `src/news_cards_ai/*`
  - `workspace.toml`

## High Priority

- Decompose `_LoopState` into smaller state objects:
  - round/model budget state
  - workspace/verification state
  - planner/subtask state
  - guard/preflight state
- Finish physical relocation of memory hook implementation:
  - `umbrella/memory/agent_hooks.py` is now the Umbrella API and injectable protocol.
  - `ouroboros/memory_hooks.py` should become a pure compatibility shim once the remaining tests are adjusted away from private helper imports.
- Continue splitting `ouroboros/ouroboros/loop.py` into phase driver modules:
  - planner/rescue driver
  - subtask/review driver
  - final aggregation driver
  - generic LLM/tool execution driver
- Extract completion-tool acceptance into a small state machine instead of keeping it inline in `_run_llm_phase`.
- Move planner discovery policy into one shared module. It is currently duplicated conceptually between `loop.py`, `completion_gates.py`, and planner prompts.
- Split `ouroboros/ouroboros/tools/umbrella_tools.py` by concern:
  - workspace file IO
  - memory tools
  - verification tools
  - web/search tools
  - background/runtime helpers
- Make verification result payloads versioned (`schema_version`) so loop-state parsing does not depend on ad-hoc JSON fields.
- Stabilize broad-suite baseline failures before treating full `umbrella/tests ouroboros/tests` as a mandatory green gate.

## Medium Priority

- Replace root-file layout regex duplication between `update_workspace_seed` and `final_sweep` with one shared policy module.
- Turn memory trust filtering into an explicit `MemoryTrustLevel` model instead of tag/room heuristics.
- Add a structured `completion_tool_status` field to control tools so the loop does not infer success from `OK:` text.
- Add a focused trace reader for active runs; current analysis still requires ad-hoc scripts over logs and JSONL.
- Add planner tests for rescue behavior when discovery is required but the first propose attempt is rejected.
- Complete `ouroboros/tools/control.py` extraction so it only contains schemas and direct forwarding to `umbrella.control_plane` APIs.
- Remove or replace deprecated `umbrella/app.py` paths once callers are verified.
- Fix workspace usage indexing recursion for giant nested expressions.
- Make `record_idea` semantic mirroring tests deterministic in full-suite order.

## Nice To Have

- Add a small harness dashboard view for current phase, plan cursor, latest verify run, discovery calls, and rejected completion attempts.
- Add a migration path for old unverified memory entries so they do not keep appearing as trusted context.
- Give verification `next_actions` richer typed categories (`layout`, `tests`, `missing_file`, `command_failure`) for easier prompt injection.
