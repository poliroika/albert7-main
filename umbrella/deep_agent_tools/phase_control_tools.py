"""Umbrella-owned phase-control tool registry and compatibility barrel."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools import phase_control_actions as _actions
from umbrella.deep_agent_tools import phase_control_base as _base
from umbrella.deep_agent_tools import phase_control_completion as _completion
from umbrella.deep_agent_tools import phase_control_research as _research
from umbrella.deep_agent_tools import phase_control_review as _review

_MODULES = (_base, _research, _review, _completion, _actions)

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
                    "update node status/subtask cards). For a generated test "
                    "contract that is internally wrong, patch the current "
                    "subtask with contract_migration_reason and "
                    "contract_migration_files before editing that success-test file."
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
                                "\"contract_migration_reason\":\"why the generated "
                                "test contract is wrong\","
                                "\"contract_migration_files\":[\"tests/test_x.py\"]}]}"
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
                    "Submit the verdict of a mini review phase (ok/revise/abort). "
                    "revise/abort must include actionable revisions or notes."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["verdict"],
                    "properties": {
                        "verdict": {"type": "string", "enum": ["ok", "revise", "abort"]},
                        "revisions": {"type": "array", "items": {"type": "string"}},
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
                "description": "Submit verification result (pass/fail).",
                "parameters": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {
                        "status": {"type": "string", "enum": ["pass", "fail"]},
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
                        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    },
                },
            },
            handler=_submit_reflection,
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
                "description": "Patch a subtask card (goal, tools, test, etc.).",
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
                    "emit the Umbrella phase signal. Also supports legacy "
                    "PhasePlan-card completion by subtask_id."
                ),
                "parameters": {
                    "type": "object",
                    "required": [],
                    "properties": {
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
