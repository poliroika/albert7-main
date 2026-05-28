"""Umbrella-owned phase-control tool registry and compatibility barrel."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools import phase_control_actions as _actions
from umbrella.deep_agent_tools import phase_control_base as _base
from umbrella.deep_agent_tools import phase_control_research as _research
from umbrella.deep_agent_tools import phase_control_retry as _retry
from umbrella.contracts.schemas import (
    COMPLETION_CONTRACT_SCHEMA,
    EVIDENCE_REF_SCHEMA,
    REVIEW_COVERAGE_SCHEMA,
    REVIEW_ISSUE_SCHEMA,
    VERIFICATION_REPORT_REF_SCHEMA,
)

_MODULES = (_base, _research, _retry, _actions)

for _module in _MODULES:
    for _name in getattr(_module, "__all__", ()): 
        globals()[_name] = getattr(_module, _name)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="mutate_phase_plan",
            schema={
                "name": "mutate_phase_plan",
                "description": (
                    "Mutate the active PhasePlan (add/remove/reorder phases, "
                    "update node status/subtask cards). Subtask cards must "
                    "continue to satisfy contract v1 typed proof validation. "
                    "Do not use this just to gain permission for an ordinary "
                    "source edit; PhasePlan file ownership is advisory during "
                    "execute. Plan-contract revisions must use a typed "
                    "target_subtask_id plus a semantic proof patch; metadata "
                    "or notes without a real contract diff are rejected."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["patch"],
                    "properties": {
                        "patch": {
                            "type": "object",
                            "description": (
                                "Key-value patch applied to PhasePlan. Example: "
                                "{\"subtasks\":[{\"id\":\"subtask_1\","
                                "\"proof\":{\"execution\":{\"kind\":\"pytest\","
                                "\"command\":[\"python\",\"-m\",\"pytest\","
                                "\"tests/test_x.py\",\"-q\"]}}}]}"
                            ),
                        },
                        "target_subtask_id": {
                            "type": "string",
                            "description": (
                                "Optional selector for the execute subtask to mutate. "
                                "When set, patch contains subtask fields, not an id."
                            ),
                        },
                        "subtask_id": {
                            "type": "string",
                            "description": (
                                "Backward-compatible alias for target_subtask_id. "
                                "Use only as a selector, not inside patch."
                            ),
                        },
                    },
                },
            },
            handler=_mutate_phase_plan,
        ),
        ToolEntry(
            name="add_phase",
            schema={
                "name": "add_phase",
                "description": "Insert an extra phase into the PhasePlan after a named phase.",
                "parameters": {
                    "type": "object",
                    "required": ["after", "manifest_id"],
                    "properties": {
                        "after": {"type": "string"},
                        "manifest_id": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
            handler=_add_phase,
        ),
        ToolEntry(
            name="loop_back_to",
            schema={
                "name": "loop_back_to",
                "description": "Reset a phase to pending so the runner re-executes it.",
                "parameters": {
                    "type": "object",
                    "required": ["phase"],
                    "properties": {
                        "phase": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            handler=_loop_back_to,
        ),
        ToolEntry(
            name="submit_capability_declaration",
            schema={
                "name": "submit_capability_declaration",
                "description": (
                    "Persist discovery-backed capability declaration before "
                    "research handoff. Capabilities are free-form slugs; optional "
                    "per-capability probe runs a workspace argv command. "
                    "Capabilities describe what the platform/tooling can run, "
                    "not which proof strategy is preferred for this task; do "
                    "not mark a capability unavailable merely because it is "
                    "not suitable or not needed. "
                    "Harness runtime capabilities such as desktop_gui_runtime "
                    "must be probe-backed under that same slug when available; "
                    "for real-window GUI tasks, try the same-slug probe before "
                    "declaring the runtime unavailable unless policy already "
                    "proves it cannot run. A desktop_gui_runtime probe must "
                    "create/update/destroy or show a native window/root; "
                    "import-only checks belong to desktop_gui_headless or a "
                    "library-specific capability. If the handoff recommends "
                    "Tkinter/PyQt/PySide/wxPython/native desktop GUI, declare "
                    "a usable desktop_gui_headless or desktop_gui_runtime "
                    "capability instead of leaving GUI proof implicit."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["capabilities", "notes"],
                    "properties": {
                        "capabilities": {
                            "type": "object",
                            "description": (
                                "Capability slug -> bool or "
                                "{available, source, reason, probe:{kind,command}}. "
                                "available=false must mean a real platform, "
                                "policy, install, or failed-probe limitation, "
                                "not a planning preference. "
                                "Probe-required harness capabilities cannot be "
                                "declared available without a probe on that slug. "
                                "For desktop_gui_runtime, do not use import-only "
                                "commands; create/destroy or show a real GUI root. "
                                "For native GUI toolkit work, include "
                                "desktop_gui_headless available=true when "
                                "headless adapter/controller proof can run."
                            ),
                        },
                        "probes": {
                            "type": "object",
                            "description": (
                                "Optional slug -> probe spec; merged into capabilities. "
                                "This is how research runs capability checks "
                            "without shell access. For Tkinter real-window "
                            "desktop runtime, use probes.desktop_gui_runtime="
                            "{\"kind\":\"command\",\"command\":[\"python\","
                            "\"-c\",\"import tkinter as tk; root=tk.Tk(); "
                            "root.update(); root.destroy()\"],\"expect_exit\":0}. "
                            "If you also pass capabilities.<slug>.available, "
                            "it must match the probe result."
                        ),
                        },
                        "discovery_channels": {
                            "type": "array",
                            "description": (
                                "Discovery rows. Preferred row shape: "
                                "{tool, outcome, notes}. Common rows such as "
                                "{channel, search, results, sources} are "
                                "normalized."
                            ),
                            "items": {"type": "object"},
                        },
                        "discoveries": {
                            "type": "array",
                            "description": (
                                "Alias for discovery_channels for compatibility."
                            ),
                            "items": {"type": "object"},
                        },
                        "recommended_skills": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "constraints": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "limitations": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "notes": {"type": "string"},
                        "evidence_refs": {
                            "type": "array",
                            "items": EVIDENCE_REF_SCHEMA,
                        },
                        "status": {
                            "type": "string",
                            "enum": ["draft", "submitted"],
                            "default": "submitted",
                        },
                    },
                },
            },
            handler=_submit_capability_declaration,
        ),
        ToolEntry(
            name="submit_research_summary",
            schema={
                "name": "submit_research_summary",
                "description": "Signal completion of the research phase with architecture and findings references.",
                "parameters": {
                    "type": "object",
                    "required": ["architecture_id", "findings_ids", "notes"],
                    "properties": {
                        "architecture_id": {"type": "string"},
                        "findings_ids": {"type": "array", "items": {"type": "string"}},
                        "notes": {
                            "type": "string",
                            "minLength": 20,
                            "description": (
                                "Concrete handoff notes covering libraries, "
                                "skills, risks, and recommended architecture."
                            ),
                        },
                        "coverage_status": {
                            "type": "string",
                            "enum": ["complete", "source_scarce", "blocked"],
                            "description": (
                                "Optional. Use source_scarce only when all "
                                "required discovery channels were attempted "
                                "and fewer usable sources exist than the "
                                "finding floor; do not use it to cite fake or "
                                "duplicate findings."
                            ),
                        },
                        "source_scarcity_reason": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
                    },
                },
            },
            handler=_submit_research_summary,
        ),
        ToolEntry(
            name="submit_micro_review",
            schema={
                "name": "submit_micro_review",
                "description": (
                    "Submit a typed mini-review contract. Notes are human-only; "
                    "machine decisions use issue codes/severities. For ok, "
                    "coverage booleans mean checked/no blocker; do not use "
                    "false for not-applicable."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["verdict", "issues", "coverage"],
                    "properties": {
                        "verdict": {"type": "string", "enum": ["ok", "revise", "abort"]},
                        "issues": {"type": "array", "items": REVIEW_ISSUE_SCHEMA},
                        "coverage": REVIEW_COVERAGE_SCHEMA,
                        "required_plan_changes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "loop_back_target": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
            handler=_submit_micro_review,
        ),
        ToolEntry(
            name="request_scope_change",
            schema={
                "name": "request_scope_change",
                "description": (
                    "Expand the active execute subtask scope by adding paths to "
                    "files_to_create. Use after scope_change_required blocks."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["paths"],
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "rationale": {"type": "string"},
                    },
                },
            },
            handler=_request_scope_change,
        ),
        ToolEntry(
            name="submit_phase_plan",
            schema={
                "name": "submit_phase_plan",
                "description": (
                    "Signal that the planning phase is complete with a filled "
                    "PhasePlan. If plan_id is omitted, the latest accepted "
                    "propose_phase_plan artifact is used."
                ),
                "parameters": {
                    "type": "object",
                    "required": [],
                    "properties": {
                        "plan_id": {
                            "type": "string",
                            "description": (
                                "Optional. Usually pass the plan_id returned by "
                                "propose_phase_plan; omit to submit the latest proposal."
                            ),
                        },
                        "notes": {"type": "string"},
                    },
                },
            },
            handler=_submit_phase_plan,
        ),
        ToolEntry(
            name="submit_final_review",
            schema={
                "name": "submit_final_review",
                "description": "Submit the outcome of the final review phase.",
                "parameters": {
                    "type": "object",
                    "required": ["outcome"],
                    "properties": {
                        "outcome": {"type": "string", "enum": ["ok", "loop_back"]},
                        "notes": {"type": "string"},
                    },
                },
            },
            handler=_submit_final_review,
        ),
        ToolEntry(
            name="submit_verification",
            schema={
                "name": "submit_verification",
                "description": (
                    "Submit verification result. pass requires a "
                    "ledger-backed VerificationReportRef."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {
                        "status": {"type": "string", "enum": ["pass", "fail"]},
                        "verification_report_ref": VERIFICATION_REPORT_REF_SCHEMA,
                        "details": {"type": "string"},
                    },
                },
            },
            handler=_submit_verification,
        ),
        ToolEntry(
            name="submit_reflection",
            schema={
                "name": "submit_reflection",
                "description": "Submit a verbal reflection on failure with mandatory evidence citations.",
                "parameters": {
                    "type": "object",
                    "required": ["text", "applies_to_phase", "evidence_refs"],
                    "properties": {
                        "text": {"type": "string"},
                        "applies_to_phase": {"type": "string"},
                        "applies_to_subtask": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": EVIDENCE_REF_SCHEMA, "minItems": 1},
                        "proposed_bkb_rules": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                },
            },
            handler=_submit_reflection,
        ),
        ToolEntry(
            name="accept_bkb_proposal",
            schema={
                "name": "accept_bkb_proposal",
                "description": (
                    "Accept a proposed BKB patch written by submit_reflection "
                    "(drive/state/proposed_bkb_patch.json) after evidence validation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patch_id": {"type": "string"},
                        "workspace_id": {"type": "string"},
                    },
                },
            },
            handler=_accept_bkb_proposal,
        ),
        ToolEntry(
            name="submit_preflight_report",
            schema={
                "name": "submit_preflight_report",
                "description": (
                    "Report preflight platform readiness. Use blocked only for "
                    "environment, credential, memory, MCP, task-charter, or human "
                    "intervention blockers; implementation defects belong to later phases."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {
                        "status": {"type": "string", "enum": ["ready", "blocked"]},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                        "research_depth": {
                            "type": "string",
                            "enum": ["none", "light", "full"],
                            "description": "Required when status=ready. Task complexity tier for research phase.",
                        },
                        "research_depth_rationale": {
                            "type": "string",
                            "description": "Brief reason for the chosen depth (max 500 chars).",
                        },
                    },
                },
            },
            handler=_submit_preflight_report,
        ),
        ToolEntry(
            name="edit_subtask_card",
            schema={
                "name": "edit_subtask_card",
                "description": "Patch a subtask card (goal, files, proof, tools, etc.).",
                "parameters": {
                    "type": "object",
                    "required": ["subtask_id", "patch"],
                    "properties": {
                        "subtask_id": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                },
            },
            handler=_edit_subtask_card,
        ),
        ToolEntry(
            name="mark_subtask_complete",
            schema={
                "name": "mark_subtask_complete",
                "description": (
                    "Mark the current internal Ouroboros subtask complete and "
                    "emit the Umbrella phase signal. Phase-run completion "
                    "requires a typed CompletionContract."
                ),
                "parameters": {
                    "type": "object",
                    "required": [],
                    "properties": {
                        "completion_contract": COMPLETION_CONTRACT_SCHEMA,
                        "subtask_id": {"type": "string"},
                        "notes": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["done", "failed", "skipped"],
                            "default": "done",
                        },
                        "summary": {"type": "string"},
                        "evidence": {
                            "oneOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "string"},
                            ]
                        },
                    },
                },
            },
            handler=_mark_subtask_complete,
        ),
        ToolEntry(
            name="request_watcher_review",
            schema={
                "name": "request_watcher_review",
                "description": (
                    "Record a typed retry-watcher review/control signal for "
                    "the current phase. Returns verdict, allowed_next_actions, "
                    "forbidden_next_actions, and any typed plan_revision_patch; "
                    "it does not synchronously run a separate LLM. Free-text "
                    "reason is notes only; plan revision requires typed "
                    "contract_issues with contract_path, invalid_values or "
                    "required_deltas, and evidence_refs."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["reason"],
                    "properties": {
                        "reason": {"type": "string"},
                        "contract_issues": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["code", "contract_path"],
                                "properties": {
                                    "code": {
                                        "type": "string",
                                        "enum": [
                                            "bad_generated_oracle",
                                            "plan_contract_issue",
                                        ],
                                    },
                                    "target_subtask_id": {"type": "string"},
                                    "contract_path": {"type": "string"},
                                    "invalid_values": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "required_deltas": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["op", "path"],
                                            "properties": {
                                                "op": {
                                                    "type": "string",
                                                    "enum": ["remove", "replace", "add"],
                                                },
                                                "path": {"type": "string"},
                                                "values": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                },
                                                "replacement": {},
                                            },
                                        },
                                    },
                                    "evidence_refs": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "evidence": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            handler=_request_watcher_review,
        ),
        ToolEntry(
            name="harness_run",
            schema={
                "name": "harness_run",
                "description": (
                    "Record a harness run request/control signal for a subtask. "
                    "Candidate execution and winner selection are handled by "
                    "the phase runner, not synchronously by this tool call."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["subtask_id"],
                    "properties": {
                        "subtask_id": {"type": "string"},
                        "n_candidates": {"type": "integer", "minimum": 1, "maximum": 8},
                        "strategy": {"type": "string", "enum": ["tests_pass", "metric", "vote"]},
                        "timeout_sec": {"type": "integer"},
                    },
                },
            },
            handler=_harness_run,
        ),
        ToolEntry(
            name="run_subtask_proof",
            schema={
                "name": "run_subtask_proof",
                "description": (
                    "Execute the active phase-plan subtask proof command and return "
                    "ledger-backed verification_report / proof_ref for mark_subtask_complete."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subtask_id": {
                            "type": "string",
                            "description": "Defaults to the first pending execute subtask.",
                        },
                    },
                },
            },
            handler=_run_subtask_proof,
        ),
    ]


__all__ = [
    name
    for name in globals()
    if not (name.startswith("__") and name.endswith("__"))
]
