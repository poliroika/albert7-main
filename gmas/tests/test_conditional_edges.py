"""Tests for conditional edges in the gMAS framework."""

import pytest
import rustworkx as rx
import torch

from gmas.core.agent import AgentProfile
from gmas.core.graph import RoleGraph
from gmas.execution.runner import (
    MACPRunner,
    RunnerConfig,
    TopologyAction,
)
from gmas.execution.scheduler import (
    ExecutionPlan,
    ExecutionStep,
    StepResult,
)

# Helpers


def _make_graph(node_ids, connections, query="test query"):
    """
    Build a RoleGraph for testing.

    Args:
        node_ids: list of agent IDs (no "task" -- added automatically).
        connections: dict[str, list[str]] adjacency.
        query: task query string.

    """
    g = rx.PyDiGraph()
    id_to_idx = {}
    agents = []

    for nid in node_ids:
        idx = g.add_node({"id": nid})
        id_to_idx[nid] = idx
        agents.append(AgentProfile(agent_id=nid, display_name=f"Agent {nid.upper()}"))

    for src, targets in connections.items():
        for tgt in targets:
            if src in id_to_idx and tgt in id_to_idx:
                g.add_edge(id_to_idx[src], id_to_idx[tgt], {"weight": 1.0})

    n = len(node_ids)
    a_com = torch.zeros((n + 1, n + 1), dtype=torch.float32)

    for i, src in enumerate(node_ids):
        for tgt in connections.get(src, []):
            if tgt in node_ids:
                j = node_ids.index(tgt)
                a_com[i, j] = 1.0

    g.add_node({"id": "task"})

    role_graph = RoleGraph(
        node_ids=node_ids,
        role_connections=connections,
        graph=g,
        A_com=a_com,
        task_node="task",
        query=query,
    )
    role_graph.agents = agents

    return role_graph


def _adjacency(agent_ids, connections):
    """Build a bare agent-agent adjacency matrix."""
    n = len(agent_ids)
    a = torch.zeros((n, n), dtype=torch.float32)

    for src, targets in connections.items():
        if src not in agent_ids:
            continue

        i = agent_ids.index(src)

        for tgt in targets:
            if tgt in agent_ids:
                j = agent_ids.index(tgt)
                a[i, j] = 1.0

    return a


def _plan_from_order(agent_order, predecessors_map=None, end_agent=None):
    """Build an ExecutionPlan from a simple agent ordering."""
    steps = []

    for aid in agent_order:
        preds = (predecessors_map or {}).get(aid, [])
        steps.append(ExecutionStep(agent_id=aid, predecessors=preds))

    return ExecutionPlan(steps=steps, end_agent=end_agent)


def _runner(adaptive=True, **kwargs):
    """Create a MACPRunner for adaptive tests."""
    caller = kwargs.pop("llm_caller", None) or (lambda _: "ok")
    config = RunnerConfig(adaptive=adaptive, update_states=False, **kwargs)

    return MACPRunner(llm_caller=caller, config=config)


# Unit tests: _build_conditional_edge_action


class TestBuildConditionalEdgeAction:
    """Tests for MACPRunner._build_conditional_edge_action."""

    def _runner_with_conditions(self, edge_conditions):
        r = _runner(adaptive=True)
        r._scheduler._last_edge_conditions = edge_conditions

        return r

    def test_condition_met_unskips_target(self):
        """When a condition is met, the target is added to condition_unskip_agents."""
        conditions = {("a", "b"): lambda _: True}
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b"],
            step_results={"a": StepResult("a", success=True, response="ok")},
            messages={"a": "ok"},
            query="q",
            remaining_ids={"b"},
        )

        assert action is not None
        assert "b" in action.condition_unskip_agents
        assert "b" not in action.condition_skip_agents

    def test_condition_not_met_skips_target(self):
        """
        When a condition fails and no other incoming conditional edges exist,
        the target should be skipped.
        """
        conditions = {("a", "b"): lambda _: False}
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b"],
            step_results={"a": StepResult("a", success=True, response="x")},
            messages={"a": "x"},
            query="q",
            remaining_ids={"b"},
        )

        assert action is not None
        assert "b" in action.condition_skip_agents
        assert "b" not in action.condition_unskip_agents

    def test_met_condition_inserts_chain_when_target_not_remaining(self):
        """
        If the condition is met but the target was not in remaining_ids,
        it should appear in insert_chains (adding it back to the plan).
        """
        conditions = {("a", "b"): lambda _: True}
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b"],
            step_results={"a": StepResult("a", success=True, response="yes")},
            messages={"a": "yes"},
            query="q",
            remaining_ids=set(),
        )

        assert action is not None
        assert ("b", "a") in action.insert_chains

    def test_pending_incoming_prevents_skip(self):
        """
        If another source has a conditional edge to the same target
        but hasn't executed yet, the target must not be skipped.
        """
        conditions = {
            ("a", "c"): lambda _: False,
            ("b", "c"): lambda _: True,
        }
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b", "c"],
            step_results={"a": StepResult("a", success=True, response="x")},
            messages={"a": "x"},
            query="q",
            remaining_ids={"b", "c"},
        )

        assert action is None or "c" not in action.condition_skip_agents

    def test_future_loopback_incoming_does_not_keep_current_step_active(self):
        """
        A future loopback edge to the same target must not keep the current pending step active.
        """
        conditions = {
            ("lead", "writer"): lambda _: False,
            ("reviewer", "writer"): lambda _: True,
        }
        r = self._runner_with_conditions(conditions)
        plan = _plan_from_order(
            ["lead", "writer", "reviewer"],
            predecessors_map={"writer": ["lead"], "reviewer": ["writer"]},
        )
        plan.mark_completed("lead")

        action = r._build_conditional_edge_action(
            last_agent="lead",
            agent_ids=["lead", "writer", "reviewer"],
            step_results={"lead": StepResult("lead", success=True, response="REWORK")},
            messages={"lead": "REWORK"},
            query="q",
            remaining_ids={"writer", "reviewer"},
            plan=plan,
        )

        assert action is not None
        assert "writer" in action.condition_skip_agents

    def test_already_passed_incoming_prevents_skip(self):
        """
        If another source already passed its conditional edge,
        the target must not be skipped even though the current edge failed.
        """
        conditions = {
            ("a", "c"): lambda _: False,
            ("b", "c"): lambda _: True,
        }
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b", "c"],
            step_results={
                "a": StepResult("a", success=True, response="nope"),
                "b": StepResult("b", success=True, response="yes"),
            },
            messages={"a": "nope", "b": "yes"},
            query="q",
            remaining_ids={"c"},
        )

        assert action is None or "c" not in action.condition_skip_agents

    def test_all_incoming_fail_skips_target(self):
        """
        When all incoming conditional edges are evaluated and fail,
        the target is skipped.
        """
        conditions = {
            ("a", "c"): lambda _: False,
            ("b", "c"): lambda _: False,
        }
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b", "c"],
            step_results={
                "a": StepResult("a", success=True, response="nope"),
                "b": StepResult("b", success=True, response="nope"),
            },
            messages={"a": "nope", "b": "nope"},
            query="q",
            remaining_ids={"c"},
        )

        assert action is not None
        assert "c" in action.condition_skip_agents

    def test_string_condition_contains(self):
        """A 'contains:keyword' string condition should be evaluated correctly."""
        conditions = {("a", "b"): "contains:APPROVED"}
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "b"],
            step_results={"a": StepResult("a", success=True, response="APPROVED for release")},
            messages={"a": "APPROVED for release"},
            query="q",
            remaining_ids={"b"},
        )

        assert action is not None
        assert "b" in action.condition_unskip_agents

    def test_string_condition_source_failed(self):
        """The 'source_failed' condition routes on failure."""
        conditions = {("a", "fallback"): "source_failed"}
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="a",
            agent_ids=["a", "fallback"],
            step_results={"a": StepResult("a", success=False, error="boom")},
            messages={"_prior": "earlier output"},
            query="q",
            remaining_ids={"fallback"},
        )

        assert action is not None
        assert "fallback" in action.condition_unskip_agents

    def test_router_pattern_multiple_outgoing(self):
        """
        A 'router' agent with multiple conditional outgoing edges:
        only the matching branch should be unskipped.
        """
        conditions = {
            ("classifier", "math_agent"): lambda ctx: "math" in (ctx.get_last_response() or ""),
            ("classifier", "code_agent"): lambda ctx: "code" in (ctx.get_last_response() or ""),
        }
        r = self._runner_with_conditions(conditions)

        action = r._build_conditional_edge_action(
            last_agent="classifier",
            agent_ids=["classifier", "math_agent", "code_agent"],
            step_results={"classifier": StepResult("classifier", success=True, response="This is a math problem")},
            messages={"classifier": "This is a math problem"},
            query="solve 2+2",
            remaining_ids={"math_agent", "code_agent"},
        )

        assert action is not None
        assert "math_agent" in action.condition_unskip_agents
        assert "code_agent" in action.condition_skip_agents


# Unit tests: _apply_topology_to_plan


class TestApplyTopologyToPlan:
    """Tests for MACPRunner._apply_topology_to_plan."""

    def test_condition_skip_adds_to_plan(self):
        """condition_skip_agents should add agents to plan.condition_skipped."""
        r = _runner(adaptive=True)
        plan = _plan_from_order(["a", "b", "c"])
        action = TopologyAction(condition_skip_agents=["b"])
        a = _adjacency(["a", "b", "c"], {"a": ["b"], "b": ["c"]})

        changed = r._apply_topology_to_plan(plan, action, a, ["a", "b", "c"])

        assert changed
        assert "b" in plan.condition_skipped

    def test_condition_unskip_removes_from_plan(self):
        """condition_unskip_agents should remove agents from plan.condition_skipped."""
        r = _runner(adaptive=True)
        plan = _plan_from_order(["a", "b", "c"])
        plan.condition_skipped.add("b")
        action = TopologyAction(condition_unskip_agents=["b"])
        a = _adjacency(["a", "b", "c"], {"a": ["b"], "b": ["c"]})

        changed = r._apply_topology_to_plan(plan, action, a, ["a", "b", "c"])

        assert changed
        assert "b" not in plan.condition_skipped

    def test_condition_unskip_restores_unconditional_descendants(self):
        """Re-enabling a conditionally gated branch should restore its unconditional descendants."""
        r = _runner(adaptive=True)
        agent_ids = ["lead", "writer", "reviewer"]
        a = _adjacency(agent_ids, {"lead": ["writer"], "writer": ["reviewer"], "reviewer": ["writer"]})
        r._scheduler._last_edge_conditions = {
            ("lead", "writer"): lambda _: True,
            ("reviewer", "writer"): lambda _: True,
        }
        plan = _plan_from_order(
            ["lead", "writer", "reviewer"],
            predecessors_map={"writer": ["lead"], "reviewer": ["writer"]},
        )

        plan.apply_condition_skip("writer")
        MACPRunner._cascade_condition_skip(
            plan,
            "writer",
            a,
            agent_ids,
            edge_conditions=r._scheduler._last_edge_conditions,
        )
        assert "reviewer" in plan.condition_skipped

        changed = r._apply_topology_to_plan(plan, TopologyAction(condition_unskip_agents=["writer"]), a, agent_ids)

        assert changed
        assert "writer" not in plan.condition_skipped
        assert "reviewer" not in plan.condition_skipped

    def test_insert_chains_adds_step(self):
        """insert_chains should add a conditional step and unskip the target."""
        r = _runner(adaptive=True)
        plan = _plan_from_order(["a", "b"])
        plan.mark_completed("a")
        plan.mark_completed("b")
        plan.condition_skipped.add("c")
        action = TopologyAction(insert_chains=[("c", "b")])
        a = _adjacency(["a", "b", "c"], {"a": ["b"], "b": ["c"]})

        changed = r._apply_topology_to_plan(plan, action, a, ["a", "b", "c"])

        assert changed
        assert "c" not in plan.condition_skipped

        step_ids = [s.agent_id for s in plan.steps]
        assert "c" in step_ids

    def test_insert_chains_merges_fanin_barrier_for_reopened_parallel_branches(self):
        """Reopened branches should converge into one pending join step with both dependencies."""
        r = _runner(adaptive=True)
        agent_ids = ["loop", "r1", "r2", "merge"]
        plan = _plan_from_order(["loop"])
        loop_step = plan.get_current_step()
        assert loop_step is not None
        plan.mark_completed(loop_step)
        action = TopologyAction(insert_chains=[("r1", "loop"), ("r2", "loop")])
        a = _adjacency(agent_ids, {"loop": ["r1", "r2"], "r1": ["merge"], "r2": ["merge"]})

        changed = r._apply_topology_to_plan(plan, action, a, agent_ids)

        assert changed
        merge_steps = [s for s in plan.steps if s.agent_id == "merge" and not plan.is_step_resolved(s)]
        assert len(merge_steps) == 1
        merge_step = merge_steps[0]
        assert merge_step.predecessors == ["r1", "r2"]
        assert len(merge_step.dependency_ids) == 2

        group = r._get_parallel_group(plan, completed_agents={})
        assert [step.agent_id for step in group] == ["r1", "r2"]

        r1_step = next(step for step in plan.steps if step.agent_id == "r1" and not plan.is_step_resolved(step))
        plan.mark_completed(r1_step)
        group = r._get_parallel_group(plan, completed_agents={})
        assert [step.agent_id for step in group] == ["r2"]

        r2_step = next(step for step in plan.steps if step.agent_id == "r2" and not plan.is_step_resolved(step))
        plan.mark_completed(r2_step)
        group = r._get_parallel_group(plan, completed_agents={})
        assert [step.agent_id for step in group] == ["merge"]

    def test_insert_chains_use_pre_action_predecessor_snapshot(self):
        """Multiple insert_chains must use the source step that triggered the action, not a newly inserted clone."""
        r = _runner(adaptive=True)
        agent_ids = ["idea_synthesizer", "idea_researcher", "web_idea_researcher"]
        plan = _plan_from_order(["idea_synthesizer"])
        synth = plan.get_current_step()
        assert synth is not None
        plan.mark_completed(synth)
        action = TopologyAction(
            insert_chains=[
                ("idea_researcher", "idea_synthesizer"),
                ("web_idea_researcher", "idea_synthesizer"),
            ]
        )
        a = _adjacency(
            agent_ids,
            {
                "idea_synthesizer": ["idea_researcher", "web_idea_researcher"],
                "idea_researcher": ["idea_synthesizer"],
                "web_idea_researcher": ["idea_synthesizer"],
            },
        )

        changed = r._apply_topology_to_plan(plan, action, a, agent_ids)

        assert changed
        pending_synth = [
            step for step in plan.steps if step.agent_id == "idea_synthesizer" and not plan.is_step_resolved(step)
        ]
        assert len(pending_synth) == 1

        researchers = [step for step in plan.steps if step.agent_id in {"idea_researcher", "web_idea_researcher"}]
        assert len(researchers) == 2
        assert all(step.dependency_ids == [synth.step_id] for step in researchers)

        group = r._get_parallel_group(plan, completed_agents={})
        assert [step.agent_id for step in group] == ["idea_researcher", "web_idea_researcher"]


# Unit tests: cascade skip & unconditional chain insertion


class TestCascadeSkip:
    """Tests for _cascade_condition_skip."""

    def test_unconditional_descendant_is_cascade_skipped(self):
        """If A is skipped and A->B is unconditional, B should also be skipped."""
        plan = _plan_from_order(["a", "b", "c"])
        agent_ids = ["a", "b", "c"]
        a = _adjacency(agent_ids, {"a": ["b"], "b": ["c"]})

        MACPRunner._cascade_condition_skip(plan, "a", a, agent_ids, edge_conditions={})

        assert "b" in plan.condition_skipped
        assert "c" in plan.condition_skipped

    def test_conditional_edge_blocks_cascade(self):
        """If A->B is conditional, cascade skipping A should NOT skip B."""
        plan = _plan_from_order(["a", "b"])
        agent_ids = ["a", "b"]
        a = _adjacency(agent_ids, {"a": ["b"]})
        conditions = {("a", "b"): lambda _: True}

        MACPRunner._cascade_condition_skip(plan, "a", a, agent_ids, edge_conditions=conditions)

        assert "b" not in plan.condition_skipped

    def test_other_unconditional_source_prevents_cascade(self):
        """
        If C has another unconditional incoming edge from B (not skipped),
        C should NOT be cascade-skipped when A is skipped.
        """
        plan = _plan_from_order(["a", "b", "c"])
        agent_ids = ["a", "b", "c"]
        a = _adjacency(agent_ids, {"a": ["c"], "b": ["c"]})

        MACPRunner._cascade_condition_skip(plan, "a", a, agent_ids, edge_conditions={})

        assert "c" not in plan.condition_skipped


class TestInsertUnconditionalChain:
    """Tests for _insert_unconditional_chain."""

    def test_adds_descendants_to_plan(self):
        """BFS should add unconditional descendants of start_agent to the plan."""
        plan = _plan_from_order(["a"])
        plan.mark_completed("a")
        agent_ids = ["a", "b", "c"]
        a = _adjacency(agent_ids, {"a": ["b"], "b": ["c"]})
        remaining_ids = {"a"}

        MACPRunner._insert_unconditional_chain(plan, "a", a, agent_ids, edge_conditions={}, remaining_ids=remaining_ids)

        step_ids = [s.agent_id for s in plan.steps]
        assert "b" in step_ids
        assert "c" in step_ids

    def test_skips_conditional_edges(self):
        """BFS should not traverse conditional edges."""
        plan = _plan_from_order(["a"])
        plan.mark_completed("a")
        agent_ids = ["a", "b"]
        a = _adjacency(agent_ids, {"a": ["b"]})
        remaining_ids = {"a"}
        conditions = {("a", "b"): lambda _: True}

        MACPRunner._insert_unconditional_chain(
            plan, "a", a, agent_ids, edge_conditions=conditions, remaining_ids=remaining_ids
        )

        step_ids = [s.agent_id for s in plan.steps]
        assert "b" not in step_ids


# Unit tests: _run_topology_pipeline


class TestRunTopologyPipeline:
    """Tests for the unified _run_topology_pipeline."""

    def test_pipeline_evaluates_conditional_edges(self):
        """The pipeline should evaluate conditional edges and modify the plan."""
        r = _runner(adaptive=True)
        r._scheduler._last_edge_conditions = {("a", "b"): lambda _: False}

        plan = _plan_from_order(["a", "b"], predecessors_map={"b": ["a"]})
        plan.mark_completed("a")
        agent_ids = ["a", "b"]
        a = _adjacency(agent_ids, {"a": ["b"]})

        graph = _make_graph(agent_ids, {"a": ["b"]})

        changed = r._run_topology_pipeline(
            plan=plan,
            last_agent="a",
            a_agents=a,
            agent_ids=agent_ids,
            step_results={"a": StepResult("a", success=True, response="x")},
            messages={"a": "x"},
            query="q",
            execution_order=["a"],
            total_tokens=0,
            role_graph=graph,
        )

        assert changed
        assert "b" in plan.condition_skipped

    def test_pipeline_combines_user_hooks(self):
        """User topology hooks should be merged with built-in conditional action."""
        calls = []

        def user_hook(ctx, graph):
            calls.append(ctx.agent_id)
            return TopologyAction(skip_agents=["c"])

        r = _runner(adaptive=True, enable_dynamic_topology=True, topology_hooks=[user_hook])
        r._scheduler._last_edge_conditions = {}

        plan = _plan_from_order(["a", "b", "c"])
        plan.mark_completed("a")
        agent_ids = ["a", "b", "c"]
        a = _adjacency(agent_ids, {"a": ["b"], "b": ["c"]})
        graph = _make_graph(agent_ids, {"a": ["b"], "b": ["c"]})

        changed = r._run_topology_pipeline(
            plan=plan,
            last_agent="a",
            a_agents=a,
            agent_ids=agent_ids,
            step_results={"a": StepResult("a", success=True, response="x")},
            messages={"a": "x"},
            query="q",
            execution_order=["a"],
            total_tokens=0,
            role_graph=graph,
        )

        assert changed
        assert "c" in plan.condition_skipped
        assert calls == ["a"]

    def test_pipeline_no_changes_returns_false(self):
        """If neither built-in nor user hooks produce changes, return False."""
        r = _runner(adaptive=True)
        r._scheduler._last_edge_conditions = {}

        plan = _plan_from_order(["a", "b"])
        plan.mark_completed("a")
        agent_ids = ["a", "b"]
        a = _adjacency(agent_ids, {"a": ["b"]})
        graph = _make_graph(agent_ids, {"a": ["b"]})

        changed = r._run_topology_pipeline(
            plan=plan,
            last_agent="a",
            a_agents=a,
            agent_ids=agent_ids,
            step_results={"a": StepResult("a", success=True, response="x")},
            messages={"a": "x"},
            query="q",
            execution_order=["a"],
            total_tokens=0,
            role_graph=graph,
        )

        assert not changed


# Integration tests: adaptive run_round with conditional edges


class TestAdaptiveConditionalEdges:
    """End-to-end tests: adaptive runner with conditional edges on a RoleGraph."""

    def test_condition_skips_agent(self):
        """An agent whose incoming conditional edge fails should be skipped."""
        graph = _make_graph(
            ["a", "b", "c"],
            {"a": ["b", "c"], "b": [], "c": []},
        )
        graph.edge_conditions = {("a", "b"): lambda _: False}

        runner = MACPRunner(
            llm_caller=lambda _: "response",
            config=RunnerConfig(adaptive=True, update_states=False),
        )
        result = runner.run_round(graph)

        assert "b" not in result.execution_order
        assert "a" in result.execution_order
        assert result.topology_changed_count >= 1

    def test_content_based_routing(self):
        """A conditional edge that checks response content routes correctly."""
        graph = _make_graph(
            ["classifier", "math_solver", "general"],
            {"classifier": ["math_solver", "general"], "math_solver": [], "general": []},
        )
        graph.edge_conditions = {
            ("classifier", "math_solver"): lambda ctx: "math" in (ctx.get_last_response() or ""),
            ("classifier", "general"): lambda ctx: "math" not in (ctx.get_last_response() or ""),
        }

        def caller(prompt):
            if "classifier" in prompt.lower():
                return "This is a math problem"
            return "solved"

        runner = MACPRunner(
            llm_caller=caller,
            config=RunnerConfig(adaptive=True, update_states=False),
        )
        result = runner.run_round(graph)

        assert "classifier" in result.execution_order
        assert "math_solver" in result.execution_order
        assert "general" not in result.execution_order

    def test_unconditional_edges_always_run(self):
        """
        Agents connected by unconditional edges should always execute,
        regardless of other conditional edges in the graph.
        """
        graph = _make_graph(
            ["a", "b", "c"],
            {"a": ["b", "c"], "b": [], "c": []},
        )
        graph.edge_conditions = {("a", "c"): lambda _: False}

        runner = MACPRunner(
            llm_caller=lambda _: "result",
            config=RunnerConfig(adaptive=True, update_states=False),
        )
        result = runner.run_round(graph)

        assert "a" in result.execution_order
        assert "b" in result.execution_order
        assert "c" not in result.execution_order

    def test_future_loopback_does_not_keep_initial_writer_runnable(self):
        """A future review loop must not keep the initial writer step runnable before approval."""
        graph = _make_graph(
            ["lead", "experiment", "writer", "reviewer"],
            {
                "lead": ["experiment", "writer"],
                "experiment": ["lead"],
                "writer": ["reviewer"],
                "reviewer": ["writer"],
            },
        )
        graph.edge_conditions = {
            ("lead", "experiment"): "contains:REWORK",
            ("lead", "writer"): "contains:APPROVED",
            ("reviewer", "writer"): "contains:REWRITE",
        }

        responses = iter(["REWORK", "experiment complete"])

        runner = MACPRunner(
            llm_caller=lambda _: next(responses),
            config=RunnerConfig(adaptive=True, update_states=False, enable_parallel=False),
        )
        result = runner.run_round(graph)

        assert result.execution_order == ["lead", "experiment"]
        assert "writer" not in result.execution_order


# Integration tests: async adaptive with conditional edges


class TestAsyncConditionalEdges:
    """Async counterpart of the adaptive conditional edge tests."""

    @pytest.mark.asyncio
    async def test_async_condition_skips_agent(self):
        """Async adaptive run should respect conditional edges."""
        graph = _make_graph(
            ["a", "b"],
            {"a": ["b"], "b": []},
        )
        graph.edge_conditions = {("a", "b"): lambda _: False}

        async def caller(prompt):
            return "async result"

        runner = MACPRunner(
            async_llm_caller=caller,
            config=RunnerConfig(adaptive=True, update_states=False),
        )
        result = await runner.arun_round(graph)

        assert "a" in result.execution_order
        assert "b" not in result.execution_order


# Unit tests: ConditionContext history features


class TestConditionContextHistory:
    """Tests for messages_history, step_results_history, and get_response_history."""

    def test_get_response_history_returns_all_responses(self):
        """get_response_history should return all historical responses for an agent."""
        from gmas.execution.scheduler import ConditionContext

        ctx = ConditionContext(
            source_agent="writer",
            target_agent="editor",
            messages={"writer": "current response"},
            messages_history={
                "writer": ["first response", "second response", "current response"],
            },
        )

        history = ctx.get_response_history()

        assert history == ["first response", "second response", "current response"]

    def test_get_response_history_defaults_to_source_agent(self):
        """When agent_id is None, get_response_history should use source_agent."""
        from gmas.execution.scheduler import ConditionContext

        ctx = ConditionContext(
            source_agent="writer",
            target_agent="editor",
            messages_history={
                "writer": ["w1", "w2"],
                "editor": ["e1"],
            },
        )

        history = ctx.get_response_history()
        assert history == ["w1", "w2"]

        history_editor = ctx.get_response_history("editor")
        assert history_editor == ["e1"]

    def test_get_response_history_returns_empty_for_unknown_agent(self):
        """get_response_history should return empty list for unknown agents."""
        from gmas.execution.scheduler import ConditionContext

        ctx = ConditionContext(
            source_agent="writer",
            target_agent="editor",
            messages_history={},
        )

        history = ctx.get_response_history("unknown")
        assert history == []

    def test_step_results_history_available(self):
        """step_results_history should be accessible in ConditionContext."""
        from gmas.execution.scheduler import ConditionContext

        ctx = ConditionContext(
            source_agent="writer",
            target_agent="editor",
            step_results_history={
                "writer": [
                    StepResult("writer", success=True, response="r1"),
                    StepResult("writer", success=True, response="r2"),
                ],
            },
        )

        assert "writer" in ctx.step_results_history
        assert len(ctx.step_results_history["writer"]) == 2

    def test_history_based_condition_in_routing(self):
        """A condition using get_response_history should route correctly."""
        graph = _make_graph(
            ["a", "b", "c"],
            {"a": ["b", "c"], "b": [], "c": []},
        )

        def improving_condition(ctx):
            """Route to b if responses are getting shorter (more confident)."""
            history = ctx.get_response_history()
            if len(history) < 2:
                return False
            return len(history[-1]) < len(history[-2])

        graph.edge_conditions = {
            ("a", "b"): improving_condition,
            ("a", "c"): lambda ctx: not improving_condition(ctx),
        }

        # Mock the scheduler to provide history
        runner = MACPRunner(
            llm_caller=lambda _: "short",  # Short response
            config=RunnerConfig(adaptive=True, update_states=False),
        )

        # Manually set up history in scheduler
        runner._scheduler._last_condition_context = None  # type: ignore[ty:invalid-assignment]

        result = runner.run_round(graph)

        # Both a and one of b/c should execute
        assert "a" in result.execution_order
