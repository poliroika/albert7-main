# Civilization Calibration Bug Log

Purpose: keep a short audit trail for the `workspaces/civilization` calibration loop so each new bug can be checked against prior fixes before changing Umbrella/Ouroboros again.

Rule for future fixes: before patching product code, prompts, tools, schemas, validators, tests, or orchestration for this calibration, read this file and confirm whether the new failure is a regression of a prior entry or a distinct bug.

## 2026-05-20 - Explicit Research Observations Were Promoted Into Findings

- Run: `phase_web_694128fb`.
- Symptom: after empty GitHub searches were correctly rejected as research-finding sources, the agent tried to save "GitHub returned 0 results" with `kind="observation"` and `evidence_kind="observation_from_log"`. `palace_add` still treated the call as a counted `research_finding`, demanded source provenance, and trapped research in repeated rejected `palace_add` attempts.
- Risk: the memory layer ignores the caller's explicit trust tier. Unverified notes, empty-result observations, and progress bookkeeping can either be blocked as if they were findings or, in other shapes, promoted into hot verified research memory.
- Cause: research-phase compatibility inference treated both omitted `kind` and explicit `kind="observation"` as candidates for auto-promotion to `research_finding`.
- Fix: `palace_add` now distinguishes omitted kind from explicit observation. Omitted concrete research notes may still be compatibility-promoted, but explicit `kind="observation"` remains an untrusted observation and does not need counted-finding provenance.
- Regression: `test_palace_add_keeps_explicit_research_observation_as_untrusted_note`.

## 2026-05-20 - Empty GitHub Search Accepted As Concrete Research Finding

- Run: `phase_web_01a37983`.
- Symptom: `github_project_search` returned `status=ok` with `results=[]`, but research later saved a verified `palace_add(kind=research_finding)` claiming "GitHub discovery ... yielded several relevant implementations" and naming repositories such as `civil-ai/civilization-game` and `Strategy-LLM`, citing `github_project_search:<query>` as provenance. `research_review` accepted the summary and advanced to plan.
- Risk: a finding can have a syntactically current `source_id` while its claim is unsupported by the source payload. This lets fabricated project evidence enter hot verified memory and plan context.
- Cause: `palace_add` provenance validation checked that a matching discovery tool call existed and had `status=ok`, but for `github_project_search` it did not require the source row to contain any returned repositories.
- Fix: research source validation now treats `github_project_search` as a result-bearing source. Empty result sets are not usable evidence for counted `research_finding` memory; they may still be saved as observations/leads.
- Regression: `test_palace_add_rejects_tool_qualified_github_source_with_empty_results`.

## 2026-05-20 - Unsupported Model Alias Leaked Into Generated Workspace Contract

- Run: `phase_web_92538072`.
- Symptom: execute generated `tests/test_config.py` with a test named around `OUROBOROS_LLM_MODEL`, even though the user-facing workspace project is a standalone app and should expose generic `LLM_*` runtime settings rather than teaching deep-agent/control-plane typo details.
- Risk: control-plane alias trivia becomes product API, docs, and tests inside generated workspaces. That creates brittle tests, confuses users, and couples standalone projects to Umbrella/Ouroboros internals.
- Cause: plan/execute/policy prompts, `env_check` advisories, and generic `llm_runtime_contract` feedback repeatedly put Umbrella compatibility aliases ahead of the standalone project contract or said "the model alias is `OUROBOROS_MODEL`, not `OUROBOROS_LLM_MODEL`" even when the rejected payload did not contain the bad alias. Plan validators also allowed protective mentions of the unsupported alias, so the model preserved the anti-pattern as product logic.
- Fix: agent-facing prompts and `env_check` now present `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` as the standalone public contract, with `OUROBOROS_*` only as optional inherited compatibility aliases. Structured `env_check.accepted_*` lists are public-first. Generic guard feedback no longer names `OUROBOROS_LLM_MODEL`; validators reject any appearance of that unsupported alias in plan/workspace artifacts, including protective wording, and keep the bad alias visible only when the payload actually contains it.
- Regression: `test_env_check_accepts_ouroboros_llm_key_alias`, `test_agent_facing_runtime_prompts_do_not_teach_unsupported_model_alias`, `test_apply_workspace_patch_blocks_protective_unsupported_model_alias_docs`, `test_propose_phase_plan_accepts_public_llm_alias_contract_without_ouroboros_aliases`, `test_propose_phase_plan_rejects_protective_unsupported_model_alias_note`, `test_latest_phase_plan_execution_floor_accepts_public_llm_alias_contract`, and `test_latest_phase_plan_execution_floor_rejects_protective_model_alias_note`.

## 2026-05-20 - Watcher Did Not Structure Bad Generated Success-Test Contract

- Run: `phase_web_92538072`.
- Symptom: `backend-setup` failed `python -m pytest tests/test_config.py -q` because a generated alias-priority test set only model env vars while calling strict `get_llm_config()`, which requires API key/base URL. `request_watcher_review` recorded the failing output but stored the proposed test edit as plain operator text; `mutate_phase_plan` then rejected the active success-test migration as unproven, while direct test edits were correctly blocked.
- Risk: execute gets trapped between "repair implementation" and "test contract is bad" policies. Watcher memory can also replay a rejected test-edit recipe as hot guidance.
- Cause: retry watcher payloads had only generic `review_recorded` structure and free-form recommendation text. The mutation gate could only infer proof from broad text fragments, not a typed watcher verdict tied to the success-test target file.
- Fix: retry watcher now emits a structured `contract_migration.verdict=bad_generated_success_test_contract` with target files and evidence when the latest failure plus operator reason indicate a generated test-contract contradiction. `mutate_phase_plan` accepts active success-test migration when that typed watcher evidence supports the targeted file. The palace mirror redacts raw test-edit recipes for structured contract-migration reviews.
- Regression: `test_request_watcher_review_classifies_bad_generated_success_test_contract` and `test_mutate_phase_plan_accepts_watcher_proven_bad_generated_success_test_contract`.

## 2026-05-20 - Add File Accepted Literal Patch Hunk Marker

- Run: `phase_web_92538072`.
- Symptom: `apply_workspace_patch` accepted `*** Add File` content that began with literal `@@`, so generated Markdown files such as `docs/architecture.md` and `docs/game_mechanics.md` started with a patch hunk marker.
- Risk: malformed patch syntax can leak into generated text/source files, later tests may normalize around bad artifacts, and agent feedback hides the real patch-format mistake.
- Cause: Add File validation did not reject literal hunk marker lines. `@@` is valid as a control marker for `Update File` hunks but not as new-file content.
- Fix: Add File and paired Delete/Add replacement planning now block literal `@@` content lines with an actionable `patch_add_file_literal_hunk_marker` payload.
- Regression: `test_apply_workspace_patch_rejects_add_file_literal_hunk_marker`.

## 2026-05-20 - Inline Docs Content `python -c` Success Test Accepted

- Run: `phase_web_c7817420`.
- Symptom: after several healthy plan rejections, `propose_phase_plan` accepted and `plan_review` approved `docs-contract.success_test = python -c "... open('README.md').read() ... open('docs/architecture.md').read() ..."`. The plan then entered execute with an inline generated-docs content verifier instead of a checked-in pytest or verification script.
- Risk: documentation leaves can satisfy Umbrella with ad hoc inline content probes that are hard to review, mutate, or connect to EvidenceGraph proof targets. This is the same proof-contract family as file-existence-only checks, but the captured plan bypassed it by checking substrings.
- Cause: success-test policy rejected file/path existence expressions and complex/import-only `python -c`, but did not classify inline `open(...).read()` / `read_text()` checks against generated README/docs content as non-durable proof.
- Fix: shared success-test validation now rejects inline `python -c` generated documentation/content checks and tells the agent to put docs/content assertions in a checked-in pytest or verification script. Runner execution-floor validation uses the same helper instead of a separate copy.
- Regression: `test_propose_phase_plan_rejects_inline_docs_content_python_success_test` and `test_latest_phase_plan_execution_floor_rejects_inline_docs_content_python_success_test`.

## 2026-05-20 - Observation Memory Surfaced As Trusted Verified Palace Memory

- Run: `phase_web_c7817420`.
- Symptom: research saved a self-authored synthesis/progress-style `palace_add(kind=observation, evidence_kind=verified_outcome)` row with `verified=true` and `source_path=tool:palace_add`; later `palace_search(include_unverified=false)` returned its legacy `drawer_*` mirror under trusted `palace_memory` during plan.
- Risk: non-finding observations can influence later phases as trusted hot memory even when they are not accepted research findings and have no external provenance edge. This weakens the memory hierarchy and reopens the stale/unverified memory class under a different shape.
- Cause: `palace_add` treated `evidence_kind=verified_outcome` as enough to mark ordinary observations verified, and the read-side filter did not demote legacy mirrors where `kind=observation`, `evidence_kind=verified_outcome`, and `source_path=tool:palace_add`.
- Fix: ordinary observations no longer become verified solely from `evidence_kind=verified_outcome`; research findings that pass provenance validation still become verified, while a small allowlist covers true completion/verification outcome memory. `palace_search(include_unverified=false)` also demotes existing legacy mirrors with the captured self-verified observation shape.
- Regression: `test_palace_add_observation_verified_outcome_stays_untrusted` and `test_palace_add_research_finding_with_current_source_remains_verified`.

## 2026-05-19 - Patch Mismatch Feedback Hid JSON-Escaped Line Endings

- Run: `phase_web_7cc8dbd2`
- Symptom: execute correctly identified an internally inconsistent generated success test and accepted a `mutate_phase_plan` contract migration, but then got stuck on `tests/test_game_state.py`: repeated `apply_workspace_patch` calls returned `patch_hunk_mismatch`, one retry copied literal `\r` markers from a JSON `read_file` payload into the hunk, and `request_watcher_review` replied with generic retry advice.
- Cause: after accepted active success-test migration, the patch guard allowed exact updates but did not explain that JSON-rendered `\r`/`\n` text must be converted back to real patch line breaks. `read_file(line_start=...)` also marked complete requested line slices as `truncated=true` just because more file lines existed below, reinforcing the false belief that exact context was unavailable.
- Fix: `read_file` now distinguishes cap truncation from normal line slices (`line_range_complete`, `has_more_lines_after`), `patch_hunk_mismatch` payloads detect literal escaped line endings and include a targeted `read_file` line-slice hint plus current context, and watcher recommendations inspect recent patch-mismatch tool rows before returning generic retry guidance.
- Regression: `test_apply_workspace_patch_contract_migration_mismatch_explains_json_escapes`, `test_request_watcher_review_reports_patch_escape_guidance_before_threshold`, and the updated `test_read_workspace_file_supports_line_start`.

## 2026-05-19 - Split-Brain Palace Memory Facades

- Run: audit during `phase_web_7cc8dbd2`
- Symptom: phase prompts recall from the newer `MemPalace`, while `palace_search`/`get_umbrella_memory`, some prior-knowledge paths, and legacy mirrors still read `PalaceBackend`; watcher prompt claims palace access without receiving a recall snapshot.
- Cause: Umbrella added a newer hierarchical memory facade without fully retiring or wrapping the legacy facade, so write/read contracts and UI memory graphs can disagree about canonical ids, scope, and freshness.
- Fix: pending. Next conceptual memory pass should make one Umbrella-level canonical read/write facade for `palace_add`, `palace_search`, phase recall, watcher snapshots, cleanup, and UI graph rendering; mirrors should report `canonical_id`, `saved_new`, `saved_legacy`, and errors instead of silently accepting partial writes.
- Regression: pending with captured memory/tool payloads before product changes.

## 2026-05-19 - Research Summary Accepted Interrupted Coverage Handoff

- Run: `phase_web_368db408`
- Symptom: `submit_research_summary` accepted notes beginning `PHASE INTERRUPTED - INCOMPLETE COVERAGE` and saying discovery requirements were not complete, as long as three `palace_add` ids were present.
- Cause: the placeholder/pending research-summary detector covered `Research in progress`, `currently N findings`, and similar progress wording, but missed interrupted/incomplete-coverage language from a real LLM handoff.
- Fix: expanded `_RESEARCH_SUMMARY_PLACEHOLDER_RE` to reject `phase interrupted`, `incomplete coverage`, and explicit missing required discovery wording before writing `research_summary_latest.json`.
- Regression: `test_submit_research_summary_rejects_captured_interrupted_coverage_notes`.

## 2026-05-19 - Research Scratchpad Counted As Accepted Finding

- Run: `phase_web_368db408`
- Symptom: research saved only two real `kind=research_finding` entries, plus a `kind=scratchpad` progress note saying `Research progress: 1/3 palace findings saved`; `submit_research_summary` cited the scratchpad id as the third finding and advanced.
- Cause: `_accepted_palace_add_aliases_for_task` treated every saved `palace_add` row as a valid research finding, regardless of `kind`, tags, or progress/placeholder content.
- Fix: research-summary id normalization now excludes explicit non-finding palace rows (`scratchpad`, `progress`, notes/status) and rows whose title/content matches pending/progress handoff language. Unknown-id feedback now clarifies that ids must come from concrete `research_finding` entries.
- Regression: `test_submit_research_summary_does_not_count_scratchpad_as_finding`.

## 2026-05-19 - GMAS Context Tool Rejected Common `limit` Alias

- Run: `phase_web_368db408`
- Symptom: research called `get_gmas_context(..., limit=...)` and received `TOOL_ARG_ERROR: unexpected keyword argument 'limit'`, wasting a discovery turn even though `limit` is a common synonym for `max_results`.
- Cause: the GMAS context handlers and schema accepted only `max_results`; the tool layer did not tolerate this benign alias.
- Fix: `get_gmas_context` and `search_gmas_knowledge` now accept `limit` as a backward-compatible alias for `max_results`, and the exposed tool schema documents the alias while still preferring `max_results`.
- Regression: `test_get_gmas_context_accepts_limit_alias` and `test_gmas_context_tool_schema_accepts_limit_alias`.

## 2026-05-19 - Contract Migration Lost From Subtask Card

- Run: `phase_web_eb6b24c7`
- Symptom: execute found a generated success test typo (`DiplomaticStatus.CHELLY`) and correctly called `mutate_phase_plan`, but `phase_plan.json` later showed `contract_migration_reason` as missing on the subtask card.
- Cause: plan mutation wrote the raw edit into `edits_log`, but `SubtaskCard` serialization/load did not preserve contract-migration fields.
- Fix: added `contract_migration_reason` and `contract_migration_files` to `SubtaskCard`; taught plan loading and runner plan projection to preserve them.
- Regression: `test_mutate_phase_plan_records_contract_migration_reason` now round-trips through `load_plan`/`save_plan`.

## 2026-05-19 - Contract Migration Trapped By Replacement-Required Guard

- Run: `phase_web_eb6b24c7`
- Symptom: after accepted contract migration, exact small `Update File` attempts for `tests/test_simulation.py` were blocked by `patch_hunk_mismatch_replacement_required`, pushing the agent toward full Delete/Add replacement of a large active success-test file.
- Cause: repeated hunk mismatch escalation did not distinguish ordinary implementation repairs from accepted active success-test contract migrations.
- Fix: when an active success-test file has a recent accepted contract migration, the replacement-required guard allows exact update attempts and gives migration-specific next-step guidance.
- Regression: `test_apply_workspace_patch_contract_migration_allows_exact_update_after_repeated_hunk_mismatches`.

## 2026-05-19 - Incomplete Research Summary Accepted

- Run: `phase_web_225ca559`
- Symptom: `submit_research_summary` accepted notes saying `Research in progress`, `Currently 2 findings persisted`, and `need minimum 3 findings before completion`; research review then treated the note as stale and allowed planning.
- Cause: placeholder/pending research-summary detector did not cover progress/incomplete-count wording.
- Fix: expanded `_RESEARCH_SUMMARY_PLACEHOLDER_RE` to reject progress-state handoffs such as `research in progress`, `continuing to gather evidence`, and `need/currently N findings` before completion.
- Regression: `test_submit_research_summary_rejects_captured_incomplete_progress_notes`.

## 2026-05-19 - Import Repair Added Duplicates Instead Of Removing Broken Symbol

- Run: `phase_web_f3cdbe8c`
- Symptom: execute tried to repair `src/civilization/game_state.py` after `ImportError: cannot import name 'BorderPos' from ...map`, but repeated `Update File` patches only added corrected `.map` import lines while leaving the stale `BorderPos` import at the top, so every import check kept failing.
- Cause: `apply_workspace_patch` validated local module existence but not whether `from .module import Name` names are actually exported by that local module. The patch engine therefore accepted a syntactically valid Python file that still had the same broken local import plus duplicate replacement imports.
- Fix: extended Umbrella's Python import guard to resolve local module contents and block `python_missing_local_import_symbol` when an imported local symbol is absent, including same-patch planned contents.
- Regression: `test_apply_workspace_patch_blocks_import_repair_that_keeps_missing_symbol` replays the captured duplicate-import repair shape and verifies a real replacement hunk is still accepted.

## 2026-05-19 - Coarse Execute Leaves Exhausted The Web Run Budget

- Run: `phase_web_6dc39018`
- Symptom: a clean Web UI run reached execute, but by round 100/120 it was still repairing `phase-1-subtask-2` (`tests/test_models.py`) with only 1 of 9 subtasks complete. The accepted plan packed multiple domains into broad leaves such as domain models (5 files), API (7 files), frontend (7 files), and mechanics (7 files), so one success test generated a large API-matching repair loop.
- Cause: the plan prompt asked for bounded subtasks, but the runner only enforced an upper leaf count (`>12`) and did not reject over-broad implementation leaves. That pressure encouraged fewer, wider subtasks. Separately, the Web UI recorded `max_rounds` but did not pass the selected round/verify budgets into the PhaseRunner worker environment, so Umbrella was displaying control values that Ouroboros did not reliably inherit.
- Fix: relaxed large-app compactness to 8-16 leaves, added a runner guard that rejects broad greenfield/full-stack implementation leaves with more than a bounded file span, clarified the plan prompt to split large domains into 2-4 file vertical leaves, raised the documented default Web UI/Ouroboros run budget to 200 rounds, and made the Web UI worker propagate `OUROBOROS_MAX_ROUNDS` and `OUROBOROS_WEB_MAX_VERIFY_RETRIES` to the inherited runtime.
- Regression: `test_latest_phase_plan_execution_floor_rejects_captured_coarse_fullstack_leaf`, `test_latest_phase_plan_execution_floor_accepts_sixteen_narrow_fullstack_leaves`, `test_web_phase_defaults_use_documented_ouroboros_round_budget`, and `test_phase_runner_worker_applies_web_round_and_verify_limits`.

## 2026-05-19 - Plan Compactness Contract Split Between Runner And Tool

- Run: `phase_web_702a5d62`
- Symptom: after the coarse-leaf fix, the live Web UI `propose_phase_plan` tool still rejected a 13-leaf greenfield plan with old wording: `keep large greenfield Umbrella plans compact at roughly 8-12 leaves`, even though the runner execution-floor contract had been changed to accept up to 16 narrow leaves.
- Cause: the compactness rule existed in multiple product layers. `umbrella/orchestrator/runner.py` had the new `8-16` guard, but `umbrella/deep_agent_tools/phase_contract_paths.py`, `umbrella/prompts/phases/plan.system.md`, and the `decision-policy` skill still used the old `8-12/>12` guidance.
- Fix: aligned tool validator, plan prompt, and planning skill to the same 8-16 contract.
- Regression: `test_propose_phase_plan_accepts_thirteen_narrow_greenfield_leaves` and the updated `test_propose_phase_plan_rejects_over_granular_greenfield_plan` cover the tool-level boundary used by Web UI runs.

## 2026-05-19 - Bare Src Package Layout Accepted

- Run: `phase_web_2bb95da0`
- Symptom: `propose_phase_plan` and `plan_review` accepted a greenfield Python plan that put production modules at `src/__init__.py`, `src/game_engine.py`, `src/models.py`, and parallel roots like `src/config/...`, `src/agents/...`, and `src/api/...`. Execute then began writing that layout.
- Cause: both the phase-plan path validator and workspace write guard treated any Python path under `src/` as valid, instead of requiring one canonical `src/<package>/...` package root.
- Fix: reject bare `src/*.py`/`src/__init__.py` and multiple parallel Python roots under `src/` in both plan validation and workspace patch/write validation; updated plan and plan-review prompts to state the same rule.
- Regression: `test_propose_phase_plan_rejects_captured_bare_src_python_layout`, `test_apply_workspace_patch_blocks_bare_src_python_module`, and `test_apply_workspace_patch_blocks_parallel_src_python_roots`.

## 2026-05-19 - Command-Prefixed Success Tests Accepted

- Run: `phase_web_ee48ce93`
- Symptom: `propose_phase_plan` accepted 16 subtasks whose `success_test` values were strings like `Command: pytest tests/test_game_state.py -v -k 'test_gamestate_initialization'`. These are not exact executable commands and would fail if passed directly to the workspace shell.
- Cause: prose-prefix validation only checked alias fields such as `verification`, and the prefix list did not include `Command:`.
- Fix: added one shared success-test prose-prefix regex covering `Run:`, `Verify:`, `Check:`, `Assert:`, and `Command:` for both top-level `success_test` and alias fields.
- Regression: `test_propose_phase_plan_rejects_captured_command_prefixed_success_test`.

## 2026-05-19 - Broad Leaf Guard Still Split Between Tool And Runner

- Run: `phase_web_3bbfc06b`
- Symptom: `propose_phase_plan` accepted broad implementation leaves such as `turn_engine (5 files)`, `gmas_economy (6 files)`, `gmas_diplomacy (5 files)`, `api_server (5 files)`, `frontend_game_display (5 files)`, and `frontend_controls (5 files)`. After `submit_phase_plan`, the Runner execution-floor guard rejected the latest plan and looped back to plan, wasting Web UI/LLM rounds.
- Cause: the earlier compactness fix aligned the total 8-16 leaf count across layers, but the per-leaf 2-4 file-width guard only existed in `umbrella/orchestrator/runner.py`; the `propose_phase_plan` tool contract still allowed over-broad greenfield/full-stack leaves.
- Fix: added a tool-level broad-leaf validator to `umbrella/deep_agent_tools/phase_contract_paths.py` using the same large-greenfield and setup/final exemption shape as Runner, so broad leaves are rejected before `submit_phase_plan`.
- Regression: `test_propose_phase_plan_rejects_captured_broad_leaf_before_submit` replays the accepted broad-leaf shape, and `test_propose_phase_plan_accepts_split_version_of_captured_broad_leaf` covers the narrow split version.

## 2026-05-19 - Supported LLM Aliases Deprecated In Phase Memory

- Run: `phase_web_40737336`
- Symptom: execute accepted `mutate_phase_plan` contract-migration memory saying `LLM_*` variables were legacy/unsupported and should be removed, even though the product contract requires generated workspaces to support `OUROBOROS_LLM_API_KEY/LLM_API_KEY`, `OUROBOROS_LLM_BASE_URL/LLM_BASE_URL`, and `OUROBOROS_MODEL/LLM_MODEL`. `mark_subtask_complete` then accepted completion memory that narrowed the runtime description to `OUROBOROS_LLM_*` only.
- Cause: completion-memory guards rejected forbidden `OPENAI_*`/provider-specific claims, but did not protect supported `LLM_*` aliases from being falsely deprecated or excluded in higher-level subtask memory and palace mirrors.
- Fix: added a shared phase-memory guard that rejects contract-migration and completion memory when it treats `LLM_API_KEY`, `LLM_BASE_URL`, or `LLM_MODEL` as unsupported/removable, or narrows LLM runtime support to `OUROBOROS_*` only.
- Regression: `test_mutate_phase_plan_rejects_captured_llm_alias_deprecation_memory`, `test_mutate_phase_plan_accepts_supported_llm_alias_memory`, `test_mark_subtask_complete_rejects_captured_ouroboros_only_alias_memory`, and `test_mark_subtask_complete_accepts_supported_llm_alias_memory`.

## 2026-05-19 - Repeated Plan Rejections Lacked A Concrete Repair Recipe

- Run: `phase_web_6392fc0a`
- Symptom: a clean Web UI run stayed in plan after five `propose_phase_plan` attempts. The validator correctly rejected missing exact LLM aliases, too many leaves, broad subtasks, non-automatable success tests, and mock/fake LLM paths, but the rejection feedback did not give the model a compact shape to resubmit. The mock/fake rejection also omitted the matched text, making it hard to tell whether the issue was real or protective wording.
- Cause: plan prompt contained the individual rules, but neither prompt nor tool feedback gave a reusable repair recipe for repeated multi-issue LLM/frontend/backend plan failures.
- Fix: added validator feedback with matched text for LLM mock/fake detections, appended a concise repair recipe for common plan-policy failures, and strengthened the plan prompt with a 10-14 leaf LLM/frontend/backend skeleton, exact alias spelling requirements, and guidance to keep LLM skips inside checked-in tests instead of custom success-test flags.
- Regression: `test_propose_phase_plan_rejects_mock_fake_llm_test_strategy`, `test_propose_phase_plan_rejection_gives_llm_repair_recipe`, and `test_plan_prompt_documents_executable_leaf_payload_contract`.

## 2026-05-19 - Research Palace Saved Forbidden LLM Fallback Memory

- Run: `phase_web_8b680883`
- Symptom: research `palace_add` saved a finding to `palace.run` that said bot timeout should fallback to simpler heuristics. A later `submit_research_summary` correctly rejected that id, but the bad drawer was already in hierarchical memory. The accepted summary also cited another finding that mentioned caching similar decisions and fallback to heuristics when LLM cost is high.
- Cause: `palace_add` gated research/plan memory using `phase_label`, but Web UI phase contexts can expose `phase_label=linear` while the real phase is in `task_id` (`...:research`). Separately, `_llm_fallback_handoff_issue` treated an entire long finding as protective if any unrelated sentence matched `no/not ... fallback`, so a local unsafe fallback sentence could be missed. The review regex also matched singular `heuristic` but missed captured plural `heuristics`.
- Fix: infer the `palace_add` guard phase from the `task_id` suffix when the phase label is generic, evaluate protective fallback wording only in a local window around the matched fallback claim, and cover plural `heuristics` in the shared fallback regexes plus the workspace write guard.
- Regression: `test_palace_add_rejects_captured_research_fallback_with_linear_label`, `test_palace_add_accepts_protective_no_fallback_with_linear_label`, `test_submit_research_summary_rejects_captured_cost_fallback_finding`, `test_submit_research_summary_allows_protective_no_fallback_finding`, and `test_apply_workspace_patch_blocks_plural_heuristics_llm_fallback`.

## 2026-05-19 - Research Summary Double-Counted Memory Aliases

- Run: `phase_web_0dd335be`
- Symptom: a no-limit Web UI run saved two real research `palace_add` entries, then `submit_research_summary` accepted four `findings_ids` by listing both the primary UUID and legacy `drawer_*` id for each entry. The handoff reported `findings: 4` and persisted duplicate aliases in `research_summary_latest.json`.
- Cause: research-summary validation treated every accepted id alias as an independent finding. It did not canonicalize `id`/`memory_id`/`artifact_id`/`legacy.id` back to the same `palace_add` row before counting or persisting findings.
- Fix: build a canonical alias map for accepted `palace_add` rows, reject summaries that cite the same memory entry through multiple aliases, count only unique canonical findings, and persist primary ids in the summary artifact/control signal.
- Regression: `test_submit_research_summary_rejects_duplicate_id_and_legacy_aliases` and `test_submit_research_summary_normalises_legacy_alias_to_primary_id`.

## 2026-05-19 - Plan Review Accepted Conservative LLM Fallback

- Run: `phase_web_30316f53`
- Symptom: `propose_phase_plan` accepted a plan whose `decision_policy.agent_behavior` said `LLM failure logs error and uses fallback conservative strategy`; `plan_review` read the fresh artifact and still submitted `verdict=ok`, so execute started from an unsafe LLM fallback plan.
- Cause: phase-plan fallback detection treated the whole string as protective because it contained nearby wording `not hardcoded rules`, even though the later sentence allowed a fallback strategy. Plan review only enforced artifact-read discipline and did not re-run the plan policy validator before `ok`.
- Fix: evaluate fallback protection around the matched fallback claim instead of the whole plan string, classify `conservative strategy` as a forbidden LLM fallback replacement, and make `plan_review ok` re-run phase-plan policy checks on the fresh artifact.
- Regression: `test_propose_phase_plan_rejects_captured_conservative_strategy_fallback` and `test_plan_review_ok_rejects_captured_conservative_strategy_fallback_plan`.

## 2026-05-19 - Subtask Completion Chased Future Verify Gate

- Run: `phase_web_651c6791`
- Symptom: `st_004` passed its declared engine success test, but `mark_subtask_complete` kept rejecting it because the latest global `run_workspace_verify` still had future/global failures such as `multi_agent_gmas` skill import checks. Ouroboros then requested an extra subtask to edit `.umbrella/workspace.toml` and `.memory/blockers.md`, saved a blocker note to run memory, and created a placeholder `src/civsim/ai/gmas_bridge.py` to satisfy the check.
- Cause: subtask completion treated any red workspace verify as a leaf blocker even when the failing required step belonged to a later planned subtask. `request_extra_subtask` did not validate proposed subtasks against plan/workspace policy, and `apply_workspace_patch` allowed empty non-init source files and placeholder integration bridges.
- Fix: scope red verify completion blocking to failures that mention the active subtask's declared success test files, planned files, or contract-migration files; allow future GMAS/LLM skill failures to remain for later GMAS/LLM subtasks. Validate extra-subtask proposals with the same phase-plan policy and reject control-plane/memory workaround subtasks. Block empty non-`__init__` source files and placeholder GMAS/LLM bridge code at workspace patch time.
- Regression: `test_subtask_completion_allows_future_gmas_verify_failure`, `test_subtask_completion_blocks_relevant_verify_failure_in_touched_file`, `test_request_extra_subtask_rejects_captured_control_plane_workaround`, `test_request_extra_subtask_accepts_product_subtask`, `test_apply_workspace_patch_blocks_empty_non_init_python_file`, `test_apply_workspace_patch_allows_empty_init_file`, and `test_apply_workspace_patch_blocks_placeholder_gmas_bridge`.

## 2026-05-19 - Claimed Success Test Evidence Bypassed Completion Gate

- Run: `phase_web_86d250a5`
- Symptom: initial monitoring showed `mark_subtask_complete` first rejecting `setup-project` for missing exact success-test evidence, then accepting after the model added a textual evidence item saying `python -m pytest tests/test_setup.py -q` passed.
- Resolution: not a product bug. A later audit of the full `tools.jsonl` found the missing `shell` row at `2026-05-19T14:18:50Z`: the exact command `python -m pytest tests/test_setup.py -q` ran with exit code 0 and `4 passed`. The earlier monitor filtered too narrowly and omitted `shell` rows.
- Fix: no code change; keep future monitor snapshots including `shell` and `terminal_session` whenever auditing subtask completion.
- Regression: none needed because the captured payload had valid machine evidence.

## 2026-05-19 - GMAS Compliance-Only Imports Passed Quality Gates

- Run: `phase_web_86d250a5`
- Symptom: `run_unit_tests` reported `skill_compliance:multi_agent_gmas` and `skill_quality:multi_agent_gmas_no_mock_scaffold` as passing even though application files contained only GMAS re-exports with comments like `Import gmas to satisfy the GMAS skill requirement`.
- Cause: placeholder bridge blocking looked for `placeholder/stub/todo` wording near GMAS/LLM but missed compliance-only language, and the skill-quality scanner did not flag code that imports GMAS only to satisfy a control-plane check.
- Fix: blocked GMAS/LLM compliance-only import language both at `apply_workspace_patch` time and in the skill-quality/source-policy scanner.
- Regression: `test_apply_workspace_patch_blocks_compliance_only_gmas_import` and `test_fails_on_compliance_only_gmas_imports`.

## 2026-05-19 - Tool Audit Log Depth-Limited Phase Mutation Files

- Run: `phase_web_0577b660`
- Symptom: execute called `mutate_phase_plan` with `contract_migration_files`, but `tools.jsonl` recorded the value as `[{"_depth_limit": true}]`. The persisted `phase_plan.json` had the correct file list, so the mutation itself succeeded, but the audit log lost the exact captured payload needed for regression and operator review.
- Cause: generic tool-argument sanitization replaced nested values deeper than three levels with `_depth_limit`, including short, non-secret string lists in control-plane contracts.
- Fix: raised the tool-argument audit sanitizer depth enough to preserve compact nested control-plane payloads while still depth-limiting deeper structures and truncating large strings.
- Regression: `test_sanitize_tool_args_preserves_phase_mutation_file_lists` and `test_sanitize_tool_args_still_depth_limits_deep_payloads`.

## 2026-05-19 - Quoted Python Source Lines Passed Patch Validation

- Run: `phase_web_0577b660`
- Symptom: `apply_workspace_patch` accepted `src/civ_game/models/player.py` where every source line was wrapped as a quoted Python string literal. `ast.parse` succeeded because the file was just many top-level string expressions, but the intended classes/imports were never defined and the next success test failed.
- Cause: workspace patch validation checked syntax and import resolution but did not detect source-code files that are transport-escaped line by line.
- Fix: added a Python source guard that blocks files where most non-empty lines are quoted string literals containing code-like markers and the parsed module defines no real top-level symbols.
- Regression: `test_apply_workspace_patch_blocks_quoted_python_source_lines`.

## 2026-05-19 - Research Memory Forced LLM Env Contract Into Every Finding

- Run: `phase_web_fa9a4d2c`
- Symptom: `palace_add` rejected useful domain findings such as game mechanics scope, GMAS graph topology, and stack decisions unless each finding repeated the full `OUROBOROS_LLM_API_KEY`/`LLM_API_KEY`, `OUROBOROS_LLM_BASE_URL`/`LLM_BASE_URL`, and `OUROBOROS_MODEL`/`LLM_MODEL` runtime contract.
- Cause: the same LLM env omission guard was used for both authoritative handoffs and individual research memory entries. That protected plan safety, but polluted hierarchical memory by forcing every low-level finding to carry the same runtime boilerplate.
- Fix: `palace_add` still rejects narrow or unsafe credential contracts, but no longer requires every domain finding to repeat the full env contract. `submit_research_summary` now enforces the full LLM env contract across the summary plus cited findings, so the handoff remains safe while low-level memory stays focused.
- Regression: `test_palace_add_accepts_domain_research_without_repeating_llm_env_contract`, `test_submit_research_summary_rejects_llm_handoff_without_env_contract`, and `test_submit_research_summary_accepts_domain_finding_when_env_contract_cited`.

## 2026-05-19 - Research Summary Missed LLM-Driven Agent Wording

- Run: `phase_web_3c6a6b33`
- Symptom: after the memory-boilerplate fix, `submit_research_summary` accepted a research handoff citing LLM-driven game AI findings without any cited finding or notes containing the full Umbrella/Ouroboros env alias contract.
- Cause: the summary-level LLM env detector required explicit env aliases only for phrases like `LLM-powered` or `LLM calls`, but missed common handoff wording such as `LLM-driven`, `LLM agents`, and `LLM AI design`.
- Fix: expanded the LLM env-contract trigger to cover `LLM-driven`, `LLM-backed`, `LLM agents/bots/decisions/game AI/AI design/strategy`, while keeping domain `palace_add` entries free from per-finding boilerplate.
- Regression: `test_submit_research_summary_rejects_captured_llm_driven_without_env_contract`; updated positive plan/research fixtures now spell the exact env alias contract when they describe LLM-driven behavior.

## 2026-05-19 - Unsafe Research Finding Remained In Hot Memory

- Run: `phase_web_2dc4819e`
- Symptom: `palace_add` saved a current-run research finding whose LLM runtime contract included `Graceful Degradation: When LLM credentials are absent, provide mock/simulation mode for testing without paying for AI`. `research_review` detected the unsafe hot-memory finding, but its `revise` verdict was rejected as a minor wording/citation issue; it then submitted `ok` because `research_summary_latest.json` had cleaner `No mock/fallback mode` wording.
- Cause: LLM fallback/mock detection evaluated too much surrounding text, so unrelated `Unit tests without real LLM (mock responses)` could make a production `mock/simulation mode` clause look protective. Research-review blocking classification also treated unsafe cited memory as a summary/citation detail, and `ok` did not re-run the policy check on findings cited by the latest summary.
- Fix: localize fallback/mock policy checks to the matched claim window, allow `replace/remove/revise/contains unrevised` review wording as protective only when it names a bad clause to remove, classify unsafe hot-memory findings as blocking research defects, make `research_review ok` re-check cited `palace_add` findings from the research task, and update the research-review prompt so cited current-run hot memory cannot be waved through as harmless when the summary is cleaner.
- Regression: `test_palace_add_rejects_captured_graceful_degradation_mock_mode`, `test_research_review_revise_allows_unsafe_hot_memory_blocker`, and `test_research_review_ok_rejects_summary_citing_unsafe_hot_memory`.

## 2026-05-19 - Env-Alias Fallback Regex Matched `LLM_CONFIG.md`

- Run: affected-suite regression after the hot-memory fix.
- Symptom: `test_submit_micro_review_rejects_fallback_model_strategy_revision` failed because review feedback asking for `docs/LLM_CONFIG.md` plus a `fallback model strategy` was accepted. The fallback guard misread `LLM_CONFIG` as a supported runtime alias.
- Cause: the env-alias fallback allowlist matched arbitrary `LLM_[A-Z0-9_]*` and `OUROBOROS_*` strings instead of only the supported aliases/wildcards: `OUROBOROS_LLM_API_KEY`, `OUROBOROS_LLM_BASE_URL`, `OUROBOROS_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_*`/`OUROBOROS_LLM_*` wording.
- Fix: narrowed both review and plan env-alias fallback regexes so filenames or invented variables like `LLM_CONFIG` cannot protect real fallback-model strategy text.
- Regression: existing `test_submit_micro_review_rejects_fallback_model_strategy_revision` plus alias-positive checks `test_submit_micro_review_allows_runtime_env_alias_fallback_revision` and `test_submit_micro_review_allows_parenthetical_runtime_env_alias_fallback`.

## 2026-05-19 - Execute Learned GMAS Context From First Write Rejection

- Run: `phase_web_8d3da1e8`
- Symptom: execute started, attempted its first workspace write, was correctly blocked by `gmas_context_before_first_write`, then called `get_gmas_context` and retried. The guard protected the workspace, but the agent was still learning the contract from a failed write instead of beginning execute with a GMAS retrieval.
- Cause: the execute system prompt had the pre-write requirement buried inside a long workspace-write bullet, while Umbrella's prior-knowledge skill banner still used the older `update_workspace_seed` wording instead of the current general write tools such as `apply_workspace_patch`. The current subtask card prompt also did not repeat the retrieval requirement near the concrete subtask/action context.
- Fix: frontloaded a conditional GMAS/LLM-agent gate in the execute prompt, added a GMAS/LLM pre-write line to the projected execute subtask prompt only when Umbrella has detected `multi_agent_gmas` or the active subtask itself is LLM/agent-oriented, and updated the Umbrella skill/prior-knowledge banner to say `get_gmas_context`/`search_gmas_knowledge` must happen before the first workspace write (`apply_workspace_patch`, `update_workspace_seed`, `repo_write_commit`, seed update, or equivalent). It now also clarifies that prefetched GMAS context is background, not a substitute for execute-time retrieval.
- Regression: `test_execute_prompt_frontloads_conditional_gmas_pre_write_contract`, `test_execute_prompt_adds_gmas_prewrite_gate_only_when_required`, `test_build_phase_task_injects_gmas_gate_from_detected_domain`, updated `test_execute_prompt_names_current_projected_subtask`, and updated GMAS prior-knowledge banner assertions.

## 2026-05-19 - Research Summary Accepted Mock Fallback After Test-Skip Wording

- Run: `phase_web_bf471ba0`
- Symptom: `submit_research_summary` accepted notes saying `MUST provide fallback mode (mock/deterministic bots) when LLM unavailable` and `using mock opponents`. Research review later revised it, but the unsafe handoff had already been persisted to `research_summary_latest.json`.
- Cause: the local fallback-protection regex treated the nearby word `Tests` in `Tests MUST skip LLM-dependent tests when credentials are missing` and the phrase `LLM not configured` as protective context for the later forbidden fallback/mock claim.
- Fix: narrowed protective fallback wording so only explicit detect/assert/enforce/prevent/prove/confirm style validation language protects a fallback mention, and added an explicit-danger check for required/provided fallback modes, mock/deterministic bots, and mock opponents. Direct `never/no fallback` wording remains allowed.
- Regression: `test_submit_research_summary_rejects_captured_mock_fallback_notes` plus focused positives for protective no-fallback review/summary wording.

## 2026-05-19 - Research Summary Accepted Human-Only Fallback Mode

- Run: `phase_web_52ccc80f`
- Symptom: after the mock fallback fix, `submit_research_summary` accepted a cleaner handoff that still said `fail fast if credentials missing, support human-only fallback mode`.
- Cause: the fallback detector only matched fallback claims when paired with mock/static/default/strategy-style words. A plain `fallback mode` in an LLM/GMAS handoff slipped through even though the required behavior is fail/skip/pause/clear real-LLM-required messaging, not a fallback product mode.
- Fix: treat `fallback mode` itself as a fallback-policy claim in LLM/GMAS/bot handoffs, with env-alias fallback chains and explicit `no/never fallback` wording still allowed.
- Regression: `test_submit_research_summary_rejects_captured_human_only_fallback_mode`.

## 2026-05-19 - Phase Manifest Prompts Were Not Active Phase Input

- Run: discovered while investigating `phase_web_9701612a`.
- Symptom: repeated prompt edits to `umbrella/prompts/phases/*.system.md` had weaker-than-expected effect. The phase runner built rich generated task input from artifacts, tools, and memory, but the manifest `prompt_files.system` and `prompt_files.user_overlay` contents were not loaded into that active phase task input.
- Cause: `PhaseManifest` parsed prompt file paths, but `build_phase_task` did not read them. Ouroboros only received the generated worker prompt plus manifest metadata, so some phase contracts existed as files/tests but were not reliably visible to the deep-agent worker.
- Fix: `build_phase_task` now loads manifest prompt files into a `Phase instructions loaded from manifest` section, records loaded paths in `context_overlays.phase_prompt_files_loaded`, and runner passes the repo root so this works from any workspace. Added a manifest test that prompt file paths exist.
- Regression: `test_build_phase_task_loads_manifest_prompt_files` and `test_manifest_prompt_files_exist`.

## 2026-05-19 - Research Summary Accepted Rule-Based Graceful Degradation

- Run: `phase_web_9701612a`
- Symptom: the second research summary was persisted with an otherwise good LLM env contract but still said missing credentials should use `graceful degradation to rule-based AI`. Plan later corrected toward real LLM only, but the unsafe research handoff had already been written to `.memory/drive/state/research_summary_latest.json` and approved by review.
- Cause: the research handoff/review guard focused on explicit `fallback` wording. It already blocked mock/human-only/fallback-mode variants but did not treat `graceful degradation to rule-based AI` as the same forbidden replacement behavior when the word fallback was absent.
- Fix: added a conditional Umbrella LLM/agent runtime policy capsule loaded from detected domains (`active_skills.json`, `domains.json`, or `workspace.toml`) and injected into phase task input only for LLM/agent/GMAS contexts. The shared research/review guard now treats graceful degradation plus rule-based/heuristic/action/runtime wording as forbidden LLM replacement behavior.
- Regression: `test_build_phase_task_loads_detected_domains_from_active_skills`, `test_submit_research_summary_rejects_captured_rule_based_degradation`, and `test_research_review_ok_rejects_captured_rule_based_degradation_summary`.

## 2026-05-19 - Research Finding Floor Counted Summary As A Finding

- Run: `phase_web_1254769e`
- Symptom: research had manifest/prompt requirements for 3 accepted `palace_add` findings, but `submit_research_summary` accepted the handoff after only 2 accepted findings. The phase runner also considered the summary itself as one `palace.run` write, so research advanced with a thinner memory hierarchy than the phase contract requested.
- Cause: both the phase-control summary validator and the phase-runner handoff floor subtracted one from `min_palace_writes`, treating `submit_research_summary` as part of the finding floor. That contradicted the prompt wording: the summary is the handoff after concrete findings, not a substitute for a finding.
- Fix: research `min_palace_writes` now means N accepted `palace_add` findings. `submit_research_summary` and runner handoff validation both require the full floor before accepting/advancing.
- Regression: `test_submit_research_summary_requires_manifest_finding_floor`, `test_research_summary_finding_floor_derived_from_manifest`, and `test_latest_research_summary_requires_manifest_finding_floor`.

## 2026-05-19 - Research Summary Promoted Mock LLM Behavior Verification

- Run: `phase_web_c8307523`
- Symptom: research wrote three accepted findings, then `submit_research_summary` persisted a top-level handoff whose testing strategy said `integration tests (mock LLM for bot behavior verification)`. Research review approved it, so plan/execute inherited memory that could justify proving core bot behavior with a mocked LLM.
- Cause: research-summary and `palace_add` memory guards only called the fallback/degradation detector. The separate mock/fake/dry-run LLM test-double detector existed for review feedback and plan validation, but not for research handoffs or hot memory promotion. Its protective helper also treated generic `test/tests` wording before `mock` as safe, which would be too broad for phase memory.
- Fix: added a shared LLM test-double handoff guard for research summaries, research-review `ok`, and research/plan `palace_add`. It rejects mock/fake/dry-run LLM test doubles for LLM/GMAS/bot handoffs unless the wording is explicitly protective, such as rejecting or forbidding mock LLM paths. The protective helper no longer treats generic `test/tests/verification` wording as sufficient protection.
- Regression: `test_submit_research_summary_rejects_captured_mock_llm_behavior_verification`, `test_research_review_ok_rejects_captured_mock_llm_behavior_summary`, and `test_palace_add_rejects_captured_mock_llm_behavior_verification`.

## 2026-05-19 - Execute Still Learned GMAS Context From First Write Block

- Run: `phase_web_21402401`
- Symptom: after prompt-level GMAS pre-write fixes, a clean Web UI run reached execute and the first `apply_workspace_patch` was still blocked with `gmas_context_before_first_write`. The agent then called `get_gmas_context` and continued. The workspace was protected, but the deep agent still learned the rule from a failed write instead of receiving the GMAS context before action.
- Cause: the conditional execute prompt and projected subtask card contained the pre-write instruction, but the first accepted subtask was generic project setup, so the model optimized directly for setup writes. The only hard sequencing lived in the write guard, which necessarily produces a blocked tool call if the model ignores the prompt.
- Fix: added an Umbrella execute prelude. When `build_phase_task` detects `multi_agent_gmas` for execute, the PhaseRunner retrieves GMAS context before launching the worker, injects the prelude into the phase input, and writes a transparent `get_gmas_context` row to `tools.jsonl` with `injected_by=umbrella_phase_prelude`. The write guard now sees same-task GMAS context before the first workspace write, while the agent is still told to refresh context before task-specific GMAS agent/graph/tool code.
- Regression: `test_phase_runner_injects_gmas_context_prelude_before_execute_write`.

## 2026-05-19 - Workspace Palace Memory Was Flattened Or Hidden From Later Phases

- Run: `phase_web_0889a80b`
- Symptom: after research/plan wrote concrete `palace_add` entries, `memory/knowledge/umbrella_memory.md` still said `No palace memories yet`. Plan subtask cards were also saved to the logical `palace.run` store even when their `palace_path` was `workspaces/civilization/plan/subtasks`.
- Cause: the drive sync bridge still searched only the manager `.umbrella/palace` path, while the current MemPalace writes are workspace-scoped under `workspaces/<id>/.memory/palace`. The plan phase manifest also routed `subtask_card` writes to `palace.run`, and the legacy hierarchical mirror collapsed nested paths such as `plan/subtasks` into the room `plan`.
- Fix: changed bridge recall to use `palace_path_for(repo_root, workspace_id)`, routed plan `subtask_card` and execute/subtask scoped artifacts through `palace.subtask`, preserved nested legacy rooms like `plan/subtasks`, and added subtask-id metadata for subtask-scoped `palace_add` writes.
- Regression: `test_sync_umbrella_context_to_drive_reads_workspace_palace_memory`, `test_phase_memory_routes_subtasks_to_subtask_store`, `test_palace_add_routes_plan_subtask_card_to_subtask_store`, and `test_save_umbrella_memory_preserves_nested_workspace_room`.

## 2026-05-19 - Default Palace Path Collapsed Phase Memory Into Generic `phase`

- Run: `phase_web_43e27251`
- Symptom: accepted research findings had the logical `palace.run` store, but the legacy hierarchical mirror saved them under room `phase` because the model omitted `palace_path`. The plan phase could still retrieve them semantically, but operator memory hierarchy lost the real phase name.
- Cause: `_palace_add` defaulted missing `palace_path` to `workspaces/<id>/phase` instead of deriving the current phase from `task_id`/phase guard. This also would make omitted plan subtask paths land outside `plan/subtasks`.
- Fix: `_palace_add` now derives the default hierarchical path from the guarded phase label (`research`, `plan`, etc.) and routes `kind=subtask_card` to `<phase>/subtasks` when no path is supplied.
- Regression: existing `test_palace_add_accepts_optional_metadata` now checks default `workspaces/<id>/research`, plus `test_palace_add_defaults_plan_subtask_path_from_phase`.

## 2026-05-19 - Protective LLM Runtime Docs Were Blocked As Provider Defaults

- Run: `phase_web_3ec402e6`
- Symptom: execute repeatedly blocked `docs/architecture.md` because it documented `Forbidden: hardcoded provider/model defaults such as https://api.openai.com/v1`, even though that sentence was protective rather than a generated runtime default.
- Cause: `_llm_runtime_contract_block` scanned markdown documentation the same way as executable code/env files and did not distinguish forbidden/no/default warning prose from actual provider fallback configuration.
- Fix: allow markdown provider-default mentions only when the local line/block is explicitly protective; keep blocking executable code, env files, package config, and non-protective docs that set OpenAI/gpt defaults.
- Regression: add a captured apply-workspace-patch test proving protective docs pass while code/env defaults still fail.

## 2026-05-19 - Retry Watcher Used A Non-Declared Pytest Probe As Latest Failure

- Run: `phase_web_3ec402e6`
- Symptom: after repeated `python -m pytest tests/test_game_state.py -q` failures, execute ran a nonexistent probe `tests/test_game_state.py::TestCityBuildingType::test_add_building`. `request_watcher_review` recorded that probe as `latest_failure`, so the review diagnosis was about import/runtime confusion rather than the declared success test failures.
- Cause: retry escalation used loose pytest target alternatives for both failure counting and latest-failure evidence. A narrower pytest node-id under the same file was allowed to replace the declared success-test row in the watcher payload.
- Fix: keep loose subset matching only for counting repeated repair attempts, but prefer the latest exact declared success-test failure for watcher evidence and escalation messaging.
- Regression: add a captured retry-watcher test where two declared full-file failures plus one bad node-id probe produce `latest_failure.command` from the declared full-file command, not the probe.

## 2026-05-19 - GMAS Prewrite Context Was Task-Scoped Instead Of Subtask-Scoped

- Run: `phase_web_3ec402e6`
- Symptom: the execute prelude correctly fetched GMAS context for `project-setup`, but later execute retries considered GMAS context already present for future subtasks because the same `phase_web_...:execute` task log contained any `get_gmas_context` row.
- Cause: the GMAS first-write gate checked only tool name presence in the current task log and explicit-call counters. It did not validate retrieval success or bind the context to the active subtask / query surface.
- Fix: make injected GMAS context and accepted tool-log evidence subtask-aware and success-aware; generic setup context should not satisfy future LLM/GMAS implementation writes unless the active subtask has fresh relevant context.
- Regression: add tests that stale context from a previous subtask does not unblock GMAS/agent writes, failed context retrieval does not count, and fresh context for the active subtask does.

## 2026-05-19 - Submitted Plan Was Ceremonial While Latest Draft Drove Review/Execute

- Run: read-only audit after `phase_web_3ec402e6`.
- Symptom: `submit_phase_plan(plan_id=...)` validated the selected plan id, but plan_review and execute still read `phase_plan_proposal_latest.json`. A later unsubmitted proposal could become the reviewed/executed contract.
- Cause: the control-plane had proposal artifacts but no canonical submitted-plan artifact. Runner floor checks and execute subtask projection treated "latest proposal" as authoritative.
- Fix: `submit_phase_plan` now persists `.memory/drive/state/phase_plan_submitted_latest.json`; plan_review prompt/guards and runner projection read the submitted contract. Execute subtask sync also updates same-id cards when success tests/goals/files change.
- Regression: `test_submit_phase_plan_persists_selected_plan_not_latest`, `test_phase_plan_execution_floor_uses_submitted_plan_over_latest`, `test_sync_execute_subtasks_updates_same_id_contract_changes`, and submitted-plan plan_review artifact tests.

## 2026-05-19 - Subtask Completion Accepted Failed Or Stale Success Evidence

- Run: read-only audit after `phase_web_3ec402e6`.
- Symptom: success tests naming tools such as `run_real_e2e`/`harness_run` could be satisfied by any tool row, and shell success evidence could be reused after later workspace writes.
- Cause: completion gates checked presence for most required tools and only applied stale-after-write logic to `run_workspace_verify`.
- Fix: required tool success tests now need a passing payload/status, and shell/run-workspace-command evidence must be rerun after the last effective repair write.
- Regression: `test_mark_subtask_complete_rejects_failed_required_tool_success_test`, `test_mark_subtask_complete_rejects_stale_shell_success_after_repair_write`, and updated harness/verify completion tests.

## 2026-05-19 - Memory Side Channels Promoted Or Hid The Wrong Tier

- Run: read-only audit after `phase_web_3ec402e6`.
- Symptom: `mirror_subtask_to_memory` silently failed because it passed unsupported `workspace_id` into `MemPalace.add`; unverified lessons were mirrored as `verified`; `promote_to_durable` wrote only legacy memory while verify required `palace.durable`.
- Cause: older legacy-memory APIs and newer `MemPalace` store contracts had drifted apart. Exit criteria counted tool-log shape instead of requiring a real durable-store result.
- Fix: subtask mirrors now call `MemPalace.add` with subtask/run/task metadata; demoted lessons keep `unverified_lesson/avoid` tags and `verified=False`; `promote_to_durable` writes `palace.durable` with `tier=always_on`, `scope=cross_run_durable`, `verified=True`, and runner counts only rows with a durable store node id.
- Regression: `test_mirror_subtask_writes_to_palace`, `test_save_umbrella_lesson_does_not_semantically_verify_demoted_lesson`, `test_promote_to_durable_writes_verified_palace_durable_store`, and `test_verify_completion_rejects_promote_to_durable_without_durable_store`.

## 2026-05-19 - Periodic Recall Could Reinject Stale Write Memory

- Run: read-only memory audit after `phase_web_3ec402e6`.
- Symptom: periodic recall pulled recent workspace `changes/errors` by timestamp only, so old run write/error memory could appear as fresh `[WORKSPACE MEMORY]` in later phases.
- Cause: auto-recorded write memories had no task/run metadata, and recall did not filter run-scoped rooms by current task.
- Fix: auto-recorded write memory now includes `task_id` and `run_id`; periodic recall filters run-scoped `changes/errors` to the current run when a task id is available.
- Regression: `test_periodic_recall_filters_stale_run_scoped_change_memory` and `test_records_task_and_run_metadata_for_auto_changes`.

## 2026-05-19 - GitHub Discovery Rejected Common `max_results` Argument

- Run: `phase_web_d7847051`.
- Symptom: research called `github_project_search(query=..., language=..., max_results=...)` and received `TOOL_ARG_ERROR: unexpected keyword argument 'max_results'`. It later recovered with `max_repos`, but the live run lost a discovery attempt and operator clarity.
- Cause: most discovery tools expose `max_results`; `github_project_search` exposed only `max_repos`, so the tool schema/handler contract was inconsistent with the rest of the discovery surface.
- Fix: accept `max_results` as an alias for `max_repos`, clamp it through the same repo limit, and advertise the alias in the tool schema.
- Regression: `test_github_project_search_accepts_max_results_alias`.

## 2026-05-19 - Phase-Control Signals Still Flattened To `linear`

- Run: `phase_web_cc89f1ee`.
- Symptom: `phase_control_signals.jsonl` recorded `submit_preflight_report`, `submit_research_summary`, and `submit_phase_plan` with `"phase": "linear"` even though each `task_id` had a concrete suffix such as `:research` or `:plan`.
- Cause: earlier phase-label fixes covered phase-contract artifacts, but the shared phase-control signal writer still trusted `loop_state_view.phase_label` before task id. In Web UI phase runs that label can be generic `linear`.
- Fix: `phase_control_base` now derives the Umbrella phase from the `task_id` suffix first, treats generic `linear`/`phase` as non-authoritative, and uses that phase for control signals and research/submitted-plan artifacts.
- Regression: `test_phase_control_signal_derives_phase_from_task_id_when_label_is_linear`; `test_submit_research_summary_persists_latest_artifact` now asserts `phase=research` even when the loop label is `linear`.

## 2026-05-19 - Plan Accepted Decorative `echo` Success Sentinel That Stalled Execute

- Run: `phase_web_2d604b82`.
- Symptom: execute repaired the frontend build, but `project-setup` could not complete because the accepted success test was `cd frontend && npm install && npm run build && echo 'Frontend deps OK'`. The terminal tool treated `echo` as a separate executable on Windows and failed with `WinError 2`; `mark_subtask_complete` then correctly rejected the subtask for missing successful evidence.
- Cause: the plan contract rejected many fragile shell forms, but still allowed decorative shell output fragments such as `echo`/`printf`/`Write-Host` at the end of `success_test`. Those fragments add no behavioral proof and can become host-specific failures.
- Fix: `success_test` validation now rejects decorative shell output segments and the plan prompt explicitly tells planning agents to use the real proof command only, e.g. `cd frontend && npm run build`.
- Regression: `test_propose_phase_plan_rejects_decorative_echo_success_test`.

## 2026-05-19 - Phase Memory Recall Ignored Manifest Warm Search And Missed Research Findings

- Run: memory audit during `phase_web_2d604b82`.
- Symptom: research wrote three accepted `palace.run` hot findings, but the later plan phase task context had an empty automatic `recall_bundle.hot`/`warm`. The planning agent recovered by manually calling `palace_search`, but Umbrella did not preload the hierarchy it had already collected.
- Cause: `build_phase_task` passed only `always_on` and `hot` rules into `MemPalace.recall`, never `warm_search` or a query seed. The plan manifest also looked for `research_summary` but not the actual `research_finding` tags produced by research.
- Fix: Umbrella now builds a phase recall query seed from phase metadata, active subtask, retry context, and `TASK_MAIN.md`; passes manifest `warm_search` into `MemPalace.recall`; renders warm context in phase prompts; and the plan manifest hot tags include `research_finding`.
- Regression: `test_recall_uses_manifest_warm_search_rules` and `test_build_phase_task_passes_manifest_warm_search_and_task_query_seed`.

## 2026-05-19 - `palace_add` Logs Hid Current-Run Scope Metadata

- Run: memory audit during `phase_web_2d604b82`.
- Symptom: `palace_add` tool rows showed store/tier/scope but omitted run id, phase, verified flag, and source path, making current-run versus stale-memory auditing harder from `tools.jsonl`.
- Cause: the tool compatibility payload was narrower than the metadata written into `MemPalace`.
- Fix: `palace_add` now returns phase, run id, verified, and source path alongside store/tier/scope.
- Regression: covered by memory/phase runner audit tests above; add a narrower payload assertion if this field regresses again.

## 2026-05-19 - GMAS Domain Signal Leaked Into Non-Agent Execute Subtasks

- Run: read-only execute/watcher audit after `phase_web_2d604b82`.
- Symptom: `multi_agent_gmas` was treated as a workspace-global execute prewrite concern, so setup/frontend/API work could receive GMAS prompt/policy pressure even when the active subtask did not implement agents, LLM behavior, bots, tools, judges, or GMAS memory.
- Cause: `build_phase_task` returned `gmas_prewrite_required=True` for execute whenever the workspace domain contained `multi_agent_gmas`; the execute prompt also said `workspace.toml` with `multi_agent_gmas = true` required GMAS before any patch.
- Fix: execute GMAS prewrite/policy loading is now based on the current pending subtask surface. Research/plan can still use the domain signal, but execute setup/frontend/API leaves are not polluted until an agent/LLM subtask becomes active. The execute prompt now states that `multi_agent_gmas` is a domain signal, not a blanket write gate.
- Regression: `test_build_phase_task_skips_gmas_gate_for_non_agent_execute_subtask`, `test_build_phase_task_injects_gmas_gate_for_agent_execute_subtask`, and updated prelude prompt assertions.

## 2026-05-19 - Charter Reads Did Not Count As Review File Reads

- Run: read-only phase-control audit after `phase_web_2d604b82`.
- Symptom: research review could call `read_workspace_charter`, receive `TASK_MAIN.md`/`workspace.toml`, and still be rejected for not reading `workspace.toml` before `verdict=ok`.
- Cause: `_read_file_paths_for_task` only counted `read_file` rows, even though `read_workspace_charter` returns concrete workspace file contents and is explicitly allowed in research review.
- Fix: research/review file-read accounting now parses `read_workspace_charter` results and counts the concrete files included in its `files` payload.
- Regression: `test_research_review_ok_counts_workspace_charter_as_file_read`.

## 2026-05-19 - Annotated Pseudo-Paths Were Accepted In Plan File Fields

- Run: read-only plan audit after `phase_web_2d604b82`.
- Symptom: a submitted plan could contain entries such as `frontend/package.json (deps added)` or `frontend/src/App.tsx (updated)` in file fields. Those are not real workspace paths but could still flow into review/execute cards.
- Cause: invalid whitespace paths normalized to an empty string and were then ignored by path validators instead of being rejected as path hygiene errors.
- Fix: phase-plan path policy now rejects annotated pseudo-paths in file fields and tells the planner to put status notes in `goal`, `description`, or `notes`.
- Regression: `test_propose_phase_plan_rejects_annotated_pseudo_paths`.

## 2026-05-19 - Execute Tests Invented APIs From Earlier Subtasks

- Run: `phase_web_3617b24b`.
- Symptom: `gmas-agents` generated `tests/test_agent_graph.py` with stale/speculative `GameState.create_initial`, `_add_city`, `_add_alliance`, and `_add_war` calls after `domain-state` had already created a different public API. The worker then looped through repeated contract migrations, hunk mismatches, and blocked completion attempts.
- Cause: execute instructions emphasized patch mechanics and test-weakening guards, but did not clearly require later-subtask tests to read and target the actual public APIs produced by earlier subtasks before writing or repairing tests.
- Fix: execute phase prompt now requires reading relevant existing source files before writing/repairing tests, targeting actual public APIs, avoiding invented helper classes/legacy methods, and preferring small source compatibility repairs before test-contract migration when that preserves intended behavior.
- Regression: `test_build_phase_task_loads_execute_existing_api_test_guidance`.

## 2026-05-19 - Windows Text Reads Broke Generated Docs Verification

- Run: `phase_web_fe6f7d1b`.
- Symptom: `architecture-docs` created Markdown docs and `tests/verify_docs.py`; the success test failed with repeated `UnicodeDecodeError` because generated tests used `Path.read_text()` without `encoding` on Windows while docs contained UTF-8/non-ASCII text and mojibake fragments.
- Cause: execute prompt did not tell workspace agents to use explicit UTF-8 in generated Python text readers, and workspace patch guards allowed new Python tests/scripts with locale-dependent `read_text()` calls.
- Fix: `apply_workspace_patch`/`update_workspace_seed` Python validation now blocks `Path.read_text()` without an explicit encoding, with a repair hint to use `encoding="utf-8"`; execute prompt also requires UTF-8-clean docs and explicit UTF-8 text reads.
- Regression: `test_apply_workspace_patch_rejects_python_read_text_without_encoding`, `test_apply_workspace_patch_allows_python_read_text_with_utf8_encoding`, and execute prompt assertion in `test_build_phase_task_loads_execute_existing_api_test_guidance`.

## 2026-05-19 - Review Guards Rejected Protective No-Mock/No-OpenAI Wording

- Run: `phase_web_fe6f7d1b`.
- Symptom: `research_review` rejected `with prohibition on mock/fake decisions` as if the reviewer requested mocks; `plan_review` rejected `No required OPENAI_API_KEY ... no gpt-* model defaults` as if the reviewer required OpenAI/provider-specific models.
- Cause: review-policy protective classifiers covered some `no/never/reject` forms but missed noun-form `prohibition`, and the provider-specific model guard had no protective local-claim skip.
- Fix: shared review policy now treats `prohibition` as protective for mock/fake/dry-run terms, adds `_review_provider_model_match_is_protective`, checks provider matches in local claim windows, and derives review error phase from `task_id` when UI phase label is generic `linear`.
- Regression: `test_submit_micro_review_allows_captured_prohibition_on_mock_fake_decisions`, `test_submit_micro_review_allows_captured_no_openai_no_gpt_review_checklist`, `test_submit_micro_review_allows_reject_gpt_default_revision`, and `test_submit_micro_review_provider_model_error_uses_task_phase_when_label_linear`.

## 2026-05-19 - Split `python -c` Mutation Fragments Created False Repair Evidence

- Run: `phase_web_fe6f7d1b`.
- Symptom: after watcher escalation, execute called `python -c` with code split across argv entries; the first fragment exited with code 0 while later fragments contained `write_text(...)`. Python only executed the first `-c` argument, but the run looked like a successful repair attempt to the model/operator.
- Cause: workspace mutation guard inspected only `cmd[2]` for Python `-c` mutations. It missed mutating fragments placed in later argv elements, which are invalid as executable code but still dangerous as false evidence.
- Fix: command guard now also scans the joined `python -c` argument tail for mutating file operations and blocks split mutation fragments before execution.
- Regression: `test_run_workspace_command_blocks_split_python_c_mutation_fragments`.

## 2026-05-19 - Phase `read_file` Rejected Line-Based Reads

- Run: `phase_web_468af5e0`.
- Symptom: execute called phase `read_file` with `line_start` while repairing `docs/architecture.md` after pytest failures and received `TOOL_ARG_ERROR: _read_file() got an unexpected keyword argument 'line_start'`.
- Cause: the phase-contract `read_file` compatibility alias exposed only `offset` even though the underlying `read_workspace_file` and tool guidance support `line_start`/`line_count`.
- Fix: pass `line_start` and `line_count` through the phase `read_file` handler and advertise both fields in the phase tool schema.
- Regression: `test_phase_read_file_alias_supports_line_start_from_execute_capture`.

## 2026-05-19 - Superseded Plan Memory Reached Execute Recall

- Run: `phase_web_468af5e0`.
- Symptom: after a plan-review loop-back, execute received both the corrected plan memory (`f3cc2789...`) and an older plan memory (`27503364...`) in `recall_bundle.hot`, even though the authoritative submitted/current artifacts had already moved on.
- Cause: `MemPalace.recall` filtered hot memories to the current run, but did not demote or suppress superseded current-run plan proposals/plan summaries after loop-back.
- Fix: post-plan recall phases (`plan_review`, `execute`, `final_review`, `verify`) now drop `phase_plan_proposal`/`umbrella_plan_candidate` hot entries and keep only the latest selected `phase_plan` hot entry for the same run, while preserving research findings.
- Regression: `test_recall_filters_superseded_plan_drafts_after_loopback`.

## 2026-05-19 - Completion Forcing Ignored Required Memory Writes

- Run: `phase_web_479edbff`.
- Symptom: research rounds 5/7/9/11/12 had native `tool_choice` forced to `submit_research_summary` before the phase had the manifest-required 3 accepted `palace_add` findings, causing repeated validator rejections instead of steering the agent toward the missing memory writes.
- Cause: Ouroboros loop completion forcing only received `required_calls` from Umbrella's phase manifest. `min_palace_writes` / `required_palace_writes` were rendered in the prompt and checked later by validators/runner, but were not part of the native `tool_choice` readiness gate.
- Fix: Umbrella now passes manifest completion prerequisites in `tool_filter.completion_prerequisites`; the Ouroboros loop checks accepted prerequisite memory writes before forcing a completion tool, and forces/nudges the prerequisite tool such as `palace_add` while the memory floor is still unsatisfied.
- Regression: `test_required_phase_completion_nudge_waits_for_palace_prerequisite`, `test_required_phase_completion_nudge_forces_submit_after_palace_prerequisite`, and `test_build_phase_task_passes_completion_prerequisites_to_loop`.

## 2026-05-20 - Active Success-Test Contract Migration Escaped Through Plan Mutation

- Run: `phase_web_d94824b2`.
- Symptom: after `project-setup-and-domain-state` failed `python -m pytest tests/test_game_state.py -q` three times and watcher advised repairing implementation/test contract mismatch, execute called `mutate_phase_plan` with two patches for the same subtask and marked `tests/test_game_state.py` as `contract_migration_files`. The reasons were API preference/clean-architecture mismatch and line-ending/import-failure claims, not an internally contradictory generated test. `test_weakening_guard` later blocked an attempted overwrite that removed many tests, but the phase plan had already recorded a misleading test-contract migration.
- Cause: `mutate_phase_plan` applied subtask patches without pre-validating duplicate IDs and treated any `contract_migration_reason` as enough to unlock active success-test edits after failures.
- Fix: `mutate_phase_plan` now validates subtask patches before mutation, rejects duplicate patch entries for one subtask, and rejects active declared success-test contract migration after failures unless the reason proves a genuine test contradiction, typo, impossible assertion, or accepted-plan conflict. API preference, clean-architecture preference, line-ending/patch issues, and import failures must be repaired in implementation instead.
- Regression: `test_mutate_phase_plan_rejects_duplicate_subtask_patch_from_capture` and `test_mutate_phase_plan_rejects_active_success_test_api_preference_capture`.

## 2026-05-20 - Research Memory Without Kind Fell Out Of Canonical Recall

- Run: `phase_web_d94824b2`.
- Symptom: research saved a concrete GMAS finding with `palace_add` but without `kind/tags`; `submit_research_summary` cited its UUID, while later phase recall only loaded the tagged research findings. Manual `palace_search` still found a legacy drawer mirror, creating split provenance between summary citations, canonical MemPalace recall, and legacy search.
- Cause: compatibility `palace_add` defaulted omitted kind to `observation`, then added only `observation`/phase tags. Research manifest hot recall expects `research_finding`, `mcp_candidate`, or `skill_candidate`, so an accepted research finding could be saved but not promoted as phase evidence.
- Fix: research-phase `palace_add` now infers `kind=research_finding` for concrete default observations when no explicit evidence tag is provided, while keeping scratchpad/progress notes as `observation`. The JSON payload now includes canonical `kind` and `tags` for auditability.
- Regression: `test_palace_add_research_defaults_concrete_observation_to_research_finding` and `test_palace_add_research_progress_note_is_not_research_finding`.

## 2026-05-20 - Plan Accepted Failure-Masked Success Test

- Run: `phase_web_f0cee725`.
- Symptom: plan validation rejected several unsafe shapes, then accepted a submitted plan with `project-setup.success_test = "python -m pytest tests/test_pkg_imports.py -q || true"` and `localhost-deployment.success_test = "run_workspace_verify"`. Execute started from a contract that could pass even when pytest failed and that used a workspace-level gate as a per-subtask proof.
- Cause: success-test validation did not reject shell failure masking such as `|| true`, and the generic-tool guard still allowed bare `run_workspace_verify`/`run_unit_tests` for labels like deployment/final/localhost.
- Fix: both the phase contract validator and the Umbrella runner execution floor now reject unconditional success masks (`|| true`, `|| exit 0`, `|| :`) and reject bare `run_workspace_verify`/`run_unit_tests` for plan subtasks. A subtask must name a concrete local command, checked-in test, HTTP/browser proof, or explicit tool proof; workspace verify remains a control-plane/final verification action after concrete smoke/e2e evidence.
- Regression: `test_propose_phase_plan_rejects_captured_shell_masked_success_test`, `test_propose_phase_plan_rejects_bare_workspace_verify_for_deployment_subtask`, `test_latest_phase_plan_execution_floor_rejects_shell_masked_success_test`, and `test_latest_phase_plan_execution_floor_rejects_bare_verify_final_gate`.

## 2026-05-20 - Plan Accepted `cd src` Pytest Success Tests

- Run: `phase_web_921912db`.
- Symptom: plan accepted top-level `files_to_create` such as `tests/test_architecture.py`, but paired them with success tests like `cd src && python -m pytest tests/test_architecture.py -q`. That command would look for `src/tests/test_architecture.py`, not the planned workspace-level test, so Execute could start from an invalid proof contract.
- Cause: existing validators rejected tests planned under `src/` and direct `src/.../test_*.py` pytest targets, but missed the equivalent cwd shift where the command changes into `src` before invoking `pytest tests/...`.
- Fix: phase contract validation and the runner execution floor now reject `cd src && pytest...` / `cd src && python -m pytest...` success tests. Greenfield Python tests must be run from the workspace root with commands such as `python -m pytest tests/test_x.py -q`.
- Regression: `test_propose_phase_plan_rejects_captured_cd_src_pytest_success_test` and `test_latest_phase_plan_execution_floor_rejects_captured_cd_src_pytest`.

## 2026-05-20 - Plan Review Treated Protective No-Mock Notes As Hard Blockers

- Run: `phase_web_4a0129c3`.
- Symptom: `plan_review` said the submitted plan was structurally solid, but returned `revise` for package-script details, e2e scenario specificity, and dev-script shape. The notes also said the plan had `no mock/fake LLM fallbacks`; the review guard treated those protective words as a hard blocker and allowed the loop-back. The next planning retry drifted back into previously banned layouts such as `backend/src/...`, root `scripts/verify_*.py`, and `python -c` success checks.
- Cause: plan-review hard-blocker detection searched the full feedback text for words like `mock`, `fake`, and `fallback` without checking whether the local claim was protective/positive. That bypassed the existing nonblocking-detail guard.
- Fix: plan-review validation now ignores hard-blocker matches whose local claim is protective, env-alias wording, or no-mock/no-fallback enforcement. Executable plans should receive these implementation details as `verdict=ok` notes for execute/watcher unless a true missing/unsafe/unverifiable contract is named.
- Regression: `test_plan_review_rejects_captured_package_e2e_detail_revise_loop`.

## 2026-05-20 - Research Summary Accepted Cached Bot Decisions

- Run: `phase_web_14d924fc`.
- Symptom: research wrote three valid findings and submitted a summary whose performance section said to mitigate LLM latency with `caching stable, unchanging decisions`. Research review accepted the handoff after reading the artifact, so plan could inherit memory that permits cached bot decisions.
- Cause: shared LLM fallback guards rejected explicit fallback/cached-decision replacement language, and plan validation rejected `decision caching`, but research handoff validation did not reject direct `caching ... decisions` wording when it was framed as performance mitigation rather than fallback.
- Fix: added a shared handoff guard for cached decision/action/response/reasoning reuse in LLM/GMAS/bot contexts. It now applies to research summaries, research-review `ok` checks, and research/plan `palace_add`; plan validation also catches adjectival forms such as `caching stable, unchanging decisions`. Protective no-caching wording remains allowed.
- Regression: `test_submit_research_summary_rejects_captured_decision_caching_notes`, `test_palace_add_rejects_captured_llm_decision_caching`, and the updated `test_propose_phase_plan_rejects_captured_civilization_decision_caching`.

## 2026-05-20 - Discovery Tools Rejected Benign `intent` Metadata

- Run: `phase_web_65835290`.
- Symptom: research called `mcp_discover(..., intent=...)` twice and `web_search(..., intent=...)` once, producing `TOOL_ARG_ERROR` rows before recovering with narrower calls. Discovery still completed, but the phase wasted rounds and polluted the operator log.
- Cause: `deep_search` is intent-aware, and the model generalized that optional metadata to adjacent discovery/search tools. `mcp_discover` and `web_search` schemas and handlers did not accept the benign field.
- Fix: `mcp_discover` and `web_search` now accept optional `intent` metadata, echo it in JSON output for auditability, and keep provider/server behavior unchanged.
- Regression: `test_web_search_accepts_intent_metadata_from_capture` and the updated `test_mcp_discover_tool_uses_github_search`.

## 2026-05-20 - Palace Finding Claimed Failed `web_search` As Verified Source

- Run: `phase_web_65835290`.
- Symptom: research saved `finding-002-web-stack` with `source_id="web_search"` and `evidence_kind="verified_outcome"`, even though every `web_search` call in that task was `provider_unavailable` or `TOOL_ARG_ERROR`.
- Cause: `palace_add` stored source/evidence metadata but did not verify that a tool-named source had a successful tool row before allowing `verified_outcome`.
- Fix: `palace_add` now checks tool-named sources (`web_search`, `deep_search`, GitHub search/snippets, `mcp_discover`) when `evidence_kind=verified_outcome`. If the current task has no successful row for that source, the memory write is rejected; non-tool source paths and lower-confidence evidence kinds remain available.
- Regression: `test_palace_add_rejects_verified_web_search_source_without_success` and `test_palace_add_accepts_verified_mcp_source_after_success`.

## 2026-05-20 - GMAS Retrieval Rejected Benign `intent` Metadata

- Run: `phase_web_e4cde249`.
- Symptom: research called `search_gmas_knowledge(..., intent=...)` and received `TOOL_ARG_ERROR: unexpected keyword argument 'intent'` before retrying without the metadata. The run recovered, but the phase wasted a round and the operator log showed a tool-contract mismatch.
- Cause: `deep_search`, `web_search`, and `mcp_discover` now accept/echo intent metadata for auditability, but the GMAS retrieval tools still exposed a narrower schema even though phase prompts encourage intent-aware retrieval.
- Fix: `search_gmas_knowledge` and `get_gmas_context` now accept optional `intent` metadata, echo it in the returned JSON when present, and keep retrieval behavior unchanged. This keeps GMAS usage scoped to agent/LLM tasks while making the Umbrella tool contract consistent for any deep agent.
- Regression: `test_search_gmas_knowledge_accepts_intent_metadata_from_capture` and the updated `test_gmas_context_tool_schema_accepts_limit_alias_and_intent`.

## 2026-05-20 - Research Memory Counted Ledger/Architecture As Findings

- Runs: `phase_web_e4cde249`, reproduced in shape during `phase_web_5a090940`.
- Symptom: research could save or cite non-finding memory as an accepted finding. In one captured run, a progress/evidence ledger was stored as `kind=research_finding` and cited by the summary; in the next run, an `architecture` memory row was listed as a third accepted finding to satisfy the finding floor.
- Cause: research-summary accepted-id lookup treated nearly any successful `palace_add` row as a finding unless it was explicitly tagged as a small set of non-finding kinds. The save-side progress detector also missed phrases like `Research evidence ledger` and `Current finding attempts`.
- Fix: research `palace_add` now rejects explicit `research_finding` writes that are progress ledgers/status/finding-count notes, and research-summary accepted-id lookup excludes `architecture`, `phase_plan`, `research_summary`, progress ledgers, and status/scratchpad memory. Architecture remains a separate handoff via `architecture_id`; it no longer inflates `findings_ids`.
- Regression: `test_palace_add_rejects_explicit_research_finding_progress_ledger`, `test_submit_research_summary_rejects_captured_progress_ledger_finding`, and `test_submit_research_summary_does_not_count_architecture_as_finding`.

## 2026-05-20 - Plan Accepted Implicit E2E Pytest Target

- Run: `phase_web_e4cde249`.
- Symptom: a plan reached `submit_phase_plan` with `e2e-localhost-verify.success_test = "python -m pytest tests/test_e2e_simulation.py -q --localhost -k test_full_game"`, but no subtask declared `tests/test_e2e_simulation.py` in `files_to_create`, `files_to_change`, or `files_affected`. `plan_review` caught the issue later, causing another loop.
- Cause: phase-plan validation checked that success tests were executable, but not that e2e/localhost pytest proof files were part of the plan file contract.
- Fix: phase-plan validation now rejects e2e/localhost/browser/smoke pytest success tests whose target test file is neither already present in the workspace nor declared by a plan leaf. The repair path is to add the checked-in test file to the owning subtask.
- Regression: `test_propose_phase_plan_rejects_captured_e2e_pytest_target_not_declared` and `test_propose_phase_plan_accepts_e2e_pytest_target_when_declared`.

## 2026-05-20 - Protective Unsupported Model Alias Notes Were Blocked

- Run: `phase_web_92978867`.
- Symptom: research `palace_add` calls that warned not to use `OUROBOROS_LLM_MODEL` were rejected as if they proposed the unsupported alias.
- Cause: the LLM env contract guard matched the bare token globally and did not inspect the local claim for protective wording such as `not`, `do not use`, `unsupported`, or `instead use OUROBOROS_MODEL`.
- Fix: model-alias validation now checks each `OUROBOROS_LLM_MODEL` match in a local window and ignores protective claims. The same context-aware guard is mirrored in Runner execution-floor validation and workspace write validation for generated docs/code.
- Regression: `test_palace_add_accepts_protective_unsupported_model_alias_note`, `test_propose_phase_plan_accepts_protective_unsupported_model_alias_note`, `test_latest_phase_plan_execution_floor_accepts_protective_model_alias_note`, and `test_apply_workspace_patch_allows_protective_unsupported_model_alias_docs`.

## 2026-05-20 - Plan Accepted Unmanaged Localhost Curl Proof

- Run: `phase_web_92978867`.
- Symptom: accepted plan leaf `localhost-verification` used `curl -f http://127.0.0.1:8000/health && ...` before any declared server launcher or checked-in e2e harness could start the service.
- Cause: `curl -f` counted as an automated success test, but the validator did not distinguish managed HTTP/browser proof from probing a pre-existing localhost listener.
- Fix: phase-plan and Runner success-test validation now reject direct localhost HTTP shell probes unless the proof is a managed Umbrella HTTP gate or checked-in test/browser harness that owns service startup/teardown.
- Regression: `test_propose_phase_plan_rejects_unmanaged_localhost_curl_success_test` and `test_latest_phase_plan_execution_floor_rejects_unmanaged_localhost_curl`.

## 2026-05-20 - Frontend Test Command And Declared Path Could Diverge

- Run: `phase_web_92978867`.
- Symptom: accepted frontend leaves declared tests under workspace-level `tests/frontend/*.test.ts`, but success tests ran inside `frontend` with `cd frontend && npm test -- *.test.ts`.
- Cause: path validation checked Python e2e pytest targets but did not project frontend package cwd into JavaScript test target paths.
- Fix: phase-plan and Runner validation now resolve explicit JS/TS test file targets under `cd frontend`; a basename target must match a declared or existing `frontend/...` test file, and non-frontend matches are rejected with a repair message.
- Regression: `test_propose_phase_plan_rejects_frontend_test_declared_outside_frontend` and `test_latest_phase_plan_execution_floor_rejects_frontend_test_path_mismatch`.

## 2026-05-20 - Palace Search Did Not Resolve Canonical UUIDs First

- Run: `phase_web_92978867`.
- Symptom: review phases searched for accepted MemPalace UUIDs but `palace_search` returned semantic legacy drawer neighbors instead of the exact canonical `palace.run` entries.
- Cause: `palace_add` writes to canonical MemPalace and mirrors to legacy memory, but `get_umbrella_memory`/`palace_search` still started from the legacy backend and ideas JSONL without a by-id MemPalace path.
- Fix: added canonical `MemPalace.get(node_id)` and made `get_umbrella_memory` short-circuit UUID queries through exact canonical lookup. Missing UUIDs now return explicit `exact_lookup.missing_ids` instead of semantic neighbors.
- Regression: `test_palace_search_returns_canonical_mempalace_node_by_uuid` and `test_palace_search_uuid_miss_does_not_return_semantic_legacy_neighbors`.

## 2026-05-20 - Task Result Artifacts Can Store Empty-Response Warning As Completed

- Run: `phase_web_92978867`.
- Symptom: several `.memory/drive/task_results/*.json` files reported `status=completed` while `result` was `Model returned an empty response`, even though phase-control artifacts showed real accepted plan/review/execute state.
- Cause: pending investigation in task-result capture/orchestration. This can pollute retry context as if a transient model failure were a completed phase fact.
- Fix: pending. Candidate fix layer is Runner/Ouroboros task-result capture: empty-response warnings should be error/empty metadata or ignored when phase-control signals provide authoritative state.
- Regression: pending with captured task_result payloads.

## 2026-05-20 - Patch-Mismatch Recovery Did Not Force Watcher Review

- Run: `phase_web_92978867`.
- Symptom: execute hit repeated `patch_hunk_mismatch_replacement_required` guidance, but no `request_watcher_review` appeared before the run was stopped.
- Cause: pending investigation. The write guard suggests watcher/replacement recovery, but orchestration may still allow ordinary tool-loop continuation.
- Fix: pending. Candidate fix layer is watcher/tool policy: after the replacement-required sentinel, block unrelated writes/reruns until replacement or `request_watcher_review` happens.
- Regression: pending with captured tool-log rows.

## 2026-05-20 - Preflight Completion Was Forced Before Charter Read

- Run: `phase_web_baf6b5c1`.
- Symptom: preflight ran `env_check`, `palace_health`, `mcp_health`, and `skill_audit`, then the loop forced `submit_preflight_report` on round 5. The report was accepted as `blocked` because `read_workspace_charter` had not run, so the entire Umbrella phase plan failed before research.
- Cause: required preflight checks existed only in prompt text. `exit_criteria.required_calls` told Ouroboros that `submit_preflight_report` completed the phase, but Umbrella did not pass a machine-readable prior-check contract, and the loop could accept/force a completion tool before those checks were satisfied.
- Fix: added `exit_criteria.required_prior_calls` to phase manifests and schema, wired it into `build_phase_task` as `tool_filter.completion_prerequisites.required_tools`, and taught the Ouroboros loop to force missing prior tools before completion tools. A completion tool that returns `OK` is now still held in-phase if required prior tool calls are missing, and failed/blocked JSON check results no longer count as accepted prerequisites.
- Regression: `test_build_phase_task_passes_required_prior_calls_to_loop`, `test_required_phase_completion_nudge_waits_for_prior_tool_call`, `test_required_phase_completion_nudge_ignores_failed_prior_tool_call`, `test_accepted_completion_tool_waits_for_prior_tool_calls`, and `test_required_phase_completion_nudge_forces_submit_after_prior_tool_calls`.

## 2026-05-20 - GMAS Gate Treated Package Markers As Agent Implementation

- Run: `phase_web_d3db1ce5`.
- Symptom: execute blocked the first setup write with `reason=gmas_context_before_first_write` on active subtask `project-setup`, even though the leaf only initialized package directories, frontend entrypoint shell files, docs, and structure tests. The captured leaf declared `src/civgame/ai/__init__.py`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `tests/test_project_structure.py`, and docs under a setup title.
- Cause: GMAS subtask classification treated any `src/.../ai/...` path as real LLM/agent implementation. A package marker such as `src/civgame/ai/__init__.py` therefore overrode the setup-only classifier and caused both the worker prelude and write gate to demand GMAS context too early.
- Fix: GMAS scope detection now distinguishes project shell paths (`__init__.py`, `py.typed`, common frontend entrypoint shell files, and project-structure tests) from actual LLM/agent/GMAS implementation files. Setup leaves with only config/docs/package markers/entrypoint shell files no longer require GMAS, while leaves that create `agent_builder.py`, `game_tools.py`, LLM tests, bot logic, or similar implementation still do.
- Regression: expanded `test_gmas_context_gate_skips_setup_dependency_leaf` and `test_build_phase_task_skips_gmas_gate_for_setup_dependency_leaf` with the captured `project-setup` shape from `phase_web_d3db1ce5`.

## 2026-05-20 - Phase Plan Mutation Replaced File Scope Lists

- Run: `phase_web_d3db1ce5`.
- Symptom: after `project-setup` needed `frontend/tsconfig.node.json`, a `mutate_phase_plan` patch left `project-setup.files_to_create` containing only `frontend/tsconfig.node.json`, losing the original declared setup scope. That weakens active-subtask write-scope enforcement and makes later watcher evidence harder to audit.
- Cause: `mutate_phase_plan` treated list-valued subtask fields as replacement assignments. For phase-plan file-scope fields, the tool is used as a partial patch channel; replacing the whole list is too destructive unless an explicit replacement mode exists.
- Fix: `mutate_phase_plan` now merges `files_to_create`, `files_to_change`, and `files_affected` by default, preserving existing entries and appending new unique paths. Contract-migration file lists still remain explicit replacement metadata.
- Regression: `test_mutate_phase_plan_merges_file_scope_lists_from_captured_setup_patch`.

## 2026-05-20 - Completion Schema Hid The Real Success-Test Gate

- Run: `phase_web_d3db1ce5`.
- Symptom: execute attempted `mark_subtask_complete` after `python -m pytest tests/test_project_structure.py -q` failed, but the call was rejected at schema preflight because `evidence` was a string instead of an array. The stricter success-test completion gate did not get to return the more useful remediation message.
- Cause: the public tool schema only accepted `evidence: string[]`, while model calls often supply one evidence sentence. This made a shape error mask the real gate.
- Fix: `mark_subtask_complete` now accepts either a string or an array for `evidence`, normalizes internally to a list, and then applies the existing success-test gate. A failed declared success test is now rejected because no matching successful command evidence exists, not because of a JSON shape mismatch.
- Regression: `test_mark_subtask_complete_normalizes_string_evidence_before_success_gate`.

## 2026-05-20 - Explicit Watcher Review Was Not Recorded On First Real Failure

- Run: `phase_web_d3db1ce5`.
- Symptom: after a real declared success-test failure, `request_watcher_review` could return `status=review_not_required` simply because the repeated-failure threshold had not been reached. That made the watcher less useful for diagnosing generated-test contract contradictions and kept subtask-scoped memory thin.
- Cause: the watcher handler used the retry threshold as both an automatic-escalation gate and an explicit-review recording gate.
- Fix: explicit `request_watcher_review` calls now record a watcher review after any real latest failure unless a specialized patch-mismatch guidance path is already being returned. Threshold behavior still controls repeated-failure escalation language.
- Regression: `test_request_watcher_review_records_first_explicit_declared_failure`.

## 2026-05-20 - GMAS Context Tool Rejected `slug` Metadata

- Run: `phase_web_b46ac05c`.
- Symptom: research called `get_gmas_context(query=..., max_results=5, slug="gmas-overview")` and got `TOOL_ARG_ERROR: unexpected keyword argument 'slug'`, even though the slug was harmless audit metadata and a prior `search_gmas_knowledge` call had already succeeded.
- Cause: GMAS retrieval tools had been hardened for `limit` and `intent` aliases, but not for common metadata labels such as `slug`. The handler accepted only a narrow parameter set while the LLM naturally added a retrieval label.
- Fix: `get_gmas_context` and `search_gmas_knowledge` now accept optional `slug` metadata, preserve it in the returned payload for auditability, and expose it in the tool schema. Retrieval behavior is still driven only by `query`, `max_results`, and context-size parameters.
- Regression: `test_get_gmas_context_accepts_slug_metadata_from_capture` and updated `test_gmas_context_tool_schema_accepts_limit_intent_and_slug`.

## 2026-05-20 - Research Finding Filter Rejected Incidental Placeholder/TBD Words

- Run: `phase_web_92a8e0d4`.
- Symptom: `palace_add` saved concrete `kind=research_finding` rows, but `submit_research_summary` rejected their ids as not accepted. The rejected findings were real workspace/web architecture observations; they merely mentioned `workspace.toml contains placeholder meta configuration` and `API shape TBD`.
- Cause: the accepted-finding lookup reused the broad research-summary placeholder regex against every finding body. That regex is appropriate for blocking incomplete handoff notes, but too broad for concrete findings that mention a placeholder file or a design detail still to be planned.
- Fix: finding lookup now uses a narrower finding-level placeholder/progress detector. It still rejects scratchpads, progress ledgers, `Research in progress`, and `1/3 findings` rows, but allows concrete `research_finding` entries with incidental `placeholder` or `TBD` wording.
- Regression: `test_submit_research_summary_counts_incidental_placeholder_words_in_findings`.

## 2026-05-20 - Execute Wrote Future-Subtask Files During Setup

- Run: `phase_web_9b94464f`.
- Symptom: execute started with active leaf `setup-project`, but before running `tests/test_project_setup.py` or marking that leaf complete, it wrote future/undeclared files such as `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/index.css`, `frontend/index.html`, and `README.md`. This blurred phase-plan ownership and made later watcher/review evidence harder to trust.
- Cause: execute prompt said to work one subtask at a time, but `apply_workspace_patch` did not enforce the active leaf's declared `files_to_create` / `files_to_change` / `files_affected`. Phase-run execute is driven as one deep-agent task, so without a tool-level scope contract the model can batch future leaves opportunistically.
- Fix: `apply_workspace_patch` now reads the current execute subtask from `.memory/drive/state/phase_plan.json` and blocks writes outside that subtask's declared file scope. If the blocked path belongs to a later subtask, the response names that future owner; if the active subtask genuinely needs the path, the required repair is `mutate_phase_plan` before writing. The execute prompt now states the same write-scope contract.
- Regression: `test_apply_workspace_patch_blocks_future_subtask_file_before_current_complete` and `test_apply_workspace_patch_allows_active_subtask_declared_file`.

## Current Checks

- 2026-05-20 live run `phase_web_ce127a9e`: Execute prelude injected `get_gmas_context` for `project-setup` even though that leaf only created package/env/README metadata and merely mentioned `frontier-ai-gmas` as a dependency for later agent leaves. Fix: GMAS pre-write gating now uses the same active-subtask scope helper in worker and write tools, and setup/config-only leaves do not require the hard GMAS prelude. GMAS remains required for leaves that write LLM/agent/GMAS/bot implementation or tests.
- 2026-05-20 live run `phase_web_ce127a9e`: Plan/review accepted `project-setup.success_test = cd frontend && npm run build` while the leaf did not declare `frontend/src/*` or `frontend/index.html`; execute then failed on missing frontend inputs and had to mutate the phase plan. Fix: phase-plan and Runner validation now reject frontend build success tests until the needed entrypoint/source files are declared in the same or an earlier leaf.
- 2026-05-20 regression while fixing GMAS scope: older tool logs with `get_gmas_context` result preview `{}` stopped satisfying the generic no-active-subtask gate. Fix: compatibility restored for empty non-error context rows, while active-subtask writes still require scoped GMAS context evidence.
- Latest GMAS setup-scope and frontend-build-order fix: focused regressions passed; affected phase-contract/runner/workspace-command/path-normalisation/repo-write suite `505 passed`; compile clean for `workspace_gmas.py`, `worker.py`, `phase_contract_success.py`, and `runner.py`.
- Latest execute subtask write-scope fix: focused `2 passed`; affected workspace terminal suite `114 passed`; combined terminal/loop/runner/manifest suite `361 passed`; compile clean for `workspace_ops.py`, `loop.py`, `worker.py`, `base.py`, and `loader.py`.
- Latest preflight required-prior-call fix: affected loop/phase-runner/manifest suite `247 passed`; compile clean for `ouroboros/ouroboros/loop.py`, `umbrella/orchestrator/worker.py`, `umbrella/phases/base.py`, and `umbrella/phases/loader.py`.
- Latest canonical UUID palace-search fix: focused `2 passed`; affected contract/runner suite `322 passed`; memory/context suite `27 passed`; compile clean for `memory.py` and `palace/facade.py`.
- Latest protective-alias/localhost/frontend-path contract fix: focused regressions `10 passed`; affected phase-contract/control suite `304 passed`; runner suite `139 passed`; workspace terminal suite `112 passed`; compile clean for `phase_contract_success.py`, `runner.py`, and `workspace_gmas.py`.
- Latest research-memory/e2e-target validator fix: focused `9 passed`; affected phase-contract/control-artifact suite `300 passed`; phase-runner suite `136 passed`; compile clean for `phase_contract_handlers.py`, `phase_control_research.py`, `phase_contract_success.py`, and `phase_contract_paths.py`.
- Latest GMAS retrieval `intent` metadata fix: focused `2 passed`; affected workspace/terminal tool suite `111 passed`; compile clean for `workspace_gmas.py` and `ouroboros_entries.py`.
- Latest verified source/evidence memory fix: focused `2 passed`; affected phase-contract suite `174 passed`; combined contract/runner/control/runtime/MCP suite `454 passed`; compile clean for `phase_contract_handlers.py`.
- Latest discovery `intent` metadata fix: focused `2 passed`; affected runtime/MCP suite `23 passed`; combined contract/runner/control/runtime/MCP suite `454 passed`; compile clean for `search.py` and `discovery.py`.
- Latest cached-decision research handoff fix: focused `3 passed`; affected control-artifact/contract suites `121 passed`, `172 passed`; combined contract/runner/control suite `429 passed`; compile clean for `phase_control_base.py`, `phase_control_research.py`, `phase_contract_handlers.py`, and `phase_contract_common.py`.
- Latest plan-review protective-note blocker fix: focused `1 passed`; protective/blocking control checks `3 passed`; affected contract/runner/control-artifact suite `427 passed`; compile clean for `phase_control_review.py`.
- Latest `cd src` pytest success-test fix: focused `2 passed`; affected contract/runner/control-artifact suites `171 passed`, `136 passed`, `119 passed`; compile clean for `phase_contract_common.py`, `phase_contract_success.py`, and `runner.py`.
- Latest plan success-test floor fix: focused `4 passed`; affected contract/runner/control-artifact suites `170 passed`, `135 passed`, `119 passed`; compile clean for `phase_contract_common.py`, `phase_contract_success.py`, and `runner.py`.
- Latest research memory kind inference: focused `3 passed`; affected phase-contract/control/runner/terminal suites `287 passed`, `243 passed`; compile clean for `phase_contract_handlers.py` and `phase_control_actions.py`.
- Latest active success-test contract-migration guard: focused `3 passed`; affected phase-control/terminal/runner/contract suites `229 passed`, `299 passed`; compile clean for `umbrella/deep_agent_tools/phase_control_actions.py`.
- Latest completion-prerequisite forcing regressions: focused `3 passed`; affected loop/phase-runner suites `77 passed`, `133 passed`; compile clean for `ouroboros/ouroboros/loop.py` and `umbrella/orchestrator/worker.py`.
- Latest focused review/UTF-8/split-`python -c` regressions: `8 passed`.
- Latest phase `read_file` + superseded plan recall regressions: focused `3 passed`; affected palace/phase-contract suite `176 passed`; GMAS scope/prelude checks `3 passed`.
- Latest affected review/terminal/runner suite after the UTF-8 and review-guard fixes: `353 passed`.
- Latest focused GMAS/prompt-loading/degradation regressions after entries above: `6 passed`.
- Latest focused research finding-floor regressions: `3 passed`.
- Latest affected runner/control/contract suite after GMAS execute prelude and mock-LLM memory guard: `418 passed`.
- Latest affected phase runner/manifest/control artifact suite: `256 passed`.
- Latest affected phase-contract/prior-knowledge suite: `170 passed`.
- Latest affected memory/manifest/phase-runner/contract suite after workspace-palace hierarchy/default-path fixes: `343 passed`.
- Latest affected suites after submitted-plan, evidence freshness, and memory-tier fixes: `304 passed`, `204 passed`, `109 passed`; focused memory/durable checks: `25 passed`.
- Latest live-run bug before rerun: `github_project_search(max_results=...)` schema mismatch fixed; `test_github_project_search_accepts_max_results_alias` and `test_github_discovery.py` passed (`9 passed`).
- Latest phase-control signal phase fix: `test_phase_control_artifacts.py` passed (`110 passed`).
- Latest echo-success-sentinel and manifest memory recall fix: focused regressions passed (`3 passed`); affected contract/runner/palace suite passed (`303 passed`).
- Latest GMAS subtask-scope, charter-read, pseudo-path, and execute API-guidance fixes: focused regressions passed (`6 passed`); affected phase contract/control/runner suite passed (`408 passed`).
- Latest full pytest attempt: `1700 passed`, `98 skipped`, `1 failed` on pre-existing `test_no_oversized_modules` because `ouroboros/ouroboros/tools/git.py` has 1053 lines over the 1000-line smoke threshold; this calibration change did not touch `git.py`.
- Current clean Web UI run after the echo-success-sentinel and memory recall fixes reached execute and exposed the `gmas-agents` API-invention loop; stopped for the fixes above. Next step is clean Web UI rerun.
- Memory expectations: clean start should show empty palace, research should write accepted findings to `palace.run`, plan mutations/completions to `palace.subtask`, verify reports to `palace.durable`, and review phases must read fresh submitted/current `.memory/drive/state/*_latest.json` artifacts before `ok`.

## 2026-05-20 - Env-Prefixed Shell Script Success Test Was Accepted

- Run: `phase_web_6809bbeb`.
- Symptom: `propose_phase_plan` accepted `RUN_TESTS_AUTO=true RUN_E2E_AUTO=true scripts/verify.sh` as a subtask `success_test`; plan review later complained about a different blocker, but the validator should have rejected the proof path before review.
- Risk: a greenfield Windows/Umbrella workspace can receive Unix-style env-prefix plus `.sh` verification, which is not a reliable managed workspace proof and can push execute into brittle shell/process-control behavior.
- Cause: the non-portable shell detector caught `./script.sh`, `bash -c`, and process-control patterns, but missed bare script paths after inline env assignments.
- Fix: `_NON_PORTABLE_SHELL_RE` now catches Unix env-prefix commands and bare `.sh` paths. Diagnostic priority was adjusted so captured “command succeeds/fails” prose still gets the clearer descriptive-outcome message, while `bash -c`, file-existence shell probes, and `Start-Job` remain classified as non-portable shell/process-control.
- Regression: `test_propose_phase_plan_rejects_env_prefixed_sh_script_success_test`, plus focused diagnostic coverage for captured outcome prose, `bash -c`, and `Start-Job`.

## 2026-05-20 - Plan Accepted `/dev/null || build` Success Test

- Run: `phase_web_b34a047f`.
- Symptom: after several healthy plan rejections, `propose_phase_plan` accepted `project-setup-docs.success_test = python -m pytest -c /dev/null -m 'not (integration or e2e)' -q 2>/dev/null || (cd frontend && npm run build)`, then `submit_phase_plan` selected it.
- Risk: execute could start from a plan whose first proof is Unix-specific, suppresses pytest stderr, and lets a frontend build hide a failed or empty pytest proof.
- Cause: success-test policy rejected `|| true` and several process-control forms, but not arbitrary `||` alternate branches, `/dev/null`, or shell redirection. Plan-review blocker classification also treated “malformed Windows-incompatible success_test” like a nonblocking implementation detail.
- Fix: success-test policy now rejects arbitrary `||` success branches, `/dev/null`, and shell redirection, while keeping process-control diagnostics for `ps/grep/pkill/bash/Start-Job`. Plan-review hard-blocker detection now recognizes malformed/invalid/non-portable/Windows-incompatible `success_test` feedback as a real revise blocker.
- Regression: `test_propose_phase_plan_rejects_captured_devnull_or_build_success_test` and `test_plan_review_revise_allows_malformed_success_test_blocker`.

## 2026-05-20 - Research Progress Scratchpad Was Saved As `research_finding`

- Run: `phase_web_e8afe5ca`.
- Symptom: `palace_add` saved three `kind=research_finding` memories whose content was the same progress note: “I need to continue researching and make at least 3 palace_add calls... Let me explore...”. They were `verified=false`, `source_path=ouros`, and later `submit_research_summary` tried to cite them as findings.
- Risk: plan/review/execute can receive scratchpad/progress text as authoritative hot research memory, weakening the hierarchical memory signal and encouraging later phases to build from non-findings.
- Cause: the research memory progress detector caught “research progress”, “scratchpad”, and “1/3 findings”, but not common self-instruction phrases like “continue researching”, “let me explore”, or “make at least N palace_add calls”. The summary validator had a similar narrower detector.
- Fix: both `palace_add` inference/guarding and `submit_research_summary` finding counting now treat those self-instruction phrases as progress notes. They are stored as ordinary observations if not explicitly marked, and explicit `research_finding` progress notes are rejected.
- Regression: `test_palace_add_research_continue_note_is_not_research_finding` and `test_submit_research_summary_rejects_captured_continue_note_finding`.

## 2026-05-20 - Explicit `verified=false` Findings Counted As Trusted Research

- Run: `phase_web_6c2e6608`.
- Symptom: `submit_research_summary` accepted three cited `palace_add` rows with `verified=false`, including a `hypothesis` row and thin observation rows. Then `palace_search(include_unverified=false)` exact-id lookup returned those canonical MemPalace nodes under trusted `palace_memory`.
- Risk: downstream review/plan/execute can treat unverified leads as accepted architecture evidence, even though the memory payload itself says it is not verified.
- Cause: finding counting ignored explicit `verified=false`, and `_is_unverified_memory` checked tags/evidence_kind but not the canonical MemPalace `verified` metadata. Exact UUID lookup therefore bypassed the semantic unverified split.
- Fix: summary finding counting now rejects rows whose result payload explicitly has `verified=false`; exact canonical lookup treats `verified=false` metadata as unverified unless `include_unverified=true`. Research `palace_add` also downcasts `hypothesis`/candidate/malformed evidence metadata to ordinary observation unless explicitly rejected.
- Regression: `test_submit_research_summary_rejects_explicit_unverified_finding`, `test_palace_search_excludes_unverified_canonical_uuid_by_default`, and `test_palace_add_research_hypothesis_is_not_research_finding`.

## 2026-05-20 - Accepted Research Findings Were Saved As Unverified

- Run: `phase_web_20eb1a6a`.
- Symptom: `palace_add(kind=research_finding)` accepted concrete research rows and returned ids, but its payload still said `verified=false`; the next `submit_research_summary` correctly rejected those ids with `Known ids: none`.
- Risk: research can loop forever after the previous unverified-memory hardening, because the write contract accepts a finding while the handoff contract refuses to trust it.
- Cause: `palace_add` used `verified=evidence_kind == "verified_outcome"` for every memory. Research findings are a phase-level evidence type: if the research tool accepts a concrete `research_finding`, it must be trusted for the current research handoff unless the caller explicitly marks it as hypothesis/candidate/unverified.
- Fix: accepted research-phase `research_finding` canonical memories now save and return `verified=true`. Explicit `verified=false` on a requested research finding is rejected; hypothesis/candidate/unverified evidence still downcasts or rejects, preserving the previous safety boundary.
- Regression: `test_palace_add_rejects_explicit_verified_false_research_finding`, strengthened `test_palace_add_accepts_optional_metadata` and `test_palace_add_research_defaults_concrete_observation_to_research_finding`, plus `test_submit_research_summary_accepts_captured_verified_research_finding`.

## 2026-05-20 - Plan Repair Oscillated Between Too Many Leaves And Broad Leaves

- Run: `phase_web_ac0780b9`.
- Symptom: `propose_phase_plan` adapted from invalid shell/path issues, but then looped through 8 plan proposals alternating between 17-25 executable leaves and broad 5-8 file leaves such as `frontend-core` or `gmas-topology-tools-router`.
- Risk: the system can burn many LLM rounds in plan without reaching execute even though the validator feedback is correct. This is a control-plane repair-delivery issue, not a workspace-specific implementation problem.
- Cause: the rejection hint said “8-16 leaves” and “2-4 files” but did not explain how to resolve the tension for large full-stack/LLM apps. The model responded by either splitting every screen/hook into too many leaves or merging them back into broad domains.
- Fix: phase-plan tool feedback now includes a tagged `[PHASE_PLAN_REPAIR_SCAFFOLD]` for large full-stack/LLM apps: aim for 12-14 vertical leaves, use universal slice types, avoid re-merging rejected broad leaves, and move future/optional files to later leaves or goal checklists instead of current `files_to_create`.
- Regression: strengthened `test_propose_phase_plan_rejects_over_granular_greenfield_plan` and `test_propose_phase_plan_rejects_captured_broad_leaf_before_submit` to assert the tagged scaffold.

## 2026-05-20 - Plan Accepted Python Verifier Under Docs, Then Mutate Bypassed Success-Test Policy

- Run: `phase_web_11159129`.
- Symptom: plan accepted `docs/verification_script.py` with `success_test = python docs/verification_script.py`. Execute correctly blocked the first write because `docs/` is documentation-only, then the agent tried to migrate to `scripts/verify_architecture.py` and finally called `mutate_phase_plan` with `success_test = python tests/test_architecture_verification.py -q`, which ordinary plan validation would reject.
- Risk: plan and execute could disagree about workspace layout, and mid-execution plan mutations could smuggle invalid proof commands past the plan contract. That turns recovery into a loop: write guard blocks, mutate accepts a bad replacement, write scope still disagrees.
- Cause: greenfield layout policy allowed `.py` under `docs/`, while `mutate_phase_plan` applied subtask-card edits directly and wrote `phase_plan.json` without rerunning the shared phase-plan policy checks used by `propose_phase_plan`/`submit_phase_plan`.
- Fix: greenfield phase plans now reject Python files under `docs/` and direct the model to put pytest verification under `tests/` or reusable code under `src/<package>/...`. `mutate_phase_plan` now validates the mutated plan with the same `_phase_plan_policy_issues` contract before writing or emitting control signals.
- Regression: `test_propose_phase_plan_rejects_captured_docs_python_verifier` and `test_mutate_phase_plan_rejects_captured_direct_python_pytest_command`.

## 2026-05-20 - Legacy Palace Search Surfaced Downcast Research Observations As Trusted Memory

- Run: `phase_web_19764f9b`.
- Symptom: research saved a caller-tagged `research_finding` as `kind=observation`, `verified=false`, and `submit_research_summary` correctly refused to cite it. In the next plan phase, `palace_search(include_unverified=false)` still returned the legacy drawer for that same observation under trusted `palace_memory`.
- Risk: later phases can build from unverified/downcast research memory even when canonical MemPalace and research handoff gates know it is not an accepted finding. This weakens the hierarchy: trusted hot memory can contain rejected leads.
- Cause: `palace_add` wrote canonical MemPalace with `verified=false`, but the legacy mirror dropped canonical provenance such as `verified`, `store`, `tier`, `scope`, `phase`, `run_id`, and `canonical_id`. Legacy metadata also stores values as strings, while `_is_unverified_memory` treated string `"False"` as truthy. Downcast observations also kept the misleading `research_finding` tag.
- Fix: legacy mirrors now receive canonical provenance metadata, string `verified=false` is parsed as false by the memory filter, and research downcasts remove the `research_finding` tag before storage. Explicit `palace_search` therefore keeps those rows out of trusted `palace_memory` unless `include_unverified=true`.
- Regression: `test_palace_search_excludes_downcast_research_observation_legacy_hit` and `test_memory_filter_detects_unverified_tags_and_rooms`.

## 2026-05-20 - Full `read_file` Metadata Lost Line Counts

- Run: `phase_web_98172342`.
- Symptom: multiple full `read_file` calls returned complete `content`, but reported `line_count=0`, `line_end=0`, `total_lines=null`, and `line_range_complete=false`.
- Risk: watcher/review phases can still inspect content, but audit signals make a full read look like an empty or incomplete line range. That weakens memory/review evidence, especially when later gates need to prove a file was read with enough context.
- Cause: `umbrella/deep_agent_tools/workspace_read.py` only filled line metadata when `line_start > 0`; full-file reads fell through `read_file_preview` and kept line metadata empty.
- Fix: normal text full-file reads now compute `total_lines`, observed `line_count`, `line_end`, `line_range_complete`, and `has_more_lines_after` directly from the decoded file text. Binary and document previews still avoid fake line counts.
- Regression: `test_read_workspace_file_full_text_reports_line_metadata`.

## 2026-05-20 - Unsubmitted Plan Draft Polluted Execute Hot Memory

- Run: `phase_web_98172342`.
- Symptom: after `propose_phase_plan` was repeatedly rejected and then corrected, execute received a `recall_bundle.hot` item from an earlier `palace_add(kind=phase_plan)` call. That memory contained a stale rejected draft with broad subtasks, invalid success tests, and old subtask ids, while the authoritative `phase_plan.json` and submitted artifact were already correct.
- Cause: direct `palace_add` from the plan phase could tag an arbitrary note as `phase_plan`, and post-plan recall treated generic `phase_plan` hot memory as selected plan memory. The submitted plan lived in `.memory/drive/state/phase_plan_submitted_latest.json`, but was not mirrored with a distinct selected-plan tag.
- Fix: `palace_add` now rejects executable `phase_plan` memory from the plan phase and tells the agent to use `propose_phase_plan` plus `submit_phase_plan`. Submitted plans are mirrored to hot memory with `phase_plan_submitted` / `umbrella_plan_selected`, and MemPalace post-plan recall drops generic unsubmitted `phase_plan` drafts.
- Regression: `test_palace_add_rejects_direct_plan_phase_plan_memory`, `test_submit_phase_plan_persists_selected_plan_not_latest`, `test_recall_filters_superseded_plan_drafts_after_loopback`, and `test_recall_drops_unsubmitted_plan_memory_after_plan_phase`.

## 2026-05-20 - Contract-Migration Evidence Was Revalidated As Project Content

- Run: `phase_web_ba3413d2`.
- Symptom: execute correctly blocked edits to `tests/test_docs_content.py` after repeated failures, watcher recorded that the generated test was self-contradictory, and the worker tried `mutate_phase_plan` with `contract_migration_reason`. The first weak reason was rejected, but stronger reasons were then rejected because they quoted `OUROBOROS_LLM_MODEL` as the bad string inside the broken test.
- Risk: a real watcher-proven test-contract defect can deadlock execute. The system asks for phase-plan mutation before changing the generated test, but the phase-plan policy treats the mutation audit note as if it were future generated docs/code.
- Cause: `_mutate_phase_plan` reran full phase-plan policy on the stored plan after adding `contract_migration_reason`. That policy should validate executable plan content and success tests, not provenance/audit fields. The active-test evidence detector also missed common wording such as "self-contradictory" and "violates its own".
- Fix: mutate policy validation now strips mutation audit/provenance fields (`contract_migration_reason`, migration files, and `edits_log`) before applying content policy checks, while still validating changed success tests and file scope. Evidence wording now recognizes self-contradictory/generated-test-sample phrasing.
- Regression: `test_mutate_phase_plan_accepts_watcher_proven_test_contract_contradiction`.

## 2026-05-20 - Mutate Recovery Was Blocked By Completed Subtasks Outside Patch Scope

- Run: found by regression while fixing `phase_web_ba3413d2`.
- Symptom: `mutate_phase_plan` recovery tests failed because the shared plan policy rejected a completed historical subtask with no `success_test`, even though the mutation only targeted the active subtask after watcher-proven failure.
- Risk: Umbrella can strand execute recovery on stale or already-closed plan-card debt unrelated to the current patch. That is especially bad in long runs where phase memory and plan cards evolve hierarchically over many subtasks.
- Cause: after the previous hardening, `_mutate_phase_plan` validated the whole persisted `phase_plan.json` exactly like a fresh proposed plan. Fresh proposals should require every leaf to be executable, but execute-time mutation must validate the changed content and active/pending plan without treating closed cards as new proposal material.
- Fix: mutate policy validation now builds a content-validation payload that strips audit fields and ignores completed/skipped subtasks outside the touched subtask ids, while still checking the touched subtask and active/pending future work.
- Regression: existing `test_apply_workspace_patch_allows_generated_test_contract_migration_after_plan_mutation` and `test_apply_workspace_patch_contract_migration_allows_exact_update_after_repeated_hunk_mismatches` now cover the boundary.

## 2026-05-20 - Execute-Time Mutation Revalidated Future Plan Cards And Runtime Overlay

- Run: `phase_web_110d7ea6`.
- Symptom: `map-engine` correctly detected an internally contradictory generated `tests/test_map.py` hex-distance assertion, but `mutate_phase_plan` rejected the contract-migration note because full plan policy also scanned stale execute overlay text from the previous `domain-models` retry, future `frontend/*` cards, and a future `tests/test_smoke.py` file listed as already changed.
- Risk: a long execute run can deadlock whenever the workspace has evolved since plan submission. Future accepted cards and stale runtime retry context should not invalidate a narrow active-subtask recovery mutation.
- Cause: execute-time mutation reused fresh-proposal validation over the whole stored plan, including runtime/audit fields. That conflated two contracts: submit/propose must validate the entire plan, while mutate must validate changed executable content without treating historical/future cards as new proposals.
- Fix: `_phase_plan_policy_payload` now strips runtime/audit fields such as `overlay` and `completion`, and for subtask-card mutations validates only the touched card plus the changed file/success-test scope instead of the card's entire historical file list. Broad `nodes`/`version` mutations still get full validation because there is no narrow touched scope.
- Regression: `test_mutate_phase_plan_ignores_future_cards_and_runtime_overlay_from_capture`.

## 2026-05-20 - Docs-Python Diagnostic Disappeared After Workspace Had Code

- Run: found by affected phase-contract suite after the execute-time mutation fix.
- Symptom: `test_propose_phase_plan_rejects_captured_docs_python_verifier` still rejected the bad plan, but the returned policy message no longer mentioned `docs/verification_script.py`; a parallel-root diagnostic won first because the docs-Python check returned early when the workspace looked non-empty.
- Risk: plan-phase repair feedback becomes less actionable in long runs. The model can fix the wrong thing first and keep `docs/*.py` verifier debt alive.
- Cause: the “Python files do not belong under docs” rule lived inside the greenfield layout branch and was skipped whenever implementation roots were present.
- Fix: docs-Python path detection now runs as a universal phase-plan path policy before the greenfield-only layout rules. Existing-code plans can still be repaired/refactored, but `docs/` remains Markdown/spec documentation rather than Python verifier storage.
- Regression: existing `test_propose_phase_plan_rejects_captured_docs_python_verifier` protects the message.

## 2026-05-20 - Alias Warning Context Was Not Accepted As Test-Contract Evidence

- Run: `phase_web_1f254e11`.
- Symptom: `tests/test_docs_readable.py` failed because it asserted `OUROBOROS_LLM_MODEL` must not appear anywhere, while `docs/llm_runtime.md` mentioned it only in a warning: `NOT OUROBOROS_LLM_MODEL`. `mutate_phase_plan` rejected the contract-migration reason, so execute tried to rewrite docs around an over-strict test instead of correcting the generated test contract.
- Risk: LLM/runtime docs can be made less clear just to satisfy a brittle generated test, and execute can drift into patch churn even though the intended behavior is protective documentation.
- Cause: the active success-test migration evidence detector recognized contradictions and impossible assertions, but not the common “forbidden string appears only in warning/negative context” failure shape.
- Fix: active success-test contract migration evidence now recognizes warning-context wording such as “correctly warns”, “warning context(s)”, and “not to use that alias”.
- Regression: `test_mutate_phase_plan_accepts_wrong_alias_warning_context_capture`.

## 2026-05-20 - Replacement Patch Feedback Did Not Explain `+*** End Patch`

- Run: `phase_web_1f254e11`.
- Symptom: after repeated hunk mismatches, Umbrella required a paired Delete/Add replacement. The worker then emitted large replacement patches with the final `*** End Patch` marker prefixed as file content (`+*** End Patch`), causing repeated `patch_parse_error: patch must end with *** End Patch`.
- Risk: recovery feedback can send the model into a malformed-patch loop even after the control plane correctly escalates away from fragile update hunks.
- Cause: parse-error feedback only repeated the generic envelope rule. It did not detect that the terminator was present but incorrectly prefixed as an added line.
- Fix: `apply_workspace_patch` parse errors now detect `+*** End Patch` and explicitly say the final terminator is a control line with no leading `+`; only replacement file content lines are prefixed.
- Regression: `test_apply_workspace_patch_parse_error_explains_prefixed_end_marker`.

## 2026-05-20 - Active Greenfield Leaf Was Treated As Existing-Code Rebuild During Mutation

- Run: `phase_web_0607bdc8`.
- Symptom: execute correctly obtained GMAS context before writing, then `project-setup` created `frontend/package.json` and `frontend/tsconfig.json`. When `frontend/tsconfig.node.json` was blocked by active write scope, the worker used `mutate_phase_plan` to add that path to the active leaf. The mutation was rejected because the workspace now had an existing `frontend` implementation root, created by the same active leaf.
- Risk: long greenfield execute runs can deadlock on legitimate file-scope repair. Umbrella asks the agent to mutate the phase plan, but the mutation validator reinterprets the active leaf's already-started scaffold as an attempt to rebuild an existing project.
- Cause: execute-time mutation policy reused old title/goal text such as "Create project structure" while checking the current filesystem. That conflated fresh plan proposal validation with narrow active-leaf mutation validation.
- Fix: `_phase_plan_policy_payload` now builds a minimal validation view for touched subtasks: it keeps id, current/patched `success_test`, patched fields, and changed file scope, but does not re-grade unchanged title/goal scaffold wording against workspace roots created during the same active leaf.
- Regression: `test_mutate_phase_plan_merges_file_scope_lists_from_captured_setup_patch` now creates partial `frontend` files before calling `mutate_phase_plan`, reproducing the captured failure shape.

## 2026-05-20 - Research Finding Tag Was Silently Saved As Observation

- Run: `phase_web_2a0e2104`.
- Symptom: research repeatedly called `palace_add` with `tags="research_finding,..."` but without `kind="research_finding"`. Umbrella saved those rows as `kind=observation`, stripped the `research_finding` tag, and returned ids. The agent then cited those ids in `submit_research_summary`, which correctly rejected them as non-findings, causing a loop.
- Risk: research can burn many rounds writing untrusted hot observations that look useful to the operator but cannot satisfy the phase handoff. This weakens the memory hierarchy and delays the actual project build.
- Cause: `palace_add` allowed ambiguous counted-finding intent through tags while defaulting omitted `kind` to observation. The tool protected trusted memory, but its repair signal arrived too late at summary time instead of at the write boundary.
- Fix: research-phase `palace_add` now rejects calls tagged as `research_finding` when they would be stored as `observation`, with explicit guidance to call `palace_add(kind="research_finding", ...)` for counted findings or remove the tag for leads. The research prompt now states that counted `findings_ids` require `kind="research_finding"`.
- Regression: `test_palace_add_rejects_ambiguous_research_finding_tag_without_kind`.

## 2026-05-20 - Watcher-Proven TOML Test Defect Could Not Migrate Contract

- Run: `phase_web_8d1bf872`.
- Symptom: execute generated `tests/test_setup.py` with `assert "tool.uv.sources" in pyproject`, while valid TOML parsing represents `[tool.uv.sources]` as nested dictionaries. The worker produced failing pytest evidence, diagnostic TOML output, and `request_watcher_review(status=review_recorded)`, but `mutate_phase_plan` still rejected the contract migration as not proven.
- Risk: long execute runs can deadlock after Umbrella itself verifies a generated success-test defect. The watcher record becomes journal text rather than usable control-plane evidence.
- Cause: active success-test migration checked mostly the local `contract_migration_reason` against fixed text fragments and did not read structured watcher review payloads for the same `subtask_id` and `success_test`. It also missed common structural-defect language such as `structurally impossible`, `flat key`, and `nested dictionaries`. Separately, `request_watcher_review` could return `status=review_recorded` before the repeated-failure threshold, but retry-state loading ignored those accepted records.
- Fix: `mutate_phase_plan` now accepts structured Umbrella watcher evidence for the same active subtask/success-test when that evidence proves an internal test-contract defect, while keeping API-preference and clean-architecture migrations blocked. The evidence matcher now covers structural impossibility language without hardcoding `tool.uv.sources`. Retry-state loading now treats any Umbrella `review_recorded` watcher payload with at least one failed attempt as an actual review; the threshold remains the point where a new watcher becomes mandatory.
- Regression: `test_mutate_phase_plan_accepts_watcher_proven_structural_toml_test_defect` and `test_retry_state_counts_recorded_watcher_review_before_threshold`.

## 2026-05-20 - Research Findings Could Self-Verify Without Source Provenance

- Run: `phase_web_3dde17c1`.
- Symptom: research saved counted `research_finding` rows with no `source_id` or vague sources such as `github_inspection`; the returned memory had `verified=true` and `source_path="tool:palace_add"`, so the palace write itself became the proof. Progress notes like `First finding stored` were promoted to trusted research findings.
- Risk: later plan/execute phases can build from hot memory that is not tied to any current discovery result. The memory hierarchy becomes a journal of claims rather than a provenance graph.
- Cause: after accepted research findings were made `verified=true`, `palace_add` did not require counted research findings to cite a verifiable current source. Exact tool verification only applied to `evidence_kind=verified_outcome` and only for a few tool ids, so missing/vague/default sources bypassed the boundary.
- Fix: research-phase `research_finding` now requires source provenance from a current logged tool result. Accepted sources include exact discovery tools (`github_project_search`, `mcp_discover`, `web_search`, `deep_search`, `search_gmas_knowledge`, `get_gmas_context`, `read_file`, `read_workspace_charter`, `env_check`, `palace_search`), tool-qualified ids such as `deep_search:<intent-or-query>`, or structured namespaces such as `github:owner/repo` and `gmas:topic`. `github:owner/repo` must match a current `github_project_search` result; `tool:<qualifier>` must match current tool payload fields such as intent/query/result metadata; `gmas:*` requires current GMAS discovery. Missing/self/vague sources are rejected with guidance to save non-evidence notes as `kind=observation`. The research prompt and `palace_add` schema now document this source grammar.
- Follow-up during `phase_web_252f4329`: `deep_search:github_discovery` still failed because `result_preview` was truncated and the validator looked only at result payload, while the stable `intent`/`query` lived in logged tool args. Provenance matching now reads both `args` and result payloads.
- Regression: `test_palace_add_rejects_research_finding_without_current_source`, `test_palace_add_rejects_unmatched_github_namespace_source`, `test_palace_add_accepts_matched_github_namespace_source`, `test_palace_add_accepts_tool_qualified_deep_search_source`, and `test_palace_add_accepts_tool_qualified_source_from_logged_args_when_preview_truncated`.

## 2026-05-20 - PhasePlan Accepted A Pytest Success Test With No Owned Test File

- Run: `phase_web_05a23e7b`.
- Symptom: plan accepted `docs-env-contract` with `success_test="python -m pytest tests/test_docs.py -q"`, but the leaf only declared `README.md`, `.env.example`, `docs/architecture.md`, and `docs/agent_topology.md`. Execute wrote the docs, then pytest failed with `ERROR: file or directory not found: tests/test_docs.py`. The write-scope guard correctly blocked creating `tests/test_docs.py`, because the test file was not in the active leaf file contract.
- Risk: Umbrella can accept an executable plan that is structurally impossible to complete without mutating its own proof contract. Watcher then records the gap, but execute churns between missing-test failures, write-scope blocks, and rejected contract migration.
- Cause: plan policy checked that `success_test` looked executable, but did not build an evidence/proof-target graph tying explicit pytest file targets to files available by that leaf. The existing e2e-only check was too narrow and allowed ordinary documentation/setup leaves to reference implicit tests.
- Fix: added `umbrella.deep_agent_tools.evidence_graph.PhasePlanEvidenceGraph`, a shared structural layer that maps each plan leaf to declared files and pytest proof targets. `propose_phase_plan`, `submit_phase_plan`, `plan_review`, and runner execution-floor checks now reject explicit pytest targets unless the target already exists or is declared in the same or an earlier leaf. This also covers success-test aliases such as `success_checks` and normalizes workspace-prefixed plan paths.
- Regression: `test_propose_phase_plan_rejects_captured_docs_pytest_target_not_owned` and `test_latest_phase_plan_execution_floor_rejects_unowned_pytest_target`. Existing positive fixtures were updated to declare the pytest files they claim to run.

## 2026-05-20 - PhasePlan Accepted Typoed LLM Base URL Alias

- Run: `phase_web_05a23e7b`.
- Symptom: the submitted plan's top-level `llm_runtime_contract` said `OUROBOROS_LLM_BASE_URL or LL_BASE_URL`, while the supported alias is `LLM_BASE_URL`. Other fields in the same plan correctly mentioned `LLM_BASE_URL`, so aggregate alias checks considered the contract complete.
- Risk: later phases can inherit a typoed runtime contract and generate docs/tests/code that look close to correct but fail the actual inherited LLM env resolution path.
- Cause: LLM env checks verified that all required aliases appeared somewhere in the plan, but there was no Umbrella-level DomainPolicy list of unsupported alias tokens. A typo could coexist with correct aliases elsewhere and pass.
- Fix: added `umbrella.deep_agent_tools.domain_policy` with canonical LLM runtime alias groups and unsupported alias detection. Phase-contract validation and runner execution-floor validation now reject `LL_BASE_URL` with guidance to use `LLM_BASE_URL`. Existing protective handling for `OUROBOROS_LLM_MODEL` remains in place.
- Regression: `test_propose_phase_plan_rejects_unsupported_ll_base_url_alias` and `test_latest_phase_plan_execution_floor_rejects_unsupported_ll_base_url_alias`.
