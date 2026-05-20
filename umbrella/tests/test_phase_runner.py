"""
Phase runner integration tests — uses fake (no-LLM) launcher to exercise
the full preflight→research→plan→execute→verify pipeline.
"""
import json
import pathlib
import time
import pytest
from unittest.mock import MagicMock, patch

from umbrella.orchestrator.phase_plan import build_default_plan, save_plan, load_plan
from umbrella.orchestrator.runner import PhaseRunner
from umbrella.orchestrator.worker import (
    authoritative_artifacts_for_phase,
    build_phase_task,
    render_phase_user_prompt,
)
from umbrella.memory.palace.facade import MemPalace
from umbrella.orchestrator.watcher import WatcherPollLoop
from umbrella.phases.loader import load_manifest
from umbrella.phases.base import PhasePlan, PhaseNode, SubtaskCard, SuccessTest, WatcherSignal


class _FakeHandle:
    def __init__(self, result):
        self._result = result

    def wait(self):
        return dict(self._result)


class _FakeLauncher:
    def __init__(self, drive_root, *, write_required_signal: bool = True):
        self.drive_root = drive_root
        self.write_required_signal = write_required_signal

    def submit_task(self, task, timeout=None):
        if self.write_required_signal:
            manifest = (task.get("context_overlays") or {}).get("phase_manifest") or {}
            required = ((manifest.get("exit_criteria") or {}).get("required_calls") or [])
            for tool_name in required:
                payload = {"status": "ready"} if tool_name == "submit_preflight_report" else {}
                row = {
                    "signal_id": f"sig-{tool_name}",
                    "created_at": time.time(),
                    "kind": tool_name,
                    "payload": payload,
                    "task_id": task["id"],
                    "phase": task["id"].split(":", 1)[-1],
                }
                state = self.drive_root / "state"
                state.mkdir(parents=True, exist_ok=True)
                with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            palace_rules = ((manifest.get("exit_criteria") or {}).get("min_palace_writes") or [])
            if palace_rules:
                logs = self.drive_root / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                needed = max(int(rule.get("n") or 1) for rule in palace_rules)
                with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
                    for idx in range(needed):
                        f.write(
                            json.dumps(
                                {
                                    "task_id": task["id"],
                                    "tool": "palace_add",
                                    "result_preview": "OK: memory saved",
                                    "idx": idx,
                                }
                            )
                            + "\n"
                        )
        return _FakeHandle({"status": "complete", "task_id": task["id"], "events": []})


class _ReviewFakeLauncher:
    def __init__(self, drive_root, *, verdict: str, loop_back_target: str | None = None):
        self.drive_root = drive_root
        self.verdict = verdict
        self.loop_back_target = loop_back_target
        self.submitted: list[str] = []
        self.tasks: list[dict] = []
        self.review_calls = 0

    def submit_task(self, task, timeout=None):
        self.submitted.append(task["id"])
        self.tasks.append(task)
        task_id = task["id"]
        state = self.drive_root / "state"
        state.mkdir(parents=True, exist_ok=True)
        phase = task_id.split(":", 1)[-1]
        rows = []
        manifest = (task.get("context_overlays") or {}).get("phase_manifest") or {}
        required = ((manifest.get("exit_criteria") or {}).get("required_calls") or [])
        if phase == "plan" and "propose_phase_plan" in required:
            (state / "phase_plan_proposal_latest.json").write_text(
                json.dumps(
                    {
                        "run_id": task_id.split(":", 1)[0],
                        "workspace_id": "test_ws",
                        "plan_id": "test-plan",
                        "plan": {
                            "subtasks": [
                                {
                                    "id": "build",
                                    "title": "Build",
                                    "success_test": "pytest -q",
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
        review_required = "submit_micro_review" in required
        verdict = self.verdict
        if review_required and verdict == "revise" and self.review_calls > 0:
            verdict = "pass"
        if "submit_micro_review" not in required:
            for tool_name in required:
                rows.append(
                    {
                        "signal_id": f"sig-{tool_name}-{phase}",
                        "created_at": time.time(),
                        "kind": tool_name,
                        "payload": {"plan_id": "test-plan"}
                        if tool_name == "submit_phase_plan"
                        else {},
                        "task_id": task["id"],
                        "phase": phase,
                    }
                )
            palace_rules = ((manifest.get("exit_criteria") or {}).get("min_palace_writes") or [])
            if palace_rules:
                logs = self.drive_root / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                needed = max(int(rule.get("n") or 1) for rule in palace_rules)
                with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
                    for idx in range(needed):
                        f.write(
                            json.dumps(
                                {
                                    "task_id": task["id"],
                                    "tool": "palace_add",
                                    "result_preview": "OK: memory saved",
                                    "idx": idx,
                                }
                            )
                            + "\n"
                        )
        elif self.loop_back_target and verdict == "revise":
            rows.append(
                {
                    "signal_id": f"sig-loop-{phase}",
                    "created_at": time.time(),
                    "kind": "loop_back_to",
                    "payload": {
                        "phase": self.loop_back_target,
                        "reason": "test revision",
                    },
                    "task_id": task["id"],
                    "phase": phase,
                }
            )
        if review_required:
            self.review_calls += 1
            rows.append(
                {
                    "signal_id": f"sig-review-{phase}",
                    "created_at": time.time(),
                    "kind": "submit_micro_review",
                    "payload": {
                        "verdict": verdict,
                        "revisions": ["subtask_06 must add chat-based input"],
                        "notes": "test",
                    },
                    "task_id": task["id"],
                    "phase": phase,
                }
            )
        with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return _FakeHandle({"status": "complete", "task_id": task["id"], "events": []})


class _ExecuteRetryFakeLauncher:
    def __init__(self, drive_root):
        self.drive_root = drive_root
        self.submitted: list[str] = []

    def submit_task(self, task, timeout=None):
        self.submitted.append(task["id"])
        state = self.drive_root / "state"
        logs = self.drive_root / "logs"
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        task_id = task["id"]
        phase = task_id.split(":", 1)[-1]
        if self.submitted.count(task_id) >= 2:
            with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "tool": "apply_workspace_patch",
                            "result_preview": '{"status": "applied", "applied": ["app.py"]}',
                        }
                    )
                    + "\n"
                )
            row = {
                "signal_id": "sig-mark",
                "created_at": time.time(),
                "kind": "mark_subtask_complete",
                "payload": {"subtask_id": "existing", "evidence": "test"},
                "task_id": task_id,
                "phase": phase,
            }
            with (state / "phase_control_signals.jsonl").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps(row) + "\n")
        return _FakeHandle({"status": "complete", "task_id": task_id, "events": []})


class _ExecuteCapturedFailureRetryFakeLauncher:
    def __init__(self, drive_root):
        self.drive_root = drive_root
        self.submitted: list[str] = []
        self.tasks: list[dict] = []

    def submit_task(self, task, timeout=None):
        self.submitted.append(task["id"])
        self.tasks.append(task)
        task_id = task["id"]
        phase = task_id.split(":", 1)[-1]
        state = self.drive_root / "state"
        logs = self.drive_root / "logs"
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "task_id": task_id,
                        "tool": "apply_workspace_patch",
                        "result_preview": '{"status": "applied", "applied": ["src/civilization/state.py"]}',
                    }
                )
                + "\n"
            )
        if self.submitted.count(task_id) >= 2:
            plan_path = state / "phase_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            execute = next(node for node in plan["nodes"] if node["id"] == "execute")
            current = next(
                item for item in execute["subtasks"] if item.get("status") != "done"
            )
            current["status"] = "done"
            current["completion"] = {
                "summary": "core model repair completed after retry context",
                "evidence": ["python -m pytest tests/test_models.py -v --tb=short"],
            }
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            row = {
                "signal_id": "sig-core-models",
                "created_at": time.time(),
                "kind": "mark_subtask_complete",
                "payload": {
                    "subtask_id": current["id"],
                    "summary": current["completion"]["summary"],
                    "evidence": current["completion"]["evidence"],
                },
                "task_id": task_id,
                "phase": phase,
            }
            with (state / "phase_control_signals.jsonl").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps(row) + "\n")
            return _FakeHandle(
                {
                    "status": "complete",
                    "task_id": task_id,
                    "result": "OK: core_models completed",
                    "events": [],
                }
            )
        captured = (
            "## Subtask Status: Partially Complete\n\n"
            "The `core_models` subtask has been partially implemented but the "
            "success test (`python -m pytest tests/test_models.py -v --tb=short`) "
            "does not pass yet.\n\n"
            "### Current State\n"
            "- **Tests Passing**: 23/29 (79%)\n\n"
            "### Remaining Failures (6 tests)\n"
            "1. `test_unit_movement` - Unit.can_move() returns False\n"
            "2. `test_game_initialization` - Expects 100 territories\n\n"
            "### Concrete Blocker\n"
            "Attempted to apply final comprehensive repair but encountered "
            "`patch_hunk_mismatch` on `src/civilization/models.py`.\n"
        )
        return _FakeHandle(
            {
                "status": "complete",
                "task_id": task_id,
                "result": captured,
                "events": [],
            }
        )


class _ExecuteSubtaskQueueFakeLauncher:
    def __init__(self, drive_root):
        self.drive_root = drive_root
        self.submitted: list[str] = []

    def submit_task(self, task, timeout=None):
        self.submitted.append(task["id"])
        task_id = task["id"]
        phase = task_id.split(":", 1)[-1]
        state = self.drive_root / "state"
        logs = self.drive_root / "logs"
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        plan_path = state / "phase_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        execute = next(node for node in plan["nodes"] if node["id"] == "execute")
        current = next(
            item for item in execute["subtasks"] if item.get("status") != "done"
        )
        current["status"] = "done"
        plan["version"] = int(plan.get("version") or 0) + 1
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "task_id": task_id,
                        "tool": "apply_workspace_patch",
                        "result_preview": '{"status": "applied", "applied": ["app.py"]}',
                    }
                )
                + "\n"
            )
        with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "signal_id": f"sig-{current['id']}",
                        "created_at": time.time(),
                        "kind": "mark_subtask_complete",
                        "payload": {"subtask_id": current["id"], "evidence": "test"},
                        "task_id": task_id,
                        "phase": phase,
                    }
                )
                + "\n"
            )
        return _FakeHandle({"status": "complete", "task_id": task_id, "events": []})


class _PlanRetryFakeLauncher:
    def __init__(self, drive_root):
        self.drive_root = drive_root
        self.submitted: list[str] = []

    def submit_task(self, task, timeout=None):
        self.submitted.append(task["id"])
        state = self.drive_root / "state"
        logs = self.drive_root / "logs"
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        task_id = task["id"]
        phase = task_id.split(":", 1)[-1]
        (state / "phase_plan_proposal_latest.json").write_text(
            json.dumps(
                {
                    "run_id": task_id.split(":", 1)[0],
                    "workspace_id": "test_ws",
                    "plan_id": "draft",
                    "plan": {
                        "subtasks": [
                            {
                                "id": "build",
                                "title": "Build",
                                "success_test": "pytest -q",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        rows = [
            {
                "signal_id": "sig-propose",
                "created_at": time.time(),
                "kind": "propose_phase_plan",
                "payload": {"plan": {"id": "draft"}},
                "task_id": task_id,
                "phase": phase,
            }
        ]
        if self.submitted.count(task_id) >= 2:
            rows.append(
                {
                    "signal_id": "sig-submit",
                    "created_at": time.time(),
                    "kind": "submit_phase_plan",
                    "payload": {"plan_id": "draft"},
                    "task_id": task_id,
                    "phase": phase,
                }
            )
        with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "task_id": task_id,
                        "tool": "palace_add",
                        "result_preview": '{"saved": true, "store": "palace.run"}',
                    }
                )
                + "\n"
            )
        return _FakeHandle({"status": "complete", "task_id": task_id, "events": []})


@pytest.fixture
def tmp_workspace(tmp_path):
    ws_root = tmp_path / "workspaces" / "test_ws"
    ws_root.mkdir(parents=True)
    drive = ws_root / ".memory" / "drive"
    drive.mkdir(parents=True)
    (drive / "logs").mkdir()
    (drive / "state").mkdir()
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    return {"repo_root": repo_root, "workspace_id": "test_ws", "drive_root": drive}


def test_build_default_plan():
    plan = build_default_plan("ws1")
    assert plan.nodes[0].id == "preflight"
    assert plan.nodes[-1].id == "verify"
    assert all(n.status == "pending" for n in plan.nodes)


def test_phase_prompt_disambiguates_skills_from_enable_tools(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "plan.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []

    prompt = render_phase_user_prompt(manifest, recall)

    assert "Recommended skills (load with `load_skill`, not `enable_tools`)" in prompt
    assert "These are skill slugs, not tool names" in prompt
    assert "- task-decomposition" in prompt


def test_phase_prompt_lists_required_palace_writes(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "research.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        workspace_id=tmp_workspace["workspace_id"],
    )

    assert "Required palace writes before completion" in prompt
    assert "Call `palace_add` at least 3 time(s) for `palace.run`" in prompt
    assert "research_finding" in prompt
    assert "Do not call the completion tool" in prompt


def test_build_phase_task_passes_completion_prerequisites_to_loop(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "research.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.warm = []
    recall.to_payload.return_value = {
        "always_on": [],
        "hot": [],
        "warm": [],
        "graph_neighbours": [],
    }
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(id="research", manifest_id="research")

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="phase-r-prereq",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    prereqs = task["tool_filter"]["completion_prerequisites"]["palace_writes"]
    assert prereqs == [
        {
            "store": "palace.run",
            "tag": "",
            "n": 3,
            "tools": ["palace_add"],
        }
    ]


def test_build_phase_task_passes_required_prior_calls_to_loop(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "preflight.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.warm = []
    recall.to_payload.return_value = {
        "always_on": [],
        "hot": [],
        "warm": [],
        "graph_neighbours": [],
    }
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(id="preflight", manifest_id="preflight")

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="phase-r-preflight",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    prereqs = task["tool_filter"]["completion_prerequisites"]
    assert prereqs["required_tools"] == [
        "env_check",
        "palace_health",
        "mcp_health",
        "skill_audit",
        "read_workspace_charter",
    ]
    assert "Required tool checks before completion" in task["input"]
    assert "read_workspace_charter" in task["input"]


def test_phase_prompt_uses_promote_for_verify_durable_write(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "verify.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        workspace_id=tmp_workspace["workspace_id"],
    )

    assert "Call `promote_to_durable` at least 1 time(s) for `palace.durable`" in prompt
    assert "Call `palace_add` at least" not in prompt


def test_research_review_manifest_allows_current_file_verification(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "research_review.yaml"
    )

    assert {"read_file", "list_files", "read_workspace_charter"}.issubset(
        set(manifest.allowed_tools)
    )
    assert {"get_gmas_context", "search_gmas_knowledge"}.issubset(
        set(manifest.allowed_tools)
    )


def test_execute_prompt_names_current_projected_subtask(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="repair_api",
                title="Repair API integration",
                goal="Fix the failing HTTP create-game endpoint.",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                success_test=SuccessTest(kind="cmd", value="pytest tests/test_api.py -q"),
            )
        ],
    )
    prompt = render_phase_user_prompt(manifest, recall, phase_node=node)

    assert "Current execute subtask queue" in prompt
    assert "`repair_api`" in prompt
    assert "pytest tests/test_api.py -q" in prompt
    assert "GMAS/LLM pre-write gate" not in prompt
    assert 'mark_subtask_complete(subtask_id="repair_api"' in prompt


def test_execute_prompt_adds_gmas_prewrite_gate_only_when_required(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="gmas_agents",
                title="Wire GMAS agent graph",
                goal="Implement LLM-backed GMAS agents and judge node.",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                success_test=SuccessTest(
                    kind="cmd", value="python -m pytest tests/test_agents.py -q"
                ),
            )
        ],
    )

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        phase_node=node,
        gmas_prewrite_required=True,
    )

    assert "GMAS/LLM pre-write gate" in prompt
    assert "before the first `apply_workspace_patch`" in prompt
    assert "Umbrella execute prelude: GMAS context" in prompt
    assert "satisfies the subtask first-write gate" in prompt
    assert 'mark_subtask_complete(subtask_id="gmas_agents"' in prompt


def test_phase_runner_injects_gmas_context_prelude_before_execute_write(
    tmp_workspace, monkeypatch
):
    calls = []

    def fake_build_gmas_context(repo_root, query, max_results, max_chars_per_hit):
        calls.append(
            {
                "repo_root": repo_root,
                "query": query,
                "max_results": max_results,
                "max_chars_per_hit": max_chars_per_hit,
            }
        )
        return {
            "query": query,
            "recommended_pattern": "Use MACPRunner with AgentProfile tools",
            "confidence": 0.9,
            "key_files": ["gmas/examples/agent_with_tools_example.py"],
        }

    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        fake_build_gmas_context,
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=MagicMock(),
    )
    task = {
        "id": "r-gmas:execute",
        "input": "# Phase: execute\n",
        "context_overlays": {
            "gmas_prewrite_required": True,
            "phase_node": {
                "manifest_id": "execute",
                "subtasks": [
                    {
                        "id": "gmas-agents",
                        "status": "pending",
                        "title": "Wire GMAS agent graph",
                        "goal": "Create LLM-backed GMAS agents for bot decisions.",
                        "success_test": {
                            "value": "python -m pytest tests/test_agents.py -q"
                        },
                    }
                ],
            },
        },
    }

    runner._inject_gmas_prewrite_context(task)

    assert calls
    assert "gmas-agents" in calls[0]["query"]
    assert "Umbrella execute prelude: GMAS context" in task["input"]
    assert task["context_overlays"]["gmas_prewrite_context_injected"] == "ok"
    assert task["context_overlays"]["gmas_prewrite_context_subtask_id"] == "gmas-agents"
    tools_log = tmp_workspace["drive_root"] / "logs" / "tools.jsonl"
    rows = [
        json.loads(line)
        for line in tools_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["task_id"] == "r-gmas:execute"
    assert rows[-1]["tool"] == "get_gmas_context"
    assert rows[-1]["args"]["injected_by"] == "umbrella_phase_prelude"
    assert rows[-1]["args"]["active_subtask_id"] == "gmas-agents"

    from ouroboros.tools.registry import ToolContext
    from umbrella.deep_agent_tools.workspace_gmas import (
        _gmas_context_before_write_block,
    )

    workspace_root = tmp_workspace["drive_root"].parent.parent
    (workspace_root / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_workspace["repo_root"],
        host_repo_root=tmp_workspace["repo_root"],
        drive_root=tmp_workspace["drive_root"],
    )
    ctx.task_id = "r-gmas:execute"

    assert (
        _gmas_context_before_write_block(
            ctx, tmp_workspace["workspace_id"], workspace_root
        )
        is None
    )


def test_build_phase_task_loads_execute_existing_api_test_guidance(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.warm = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="feature_tests",
                title="Add feature tests",
                goal="Test the next product slice against existing APIs.",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                success_test=SuccessTest(
                    kind="cmd", value="pytest tests/test_feature.py -q"
                ),
            )
        ],
    )

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-execute",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    assert "Before writing or repairing tests" in task["input"]
    assert "target their actual public APIs" in task["input"]
    assert "do not invent helper classes" in task["input"].lower()
    assert 'read_text(encoding="utf-8")' in task["input"]


def test_build_phase_task_skips_gmas_gate_for_non_agent_execute_subtask(
    tmp_workspace,
):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    (tmp_workspace["drive_root"].parent / "domains.json").write_text(
        json.dumps({"domains": ["multi_agent_gmas"]}),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="repair_api",
                title="Repair API integration",
                goal="Fix the failing HTTP create-game endpoint.",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                success_test=SuccessTest(
                    kind="cmd", value="pytest tests/test_api.py -q"
                ),
            )
        ],
    )

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-gmas",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    assert task["context_overlays"]["detected_domains"] == ["multi_agent_gmas"]
    assert task["context_overlays"]["gmas_prewrite_required"] is False
    assert "GMAS/LLM pre-write gate" not in task["input"]
    assert "LLM/agent runtime policy" not in task["input"]
    assert task["context_overlays"]["domain_policy_files_loaded"] == []


def test_build_phase_task_skips_gmas_gate_for_setup_dependency_leaf(
    tmp_workspace,
):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    (tmp_workspace["drive_root"].parent / "domains.json").write_text(
        json.dumps({"domains": ["multi_agent_gmas"]}),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="project-setup",
                title="Initialize project structure and dependencies",
                goal=(
                    "Create workspace layout with Python backend (src/civgame/...) "
                    "and React+TSX frontend (frontend/src/...), configure "
                    "dependencies, implement entrypoint files. Verify import "
                    "infrastructure and basic build capability."
                ),
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                success_test=SuccessTest(
                    kind="cmd",
                    value="python -m pytest tests/test_project_structure.py -q",
                ),
                files_to_create=[
                    "src/civgame/__init__.py",
                    "src/civgame/game/__init__.py",
                    "src/civgame/api/__init__.py",
                    "src/civgame/engine/__init__.py",
                    "src/civgame/ai/__init__.py",
                    "pyproject.toml",
                    "requirements.txt",
                    "frontend/package.json",
                    "frontend/vite.config.ts",
                    "frontend/tsconfig.json",
                    "frontend/index.html",
                    "frontend/src/main.tsx",
                    "frontend/src/App.tsx",
                    "frontend/src/vite-env.d.ts",
                    "tests/test_project_structure.py",
                    "README.md",
                    "docs/architecture.md",
                ],
            )
        ],
    )

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-gmas",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    assert task["context_overlays"]["detected_domains"] == ["multi_agent_gmas"]
    assert task["context_overlays"]["gmas_prewrite_required"] is False
    assert "GMAS/LLM pre-write gate" not in task["input"]
    assert "LLM/agent runtime policy" not in task["input"]


def test_build_phase_task_injects_gmas_gate_for_agent_execute_subtask(
    tmp_workspace,
):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    (tmp_workspace["drive_root"].parent / "domains.json").write_text(
        json.dumps({"domains": ["multi_agent_gmas"]}),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.warm = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="gmas_agents",
                title="Wire GMAS agent graph",
                goal="Create LLM-backed GMAS bots for civilization turns.",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                success_test=SuccessTest(
                    kind="cmd", value="pytest tests/test_agents.py -q"
                ),
            )
        ],
    )

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-gmas",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    assert task["context_overlays"]["detected_domains"] == ["multi_agent_gmas"]
    assert task["context_overlays"]["gmas_prewrite_required"] is True
    assert "GMAS/LLM pre-write gate" in task["input"]
    assert "LLM/agent runtime policy" in task["input"]
    assert "rule-based AI" in task["input"]
    assert "Forbidden replacement behavior" in task["input"]
    assert (
        "umbrella/prompts/policies/llm_agent_runtime.md"
        in task["context_overlays"]["domain_policy_files_loaded"]
    )


def test_build_phase_task_loads_manifest_prompt_files(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "plan.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(id="plan", manifest_id="plan")

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-plan-prompts",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    assert "## Phase instructions loaded from manifest" in task["input"]
    assert "umbrella/prompts/phases/plan.system.md" in task["input"]
    assert "Every executable leaf subtask" in task["input"]
    assert "umbrella/prompts/phases/plan.user_overlay.md" in task["input"]
    assert "LLM/agent runtime policy" not in task["input"]
    assert "umbrella/prompts/phases/plan.system.md" in task["context_overlays"][
        "phase_prompt_files_loaded"
    ]


def test_build_phase_task_passes_manifest_warm_search_and_task_query_seed(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "plan.yaml"
    )
    workspace_root = tmp_workspace["drive_root"].parent.parent
    (workspace_root / "TASK_MAIN.md").write_text(
        "Build a Civilization game with LLM diplomacy.",
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = [
        {
            "content": "Research finding should be automatically hot for planning.",
        }
    ]
    recall.warm = [
        {
            "content": "Durable planning lesson should be rendered.",
        }
    ]
    recall.to_payload.return_value = {
        "always_on": [],
        "hot": recall.hot,
        "warm": recall.warm,
        "graph_neighbours": [],
    }
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(id="plan", manifest_id="plan")

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-plan-memory",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    kwargs = palace.recall.call_args.kwargs
    assert any(
        "research_finding" in tuple(getattr(rule, "tags", ()))
        for rule in kwargs["hot_rules"]
    )
    assert kwargs["warm_search_rules"] == manifest.memory.warm_search
    assert "Civilization game with LLM diplomacy" in kwargs["query_seed"]
    assert "## Warm context (cross-run search)" in task["input"]
    assert "Durable planning lesson should be rendered." in task["input"]


def test_build_phase_task_loads_detected_domains_from_active_skills(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "research.yaml"
    )
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "active_skills.json").write_text(
        json.dumps(
            {
                "entry": {
                    "workspace_id": tmp_workspace["workspace_id"],
                    "domains": ["multi_agent_gmas"],
                }
            }
        ),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(id="research", manifest_id="research")

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-active-skills",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    assert task["context_overlays"]["detected_domains"] == ["multi_agent_gmas"]
    assert "LLM/agent runtime policy" in task["input"]


def test_build_phase_task_context_overlay_is_json_serializable(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []
    recall.to_payload.return_value = {}
    palace = MagicMock()
    palace.recall.return_value = recall
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="st_json",
                title="JSON-safe context",
                goal="Keep projected subtasks visible to Ouroboros.",
                allowed_tools=frozenset({"shell"}),
                allowed_skills=frozenset({"task-decomposition"}),
                success_test=SuccessTest(kind="cmd", value="pytest -q"),
            )
        ],
    )

    task = build_phase_task(
        phase_node=node,
        manifest=manifest,
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-json",
        palace=palace,
        drive_root=tmp_workspace["drive_root"],
        repo_root=tmp_workspace["repo_root"],
    )

    json.dumps(task["context_overlays"], ensure_ascii=False)
    subtask = task["context_overlays"]["phase_node"]["subtasks"][0]
    assert subtask["allowed_tools"] == ["shell"]
    assert subtask["allowed_skills"] == ["task-decomposition"]


def test_plan_review_prompt_includes_authoritative_submitted_plan(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "plan_review.yaml"
    )
    latest = {
        "run_id": "r-plan",
        "workspace_id": tmp_workspace["workspace_id"],
        "plan": {"subtasks": [{"id": "real-latest", "verification": "pytest"}]},
        "notes": "",
    }
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(latest),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = [
        {
            "content": '{"plan": {"text": "stale malformed plan from palace"}}',
        }
    ]

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        authoritative_artifacts=authoritative_artifacts_for_phase(
            manifest_id=manifest.id,
            drive_root=tmp_workspace["drive_root"],
        ),
    )

    assert "## Authoritative review artifacts" in prompt
    assert "phase_plan_submitted_latest.json" in prompt
    assert "real-latest" in prompt
    assert "stale malformed plan from palace" in prompt
    assert prompt.index("real-latest") < prompt.index("stale malformed plan")


def test_execute_prompt_includes_authoritative_latest_plan(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    current_plan = {
        "run_id": "r-execute",
        "workspace_id": tmp_workspace["workspace_id"],
        "plan_id": "current",
        "nodes": [
            {"id": "plan", "manifest_id": "plan", "status": "done"},
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "setup_project_structure",
                        "title": "Setup project",
                        "status": "pending",
                    }
                ],
            },
        ],
    }
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(current_plan),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = [
        {
            "content": '{"plan": {"subtasks": [{"id": "stale_old_game_core"}]}}',
        }
    ]

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        authoritative_artifacts=authoritative_artifacts_for_phase(
            manifest_id=manifest.id,
            drive_root=tmp_workspace["drive_root"],
            run_id="r-execute",
        ),
    )

    assert "## Authoritative phase artifacts" in prompt
    assert "phase_plan.json" in prompt
    assert "setup_project_structure" in prompt
    assert "stale_old_game_core" in prompt
    assert prompt.index("setup_project_structure") < prompt.index("stale_old_game_core")


def test_execute_prompt_rejects_phase_plan_without_current_run_id(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    stale_plan = {
        "workspace_id": tmp_workspace["workspace_id"],
        "plan_id": "legacy-without-run",
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [{"id": "stale_subtask_without_run_id"}],
            }
        ],
    }
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(stale_plan),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        authoritative_artifacts=authoritative_artifacts_for_phase(
            manifest_id=manifest.id,
            drive_root=tmp_workspace["drive_root"],
            run_id="r-current",
        ),
    )

    assert "MISSING: no current phase plan state was found" in prompt
    assert "stale_subtask_without_run_id" not in prompt


def test_execute_prompt_omits_latest_plan_proposal_as_authoritative(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "execute.yaml"
    )
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "r-execute",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "current",
                "nodes": [{"id": "execute", "manifest_id": "execute"}],
            }
        ),
        encoding="utf-8",
    )
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-execute",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {"subtasks": [{"id": "stale_extra_subtask"}]},
            }
        ),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        authoritative_artifacts=authoritative_artifacts_for_phase(
            manifest_id=manifest.id,
            drive_root=tmp_workspace["drive_root"],
            run_id="r-execute",
        ),
    )

    assert "phase_plan.json" in prompt
    assert "phase_plan_proposal_latest.json" not in prompt
    assert "stale_extra_subtask" not in prompt


def test_research_review_prompt_includes_authoritative_latest_summary(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "research_review.yaml"
    )
    latest = {
        "run_id": "r-research",
        "workspace_id": tmp_workspace["workspace_id"],
        "architecture_id": "real-research-arch",
        "findings_ids": ["gmas", "web-game"],
        "notes": "Use FastAPI plus TSX and GMAS-backed bot agents.",
    }
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps(latest),
        encoding="utf-8",
    )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = [{"content": "palace has not indexed research yet"}]

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        authoritative_artifacts=authoritative_artifacts_for_phase(
            manifest_id=manifest.id,
            drive_root=tmp_workspace["drive_root"],
            run_id="r-research",
        ),
    )

    assert "## Authoritative review artifacts" in prompt
    assert "research_summary_latest.json" in prompt
    assert "real-research-arch" in prompt
    assert "palace has not indexed research yet" in prompt
    assert prompt.index("real-research-arch") < prompt.index("palace has not indexed")


def test_research_review_authoritative_summary_ignores_stale_run_artifact(tmp_workspace):
    manifest = load_manifest(
        tmp_workspace["repo_root"]
        / "umbrella"
        / "phases"
        / "manifests"
        / "research_review.yaml"
    )
    state = tmp_workspace["drive_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps({"run_id": "old-run", "architecture_id": "stale"}),
        encoding="utf-8",
    )
    with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "kind": "submit_research_summary",
                    "task_id": "new-run:research",
                    "phase": "research",
                    "created_at": 123.0,
                    "payload": {"architecture_id": "fresh-from-signal"},
                }
            )
            + "\n"
        )
    recall = MagicMock()
    recall.always_on = []
    recall.hot = []

    prompt = render_phase_user_prompt(
        manifest,
        recall,
        authoritative_artifacts=authoritative_artifacts_for_phase(
            manifest_id=manifest.id,
            drive_root=tmp_workspace["drive_root"],
            run_id="new-run",
        ),
    )

    assert "fresh-from-signal" in prompt
    assert "stale" not in prompt


def test_research_summary_counts_as_run_memory_write(tmp_path):
    runner = object.__new__(PhaseRunner)
    runner._drive_root = tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "task_id": "run-1:research",
                    "tool": "palace_add",
                    "result_preview": json.dumps(
                        {"saved": True, "id": "memory-1", "legacy": {"id": "finding-1"}}
                    ),
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "task_id": "run-1:research",
                    "tool": "submit_research_summary",
                    "args": {
                        "architecture_id": "arch-1",
                        "findings_ids": ["finding-1"],
                        "notes": "Concrete research notes that summarize real findings.",
                    },
                    "result_preview": "OK: Research summary submitted",
                }
            )
            + "\n"
        )
    rule = MagicMock()
    rule.store = "palace.run"
    rule.tag = None

    count = runner._phase_required_palace_write_count(
        task_id="run-1:research",
        rule=rule,
    )

    assert count == 2


def test_phase_plan_proposal_counts_as_run_memory_write(tmp_path):
    runner = object.__new__(PhaseRunner)
    runner._drive_root = tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:plan",
                "tool": "propose_phase_plan",
                "args": {
                    "plan": {
                        "plan_id": "plan-1",
                        "subtasks": [
                            {
                                "id": "build",
                                "success_test": "python -m pytest tests -q",
                            }
                        ],
                    }
                },
                "result_preview": "OK: phase plan proposal recorded",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rule = MagicMock()
    rule.store = "palace.run"
    rule.tag = None

    count = runner._phase_required_palace_write_count(
        task_id="run-1:plan",
        rule=rule,
    )

    assert count == 1


def test_latest_phase_plan_floor_rejects_over_granular_greenfield_plan(tmp_path):
    runner = object.__new__(PhaseRunner)
    runner._drive_root = tmp_path / "drive"
    runner._repo_root = tmp_path
    runner._workspace_id = "test_ws"
    state = runner._drive_root / "state"
    state.mkdir(parents=True)
    (tmp_path / "workspaces" / "test_ws").mkdir(parents=True)
    subtasks = [
        {
            "id": f"slice_{idx}",
            "title": f"Build game slice {idx}",
            "goal": "Implement one vertical backend/frontend LLM game slice.",
            "files_to_create": (
                ["docs/architecture.md"]
                if idx == 0
                else [f"src/civ/slice_{idx}.py", f"tests/test_slice_{idx}.py"]
            ),
            "success_test": f"python -m pytest tests/test_slice_{idx}.py -q",
        }
        for idx in range(17)
    ]
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "workspace_id": "test_ws",
                "plan_id": "too-large",
                "plan": {
                    "summary": "Large FastAPI React GMAS civilization game.",
                    "subtasks": subtasks,
                },
            }
        ),
        encoding="utf-8",
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="run-1")

    assert "17 executable leaves" in failure
    assert "8-16" in failure


def test_research_summary_with_fake_findings_does_not_count_as_run_memory_write(
    tmp_path,
):
    runner = object.__new__(PhaseRunner)
    runner._drive_root = tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "submit_research_summary",
                "args": {
                    "architecture_id": "arch-1",
                    "findings_ids": ["finding_001"],
                    "notes": "Concrete looking notes with invented finding identifiers.",
                },
                "result_preview": "OK: Research summary submitted",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rule = MagicMock()
    rule.store = "palace.run"
    rule.tag = None

    count = runner._phase_required_palace_write_count(
        task_id="run-1:research",
        rule=rule,
    )

    assert count == 0


def test_research_summary_finding_floor_derived_from_manifest():
    manifest = MagicMock()
    rule = MagicMock()
    rule.store = "palace.run"
    rule.n = 4
    manifest.exit_criteria.required_palace_writes = ()
    manifest.exit_criteria.min_palace_writes = (rule,)

    assert PhaseRunner._research_summary_min_valid_findings_for_manifest(manifest) == 4


def test_latest_research_summary_requires_manifest_finding_floor(tmp_path):
    runner = object.__new__(PhaseRunner)
    runner._drive_root = tmp_path / "drive"
    logs = runner._drive_root / "logs"
    state = runner._drive_root / "state"
    logs.mkdir(parents=True)
    state.mkdir(parents=True)
    rows = []
    for idx in range(2):
        finding_id = f"finding-{idx + 1}"
        rows.append(
            {
                "task_id": "phase_web_1254769e:research",
                "tool": "palace_add",
                "result_preview": json.dumps({"saved": True, "id": finding_id}),
            }
        )
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_1254769e",
                "task_id": "phase_web_1254769e:research",
                "architecture_id": "arch-civilization-gmas-web-v1",
                "findings_ids": ["finding-1", "finding-2"],
                "notes": "Concrete research handoff with only two accepted findings.",
            }
        ),
        encoding="utf-8",
    )

    failure = runner._latest_research_summary_handoff_failure(
        run_id="phase_web_1254769e",
        min_valid_findings=3,
    )

    assert "2/3 accepted palace_add" in failure


def test_save_and_load_plan(tmp_path):
    plan = build_default_plan("ws1", run_id="run-42")
    save_plan(plan, tmp_path)
    loaded = load_plan(tmp_path)
    assert loaded is not None
    assert loaded.run_id == "run-42"
    assert len(loaded.nodes) == len(plan.nodes)


def test_runner_rebuilds_stale_phase_plan_for_new_run(tmp_workspace):
    old_plan = build_default_plan(tmp_workspace["workspace_id"], run_id="old-run")
    for node in old_plan.nodes:
        node.status = "done"
    save_plan(old_plan, tmp_workspace["drive_root"])

    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=_FakeLauncher(tmp_workspace["drive_root"]),
    )
    list(runner.run("test task", phases=["preflight"], run_id="new-run"))
    loaded = load_plan(tmp_workspace["drive_root"])
    assert loaded.run_id == "new-run"
    assert [node.id for node in loaded.nodes] == ["preflight"]


def test_runner_dry_run(tmp_workspace):
    """Dry run returns manifest info without calling LLM."""
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
    )
    results = list(runner.run("Build a web API", dry_run=True, run_id="r1"))
    assert len(results) == 1
    assert results[0].ok is True
    data = results[0].data
    assert "phases" in data or "manifests_ok" in data


def test_runner_no_launcher_marks_done(tmp_workspace):
    """A fake launcher plus required phase signal advances phases to done."""
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=_FakeLauncher(tmp_workspace["drive_root"]),
    )
    results = list(runner.run("test task", phases=["preflight"], run_id="r2"))
    assert any(r.ok for r in results)


def test_runner_fails_when_required_phase_call_missing(tmp_workspace):
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=_FakeLauncher(tmp_workspace["drive_root"], write_required_signal=False),
    )
    results = list(runner.run("test task", phases=["preflight"], run_id="r-missing"))
    assert any(not r.ok for r in results)
    loaded = load_plan(tmp_workspace["drive_root"])
    assert loaded.get_node("preflight").status == "failed"


def test_runner_fails_execute_without_workspace_write(tmp_workspace):
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=_FakeLauncher(tmp_workspace["drive_root"]),
    )
    results = list(runner.run("test task", phases=["execute"], run_id="r-empty-exec"))
    assert any(not r.ok for r in results)
    assert "without any effective workspace write" in results[-1].errors[0].message


def test_runner_retries_execute_missing_required_mark(tmp_workspace):
    launcher = _ExecuteRetryFakeLauncher(tmp_workspace["drive_root"])
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=launcher,
    )

    results = list(runner.run("test task", phases=["execute"], run_id="r-exec-retry"))

    assert all(r.ok for r in results)
    assert launcher.submitted.count("r-exec-retry:execute") == 2
    assert any(
        (r.data or {}).get("retry_reason", "").startswith(
            "phase exit criteria missing required call"
        )
        for r in results
        if r.ok
    )


def test_runner_retries_execute_with_captured_failure_context_and_memory(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_ccadf809",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "phase_plan:slice1_game_core",
                "plan": {
                    "subtasks": [
                        {
                            "id": "core_models",
                            "title": "Implement Core Game Models and State",
                            "goal": "Create core models and state utilities.",
                            "files_to_create": [
                                "src/civilization/models.py",
                                "src/civilization/state.py",
                                "tests/test_models.py",
                            ],
                            "success_test": {
                                "kind": "cmd",
                                "value": "python -m pytest tests/test_models.py -v --tb=short",
                            },
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    launcher = _ExecuteCapturedFailureRetryFakeLauncher(drive)
    palace_repo_root = drive.parent.parent.parent.parent
    palace = MemPalace(palace_repo_root, tmp_workspace["workspace_id"])
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=launcher,
        palace=palace,
    )

    results = list(
        runner.run("build the civilization project", phases=["execute"], run_id="phase_web_ccadf809")
    )

    assert all(r.ok for r in results)
    assert launcher.submitted == [
        "phase_web_ccadf809:execute",
        "phase_web_ccadf809:execute",
    ]
    retry_input = launcher.tasks[1]["input"]
    assert "last_task_result_excerpt" in retry_input
    assert "23/29" in retry_input
    assert "patch_hunk_mismatch" in retry_input
    loaded = load_plan(drive)
    execute = loaded.get_node("execute")
    assert execute.status == "done"
    assert execute.subtasks[0].completion["summary"].startswith("core model repair")
    memories = palace.list_all(stores=["palace.subtask"], n=10)
    assert any(
        "phase_retry_context" in str(item.get("content") or "")
        and "core_models" in str(item.get("content") or "")
        and "23/29" in str(item.get("content") or "")
        for item in memories
    )


def test_runner_projects_latest_plan_subtasks_and_executes_them_one_at_a_time(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-subtasks",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "accepted-plan",
                "plan": {
                    "subtasks": [
                        {"id": "first", "title": "First repair", "success_test": "pytest a"},
                        {"id": "second", "title": "Second repair", "success_test": "pytest b"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    launcher = _ExecuteSubtaskQueueFakeLauncher(drive)
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=launcher,
    )

    results = list(runner.run("test task", phases=["execute"], run_id="r-subtasks"))

    assert all(r.ok for r in results)
    assert launcher.submitted == ["r-subtasks:execute", "r-subtasks:execute"]
    loaded = load_plan(drive)
    execute = loaded.get_node("execute")
    assert [card.status for card in execute.subtasks] == ["done", "done"]
    assert any(
        "execute phase still has incomplete subtask" in (r.data or {}).get("retry_reason", "")
        for r in results
        if r.ok
    )


def test_runner_projects_verification_aliases_as_success_tests(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-verification-aliases",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "accepted-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "fix_api",
                            "title": "Fix API",
                            "verification": "pytest tests/test_api.py -q",
                        },
                        {
                            "id": "verify_browser",
                            "title": "Verify Browser",
                            "verification_commands": [
                                "npm run build",
                                "run_workspace_verify",
                            ],
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    launcher = _ExecuteSubtaskQueueFakeLauncher(drive)
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=launcher,
    )

    results = list(
        runner.run("test task", phases=["execute"], run_id="r-verification-aliases")
    )

    assert all(r.ok for r in results)
    loaded = load_plan(drive)
    execute = loaded.get_node("execute")
    assert [card.success_test.value for card in execute.subtasks] == [
        "pytest tests/test_api.py -q",
        "npm run build; run_workspace_verify",
    ]


def test_runner_prefers_concrete_verification_command_over_bare_success_alias(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-concrete-alias",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "accepted-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "validate_config",
                            "title": "Validate config",
                            "success_test": "run_workspace_verify",
                            "verification_command": (
                                "python -m pytest tests/test_config.py -q"
                            ),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_ExecuteSubtaskQueueFakeLauncher(drive),
    )

    results = list(runner.run("test task", phases=["execute"], run_id="r-concrete-alias"))

    assert all(r.ok for r in results)
    execute = load_plan(drive).get_node("execute")
    assert execute.subtasks[0].success_test.value == (
        "python -m pytest tests/test_config.py -q"
    )


def test_runner_does_not_project_execute_subtasks_before_plan_review_passes(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-wait-review",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "pending-review-plan",
                "plan": {
                    "subtasks": [
                        {"id": "build", "title": "Build", "success_test": "pytest -q"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    plan = build_default_plan(
        tmp_workspace["workspace_id"],
        run_id="r-wait-review",
        phases=["plan", "plan_review", "execute"],
    )
    plan.get_node("plan").status = "done"
    plan.get_node("plan_review").status = "pending"
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    assert not runner._sync_execute_subtasks_from_latest_plan(
        plan,
        run_id="r-wait-review",
    )
    assert plan.get_node("execute").subtasks is None


def test_runner_projects_latest_plan_phases_as_execute_subtasks(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-plan-phases",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "accepted-plan",
                "plan": {
                    "phases": [
                        {
                            "id": "fix_api",
                            "title": "Fix API",
                            "description": "Repair the game create endpoint.",
                            "success_test": "pytest tests/test_api.py -q",
                        },
                        {
                            "id": "verify_gameplay",
                            "title": "Verify Gameplay",
                            "test_strategy": "run_workspace_verify",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    launcher = _ExecuteSubtaskQueueFakeLauncher(drive)
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=launcher,
    )

    results = list(
        runner.run("test task", phases=["execute"], run_id="r-plan-phases")
    )

    assert all(r.ok for r in results)
    loaded = load_plan(drive)
    execute = loaded.get_node("execute")
    assert [card.id for card in execute.subtasks] == ["fix_api", "verify_gameplay"]
    assert [card.status for card in execute.subtasks] == ["done", "done"]


def test_latest_phase_plan_execution_floor_requires_each_subtask_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-missing-success",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {"id": "has_test", "verification": "pytest -q"},
                        {"id": "missing_test", "title": "Missing test"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    assert not runner._latest_phase_plan_has_execution_floor(
        run_id="r-missing-success"
    )
    assert "missing_test" in runner._latest_phase_plan_execution_floor_failure(
        run_id="r-missing-success"
    )


def test_latest_phase_plan_execution_floor_accepts_ordered_subtasks(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-ordered",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "ordered-plan",
                "plan": {
                    "ordered_subtasks": [
                        {
                            "id": "diagnose",
                            "title": "Diagnose current state",
                            "verification": "pytest -q",
                        },
                        {
                            "id": "verify_ui",
                            "title": "Verify UI",
                            "test_strategy": "run_real_e2e",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-ordered",
        nodes=[PhaseNode(id="execute", manifest_id="execute", status="pending")],
    )

    assert runner._latest_phase_plan_execution_floor_failure(run_id="r-ordered") == ""
    assert runner._sync_execute_subtasks_from_latest_plan(plan, run_id="r-ordered")
    execute = plan.get_node("execute")
    assert execute is not None
    assert [card.id for card in execute.subtasks or []] == ["diagnose", "verify_ui"]
    assert [card.success_test.value for card in execute.subtasks or []] == [
        "pytest -q",
        "run_real_e2e",
    ]


def test_phase_plan_execution_floor_uses_submitted_plan_over_latest(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-selected",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "submitted",
                "plan": {
                    "subtasks": [
                        {
                            "id": "submitted_subtask",
                            "title": "Submitted subtask",
                            "goal": "Use the submitted contract.",
                            "files_to_create": [
                                "src/demo/app.py",
                                "tests/test_submitted.py",
                            ],
                            "success_test": "python -m pytest tests/test_submitted.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-selected",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "unsubmitted-latest",
                "plan": {
                    "subtasks": [
                        {
                            "id": "broken_latest",
                            "title": "Broken latest",
                            "goal": "This proposal was not submitted.",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-selected",
        nodes=[PhaseNode(id="execute", manifest_id="execute", status="pending")],
    )

    assert runner._latest_phase_plan_execution_floor_failure(run_id="r-selected") == ""
    assert runner._sync_execute_subtasks_from_latest_plan(plan, run_id="r-selected")
    execute = plan.get_node("execute")
    assert execute is not None
    assert [card.id for card in execute.subtasks or []] == ["submitted_subtask"]


def test_latest_phase_plan_execution_floor_rejects_unowned_pytest_target(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-unowned-pytest-target",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-docs-target-gap",
                "plan": {
                    "subtasks": [
                        {
                            "id": "docs-env-contract",
                            "title": "Write README, architecture docs, and LLM env contract",
                            "goal": "Document project purpose and LLM runtime aliases.",
                            "files_to_create": [
                                "README.md",
                                ".env.example",
                                "docs/architecture.md",
                                "docs/agent_topology.md",
                            ],
                            "success_test": "python -m pytest tests/test_docs.py -q",
                        },
                        {
                            "id": "project-setup",
                            "title": "Initialize project",
                            "goal": "Create Python package metadata.",
                            "files_to_create": [
                                "pyproject.toml",
                                "src/civiz/__init__.py",
                                "tests/test_dependencies.py",
                            ],
                            "success_test": "python -m pytest tests/test_dependencies.py -q",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-unowned-pytest-target"
    )

    assert "tests/test_docs.py" in failure
    assert "unavailable pytest proof target" in failure


def test_sync_execute_subtasks_updates_same_id_contract_changes(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-revised",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "submitted",
                "plan": {
                    "subtasks": [
                        {
                            "id": "same_id",
                            "title": "Revised title",
                            "goal": "Run the revised success test.",
                            "files_to_create": ["src/demo/app.py"],
                            "success_test": "python -m pytest tests/test_revised.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-revised",
        nodes=[
            PhaseNode(
                id="execute",
                manifest_id="execute",
                status="pending",
                subtasks=[
                    SubtaskCard(
                        id="same_id",
                        title="Old title",
                        goal="Run the old success test.",
                        allowed_tools=frozenset(),
                        allowed_skills=frozenset(),
                        success_test=SuccessTest(
                            kind="cmd",
                            value="python -m pytest tests/test_old.py -q",
                        ),
                    )
                ],
            )
        ],
    )

    assert runner._sync_execute_subtasks_from_latest_plan(plan, run_id="r-revised")
    execute = plan.get_node("execute")
    assert execute is not None
    assert execute.subtasks
    assert execute.subtasks[0].title == "Revised title"
    assert (
        execute.subtasks[0].success_test.value
        == "python -m pytest tests/test_revised.py -q"
    )


def test_runner_projects_nested_phase_leaf_subtasks(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-nested",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "phases": [
                        {
                            "id": "umbrella_fix",
                            "title": "Fix grouped behavior",
                            "subtasks": [
                                {
                                    "id": "repair_api",
                                    "title": "Repair API",
                                    "files_to_create": [
                                        "src/civgame/api.py",
                                        "tests/test_api.py",
                                        "docs/architecture.md",
                                    ],
                                    "verification": "pytest tests/test_api.py -q",
                                },
                                {
                                    "id": "verify_ui",
                                    "title": "Verify UI",
                                    "files_to_change": ["frontend/src/App.tsx"],
                                    "dependencies": ["repair_api"],
                                    "verification": "npm run build",
                                },
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-nested",
        nodes=[PhaseNode(id="execute", manifest_id="execute", status="pending")],
    )

    assert runner._latest_phase_plan_execution_floor_failure(run_id="r-nested") == ""
    assert runner._sync_execute_subtasks_from_latest_plan(plan, run_id="r-nested")
    execute = plan.get_node("execute")
    assert execute is not None
    assert [card.id for card in execute.subtasks or []] == ["repair_api", "verify_ui"]
    assert execute.subtasks[0].files_to_create == [
        "src/civgame/api.py",
        "tests/test_api.py",
        "docs/architecture.md",
    ]
    assert execute.subtasks[1].files_to_change == ["frontend/src/App.tsx"]
    assert execute.subtasks[1].dependencies == ["repair_api"]


def test_latest_phase_plan_execution_floor_rejects_user_report_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-human-test",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "manual_play",
                            "title": "Manual play test",
                            "success_test": (
                                "Manual 10-turn gameplay session completes and "
                                "user reports game is playable"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-human-test"
    )
    assert "non-automatable" in failure
    assert "manual_play" in failure


def test_latest_phase_plan_execution_floor_rejects_manual_browser_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-manual-browser",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "ui_check",
                            "success_test": (
                                "Manual verification: load http://localhost:8080 "
                                "in browser and create a game"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-manual-browser"
    )
    assert "non-automatable" in failure
    assert "ui_check" in failure


def test_latest_phase_plan_execution_floor_rejects_descriptive_browser_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-browser-description",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "manual_e2e_verification",
                            "success_test": (
                                "Server starts cleanly; browser opens to localhost:5173; "
                                "human player completes 3 turns with AI responses visible; "
                                "browser console has zero errors; WebSocket messages show "
                                "in network inspector"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-browser-description"
    )
    assert "non-automatable" in failure
    assert "manual_e2e_verification" in failure


def test_latest_phase_plan_execution_floor_rejects_vague_documentation_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-vague-test",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "diagnose_contracts",
                            "success_test": (
                                "Documentation of actual signatures and exports"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-vague-test"
    )

    assert "non-automatable" in failure
    assert "diagnose_contracts" in failure


def test_latest_phase_plan_execution_floor_rejects_acceptance_without_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-acceptance-only",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "acceptance_only",
                            "acceptance_criteria": "pytest tests/test_api.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-acceptance-only"
    )

    assert "without success tests" in failure
    assert "acceptance_only" in failure


def test_latest_phase_plan_execution_floor_rejects_bare_verify_overuse(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-generic-overuse",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "generic-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": f"build_piece_{idx}",
                            "title": f"Build piece {idx}",
                            "success_test": "run_workspace_verify",
                            "files_affected": [
                                f"src/test_ws/piece_{idx}.py",
                                *(
                                    ["docs/architecture.md"]
                                    if idx == 0
                                    else []
                                ),
                            ],
                        }
                        for idx in range(6)
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-generic-overuse"
    )

    assert "bare `run_workspace_verify`" in failure


def test_latest_phase_plan_execution_floor_rejects_bare_verify_final_gate(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_f0cee725",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-bare-deployment-verify",
                "plan": {
                    "subtasks": [
                        {
                            "id": "localhost-deployment",
                            "title": "Localhost Deployment",
                            "goal": "Deploy and verify localhost game server.",
                            "files_to_create": ["tests/test_localhost_deployment.py"],
                            "success_test": "run_workspace_verify",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_f0cee725"
    )

    assert "bare `run_workspace_verify`" in failure
    assert "localhost-deployment" in failure


def test_latest_phase_plan_execution_floor_rejects_unmanaged_localhost_curl(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_92978867",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "localhost-verification",
                            "title": "Localhost verification",
                            "goal": "Verify backend and frontend game flow.",
                            "files_to_create": [
                                "tests/integration/full_game_flow.py"
                            ],
                            "success_test": (
                                "curl -f http://127.0.0.1:8000/health && "
                                "cd frontend && npm run build && "
                                "python -m pytest "
                                "tests/integration/full_game_flow.py -q"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_92978867"
    )

    assert "direct HTTP shell command" in failure
    assert "localhost-verification" in failure


def test_latest_phase_plan_execution_floor_rejects_frontend_test_path_mismatch(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_92978867",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "player-action-panels",
                            "title": "Player action panels",
                            "goal": "Implement frontend panel tests.",
                            "files_to_create": ["tests/frontend/panels.test.ts"],
                            "success_test": "cd frontend && npm test -- panels.test.ts",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_92978867"
    )

    assert "player-action-panels" in failure
    assert "outside the frontend package" in failure


def test_latest_phase_plan_execution_floor_rejects_success_test_list(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-list-success",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "list-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "env_setup",
                            "title": "Set up environments",
                            "success_test": [
                                "python -m pytest tests/test_backend.py -q",
                                "cd frontend && npm run build",
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-list-success"
    )

    assert "success_test must be a single executable" in failure


def test_latest_phase_plan_execution_floor_rejects_option_only_success_test_object(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-option-only-success-test",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "option-only-success-test-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "game_engine",
                            "title": "Build game engine",
                            "goal": "Create deterministic game engine.",
                            "files_to_create": ["src/demo/game_engine.py"],
                            "success_test": {
                                "type": "python",
                                "command": "-m pytest tests/test_game_engine.py -v",
                            },
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-option-only-success-test"
    )

    assert "missing an executable" in failure
    assert "python -m pytest" in failure


def test_latest_phase_plan_execution_floor_rejects_invalid_python_and_shell(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-bad-command",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "bad-command-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "tools_import",
                            "title": "Validate tools import",
                            "success_test": (
                                "python -c \"from bot_engine.tools import "
                                "analyze_economy, evaluate_trade proposal\""
                            ),
                        },
                        {
                            "id": "localhost",
                            "title": "Verify localhost",
                            "success_test": (
                                "ps aux | grep uvicorn || "
                                "(uvicorn backend.main:app & sleep 2 && pkill -f uvicorn)"
                            ),
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-bad-command"
    )

    assert "invalid `python -c` code" in failure
    assert "non-portable or unmanaged shell/process-control" in failure


def test_latest_phase_plan_execution_floor_rejects_shell_masked_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_f0cee725",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-masked-pytest",
                "plan": {
                    "subtasks": [
                        {
                            "id": "project-setup",
                            "title": "Project setup",
                            "goal": "Create package skeleton and import tests.",
                            "files_to_create": [
                                "src/test_ws/__init__.py",
                                "tests/test_pkg_imports.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_pkg_imports.py -q || true"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_f0cee725"
    )

    assert "masks command failure" in failure
    assert "project-setup" in failure


def test_latest_phase_plan_execution_floor_rejects_frontend_build_before_entrypoint(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_ce127a9e",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-frontend-build-before-entrypoint",
                "plan": {
                    "subtasks": [
                        {
                            "id": "project-setup",
                            "title": "Initialize Python + React project with dependencies",
                            "goal": (
                                "Create pyproject.toml, frontend package metadata, "
                                "Vite config, env example, and README."
                            ),
                            "files_to_create": [
                                "pyproject.toml",
                                "frontend/package.json",
                                "frontend/vite.config.ts",
                                "frontend/tsconfig.json",
                                ".env.example",
                                "README.md",
                            ],
                            "success_test": "cd frontend && npm run build",
                        },
                        {
                            "id": "frontend-setup",
                            "title": "Initialize React + TypeScript + Vite frontend",
                            "goal": "Create the frontend source entrypoint.",
                            "files_to_create": [
                                "frontend/src/main.tsx",
                                "frontend/src/App.tsx",
                                "frontend/src/index.css",
                            ],
                            "success_test": "cd frontend && npm run build",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_ce127a9e"
    )

    assert "frontend build success_test before the files needed" in failure
    assert "project-setup" in failure
    assert "frontend/src/<entry>.tsx" in failure
    assert "frontend/index.html" in failure


def test_latest_phase_plan_execution_floor_rejects_workspace_prefixed_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-workspace-prefixed-test",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "workspace-prefixed-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "backend_tests",
                            "title": "Run backend tests",
                            "goal": "Verify backend behavior.",
                            "success_test": (
                                "cd workspaces/test_ws/backend && "
                                "python -m pytest tests/test_api.py -q"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-workspace-prefixed-test"
    )

    assert "host workspace path" in failure


def test_latest_phase_plan_execution_floor_rejects_captured_cd_src_pytest(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_921912db",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-cd-src-pytest",
                "plan": {
                    "subtasks": [
                        {
                            "id": "docs-architecture",
                            "title": "Document game architecture",
                            "goal": "Create durable architecture docs and tests.",
                            "files_to_create": [
                                "docs/architecture.md",
                                "docs/agent_topology.md",
                                "tests/test_architecture.py",
                            ],
                            "success_test": (
                                "cd src && python -m pytest "
                                "tests/test_architecture.py -q"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_921912db"
    )

    assert "changes into source root `src`" in failure
    assert "docs-architecture" in failure


def test_latest_phase_plan_execution_floor_rejects_direct_python_pytest_node(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-direct-python-pytest",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "direct-python-pytest-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_connectivity",
                            "title": "Validate LLM connectivity",
                            "success_test": (
                                "python tests/test_llm_config.py::test_llm_connectivity"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-direct-python-pytest"
    )

    assert "python -m pytest" in failure


def test_latest_phase_plan_execution_floor_rejects_bare_assert_shell_segment(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-bare-assert",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "bare-assert-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "frontend_build",
                            "title": "Build frontend",
                            "success_test": (
                                "cd frontend && npm run build && "
                                "assert os.path.exists('frontend/dist/index.html')"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-bare-assert"
    )

    assert "bare Python `assert`" in failure


def test_latest_phase_plan_execution_floor_rejects_bash_script_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-bash-script",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "bash-script-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "deployment",
                            "title": "Deployment verification",
                            "success_test": "bash tests/deployment/test_launch.sh",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-bash-script")

    assert "non-portable or unmanaged shell/process-control" in failure


def test_latest_phase_plan_execution_floor_rejects_exit_status_shell_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-exit-status",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "exit-status-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "frontend_typecheck",
                            "title": "Type-check frontend",
                            "success_test": "cd frontend && npx tsc --noEmit && exit $?",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-exit-status")

    assert "non-portable or unmanaged shell/process-control" in failure


def test_latest_phase_plan_execution_floor_rejects_inline_exit_if_shell_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_740d5c97",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-frontend-build",
                "plan": {
                    "subtasks": [
                        {
                            "id": "phase_web_740d5c97:subtask_4",
                            "title": "React Frontend with TypeScript + JSX",
                            "success_test": (
                                "cd /workspace/frontend && npm run build && "
                                "exit 0 if [ $? -eq 0 ]; then echo "
                                "'Frontend build successful'; else echo "
                                "'Frontend build failed'; exit 1; fi"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_740d5c97"
    )

    assert "non-portable or unmanaged shell/process-control" in failure


def test_latest_phase_plan_execution_floor_rejects_start_job_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-start-job",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "start-job-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "localhost_smoke",
                            "title": "Localhost smoke",
                            "success_test": (
                                'powershell -Command "Start-Job -ScriptBlock '
                                "{ .\\scripts\\dev.ps1 }; Invoke-WebRequest "
                                "http://localhost:8000/health\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-start-job")

    assert "non-portable or unmanaged shell/process-control" in failure


def test_latest_phase_plan_execution_floor_rejects_placeholder_and_llm_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-placeholder-plan",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "placeholder-plan",
                "plan": {
                    "phases": [
                        {
                            "id": "phase_1",
                            "title": "Build game",
                            "subtasks": [{"_depth_limit": True}],
                        }
                    ],
                    "decision_policy": (
                        "If LLM fails, fallback to deterministic heuristic "
                        "decisions."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-placeholder-plan"
    )

    assert "depth-limit placeholder" in failure


def test_latest_phase_plan_execution_floor_rejects_llm_heuristic_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-llm-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_decisions",
                            "title": "Build bot decisions",
                            "goal": "Use GMAS LLM calls for bot turns.",
                            "success_test": "python -m pytest tests/test_bots.py -q",
                            "failure_policy": (
                                "If LLM fails, fallback to deterministic "
                                "heuristic decisions."
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-llm-fallback"
    )

    assert "heuristic fallback" in failure


def test_latest_phase_plan_execution_floor_rejects_no_credentials_llm_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-no-credentials-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "no-credentials-fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_decisions",
                            "title": "Build bot decisions",
                            "goal": "Use GMAS LLM calls for bot turns.",
                            "success_test": "python -m pytest tests/test_bots.py -q",
                            "failure_policy": (
                                "If no LLM credentials configured, use "
                                "deterministic fallback rules for bots."
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-no-credentials-fallback"
    )

    assert "fallback for required LLM behavior" in failure


def test_latest_phase_plan_execution_floor_rejects_generic_llm_fallback_logic(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-generic-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "generic-fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_decisions",
                            "title": "Build GMAS bot decisions",
                            "goal": "Use real LLM decisions for bot turns.",
                            "success_test": "python -m pytest tests/test_bots.py -q",
                        }
                    ],
                    "risks_and_mitigations": [
                        {
                            "risk": "LLM response nondeterminism",
                            "mitigation": (
                                "Use harness_run for agent tests; add "
                                "retry/fallback logic."
                            ),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-generic-fallback"
    )

    assert "generic fallback logic" in failure


def test_latest_phase_plan_execution_floor_rejects_hyphenated_llm_fallback_behavior(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-hyphen-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "hyphen-fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_decisions",
                            "title": "Build GMAS bot decisions",
                            "goal": "Use real LLM decisions for bot turns.",
                            "success_test": "python -m pytest tests/test_bots.py -q",
                        }
                    ],
                    "risks_and_mitigations": [
                        {
                            "risk": "LLM timeout",
                            "mitigation": "Add retry policy and fall-back behavior.",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-hyphen-fallback"
    )

    assert "generic fallback logic" in failure


def test_latest_phase_plan_execution_floor_rejects_llm_random_valid_action_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-random-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "random-fallback-plan",
                "plan": {
                    "subtasks": [
                            {
                                "id": "bot_turns",
                                "title": "Build bot turns",
                                "goal": "Use GMAS LLM calls for bot turns.",
                                "files_to_create": [
                                    "src/test_ws/agents/bots.py",
                                    "tests/test_bots.py",
                                ],
                                "success_test": "python -m pytest tests/test_bots.py -q",
                            }
                    ],
                    "risk_mitigation": {
                        "timeout_risk": (
                            "Set LLM timeout=30s per turn, fallback to random "
                            "valid action if exceeded."
                        )
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-random-fallback"
    )

    assert "fallback for required LLM behavior" in failure


def test_latest_phase_plan_execution_floor_rejects_llm_cached_decision_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-cached-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "cached-fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_failure_handling",
                            "title": "Handle LLM failure",
                            "goal": (
                                "For LLM failures, fallback to cached decisions "
                                "and graceful degradation."
                            ),
                            "success_test": "python -m pytest tests/test_llm.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-cached-fallback"
    )

    assert "fallback for required LLM behavior" in failure


def test_latest_phase_plan_execution_floor_rejects_rule_based_ai_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-rule-based-ai-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "rule-based-ai-fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "st_3_1_gmas_integration",
                            "title": "GMAS framework integration",
                            "goal": "Use real GMAS LLM calls for bot turns.",
                            "success_test": "python -m pytest tests/test_gmas.py -q",
                        }
                    ],
                    "risk_mitigation": [
                        {
                            "risk": "GMAS integration complexity could delay AI implementation",
                            "mitigation": (
                                "Implement simple rule-based AI fallback first, "
                                "incrementally add LLM features, use GMAS examples "
                                "from gmas/examples/ as reference"
                            ),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-rule-based-ai-fallback"
    )

    assert "fallback for required LLM behavior" in failure


def test_latest_phase_plan_execution_floor_rejects_decision_caching(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-decision-caching",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "decision-caching-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "st_3_1_gmas_integration",
                            "title": "GMAS framework integration",
                            "goal": "Use real GMAS LLM calls for bot turns.",
                            "success_test": "python -m pytest tests/test_gmas.py -q",
                        }
                    ],
                    "risk_mitigation": [
                        {
                            "risk": "LLM API costs could exceed budget during development",
                            "mitigation": (
                                "Use model from OUROBOROS_MODEL/LLM_MODEL env var, "
                                "implement decision caching, set strict token limits "
                                "per turn, track costs in game logs"
                            ),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-decision-caching"
    )

    assert "cached decision/action/reasoning reuse" in failure


def test_latest_phase_plan_execution_floor_rejects_key_context_llm_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-key-context-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "key-context-fallback-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_runtime",
                            "title": "Wire real GMAS bot runtime",
                            "goal": "Use real GMAS LLM calls for bot turns.",
                            "success_test": "python -m pytest tests/test_gmas.py -q",
                        }
                    ],
                    "decision_policies": {
                        "llm_failure_handling": (
                            "Fallback to weighted heuristic for critical path; "
                            "log failures for learning"
                        )
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-key-context-fallback"
    )

    assert "fallback for required LLM behavior" in failure


def test_latest_phase_plan_execution_floor_rejects_key_context_reasoning_cache(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-key-context-cache",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "key-context-cache-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_runtime",
                            "title": "Wire real GMAS bot runtime",
                            "goal": "Use real GMAS LLM calls for bot turns.",
                            "success_test": "python -m pytest tests/test_gmas.py -q",
                        }
                    ],
                    "risk_mitigation": {
                        "llm_cost": (
                            "Cache common reasoning; prompt engineering to reduce "
                            "tokens per turn"
                        )
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-key-context-cache"
    )

    assert "cached decision/action/reasoning reuse" in failure


def test_latest_phase_plan_execution_floor_rejects_llm_error_as_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-error-as-success",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "error-as-success-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "gmas_runner",
                            "title": "Run GMAS bot decisions",
                            "goal": "Execute real GMAS/LLM bot turns with inherited env.",
                            "success_test": (
                                "python -c \"result = {'error': 'ERROR_LLM'}; "
                                "assert 'success' in result or 'error' in result or "
                                "'ERROR_LLM' in str(result)\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-error-as-success"
    )

    assert "error path as a passing outcome" in failure


def test_latest_phase_plan_execution_floor_accepts_protective_no_fallback_policy(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-no-fallback",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "no-fallback-plan",
                "plan": {
                    "subtasks": [
                            {
                                "id": "bot_turns",
                                "title": "Build bot turns",
                                "goal": "Use GMAS LLM calls for bot turns.",
                                "files_to_create": [
                                    "src/test_ws/agents/bots.py",
                                    "tests/test_bots.py",
                                ],
                                "success_test": "python -m pytest tests/test_bots.py -q",
                            }
                    ],
                    "llm_policy": (
                        "No fallback to hardcoded rules. LLM API errors surface "
                        "as exceptions and verification tests detect hardcoded "
                        "fallback logic. Runtime resolves "
                        "OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                        "OUROBOROS_MODEL/LLM_MODEL."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-no-fallback")

    assert failure == ""


def test_latest_phase_plan_execution_floor_unwraps_serialized_plan_object(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    embedded = {
        "subtasks": [
                {
                    "id": "domain_model",
                    "title": "Domain model",
                    "goal": "Implement game domain behavior.",
                    "files_to_create": [
                        "src/test_ws/domain.py",
                        "tests/test_domain.py",
                    ],
                    "success_test": "python -m pytest tests/test_domain.py -q",
                }
        ]
    }
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-serialized-plan",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "serialized-plan",
                "plan": {"plan": json.dumps(embedded), "plan_len": 1234},
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-serialized-plan"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_invalid_serialized_plan_string(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-invalid-serialized-plan",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "invalid-serialized-plan",
                "plan": {
                    "plan": (
                        '{"subtasks":[{"id":"domain_model","title":"Domain model",'
                        '"success_test":"python -m pytest tests/test_domain.py -q"}'
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-invalid-serialized-plan"
    )

    assert "serialized text in `plan.plan`" in failure


def test_latest_phase_plan_execution_floor_rejects_captured_coarse_fullstack_leaf(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-coarse-civilization-leaf",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "civilization-phase-1",
                "plan": {
                    "title": "Build simplified Civilization with FastAPI and React",
                    "subtasks": [
                        {
                            "id": "setup",
                            "title": "Initialize project structure",
                            "mode": "setup",
                            "files_to_create": [
                                "pyproject.toml",
                                "frontend/package.json",
                                "docs/architecture.md",
                                "tests/test_project_setup.py",
                                "src/civgame/__init__.py",
                            ],
                            "success_test": "python -m pytest tests/test_project_setup.py -q",
                        },
                        {
                            "id": "phase-1-subtask-2",
                            "title": "Implement core game domain models",
                            "mode": "implementation",
                            "goal": (
                                "Define players, territories, resources, economy, "
                                "units, buildings, and diplomatic relationships."
                            ),
                            "files_to_create": [
                                "src/civgame/models/game_state.py",
                                "src/civgame/models/economy.py",
                                "src/civgame/models/diplomacy.py",
                                "src/civgame/models/__init__.py",
                                "tests/test_models.py",
                            ],
                            "success_test": "python -m pytest tests/test_models.py -v --tb=short",
                        },
                        {
                            "id": "agent_graph",
                            "title": "Build agent graph",
                            "files_to_create": [
                                "src/civgame/agents/graph.py",
                                "tests/test_agent_graph.py",
                            ],
                            "success_test": "python -m pytest tests/test_agent_graph.py -q",
                        },
                        {
                            "id": "api",
                            "title": "Build FastAPI routes",
                            "files_to_create": [
                                "src/civgame/api/main.py",
                                "tests/test_api.py",
                            ],
                            "success_test": "python -m pytest tests/test_api.py -q",
                        },
                        {
                            "id": "frontend",
                            "title": "Build React UI",
                            "files_to_create": [
                                "frontend/src/App.tsx",
                                "frontend/src/App.test.tsx",
                            ],
                            "success_test": "cd frontend && npx tsc --noEmit",
                        },
                        {
                            "id": "e2e",
                            "title": "Verify local launch",
                            "mode": "verification",
                            "files_to_create": ["tests/integration/test_e2e.py"],
                            "success_test": "python -m pytest tests/integration/test_e2e.py -q",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-coarse-civilization-leaf"
    )

    assert "too broad" in failure
    assert "phase-1-subtask-2" in failure


def test_latest_phase_plan_execution_floor_accepts_sixteen_narrow_fullstack_leaves(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    subtasks = [
        {
            "id": "setup",
            "title": "Initialize FastAPI React project",
            "mode": "setup",
            "files_to_create": [
                "pyproject.toml",
                "frontend/package.json",
                "docs/architecture.md",
                "tests/test_project_setup.py",
            ],
            "success_test": "python -m pytest tests/test_project_setup.py -q",
        }
    ]
    for idx in range(1, 15):
        subtasks.append(
            {
                "id": f"slice_{idx:02d}",
                "title": f"Build vertical slice {idx}",
                "goal": f"Implement one bounded behavior slice {idx}.",
                "files_to_create": [
                    f"src/civgame/slice_{idx:02d}.py",
                    f"tests/test_slice_{idx:02d}.py",
                ],
                "success_test": f"python -m pytest tests/test_slice_{idx:02d}.py -q",
            }
        )
    subtasks.append(
        {
            "id": "final_smoke",
            "title": "Verify local launch smoke",
            "mode": "verification",
            "files_to_create": ["tests/integration/test_e2e_smoke.py"],
            "success_test": "python -m pytest tests/integration/test_e2e_smoke.py -q",
        }
    )
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-sixteen-narrow-leaves",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "narrow-fullstack-plan",
                "plan": {
                    "title": "Build FastAPI React civilization app",
                    "subtasks": subtasks,
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-sixteen-narrow-leaves"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_accepts_python_inline_assert_semicolon(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-inline-python-assert",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "inline-python-assert-plan",
                "plan": {
                    "subtasks": [
                            {
                                "id": "config_check",
                                "title": "Config check",
                                "goal": "Validate a simple inline config assertion.",
                                "files_to_create": [
                                    "src/test_ws/config.py",
                                    "tests/test_config.py",
                                ],
                                "success_test": (
                                'python -c "value = 2; assert value == 2" && '
                                "python -m pytest tests/test_config.py -q"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-inline-python-assert"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_accepts_llm_env_alias_fallback_chain(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-env-alias-chain",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "env-alias-chain-plan",
                "plan": {
                    "subtasks": [
                            {
                                "id": "bot_turns",
                                "title": "Build bot turns",
                                "goal": "Use GMAS LLM calls for bot turns.",
                                "files_to_create": [
                                    "src/test_ws/agents/bots.py",
                                    "tests/test_bots.py",
                                ],
                                "success_test": "python -m pytest tests/test_bots.py -q",
                            }
                    ],
                    "llm_config": (
                        "Support OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                        "OUROBOROS_MODEL/LLM_MODEL. Check OUROBOROS_* first, "
                        "fall back to LLM_* aliases."
                    ),
                    "bot_count": "Default to 3 AI civilizations for testing.",
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-env-alias-chain"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_captured_ouroboros_only_env(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-ouroboros-only-env",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "ouroboros-only-env-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_runtime",
                            "title": "Wire LLM bot runtime",
                            "goal": (
                                "GMAS bots resolve OUROBOROS_LLM_API_KEY, "
                                "OUROBOROS_LLM_BASE_URL, and OUROBOROS_MODEL "
                                "from inherited runtime for real LLM decisions."
                            ),
                            "files_to_create": [
                                "src/demo/llm_runtime.py",
                                "tests/test_llm_runtime.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_llm_runtime.py -q"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-ouroboros-only-env"
    )

    assert "standalone LLM runtime env contract" in failure
    assert "LLM_API_KEY" in failure
    assert "LLM_BASE_URL" in failure
    assert "LLM_MODEL" in failure


def test_latest_phase_plan_execution_floor_rejects_unsupported_ll_base_url_alias(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-ll-base-url-typo",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "ll-base-url-typo-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_runtime",
                            "title": "Wire LLM bot runtime",
                            "goal": (
                                "GMAS bots resolve OUROBOROS_LLM_API_KEY/"
                                "LLM_API_KEY, OUROBOROS_LLM_BASE_URL/"
                                "LLM_BASE_URL, and OUROBOROS_MODEL/LLM_MODEL."
                            ),
                            "files_to_create": [
                                "src/demo/llm_runtime.py",
                                "tests/test_llm_runtime.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_llm_runtime.py -q"
                            ),
                        }
                    ],
                    "llm_runtime_contract": (
                        "Backend runtime resolves OUROBOROS_LLM_API_KEY or "
                        "LLM_API_KEY, OUROBOROS_LLM_BASE_URL or LL_BASE_URL, "
                        "and OUROBOROS_MODEL or LLM_MODEL."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-ll-base-url-typo"
    )

    assert "LL_BASE_URL" in failure
    assert "LLM_BASE_URL" in failure


def test_latest_phase_plan_execution_floor_accepts_llm_env_alias_parenthetical_fallback(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-env-alias-parenthetical",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "env-alias-parenthetical-plan",
                "plan": {
                    "subtasks": [
                            {
                                "id": "bot_turns",
                                "title": "Build bot turns",
                                "goal": "Use GMAS LLM calls for bot turns.",
                                "files_to_create": [
                                    "src/test_ws/agents/bots.py",
                                    "tests/test_bots.py",
                                ],
                                "success_test": "python -m pytest tests/test_bots.py -q",
                            }
                    ],
                    "llm_config": (
                        "Priority: OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                        "OUROBOROS_MODEL/LLM_MODEL. OUROBOROS aliases are "
                        "checked first, then LLM aliases (fallback). "
                        "LLM calls raise AgentExecutionError on "
                        "timeout; no replacement decisions are produced."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-env-alias-parenthetical"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_greenfield_python_outside_src(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-root-layout",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "root-layout-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "engine",
                            "title": "Build game engine",
                            "goal": "Create Python backend package.",
                            "files_to_create": ["pyproject.toml", "game_engine/state.py"],
                            "success_test": "python -m pytest tests/test_state.py -q",
                        },
                        {
                            "id": "agents",
                            "title": "Build LLM GMAS agents",
                            "goal": "Build GMAS agents for bot turns.",
                            "files_to_create": ["agents/civ_agents.py"],
                            "success_test": "python -m pytest tests/test_agents.py -q",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-root-layout")

    assert "outside `src/<package>/...`" in failure
    assert "game_engine/state.py" in failure


def test_latest_phase_plan_execution_floor_rejects_greenfield_pytest_inside_src(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-src-test-layout",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "src-test-layout-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "engine",
                            "title": "Build game engine",
                            "goal": "Create Python backend package.",
                            "files_to_create": [
                                "pyproject.toml",
                                "src/civgame/game_engine/state.py",
                                "docs/architecture.md",
                            ],
                            "success_test": "python -m pytest tests/test_state.py -q",
                        },
                        {
                            "id": "deployment",
                            "title": "Verify deployment",
                            "goal": "Create automated deployment proof.",
                            "files_to_create": [
                                "src/civgame/verify/local_deployment_test.py"
                            ],
                            "success_test": (
                                "python -m pytest "
                                "src/civgame/verify/local_deployment_test.py -q"
                            ),
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-src-test-layout"
    )

    assert "pytest/test modules outside `tests/`" in failure
    assert "src/civgame/verify/local_deployment_test.py" in failure


def test_latest_phase_plan_execution_floor_rejects_greenfield_pytest_inside_docs(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-docs-test-layout",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "docs-test-layout-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "docs",
                            "title": "Write architecture docs",
                            "goal": "Create docs and tests for architecture.",
                            "files_to_create": [
                                "pyproject.toml",
                                "docs/architecture.md",
                                "docs/test_game_model.py",
                            ],
                            "success_test": (
                                "python -m pytest docs/test_game_model.py -q"
                            ),
                        },
                        {
                            "id": "engine",
                            "title": "Build game engine",
                            "goal": "Create Python backend package.",
                            "files_to_create": ["src/civgame/game_engine/state.py"],
                            "success_test": "python -m pytest tests/test_state.py -q",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-docs-test-layout"
    )

    assert "pytest/test modules outside `tests/`" in failure
    assert "docs/test_game_model.py" in failure


def test_latest_phase_plan_execution_floor_normalises_workspace_prefixed_paths(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    workspace_id = tmp_workspace["workspace_id"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-prefixed-layout",
                "workspace_id": workspace_id,
                "plan_id": "prefixed-layout-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "engine",
                            "title": "Build game engine",
                            "goal": "Create Python backend package.",
                            "files_to_create": [
                                f"{workspace_id}/pyproject.toml",
                                f"{workspace_id}/src/{workspace_id}/state.py",
                                f"{workspace_id}/docs/architecture.md",
                                f"{workspace_id}/tests/test_state.py",
                            ],
                            "success_test": "python -m pytest tests/test_state.py -q",
                        },
                        {
                            "id": "ui",
                            "title": "Build UI",
                            "goal": "Create frontend shell.",
                            "files_to_create": [
                                f"{workspace_id}/frontend/package.json",
                                f"{workspace_id}/frontend/src/App.tsx",
                            ],
                            "success_test": "npm --prefix frontend run build",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=workspace_id,
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-prefixed-layout"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_complex_plan_without_docs(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-no-docs-layout",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "no-docs-layout-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "engine",
                            "title": "Build game engine",
                            "goal": "Create Python game engine.",
                            "files_to_create": [
                                "pyproject.toml",
                                "src/civ/game_engine/state.py",
                            ],
                            "success_test": "python -m pytest tests/test_state.py -q",
                        },
                        {
                            "id": "agents",
                            "title": "Build LLM GMAS agents",
                            "goal": "Build GMAS agents for bot turns.",
                            "files_to_create": ["src/civ/agents/civ_agents.py"],
                            "success_test": "python -m pytest tests/test_agents.py -q",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-no-docs-layout"
    )

    assert "lacks a durable `docs/`" in failure


def test_latest_phase_plan_execution_floor_uses_nested_leaves_with_phase_test_strategy(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-nested-phase-wrapper",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "nested-phase-wrapper-plan",
                "plan": {
                    "phases": [
                        {
                            "id": "phase_1_foundations",
                            "title": "Foundations",
                            "summary": "Build the Python LLM game foundation.",
                            "test_strategy": "Health, model, and agent tests pass.",
                            "subtasks": [
                                {
                                    "id": "domain",
                                    "title": "Build domain state",
                                    "goal": "Create src package and deterministic state.",
                                    "files_to_create": [
                                        "pyproject.toml",
                                        "src/civ/game_engine/state.py",
                                        "docs/architecture.md",
                                        "tests/test_state.py",
                                    ],
                                    "success_test": "python -m pytest tests/test_state.py -q",
                                },
                                {
                                    "id": "agents",
                                    "title": "Build GMAS LLM agents",
                                    "goal": "Create real runtime-env GMAS agent path.",
                                    "files_to_create": [
                                        "src/civ/agents/civ_agents.py",
                                        "tests/test_agents.py",
                                    ],
                                    "success_test": "python -m pytest tests/test_agents.py -q",
                                },
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-nested-phase-wrapper"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_accepts_success_check_alias(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-success-check-alias",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "success-check-alias-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "engine",
                            "title": "Build engine",
                            "goal": "Create a checked domain model.",
                            "files_to_create": [
                                "pyproject.toml",
                                "src/civ/game_engine/state.py",
                                "docs/architecture.md",
                                "tests/test_state.py",
                            ],
                            "success_checks": "python -m pytest tests/test_state.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-success-check-alias"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_unsupported_ouroboros_model_alias(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-wrong-model-alias",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "wrong-model-alias-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_turns",
                            "title": "Build bot turns",
                            "goal": (
                                "Use GMAS with OUROBOROS_LLM_API_KEY, "
                                "OUROBOROS_LLM_BASE_URL, and OUROBOROS_LLM_MODEL."
                            ),
                            "success_test": "python -m pytest tests/test_bots.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-wrong-model-alias"
    )

    assert "OUROBOROS_LLM_MODEL" in failure
    assert "OUROBOROS_MODEL" in failure


def test_latest_phase_plan_execution_floor_rejects_protective_model_alias_note(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-protective-model-alias",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "protective-model-alias-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_turns",
                            "title": "Build bot turns",
                            "goal": (
                                "Use GMAS with OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                                "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                                "OUROBOROS_MODEL/LLM_MODEL. Do not use "
                                "OUROBOROS_LLM_MODEL for model selection."
                            ),
                            "files_to_create": [
                                "src/civ/agents/bot_turns.py",
                                "tests/test_bots.py",
                                "docs/architecture.md",
                            ],
                            "success_test": "python -m pytest tests/test_bots.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-protective-model-alias"
    )

    assert "OUROBOROS_LLM_MODEL" in failure


def test_latest_phase_plan_execution_floor_rejects_provider_specific_llm_default(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-gpt-default",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "gpt-default-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_budget",
                            "title": "Add LLM budget guard",
                            "goal": (
                                "Use GMAS with OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                                "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                                "OUROBOROS_MODEL/LLM_MODEL, estimating cost "
                                "with gpt-4o-mini as the default."
                            ),
                            "success_test": "python -m pytest tests/test_budget.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-gpt-default")

    assert "provider/model-specific" in failure
    assert "gpt-*" in failure


def test_latest_phase_plan_execution_floor_rejects_missing_llm_env_alias_contract(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-missing-llm-env",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "missing-llm-env-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "gmas_agents",
                            "title": "Build GMAS bot agents",
                            "goal": (
                                "Create LLM-powered bot decision agents and "
                                "provider configuration for real bot turns."
                            ),
                            "success_test": "python -m pytest tests/test_agents.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-missing-llm-env"
    )

    assert "omits the standalone LLM runtime env contract" in failure


def test_latest_phase_plan_execution_floor_rejects_llm_agents_without_env_section(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-llm-agents-no-env",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "llm-agents-no-env-plan",
                "plan": {
                    "title": "LLM-powered civilization agents",
                    "subtasks": [
                        {
                            "id": "economic_agent",
                            "title": "Implement Economic AI Agent",
                            "goal": (
                                "Build a GMAS economic agent using LLM "
                                "reasoning to choose production and trade "
                                "decisions."
                            ),
                            "files_to_create": [
                                "docs/architecture.md",
                                "src/demo/agents/economic.py",
                                "tests/test_economic_agent.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_economic_agent.py -q"
                            ),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-llm-agents-no-env"
    )

    assert "omits the standalone LLM runtime env contract" in failure


def test_latest_phase_plan_execution_floor_accepts_public_llm_alias_contract(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-public-llm-env",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "public-llm-env-plan",
                "plan": {
                    "llm_runtime_contract": {
                        "api_key": "LLM_API_KEY",
                        "base_url": "LLM_BASE_URL",
                        "model": "LLM_MODEL",
                    },
                    "subtasks": [
                        {
                            "id": "economic_agent",
                            "title": "Implement Economic AI Agent",
                            "goal": (
                                "Build a GMAS economic agent using LLM_MODEL "
                                "and generic LLM env aliases for decisions."
                            ),
                            "files_to_create": [
                                "docs/architecture.md",
                                "src/demo/agents/economic.py",
                                "tests/test_economic_agent.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_economic_agent.py -q"
                            ),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-public-llm-env"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_empty_test_skeletons(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-empty-tests",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "empty-tests-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "test_infra",
                            "title": "Create test infrastructure",
                            "goal": (
                                "Create empty test files with basic imports for "
                                "all referenced pytest modules."
                            ),
                            "success_test": "python -m pytest tests/test_ai.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-empty-tests")

    assert "empty/basic-import test skeletons" in failure


def test_latest_phase_plan_execution_floor_allows_protective_empty_test_language(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-protective-tests",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "protective-tests-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "test_infra",
                            "title": "Create behavioral test infrastructure",
                            "goal": (
                                "Do not create empty or import-only test shells; "
                                "tests must contain executable assertions that "
                                "fail for real behavior regressions."
                            ),
                            "files_to_create": ["tests/test_ai.py"],
                            "success_test": "python -m pytest tests/test_ai.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-protective-tests"
    )

    assert "empty/basic-import test skeletons" not in failure


def test_latest_phase_plan_execution_floor_allows_captured_no_import_only_policy(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-no-import-only-policy",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-no-import-only-policy",
                "plan": {
                    "subtasks": [
                        {
                            "id": "gmas_llm_tests",
                            "title": "Add GMAS LLM integration tests",
                            "goal": (
                                "Create tests that prove GMAS bots call the "
                                "real runtime path."
                            ),
                            "files_to_create": ["tests/test_gmas_agents.py"],
                            "success_test": (
                                "python -m pytest tests/test_gmas_agents.py -q"
                            ),
                        }
                    ],
                    "decision_policies": {
                        "llm_runtime_configuration": (
                            "Resolve API key from OUROBOROS_LLM_API_KEY then "
                            "LLM_API_KEY, base URL from OUROBOROS_LLM_BASE_URL "
                            "then LLM_BASE_URL, and model from OUROBOROS_MODEL "
                            "then LLM_MODEL."
                        ),
                        "testing_authenticity": (
                            "No import-only tests. LLM-backed tests verify "
                            "actual GMAS tool calling via MACPRunner and "
                            "structured JSONs. E2E tests run real turns with "
                            "real LLM when credentials are present; if absent, "
                            "tests skip with pytest.skip and message, not "
                            "silent pass."
                        )
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-no-import-only-policy"
    )

    assert "empty/basic-import test skeletons" not in failure


def test_latest_phase_plan_execution_floor_rejects_mock_llm_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-mock-llm-proof",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "mock-llm-proof-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "gmas_bot_system",
                            "title": "GMAS Multi-Agent Bot System",
                            "goal": "Build real LLM-backed bot turns.",
                            "success_test": (
                                "pytest tests/test_bot_graph.py "
                                "tests/test_gmas_integration.py -v --mock-llm"
                            ),
                        }
                    ],
                    "llm_config": (
                        "Runtime resolves OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                        "OUROBOROS_MODEL/LLM_MODEL."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-mock-llm-proof"
    )

    assert "mock" in failure.lower()
    assert "real runtime" in failure


def test_latest_phase_plan_execution_floor_rejects_mock_e2e_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-mock-e2e-proof",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "mock-e2e-proof-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "e2e_integration",
                            "title": "End-to-End Integration Test",
                            "goal": "Run the full game through the real runtime.",
                            "success_test": (
                                "python -m pytest tests/integration/test_e2e.py "
                                "--mock -v"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-mock-e2e-proof"
    )

    assert "mocked path" in failure


def test_latest_phase_plan_execution_floor_rejects_collect_only_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-collect-only-proof",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "collect-only-proof-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "project_setup",
                            "title": "Backend Project Setup",
                            "goal": "Create the backend package structure.",
                            "success_test": "python -m pytest --collect-only",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-collect-only-proof"
    )

    assert "collects pytest tests" in failure


def test_latest_phase_plan_execution_floor_rejects_import_only_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-import-only",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "import-only-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_tools",
                            "title": "Create bot tools",
                            "goal": "Create callable bot decision tools.",
                            "success_test": (
                                "python -c \"from backend.bots.bot_tools import "
                                "build_city; print('Bot tools imported')\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-import-only")

    assert "only imports modules" in failure


def test_latest_phase_plan_execution_floor_rejects_complex_python_inline(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-complex-inline",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "complex-inline-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "localhost_check",
                            "title": "Verify localhost",
                            "goal": "Start server and verify HTTP readiness.",
                            "success_test": (
                                "python -c \"import subprocess; import time; "
                                "import requests; proc = subprocess.Popen("
                                "['python', '-m', 'backend.api']); time.sleep(3); "
                                "resp = requests.get('http://localhost:8000/health'); "
                                "proc.kill(); assert resp.status_code == 200\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(run_id="r-complex-inline")

    assert "too complex" in failure


def test_latest_phase_plan_execution_floor_rejects_python_inline_workspace_import(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_104ff3a2",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-inline-api-schema",
                "plan": {
                    "subtasks": [
                        {
                            "id": "arch_api",
                            "title": "Design REST API architecture",
                            "success_test": (
                                "python -c \"from src.civgame.api_schemas "
                                "import GameState, Civilization, PlayerAction; "
                                "gs = GameState(civilizations=[], "
                                "map={'width': 10, 'height': 10}, turn=1); "
                                "assert gs.turn == 1\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="phase_web_104ff3a2"
    )

    assert "imports workspace/application modules" in failure


def test_latest_phase_plan_execution_floor_rejects_captured_multiline_python_inline(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-captured-multiline-inline",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "captured-multiline-inline-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "game-loop-orchestration",
                            "title": "Implement game loop and turn orchestration",
                            "goal": "Connect game loop to real GameState behavior.",
                            "files_to_create": ["src/civgame/game/loop.py"],
                            "success_test": (
                                "python -c \"\n"
                                "from civgame.game.loop import GameLoop\n"
                                "gs = type('GameState', (), {'turn': 0})()  # Mock state\n"
                                "loop = GameLoop(game_state=gs)\n"
                                "assert loop is not None\n"
                                "print('Game loop OK')\n"
                                "\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-captured-multiline-inline"
    )

    assert "too complex" in failure


def test_latest_phase_plan_execution_floor_rejects_descriptive_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-descriptive-success",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "descriptive-success-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "domain_model",
                            "title": "Domain model",
                            "goal": "Implement and test game objects.",
                            "success_test": (
                                "python -m pytest tests/test_game_objects.py -q "
                                "- must instantiate all classes"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-descriptive-success"
    )

    assert "descriptive acceptance text" in failure


def test_latest_phase_plan_execution_floor_rejects_generic_tool_pseudo_args(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-tool-pseudo-args",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "tool-pseudo-args-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "domain_model",
                            "title": "Domain model",
                            "goal": "Implement and test game objects.",
                            "success_test": "run_unit_tests tests/test_game_objects.py",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-tool-pseudo-args"
    )

    assert "pseudo-arguments" in failure


def test_latest_phase_plan_execution_floor_rejects_generic_tool_colon_pseudo_args(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-tool-colon-pseudo-args",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "tool-colon-pseudo-args-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "ai_controller",
                            "title": "AI controller",
                            "goal": "Implement and test GMAS AI turns.",
                            "success_test": (
                                "harness_run:subtask_ai_controller:3:tests_pass"
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-tool-colon-pseudo-args"
    )

    assert "pseudo-arguments" in failure


def test_latest_phase_plan_execution_floor_rejects_file_existence_only_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-file-existence-only",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "file-existence-only-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "game_map",
                            "title": "Game map",
                            "goal": "Create the map component.",
                            "success_test": (
                                "node -e \"const fs=require('fs'); "
                                "assert(fs.existsSync('frontend/src/GameMap.tsx'))\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-file-existence-only"
    )

    assert "only checks file/path existence" in failure


def test_latest_phase_plan_execution_floor_rejects_pathlib_join_exists_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-pathlib-join-existence",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "pathlib-join-existence-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "architecture_docs",
                            "title": "Create architecture docs",
                            "goal": "Write durable architecture docs.",
                            "success_test": (
                                "python -c \"from pathlib import Path; "
                                "docs = Path('docs'); required = ['architecture.md']; "
                                "missing = [f for f in required if not "
                                "(docs/f).exists()]; assert not missing\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-pathlib-join-existence"
    )

    assert "only checks file/path existence" in failure


def test_latest_phase_plan_execution_floor_rejects_inline_docs_content_python_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-inline-docs-content",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "inline-docs-content-plan",
                "plan": {
                    "llm_runtime_contract": (
                        "Generated code resolves OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                        "OUROBOROS_MODEL/LLM_MODEL, and surfaces missing runtime "
                        "credentials as explicit errors."
                    ),
                    "subtasks": [
                        {
                            "id": "docs-contract",
                            "title": "Write architecture docs and runtime contract",
                            "goal": "Document the runtime contract.",
                            "files_to_create": [
                                "README.md",
                                "docs/architecture.md",
                                "docs/bot_personas.md",
                                "docs/setup.md",
                            ],
                            "success_test": (
                                "python -c \"import os; assert "
                                "'OUROBOROS_LLM_API_KEY' in open('README.md').read() "
                                "and 'GMAS' in open('docs/architecture.md').read() "
                                "and 'bot personas' in open('docs/bot_personas.md').read().lower() "
                                "and 'WS' in open('docs/setup.md').read()\""
                            ),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-inline-docs-content"
    )

    assert "documentation/content inline" in failure


def test_runner_projects_verification_commands_object_as_success_test(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-verification-commands-object",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "accepted-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_tools",
                            "title": "Bot tools",
                            "goal": "Implement GMAS bot tool behavior.",
                            "verification": {
                                "commands": [
                                    "python -m pytest tests/test_bot_tools.py -q"
                                ]
                            },
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    launcher = _ExecuteSubtaskQueueFakeLauncher(drive)
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=launcher,
    )

    results = list(
        runner.run("test task", phases=["execute"], run_id="r-verification-commands-object")
    )

    assert all(r.ok for r in results)
    loaded = load_plan(drive)
    execute = loaded.get_node("execute")
    assert execute.subtasks[0].success_test.value == (
        "python -m pytest tests/test_bot_tools.py -q"
    )


def test_latest_phase_plan_execution_floor_rejects_verification_list_alias(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-verification-list-alias",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "verification-list-alias-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_tools",
                            "title": "Bot tools",
                            "goal": "Implement GMAS bot tool behavior.",
                            "verification": [
                                "Run: python -m pytest tests/test_bot_tools.py -q",
                                "Run: python -m pytest tests/test_agent_graph.py -q",
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-verification-list-alias"
    )

    assert "`verification` is a list" in failure
    assert "top-level `success_test`" in failure


def test_latest_phase_plan_execution_floor_accepts_public_llm_env_notes(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-narrow-env-notes",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "narrow-env-notes-plan",
                "notes": (
                    "This project requires real LLM configuration via LLM_API_KEY, "
                    "LLM_BASE_URL, and LLM_MODEL environment variables."
                ),
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_runtime",
                            "title": "LLM runtime",
                            "goal": "GMAS bots call the inherited LLM runtime.",
                            "files_to_create": [
                                "src/demo/llm_runtime.py",
                                "tests/test_llm_runtime.py",
                            ],
                            "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-narrow-env-notes"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_rejects_mock_fake_llm_strategy(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-mock-fake-llm-strategy",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "mock-fake-llm-strategy-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_turns",
                            "title": "LLM bot turns",
                            "goal": "GMAS bots make economy and diplomacy decisions.",
                            "success_test": "python -m pytest tests/test_real_llm_game.py -q",
                        }
                    ],
                    "test_strategy": {
                        "integration": (
                            "WebSocket connection tests with mock/fake LLM for reliability."
                        )
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-mock-fake-llm-strategy"
    )

    assert "mock/fake/dry-run LLM behavior" in failure


def test_latest_phase_plan_execution_floor_allows_protective_no_mock_llm_language(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-protective-no-mock-llm",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "protective-no-mock-llm-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "bot_runtime",
                            "title": "Wire GMAS bot runtime",
                            "goal": (
                                "GMAS bot decisions use inherited real LLM "
                                "runtime without mock behavior."
                            ),
                            "files_to_create": [
                                "docs/architecture.md",
                                "src/demo/llm_runtime.py",
                                "tests/test_llm_runtime.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_llm_runtime.py -q"
                            ),
                        }
                    ],
                    "test_strategy": {
                        "integration": (
                            "Reject mock/fake LLM paths and require "
                            "OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                            "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                            "OUROBOROS_MODEL/LLM_MODEL. No mock LLM behavior "
                            "is accepted."
                        )
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-protective-no-mock-llm"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_allows_mock_terms_in_anti_patterns(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-anti-patterns-no-mock",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "anti-patterns-no-mock-plan",
                "plan": {
                    "task_summary": (
                        "Build an LLM/GMAS bot path with real runtime env. "
                        "All LLM calls are real - no mocks, no dry-run."
                    ),
                    "subtasks": [
                        {
                            "id": "bot_runtime",
                            "title": "Wire real GMAS bot runtime",
                            "goal": (
                                "Resolve OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
                                "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and "
                                "OUROBOROS_MODEL/LLM_MODEL, then call the "
                                "real GMAS/LLM decision path."
                            ),
                            "files_to_create": [
                                "docs/architecture.md",
                                "src/demo/llm_runtime.py",
                                "tests/test_real_llm_runtime.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_real_llm_runtime.py -q"
                            ),
                        }
                    ],
                    "anti_patterns_to_avoid": [
                        "Mock LLM responses in any capacity",
                        "Dry-run mode without real agent execution",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-anti-patterns-no-mock"
    )

    assert failure == ""


def test_latest_phase_plan_execution_floor_projects_phase_mapping_containers(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-mapped-phases",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan_id": "mapped-phases",
                "plan": {
                    "phases": {
                        "phase_1_setup": {
                            "title": "Setup",
                            "subtasks": {
                                "setup_backend": {
                                    "id": "setup_backend",
                                    "title": "Setup backend",
                                    "goal": "Create backend package and tests.",
                                    "success_test": (
                                        "python -m pytest backend/tests/test_setup.py -q"
                                    ),
                                }
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    failure = runner._latest_phase_plan_execution_floor_failure(
        run_id="r-mapped-phases"
    )

    assert failure == ""


def test_runner_retries_plan_missing_required_submit(tmp_workspace):
    launcher = _PlanRetryFakeLauncher(tmp_workspace["drive_root"])
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=launcher,
    )

    results = list(runner.run("test task", phases=["plan"], run_id="r-plan-retry"))

    assert all(r.ok for r in results)
    assert launcher.submitted.count("r-plan-retry:plan") == 2
    assert any(
        (r.data or {}).get("retry_reason", "").startswith(
            "phase exit criteria missing required call"
        )
        for r in results
        if r.ok
    )


def test_runner_defaults_revise_review_to_previous_phase(tmp_workspace):
    launcher = _ReviewFakeLauncher(tmp_workspace["drive_root"], verdict="revise")
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=launcher,
    )

    results = list(
        runner.run("test task", phases=["plan", "plan_review"], run_id="r-revise")
    )

    assert all(r.ok for r in results)
    assert launcher.submitted.count("r-revise:plan") == 2
    assert launcher.submitted.count("r-revise:plan_review") == 2
    second_plan_task = [
        task for task in launcher.tasks if task["id"] == "r-revise:plan"
    ][1]
    assert "Active retry/revision contract" in second_plan_task["input"]
    assert "subtask_06 must add chat-based input" in second_plan_task["input"]
    phase_overlay = second_plan_task["context_overlays"]["phase_node"]["overlay"]
    assert phase_overlay["revision_contract"]["revisions"] == [
        "subtask_06 must add chat-based input"
    ]
    assert any(
        (r.data or {}).get("phase") == "plan_review"
        and (r.data or {}).get("outcome") == "loop_back"
        and str((r.data or {}).get("retry_reason") or "").startswith(
            "micro review requested revisions"
        )
        for r in results
        if r.ok
    )


def test_runner_accumulates_revision_contracts_across_review_loops(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    plan = build_default_plan(
        tmp_workspace["workspace_id"],
        run_id="r-accumulate",
        phases=["plan", "plan_review"],
    )
    plan_node = plan.get_node("plan")
    review_node = plan.get_node("plan_review")
    plan_node.overlay = {
        "revision_contract": {
            "source_phase": "plan_review",
            "source_task_id": "r-accumulate:plan_review",
            "verdict": "revise",
            "revisions": ["keep retention window at 20 turns"],
            "notes": "first review",
        }
    }
    review_node.status = "running"
    review_node.started_at = time.time() - 1
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "sig-revise-new",
                    "created_at": time.time(),
                    "kind": "submit_micro_review",
                    "payload": {
                        "verdict": "revise",
                        "revisions": ["add timeout=30s per agent"],
                        "notes": "second review",
                    },
                    "task_id": "r-accumulate:plan_review",
                    "phase": "plan_review",
                }
            )
            + "\n"
        )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )

    runner._finish_phase_loop_back(
        phase_node=review_node,
        plan=plan,
        run_id="r-accumulate",
        outcome={"task_id": "r-accumulate:plan_review"},
        loop_back_target="plan",
        retry_reason="micro review requested revisions",
    )

    revisions = plan.get_node("plan").overlay["revision_contract"]["revisions"]
    assert revisions == [
        "keep retention window at 20 turns",
        "add timeout=30s per agent",
    ]


def test_runner_respects_repeated_plan_review_revisions_when_plan_is_executable(
    tmp_workspace,
):
    drive_root = tmp_workspace["drive_root"]
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-cap",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "build",
                            "success_criteria": ["pytest passes"],
                            "verification_commands": ["pytest"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        for idx in range(2):
            f.write(
                json.dumps(
                    {
                        "signal_id": f"sig-revise-{idx}",
                        "created_at": time.time() + idx,
                        "kind": "submit_micro_review",
                        "payload": {"verdict": "revise", "notes": "more detail"},
                        "task_id": "r-cap:plan_review",
                        "phase": "plan_review",
                    }
                )
                + "\n"
            )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive_root,
        launcher=_FakeLauncher(drive_root),
    )
    node = PhaseNode(
        id="plan_review",
        manifest_id="plan_review",
        status="running",
        started_at=time.time(),
    )

    target = runner._phase_loop_back_target(
        phase_node=node,
        outcome={"task_id": "r-cap:plan_review"},
    )

    assert target == "plan"


def test_runner_never_caps_repeated_research_review_revisions_when_summary_is_sufficient(
    tmp_workspace,
):
    drive_root = tmp_workspace["drive_root"]
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-research-cap",
                "workspace_id": tmp_workspace["workspace_id"],
                "architecture_id": "arch-current",
                "task_id": "r-research-cap:research",
                "findings_ids": ["finding-1", "finding-2"],
                "notes": "Architecture, tools, risks, and plan handoff are covered.",
            }
        ),
        encoding="utf-8",
    )
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as f:
        for idx in range(2):
            f.write(
                json.dumps(
                    {
                        "task_id": "r-research-cap:research",
                        "tool": "palace_add",
                        "result_preview": json.dumps(
                            {
                                "saved": True,
                                "legacy": {"id": f"finding-{idx + 1}"},
                            }
                        ),
                    }
                )
                + "\n"
            )
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        for idx in range(2):
            f.write(
                json.dumps(
                    {
                        "signal_id": f"sig-research-revise-{idx}",
                        "created_at": time.time() + idx,
                        "kind": "submit_micro_review",
                        "payload": {"verdict": "revise", "notes": "more detail"},
                        "task_id": "r-research-cap:research_review",
                        "phase": "research_review",
                    }
                )
                + "\n"
            )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive_root,
        launcher=_FakeLauncher(drive_root),
    )
    node = PhaseNode(
        id="research_review",
        manifest_id="research_review",
        status="running",
        started_at=time.time(),
    )

    target = runner._phase_loop_back_target(
        phase_node=node,
        outcome={"task_id": "r-research-cap:research_review"},
    )

    assert target == "research"


def test_runner_does_not_cap_research_review_revisions_with_fake_findings(
    tmp_workspace,
):
    drive_root = tmp_workspace["drive_root"]
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-research-fake",
                "workspace_id": tmp_workspace["workspace_id"],
                "architecture_id": "arch-current",
                "task_id": "r-research-fake:research",
                "findings_ids": ["finding_001", "finding_002"],
                "notes": "Architecture, tools, risks, and plan handoff are covered.",
            }
        ),
        encoding="utf-8",
    )
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        for idx in range(2):
            f.write(
                json.dumps(
                    {
                        "signal_id": f"sig-research-fake-revise-{idx}",
                        "created_at": time.time() + idx,
                        "kind": "submit_micro_review",
                        "payload": {"verdict": "revise", "notes": "more detail"},
                        "task_id": "r-research-fake:research_review",
                        "phase": "research_review",
                    }
                )
                + "\n"
            )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive_root,
        launcher=_FakeLauncher(drive_root),
    )
    node = PhaseNode(
        id="research_review",
        manifest_id="research_review",
        status="running",
        started_at=time.time(),
    )

    target = runner._phase_loop_back_target(
        phase_node=node,
        outcome={"task_id": "r-research-fake:research_review"},
    )

    assert target == "research"


def test_runner_deduplicates_current_phase_control_signal_against_ledger(
    tmp_workspace,
):
    drive_root = tmp_workspace["drive_root"]
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "r-dupe",
                "workspace_id": tmp_workspace["workspace_id"],
                "plan": {
                    "subtasks": [
                        {
                            "id": "build",
                            "verification": "pytest -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    row = {
        "signal_id": "sig-same-current-and-ledger",
        "created_at": time.time(),
        "kind": "submit_micro_review",
        "payload": {"verdict": "revise", "notes": "fix the plan"},
        "task_id": "r-dupe:plan_review",
        "phase": "plan_review",
    }
    (state / "phase_control_signal.json").write_text(
        json.dumps(row),
        encoding="utf-8",
    )
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive_root,
        launcher=_FakeLauncher(drive_root),
    )
    node = PhaseNode(
        id="plan_review",
        manifest_id="plan_review",
        status="running",
        started_at=0,
    )

    assert runner._phase_loop_back_target(
        phase_node=node,
        outcome={"task_id": "r-dupe:plan_review"},
    ) == "plan"


def test_runner_fails_revise_review_when_no_loopback_target_exists(tmp_workspace):
    launcher = _ReviewFakeLauncher(tmp_workspace["drive_root"], verdict="revise")
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=launcher,
    )

    results = list(runner.run("test task", phases=["plan_review"], run_id="r-revise-missing-target"))

    assert any(not r.ok for r in results)
    assert "no accepted loop_back_to" in results[-1].errors[0].message
    loaded = load_plan(tmp_workspace["drive_root"])
    assert loaded.get_node("plan_review").status == "failed"


def test_final_review_loop_back_signal_targets_execute(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "sig-final-loop",
                    "created_at": time.time(),
                    "kind": "submit_final_review",
                    "payload": {"outcome": "loop_back", "notes": "e2e failed"},
                    "task_id": "r-final:final_review",
                    "phase": "final_review",
                }
            )
            + "\n"
        )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    node = PhaseNode(
        id="final_review",
        manifest_id="final_review",
        status="running",
        started_at=0,
    )

    target = runner._phase_loop_back_target(
        phase_node=node,
        outcome={"task_id": "r-final:final_review"},
    )

    assert target == "execute"


def test_verify_completion_accepts_promote_to_durable_write(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    task_id = "r-verify:verify"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "sig-submit",
                    "created_at": time.time(),
                    "kind": "submit_verification",
                    "payload": {"status": "pass", "details": "green"},
                    "task_id": task_id,
                    "phase": "verify",
                }
            )
            + "\n"
        )
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "task_id": task_id,
                    "tool": "promote_to_durable",
                    "args": {"tags": "verification_report,green"},
                    "result_preview": json.dumps(
                        {
                            "saved": True,
                            "durable_store": "palace.durable",
                            "durable_node_id": "durable-1",
                        }
                    ),
                }
            )
            + "\n"
        )
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "verify.yaml"
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    node = PhaseNode(id="verify", manifest_id="verify", status="running", started_at=0)
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-verify",
        nodes=[node],
    )

    failure = runner._phase_completion_failure(
        phase_node=node,
        plan=plan,
        manifest=manifest,
        outcome={"task_id": task_id},
    )

    assert failure == ""


def test_verify_completion_rejects_promote_to_durable_without_durable_store(
    tmp_workspace,
):
    drive = tmp_workspace["drive_root"]
    task_id = "r-verify:verify"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "sig-submit",
                    "created_at": time.time(),
                    "kind": "submit_verification",
                    "payload": {"status": "pass", "details": "green"},
                    "task_id": task_id,
                    "phase": "verify",
                }
            )
            + "\n"
        )
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "task_id": task_id,
                    "tool": "promote_to_durable",
                    "args": {"tags": "verification_report,green"},
                    "result_preview": '{"saved": true, "id": "legacy-only"}',
                }
            )
            + "\n"
        )
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "verify.yaml"
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    node = PhaseNode(id="verify", manifest_id="verify", status="running", started_at=0)
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-verify",
        nodes=[node],
    )

    failure = runner._phase_completion_failure(
        phase_node=node,
        plan=plan,
        manifest=manifest,
        outcome={"task_id": task_id},
    )

    assert "promote_to_durable 0/1" in failure


def test_verify_fail_signal_loops_back_to_execute(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    task_id = "r-verify-fail:verify"
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "sig-submit-fail",
                    "created_at": time.time(),
                    "kind": "submit_verification",
                    "payload": {"status": "fail", "details": "http boot failed"},
                    "task_id": task_id,
                    "phase": "verify",
                }
            )
            + "\n"
        )
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "verify.yaml"
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    node = PhaseNode(id="verify", manifest_id="verify", status="running", started_at=0)
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-verify-fail",
        nodes=[node],
    )

    failure = runner._phase_completion_failure(
        phase_node=node,
        plan=plan,
        manifest=manifest,
        outcome={"task_id": task_id},
    )
    target = runner._phase_loop_back_target(
        phase_node=node,
        outcome={"task_id": task_id},
    )

    assert failure == ""
    assert target == "execute"


def test_verify_completion_requires_promote_tag(tmp_workspace):
    drive = tmp_workspace["drive_root"]
    task_id = "r-verify-missing-tag:verify"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    with (state / "phase_control_signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "sig-submit",
                    "created_at": time.time(),
                    "kind": "submit_verification",
                    "payload": {"status": "pass", "details": "green"},
                    "task_id": task_id,
                    "phase": "verify",
                }
            )
            + "\n"
        )
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "task_id": task_id,
                    "tool": "promote_to_durable",
                    "args": {"tags": "durable"},
                    "result_preview": '{"saved": true, "id": "durable-1"}',
                }
            )
            + "\n"
        )
    manifest = load_manifest(
        tmp_workspace["repo_root"] / "umbrella" / "phases" / "manifests" / "verify.yaml"
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=drive,
        launcher=_FakeLauncher(drive),
    )
    node = PhaseNode(id="verify", manifest_id="verify", status="running", started_at=0)
    plan = PhasePlan(
        plan_id="plan",
        workspace_id=tmp_workspace["workspace_id"],
        run_id="r-verify-missing-tag",
        nodes=[node],
    )

    failure = runner._phase_completion_failure(
        phase_node=node,
        plan=plan,
        manifest=manifest,
        outcome={"task_id": task_id},
    )

    assert "promote_to_durable 0/1" in failure


def test_runner_loopback_revisits_previous_phase(tmp_workspace):
    launcher = _ReviewFakeLauncher(
        tmp_workspace["drive_root"],
        verdict="revise",
        loop_back_target="plan",
    )
    runner = PhaseRunner(
        repo_root=tmp_workspace["repo_root"],
        workspace_id=tmp_workspace["workspace_id"],
        drive_root=tmp_workspace["drive_root"],
        launcher=launcher,
    )

    results = list(
        runner.run("test task", phases=["plan", "plan_review"], run_id="r-loop")
    )

    assert all(r.ok for r in results)
    assert launcher.submitted.count("r-loop:plan") == 2
    assert launcher.submitted.count("r-loop:plan_review") == 2


def test_watcher_abort_signal_write_read(tmp_workspace):
    """Watcher can write a signal and the poll loop reads it back correctly."""
    import time, uuid
    drive = tmp_workspace["drive_root"]
    watcher = WatcherPollLoop(drive)
    sig = WatcherSignal(
        signal_id=str(uuid.uuid4()),
        created_at=time.time(),
        kind="abort_phase",
        reason="stall test",
        trigger="stall",
    )
    watcher.write_signal(sig)
    read_back = watcher.read_pending_signal()
    assert read_back is not None
    assert read_back.kind == "abort_phase"
    assert read_back.reason == "stall test"


def test_phase_plan_serialization_roundtrip(tmp_path):
    plan = build_default_plan("ws-round", run_id="rr1")
    plan.get_node("execute").subtasks = [
        SubtaskCard(
            id="st1",
            title="One",
            goal="Do one thing",
            allowed_tools=frozenset({"shell"}),
            allowed_skills=frozenset(),
            success_test=SuccessTest(kind="cmd", value="pytest && echo ✓"),
            files_to_create=["src/app.py"],
            files_to_change=["tests/test_app.py"],
            dependencies=["setup"],
        )
    ]
    plan.mutate({"extra_key": "value"}, actor="test")
    save_plan(plan, tmp_path)
    loaded = load_plan(tmp_path)
    assert loaded.version == 1
    assert len(loaded.edits_log) == 1
    assert loaded.edits_log[0].actor == "test"
    execute = loaded.get_node("execute")
    assert execute.subtasks[0].id == "st1"
    assert execute.subtasks[0].success_test.value == "pytest && echo ✓"
    assert execute.subtasks[0].files_to_create == ["src/app.py"]
    assert execute.subtasks[0].files_to_change == ["tests/test_app.py"]
    assert execute.subtasks[0].dependencies == ["setup"]


def test_phase_plan_load_maps_verification_alias_to_success_test(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-alias",
                "workspace_id": "ws-alias",
                "run_id": "run-alias",
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "pending",
                        "subtasks": [
                            {
                                "id": "st_alias",
                                "title": "Alias test",
                                "goal": "Keep verification evidence",
                                "verification": "pytest tests/test_api.py -q",
                            }
                        ],
                    }
                ],
                "version": 0,
                "edits_log": [],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_plan(tmp_path)

    execute = loaded.get_node("execute")
    assert execute.subtasks[0].success_test.value == "pytest tests/test_api.py -q"
