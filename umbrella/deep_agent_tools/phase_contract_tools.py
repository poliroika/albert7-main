"""Umbrella-owned phase-contract tool registry and compatibility barrel."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools import phase_contract_base as _base
from umbrella.deep_agent_tools import phase_contract_declarations as _declarations
from umbrella.deep_agent_tools import domain_policy as _domain_policy
from umbrella.deep_agent_tools import phase_contract_handlers as _handlers
from umbrella.contracts.schemas import EVIDENCE_REF_SCHEMA, VERIFICATION_REPORT_REF_SCHEMA
from umbrella.deep_agent_tools.research_provenance import SOURCE_ID_DESCRIPTION

_MODULES = (
    _base,
    _declarations,
    _domain_policy,
    _handlers,
)

for _module in _MODULES:
    for _name in getattr(_module, "__all__", ()): 
        globals()[_name] = getattr(_module, _name)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry("list_files", _schema("list_files", "Alias for list_workspace_files in the active workspace.", {"workspace_id": {"type": "string"}, "subdir": {"type": "string"}, "max_entries": {"type": "integer", "default": 300}}), _list_files),
        ToolEntry("read_file", _schema("read_file", "Alias for read_workspace_file in the active workspace.", {"workspace_id": {"type": "string"}, "file_path": {"type": "string"}, "max_chars": {"type": "integer", "default": 30000}, "offset": {"type": "integer", "default": 0}, "line_start": {"type": "integer", "default": 0, "description": "1-based line number for line-oriented snippets. Prefer this over offset when you have pytest/rg line numbers."}, "line_count": {"type": "integer", "default": 160}}, ["file_path"]), _read_file),
        ToolEntry("shell", _schema("shell", "Alias for run_workspace_command scoped to the active workspace.", {"workspace_id": {"type": "string"}, "command": {"type": ["array", "string"]}, "argv": {"type": "array", "items": {"type": "string"}}, "subdir": {"type": "string"}, "timeout_seconds": {"type": "integer", "default": 180}, "allow_dependency_install": {"type": "boolean", "default": False}}), _shell, is_code_tool=True, timeout_sec=600),
        ToolEntry("terminal_session", _schema("terminal_session", "Compatibility alias for a foreground workspace command.", {"workspace_id": {"type": "string"}, "command": {"type": ["array", "string"]}, "argv": {"type": "array", "items": {"type": "string"}}, "subdir": {"type": "string"}, "timeout_seconds": {"type": "integer", "default": 180}, "allow_dependency_install": {"type": "boolean", "default": False}}), _shell, is_code_tool=True, timeout_sec=600),
        ToolEntry("run_unit_tests", _schema("run_unit_tests", "Compatibility alias for run_workspace_verify.", {"workspace_id": {"type": "string"}, "timeout_seconds": {"type": "integer", "default": 600}}), _run_unit_tests, is_code_tool=True, timeout_sec=900),
        ToolEntry("run_real_e2e", _schema("run_real_e2e", "Run the workspace e2e suite and enforce goal-appropriate localhost/browser evidence when the charter requires a web UI.", {"workspace_id": {"type": "string"}, "timeout_seconds": {"type": "integer", "default": 600}}), _run_real_e2e, is_code_tool=True, timeout_sec=900),
        ToolEntry("palace_search", _schema("palace_search", "Compatibility alias for get_umbrella_memory.", {"query": {"type": "string"}, "palace_path": {"type": "string"}, "workspace_id": {"type": "string"}, "limit": {"type": "integer", "default": 10}, "include_unverified": {"type": "boolean", "default": False}}), _palace_search),
        ToolEntry("palace_add", _schema("palace_add", "Persist a concrete phase finding/artifact to Umbrella memory. The logical store is inferred from the active phase manifest; use accepted calls before phase completion when required.", {"title": {"type": "string"}, "content": {"type": "string"}, "palace_path": {"type": "string", "description": "Optional path hint. Prefer workspaces/<workspace_id>/<phase> or a logical store such as palace.run when the phase prompt specifies one."}, "kind": {"type": "string"}, "workspace_id": {"type": "string"}, "tags": {"type": "string"}, "source_id": {"type": "string", "description": SOURCE_ID_DESCRIPTION}, "evidence_kind": {"type": "string"}}, ["content"]), _palace_add),
        ToolEntry("palace_link", _schema("palace_link", "Record a typed link between memory/artifact identifiers.", {"source_id": {"type": "string"}, "target_id": {"type": "string"}, "relation": {"type": "string"}, "notes": {"type": "string"}, "workspace_id": {"type": "string"}}), _palace_link),
        ToolEntry("palace_walk", _schema("palace_walk", "Walk/list the workspace memory tree.", {"workspace_id": {"type": "string"}}), lambda ctx, **kw: umbrella_tools.list_memory_tree(ctx, workspace_id=kw.get("workspace_id") or _workspace_id(ctx))),
        ToolEntry("read_workspace_charter", _schema("read_workspace_charter", "Read TASK_MAIN/workspace charter files for the active workspace.", {"workspace_id": {"type": "string"}, "max_chars": {"type": "integer", "default": 20000}}), _read_workspace_charter),
        ToolEntry("env_check", _schema("env_check", "Report non-secret runtime/env readiness.", {}), _env_check),
        ToolEntry("palace_health", _schema("palace_health", "Check memory palace availability.", {}), _palace_health),
        ToolEntry("mcp_health", _schema("mcp_health", "Check MCP registry availability.", {}), _mcp_health),
        ToolEntry("skill_audit", _schema("skill_audit", "List installed workspace skill descriptors.", {"workspace_id": {"type": "string"}}), _skill_audit),
        ToolEntry("request_human_checkpoint", _schema("request_human_checkpoint", "Request an operator checkpoint.", {"reason": {"type": "string"}, "payload": {"type": "object"}}), _request_human_checkpoint),
        ToolEntry("request_extra_subtask", _schema("request_extra_subtask", "Request adding a new phase subtask.", {"reason": {"type": "string"}, "proposed_subtask": {"type": "object"}}), _request_extra_subtask),
        ToolEntry("register_temp_tool", _schema("register_temp_tool", "Register a temporary tool proposal for review.", {"name": {"type": "string"}, "description": {"type": "string"}, "schema": {"type": "object"}}, ["name"]), _register_temp_tool),
        ToolEntry("propose_phase_plan", _schema("propose_phase_plan", "Record a proposed Umbrella phase plan using contract v1 typed proof objects only.", {"plan": {"type": "object"}, "notes": {"type": "string"}}), _propose_phase_plan),
        ToolEntry("propose_subtasks", _schema("propose_subtasks", "Record proposed Umbrella subtasks.", {"steps": {"type": "array", "items": {"type": "object"}}, "notes": {"type": "string"}}), _propose_subtasks),
        ToolEntry("read_drive_log", _schema("read_drive_log", "Read recent lines from a drive log file.", {"log_name": {"type": "string"}, "tail": {"type": "integer", "default": 100}}), _read_drive_log),
        ToolEntry("read_terminal_scrollback", _schema("read_terminal_scrollback", "Read workspace terminal scrollback.", {"workspace_id": {"type": "string"}, "last_lines": {"type": "integer", "default": 200}}), _read_terminal_scrollback),
        ToolEntry(
            "promote_to_durable",
            _schema(
                "promote_to_durable",
                "Promote a verified artifact/note to durable memory (palace.durable).",
                {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "tags": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
                    "trust_level": {
                        "type": "string",
                        "enum": [
                            "public_verified",
                            "mutation_verified",
                            "hidden_verified",
                            "adversarial_verified",
                        ],
                    },
                    "verification_report_ref": VERIFICATION_REPORT_REF_SCHEMA,
                },
                ["content"],
            ),
            _promote_to_durable,
        ),
        ToolEntry("wipe_workspace", _schema("wipe_workspace", "Blocked destructive compatibility tool.", {"reason": {"type": "string"}}), _blocked_destructive),
        ToolEntry("reset_palace", _schema("reset_palace", "Blocked destructive compatibility tool.", {"reason": {"type": "string"}}), _blocked_destructive),
    ]


__all__ = [
    name
    for name in globals()
    if not (name.startswith("__") and name.endswith("__"))
]
