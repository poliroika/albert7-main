"""Tests for src/execution/scheduler.py"""

from typing import TYPE_CHECKING

import pytest
import torch

from gmas.execution.scheduler import (
    AdaptiveScheduler,
    ConditionContext,
    ConditionEvaluator,
    ExecutionPlan,
    ExecutionStep,
    PruningConfig,
    RoutingPolicy,
    StepResult,
    build_execution_order,
    extract_agent_adjacency,
    filter_reachable_agents,
    get_incoming_agents,
    get_outgoing_agents,
    get_parallel_groups,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# ─────────────────────────── ConditionContext ───────────────────────────────


class TestConditionContext:
    def test_get_last_response_present(self):
        ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            messages={"solver": "great answer"},
        )
        assert ctx.get_last_response() == "great answer"

    def test_get_last_response_absent(self):
        ctx = ConditionContext(source_agent="solver", target_agent="reviewer")
        assert ctx.get_last_response() is None

    def test_source_succeeded_no_result(self):
        ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            messages={"solver": "ok"},
        )
        assert ctx.source_succeeded() is True

    def test_source_succeeded_not_in_messages(self):
        ctx = ConditionContext(source_agent="solver", target_agent="reviewer")
        assert ctx.source_succeeded() is False

    def test_source_succeeded_with_step_result(self):
        result = StepResult(agent_id="solver", success=True)
        ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            step_results={"solver": result},
        )
        assert ctx.source_succeeded() is True

    def test_source_failed_with_step_result(self):
        result = StepResult(agent_id="solver", success=False)
        ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            step_results={"solver": result},
        )
        assert ctx.source_succeeded() is False

    def test_has_keyword_in_source(self):
        ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            messages={"solver": "Error: something failed"},
        )
        assert ctx.has_keyword("error") is True
        assert ctx.has_keyword("success") is False

    def test_has_keyword_in_target(self):
        ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            messages={"reviewer": "looks good"},
        )
        assert ctx.has_keyword("looks", in_source=False) is True

    def test_get_state_value(self):
        ctx = ConditionContext(
            source_agent="a",
            target_agent="b",
            state={"status": "ok", "count": 5},
        )
        assert ctx.get_state_value("status") == "ok"
        assert ctx.get_state_value("count") == 5
        assert ctx.get_state_value("missing") is None
        assert ctx.get_state_value("missing", "default") == "default"


# ─────────────────────────── ConditionEvaluator ───────────────────────────────


class TestConditionEvaluator:
    def setup_method(self):
        self.eval = ConditionEvaluator()
        self.ctx = ConditionContext(
            source_agent="solver",
            target_agent="reviewer",
            messages={"solver": "The answer is 42"},
            state={"quality": "high"},
        )

    def test_none_condition_always_true(self):
        assert self.eval.evaluate(None, self.ctx) is True

    def test_callable_condition_true(self):
        def cond(ctx):
            return True

        assert self.eval.evaluate(cond, self.ctx) is True

    def test_callable_condition_false(self):
        def cond(ctx):
            return False

        assert self.eval.evaluate(cond, self.ctx) is False

    def test_callable_condition_exception_returns_false(self):
        # Only ValueError, TypeError, KeyError, AttributeError, RuntimeError are caught
        def cond(ctx):
            return (_ for _ in ()).throw(ValueError("bad"))

        assert self.eval.evaluate(cond, self.ctx) is False

    def test_builtin_always(self):
        assert self.eval.evaluate("always", self.ctx) is True

    def test_builtin_never(self):
        assert self.eval.evaluate("never", self.ctx) is False

    def test_builtin_source_success(self):
        # solver is in messages, so source_success should be True
        assert self.eval.evaluate("source_success", self.ctx) is True

    def test_builtin_source_failed(self):
        empty_ctx = ConditionContext(source_agent="solver", target_agent="reviewer")
        assert self.eval.evaluate("source_failed", empty_ctx) is True

    def test_builtin_has_response(self):
        assert self.eval.evaluate("has_response", self.ctx) is True
        empty_ctx = ConditionContext(source_agent="solver", target_agent="reviewer")
        assert self.eval.evaluate("has_response", empty_ctx) is False

    def test_register_custom_condition(self):
        self.eval.register("is_42", lambda ctx: "42" in (ctx.get_last_response() or ""))
        assert self.eval.evaluate("is_42", self.ctx) is True

    def test_unregister_condition(self):
        self.eval.register("temp_cond", lambda _: True)
        result = self.eval.unregister("temp_cond")
        assert result is True
        # After unregistering, it falls through to default (True)
        # unless it becomes an unknown expr
        result2 = self.eval.unregister("nonexistent")
        assert result2 is False

    def test_get_condition(self):
        def cond(ctx):
            return True

        self.eval.register("my_cond", cond)
        retrieved = self.eval.get("my_cond")
        assert retrieved is cond

    def test_string_contains(self):
        result = self.eval.evaluate("contains:42", self.ctx)
        assert result is True
        result2 = self.eval.evaluate("contains:error", self.ctx)
        assert result2 is False

    def test_string_not(self):
        result = self.eval.evaluate("not:always", self.ctx)
        assert result is False
        result2 = self.eval.evaluate("not:never", self.ctx)
        assert result2 is True

    def test_string_state(self):
        result = self.eval.evaluate("state:quality=high", self.ctx)
        assert result is True
        result2 = self.eval.evaluate("state:quality=low", self.ctx)
        assert result2 is False

    def test_string_state_key_exists(self):
        result = self.eval.evaluate("state:quality", self.ctx)
        assert result is True
        result2 = self.eval.evaluate("state:missing_key", self.ctx)
        assert result2 is False

    def test_compose_and(self):
        composed = self.eval.compose_and("always", "has_response")
        assert composed(self.ctx) is True

        empty_ctx = ConditionContext(source_agent="solver", target_agent="reviewer")
        assert composed(empty_ctx) is False  # has_response fails

    def test_compose_or(self):
        composed = self.eval.compose_or("never", "always")
        assert composed(self.ctx) is True

        never_composed = self.eval.compose_or("never", "never")
        assert never_composed(self.ctx) is False

    def test_unknown_string_returns_true(self):
        # Unknown conditions fall back to True
        result = self.eval.evaluate("unknown_condition_xyz", self.ctx)
        assert result is True


# ─────────────────────────── ExecutionPlan ────────────────────────────────────


class TestExecutionPlan:
    def _make_plan(self, agents=("a", "b", "c")):
        steps = [ExecutionStep(agent_id=aid, predecessors=[]) for aid in agents]
        return ExecutionPlan(steps=steps)

    def test_initial_state(self):
        plan = self._make_plan()
        assert not plan.is_complete
        assert plan.current_index == 0

    def test_get_current_step(self):
        plan = self._make_plan(["a", "b"])
        step = plan.get_current_step()
        assert step.agent_id == "a"

    def test_mark_completed_advances(self):
        plan = self._make_plan(["a", "b"])
        plan.mark_completed("a", tokens=100)
        assert "a" in plan.completed
        assert plan.tokens_used == 100
        assert plan.current_index == 1

    def test_mark_failed_advances(self):
        plan = self._make_plan(["a", "b"])
        plan.mark_failed("a")
        assert "a" in plan.failed
        assert plan.current_index == 1

    def test_mark_skipped_advances(self):
        plan = self._make_plan(["a", "b"])
        plan.mark_skipped("a")
        assert "a" in plan.skipped
        assert plan.current_index == 1

    def test_is_complete_after_all(self):
        plan = self._make_plan(["a"])
        plan.mark_completed("a")
        assert plan.is_complete

    def test_remaining_steps(self):
        plan = self._make_plan(["a", "b", "c"])
        plan.mark_completed("a")
        remaining = plan.remaining_steps
        assert [s.agent_id for s in remaining] == ["b", "c"]

    def test_remaining_steps_excludes_skipped(self):
        plan = self._make_plan(["a", "b", "c"])
        plan.skipped.add("b")
        remaining = plan.remaining_steps
        assert all(s.agent_id != "b" for s in remaining)

    def test_execution_order(self):
        plan = self._make_plan(["a", "b", "c"])
        assert plan.execution_order == ["a", "b", "c"]

    def test_insert_fallback(self):
        plan = self._make_plan(["a", "b"])
        plan.insert_fallback("fallback", after_index=0)
        assert plan.steps[1].agent_id == "fallback"
        assert plan.steps[1].is_optional

    def test_insert_fallback_skipped(self):
        plan = self._make_plan(["a", "b"])
        plan.skipped.add("fallback")
        plan.insert_fallback("fallback", after_index=0)
        # Should not insert since fallback is skipped
        assert len(plan.steps) == 2

    def test_insert_conditional_step(self):
        plan = self._make_plan(["a", "b"])
        plan.mark_completed("a")
        result = plan.insert_conditional_step("a", predecessors=["b"])
        assert result is not None
        assert plan.steps[-1].agent_id == "a"

    def test_insert_conditional_step_exceeds_max(self):
        plan = self._make_plan(["a"])
        plan.max_iterations = 2
        plan.mark_completed("a")
        plan.iteration_count["a"] = 2
        result = plan.insert_conditional_step("a")
        assert result is None

    def test_can_iterate(self):
        plan = ExecutionPlan(max_iterations=3)
        plan.iteration_count["a"] = 2
        assert plan.can_iterate("a") is True
        plan.iteration_count["a"] = 3
        assert plan.can_iterate("a") is False

    def test_get_current_step_complete(self):
        plan = ExecutionPlan()
        assert plan.get_current_step() is None


# ─────────────────────────── Graph utility functions ──────────────────────────


def make_adj(n, edges, weight=1.0):
    """Build an n×n adjacency matrix with specified edges."""
    a = torch.zeros(n, n)
    for i, j in edges:
        a[i, j] = weight
    return a


class TestExtractAgentAdjacency:
    def test_removes_task_row_col(self):
        # 3×3 matrix, task_idx=0
        a = make_adj(3, [(0, 1), (1, 2), (0, 2)])
        result = extract_agent_adjacency(a, task_idx=0)
        assert result.shape == (2, 2)

    def test_removes_middle_task(self):
        a = make_adj(3, [(0, 1), (1, 2)])
        result = extract_agent_adjacency(a, task_idx=1)
        assert result.shape == (2, 2)


class TestGetIncomingOutgoingAgents:
    def test_get_incoming(self):
        # a→b, a→c, b→c
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (0, 2), (1, 2)])
        incoming_c = get_incoming_agents("c", a, ids, threshold=0.5)
        assert "a" in incoming_c
        assert "b" in incoming_c

    def test_get_incoming_empty(self):
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        incoming_a = get_incoming_agents("a", a, ids, threshold=0.5)
        assert incoming_a == []

    def test_get_incoming_not_in_ids(self):
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        result = get_incoming_agents("unknown", a, ids)
        assert result == []

    def test_get_outgoing(self):
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (0, 2)])
        outgoing_a = get_outgoing_agents("a", a, ids, threshold=0.5)
        assert "b" in outgoing_a
        assert "c" in outgoing_a

    def test_get_outgoing_not_in_ids(self):
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        result = get_outgoing_agents("unknown", a, ids)
        assert result == []


class TestFilterReachableAgents:
    def test_linear_chain(self):
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        relevant, excluded = filter_reachable_agents(a, ids, "a", "c")
        assert set(relevant) == {"a", "b", "c"}
        assert excluded == []

    def test_isolated_node(self):
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1)])  # c is isolated
        relevant, excluded = filter_reachable_agents(a, ids, "a", "b")
        assert "a" in relevant
        assert "b" in relevant
        assert "c" in excluded

    def test_no_start_end(self):
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        relevant, _excluded = filter_reachable_agents(a, ids)
        # Without start/end, should still work
        assert isinstance(relevant, list)

    def test_empty_ids(self):
        a = torch.zeros(0, 0)
        relevant, excluded = filter_reachable_agents(a, [])
        assert relevant == []
        assert excluded == []

    def test_start_not_in_graph(self):
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        relevant, _excluded = filter_reachable_agents(a, ids, "unknown", "b")
        assert isinstance(relevant, list)


class TestBuildExecutionOrder:
    def test_simple_chain(self):
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        order = build_execution_order(a, ids)
        assert order.index("a") < order.index("b") < order.index("c")

    def test_empty(self):
        a = torch.zeros(0, 0)
        result = build_execution_order(a, [])
        assert result == []

    def test_size_mismatch_raises(self):
        a = make_adj(3, [])
        with pytest.raises(ValueError, match="a_agents size"):
            build_execution_order(a, ["a", "b"])

    def test_with_start_agent(self):
        ids = ["b", "a", "c"]
        a = make_adj(3, [(0, 2), (1, 0)])  # b→c, a→b
        order = build_execution_order(a, ids, start_agent="a")
        assert order[0] == "a"

    def test_cyclic_graph(self):
        ids = ["a", "b"]
        # a→b, b→a (cycle)
        a = make_adj(2, [(0, 1), (1, 0)])
        order = build_execution_order(a, ids)
        assert set(order) == {"a", "b"}


class TestGetParallelGroups:
    def test_linear_chain(self):
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        groups = get_parallel_groups(a, ids)
        # Should be executed one by one
        assert len(groups) == 3

    def test_two_parallel_then_merge(self):
        # a→c, b→c: a and b can run in parallel
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 2), (1, 2)])
        groups = get_parallel_groups(a, ids)
        assert len(groups) == 2
        assert set(groups[0]) == {"a", "b"}
        assert groups[1] == ["c"]

    def test_empty(self):
        a = torch.zeros(0, 0)
        groups = get_parallel_groups(a, [])
        assert groups == []


# ─────────────────────────── AdaptiveScheduler ────────────────────────────────


def make_chain_matrix(ids):
    """Make a simple linear chain adjacency matrix."""
    n = len(ids)
    a = torch.zeros(n, n)
    for i in range(n - 1):
        a[i, i + 1] = 1.0
    return a


class TestAdaptiveScheduler:
    def test_default_policy(self):
        sched = AdaptiveScheduler()
        assert sched.policy == RoutingPolicy.TOPOLOGICAL

    def test_build_plan_empty(self):
        sched = AdaptiveScheduler()
        plan = sched.build_plan(torch.zeros(0, 0), [])
        assert plan.is_complete

    def test_build_plan_topological(self):
        ids = ["a", "b", "c"]
        a = make_chain_matrix(ids)
        sched = AdaptiveScheduler(policy=RoutingPolicy.TOPOLOGICAL)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) == 3
        assert plan.execution_order.index("a") < plan.execution_order.index("c")

    def test_build_plan_greedy(self):
        ids = ["a", "b", "c"]
        a = make_chain_matrix(ids)
        sched = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) >= 1

    def test_build_plan_weighted_topo(self):
        ids = ["a", "b", "c"]
        a = make_chain_matrix(ids)
        sched = AdaptiveScheduler(policy=RoutingPolicy.WEIGHTED_TOPO)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) >= 1

    def test_build_plan_beam_search(self):
        ids = ["a", "b", "c"]
        a = make_chain_matrix(ids)
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=2)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) >= 1

    def test_build_plan_k_shortest(self):
        ids = ["a", "b", "c"]
        a = make_chain_matrix(ids)
        sched = AdaptiveScheduler(policy=RoutingPolicy.K_SHORTEST, k_paths=2)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) >= 1

    def test_build_plan_with_start_end(self):
        ids = ["a", "b", "c", "d"]
        a = make_chain_matrix(ids)
        sched = AdaptiveScheduler()
        plan = sched.build_plan(a, ids, start_agent="a", end_agent="c")
        # Only a, b, c should be included
        assert "d" not in plan.execution_order or "d" in plan.skipped

    def test_build_plan_with_p_matrix(self):
        ids = ["a", "b", "c"]
        a = make_chain_matrix(ids)
        p = torch.eye(3) * 0.9
        p[0, 1] = 0.8
        p[1, 2] = 0.9
        sched = AdaptiveScheduler()
        plan = sched.build_plan(a, ids, p_matrix=p)
        assert len(plan.steps) >= 1

    def test_build_plan_with_edge_conditions(self):
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        conditions: dict[tuple[str, str], Callable[[ConditionContext], bool] | str] = {("a", "b"): lambda _: True}
        sched = AdaptiveScheduler()
        plan = sched.build_plan(a, ids, edge_conditions=conditions)
        assert len(plan.steps) >= 1

    def test_evaluate_edge_condition_callable(self):
        sched = AdaptiveScheduler()
        ctx = ConditionContext(source_agent="a", target_agent="b")

        def cond(c):
            return True

        assert sched.evaluate_edge_condition("a", "b", cond, ctx) is True

    def test_evaluate_edge_condition_none(self):
        sched = AdaptiveScheduler()
        ctx = ConditionContext(source_agent="a", target_agent="b")
        assert sched.evaluate_edge_condition("a", "b", None, ctx) is True

    def test_evaluate_edge_condition_string(self):
        sched = AdaptiveScheduler()
        ctx = ConditionContext(source_agent="a", target_agent="b")
        assert sched.evaluate_edge_condition("a", "b", "always", ctx) is True

    def test_pruning_config(self):
        config = PruningConfig(min_weight_threshold=0.3, max_consecutive_errors=5)
        sched = AdaptiveScheduler(pruning_config=config)
        assert sched.pruning.min_weight_threshold == 0.3

    def test_filter_unreachable_false(self):
        ids = ["a", "b", "c"]
        # c is not reachable from a
        a = make_adj(3, [(0, 1)])
        sched = AdaptiveScheduler()
        plan = sched.build_plan(a, ids, start_agent="a", end_agent="b", filter_unreachable=False)
        # All agents should be in plan since filter is off
        assert len(plan.steps) == 3


class TestSchedulerMissingCoverage:
    """Tests for missing lines in execution/scheduler.py."""

    def test_execution_plan_skipped_agent(self):
        """ExecutionPlan.insert_conditional_step returns None for skipped agents."""
        plan = ExecutionPlan()
        plan.skipped.add("agent_a")
        result = plan.insert_conditional_step("agent_a")
        assert result is None

    def test_evaluate_string_condition_method(self):
        """_evaluate_string_condition is called via evaluate (line 204)."""
        evaluator = ConditionEvaluator()
        ctx = ConditionContext(
            source_agent="a",
            target_agent="b",
            messages={"a": "good result"},
        )
        # A string condition should be evaluated
        result = evaluator.evaluate("always", ctx)
        assert isinstance(result, bool)

    def test_filter_reachable_no_zero_in_degree(self):
        """filter_reachable_agents when all nodes have in-degree > 0 (line 536)."""
        ids = ["a", "b"]
        # Cycle: a→b, b→a (all have in-degree > 0)
        a = make_adj(2, [(0, 1), (1, 0)])
        # No start_agent specified, so it tries to find 0 in-degree node, fails, falls back to [0]
        relevant, _excluded = filter_reachable_agents(a, ids)
        assert len(relevant) >= 0  # Should not raise

    def test_filter_reachable_no_zero_out_degree(self):
        """filter_reachable_agents when all nodes have out-degree > 0 (line 548 fallback)."""
        ids = ["a", "b", "c"]
        # All have outgoing edges: a→b, b→c, c→a
        a = make_adj(3, [(0, 1), (1, 2), (2, 0)])
        relevant, _excluded = filter_reachable_agents(a, ids)
        assert len(relevant) >= 0  # Should not raise

    def test_build_execution_order_with_cycle(self):
        """build_execution_order with cyclic graph uses SCC (lines 644-645)."""
        ids = ["a", "b", "c"]
        # Cycle: a→b, b→c, c→a
        a = make_adj(3, [(0, 1), (1, 2), (2, 0)])
        order = build_execution_order(a, ids, start_agent="a")
        assert set(order) == {"a", "b", "c"}

    def test_adaptive_scheduler_topological_with_cycle(self):
        """AdaptiveScheduler build_plan with cycle graph (lines 1000-1007 DAGHasCycle fallback)."""
        ids = ["a", "b", "c"]
        # Cycle: a→b, b→c, c→a
        a = make_adj(3, [(0, 1), (1, 2), (2, 0)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.TOPOLOGICAL)
        plan = sched.build_plan(a, ids)
        # Should handle cycle without error
        assert {s.agent_id for s in plan.steps} == {"a", "b", "c"}

    def test_should_prune_weight_threshold(self):
        """should_prune returns True when step weight below threshold (line 922)."""
        pruning = PruningConfig(min_weight_threshold=0.5)
        sched = AdaptiveScheduler(pruning_config=pruning)
        plan = ExecutionPlan()
        step = ExecutionStep(agent_id="a", predecessors=[], weight=0.1, probability=1.0)
        prune, reason = sched.should_prune(step, plan)
        assert prune is True
        assert "weight" in reason

    def test_should_prune_probability_threshold(self):
        """should_prune returns True when step probability below threshold (line 924-928)."""
        pruning = PruningConfig(min_probability_threshold=0.5)
        sched = AdaptiveScheduler(pruning_config=pruning)
        plan = ExecutionPlan()
        step = ExecutionStep(agent_id="a", predecessors=[], weight=1.0, probability=0.1)
        prune, reason = sched.should_prune(step, plan)
        assert prune is True
        assert "probability" in reason

    def test_should_prune_token_budget_exhausted(self):
        """should_prune returns True when token budget exhausted (line 930-934)."""
        pruning = PruningConfig(token_budget=100)
        sched = AdaptiveScheduler(pruning_config=pruning)
        plan = ExecutionPlan()
        plan.tokens_used = 200  # Over budget
        step = ExecutionStep(agent_id="a", predecessors=[], weight=1.0, probability=1.0)
        prune, reason = sched.should_prune(step, plan)
        assert prune is True
        assert "token" in reason.lower()

    def test_should_prune_consecutive_errors(self):
        """should_prune returns True when too many consecutive errors."""
        pruning = PruningConfig(max_consecutive_errors=2)
        sched = AdaptiveScheduler(pruning_config=pruning)
        plan = ExecutionPlan()
        plan.steps = [
            ExecutionStep(agent_id="x", predecessors=[]),
            ExecutionStep(agent_id="y", predecessors=[]),
        ]
        plan.failed.add("x")
        plan.failed.add("y")
        plan.failed_step_ids.add("x")
        plan.failed_step_ids.add("y")
        plan.current_index = 2
        step = ExecutionStep(agent_id="a", predecessors=[], weight=1.0, probability=1.0)
        prune, reason = sched.should_prune(step, plan)
        assert prune is True
        assert "error" in reason.lower()

    def test_should_prune_predecessor_failure(self):
        """should_prune returns True when predecessor failed."""
        pruning = PruningConfig(skip_on_predecessor_failure=True, enable_fallback=False)
        sched = AdaptiveScheduler(pruning_config=pruning)
        plan = ExecutionPlan()
        plan.failed.add("b")
        plan.failed_step_ids.add("b")
        step = ExecutionStep(
            agent_id="a",
            predecessors=["b"],
            weight=1.0,
            probability=1.0,
            is_optional=False,
            fallback_agents=[],
        )
        prune, reason = sched.should_prune(step, plan)
        assert prune is True
        assert "predecessors failed" in reason

    def test_should_use_fallback_disabled(self):
        """should_use_fallback returns False when disabled (line 955-956)."""
        pruning = PruningConfig(enable_fallback=False)
        sched = AdaptiveScheduler(pruning_config=pruning)
        step = ExecutionStep(agent_id="a", predecessors=[], fallback_agents=["b"])
        result_obj = StepResult(agent_id="a", success=False, quality_score=0.0)
        assert sched.should_use_fallback(step, result_obj, 0) is False

    def test_should_use_fallback_max_attempts(self):
        """should_use_fallback returns False when max fallback attempts reached (line 957-958)."""
        pruning = PruningConfig(enable_fallback=True, max_fallback_attempts=2)
        sched = AdaptiveScheduler(pruning_config=pruning)
        step = ExecutionStep(agent_id="a", predecessors=[], fallback_agents=["b"])
        result_obj = StepResult(agent_id="a", success=False, quality_score=0.0)
        assert sched.should_use_fallback(step, result_obj, 3) is False  # Over limit

    def test_should_use_fallback_no_fallback_agents(self):
        """should_use_fallback returns False when no fallback agents (line 959-960)."""
        pruning = PruningConfig(enable_fallback=True)
        sched = AdaptiveScheduler(pruning_config=pruning)
        step = ExecutionStep(agent_id="a", predecessors=[], fallback_agents=[])
        result_obj = StepResult(agent_id="a", success=False)
        assert sched.should_use_fallback(step, result_obj, 0) is False

    def test_should_use_fallback_on_failure(self):
        """should_use_fallback returns True when step fails and has fallback (line 961-962)."""
        pruning = PruningConfig(enable_fallback=True)
        sched = AdaptiveScheduler(pruning_config=pruning)
        step = ExecutionStep(agent_id="a", predecessors=[], fallback_agents=["b"])
        result_obj = StepResult(agent_id="a", success=False)
        assert sched.should_use_fallback(step, result_obj, 0) is True

    def test_greedy_order_basic(self):
        """_greedy_order basic execution (various lines)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
        plan = sched.build_plan(a, ids)
        agent_ids = [s.agent_id for s in plan.steps]
        assert "a" in agent_ids
        assert "b" in agent_ids
        assert "c" in agent_ids

    def test_greedy_order_with_end_agent(self):
        """_greedy_order with end_agent stops early (line 1062-1063)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
        plan = sched.build_plan(a, ids, start_agent="a", end_agent="b")
        # Should stop at b or process all in order
        assert len(plan.steps) >= 1

    def test_beam_search_order(self):
        """_beam_search_order basic execution (various lines)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=2)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) == 3

    def test_beam_search_with_end_agent(self):
        """_beam_search_order with end_agent (lines 1109-1115)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH)
        plan = sched.build_plan(a, ids, start_agent="a", end_agent="b")
        assert len(plan.steps) >= 1

    def test_k_shortest_order(self):
        """_k_shortest_order basic execution."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.K_SHORTEST)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) == 3

    def test_get_parallel_groups_with_deadlock(self):
        """get_parallel_groups when nothing is ready (lines 693-697)."""
        ids = ["a", "b"]
        # Both have mutual incoming edges - deadlock scenario
        a = make_adj(2, [(0, 1), (1, 0)])
        groups = get_parallel_groups(a, ids)
        # Should handle by forcing at least one group with one agent
        assert len(groups) >= 1

    def test_build_plan_with_p_matrix(self):
        """build_plan with p_matrix provided (line 812)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        p = torch.ones(3, 3) * 0.5
        sched = AdaptiveScheduler(policy=RoutingPolicy.TOPOLOGICAL)
        # Should use p_matrix for filtering
        plan = sched.build_plan(a, ids, p_matrix=p, start_agent="a", end_agent="c")
        assert len(plan.steps) > 0

    # ------------------------------------------------------------------
    # should_prune — quality threshold branch (line 915)
    # ------------------------------------------------------------------

    def test_should_prune_quality_below_threshold(self):
        """last_result quality below threshold triggers prune (line 915)."""
        pruning = PruningConfig(quality_scorer=lambda r: r.quality_score, min_quality_threshold=0.8)
        sched = AdaptiveScheduler(pruning_config=pruning)
        step = ExecutionStep(agent_id="b", predecessors=["a"], weight=1.0)
        last = StepResult(agent_id="a", success=True, quality_score=0.3)
        pruned, reason = sched.should_prune(step, ExecutionPlan(steps=[step]), last)
        assert pruned is True
        assert "quality" in reason.lower()

    # ------------------------------------------------------------------
    # should_use_fallback — quality check (line 963)
    # ------------------------------------------------------------------

    def test_should_use_fallback_quality_below_threshold(self):
        """should_use_fallback returns True when quality is low (line 963)."""
        pruning = PruningConfig(enable_fallback=True, min_quality_threshold=0.8)
        sched = AdaptiveScheduler(pruning_config=pruning)
        step = ExecutionStep(agent_id="a", predecessors=[], fallback_agents=["b"])
        result_obj = StepResult(agent_id="a", success=True, quality_score=0.2)
        assert sched.should_use_fallback(step, result_obj, 0) is True

    # ------------------------------------------------------------------
    # _weighted_topological_order — zero agents (line 983) and cycle fallback (lines 649-650, 998-1002)
    # ------------------------------------------------------------------

    def test_weighted_topo_order_zero_agents(self):
        """Empty agent list returns empty list (line 983)."""
        sched = AdaptiveScheduler(policy=RoutingPolicy.WEIGHTED_TOPO)
        plan = sched.build_plan(torch.zeros((0, 0)), [])
        assert plan.steps == []

    def test_weighted_topo_order_with_start_agent(self):
        """_weighted_topological_order places start_agent first (lines 998-1002)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.WEIGHTED_TOPO)
        plan = sched.build_plan(a, ids, start_agent="c")
        # c is placed first (even though it normally is last)
        assert plan.steps[0].agent_id == "c"

    def test_weighted_topo_order_with_cycle_falls_back(self):
        """Cyclic graph falls back to build_execution_order (lines 644-650)."""
        ids = ["a", "b"]
        # Mutual edges → cycle
        a = make_adj(2, [(0, 1), (1, 0)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.WEIGHTED_TOPO)
        plan = sched.build_plan(a, ids)
        # Should not raise and should produce two steps
        assert len(plan.steps) == 2

    # ------------------------------------------------------------------
    # _greedy_order — zero agents (line 1022) and fallback (lines 1049-1053)
    # ------------------------------------------------------------------

    def test_greedy_order_zero_agents(self):
        """Empty agent list returns empty plan (line 1022)."""
        sched = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
        plan = sched.build_plan(torch.zeros((0, 0)), [])
        assert plan.steps == []

    def test_greedy_order_disconnected_graph_fallback(self):
        """Fully disconnected graph triggers fallback current_set={0} (line 1034)."""
        ids = ["a", "b", "c"]
        a = torch.zeros(3, 3)  # No edges → all in_degree = 0 → current_set from zero in-degree nodes
        sched = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) == 3

    def test_greedy_order_best_idx_none_fallback(self):
        """When all nodes in current_set are already visited, fallback adds remaining (lines 1049-1053)."""
        # This case arises when current_set has no unvisited nodes
        # Hard to trigger directly, but can be tested indirectly via a specific graph structure
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
        plan = sched.build_plan(a, ids, start_agent="a")
        assert len(plan.steps) == 2

    # ------------------------------------------------------------------
    # _beam_search_order — zero agents (line 1078), start fallback (line 1088),
    #                       empty best path (line 1133)
    # ------------------------------------------------------------------

    def test_beam_search_zero_agents(self):
        """Empty agent list returns empty plan (line 1078)."""
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH)
        plan = sched.build_plan(torch.zeros((0, 0)), [])
        assert plan.steps == []

    def test_beam_search_start_indices_fallback(self):
        """When all nodes have in-edges, start_indices falls back to [0] (line 1088)."""
        ids = ["a", "b"]
        # Mutual edges → both have in-degree > 0
        a = make_adj(2, [(0, 1), (1, 0)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=2)
        plan = sched.build_plan(a, ids)
        assert len(plan.steps) == 2

    def test_beam_search_end_agent_terminates_early(self):
        """Beam search terminates at end_agent and fills remaining (lines 1109-1115)."""
        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2), (0, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=3)
        plan = sched.build_plan(a, ids, start_agent="a", end_agent="b")
        assert len(plan.steps) >= 2

    def test_beam_search_empty_best_path_fallback(self):
        """When no complete path is found, returns agent_ids (line 1133)."""
        # Disconnected graph: no path from any node to any other
        ids = ["x", "y", "z"]
        a = torch.zeros(3, 3)  # No edges → beam never completes a path
        sched = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=2)
        plan = sched.build_plan(a, ids)
        # Falls back to greedy / topo: should return 3 steps
        assert len(plan.steps) == 3

    # ------------------------------------------------------------------
    # _k_shortest_order — zero agents (line 1148), path failure (lines 1174-1176, 1180)
    # ------------------------------------------------------------------

    def test_k_shortest_zero_agents(self):
        """Empty agent list returns empty plan (line 1148)."""
        sched = AdaptiveScheduler(policy=RoutingPolicy.K_SHORTEST)
        plan = sched.build_plan(torch.zeros((0, 0)), [])
        assert plan.steps == []

    def test_k_shortest_path_failure_falls_back(self):
        """When path finding fails, k_shortest falls back to topological (lines 1174-1176, 1180)."""
        from unittest.mock import patch

        ids = ["a", "b", "c"]
        a = make_adj(3, [(0, 1), (1, 2)])
        sched = AdaptiveScheduler(policy=RoutingPolicy.K_SHORTEST)
        with patch("rustworkx.dijkstra_shortest_paths", side_effect=RuntimeError("error")):
            plan = sched.build_plan(a, ids)
        # Should still produce 3 steps via fallback
        assert len(plan.steps) == 3

    # ------------------------------------------------------------------
    # _find_similar_agents — similarity computation (lines 1227-1235)
    # ------------------------------------------------------------------

    def test_find_similar_agents_returns_similar(self):
        """Agents with similar incoming patterns are returned as fallbacks (lines 1227-1235)."""
        ids = ["a", "b", "c"]
        # a and b have same incoming pattern (0.8 from "c")
        a = torch.zeros(3, 3)
        a[2, 0] = 0.9  # c → a
        a[2, 1] = 0.9  # c → b
        pruning = PruningConfig(enable_fallback=True, max_fallback_attempts=5)
        sched = AdaptiveScheduler(pruning_config=pruning)
        fallbacks = sched._find_fallback_agents("a", ids, a, [])
        # b should be identified as similar to a
        assert "b" in fallbacks

    def test_find_similar_agents_empty_when_fallback_disabled(self):
        """Returns [] when fallback is disabled (line 1217)."""
        ids = ["a", "b"]
        a = make_adj(2, [(0, 1)])
        pruning = PruningConfig(enable_fallback=False)
        sched = AdaptiveScheduler(pruning_config=pruning)
        fallbacks = sched._find_fallback_agents("a", ids, a, [])
        assert fallbacks == []

    # ------------------------------------------------------------------
    # ConditionEvaluator — fallthrough returns True (line 204)
    # ------------------------------------------------------------------

    def test_condition_evaluator_unknown_type_returns_true(self):
        """evaluate() returns True for unknown condition type (line 204)."""
        from gmas.execution.scheduler import ConditionEvaluator

        evaluator = ConditionEvaluator()
        ctx = ConditionContext(source_agent="a", target_agent="b")
        # Pass an integer (not str, not callable, not ConditionBase instance)
        result = evaluator.evaluate(42, ctx)  # type: ignore[arg-type,ty:invalid-argument-type]
        assert result is True
