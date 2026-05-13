"""End-to-end orchestration tests for the adaptive task planner.

We drive ``run_llm_loop`` with a scripted fake ``LLMClient`` and the
real ``ToolRegistry`` so the planner tools (``propose_task_plan`` etc.)
go through their actual handlers and the ``TaskPlanStore`` round-trips
the plan to disk between phases.
"""

import json
import queue
import uuid
from pathlib import Path
from typing import Any

import pytest

from ouroboros.task_planner import (
    PLANNER_MODE_ALWAYS,
    PLANNER_MODE_OFF,
    SUBTASK_STATUS_DONE,
    SUBTASK_STATUS_SKIPPED,
    TaskPlanStore,
)


# ---------------------------------------------------------------------------
# Fake LLM that emits scripted responses (tool calls or text).
# ---------------------------------------------------------------------------


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class ScriptedLLM:
    """Fake LLMClient that pops one response per ``chat()`` call.

    Each response is either ``{"text": str}`` or ``{"tool_calls": [tc, ...]}``.
    A response also gets the standard usage payload tacked on.
    """

    def __init__(self, scripted: list[dict]) -> None:
        self._script = list(scripted)
        self.history: list[dict] = []
        self.snapshot_lens: list[int] = []

    def default_model(self) -> str:
        return "fake-model"

    def available_models(self) -> list[str]:
        return ["fake-model"]

    def chat(self, **kwargs: Any) -> tuple[dict, dict]:
        msgs = kwargs.get("messages") or []
        self.snapshot_lens.append(len(msgs))
        self.history.append(kwargs)
        if not self._script:
            raise AssertionError("ScriptedLLM ran out of responses")
        item = self._script.pop(0)
        msg: dict[str, Any] = {"content": item.get("text", "")}
        if "tool_calls" in item:
            msg["tool_calls"] = item["tool_calls"]
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "cost": 0.0,
        }
        return msg, usage


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


@pytest.fixture
def loop_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the inputs ``run_llm_loop`` needs without depending on Umbrella."""
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    drive_root = tmp_path / "drive"
    drive_root.mkdir(parents=True, exist_ok=True)
    drive_logs = drive_root / "logs"
    drive_logs.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    # Stop the loop from calling the real Umbrella memory recall machinery.
    import ouroboros.memory_hooks as mh

    monkeypatch.setattr(mh, "_safe_palace", lambda *_a, **_kw: None)
    monkeypatch.setattr(mh, "_safe_store", lambda *_a, **_kw: None)
    monkeypatch.setattr(mh, "_lexical_fallback_recall", lambda *_a, **_kw: "")
    monkeypatch.setattr(mh, "recall_for_task_start", lambda *_a, **_kw: "")
    monkeypatch.setattr(mh, "recall_periodic", lambda *_a, **_kw: "")
    monkeypatch.setattr(mh, "mirror_subtask_to_memory", lambda *_a, **_kw: None)
    monkeypatch.setattr(mh, "record_workspace_change", lambda *_a, **_kw: None)

    # Suppress writes to the FS for non-planner write tools (we don't exercise them here).
    monkeypatch.setenv("OUROBOROS_PLANNER_PHASE_ROUNDS", "8")
    monkeypatch.setenv("OUROBOROS_LLM_LOOP_RETRIES", "1")
    monkeypatch.setenv("OUROBOROS_REQUIRE_PLANNER_DISCOVERY", "0")

    registry = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)
    ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
    registry.set_context(ctx)
    return {
        "registry": registry,
        "ctx": ctx,
        "drive_root": drive_root,
        "drive_logs": drive_logs,
    }


def _base_messages() -> list[dict]:
    body = (
        "Workspace path: workspaces/news_cards_ai\n\n"
        "Implement an adaptive news cards web app with backend, frontend, "
        "tests, and observability. The TASK_MAIN.md describes the deliverable: "
        "a multi-step build that needs decomposition before any code is written."
    ) * 2  # >=220 chars to trip the auto-mode planner
    return [{"role": "user", "content": body}]


def _run(monkeypatch, env, scripted, *, task_id: str = "task_test"):
    from ouroboros import loop as _loop

    llm = ScriptedLLM(scripted)
    incoming: queue.Queue = queue.Queue()
    text, usage, trace = _loop.run_llm_loop(
        messages=_base_messages(),
        tools=env["registry"],
        llm=llm,
        drive_logs=env["drive_logs"],
        emit_progress=lambda _msg: None,
        incoming_messages=incoming,
        task_id=task_id,
        drive_root=env["drive_root"],
        deadline_monotonic=None,
    )
    return text, usage, trace, llm


# ---------------------------------------------------------------------------
# Scenario 1: normal two-step plan executed end-to-end.
# ---------------------------------------------------------------------------


def test_planner_drives_two_step_plan(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)

    scripted = [
        # Planner round.
        {
            "tool_calls": [
                _tool_call(
                    "propose_discovery_plan",
                    {
                        "phases": [
                            {
                                "phase": "planner",
                                "sources": ["workspace"],
                                "max_calls": 1,
                            }
                        ],
                    },
                ),
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Discovery",
                                "description": "Read TASK_MAIN.md",
                                "success_check": "summary written",
                            },
                            {
                                "title": "Build",
                                "description": "Implement v1",
                                "success_check": "tests pass",
                            },
                        ],
                    },
                ),
            ]
        },
        # Subtask 1 → done immediately.
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "discovery summary",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        # Review 1 → text reply, no revision.
        {"text": "tail still ok"},
        # Subtask 2 → done.
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["app.py"],
                    },
                )
            ]
        },
        # Final aggregation: text reply.
        {"text": "All planned subtasks completed; final answer ready."},
    ]

    text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_two_step"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_two_step")
    assert plan is not None, "plan file must exist after planner round"
    assert plan.cursor == 2
    assert all(s.status == SUBTASK_STATUS_DONE for s in plan.subtasks)
    assert plan.subtasks[0].summary == "discovery summary"
    assert plan.subtasks[1].evidence == ["app.py"]
    assert "final answer" in text.lower()
    # Sanity: at minimum, planner + 2 subtasks + 1 review + final = 5 LLM calls.
    assert len(llm.history) == 5


def test_planner_reprompts_text_only_plan(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)

    scripted = [
        {"text": "Plan: first build the project, then verify it."},
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Build",
                                "description": "Implement the project",
                                "success_check": "project files exist",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        {"text": "Done."},
    ]

    text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_text_then_plan"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_text_then_plan")
    assert plan is not None
    assert plan.cursor == 1
    assert plan.subtasks[0].title == "Build"
    assert "done" in text.lower()
    planner_tools = [s["function"]["name"] for s in llm.history[0]["tools"]]
    assert "propose_task_plan" in planner_tools
    assert "read_workspace_file" in planner_tools
    assert any(
        isinstance(m.get("content"), str) and "[REQUIRED_TOOL_MISSING]" in m["content"]
        for m in llm.history[1]["messages"]
    )
    assert len(llm.history) == 4


def test_planner_can_inspect_workspace_before_plan(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    workspace = loop_env["ctx"].repo_dir / "workspaces" / "news_cards_ai"
    workspace.mkdir(parents=True, exist_ok=True)
    (loop_env["ctx"].repo_dir / "umbrella").mkdir(parents=True, exist_ok=True)
    (workspace / "TASK_MAIN.md").write_text(
        "Build the news cards project.", encoding="utf-8"
    )

    scripted = [
        {
            "tool_calls": [
                _tool_call("list_workspace_files", {"workspace_id": "news_cards_ai"})
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "read_workspace_file",
                    {
                        "workspace_id": "news_cards_ai",
                        "file_path": "TASK_MAIN.md",
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Build",
                                "description": "Implement the project",
                                "success_check": "project files exist",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        {"text": "Done."},
    ]

    text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_inspect_then_plan"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_inspect_then_plan")
    assert plan is not None
    assert plan.cursor == 1
    assert plan.subtasks[0].title == "Build"
    assert "done" in text.lower()
    assert len(llm.history) == 5


def test_planner_rescue_restricts_to_plan_tool_after_discovery_cap(
    monkeypatch, loop_env
):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    monkeypatch.setenv("OUROBOROS_PLANNER_INITIAL_ROUNDS", "2")
    workspace = loop_env["ctx"].repo_dir / "workspaces" / "news_cards_ai"
    workspace.mkdir(parents=True, exist_ok=True)
    (loop_env["ctx"].repo_dir / "umbrella").mkdir(parents=True, exist_ok=True)
    (workspace / "TASK_MAIN.md").write_text(
        "Build the news cards project.", encoding="utf-8"
    )

    scripted = [
        {
            "tool_calls": [
                _tool_call("list_workspace_files", {"workspace_id": "news_cards_ai"})
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "read_workspace_file",
                    {
                        "workspace_id": "news_cards_ai",
                        "file_path": "TASK_MAIN.md",
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Build",
                                "description": "Implement the project",
                                "success_check": "project files exist",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        {"text": "Done."},
    ]

    _text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_rescue_plan"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_rescue_plan")
    assert plan is not None
    rescue_tools = [s["function"]["name"] for s in llm.history[2]["tools"]]
    assert rescue_tools == ["propose_task_plan"]


def test_planner_plan_now_forces_propose_task_plan_on_third_round(
    monkeypatch, loop_env
):
    """After two discovery-only rounds, [PLANNER_PLAN_NOW] narrows tools and forces ``propose_task_plan``."""
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    monkeypatch.setenv("OUROBOROS_PLANNER_INITIAL_ROUNDS", "10")
    workspace = loop_env["ctx"].repo_dir / "workspaces" / "news_cards_ai"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "TASK_MAIN.md").write_text(
        "Build the news cards project.", encoding="utf-8"
    )

    scripted = [
        {
            "tool_calls": [
                _tool_call("list_workspace_files", {"workspace_id": "news_cards_ai"})
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "read_workspace_file",
                    {
                        "workspace_id": "news_cards_ai",
                        "file_path": "TASK_MAIN.md",
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Build",
                                "description": "Implement the project",
                                "success_check": "project files exist",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        {"text": "Done."},
    ]

    _text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_plan_now"
    )

    third = llm.history[2]
    assert third.get("tool_choice") == {
        "type": "function",
        "function": {"name": "propose_task_plan"},
    }
    msgs = third.get("messages") or []
    assert any(
        isinstance(m.get("content"), str) and "[PLANNER_PLAN_NOW]" in m["content"]
        for m in msgs
    )
    plan_now_tools = [s["function"]["name"] for s in third["tools"]]
    assert plan_now_tools == ["propose_task_plan"]


# ---------------------------------------------------------------------------
# Schema enforcement: a tool not in the active phase schema must be refused
# without execution, and the phase must exit early after 3 strikes so the
# loop can cascade (planner -> rescue -> linear) instead of burning the
# whole round cap on the same forbidden call.
# ---------------------------------------------------------------------------


def test_planner_refuses_tool_outside_active_schema(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    monkeypatch.setenv("OUROBOROS_PLANNER_INITIAL_ROUNDS", "10")

    forbidden = [
        {"tool_calls": [_tool_call("run_workspace_command", {"command": "ls"})]}
        for _ in range(4)
    ]
    rescue_plan = {
        "tool_calls": [
            _tool_call(
                "propose_task_plan",
                {
                    "steps": [
                        {
                            "title": "Build",
                            "description": "Build",
                            "success_check": "ok",
                        },
                    ],
                },
            )
        ]
    }
    finish = [
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build done",
                        "evidence": ["build artifacts generated"],
                    },
                )
            ]
        },
        {"text": "All done."},
    ]
    scripted = forbidden + [rescue_plan] + finish

    _text, _usage, _trace, llm = _run(
        monkeypatch,
        loop_env,
        scripted,
        task_id="task_forbidden_tool",
    )

    # The planner must have offered a discovery-only schema that does NOT
    # include run_workspace_command (proves Fix 2).
    planner_tools = [s["function"]["name"] for s in llm.history[0]["tools"]]
    assert "run_workspace_command" not in planner_tools

    # The forbidden call must have been refused (proves Fix 1) and the
    # phase must have cascaded into planner_rescue after 3 strikes
    # (proves the strike-counter early-exit). When the rescue phase fires
    # it forces tool_choice=propose_task_plan (proves Fix 3).
    rescue_call = llm.history[3]
    assert rescue_call.get("tool_choice") == {
        "type": "function",
        "function": {"name": "propose_task_plan"},
    }

    # Plan must end up persisted from the rescue's propose_task_plan.
    plan = TaskPlanStore(loop_env["drive_root"]).load("task_forbidden_tool")
    assert plan is not None
    assert plan.subtasks[0].title == "Build"


# ---------------------------------------------------------------------------
# Scenario 2: review phase revises the tail before continuing.
# ---------------------------------------------------------------------------


def test_review_revision_replaces_tail(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)

    scripted = [
        # Planner.
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Sketch",
                                "description": "Sketch architecture",
                                "success_check": "diagram",
                            },
                            {
                                "title": "Old Step",
                                "description": "Will be replaced",
                                "success_check": "n/a",
                            },
                        ],
                    },
                )
            ]
        },
        # Subtask 1 done.
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "sketch produced",
                        "evidence": ["architecture.md updated"],
                    },
                )
            ]
        },
        # Review 1 → revise tail with two new steps.
        {
            "tool_calls": [
                _tool_call(
                    "revise_remaining_plan",
                    {
                        "steps": [
                            {
                                "title": "New A",
                                "description": "After-evidence step A",
                                "success_check": "A done",
                            },
                            {
                                "title": "New B",
                                "description": "After-evidence step B",
                                "success_check": "B done",
                            },
                        ],
                        "reason": "Old Step turned out unnecessary",
                    },
                )
            ]
        },
        # Subtask 2 (new A) done.
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "A done",
                        "evidence": ["step_a implemented"],
                    },
                )
            ]
        },
        # Review 2: text, no revision.
        {"text": "tail still good"},
        # Subtask 3 (new B) done.
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "B done",
                        "evidence": ["step_b implemented"],
                    },
                )
            ]
        },
        # Final.
        {"text": "Done."},
    ]

    _text, _usage, _trace, _llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_revise"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_revise")
    assert plan is not None
    assert plan.revisions == 1
    titles = [s.title for s in plan.subtasks]
    assert titles == ["Sketch", "New A", "New B"]
    assert all(s.status == SUBTASK_STATUS_DONE for s in plan.subtasks)


# ---------------------------------------------------------------------------
# Scenario 3: resume — second loop run finds an existing plan and skips
# the planner round entirely.
# ---------------------------------------------------------------------------


def test_resume_skips_planner_round(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    store = TaskPlanStore(loop_env["drive_root"])
    plan = store.create_from_steps(
        task_id="task_resume",
        workspace_id="news_cards_ai",
        objective_digest="resume test",
        steps=[
            {"title": "Step A", "description": "first", "success_check": "ok"},
            {"title": "Step B", "description": "second", "success_check": "ok"},
        ],
    )
    store.start_current(plan)
    store.complete_current(
        plan, status=SUBTASK_STATUS_DONE, summary="A finished offline"
    )

    scripted = [
        # No planner round expected. First call is for the surviving subtask B.
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "B finished",
                        "evidence": ["step_b finished"],
                    },
                )
            ]
        },
        # Final aggregation reply.
        {"text": "All done after resume."},
    ]

    text, _u, _t, llm = _run(monkeypatch, loop_env, scripted, task_id="task_resume")

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_resume")
    assert plan is not None
    assert plan.cursor == 2
    assert [s.status for s in plan.subtasks] == [
        SUBTASK_STATUS_DONE,
        SUBTASK_STATUS_DONE,
    ]
    assert plan.subtasks[0].summary == "A finished offline"
    assert plan.subtasks[1].summary == "B finished"
    assert "after resume" in text.lower()
    # Exactly 2 LLM calls (subtask + final). No planner, no review (only 1 step left after resume).
    assert len(llm.history) == 2

    # Confirm the resume system message was injected.
    first_msgs = llm.history[0]["messages"]
    assert any(
        isinstance(m.get("content"), str) and "[PLAN_PROGRESS]" in m["content"]
        for m in first_msgs
    )


def test_remediation_phase_fires_when_subtask_was_skipped(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)

    scripted = [
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Build",
                                "description": "Implement",
                                "success_check": "files exist",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "skipped",
                        "summary": "Planned step is obsolete after discovery",
                        "evidence": ["replacement approach selected"],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_remediation_complete",
                    {
                        "summary": "fixed skipped build",
                        "evidence": [
                            "update_workspace_seed wrote app.py",
                            "verify exit_code=0",
                        ],
                    },
                )
            ]
        },
        {"text": "Final after remediation."},
    ]

    text, _u, _t, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_remediation"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_remediation")
    assert plan is not None
    assert plan.subtasks[0].status == SUBTASK_STATUS_SKIPPED
    assert "fixed skipped build" in plan.subtasks[0].summary
    assert "Final after remediation" in text
    remediation_tools = [
        schema["function"]["name"] for schema in llm.history[2]["tools"]
    ]
    assert "mark_remediation_complete" in remediation_tools
    assert "update_workspace_seed" in remediation_tools
    assert any(
        isinstance(msg.get("content"), str) and "[REMEDIATION PHASE]" in msg["content"]
        for msg in llm.history[2]["messages"]
    )


# ---------------------------------------------------------------------------
# Scenario 4: planner mode 'off' — no plan file is created and the loop
# behaves like the legacy linear path.
# ---------------------------------------------------------------------------


def test_phase_isolation_resets_messages_between_phases(monkeypatch, loop_env):
    """Subtask / review / final phases each start from the same short base."""
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    monkeypatch.setenv("OUROBOROS_ISOLATE_PHASES", "1")

    scripted = [
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Discovery",
                                "description": "Read TASK_MAIN.md",
                                "success_check": "summary written",
                            },
                            {
                                "title": "Build",
                                "description": "Implement v1",
                                "success_check": "tests pass",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "discovery summary",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        {"text": "tail still ok"},
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["app.py"],
                    },
                )
            ]
        },
        {"text": "All planned subtasks completed; final answer ready."},
    ]

    _text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_isolation"
    )

    assert len(llm.history) == 5
    msg_lengths = list(llm.snapshot_lens)

    subtask_1_len = msg_lengths[1]
    subtask_2_len = msg_lengths[3]
    assert subtask_2_len <= subtask_1_len + 1, (
        f"isolation broke: subtask_1 had {subtask_1_len} msgs but subtask_2 "
        f"had {subtask_2_len} (full sequence: {msg_lengths})"
    )

    final_len = msg_lengths[4]
    assert final_len <= subtask_1_len + 1


def test_phase_isolation_disabled_keeps_legacy_growth(monkeypatch, loop_env):
    """With the flag off the shared buffer grows across phases."""
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    monkeypatch.setenv("OUROBOROS_ISOLATE_PHASES", "0")

    scripted = [
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Discovery",
                                "description": "Read TASK_MAIN.md",
                                "success_check": "summary written",
                            },
                            {
                                "title": "Build",
                                "description": "Implement v1",
                                "success_check": "tests pass",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "discovery summary",
                        "evidence": ["TASK_MAIN.md"],
                    },
                )
            ]
        },
        {"text": "tail still ok"},
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "build complete",
                        "evidence": ["app.py"],
                    },
                )
            ]
        },
        {"text": "All planned subtasks completed; final answer ready."},
    ]

    _text, _usage, _trace, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_legacy_growth"
    )

    msg_lengths = list(llm.snapshot_lens)
    assert msg_lengths[3] > msg_lengths[1], msg_lengths


def test_planner_mode_off_uses_linear_path(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_OFF)

    # The legacy linear path now refuses to text-exit while no workspace
    # writes have happened. The model gets at most two `[NO_PROGRESS_GUARD]`
    # nudges before the loop gives up and accepts the final text. So a
    # scripted run that wants to exit by text alone needs three responses.
    scripted = [
        {"text": "I'm just answering directly, no plan."},
        {"text": "Still no writes — simulating a stubborn LLM."},
        {"text": "Final answering directly response."},
    ]
    text, _u, _t, llm = _run(monkeypatch, loop_env, scripted, task_id="task_off")

    plan_path = loop_env["drive_root"] / "task_plans" / "task_off.json"
    assert not plan_path.exists()
    assert "answering directly" in text
    # Two nudges => 3 LLM calls total (1 initial + 2 after nudges).
    assert len(llm.history) == 3
    # No planner system prompt should have been injected.
    assert not any(
        isinstance(m.get("content"), str) and "[PLANNER PHASE]" in m["content"]
        for m in llm.history[0]["messages"]
    )
    # The [NO_PROGRESS_GUARD] nudge should appear in the conversation passed
    # to the second LLM call.
    assert any(
        isinstance(m.get("content"), str) and "[NO_PROGRESS_GUARD]" in m["content"]
        for m in llm.history[1]["messages"]
    )


def test_subtask_phase_includes_gmas_retrieval_tools() -> None:
    from ouroboros.loop import _SUBTASK_TOOL_NAMES

    assert "get_gmas_context" in _SUBTASK_TOOL_NAMES
    assert "search_gmas_knowledge" in _SUBTASK_TOOL_NAMES


def test_subtask_phase_cap_continues_same_subtask(monkeypatch, loop_env):
    monkeypatch.setenv("OUROBOROS_PLANNER_MODE", PLANNER_MODE_ALWAYS)
    monkeypatch.setenv("OUROBOROS_PLANNER_PHASE_ROUNDS", "1")

    scripted = [
        {
            "tool_calls": [
                _tool_call(
                    "propose_task_plan",
                    {
                        "steps": [
                            {
                                "title": "Build",
                                "description": "Implement",
                                "success_check": "files exist",
                            },
                        ],
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "read_workspace_file",
                    {
                        "workspace_id": "news_cards_ai",
                        "file_path": "TASK_MAIN.md",
                    },
                )
            ]
        },
        {
            "tool_calls": [
                _tool_call(
                    "mark_subtask_complete",
                    {
                        "status": "done",
                        "summary": "completed after rescue",
                        "evidence": ["files exist"],
                    },
                )
            ]
        },
        {"text": "Final after same-subtask rescue."},
    ]

    text, _u, _t, llm = _run(
        monkeypatch, loop_env, scripted, task_id="task_same_subtask"
    )

    plan = TaskPlanStore(loop_env["drive_root"]).load("task_same_subtask")
    assert plan is not None
    assert plan.subtasks[0].status == SUBTASK_STATUS_DONE
    assert "Final after same-subtask rescue" in text
    assert any(
        isinstance(msg.get("content"), str)
        and "[SUBTASK_RESCUE_CONTINUATION]" in msg["content"]
        for call in llm.history
        for msg in call["messages"]
    )


# ---------------------------------------------------------------------------
# Self-review phase: tool-less single-round flow with auto-retry.
# ---------------------------------------------------------------------------


def _run_self_review(
    env,
    scripted,
    *,
    task_id: str = "task_self_review",
):
    from ouroboros import loop as _loop

    llm = ScriptedLLM(scripted)
    incoming: queue.Queue = queue.Queue()
    text, _usage, _trace = _loop.run_llm_loop(
        messages=[
            {
                "role": "user",
                "content": (
                    "# Self-Review of the Real Run\n"
                    "Verification has passed. Reply with LGTM <sentence> "
                    "or NEEDS_FIX <list>."
                ),
            }
        ],
        tools=env["registry"],
        llm=llm,
        drive_logs=env["drive_logs"],
        emit_progress=lambda _msg: None,
        incoming_messages=incoming,
        task_id=task_id,
        drive_root=env["drive_root"],
        deadline_monotonic=None,
        self_review_attempt=1,
    )
    return text, llm


def test_self_review_phase_runs_tool_less_single_round(monkeypatch, loop_env):
    text, llm = _run_self_review(
        loop_env,
        [{"text": "LGTM verification passes and the run delivers what was asked."}],
    )
    assert text.startswith("LGTM")
    assert len(llm.history) == 1
    first_call = llm.history[0]
    assert first_call.get("tools") in (None, [], ()), (
        "self-review must run with no tool schemas so the model "
        "literally cannot call a function"
    )
    assert "tool_choice" not in first_call or first_call.get("tool_choice") in (
        None,
        "none",
    )
    enforcement_seen = any(
        isinstance(m.get("content"), str)
        and "[SELF_REVIEW_PROTOCOL]" in m["content"]
        for m in first_call["messages"]
    )
    assert enforcement_seen, (
        "self-review prompt must inject the SELF_REVIEW_PROTOCOL "
        "system reminder forbidding tool calls and markup"
    )

    events = (loop_env["drive_logs"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"type": "self_review_started"' in events


def test_self_review_phase_retries_when_model_emits_pseudo_tool_call(
    monkeypatch, loop_env
):
    """If the model breaks the contract by emitting <tool_call> markup
    or skipping the LGTM/NEEDS_FIX prefix, the harness must retry once
    with a stricter system message and use the recovered verdict."""

    text, llm = _run_self_review(
        loop_env,
        [
            {
                "text": (
                    "<tool_call>mark_subtask_complete<arg_key>status</arg_key>"
                    "<arg_value>done</arg_value></tool_call>"
                )
            },
            {"text": "NEEDS_FIX\n1. Parser returns empty articles."},
        ],
        task_id="task_self_review_retry",
    )

    assert text.startswith("NEEDS_FIX")
    assert "Parser returns empty articles" in text
    assert len(llm.history) == 2

    retry_messages = llm.history[1]["messages"]
    retry_systems = [
        m.get("content")
        for m in retry_messages
        if m.get("role") == "system"
        and isinstance(m.get("content"), str)
    ]
    assert any("[RETRY]" in c for c in retry_systems)
    assert any("previous bad reply" in c for c in retry_systems)

    events = (loop_env["drive_logs"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"type": "self_review_contract_retry"' in events
    assert '"reason": "pseudo_tool_call"' in events
