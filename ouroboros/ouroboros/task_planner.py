"""Adaptive Task Planner — universal subtask decomposition for the loop.

Why this module exists
----------------------
Before this module, ``run_llm_loop`` was a single linear thread of
LLM-rounds: the agent received TASK_MAIN and worked on everything at
once until it produced a final answer. For non-trivial tasks this is
fragile — context grows unbounded, partial progress isn't anchored,
and every restart re-derives intent from scratch.

The planner adds three orthogonal capabilities, *without* hard-coding
heuristics for any specific domain (web apps, ML, data ETL, ...):

1. **Upfront decomposition.** A dedicated "planner round" asks the LLM
   to call ``propose_task_plan`` and return a structured list of
   subtasks (title + description + success_check). The plan is the
   contract — a small JSON document persisted to ``drive/task_plans/``.

2. **Sequential execution with a focus block.** The orchestrator walks
   the plan one subtask at a time, injecting a ``[SUBTASK i/N]`` system
   message before each phase so the LLM is anchored on a single goal.
   The phase ends when the LLM calls ``mark_subtask_complete``.

3. **Adaptive replanning.** After each subtask, a short review phase
   lets the LLM call ``revise_remaining_plan`` with a new tail. The
   number of revisions is capped to prevent oscillations.

Universality is preserved by making this module zero-dependency on
Umbrella internals: the prompt and the data format are domain-agnostic,
and a planner-mode env flag (``OUROBOROS_PLANNER_MODE``) can disable
it entirely (CI, lightweight chat tasks, etc.).

The plan file is the canonical state. ``HierarchicalMemory`` mirrors
each finished subtask as a recall record so cross-task and cross-run
memory can find prior progress, but the orchestrator never reads back
from memory to resume — it always reads the plan file.
"""

import json
import logging
import os
import pathlib
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config (env-driven, kept here so planner & loop see the same values)
# ---------------------------------------------------------------------------

PLANNER_MODE_AUTO = "auto"
PLANNER_MODE_ALWAYS = "always"
PLANNER_MODE_OFF = "off"
_VALID_MODES = {PLANNER_MODE_AUTO, PLANNER_MODE_ALWAYS, PLANNER_MODE_OFF}

# Threshold (chars) for the ``auto`` mode: tasks shorter than this are
# treated as conversational and bypass the planner entirely.
AUTO_MIN_TASK_CHARS_DEFAULT = 220


def planner_mode() -> str:
    raw = (
        (os.environ.get("OUROBOROS_PLANNER_MODE") or PLANNER_MODE_AUTO).strip().lower()
    )
    return raw if raw in _VALID_MODES else PLANNER_MODE_AUTO


def planner_max_steps() -> int:
    try:
        value = int(os.environ.get("OUROBOROS_PLANNER_MAX_STEPS", "7"))
    except (TypeError, ValueError):
        value = 7
    return max(1, min(value, 20))


def planner_replan_limit() -> int:
    try:
        value = int(os.environ.get("OUROBOROS_PLANNER_REPLAN_LIMIT", "3"))
    except (TypeError, ValueError):
        value = 3
    return max(0, min(value, 20))


def planner_phase_round_cap() -> int:
    """Soft per-subtask round cap. ``0`` explicitly means "use global cap"."""
    raw = os.environ.get("OUROBOROS_PLANNER_PHASE_ROUNDS")
    if raw is None:
        # Subtasks must not inherit an unbounded ``--max-rounds 0`` run by
        # default. The cap is soft: the loop now re-enters the same subtask
        # with a rescue prompt instead of marking it skipped.
        return 12
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 20
    return max(0, value)


def planner_remediation_round_cap() -> int:
    try:
        value = int(os.environ.get("OUROBOROS_PLANNER_REMEDIATION_ROUNDS", "24"))
    except (TypeError, ValueError):
        value = 24
    return max(4, min(value, 80))


def planner_initial_round_cap() -> int:
    """Round cap for the initial plan-creation phase.

    Models often inspect the workspace before they are ready to call
    ``propose_task_plan``. Keep this finite so planner mode can still fall
    back, but make it large enough for a read/list/read/plan sequence.
    """
    raw = os.environ.get("OUROBOROS_PLANNER_INITIAL_ROUNDS")
    if raw is None:
        raw = os.environ.get("OUROBOROS_PLANNER_PHASE_ROUNDS", "6")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 6
    return max(2, min(value, 20))


def planner_review_round_cap() -> int:
    try:
        value = int(os.environ.get("OUROBOROS_PLANNER_REVIEW_ROUNDS", "2"))
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 10))


def planner_auto_min_chars() -> int:
    try:
        value = int(
            os.environ.get(
                "OUROBOROS_PLANNER_AUTO_MIN_CHARS", str(AUTO_MIN_TASK_CHARS_DEFAULT)
            )
        )
    except (TypeError, ValueError):
        value = AUTO_MIN_TASK_CHARS_DEFAULT
    return max(0, value)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SUBTASK_STATUS_PENDING = "pending"
SUBTASK_STATUS_IN_PROGRESS = "in_progress"
SUBTASK_STATUS_DONE = "done"
SUBTASK_STATUS_FAILED = "failed"
SUBTASK_STATUS_SKIPPED = "skipped"
TERMINAL_STATUSES = {SUBTASK_STATUS_DONE, SUBTASK_STATUS_FAILED, SUBTASK_STATUS_SKIPPED}


@dataclass
class Subtask:
    id: str
    title: str
    description: str
    success_check: str = ""
    status: str = SUBTASK_STATUS_PENDING
    started_at: float = 0.0
    completed_at: float = 0.0
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    subtask_task_id: str = ""
    # Tier 3.1: free-form tags the planner can attach to gate behaviour.
    # Recognised values today:
    #   - ``domain_unknown`` -> completion gate requires at least one
    #     discovery tool call (deep_search / github_* / mcp_discover /
    #     get_umbrella_memory / web_fetch) before mark_subtask_complete.
    # Unknown tags are accepted silently for forward compatibility.
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Subtask":
        return cls(
            id=str(payload.get("id") or _new_subtask_id()),
            title=str(payload.get("title") or "").strip()[:240],
            description=str(payload.get("description") or "").strip(),
            success_check=str(payload.get("success_check") or "").strip(),
            status=str(payload.get("status") or SUBTASK_STATUS_PENDING),
            started_at=float(payload.get("started_at") or 0.0),
            completed_at=float(payload.get("completed_at") or 0.0),
            summary=str(payload.get("summary") or ""),
            evidence=list(payload.get("evidence") or []),
            subtask_task_id=str(payload.get("subtask_task_id") or ""),
            tags=[
                str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()
            ],
        )


@dataclass
class TaskPlan:
    task_id: str
    workspace_id: str
    objective_digest: str
    subtasks: list[Subtask] = field(default_factory=list)
    delivery_contract: dict[str, Any] = field(default_factory=dict)
    cursor: int = 0
    revisions: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "objective_digest": self.objective_digest,
            "delivery_contract": dict(self.delivery_contract or {}),
            "subtasks": [s.to_dict() for s in self.subtasks],
            "cursor": self.cursor,
            "revisions": self.revisions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskPlan":
        return cls(
            task_id=str(payload.get("task_id") or ""),
            workspace_id=str(payload.get("workspace_id") or ""),
            objective_digest=str(payload.get("objective_digest") or ""),
            subtasks=[Subtask.from_dict(s) for s in (payload.get("subtasks") or [])],
            delivery_contract=dict(payload.get("delivery_contract") or {}),
            cursor=int(payload.get("cursor") or 0),
            revisions=int(payload.get("revisions") or 0),
            created_at=float(payload.get("created_at") or 0.0),
            updated_at=float(payload.get("updated_at") or 0.0),
        )

    def current(self) -> Subtask | None:
        if 0 <= self.cursor < len(self.subtasks):
            return self.subtasks[self.cursor]
        return None

    def remaining(self) -> list[Subtask]:
        return list(self.subtasks[self.cursor :])

    def is_complete(self) -> bool:
        return self.cursor >= len(self.subtasks)


@dataclass(frozen=True)
class PlanExecutionContext:
    active_plan_id: str
    plan_store_root: str
    task_id: str
    phase: str = ""
    subtask_id: str = ""


def _new_subtask_id() -> str:
    return f"st_{uuid.uuid4().hex[:8]}"


_INTERACTIVE_LAUNCH_RE = re.compile(
    r"(?i)\b(python(?:3)?\s+(?:main|app|game|run|play)\.py|python(?:3)?\s+-m\s+(?:main|app|game|play))\b"
)
_IMPLEMENTATION_SUBTASK_RE = re.compile(
    r"(?i)\b(implement|implementation|build|create|write|develop|code|realiz|feature)\b"
)


def _success_check_interactive_launch_hint(success_check: str) -> bool:
    text = (success_check or "").strip()
    if not text:
        return False
    return bool(_INTERACTIVE_LAUNCH_RE.search(text))


def _subtask_prefers_write_tools(title: str, description: str) -> bool:
    text = f"{title}\n{description}".strip()
    if not text:
        return False
    return bool(_IMPLEMENTATION_SUBTASK_RE.search(text))


# ---------------------------------------------------------------------------
# Storage (atomic JSON, mirrors umbrella.orchestration.status pattern)
# ---------------------------------------------------------------------------


class TaskPlanStore:
    """File-backed canonical store of ``TaskPlan`` keyed by ``task_id``."""

    def __init__(self, drive_root: pathlib.Path):
        self.root = pathlib.Path(drive_root) / "task_plans"

    def path_for(self, task_id: str) -> pathlib.Path:
        safe = (
            "".join(ch for ch in (task_id or "default") if ch.isalnum() or ch in "-_")
            or "default"
        )
        return self.root / f"{safe}.json"

    def load(self, task_id: str) -> TaskPlan | None:
        path = self.path_for(task_id)
        if not path.exists():
            return None
        # Retry briefly: on Windows the destination may be locked for a few
        # ms during concurrent ``tmp.replace(path)`` operations.
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (PermissionError, OSError, json.JSONDecodeError) as exc:
                last_exc = exc
                time.sleep(0.02 * (attempt + 1))
                continue
            if not isinstance(payload, dict):
                return None
            return TaskPlan.from_dict(payload)
        log.warning(
            "TaskPlanStore: failed to parse %s after retries (%s)", path, last_exc
        )
        return None

    def save(self, plan: TaskPlan) -> None:
        plan.updated_at = time.time()
        if not plan.created_at:
            plan.created_at = plan.updated_at
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(plan.task_id)
        body = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    # Last-resort fallback when AV/indexer briefly locks the destination.
                    path.write_text(body, encoding="utf-8")
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    return
                time.sleep(0.05 * (attempt + 1))

    def archive(self, task_id: str, *, reason: str = "archived") -> pathlib.Path | None:
        """Move the live plan for ``task_id`` aside so the next run plans
        from scratch, while keeping the previous plan as audit history.

        Used by the verification remediation loop: when we re-submit the
        SAME ``task_id`` with a "fix what verification flagged" prompt,
        we MUST clear the cached completed plan first. Otherwise
        ``plan.is_complete()`` short-circuits the subtask phase, drops
        straight into ``final_aggregation`` with ``tool_schemas=[]``, and
        the model gets the failure context but cannot call a single tool
        to fix anything.

        Returns the new archive path on success, ``None`` if there was
        nothing to archive or the rename failed.
        """
        path = self.path_for(task_id)
        if not path.exists():
            return None
        suffix = (
            "".join(ch for ch in (reason or "archived") if ch.isalnum() or ch in "-_")[
                :48
            ]
            or "archived"
        )
        archive = path.with_name(
            f"{path.stem}.{suffix}.{int(time.time())}.{time.time_ns() % 1_000_000}.json"
        )
        for attempt in range(5):
            try:
                path.rename(archive)
                return archive
            except (PermissionError, OSError):
                if attempt == 4:
                    try:
                        path.unlink()
                    except OSError:
                        return None
                    return None
                time.sleep(0.05 * (attempt + 1))
        return None

    def create_from_steps(
        self,
        *,
        task_id: str,
        workspace_id: str,
        objective_digest: str,
        steps: list[dict[str, Any]],
        delivery_contract: dict[str, Any] | None = None,
    ) -> TaskPlan:
        subtasks = [
            Subtask(
                id=_new_subtask_id(),
                title=str(s.get("title") or f"Step {idx + 1}").strip()[:240],
                description=str(s.get("description") or "").strip(),
                success_check=str(s.get("success_check") or "").strip(),
                tags=[str(t).strip() for t in (s.get("tags") or []) if str(t).strip()],
            )
            for idx, s in enumerate(steps)
            if isinstance(s, dict)
        ]
        if not subtasks:
            raise ValueError("propose_task_plan requires at least one step")
        plan = TaskPlan(
            task_id=task_id or "default",
            workspace_id=workspace_id or "",
            objective_digest=objective_digest[:2000],
            subtasks=subtasks,
            delivery_contract=dict(delivery_contract or {}),
        )
        self.save(plan)
        return plan

    def apply_revision(
        self,
        plan: TaskPlan,
        *,
        replacement_steps_for_remaining: list[dict[str, Any]],
        reason: str,
    ) -> TaskPlan:
        if not isinstance(replacement_steps_for_remaining, list):
            raise ValueError("revision steps must be a list")
        head = plan.subtasks[: plan.cursor]
        tail = [
            Subtask(
                id=_new_subtask_id(),
                title=str(s.get("title") or f"Step {plan.cursor + idx + 1}").strip()[
                    :240
                ],
                description=str(s.get("description") or "").strip(),
                success_check=str(s.get("success_check") or "").strip(),
                tags=[str(t).strip() for t in (s.get("tags") or []) if str(t).strip()],
            )
            for idx, s in enumerate(replacement_steps_for_remaining)
            if isinstance(s, dict)
        ]
        plan.subtasks = head + tail
        plan.revisions += 1
        plan.objective_digest = (
            plan.objective_digest + f"\n[revision#{plan.revisions}] {reason}".strip()
        )[:4000]
        self.save(plan)
        return plan

    def start_current(self, plan: TaskPlan) -> Subtask | None:
        cur = plan.current()
        if cur is None:
            return None
        cur.status = SUBTASK_STATUS_IN_PROGRESS
        cur.started_at = time.time()
        self.save(plan)
        return cur

    def complete_current(
        self,
        plan: TaskPlan,
        *,
        status: str,
        summary: str,
        evidence: list[str] | None = None,
    ) -> Subtask | None:
        cur = plan.current()
        if cur is None:
            return None
        if status not in TERMINAL_STATUSES:
            status = SUBTASK_STATUS_DONE
        cur.status = status
        cur.summary = summary.strip()[:2000]
        cur.evidence = [
            str(e).strip()[:400] for e in (evidence or []) if str(e).strip()
        ][:10]
        cur.completed_at = time.time()
        plan.cursor += 1
        self.save(plan)
        return cur

    def fail_current(self, plan: TaskPlan, *, reason: str) -> Subtask | None:
        return self.complete_current(
            plan,
            status=SUBTASK_STATUS_SKIPPED,
            summary=f"Phase round cap reached, subtask auto-skipped. Reason: {reason}",
            evidence=[],
        )


# ---------------------------------------------------------------------------
# Prompts and inline blocks
# ---------------------------------------------------------------------------


def planner_system_prompt(task_main_text: str) -> str:
    """System prompt for the dedicated planner round.

    Designed to be domain-agnostic. The LLM must call
    ``propose_task_plan`` with 1..N concise steps. We strongly nudge
    "small list of independent steps with explicit success_check"
    rather than hard-coding patterns.
    """
    digest = (task_main_text or "").strip()
    if len(digest) > 4000:
        digest = digest[:4000].rstrip() + "\n…[truncated]"

    cap = planner_max_steps()
    return (
        "[PLANNER PHASE]\n"
        "You are about to start work on the task below. Before doing any "
        "real work, you MUST decompose it into a short, ordered plan of "
        "subtasks by first calling `propose_discovery_plan`, then calling "
        "the tool `propose_task_plan` exactly once.\n\n"
        f"Plan rules:\n"
        f"- Return between 1 and {cap} subtasks. Use 1 only if the task is truly trivial.\n"
        "- You may make a small number of read-only discovery tool calls first "
        "when the workspace context is genuinely unclear (for example list/read "
        "workspace docs or query memory). Do not write files or execute project "
        "work before the plan exists.\n"
        "- **Discovery plan first:** call `propose_discovery_plan` before "
        "`propose_task_plan`. In it, decide for yourself which phases/subtasks "
        "will use memory, web/deep_search, GitHub project/snippet search, MCP "
        "discovery, GMAS retrieval, and workspace reads; include rough max_calls "
        "and how useful findings will be reused as ideas, lessons, snippets, or "
        "code references. This is a strategy contract, not a rigid checklist.\n"
        "- If you do not have enough domain context to plan well, you may call "
        '`deep_search(intent="planner_research", query=...)` to fetch external '
        "evidence; do not search for trivia you already know. If the user is "
        "asking for something that is likely a well-known open-source project, "
        "you may call `github_project_search` with a small repo budget and "
        "`github_extract_snippets` on the most relevant permissively licensed "
        "repos. Prefer implementation files and examples over README-only "
        "summaries. Persist findings in workspace knowledge — never copy code "
        "blindly.\n"
        "- **Discovery before planning (invariant):** the very first useful "
        "action in this phase should be either `get_umbrella_memory(query=...)` "
        "to surface prior workspace lessons / failure patterns, or one "
        "external lookup (`deep_search`, `github_project_search`, "
        "`github_extract_snippets`, `mcp_discover`, `web_fetch`). Calling "
        "`propose_task_plan` without any such recall is fragile — you will "
        "miss known constraints. Workspace-file reads alone (`list_workspace_files`, "
        "`read_workspace_file`) do not substitute for memory/research; they "
        "tell you what files exist, not what mistakes were already made.\n"
        "- Always consider memory and tool discovery before finalizing the plan: "
        "call `mcp_discover` when an MCP/server capability might solve part "
        "of the task. If a discovered server is plausibly useful, call "
        "`mcp_install` to register it as a disabled candidate for user approval; "
        "if none are useful, say why in the discovery plan. Use web/GitHub "
        "search for current APIs, unfamiliar libraries, or prior-art "
        "implementation patterns.\n"
        "- **MCP evaluation rule:** for tasks that produce or inspect external "
        "artifacts (presentations/PPTX, documents, PDFs, images, browser output, "
        "databases, cloud APIs, design files, dashboards), explicitly evaluate "
        "whether an MCP server could improve the result. For presentation/doc "
        "generation, look for MCP candidates that can help with template "
        "inspection, rendering, office file conversion, visual QA, storage, or "
        "artifact validation. If useful, register a disabled candidate with "
        "`mcp_install`; if not useful, record the skip reason in the discovery "
        "plan so the decision is intentional, not accidental.\n"
        "- Each subtask must have: a short title, a self-contained description, "
        "and an explicit `success_check` describing what proves it is done. "
        "For implementation subtasks, include `acceptance_command: <command>` "
        "or a concrete file/signature check; prose-only success checks are invalid.\n"
        "- **Delivery contract:** `propose_task_plan` must include a top-level "
        "`delivery_contract` object with `outcome`, `proof` (or "
        "`acceptance_command`), and `artifact`/`expected_result` when relevant. "
        "The proof must exercise user-facing behavior (CLI/API/artifact/service/test), "
        "not merely import modules, compile files, or inspect function signatures. "
        "Plan at least one vertical-slice subtask that makes this contract runnable.\n"
        "- If a subtask depends on an unfamiliar API/library/current web facts, "
        'add `tags: ["domain_unknown"]`; that tag requires a memory/web/GitHub/MCP '
        "discovery call before the subtask can be marked complete.\n"
        "- Structure the plan in this strict order: "
        "(1) implementation plan/setup, "
        "(2) full feature code implementation, "
        "(3) test-fix/refactor pass, "
        "(4) **mandatory self-verification subtask** — run the workspace "
        "acceptance command(s) yourself and inspect the output BEFORE the "
        "harness verification runs. The success_check for this subtask MUST "
        "be the literal acceptance command from `workspace.toml` "
        "(or `python -m pytest -q` / equivalent). Only mark this subtask "
        "`done` when the command exits 0 in your own `run_workspace_command`. "
        "If the harness then later reports a verification failure, you will "
        "remain in the SAME run, write a diagnosis to memory via "
        '`record_idea(kind="lesson", ...)`, fix only the failing checks, '
        "self-verify again, and resubmit. There is no fresh-start retry — "
        "the round counter and task id stay the same across the entire "
        "fix-verify-fix cycle.\n"
        "- During implementation subtasks, prioritize write tools to create/update "
        "real project files before heavy validation loops.\n"
        "- Plan normal code layout: application/library code in `src/` or the "
        "existing package, tests in `tests/`, stable entrypoints/config/docs in "
        "the workspace root. Do not plan one-off diagnostic scripts or generated "
        "verification debris as deliverables.\n"
        "- Order subtasks so each one can be tackled in isolation given the previous results.\n"
        "- Prefer concrete deliverables over vague exploration steps.\n"
        "- Once you have enough context, call `propose_task_plan`; do not answer "
        "with prose-only plans.\n"
        "- After the plan is accepted, you will execute subtasks one at a time, "
        "and you can revise the remaining tail later via `revise_remaining_plan`.\n\n"
        "[EDITING_POLICY]\n"
        "During implementation phases, prefer `apply_workspace_patch` for "
        "targeted edits to existing files after `read_workspace_file`; use "
        "`update_workspace_seed` mainly for new files or intentional full rewrites.\n\n"
        "[EXAMPLE_SUBTASK_SHAPE]\n"
        "One implementation step might look like:\n"
        "  title: Add graph module and smoke test\n"
        "  description: Implement minimal runnable graph; wire runner from TASK_MAIN.\n"
        "  success_check: acceptance_command: cd workspaces/<id> && python -m pytest tests/ -q\n"
        "(Replace paths/commands with real workspace-relative commands.)\n"
        "[END_EXAMPLE]\n\n"
        "[EXACT_TOOL_CALL_SHAPE]\n"
        "First, call `propose_discovery_plan` with arguments like:\n"
        "{\n"
        '  "intent": "...",\n'
        '  "phases": [\n'
        '    {"phase": "planner", "sources": ["memory", "web", "github"], "max_calls": 4}\n'
        "  ],\n"
        '  "reuse_policy": "record useful findings with record_idea; save verified lessons after passing verify"\n'
        "}\n\n"
        "The `propose_task_plan` tool MUST receive a single argument named "
        "`steps` whose value is a JSON array, plus `delivery_contract`. "
        "Do NOT use `subtasks`, `plan`, "
        "`tasks`, `items` or a bare object — the harness will reject the "
        "call with a preflight error and you will burn a rescue round.\n"
        "Valid JSON example for tool arguments:\n"
        "{\n"
        '  "delivery_contract": {\n'
        '    "outcome": "user can run the project and get the requested result",\n'
        '    "proof": "acceptance_command: <real smoke command>",\n'
        '    "expected_result": "observable output/artifact/service response"\n'
        "  },\n"
        '  "steps": [\n'
        '    {"title": "...", "description": "...", "success_check": "..."}\n'
        "  ]\n"
        "}\n"
        "[END_EXACT_TOOL_CALL_SHAPE]\n\n"
        "[CAPABILITY-GAP ESCAPE HATCHES]\n"
        "If during execution you discover that the harness itself is the "
        "blocker — a guard rejects valid input, a tool can't parse a file "
        "format, an umbrella/ helper has the wrong default — you have two "
        "self-improvement tools available without leaving the loop:\n"
        "  - `sandbox_self_edit(file_path, new_content, reason)` — patch "
        "your own code under `ouroboros/` or `umbrella/`. The change persists "
        "after the task ends, so use it carefully for real harness bugs, "
        "NOT for workspace "
        "deliverables (workspace files go through `commit_workspace_changes`).\n"
        "  - `delegate_to_ouroboros(task_description=...)` — queue a separate "
        "self-improvement task that will land a permanent fix. Use only when a "
        "sandbox patch is not enough and the gap deserves to persist.\n"
        "Plan around these honestly: if you anticipate a likely capability "
        "gap (e.g. unusual input format), it is fine to mention the fallback "
        "in the relevant subtask's `description` instead of pretending the "
        "harness is perfect.\n\n"
        "[TASK_MAIN_DIGEST]\n"
        f"{digest}\n"
        "[END_TASK_MAIN_DIGEST]"
    )


_INVENTORY_TOP_N: int = 15
_INVENTORY_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".memory",
        ".umbrella",
        "node_modules",
        "dist",
        "build",
        ".gmas",
    }
)


def _workspace_inventory_section(workspace_root: Path | None) -> str:
    """Return a compact ``[WORKSPACE_INVENTORY]`` block for ``focus_block``.

    Top entries from the workspace root + ``src/`` + ``tests/``. Empty
    when the path is missing or unreadable. The inventory is read-only
    metadata — its purpose is to let the agent see where files belong
    without having to call ``list_workspace_files`` for every subtask.
    """

    if not workspace_root or not isinstance(workspace_root, Path):
        return ""
    try:
        if not workspace_root.is_dir():
            return ""
    except OSError:
        return ""

    def _scan(target: Path, *, top: int) -> list[str]:
        if not target.is_dir():
            return []
        try:
            entries = []
            for child in target.iterdir():
                if child.name in _INVENTORY_EXCLUDED_DIRS:
                    continue
                if child.name.startswith("."):
                    continue
                entries.append(child)
        except OSError:
            return []
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        rendered = []
        for child in entries[:top]:
            tag = "dir " if child.is_dir() else "file"
            try:
                rel = child.relative_to(workspace_root).as_posix()
            except ValueError:
                rel = child.name
            rendered.append(f"  {tag} {rel}")
        return rendered

    sections = []
    root_listing = _scan(workspace_root, top=_INVENTORY_TOP_N)
    if root_listing:
        sections.append("workspace root:\n" + "\n".join(root_listing))
    for sub in ("src", "tests"):
        sub_listing = _scan(workspace_root / sub, top=_INVENTORY_TOP_N)
        if sub_listing:
            sections.append(f"{sub}/:\n" + "\n".join(sub_listing))
    if not sections:
        return ""
    return "[WORKSPACE_INVENTORY]\n" + "\n\n".join(sections)


def _noise_detected_section(noise_paths: list[str] | None) -> str:
    """Render a ``[NOISE_DETECTED]`` section from a prior final_sweep run."""

    if not noise_paths:
        return ""
    items = [p for p in noise_paths if isinstance(p, str) and p.strip()]
    if not items:
        return ""
    bullets = "\n".join(f"  - {p}" for p in items[:_INVENTORY_TOP_N])
    extra = ""
    if len(items) > _INVENTORY_TOP_N:
        extra = f"\n  …and {len(items) - _INVENTORY_TOP_N} more"
    return (
        "[NOISE_DETECTED]\n"
        "Final-sweep flagged ad-hoc / debug files in the workspace root. "
        "Move them to src/scripts/ (if useful) or delete them before "
        "marking this subtask done. Block-level noise FAILS verification.\n"
        f"{bullets}{extra}"
    )


def focus_block(
    plan: TaskPlan,
    *,
    workspace_root: Path | None = None,
    noise_paths: list[str] | None = None,
) -> str:
    cur = plan.current()
    if cur is None:
        return ""
    total = len(plan.subtasks)
    idx = plan.cursor + 1
    completed = [
        s for s in plan.subtasks[: plan.cursor] if s.status in TERMINAL_STATUSES
    ]
    completed_brief = (
        "\n".join(
            f"  {i + 1}. [{s.status}] {s.title} — {s.summary[:160] or '(no summary)'}"
            for i, s in enumerate(completed)
        )
        or "  (none yet)"
    )
    inventory_section = _workspace_inventory_section(workspace_root)
    noise_section = _noise_detected_section(noise_paths)
    launch_guard = ""
    if _success_check_interactive_launch_hint(cur.success_check):
        launch_guard = (
            "\n\n[NON_INTERACTIVE_VALIDATION_RULE]\n"
            "This subtask references an interactive launch command in success_check. "
            "Do NOT execute local app/game launches (for example `python main.py`). "
            "Use non-interactive evidence only: tests, deterministic CLI checks, "
            "import checks, and workspace verification output."
        )
    write_guard = ""
    if _subtask_prefers_write_tools(cur.title, cur.description):
        write_guard = (
            "\n\n[PREFERRED_WRITE_TOOLS]\n"
            "This is an implementation-heavy subtask. Prioritize workspace write tools "
            "to produce concrete code changes before broad read-only exploration. "
            "For existing files, call `read_workspace_file` first and prefer "
            "`apply_workspace_patch`; use `update_workspace_seed` for new files "
            "or intentional full rewrites."
        )
    return (
        f"[SUBTASK {idx}/{total}] FOCUS\n"
        f"Title: {cur.title}\n"
        f"Description: {cur.description}\n"
        f"Success check: {cur.success_check or '(use your judgement)'}\n\n"
        "Work on THIS subtask only. Use only tools exposed in THIS phase schema. "
        "Do not call tools that are not listed in the active schema. "
        "If a tool call returns TOOL_PREFLIGHT_ERROR, TOOL_ARG_ERROR, or another "
        "tool error, read that tool result and correct your next tool call or "
        "implementation; do not summarize or skip the subtask because of a tool "
        "error. "
        "Use tools to implement and verify concrete progress for this subtask. "
        "If the subtask touches prior failures, verification policy, memory, "
        "or a subsystem that may have workspace lessons, call "
        "`get_umbrella_memory` with a specific query before repeating work. "
        "For non-trivial implementation, especially unfamiliar libraries/APIs, "
        "architecture patterns, or similar-project questions, external prior-art "
        'discovery is part of good engineering: use `deep_search(intent="subtask_evidence", ...)`, '
        "`github_project_search`/`github_extract_snippets`, `mcp_discover` "
        "(followed by `mcp_install` for a useful candidate), or `web_fetch` "
        "early unless you can name why local memory/context is enough. "
        "For artifact-heavy work (PPTX/doc/PDF/image/browser/database/cloud), "
        "MCP is a first-class option: evaluate `mcp_discover` before assuming "
        "local libraries are enough, then either install a useful disabled "
        "candidate or cite why MCP was skipped. "
        "Do not copy code blindly; use sources as references and persist useful "
        'findings with `record_idea(evidence_kind="observation_from_log")` until '
        "verification proves them. "
        "When (and only when) "
        "the success check is satisfied — or new evidence proves the planned "
        "step is obsolete/unfinishable — "
        "call `mark_subtask_complete` with a concise summary and concrete evidence "
        "(file paths, command outputs, urls). If `success_check` names an "
        "`acceptance_command`, evidence must include that command's exit 0 result. "
        "When you learn a reusable fix, persist it with `record_idea` while "
        "unverified or `save_umbrella_lesson` after verification proves it. "
        "That call ends this phase."
        f"{launch_guard}{write_guard}\n\n"
        "Completed so far:\n"
        f"{completed_brief}"
        + (f"\n\n{inventory_section}" if inventory_section else "")
        + (f"\n\n{noise_section}" if noise_section else "")
    )


def review_block(plan: TaskPlan, last_subtask: Subtask | None) -> str:
    remaining = plan.remaining()
    remaining_brief = (
        "\n".join(
            f"  {plan.cursor + i + 1}. {s.title}: {s.description[:200]}"
            for i, s in enumerate(remaining)
        )
        or "  (no more steps)"
    )
    last_label = (
        f"[{last_subtask.status}] {last_subtask.title} — {last_subtask.summary[:240] or '(no summary)'}"
        if last_subtask
        else "(none)"
    )
    cap = planner_replan_limit()
    return (
        "[REVIEW PHASE]\n"
        f"Just completed: {last_label}\n\n"
        "Remaining plan tail:\n"
        f"{remaining_brief}\n\n"
        "Allowed tool in this phase: `revise_remaining_plan` only.\n"
        "If the remaining plan is still right, do nothing — emit a brief text reply "
        "(about one sentence, under ~40 words, no tool calls) to advance to the next subtask.\n"
        "If new evidence makes the tail wrong (missing step, useless step, "
        "wrong order), call `revise_remaining_plan` exactly once with the "
        "new tail. Do not edit completed steps.\n"
        "Review also checks discovery quality: if the task is non-trivial and "
        "the implementation has no web/GitHub/MCP/prior-art basis where one was "
        "needed, revise the remaining plan to add `deep_search`, "
        "`github_project_search` / `github_extract_snippets`, `mcp_discover`, "
        "or `web_fetch` before more code.\n"
        f"Revisions used: {plan.revisions}/{cap}."
    )


def plan_progress_block(plan: TaskPlan) -> str:
    """Summary block injected on resume so the loop continues coherently."""
    if not plan.subtasks:
        return ""
    lines = [
        f"[PLAN_PROGRESS] task_id={plan.task_id} cursor={plan.cursor}/{len(plan.subtasks)}"
    ]
    for idx, s in enumerate(plan.subtasks):
        marker = ">" if idx == plan.cursor else " "
        body = (s.summary or s.description)[:200]
        lines.append(f" {marker} {idx + 1}. [{s.status}] {s.title} — {body}")
    lines.append(
        "Resume from the marked step. Do NOT call `propose_task_plan` again — "
        "the plan already exists. Use `revise_remaining_plan` if the tail is wrong."
    )
    return "\n".join(lines)


def remediation_block(plan: TaskPlan) -> str:
    failing = [
        (idx + 1, subtask)
        for idx, subtask in enumerate(plan.subtasks)
        if subtask.status in {SUBTASK_STATUS_FAILED, SUBTASK_STATUS_SKIPPED}
    ]
    failing_brief = (
        "\n".join(
            (
                f"  {idx}. [{subtask.status}] {subtask.title}\n"
                f"     success_check: {subtask.success_check or '(use judgement)'}\n"
                f"     previous_summary: {subtask.summary[:300] or '(none)'}"
            )
            for idx, subtask in failing
        )
        or "  (none)"
    )
    return (
        "[REMEDIATION PHASE]\n"
        "The plan reached the end, but one or more subtasks were skipped or failed. "
        "Do not write a final answer yet. Use workspace write/test tools to fix the "
        "missed work or revise the remaining plan if the original subtask is invalid.\n\n"
        "Previously skipped/failed subtasks:\n"
        f"{failing_brief}\n\n"
        "Rules:\n"
        "- Prefer concrete writes and deterministic verification over more inspection.\n"
        "- Use `get_umbrella_memory` for similar prior verification failures before "
        "trying the same fix again.\n"
        "- If the failure is an unfamiliar dependency/API mismatch, obscure error, "
        "or flaky behavior, use one targeted external lookup (`deep_search`, "
        "`github_project_search`, `mcp_discover`, or `web_fetch`) and cite it in "
        "your evidence; do not search by habit when the fix is obvious.\n"
        "- Re-run relevant checks after each fix.\n"
        "- If a fix teaches a reusable lesson, record it with `record_idea` "
        "while diagnosing and `save_umbrella_lesson` once verification passes.\n"
        "- When remediation is complete, call `mark_remediation_complete` with evidence.\n"
        "- Do not emit prose-only completion; this phase exits only via tool call.\n\n"
        "Skill / framework usage:\n"
        "- GMAS (`multi_agent_gmas`) is the required stack for any "
        "workspace feature that touches an LLM, prompts, model calls, "
        "RAG, generation, classification, summarization, autonomous "
        "agents, planners, or judge/evaluator nodes. Umbrella auto-detects "
        "that domain and prepares `gmas_active_context.md`; call "
        "`get_gmas_context` / `search_gmas_knowledge` for specific API "
        "patterns before implementing. Use "
        "`configure_workspace_skills(..., enabled=false, reason=...)` "
        "only as an explicit audited opt-out for pure non-LLM work."
    )


def should_run_planner(
    *, mode: str, task_main_text: str, has_existing_plan: bool
) -> bool:
    if has_existing_plan:
        return False
    if mode == PLANNER_MODE_OFF:
        return False
    if mode == PLANNER_MODE_ALWAYS:
        return True
    # auto: trigger only when the task is substantial enough to warrant planning
    text = (task_main_text or "").strip()
    return len(text) >= planner_auto_min_chars()


# ---------------------------------------------------------------------------
# Tool plumbing helpers (called by control.py tool handlers)
# ---------------------------------------------------------------------------


def store_for_ctx(ctx: Any) -> TaskPlanStore:
    drive_root = pathlib.Path(getattr(ctx, "drive_root", pathlib.Path.cwd()))
    return TaskPlanStore(drive_root)


def current_task_id(ctx: Any) -> str:
    return str(getattr(ctx, "task_id", "") or "default")


def current_plan_execution_context(ctx: Any) -> PlanExecutionContext:
    existing = getattr(ctx, "plan_execution_context", None)
    if isinstance(existing, PlanExecutionContext):
        return existing
    if isinstance(existing, dict):
        return PlanExecutionContext(
            active_plan_id=str(existing.get("active_plan_id") or ""),
            plan_store_root=str(existing.get("plan_store_root") or ""),
            task_id=str(existing.get("task_id") or current_task_id(ctx)),
            phase=str(existing.get("phase") or ""),
            subtask_id=str(existing.get("subtask_id") or ""),
        )
    return PlanExecutionContext(
        active_plan_id=str(getattr(ctx, "active_plan_id", "") or ""),
        plan_store_root=str(getattr(ctx, "drive_root", "") or ""),
        task_id=current_task_id(ctx),
    )


def active_plan_id(ctx: Any) -> str:
    plan_ctx = current_plan_execution_context(ctx)
    return plan_ctx.active_plan_id or plan_ctx.task_id or current_task_id(ctx)


def load_active_plan(ctx: Any) -> TaskPlan | None:
    return store_for_ctx(ctx).load(active_plan_id(ctx))


def current_workspace_id(ctx: Any) -> str:
    return str(getattr(ctx, "active_workspace_id", "") or "")
