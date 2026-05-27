"""Discovery helpers shared by GitHub, MCP, and research tooling."""

from umbrella.discovery.external_catalog import (
    catalog_path,
    catalog_summary_for_prompt,
    find_by_storage_ref,
    list_cards,
    mirror_preview_body,
    plan_external_memory_issues,
    register_card,
    resolve_ref,
    suggest_memory_scope_for_goal,
)

__all__ = [
    "catalog_path",
    "catalog_summary_for_prompt",
    "find_by_storage_ref",
    "list_cards",
    "mirror_preview_body",
    "plan_external_memory_issues",
    "register_card",
    "resolve_ref",
    "suggest_memory_scope_for_goal",
]
