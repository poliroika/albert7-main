"""Completion-gate helpers shared by the control-plane tools.

Extracted from ``control.py`` to keep that file under the line cap
enforced by the smoke tests. The gates here are pure functions of a
read-only snapshot the loop publishes onto ``ToolContext.loop_state_view``;
they have no side effects and return a non-empty error string when the
gate fires.

Three gates live here:

- :func:`check_discovery_gate` (Tier 3.1) — refuses
  ``mark_subtask_complete`` for ``domain_unknown`` subtasks until at
  least one external-research/recall tool has been called inside the
  subtask. Universal — no per-workspace tuning.
- :func:`check_planner_discovery_gate` (Tier 3.2) — refuses
  ``propose_task_plan`` when the planner skipped both memory recall and
  external lookups. On by default; disabled by
  ``OUROBOROS_REQUIRE_PLANNER_DISCOVERY=0``.
- :func:`check_verify_evidence_gate` (Tier 1.3) — refuses
  ``mark_subtask_complete`` / ``mark_remediation_complete`` when there
  is no fresh passing ``run_workspace_verify`` evidence for the latest
  workspace writes. Lazy-on: only engages once writes/verify have
  happened. ``OUROBOROS_REQUIRE_VERIFY_EVIDENCE`` lets operators force
  it on or off.
"""

import os
from typing import Any

from ouroboros.tools.registry import ToolContext


def check_discovery_gate(ctx: ToolContext, *, current_subtask: Any) -> str:
    """Return a non-empty error string when discovery is required but missing.

    The gate fires when:
    - The current subtask carries the ``domain_unknown`` tag (set by the
      planner when the work involves an unfamiliar API/library/framework
      and the agent doesn't already have notes about it), AND
    - The agent has not made any discovery tool call within this subtask
      (``current_subtask_discovery_calls == 0`` per ``loop_state_view``).
    - **Additionally** (tightened): if the only discovery call was
      ``get_umbrella_memory`` AND it returned no hits, the gate keeps
      firing until the agent consults at least one *external* source
      (``deep_search``/``github_*``/``mcp_discover``/``web_fetch``).
      Otherwise the agent's pattern of "1 empty memory call →
      mark_subtask_complete" leaves real GitHub / MCP / web sources
      untouched and the work proceeds without prior art.

    Backwards compat: if ``loop_state_view`` is missing or the tags list
    doesn't include ``domain_unknown``, the gate stays silent. This way
    legacy callers and plans that never opted in are unaffected.
    """

    tags = list(getattr(current_subtask, "tags", []) or [])
    if "domain_unknown" not in {t.strip().lower() for t in tags}:
        return ""
    view = getattr(ctx, "loop_state_view", None)
    if not isinstance(view, dict):
        return ""
    calls = int(view.get("current_subtask_discovery_calls") or 0)
    external_calls = int(view.get("current_subtask_external_discovery_calls") or 0)
    memory_empty = bool(view.get("last_memory_recall_empty") or False)
    if calls == 0:
        return (
            "⚠️ mark_subtask_complete: this subtask is tagged `domain_unknown` "
            "but you closed it without consulting any external knowledge. "
            "Before marking done, call ONE of: `get_umbrella_memory(query=...)`, "
            "`deep_search(intent='subtask_evidence', query=...)`, "
            "`github_project_search`, `github_extract_snippets`, "
            "`mcp_discover`, or `web_fetch`. Cite what you found and only "
            "then retry mark_subtask_complete."
        )
    if memory_empty and external_calls == 0:
        return (
            "⚠️ mark_subtask_complete: `get_umbrella_memory` returned no "
            "prior knowledge for this `domain_unknown` subtask, so you "
            "MUST consult at least one external source before closing. "
            "Call ONE of: `deep_search(intent='subtask_evidence', query=...)`, "
            "`github_project_search(query=...)`, `github_extract_snippets`, "
            "`mcp_discover(query=...)`, or `web_fetch(url=...)`. "
            "An empty memory recall is not evidence — it's a signal that "
            "you need to look outside the workspace."
        )
    return ""


def check_planner_discovery_gate(ctx: ToolContext) -> str:
    """Return a non-empty error string when the planner skipped discovery.

    Universal — applies to every workspace. The bar is intentionally low:
    one read/discovery tool call from ``DISCOVERY_TOOL_NAMES`` is enough.
    The intent is to break the "plan from the brief, no context" pattern
    where the agent commits a plan without ever inspecting the workspace
    or memory and then spends remediation rounds discovering basic facts.

    Activation policy:
    - Off by default — set ``OUROBOROS_REQUIRE_PLANNER_DISCOVERY=1`` to
      disable. Scripted tests can opt out when they do not model
      discovery calls.
    - Even when on, stays silent when ``loop_state_view`` is missing
      (we never block without the signal).
    """

    env_val = (
        str(os.environ.get("OUROBOROS_REQUIRE_PLANNER_DISCOVERY") or "").strip().lower()
    )
    if env_val in {"0", "false", "no", "off"}:
        return ""
    view = getattr(ctx, "loop_state_view", None)
    if not isinstance(view, dict) or not view:
        return ""
    if not bool(view.get("discovery_plan_proposed")):
        return (
            "⚠️ propose_task_plan: first call `propose_discovery_plan` with "
            "your own research budget for this task. Declare which phases "
            "will use memory, web/deep_search, GitHub project/snippet search, "
            "MCP discovery, GMAS retrieval, and workspace reads, including "
            "rough call budgets and how useful findings will be reused."
        )
    calls = int(view.get("planner_discovery_calls") or 0)
    external = int(view.get("planner_external_discovery_calls") or 0)
    memory_empty = bool(view.get("last_memory_recall_empty") or False)
    if calls == 0:
        return (
            "⚠️ propose_task_plan: you have not consulted memory or external "
            "research before committing a plan. Before retrying, call ONE of: "
            "`get_umbrella_memory(query=...)` for workspace memory, or an external "
            "lookup (`deep_search`, `github_project_search`, "
            "`github_extract_snippets`, `mcp_discover`, `web_fetch`). "
            "Workspace-file reads alone do not satisfy this gate — the goal is "
            "to surface prior knowledge or external evidence, not just inspect "
            "current files. Then call propose_task_plan again."
        )
    if memory_empty and external == 0:
        return (
            "⚠️ propose_task_plan: `get_umbrella_memory` returned no prior "
            "knowledge for this task, so the plan cannot rest on internal "
            "memory alone. Before retrying, call at least ONE external "
            "research tool: `deep_search(intent='prior_art', query=...)`, "
            "`github_project_search(query=...)`, `mcp_discover(query=...)`, "
            "or `web_fetch(url=...)`. An empty memory recall is a signal "
            "that the project genuinely needs outside input — planning "
            "blindly here is what burns remediation rounds later."
        )
    return ""


def check_verify_evidence_gate(ctx: ToolContext, *, gate_kind: str) -> str:
    """Return a non-empty error string when the verify evidence is stale or red.

    Activation policy (universal — no per-workspace tuning):
    - The gate only engages once **somebody** has run `run_workspace_verify`
      in this loop (``last_verify_round >= 0``) OR the agent has made
      workspace writes that we need to certify
      (``last_write_round >= 0``). This keeps purely-cognitive plans
      (research / planning / discovery only) unblocked while making the
      gate bite as soon as real work is being done.
    - Setting ``OUROBOROS_REQUIRE_VERIFY_EVIDENCE=1`` forces the gate on
      for every ``mark_*_complete`` call regardless of write/verify state.
      Setting it to ``0`` disables the gate entirely (escape hatch for
      operators who explicitly opt out).

    Required invariants when active:
    - ``last_verify_run_id`` is set (i.e. ``run_workspace_verify`` produced
      a structured report — not a skipped/errored run).
    - ``last_verify_passed`` is True.
    - ``last_verify_failed_count == 0`` (defence in depth).
    - The verify ran *after* the most recent workspace write
      (``last_verify_round > last_write_round``). Stale verify can't
      certify code the agent wrote afterwards.

    Backwards compat: if ``loop_state_view`` is empty (legacy callers,
    unit tests not exercising the loop), the gate stays silent.
    """

    view = getattr(ctx, "loop_state_view", None)
    if not isinstance(view, dict) or not view:
        return ""

    override = (
        str(os.environ.get("OUROBOROS_REQUIRE_VERIFY_EVIDENCE") or "").strip().lower()
    )
    if override in {"0", "false", "no", "off"}:
        return ""
    force_on = override in {"1", "true", "yes", "on"}

    last_write_round = int(view.get("last_write_round") or -1)
    last_verify_round = int(view.get("last_verify_round") or -1)

    if not force_on and last_write_round < 0 and last_verify_round < 0:
        # Pure discovery / cognitive flow — never wrote anything, never
        # verified anything. Closure is allowed without verify proof.
        return ""

    if not view.get("last_verify_run_id"):
        return (
            f"⚠️ {gate_kind}: no fresh verify evidence in this run. Call "
            "`run_workspace_verify(workspace_id=...)` (and pass any "
            "failures back into remediation) before closing."
        )
    if not view.get("last_verify_passed"):
        failed = int(view.get("last_verify_failed_count") or 0)
        return (
            f"⚠️ {gate_kind}: the latest `run_workspace_verify` reported "
            f"{failed} failed required step(s). Fix the failures and "
            "rerun `run_workspace_verify` so it passes before closing."
        )
    if int(view.get("last_verify_failed_count") or 0) > 0:
        return (
            f"⚠️ {gate_kind}: the latest verify still has failed required "
            "step(s). Fix them and rerun `run_workspace_verify`."
        )
    if last_write_round > last_verify_round:
        return (
            f"⚠️ {gate_kind}: workspace was modified (round "
            f"{last_write_round}) after the last passing verify (round "
            f"{last_verify_round}). Rerun `run_workspace_verify` so the "
            "evidence reflects the current code, then retry."
        )
    return ""


__all__ = [
    "check_discovery_gate",
    "check_planner_discovery_gate",
    "check_verify_evidence_gate",
]
