"""State and configuration models used by MACPRunner."""

from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import torch
from pydantic import BaseModel, ConfigDict, Field

from gmas.callbacks import Handler
from gmas.utils.memory import MemoryConfig

from ..budget import BudgetConfig
from ..errors import ErrorPolicy, ExecutionError, ExecutionMetrics
from ..scheduler import PruningConfig, RoutingPolicy, StepResult
from ..streaming import AsyncStreamCallback, StreamCallback


class HiddenState(BaseModel):
    """Agent hidden state or embeddings passed via hidden channels."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tensor: torch.Tensor | None = None
    embedding: torch.Tensor | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepContext(BaseModel):
    """Context passed to runtime topology hooks."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_id: str
    response: str | None = None
    messages: dict[str, str] = Field(default_factory=dict)
    step_result: StepResult | None = None
    execution_order: list[str] = Field(default_factory=list)
    remaining_agents: list[str] = Field(default_factory=list)
    query: str = ""
    total_tokens: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class TopologyAction(BaseModel):
    """Action returned by runtime topology hooks."""

    early_stop: bool = False
    early_stop_reason: str | None = None
    add_edges: list[tuple[str, str, float]] = Field(default_factory=list)
    remove_edges: list[tuple[str, str]] = Field(default_factory=list)
    skip_agents: list[str] = Field(default_factory=list)
    force_agents: list[str] = Field(default_factory=list)
    condition_skip_agents: list[str] = Field(default_factory=list)
    condition_unskip_agents: list[str] = Field(default_factory=list)
    insert_chains: list[tuple[str, str]] = Field(default_factory=list)
    new_end_agent: str | None = None
    trigger_rebuild: bool = False


TopologyHook = Callable[[StepContext, Any], TopologyAction | None]
AsyncTopologyHook = Callable[[StepContext, Any], Awaitable[TopologyAction | None]]


class EarlyStopCondition:
    """Condition object for stopping execution early."""

    def __init__(
        self,
        condition: Callable[[StepContext], bool],
        reason: str = "Early stop condition met",
        after_agents: list[str] | None = None,
        min_agents_executed: int = 0,
    ) -> None:
        self.condition = condition
        self.reason = reason
        self.after_agents = after_agents
        self.min_agents_executed = min_agents_executed

    def should_stop(self, ctx: StepContext) -> tuple[bool, str]:
        if len(ctx.execution_order) < self.min_agents_executed:
            return False, ""
        if self.after_agents and ctx.agent_id not in self.after_agents:
            return False, ""

        try:
            if self.condition(ctx):
                return True, self.reason
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return False, ""

        return False, ""

    @classmethod
    def on_keyword(
        cls,
        keyword: str,
        reason: str | None = None,
        *,
        case_sensitive: bool = False,
        in_last_response: bool = True,
    ) -> "EarlyStopCondition":
        def check(ctx: StepContext) -> bool:
            text = ctx.response or "" if in_last_response else " ".join(ctx.messages.values())
            if case_sensitive:
                return keyword in text
            return keyword.lower() in text.lower()

        return cls(condition=check, reason=reason or f"Keyword '{keyword}' found")

    @classmethod
    def on_token_limit(
        cls,
        max_tokens: int,
        reason: str | None = None,
    ) -> "EarlyStopCondition":
        return cls(
            condition=lambda ctx: ctx.total_tokens >= max_tokens,
            reason=reason or f"Token limit {max_tokens} reached",
        )

    @classmethod
    def on_agent_count(
        cls,
        max_agents: int,
        reason: str | None = None,
    ) -> "EarlyStopCondition":
        return cls(
            condition=lambda ctx: len(ctx.execution_order) >= max_agents,
            reason=reason or f"Agent count limit {max_agents} reached",
        )

    @classmethod
    def on_metadata(
        cls,
        key: str,
        value: Any = None,
        comparator: Callable[[Any, Any], bool] | None = None,
        reason: str | None = None,
    ) -> "EarlyStopCondition":
        def check(ctx: StepContext) -> bool:
            if key not in ctx.metadata:
                return False
            actual = ctx.metadata[key]
            if value is None:
                return True
            if comparator:
                return comparator(actual, value)
            return actual == value

        return cls(condition=check, reason=reason or f"Metadata condition met: {key}")

    @classmethod
    def on_custom(
        cls,
        condition: Callable[[StepContext], bool],
        reason: str = "Custom condition met",
        **kwargs: Any,
    ) -> "EarlyStopCondition":
        return cls(condition=condition, reason=reason, **kwargs)

    @classmethod
    def combine_any(
        cls,
        conditions: list["EarlyStopCondition"],
        reason: str = "One of conditions met",
    ) -> "EarlyStopCondition":
        def check(ctx: StepContext) -> bool:
            return any(cond.should_stop(ctx)[0] for cond in conditions)

        return cls(condition=check, reason=reason)

    @classmethod
    def combine_all(
        cls,
        conditions: list["EarlyStopCondition"],
        reason: str = "All conditions met",
    ) -> "EarlyStopCondition":
        def check(ctx: StepContext) -> bool:
            return all(cond.should_stop(ctx)[0] for cond in conditions)

        return cls(condition=check, reason=reason)


class MACPResult(NamedTuple):
    """MACP execution result with messages, metrics, and states."""

    messages: dict[str, str]
    final_answer: str
    final_agent_id: str
    execution_order: list[str]
    agent_states: dict[str, list[dict[str, Any]]] | None = None
    step_results: dict[str, StepResult] | None = None
    step_results_by_step: dict[str, StepResult] | None = None
    messages_by_step: dict[str, str] | None = None
    total_tokens: int = 0
    total_time: float = 0.0
    topology_changed_count: int = 0
    fallback_count: int = 0
    pruned_agents: list[str] | None = None
    errors: list[ExecutionError] | None = None
    hidden_states: dict[str, HiddenState] | None = None
    metrics: ExecutionMetrics | None = None
    budget_summary: dict[str, Any] | None = None
    early_stopped: bool = False
    early_stop_reason: str | None = None
    topology_modifications: int = 0


class ExecutionContext(NamedTuple):
    """Prepared graph execution context used by MACPRunner internals."""

    task_idx: int
    a_agents: Any
    agent_ids: list[str]
    query: str
    agent_lookup: dict[str, Any]
    agent_names: dict[str, str]


class RunnerConfig(BaseModel):
    """Runner configuration: timeouts, adaptivity, parallelism, budgets, and logging."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timeout: float = 60.0
    adaptive: bool = False
    enable_parallel: bool = True
    max_parallel_size: int = 5
    max_retries: int = 2
    retry_delay: float = 1.0
    retry_backoff: float = 2.0
    update_states: bool = True
    routing_policy: RoutingPolicy = RoutingPolicy.TOPOLOGICAL
    pruning_config: PruningConfig | None = None
    enable_hidden_channels: bool = False
    hidden_combine_strategy: str = "mean"
    pass_embeddings: bool = True
    error_policy: ErrorPolicy = Field(default_factory=ErrorPolicy)
    budget_config: BudgetConfig | None = None
    callbacks: list[Handler] = Field(default_factory=list)
    enable_memory: bool = False
    memory_config: MemoryConfig | None = None
    memory_context_limit: int = 5
    enable_token_streaming: bool = False
    stream_callbacks: list[StreamCallback] = Field(default_factory=list)
    async_stream_callbacks: list[AsyncStreamCallback] = Field(default_factory=list)
    prompt_preview_length: int = 100
    broadcast_task_to_all: bool = True
    enable_dynamic_topology: bool = False
    topology_hooks: list[Any] = Field(default_factory=list)
    async_topology_hooks: list[Any] = Field(default_factory=list)
    early_stop_conditions: list[Any] = Field(default_factory=list)
    max_tool_iterations: int = 3
    max_loop_iterations: int = 5
    tool_registry: Any | None = None
