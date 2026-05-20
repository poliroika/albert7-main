"""Ouroboros registry adapter for Umbrella-owned palace/memory tools."""
from typing import Any


# ---------------------------------------------------------------------------
# Handler imports are lazy so the Ouroboros registry can load without pulling
# Umbrella memory dependencies until these tools are actually surfaced.
# ---------------------------------------------------------------------------

def _handlers():
    from umbrella.deep_agent_tools.memory import (  # noqa: PLC0415
        get_umbrella_memory,
        list_memory_tree,
        save_umbrella_memory,
        record_workspace_event,
        record_idea,
        save_umbrella_lesson,
    )
    return (
        get_umbrella_memory,
        list_memory_tree,
        save_umbrella_memory,
        record_workspace_event,
        record_idea,
        save_umbrella_lesson,
    )


def get_tools() -> list[Any]:
    """Return ToolEntry list for palace/memory tools."""
    from ouroboros.tools.registry import ToolEntry  # noqa: PLC0415

    (
        get_umbrella_memory,
        list_memory_tree,
        save_umbrella_memory,
        record_workspace_event,
        record_idea,
        save_umbrella_lesson,
    ) = _handlers()

    return [
        ToolEntry(
            "get_umbrella_memory",
            {
                "name": "get_umbrella_memory",
                "description": (
                    "Semantic search over Umbrella memory (MemPalace ChromaDB). "
                    "Returns palace memories ranked by relevance + structured lessons. "
                    "Filter by workspace_id for workspace-scoped results."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "default": "",
                            "description": "Natural-language search query",
                        },
                        "workspace_id": {
                            "type": "string",
                            "default": "",
                            "description": "Scope to a workspace wing",
                        },
                        "palace_path": {
                            "type": "string",
                            "default": "",
                            "description": "Legacy path filter (workspaces/X/room)",
                        },
                        "limit": {"type": "integer", "default": 10},
                        "include_unverified": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Include candidate/hypothesis memories in main "
                                "result lists instead of only in unverified_candidates."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: get_umbrella_memory(ctx, **kw),
        ),
        ToolEntry(
            "list_memory_tree",
            {
                "name": "list_memory_tree",
                "description": (
                    "List hierarchical ideas tree (JSONL-backed palace_path counts) "
                    "for manager memory or a specific workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "default": "",
                            "description": "Empty = manager root; else workspaces/<id>/.memory",
                        },
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: list_memory_tree(ctx, **kw),
        ),
        ToolEntry(
            "save_umbrella_memory",
            {
                "name": "save_umbrella_memory",
                "description": (
                    "Save a memory entry to MemPalace (semantic ChromaDB). "
                    "Use for ideas, errors, logs, changes, decisions. "
                    "Entries are auto-classified into wings (workspaces), "
                    "halls (event types), and rooms (topics)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "palace_path": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "kind": {"type": "string", "default": "observation"},
                        "workspace_id": {"type": "string", "default": ""},
                        "tags": {"type": "string", "default": ""},
                    },
                    "required": ["palace_path", "title", "content"],
                },
            },
            lambda ctx, **kw: save_umbrella_memory(ctx, **kw),
        ),
        ToolEntry(
            "record_workspace_event",
            {
                "name": "record_workspace_event",
                "description": "Record a workspace change/log/error/idea into Umbrella hierarchical memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "event_type": {"type": "string"},
                        "summary": {"type": "string"},
                        "details": {"type": "string", "default": ""},
                        "severity": {"type": "string", "default": "info"},
                        "tags": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "event_type", "summary"],
                },
            },
            lambda ctx, **kw: record_workspace_event(ctx, **kw),
        ),
        ToolEntry(
            "record_idea",
            {
                "name": "record_idea",
                "description": (
                    "Record a hypothesis or observation in workspace hierarchical "
                    "memory. Use this for thinking out loud, noting patterns, or "
                    "capturing context the next round will need. Does NOT accept "
                    "kind='lesson' — for verified lessons call save_umbrella_lesson "
                    "instead. Only entries with evidence_kind='verified_outcome' "
                    "are mirrored to semantic search; hypotheses stay local to "
                    "the JSONL so recall stays clean."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "default": "",
                            "description": "Idea body. Use this or body.",
                        },
                        "kind": {
                            "type": "string",
                            "default": "idea",
                            "description": "idea, verification_fix, prompt_fix, tool_gap, etc. (NOT 'lesson').",
                        },
                        "title": {"type": "string", "default": ""},
                        "body": {
                            "type": "string",
                            "default": "",
                            "description": "Structured body. Use this or content.",
                        },
                        "palace_path": {
                            "type": "string",
                            "default": "",
                            "description": "Optional hierarchy path such as workspaces/<id>/ideas/verification.",
                        },
                        "tags": {"type": "string", "default": ""},
                        "workspace_id": {"type": "string", "default": ""},
                        "evidence_kind": {
                            "type": "string",
                            "default": "hypothesis",
                            "enum": [
                                "hypothesis",
                                "observation_from_log",
                                "verified_outcome",
                            ],
                            "description": (
                                "How was this idea obtained? 'hypothesis' = your guess "
                                "(default); 'observation_from_log' = you saw it in tool "
                                "output; 'verified_outcome' = you confirmed it after "
                                "running run_workspace_verify (PASS). Only "
                                "'verified_outcome' makes it into semantic recall."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: record_idea(ctx, **kw),
        ),
        ToolEntry(
            "save_umbrella_lesson",
            {
                "name": "save_umbrella_lesson",
                "description": (
                    "Save a structured workspace lesson. To rank as a verified "
                    "lesson (priority 5, recall-eligible) the call MUST include "
                    "verify_run_id from a passing run_workspace_verify, "
                    "verification_passed=True, critic_verdict='pass' and "
                    "failed_step_count=0. Lessons missing any of these are "
                    "stored as 'unverified' / 'avoid' so they don't pollute "
                    "future recall."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "change_summary": {"type": "string"},
                        "expected_effect": {"type": "string"},
                        "observed_effect": {"type": "string", "default": ""},
                        "tags": {"type": "string", "default": ""},
                        "verification_passed": {"type": "boolean", "default": False},
                        "critic_verdict": {"type": "string", "default": ""},
                        "verify_run_id": {
                            "type": "string",
                            "default": "",
                            "description": (
                                "Id of the run_workspace_verify call that backs this "
                                "lesson. Required for verified=True; without it the "
                                "lesson is stored as candidate/avoid."
                            ),
                        },
                        "failed_step_count": {
                            "type": "integer",
                            "default": 0,
                            "description": (
                                "Number of failed required steps in the verify run "
                                "that backs this lesson. Must be 0 for verified=True."
                            ),
                        },
                    },
                    "required": ["workspace_id", "change_summary", "expected_effect"],
                },
            },
            lambda ctx, **kw: save_umbrella_lesson(ctx, **kw),
        ),
    ]
