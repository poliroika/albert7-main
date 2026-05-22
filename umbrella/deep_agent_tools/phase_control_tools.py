"""Umbrella-owned phase-control tool registry and compatibility barrel."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools import phase_control_actions as _actions
from umbrella.deep_agent_tools import phase_control_base as _base
from umbrella.deep_agent_tools import phase_control_research as _research
from umbrella.contracts.schemas import (
    COMPLETION_CONTRACT_SCHEMA,
    EVIDENCE_REF_SCHEMA,
    REVIEW_ISSUE_SCHEMA,
    VERIFICATION_REPORT_REF_SCHEMA,
)

_MODULES = (_base, _research, _actions)

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
                    "continue to satisfy contract v1 typed proof validation."
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
                        }
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
            name="submit_research_summary",
            schema={
                "name": "submit_research_summary",
                "description": "Signal completion of the research phase with architecture and findings references.",
                "parameters": {
                    "type": "object",
                    "required": ["architecture_id", "findings_ids"],
                    "properties": {
                        "architecture_id": {"type": "string"},
                        "findings_ids": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
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
                    "machine decisions use issue codes/severities."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["verdict", "issues"],
                    "properties": {
                        "verdict": {"type": "string", "enum": ["ok", "revise", "abort"]},
                        "issues": {"type": "array", "items": REVIEW_ISSUE_SCHEMA},
                        "loop_back_target": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
            handler=_submit_micro_review,
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
                "description": "Ask the Watcher agent to review the current phase state.",
                "parameters": {
                    "type": "object",
                    "required": ["reason"],
                    "properties": {"reason": {"type": "string"}},
                },
            },
            handler=_request_watcher_review,
        ),
        ToolEntry(
            name="harness_run",
            schema={
                "name": "harness_run",
                "description": "Run N parallel candidates for a subtask and return the winner.",
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
    ]


__all__ = [
    name
    for name in globals()
    if not (name.startswith("__") and name.endswith("__"))
]
