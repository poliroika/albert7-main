"""
Ouroboros — Tool registry (SSOT).

Plugin architecture: each module in tools/ exports get_tools().
ToolRegistry collects all tools, provides schemas() and execute().
"""

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from collections.abc import Callable

from ouroboros.utils import safe_relpath


@dataclass
class BrowserState:
    """Per-task browser lifecycle state (Playwright). Isolated from generic ToolContext."""

    pw_instance: Any = None
    browser: Any = None
    page: Any = None
    last_screenshot_b64: str | None = None


@dataclass
class ToolContext:
    """Tool execution context — passed from the agent before each task."""

    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    host_repo_root: pathlib.Path | None = None
    branch_dev: str = "ouroboros"
    pending_events: list[dict[str, Any]] = field(default_factory=list)
    current_chat_id: int | None = None
    current_task_type: str | None = None
    context_overlays: dict[str, Any] = field(default_factory=dict)
    workspace_root_overrides: dict[str, str] = field(default_factory=dict)
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # LLM-driven runtime overrides (set by switch_model tool, read by loop.py)
    active_model_override: str | None = None
    active_effort_override: str | None = None
    active_max_tokens_override: int | None = None
    active_temperature_override: float | None = None
    active_tool_choice_override: str | None = None

    # Per-task browser state
    browser_state: BrowserState = field(default_factory=BrowserState)

    # Budget tracking (set by loop.py for real-time usage events)
    event_queue: Any | None = None
    task_id: str | None = None

    # Task depth for fork bomb protection
    task_depth: int = 0

    # True when running inside handle_chat_direct (not a queued worker task)
    is_direct_chat: bool = False

    # Tier 1.3 / 3.1: read-only view of the active loop state. The loop
    # refreshes this dict before each ``_handle_tool_calls`` batch so that
    # tools like ``mark_subtask_complete`` / ``mark_remediation_complete``
    # can refuse closure when the verify evidence is stale or absent.
    # Keys (all optional):
    #   ``round_idx``: current loop round
    #   ``last_verify_run_id``: id of latest verify (empty if none / skipped)
    #   ``last_verify_round``: round at which verify last ran (-1 if never)
    #   ``last_verify_passed``: bool
    #   ``last_verify_failed_count``: int (non-optional steps that failed)
    #   ``last_write_round``: round at which a workspace write happened (-1 if none)
    #   ``current_subtask_discovery_calls``: int
    #   ``current_subtask_tags``: list[str]
    # When this dict is empty/missing, gates degrade gracefully (no
    # enforcement) to preserve backwards compatibility with callers that
    # don't populate it (e.g. legacy tests).
    loop_state_view: dict[str, Any] = field(default_factory=dict)

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()

    def host_repo_path(self, rel: str) -> pathlib.Path:
        base = self.host_repo_root or self.repo_dir
        return (base / safe_relpath(rel)).resolve()

    def drive_logs(self) -> pathlib.Path:
        return (self.drive_root / "logs").resolve()


@dataclass
class ToolEntry:
    """Single tool descriptor: name, schema, handler, metadata."""

    name: str
    schema: dict[str, Any]
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 120


CORE_TOOL_NAMES = {
    "repo_read",
    "repo_list",
    "repo_write_commit",
    "repo_commit_push",
    "drive_read",
    "drive_list",
    "drive_write",
    "run_shell",
    "claude_code_edit",
    "git_status",
    "git_diff",
    "schedule_task",
    "wait_for_task",
    "get_task_result",
    "propose_task_plan",
    "revise_remaining_plan",
    "mark_subtask_complete",
    "mark_remediation_complete",
    "get_current_plan",
    "update_scratchpad",
    "update_prompt",
    "record_idea",
    "python_eval",
    "chat_history",
    "web_fetch",
    "send_owner_message",
    "switch_model",
    "request_restart",
    "promote_to_stable",
    "knowledge_read",
    "knowledge_write",
    "browse_page",
    "browser_action",
    "analyze_screenshot",
    "search_gmas_knowledge",
    "get_gmas_context",
    "list_workspace_files",
    "read_workspace_file",
    "run_workspace_command",
    "run_workspace_verify",
    "commit_workspace_changes",
    "get_workspace_metrics",
    "get_workspace_logs",
    "update_workspace_seed",
    "apply_workspace_patch",
    "update_workspace_from_instance",
    "delete_workspace_file",
    "configure_workspace_skills",
    "get_umbrella_memory",
    "save_umbrella_memory",
    "record_workspace_event",
    "save_umbrella_lesson",
    "propose_discovery_plan",
    # Self-improvement / capability-gap escape hatches. Without these in the
    # core schema the model cannot reach them in any reasonable number of
    # rounds (it would have to first call `list_available_tools`, then
    # `enable_tools(...)`, then finally invoke the actual tool — in practice
    # this never happens on its own). Empirically every sandbox session in
    # `.umbrella/sandbox_sessions/` shows `edited_files=[]` despite the tool
    # being implemented and policy-allowed; the only fix that actually moves
    # the needle is making it visible in the OpenAI-style `tools=[...]`
    # payload from the very first round.
    "sandbox_self_edit",
    "delegate_to_ouroboros",
    # Intent-aware research tool with per-run budget; preferred for external
    # evidence. ``web_search`` remains available as a provider-independent
    # GMAS adapter but is intentionally not core.
    "deep_search",
    # Project/tool discovery needs to be visible in the first planner
    # rounds. If these stay non-core, phase-filtered schemas mention them
    # in policy text but cannot actually route the calls.
    "github_project_search",
    "github_extract_snippets",
    "mcp_discover",
    "mcp_install",
}


DISABLED_TOOL_NAMES: set[str] = set()


class ToolRegistry:
    """Ouroboros tool registry (SSOT).

    To add a tool: create a module in ouroboros/tools/,
    export get_tools() -> List[ToolEntry].
    """

    def __init__(
        self,
        repo_dir: pathlib.Path,
        drive_root: pathlib.Path,
        host_repo_root: pathlib.Path | None = None,
    ):
        self._entries: dict[str, ToolEntry] = {}
        self._ctx = ToolContext(
            repo_dir=repo_dir,
            drive_root=drive_root,
            host_repo_root=host_repo_root,
        )
        self._load_modules()

    def _load_modules(self) -> None:
        """Auto-discover tool modules in ouroboros/tools/ that export get_tools()."""
        import importlib
        import pkgutil
        import ouroboros.tools as tools_pkg

        enable_experimental_review_tools = str(
            os.environ.get("OUROBOROS_ENABLE_EXPERIMENTAL_REVIEW_TOOLS", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        for _importer, modname, _ispkg in pkgutil.iter_modules(tools_pkg.__path__):
            if modname.startswith("_") or modname == "registry":
                continue
            if modname == "review" and not enable_experimental_review_tools:
                continue
            try:
                mod = importlib.import_module(f"ouroboros.tools.{modname}")
                if hasattr(mod, "get_tools"):
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to load tool module %s", modname, exc_info=True
                )

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def register(self, entry: ToolEntry) -> None:
        """Register a new tool (for extension by Ouroboros)."""
        self._entries[entry.name] = entry

    # --- Contract ---

    def available_tools(self) -> list[str]:
        return [
            e.name for e in self._entries.values() if e.name not in DISABLED_TOOL_NAMES
        ]

    def schemas(self, core_only: bool = False) -> list[dict[str, Any]]:
        if not core_only:
            return [
                {"type": "function", "function": e.schema}
                for e in self._entries.values()
                if e.name not in DISABLED_TOOL_NAMES
            ]
        # Core tools + meta-tools for discovering/enabling extended tools
        result = []
        for e in self._entries.values():
            if e.name in DISABLED_TOOL_NAMES:
                continue
            if e.name in CORE_TOOL_NAMES or e.name in (
                "list_available_tools",
                "enable_tools",
            ):
                result.append({"type": "function", "function": e.schema})
        return result

    def list_non_core_tools(self) -> list[dict[str, str]]:
        """Return name+description of all non-core tools."""
        result = []
        for e in self._entries.values():
            if e.name in DISABLED_TOOL_NAMES:
                continue
            if e.name not in CORE_TOOL_NAMES:
                desc = e.schema.get("description", "No description")
                result.append({"name": e.name, "description": desc})
        return result

    def get_schema_by_name(self, name: str) -> dict[str, Any] | None:
        """Return the full schema for a specific tool."""
        if name in DISABLED_TOOL_NAMES:
            return None
        entry = self._entries.get(name)
        if entry:
            return {"type": "function", "function": entry.schema}
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool (default 120)."""
        entry = self._entries.get(name)
        return entry.timeout_sec if entry is not None else 120

    def set_permission_envelope(self, envelope: Any) -> None:
        """Attach a PermissionEnvelope; checked before every tool call."""
        self._permission_envelope = envelope

    def execute(self, name: str, args: dict[str, Any]) -> str:
        if name in DISABLED_TOOL_NAMES:
            return f"⚠️ TOOL_DISABLED ({name}): temporarily disabled by runtime policy."
        # PermissionEnvelope pre-hook (set by umbrella phase runner)
        envelope = getattr(self, "_permission_envelope", None)
        if envelope is not None:
            paths = []
            for key in ("path", "paths", "working_directory", "file_path", "filepath"):
                val = args.get(key)
                if isinstance(val, str):
                    paths.append(val)
                elif isinstance(val, list):
                    paths.extend(str(p) for p in val)
            cmd = args.get("cmd") or args.get("command") or args.get("script")
            result = envelope.check(name, paths=paths, cmd=str(cmd) if cmd else None)
            if not result:
                return f"⚠️ TOOL_DENIED_BY_ENVELOPE ({name}): {result.reason}"
        entry = self._entries.get(name)
        if entry is None:
            return f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._entries.keys()))}"
        try:
            return entry.handler(self._ctx, **args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"

    def override_handler(self, name: str, handler) -> None:
        """Override the handler for a registered tool (used for closure injection)."""
        entry = self._entries.get(name)
        if entry:
            self._entries[name] = ToolEntry(
                name=entry.name,
                schema=entry.schema,
                handler=handler,
                timeout_sec=entry.timeout_sec,
            )

    @property
    def CODE_TOOLS(self) -> frozenset:
        return frozenset(e.name for e in self._entries.values() if e.is_code_tool)
