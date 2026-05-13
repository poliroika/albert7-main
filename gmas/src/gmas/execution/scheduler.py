"""
Agent execution scheduler based on graph topology.

Supports both simple topological order and adaptive policies
that account for edge weights, pruning, and re-planning.
"""

import heapq
import logging
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from typing import Any, NamedTuple

import rustworkx as rx
import torch
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
#: Small additive constant to prevent division by zero when computing edge weights.
_EPSILON: float = 1e-6
#: Default weight threshold for determining whether an edge is present.
_DEFAULT_WEIGHT_THRESHOLD: float = 0.5
#: Default similarity threshold for semantic comparisons.
_DEFAULT_SIMILARITY_THRESHOLD: float = 0.5

__all__ = [
    "AdaptiveScheduler",
    # Conditional routing
    "ConditionContext",
    "ConditionEvaluator",
    "EdgeCondition",
    "ExecutionPlan",
    "ExecutionStep",
    "PruningConfig",
    "RoutingPolicy",
    "StepResult",
    "build_execution_order",
    "extract_agent_adjacency",
    "filter_reachable_agents",
    "get_incoming_agents",
    "get_outgoing_agents",
    "get_parallel_groups",
]


# =============================================================================
# CONDITIONAL ROUTING
# =============================================================================


class ConditionContext(BaseModel):
    """
    Context for evaluating routing conditions.

    Passed to condition functions to decide which route to take.

    Attributes:
        source_agent: ID of the source agent of an edge.
        target_agent: ID of the target agent of an edge.
        messages: Dictionary of agent responses {agent_id: response}.
        step_results: Step execution results {agent_id: StepResult}.
        state: Arbitrary user-defined state.
        query: Current query/task.
        metadata: Additional metadata.

    Example:
        def my_condition(ctx: ConditionContext) -> bool:
            # Proceed to reviewer only if solver produced a response
            if "solver" in ctx.messages:
                return "error" not in ctx.messages["solver"].lower()
            return False

        builder.add_conditional_edge("solver", "reviewer", condition=my_condition)

    """

    source_agent: str
    target_agent: str
    messages: dict[str, str] = Field(default_factory=dict)
    step_results: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    query: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages_history: dict[str, list[str]] = Field(default_factory=dict)
    step_results_history: dict[str, list[Any]] = Field(default_factory=dict)

    def get_last_response(self) -> str | None:
        """Return the last response from source_agent."""
        return self.messages.get(self.source_agent)

    def get_response_history(self, agent_id: str | None = None) -> list[str]:
        """
        Return all historical responses for an agent (oldest first).

        Defaults to *source_agent* when *agent_id* is ``None``.
        """
        return self.messages_history.get(agent_id or self.source_agent, [])

    def source_succeeded(self) -> bool:
        """Return whether source_agent completed successfully."""
        result = self.step_results.get(self.source_agent)
        if result is None:
            return self.source_agent in self.messages
        return getattr(result, "success", True)

    def has_keyword(self, keyword: str, *, in_source: bool = True) -> bool:
        """Check whether a keyword is present in the response."""
        agent = self.source_agent if in_source else self.target_agent
        msg = self.messages.get(agent, "")
        return keyword.lower() in msg.lower()

    def get_state_value(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from the state dictionary."""
        return self.state.get(key, default)


# Type alias for condition functions
EdgeCondition = Callable[[ConditionContext], bool]


class ConditionEvaluator:
    """
    Evaluator for conditions attached to graph edges.

    Supports:
    - Callable conditions (functions)
    - String conditions (simple expressions)
    - Composition (AND/OR)

    Example:
        evaluator = ConditionEvaluator()

        # Register named conditions
        evaluator.register("has_error", lambda ctx: "error" in ctx.get_last_response() or "")
        evaluator.register("high_quality", lambda ctx: ctx.step_results.get(ctx.source_agent, {}).quality_score > 0.8)

        # Use by name
        if evaluator.evaluate("has_error", context):
            ...

    """

    def __init__(self) -> None:
        self._named_conditions: dict[str, EdgeCondition] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register built-in conditions."""
        # Always True/False
        self._named_conditions["always"] = lambda _ctx: True
        self._named_conditions["never"] = lambda _ctx: False

        # Source success checks
        self._named_conditions["source_success"] = lambda ctx: ctx.source_succeeded()
        self._named_conditions["source_failed"] = lambda ctx: not ctx.source_succeeded()

        # Response presence check
        self._named_conditions["has_response"] = lambda ctx: ctx.get_last_response() is not None

    def register(self, name: str, condition: EdgeCondition) -> None:
        """Register a named condition."""
        self._named_conditions[name] = condition

    def unregister(self, name: str) -> bool:
        """Remove a named condition."""
        if name in self._named_conditions:
            del self._named_conditions[name]
            return True
        return False

    def get(self, name: str) -> EdgeCondition | None:
        """Retrieve a condition by name."""
        return self._named_conditions.get(name)

    def evaluate(
        self,
        condition: EdgeCondition | str | None,
        context: ConditionContext,
    ) -> bool:
        """
        Evaluate a condition.

        Args:
            condition: Callable, condition name, or None (= always True).
            context: Context used for evaluation.

        Returns:
            True if the condition holds, False otherwise.

        """
        if condition is None:
            return True

        if isinstance(condition, str):
            # Try to look up a named condition
            named = self._named_conditions.get(condition)
            if named is not None:
                return self.evaluate(named, context)

            # Fall back to simple string expression evaluation
            return self._evaluate_string_condition(condition, context)

        if callable(condition):
            try:
                return bool(condition(context))
            except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                return False
        return True

    def _evaluate_string_condition(self, expr: str, context: ConditionContext) -> bool:
        """
        Evaluate a simple string condition.

        Supports:
        - "contains:keyword" — check for a word in the response
        - "state:key=value" — check a value in the state dict
        - "not:condition" — negation
        """
        expr = expr.strip()

        # Negation
        if expr.startswith("not:"):
            inner = expr[4:]
            return not self._evaluate_string_condition(inner, context)

        # Content check
        if expr.startswith("contains:"):
            keyword = expr[9:]
            return context.has_keyword(keyword)

        # State check
        if expr.startswith("state:"):
            kv = expr[6:]
            if "=" in kv:
                key, value = kv.split("=", 1)
                return str(context.get_state_value(key.strip())) == value.strip()
            return context.get_state_value(kv.strip()) is not None

        # Default — look up by name
        named = self._named_conditions.get(expr)
        if named is not None:
            return self.evaluate(named, context)

        return True

    def compose_and(self, *conditions: EdgeCondition | str) -> EdgeCondition:
        """Create a composed AND condition from multiple conditions."""

        def composed(ctx: ConditionContext) -> bool:
            return all(self.evaluate(c, ctx) for c in conditions)

        return composed

    def compose_or(self, *conditions: EdgeCondition | str) -> EdgeCondition:
        """Create a composed OR condition from multiple conditions."""

        def composed(ctx: ConditionContext) -> bool:
            return any(self.evaluate(c, ctx) for c in conditions)

        return composed


# Global evaluator instance (can be replaced)
_default_evaluator = ConditionEvaluator()


class RoutingPolicy(StrEnum):
    TOPOLOGICAL = "topological"
    WEIGHTED_TOPO = "weighted_topo"
    GREEDY = "greedy"
    BEAM_SEARCH = "beam_search"
    K_SHORTEST = "k_shortest"


class PruningConfig(BaseModel):
    """Pruning and fallback parameters for the scheduler."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    min_weight_threshold: float = 0.1
    min_probability_threshold: float = 0.05
    max_consecutive_errors: int = 3
    skip_on_predecessor_failure: bool = True
    token_budget: int | None = None
    quality_scorer: Callable[[str], float] | None = None
    min_quality_threshold: float = 0.3
    enable_fallback: bool = True
    max_fallback_attempts: int = 2


class StepResult(NamedTuple):
    """Step execution result used by the scheduler."""

    agent_id: str
    success: bool
    response: str | None = None
    tokens_used: int = 0
    quality_score: float = 1.0
    error: str | None = None
    fallback_used: bool = False


class ExecutionStep(BaseModel):
    """Plan step: agent, predecessors, and weight/probability metadata."""

    agent_id: str
    predecessors: list[str]
    step_id: str = ""
    dependency_ids: list[str] = Field(default_factory=list)
    weight: float = 1.0
    probability: float = 1.0
    fallback_agents: list[str] = Field(default_factory=list)
    is_optional: bool = False
    priority: int = 0

    def model_post_init(self, __context: Any, /) -> None:
        if not self.step_id:
            self.step_id = self.agent_id
        if not self.dependency_ids:
            self.dependency_ids = list(self.predecessors)

    def add_dependency(self, predecessor_agent_id: str, dependency_step_id: str) -> None:
        """Merge one more incoming dependency into the step."""
        if predecessor_agent_id and predecessor_agent_id not in self.predecessors:
            self.predecessors.append(predecessor_agent_id)
        if dependency_step_id and dependency_step_id not in self.dependency_ids:
            self.dependency_ids.append(dependency_step_id)


class ExecutionPlan(BaseModel):
    """
    Ordered sequence of steps with execution states and token accounting.

    Supports conditional loops: agents may execute more than once when
    conditional edges fire (e.g. review_failed -> mathematician).
    """

    steps: list[ExecutionStep] = Field(default_factory=list)
    completed: set[str] = Field(default_factory=set)
    failed: set[str] = Field(default_factory=set)
    skipped: set[str] = Field(default_factory=set)
    completed_step_ids: set[str] = Field(default_factory=set)
    failed_step_ids: set[str] = Field(default_factory=set)
    skipped_step_ids: set[str] = Field(default_factory=set)
    tokens_used: int = 0
    current_index: int = 0

    # Support for conditional loops
    iteration_count: dict[str, int] = Field(default_factory=dict)
    max_iterations: int = Field(default=5)  # Guard against infinite loops
    end_agent: str | None = Field(default=None)  # Terminal agent (used to signal completion)

    # Agents skipped because their conditions were not satisfied
    condition_skipped: set[str] = Field(default_factory=set)
    condition_skipped_step_ids: set[str] = Field(default_factory=set)

    def model_post_init(self, __context: Any, /) -> None:
        self._normalize_steps()
        self._advance_to_next_unresolved()

    @property
    def remaining_steps(self) -> list[ExecutionStep]:
        """Steps not yet completed or skipped, starting from current_index."""
        return [
            step
            for step in self.steps[self.current_index :]
            if not self.is_step_resolved(step)
            and step.agent_id not in self.skipped
            and not self.is_condition_skipped(step)
        ]

    @property
    def is_complete(self) -> bool:
        """True if the end of the step list has been reached."""
        return self.current_index >= len(self.steps)

    @property
    def execution_order(self) -> list[str]:
        """Current agent ordering in the plan."""
        return [step.agent_id for step in self.steps]

    def is_step_resolved(self, step: ExecutionStep) -> bool:
        """Return True if the specific step instance has already been resolved."""
        return (
            step.step_id in self.completed_step_ids
            or step.step_id in self.failed_step_ids
            or step.step_id in self.skipped_step_ids
        )

    def is_condition_skipped(self, step: ExecutionStep) -> bool:
        """Return True if the specific step is currently disabled by a condition."""
        return step.step_id in self.condition_skipped_step_ids or step.agent_id in self.condition_skipped

    def find_pending_step(self, agent_id: str) -> ExecutionStep | None:
        """Return the first unresolved pending step for the given agent."""
        for step in self.steps[self.current_index :]:
            if step.agent_id == agent_id and not self.is_step_resolved(step):
                return step
        return None

    def get_latest_step(self, agent_id: str) -> ExecutionStep | None:
        """Return the latest step instance known for an agent, resolved or pending."""
        for step in reversed(self.steps):
            if step.agent_id == agent_id:
                return step
        return None

    def apply_condition_skip(self, agent_id: str) -> None:
        """Condition-skip all currently pending instances of an agent."""
        self.condition_skipped.add(agent_id)
        for step in self.steps[self.current_index :]:
            if step.agent_id == agent_id and not self.is_step_resolved(step):
                self.condition_skipped_step_ids.add(step.step_id)

    def clear_condition_skip(self, agent_id: str) -> None:
        """Remove condition skip from all currently pending instances of an agent."""
        self.condition_skipped.discard(agent_id)
        for step in self.steps[self.current_index :]:
            if step.agent_id == agent_id:
                self.condition_skipped_step_ids.discard(step.step_id)

    def mark_completed(self, step_or_agent: ExecutionStep | str, tokens: int = 0) -> None:
        """
        Mark a step as completed and add its token count.

        Adds the agent to the completed set and increments the iteration counter.
        Loops are handled via insert_conditional_step, which appends a new step
        at the end of the plan — the completed set does not block re-execution.
        """
        step = self._resolve_step_ref(step_or_agent)
        if step is None:
            return
        self.completed.add(step.agent_id)
        self.completed_step_ids.add(step.step_id)
        self.condition_skipped_step_ids.discard(step.step_id)
        self.iteration_count[step.agent_id] = self.iteration_count.get(step.agent_id, 0) + 1
        self.tokens_used += tokens
        self._advance_to_next_unresolved()

    def mark_failed(self, step_or_agent: ExecutionStep | str) -> None:
        """Mark a step as failed."""
        step = self._resolve_step_ref(step_or_agent)
        if step is None:
            return
        self.failed.add(step.agent_id)
        self.failed_step_ids.add(step.step_id)
        self.condition_skipped_step_ids.discard(step.step_id)
        self._advance_to_next_unresolved()

    def mark_skipped(self, step_or_agent: ExecutionStep | str) -> None:
        """Mark a step as skipped."""
        step = self._resolve_step_ref(step_or_agent)
        if step is None:
            return
        self.skipped.add(step.agent_id)
        self.skipped_step_ids.add(step.step_id)
        self.condition_skipped_step_ids.discard(step.step_id)
        self._advance_to_next_unresolved()

    def advance(self) -> None:
        """Advance current_index to the next step."""
        self.current_index += 1
        self._advance_to_next_unresolved()

    def get_current_step(self) -> ExecutionStep | None:
        """Return the current step, or None if the plan is complete."""
        self._advance_to_next_unresolved()
        if self.current_index < len(self.steps):
            return self.steps[self.current_index]
        return None

    def get_step_index(self, step: ExecutionStep) -> int:
        """Return the current position of a concrete step instance in the plan."""
        for idx, candidate in enumerate(self.steps):
            if candidate.step_id == step.step_id:
                return idx
        return max(self.current_index - 1, 0)

    def insert_fallback(self, fallback_agent_id: str, after_index: int) -> None:
        """Insert a fallback agent step after the specified index."""
        if fallback_agent_id in self.skipped:
            return

        fallback_step = ExecutionStep(
            agent_id=fallback_agent_id,
            predecessors=[],
            step_id=self._next_step_id(fallback_agent_id),
            is_optional=True,
            priority=-1,
        )
        self.steps.insert(after_index + 1, fallback_step)

    def insert_conditional_step(
        self,
        agent_id: str,
        predecessors: list[str] | None = None,
        *,
        dependency_step_ids: list[str] | None = None,
    ) -> ExecutionStep | None:
        """
        Insert or update an agent step for a conditional transition (loop).

        Args:
            agent_id: ID of the agent to add.
            predecessors: Predecessors (typically the currently completed agent).
            dependency_step_ids: Specific predecessor step IDs that must complete.

        Returns:
            The pending step instance, or None if the iteration limit was exceeded.

        """
        existing = self.find_pending_step(agent_id)
        if existing is not None:
            for predecessor_agent_id, dependency_step_id in zip(
                predecessors or [],
                dependency_step_ids or predecessors or [],
                strict=False,
            ):
                existing.add_dependency(predecessor_agent_id, dependency_step_id)
            return existing

        current_iterations = self.iteration_count.get(agent_id, 0)
        if current_iterations >= self.max_iterations:
            logger.warning(
                "Loop cap reached for %s: %d/%d iterations used. "
                "Increase max_loop_iterations in RunnerConfig to allow more.",
                agent_id,
                current_iterations,
                self.max_iterations,
            )
            return None

        if agent_id in self.skipped:
            return None

        step = ExecutionStep(
            agent_id=agent_id,
            predecessors=predecessors or [],
            step_id=self._next_step_id(agent_id),
            dependency_ids=list(dependency_step_ids or predecessors or []),
            is_optional=False,
            priority=0,
        )
        if agent_id in self.condition_skipped:
            self.condition_skipped_step_ids.add(step.step_id)
        self.steps.append(step)
        return step

    def can_iterate(self, agent_id: str) -> bool:
        """Check whether the agent can be executed again."""
        return self.iteration_count.get(agent_id, 0) < self.max_iterations

    def _resolve_step_ref(self, step_or_agent: ExecutionStep | str) -> ExecutionStep | None:
        """Resolve either a step instance or an agent ID to the current pending step."""
        if isinstance(step_or_agent, ExecutionStep):
            return step_or_agent
        return self.find_pending_step(step_or_agent)

    def _normalize_steps(self) -> None:
        """Ensure every step instance has a unique step_id and concrete dependency IDs."""
        seen_ids: set[str] = set()
        per_agent_counts: dict[str, int] = {}

        for step in self.steps:
            per_agent_counts[step.agent_id] = per_agent_counts.get(step.agent_id, 0) + 1
            if not step.step_id or step.step_id in seen_ids:
                step.step_id = self._format_step_id(step.agent_id, per_agent_counts[step.agent_id])
            seen_ids.add(step.step_id)
            if not step.dependency_ids:
                step.dependency_ids = list(step.predecessors)

    def _next_step_id(self, agent_id: str) -> str:
        """Generate a unique step ID for a newly appended step instance."""
        existing = sum(1 for step in self.steps if step.agent_id == agent_id)
        return self._format_step_id(agent_id, existing + 1)

    @staticmethod
    def _format_step_id(agent_id: str, ordinal: int) -> str:
        return agent_id if ordinal <= 1 else f"{agent_id}__{ordinal}"

    def _advance_to_next_unresolved(self) -> None:
        """Move the cursor to the earliest unresolved step instance."""
        while self.current_index < len(self.steps):
            step = self.steps[self.current_index]
            if not self.is_step_resolved(step):
                break
            self.current_index += 1


def extract_agent_adjacency(
    a_com: torch.Tensor,
    task_idx: int,
) -> torch.Tensor:
    """Remove the task row/column from the agent adjacency matrix."""
    n_nodes = a_com.shape[0]
    mask = torch.ones(n_nodes, dtype=torch.bool)
    mask[task_idx] = False
    return a_com[mask][:, mask]


def get_incoming_agents(
    agent_id: str,
    a_agents: torch.Tensor,
    agent_ids: list[str],
    threshold: float = _DEFAULT_WEIGHT_THRESHOLD,
) -> list[str]:
    """Return IDs of predecessor agents where matrix weight exceeds threshold."""
    if agent_id not in agent_ids:
        return []

    agent_idx = agent_ids.index(agent_id)
    incoming: list[str] = []

    for i, aid in enumerate(agent_ids):
        if a_agents[i, agent_idx].item() > threshold:
            incoming.append(aid)

    return incoming


def get_outgoing_agents(
    agent_id: str,
    a_agents: torch.Tensor,
    agent_ids: list[str],
    threshold: float = _DEFAULT_WEIGHT_THRESHOLD,
) -> list[str]:
    """Return IDs of successor agents where matrix weight exceeds threshold."""
    if agent_id not in agent_ids:
        return []

    agent_idx = agent_ids.index(agent_id)
    outgoing: list[str] = []

    for j, aid in enumerate(agent_ids):
        if a_agents[agent_idx, j].item() > threshold:
            outgoing.append(aid)

    return outgoing


def filter_reachable_agents(  # noqa: PLR0912
    a_agents: torch.Tensor,
    agent_ids: list[str],
    start_agent: str | None = None,
    end_agent: str | None = None,
    threshold: float = _DEFAULT_WEIGHT_THRESHOLD,
) -> tuple[list[str], list[str]]:
    """
    Filter agents to retain only those on a path from start to end.

    This is a key optimization function: it excludes isolated nodes and
    subgraphs that do not affect the result, thereby saving tokens and LLM calls.

    Args:
        a_agents: Agent adjacency matrix.
        agent_ids: List of all agent IDs.
        start_agent: ID of the start agent (None = first agent with no incoming edges).
        end_agent: ID of the end agent (None = last agent with no outgoing edges).
        threshold: Minimum edge weight.

    Returns:
        Tuple of:
        - List of relevant agent_ids (on the path from start to end)
        - List of excluded agent_ids (isolated nodes)

    Example:
        relevant, excluded = filter_reachable_agents(
            a_agents, agent_ids,
            start_agent="input",
            end_agent="output"
        )
        # relevant contains only agents on the path input->output
        # excluded contains agents not needed to produce the result

    """
    num_agents = len(agent_ids)
    if num_agents == 0:
        return [], []

    # Determine the start agent
    effective_start = start_agent
    if effective_start is None:
        # First agent with no incoming edges
        in_degree = torch.sum((a_agents > threshold).int(), dim=0)
        for i, aid in enumerate(agent_ids):
            if in_degree[i].item() == 0:
                effective_start = aid
                break
        if effective_start is None:
            effective_start = agent_ids[0]

    # Determine the end agent
    effective_end = end_agent
    if effective_end is None:
        # Last agent with no outgoing edges
        out_degree = torch.sum((a_agents > threshold).int(), dim=1)
        for i in range(num_agents - 1, -1, -1):
            if out_degree[i].item() == 0:
                effective_end = agent_ids[i]
                break
        if effective_end is None:
            effective_end = agent_ids[-1]

    # Pre-compute index map for O(1) lookups
    agent_idx_map = {aid: i for i, aid in enumerate(agent_ids)}

    # Forward BFS from start — find all nodes reachable from start
    reachable_from_start: set[str] = set()
    if effective_start in agent_ids:
        bfs_queue: deque[str] = deque([effective_start])
        reachable_from_start.add(effective_start)
        while bfs_queue:
            current = bfs_queue.popleft()
            current_idx = agent_idx_map[current]
            for j, aid in enumerate(agent_ids):
                if aid not in reachable_from_start and a_agents[current_idx, j].item() > threshold:
                    reachable_from_start.add(aid)
                    bfs_queue.append(aid)

    # Backward BFS from end — find all nodes from which end is reachable
    reaching_end: set[str] = set()
    if effective_end in agent_ids:
        bfs_queue = deque([effective_end])
        reaching_end.add(effective_end)
        while bfs_queue:
            current = bfs_queue.popleft()
            current_idx = agent_idx_map[current]
            for i, aid in enumerate(agent_ids):
                if aid not in reaching_end and a_agents[i, current_idx].item() > threshold:
                    reaching_end.add(aid)
                    bfs_queue.append(aid)

    # Intersection — nodes on paths from start to end
    relevant_set = reachable_from_start & reaching_end
    relevant = [aid for aid in agent_ids if aid in relevant_set]
    excluded = [aid for aid in agent_ids if aid not in relevant_set]

    return relevant, excluded


def build_execution_order(  # noqa: PLR0912
    a_agents: torch.Tensor,
    agent_ids: list[str],
    fallback_order: list[str] | None = None,
    threshold: float = _DEFAULT_WEIGHT_THRESHOLD,
    start_agent: str | None = None,
) -> list[str]:
    """
    Build an execution order: topological sort with SCC + fallback ordering.

    Args:
        a_agents: Edge weight matrix.
        agent_ids: List of agent IDs.
        fallback_order: Order used for sorting agents within an SCC.
        threshold: Weight threshold for including an edge.
        start_agent: Start agent (will be placed first in the result).

    """
    num_agents = a_agents.shape[0]
    if num_agents != len(agent_ids):
        msg = f"a_agents size {num_agents} != agent_ids length {len(agent_ids)}"
        raise ValueError(msg)

    if num_agents == 0:
        return []

    graph = rx.PyDiGraph()
    node_indices = [graph.add_node(aid) for aid in agent_ids]

    for i in range(num_agents):
        for j in range(num_agents):
            if i != j and a_agents[i, j] > threshold:
                graph.add_edge(node_indices[i], node_indices[j], None)

    try:
        topo_order = rx.topological_sort(graph)
        return [agent_ids[node_indices.index(idx)] for idx in topo_order]
    except rx.DAGHasCycle:
        pass

    sccs = list(rx.strongly_connected_components(graph))

    scc_map: dict[int, int] = {}
    for scc_idx, scc in enumerate(sccs):
        for node_idx in scc:
            scc_map[node_idx] = scc_idx

    scc_graph = rx.PyDiGraph()
    scc_nodes = [scc_graph.add_node(i) for i in range(len(sccs))]
    scc_edges_seen: set[tuple[int, int]] = set()

    for i in range(num_agents):
        for j in range(num_agents):
            if i != j and a_agents[i, j] > threshold:
                src_scc = scc_map[node_indices[i]]
                dst_scc = scc_map[node_indices[j]]
                if src_scc != dst_scc and (src_scc, dst_scc) not in scc_edges_seen:
                    scc_graph.add_edge(scc_nodes[src_scc], scc_nodes[dst_scc], None)
                    scc_edges_seen.add((src_scc, dst_scc))

    try:
        scc_order = rx.topological_sort(scc_graph)
    except rx.DAGHasCycle:
        scc_order = list(range(len(sccs)))

    fallback = fallback_order or agent_ids
    fallback_rank = {aid: i for i, aid in enumerate(fallback)}

    result: list[str] = []
    for scc_idx in scc_order:
        scc = sccs[scc_idx]
        scc_agents: list[str] = []
        for node_idx in scc:
            agent_idx = node_indices.index(node_idx)
            scc_agents.append(agent_ids[agent_idx])

        # Sort by fallback_rank, but start_agent is always first within its SCC
        def sort_key(a: str) -> tuple[int, int]:
            is_start = 0 if a == start_agent else 1
            return (is_start, fallback_rank.get(a, len(fallback)))

        scc_agents.sort(key=sort_key)
        result.extend(scc_agents)

    return result


def get_parallel_groups(
    a_agents: torch.Tensor,
    agent_ids: list[str],
    threshold: float = _DEFAULT_WEIGHT_THRESHOLD,
) -> list[list[str]]:
    """Partition nodes into groups that can be executed in parallel."""
    num_agents = a_agents.shape[0]
    if num_agents == 0:
        return []

    in_degree = torch.sum((a_agents > threshold).int(), dim=0)
    remaining_in = in_degree.clone()
    executed = torch.zeros(num_agents, dtype=torch.bool)
    groups: list[list[str]] = []

    while not torch.all(executed):
        ready: list[str] = []
        ready = [agent_ids[i] for i in range(num_agents) if not executed[i] and remaining_in[i] == 0]

        if not ready:
            for i in range(num_agents):
                if not executed[i]:
                    ready.append(agent_ids[i])
                    break

        groups.append(ready)

        for aid in ready:
            i = agent_ids.index(aid)
            executed[i] = True
            for j in range(num_agents):
                if a_agents[i, j].item() > threshold:
                    remaining_in[j] = max(0, remaining_in[j] - 1)

    return groups


def _has_unconditional_source(
    target_idx: int,
    skip_src: str,
    a_agents: torch.Tensor,
    agent_ids: list[str],
    edge_conditions: dict[tuple[str, str], Any],
    skipped: set[str],
    threshold: float,
) -> bool:
    """Return True if *target_idx* has at least one active unconditional incoming edge."""
    target = agent_ids[target_idx]
    for k, src in enumerate(agent_ids):
        if src == skip_src or src in skipped:
            continue
        if a_agents[k, target_idx].item() > threshold and (src, target) not in edge_conditions:
            return True
    return False


def _cascade_initial_condition_skip(
    plan: ExecutionPlan,
    a_agents: torch.Tensor,
    agent_ids: list[str],
    edge_conditions: dict[tuple[str, str], Any],
    threshold: float,
    order: list[str] | None = None,
) -> None:
    """
    BFS: propagate condition_skip to unconditional descendants of already-skipped agents.

    When *order* (the topological execution order) is provided, only
    forward edges (src before tgt in *order*) are traversed.  This
    prevents back-edges in cyclic graphs from cascading the skip to
    nodes that appear earlier in the plan.
    """
    order_pos: dict[str, int] = {}
    if order is not None:
        order_pos = {aid: idx for idx, aid in enumerate(order)}

    queue = deque(list(plan.condition_skipped))
    visited = set(plan.condition_skipped)
    remaining_ids = {s.agent_id for s in plan.steps[plan.current_index :]}

    while queue:
        current = queue.popleft()
        if current not in agent_ids:
            continue
        current_idx = agent_ids.index(current)
        current_order = order_pos.get(current)
        for j, aid in enumerate(agent_ids):
            if aid in visited or aid not in remaining_ids:
                continue
            if current_order is not None:
                aid_order = order_pos.get(aid)
                if aid_order is not None and aid_order <= current_order:
                    continue
            weight = a_agents[current_idx, j].item()
            if weight <= threshold:
                continue
            if (current, aid) in edge_conditions:
                continue
            if not _has_unconditional_source(
                j, current, a_agents, agent_ids, edge_conditions, plan.condition_skipped, threshold
            ):
                visited.add(aid)
                plan.apply_condition_skip(aid)
                queue.append(aid)


class AdaptiveScheduler:
    """
    Scheduler supporting multiple routing policies.

    Supports conditional routing via edge_conditions — a dictionary of
    conditions for each edge. Conditions are evaluated at runtime through
    the topology pipeline in MACPRunner.

    Example:
        scheduler = AdaptiveScheduler(policy=RoutingPolicy.TOPOLOGICAL)

        # Edge conditions
        conditions = {
            ("solver", "reviewer"): lambda ctx: "error" not in ctx.messages.get("solver", ""),
            ("reviewer", "finalizer"): "source_success",  # built-in condition
        }

        plan = scheduler.build_plan(
            a_agents, agent_ids, p_matrix,
            edge_conditions=conditions,
            condition_context=ConditionContext(...)
        )

    """

    def __init__(
        self,
        policy: RoutingPolicy = RoutingPolicy.TOPOLOGICAL,
        pruning_config: PruningConfig | None = None,
        beam_width: int = 3,
        k_paths: int = 3,
        condition_evaluator: ConditionEvaluator | None = None,
    ):
        self.policy = policy
        self.pruning = pruning_config or PruningConfig()
        self.beam_width = beam_width
        self.k_paths = k_paths
        self.condition_evaluator = condition_evaluator or _default_evaluator
        self._last_edge_conditions: dict[tuple[str, str], EdgeCondition | str] = {}
        self._last_condition_context: ConditionContext | None = None

    def _apply_initial_condition_skips(
        self,
        plan: ExecutionPlan,
        edge_conditions: dict[tuple[str, str], EdgeCondition | str],
        effective_a: torch.Tensor,
        effective_agent_ids: list[str],
        order: list[str],
    ) -> None:
        """Mark agents that have only conditional incoming edges as initially skipped."""
        first_agent = order[0] if order else None
        conditional_targets = {tgt for (_, tgt) in edge_conditions}
        threshold = self.pruning.min_weight_threshold

        for target in conditional_targets:
            if target not in effective_agent_ids or target == first_agent:
                continue
            target_idx = effective_agent_ids.index(target)
            has_unconditional_incoming = any(
                effective_a[i, target_idx].item() > threshold
                and (effective_agent_ids[i], target) not in edge_conditions
                for i in range(len(effective_agent_ids))
                if effective_agent_ids[i] != target
            )
            if not has_unconditional_incoming:
                plan.apply_condition_skip(target)

        _cascade_initial_condition_skip(
            plan,
            effective_a,
            effective_agent_ids,
            edge_conditions,
            threshold,
            order=order,
        )

    def build_plan(
        self,
        a_agents: torch.Tensor,
        agent_ids: list[str],
        p_matrix: torch.Tensor | None = None,
        start_agent: str | None = None,
        end_agent: str | None = None,
        edge_conditions: dict[tuple[str, str], EdgeCondition | str] | None = None,
        condition_context: ConditionContext | None = None,
        *,
        filter_unreachable: bool = True,
    ) -> ExecutionPlan:
        """
        Build an ExecutionPlan according to the routing policy.

        Args:
            a_agents: Agent edge-weight matrix.
            agent_ids: List of agent IDs.
            p_matrix: Probability matrix (optional).
            start_agent: Start agent.
            end_agent: End agent.
            edge_conditions: Dictionary of conditions {(source, target): condition}.
            condition_context: Context used for condition evaluation.
            filter_unreachable: Whether to exclude isolated nodes from the plan.
                               Saves tokens by removing nodes not on the start->end path.

        Returns:
            ExecutionPlan containing the execution steps.

        """
        if a_agents.size == 0 or not agent_ids:
            return ExecutionPlan()

        self._last_edge_conditions = edge_conditions or {}
        self._last_condition_context = condition_context

        effective_a = a_agents.clone()

        effective_agent_ids = agent_ids
        effective_p = p_matrix
        excluded_agents: list[str] = []

        if filter_unreachable and (start_agent is not None or end_agent is not None):
            relevant, excluded_agents = filter_reachable_agents(
                effective_a, agent_ids, start_agent, end_agent, self.pruning.min_weight_threshold
            )

            if relevant and len(relevant) < len(agent_ids):
                indices = [agent_ids.index(aid) for aid in relevant]
                indices_t = torch.tensor(indices, dtype=torch.long)
                effective_a = effective_a[indices_t][:, indices_t]
                effective_agent_ids = relevant

                if p_matrix is not None:
                    effective_p = p_matrix[indices_t][:, indices_t]

        effective_start = start_agent if start_agent in effective_agent_ids else None
        effective_end = end_agent if end_agent in effective_agent_ids else None

        if self.policy == RoutingPolicy.GREEDY:
            order = self._greedy_order(effective_a, effective_agent_ids, effective_p, effective_start, effective_end)
        elif self.policy == RoutingPolicy.BEAM_SEARCH:
            order = self._beam_search_order(
                effective_a, effective_agent_ids, effective_p, effective_start, effective_end
            )
        elif self.policy == RoutingPolicy.K_SHORTEST:
            order = self._k_shortest_order(
                effective_a, effective_agent_ids, effective_p, effective_start, effective_end
            )
        elif self.policy == RoutingPolicy.WEIGHTED_TOPO:
            order = self._weighted_topological_order(
                effective_a, effective_agent_ids, effective_p, effective_start, effective_end
            )
        else:
            order = build_execution_order(effective_a, effective_agent_ids, start_agent=effective_start)

        steps = self._build_steps(order, effective_a, effective_agent_ids, effective_p)
        plan = ExecutionPlan(steps=steps, end_agent=end_agent)

        for excluded in excluded_agents:
            plan.skipped.add(excluded)

        if edge_conditions:
            self._apply_initial_condition_skips(plan, edge_conditions, effective_a, effective_agent_ids, order)

        return plan

    def _build_steps(
        self,
        order: list[str],
        effective_a: torch.Tensor,
        effective_agent_ids: list[str],
        effective_p: torch.Tensor | None,
    ) -> list[ExecutionStep]:
        """Build the list of execution steps from a computed order."""
        steps: list[ExecutionStep] = []
        agents_before: set[str] = set()

        for agent_id in order:
            idx = effective_agent_ids.index(agent_id)
            all_incoming = get_incoming_agents(
                agent_id, effective_a, effective_agent_ids, self.pruning.min_weight_threshold
            )
            predecessors = [p for p in all_incoming if p in agents_before]
            agents_before.add(agent_id)

            weight = self._compute_step_weight(idx, predecessors, effective_a, effective_agent_ids)
            prob = (
                self._compute_step_probability(idx, predecessors, effective_p, effective_agent_ids)
                if effective_p is not None
                else 1.0
            )
            fallbacks = self._find_fallback_agents(agent_id, effective_agent_ids, effective_a, order)

            steps.append(
                ExecutionStep(
                    agent_id=agent_id,
                    predecessors=predecessors,
                    weight=weight,
                    probability=prob,
                    fallback_agents=fallbacks,
                )
            )

        return steps

    def evaluate_edge_condition(
        self,
        source: str,
        target: str,
        condition: EdgeCondition | str | None,
        context: ConditionContext,
    ) -> bool:
        """
        Evaluate the condition for a specific edge.

        Convenience method for checking a single condition.
        """
        if condition is None:
            return True

        edge_ctx = ConditionContext(
            source_agent=source,
            target_agent=target,
            messages=context.messages,
            step_results=context.step_results,
            state=context.state,
            query=context.query,
            metadata=context.metadata,
        )
        return self.condition_evaluator.evaluate(condition, edge_ctx)

    def should_prune(
        self,
        step: ExecutionStep,
        plan: ExecutionPlan,
        last_result: StepResult | None = None,
    ) -> tuple[bool, str]:
        """Decide whether to prune a step based on weight, probability, errors, or budget."""
        # Dynamic pruning based on the predecessor's quality_score:
        # if the predecessor's response quality is below the threshold, skip the current step.
        if (
            last_result is not None
            and last_result.success
            and self.pruning.quality_scorer is not None
            and last_result.quality_score < self.pruning.min_quality_threshold
        ):
            return (
                True,
                f"predecessor quality {last_result.quality_score:.3f} < threshold "
                f"{self.pruning.min_quality_threshold} (agent: {last_result.agent_id})",
            )

        if step.weight < self.pruning.min_weight_threshold:
            return True, f"weight {step.weight:.3f} < threshold {self.pruning.min_weight_threshold}"

        if step.probability < self.pruning.min_probability_threshold:
            return (
                True,
                f"probability {step.probability:.3f} < threshold {self.pruning.min_probability_threshold}",
            )

        if self.pruning.token_budget is not None and plan.tokens_used >= self.pruning.token_budget:
            return (
                True,
                f"token budget exhausted ({plan.tokens_used}/{self.pruning.token_budget})",
            )

        consecutive_errors = self._count_consecutive_errors(plan)
        if consecutive_errors >= self.pruning.max_consecutive_errors:
            return True, f"too many consecutive errors ({consecutive_errors})"

        if self.pruning.skip_on_predecessor_failure:
            failed_predecessors = [
                predecessor_agent_id
                for predecessor_agent_id, dependency_step_id in zip(
                    step.predecessors,
                    step.dependency_ids or step.predecessors,
                    strict=False,
                )
                if dependency_step_id in plan.failed_step_ids
            ]
            has_no_fallback = not step.fallback_agents or not self.pruning.enable_fallback
            if failed_predecessors and not step.is_optional and has_no_fallback:
                return True, f"predecessors failed: {failed_predecessors}"

        return False, ""

    def should_use_fallback(
        self,
        step: ExecutionStep,
        result: StepResult,
        fallback_attempts: int,
    ) -> bool:
        """Decide whether a fallback agent should be activated for a step."""
        if not self.pruning.enable_fallback:
            return False
        if fallback_attempts >= self.pruning.max_fallback_attempts:
            return False
        if not step.fallback_agents:
            return False
        if not result.success:
            return True
        return result.quality_score < self.pruning.min_quality_threshold

    def _weighted_topological_order(
        self,
        a_agents: torch.Tensor,
        agent_ids: list[str],
        p_matrix: torch.Tensor | None = None,
        start_agent: str | None = None,
        end_agent: str | None = None,
    ) -> list[str]:
        """
        Topological sort with priorities based on sum of outgoing/incoming weights.

        When cycles are present, delegates to build_execution_order for correct
        SCC (strongly connected components) handling and start_agent placement.
        """
        del p_matrix, end_agent  # Reserved for future probability-based and end-agent routing

        num_agents = len(agent_ids)
        if num_agents == 0:
            return []

        graph = rx.PyDiGraph()
        node_indices = [graph.add_node(aid) for aid in agent_ids]

        for i in range(num_agents):
            for j in range(num_agents):
                if i != j and a_agents[i, j].item() > self.pruning.min_weight_threshold:
                    graph.add_edge(node_indices[i], node_indices[j], a_agents[i, j].item())

        try:
            topo_order = rx.topological_sort(graph)
            result = [agent_ids[node_indices.index(idx)] for idx in topo_order]
            # Ensure start_agent comes first if present
            if start_agent and start_agent in result:
                result.remove(start_agent)
                result.insert(0, start_agent)
        except rx.DAGHasCycle:
            # Graph contains cycles — fall back to SCC-based algorithm
            result = build_execution_order(
                a_agents,
                agent_ids,
                start_agent=start_agent,
                threshold=self.pruning.min_weight_threshold,
            )

        return result

    def _greedy_order(  # noqa: PLR0912
        self,
        a_agents: torch.Tensor,
        agent_ids: list[str],
        p_matrix: torch.Tensor | None = None,
        start_agent: str | None = None,
        end_agent: str | None = None,
    ) -> list[str]:
        """Greedy selection of the next node by the sum of outgoing weights."""
        num_agents = len(agent_ids)
        if num_agents == 0:
            return []

        combined = a_agents * p_matrix if p_matrix is not None else a_agents
        visited: set[int] = set()
        order: list[str] = []

        if start_agent and start_agent in agent_ids:
            current_set: set[int] = {agent_ids.index(start_agent)}
        else:
            in_degree = torch.sum((a_agents > self.pruning.min_weight_threshold).int(), dim=0)
            current_set = set(torch.where(in_degree == 0)[0].tolist())
            if not current_set:
                current_set = {0}

        while len(visited) < num_agents:
            best_idx: int | None = None
            best_score = float("-inf")

            for idx in current_set:
                if idx in visited:
                    continue
                score = torch.sum(combined[idx, :]).item()
                if score > best_score:
                    best_score = score
                    best_idx = idx

            if best_idx is None:
                for i in range(num_agents):
                    if i not in visited:
                        order.append(agent_ids[i])
                        visited.add(i)
                break

            order.append(agent_ids[best_idx])
            visited.add(best_idx)

            for j in range(num_agents):
                if combined[best_idx, j].item() > self.pruning.min_weight_threshold:
                    current_set.add(j)

            if end_agent and agent_ids[best_idx] == end_agent:
                break

        return order

    def _beam_search_order(  # noqa: PLR0912
        self,
        a_agents: torch.Tensor,
        agent_ids: list[str],
        p_matrix: torch.Tensor | None = None,
        start_agent: str | None = None,
        end_agent: str | None = None,
    ) -> list[str]:
        """Beam search over paths with maximum cumulative weight."""
        num_agents = len(agent_ids)
        if num_agents == 0:
            return []

        combined = a_agents * p_matrix if p_matrix is not None else a_agents

        if start_agent and start_agent in agent_ids:
            start_indices = [agent_ids.index(start_agent)]
        else:
            in_degree = torch.sum((a_agents > self.pruning.min_weight_threshold).int(), dim=0)
            start_indices = torch.where(in_degree == 0)[0].tolist()
            if not start_indices:
                start_indices = [0]

        beam: list[tuple[float, list[int]]] = [(0.0, [idx]) for idx in start_indices]
        heapq.heapify(beam)

        best_path: list[int] = []
        best_score = float("-inf")

        while beam:
            neg_score, path = heapq.heappop(beam)
            score = -neg_score

            if len(path) == num_agents:
                if score > best_score:
                    best_score = score
                    best_path = path
                continue

            last_idx = path[-1]
            visited = set(path)

            if end_agent and agent_ids[last_idx] == end_agent:
                remaining = [i for i in range(num_agents) if i not in visited]
                full_path = path + remaining
                if score > best_score:
                    best_score = score
                    best_path = full_path
                continue

            candidates: list[tuple[float, list[int]]] = []
            for j in range(num_agents):
                if j not in visited:
                    edge_weight = combined[last_idx, j].item()
                    new_score = score + edge_weight
                    candidates.append((new_score, [*path, j]))

            candidates.sort(key=lambda x: -x[0])
            for new_score, new_path in candidates[: self.beam_width]:
                heapq.heappush(beam, (-new_score, new_path))

            if len(beam) > self.beam_width * num_agents:
                beam = heapq.nsmallest(self.beam_width, beam)
                heapq.heapify(beam)

        if not best_path:
            return agent_ids

        return [agent_ids[idx] for idx in best_path]

    def _k_shortest_order(
        self,
        a_agents: torch.Tensor,
        agent_ids: list[str],
        p_matrix: torch.Tensor | None = None,
        start_agent: str | None = None,
        end_agent: str | None = None,
    ) -> list[str]:
        """Order based on the shortest path (by inverted weights) or topological order."""
        num_agents = len(agent_ids)
        if num_agents == 0:
            return []

        graph = rx.PyDiGraph()
        node_indices = [graph.add_node(aid) for aid in agent_ids]

        combined = a_agents * p_matrix if p_matrix is not None else a_agents

        for i in range(num_agents):
            for j in range(num_agents):
                if i != j and a_agents[i, j].item() > self.pruning.min_weight_threshold:
                    weight = 1.0 / (combined[i, j].item() + _EPSILON)
                    graph.add_edge(node_indices[i], node_indices[j], weight)

        start_idx = agent_ids.index(start_agent) if start_agent and start_agent in agent_ids else 0
        end_idx = agent_ids.index(end_agent) if end_agent and end_agent in agent_ids else num_agents - 1

        order: list[str] = []
        try:
            paths = rx.dijkstra_shortest_paths(
                graph,
                node_indices[start_idx],
                weight_fn=lambda e: e,
            )

            if node_indices[end_idx] in paths:
                path_indices = paths[node_indices[end_idx]]
                order = [agent_ids[node_indices.index(idx)] for idx in path_indices]
        except (ValueError, KeyError, IndexError, RuntimeError):
            pass  # order stays [], fall back to topological order below

        if order:
            return order + [aid for aid in agent_ids if aid not in order]
        return self._weighted_topological_order(a_agents, agent_ids, p_matrix)

    def _compute_step_weight(
        self,
        idx: int,
        predecessors: list[str],
        a_agents: torch.Tensor,
        agent_ids: list[str],
    ) -> float:
        """Average weight of incoming edges for a step."""
        if not predecessors:
            return 1.0
        weights = torch.tensor([a_agents[agent_ids.index(p), idx].item() for p in predecessors])
        return float(torch.mean(weights).item()) if len(weights) > 0 else 1.0

    def _compute_step_probability(
        self,
        idx: int,
        predecessors: list[str],
        p_matrix: torch.Tensor,
        agent_ids: list[str],
    ) -> float:
        """Product of predecessor probabilities for a step."""
        if not predecessors:
            return 1.0
        probs = torch.tensor([p_matrix[agent_ids.index(p), idx].item() for p in predecessors])
        return float(torch.prod(probs).item()) if len(probs) > 0 else 1.0

    def _find_fallback_agents(
        self,
        agent_id: str,
        agent_ids: list[str],
        a_agents: torch.Tensor,
        current_order: list[str],
    ) -> list[str]:
        """Find agents with similar incoming-edge patterns to use as fallbacks."""
        if not self.pruning.enable_fallback:
            return []

        idx = agent_ids.index(agent_id)
        fallbacks: list[str] = []
        in_pattern = a_agents[:, idx]

        for i, aid in enumerate(agent_ids):
            if aid == agent_id or aid in current_order:
                continue

            other_in = a_agents[:, i]
            dot = torch.dot(in_pattern, other_in).item()
            norm1 = torch.norm(in_pattern).item()
            norm2 = torch.norm(other_in).item()

            if norm1 > 0 and norm2 > 0:
                similarity = dot / (norm1 * norm2)
                if similarity > _DEFAULT_SIMILARITY_THRESHOLD:
                    fallbacks.append(aid)

        return fallbacks[: self.pruning.max_fallback_attempts]

    def _count_consecutive_errors(self, plan: ExecutionPlan) -> int:
        """Count how many consecutive steps before the current one ended in failure."""
        count = 0
        for step in reversed(plan.steps[: plan.current_index]):
            if step.step_id in plan.failed_step_ids:
                count += 1
            else:
                break
        return count
