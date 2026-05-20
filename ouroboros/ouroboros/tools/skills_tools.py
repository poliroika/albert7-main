"""Ouroboros registry adapter for Umbrella-owned skill tools."""

from typing import Any

from umbrella.deep_agent_tools.skills import (
    _WORKSPACE_TOML_KNOWN_SKILLS,
    _upsert_workspace_toml_skill,
    configure_workspace_skills,
    load_skill,
)


def get_tools() -> list[Any]:
    """Return ToolEntry list for skills tools."""

    from ouroboros.tools.registry import ToolEntry  # noqa: PLC0415

    return [
        ToolEntry(
            "load_skill",
            {
                "name": "load_skill",
                "description": "Load full text of an Umbrella procedural skill by slug (L3 detail).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 40000},
                    },
                    "required": ["slug"],
                },
            },
            lambda ctx, **kw: load_skill(ctx, **kw),
            timeout_sec=60,
        ),
        ToolEntry(
            "configure_workspace_skills",
            {
                "name": "configure_workspace_skills",
                "description": (
                    "Override a named skill by editing [skills] in "
                    "workspace.toml. GMAS (multi_agent_gmas) is automatically "
                    "active for LLM/model/agent work; use this to record an "
                    "explicit opt-out for pure non-LLM work, or to force it "
                    "on when the task wording is too sparse. The skill cache "
                    "is invalidated so the next attempt picks up the change."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "skill_id": {
                            "type": "string",
                            "enum": list(_WORKSPACE_TOML_KNOWN_SKILLS),
                            "description": "Currently only `multi_agent_gmas` is wired into the verifier.",
                        },
                        "enabled": {"type": "boolean"},
                        "reason": {
                            "type": "string",
                            "default": "",
                            "description": "Short justification recorded in workspace event memory.",
                        },
                    },
                    "required": ["workspace_id", "skill_id", "enabled"],
                },
            },
            lambda ctx, **kw: configure_workspace_skills(ctx, **kw),
            is_code_tool=True,
        ),
    ]