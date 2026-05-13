"""Shared imports and constants used by MACPRunner mixins."""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import torch

from gmas.callbacks import CallbackManager, Handler, get_callback_manager
from gmas.utils.memory import AgentMemory, MemoryConfig, SharedMemoryPool

from ..budget import BudgetConfig, BudgetTracker
from ..errors import ErrorPolicy, ExecutionError, ExecutionMetrics
from ..scheduler import (
    AdaptiveScheduler,
    ConditionContext,
    ExecutionPlan,
    PruningConfig,
    RoutingPolicy,
    StepResult,
    build_execution_order,
    extract_agent_adjacency,
    filter_reachable_agents,
    get_incoming_agents,
    get_parallel_groups,
)
from ..streaming import (
    AgentErrorEvent,
    AgentOutputEvent,
    AgentStartEvent,
    AnyStreamEvent,
    AsyncStreamCallback,
    FallbackEvent,
    ParallelEndEvent,
    ParallelStartEvent,
    PruneEvent,
    RunEndEvent,
    RunStartEvent,
    StreamCallback,
    StreamEvent,
    StreamEventType,
    TokenEvent,
    TopologyChangedEvent,
)
from .llm import (
    AsyncLLMCallerProtocol,
    AsyncStructuredLLMCallerProtocol,
    LLMCallerFactory,
    LLMCallerProtocol,
    StructuredLLMCallerProtocol,
)
from .prompting import StructuredPrompt, _strip_tool_metadata
from .state import (
    AsyncTopologyHook,
    EarlyStopCondition,
    ExecutionContext,
    HiddenState,
    MACPResult,
    RunnerConfig,
    StepContext,
    TopologyAction,
    TopologyHook,
)

ToolRegistry: Any

try:
    from gmas.tools import ToolRegistry as _ImportedToolRegistry
except ImportError:
    ToolRegistry = Any
    TOOLS_AVAILABLE = False
else:
    ToolRegistry = _ImportedToolRegistry
    TOOLS_AVAILABLE = True

_MIN_EDGE_WEIGHT: float = 1e-6

__all__ = [
    "TOOLS_AVAILABLE",
    "UTC",
    "_MIN_EDGE_WEIGHT",
    "AdaptiveScheduler",
    "AgentErrorEvent",
    "AgentMemory",
    "AgentOutputEvent",
    "AgentStartEvent",
    "Any",
    "AnyStreamEvent",
    "AsyncIterator",
    "AsyncLLMCallerProtocol",
    "AsyncStreamCallback",
    "AsyncStructuredLLMCallerProtocol",
    "AsyncTopologyHook",
    "Awaitable",
    "BudgetConfig",
    "BudgetTracker",
    "Callable",
    "CallbackManager",
    "ConditionContext",
    "EarlyStopCondition",
    "ErrorPolicy",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionMetrics",
    "ExecutionPlan",
    "FallbackEvent",
    "Handler",
    "HiddenState",
    "Iterator",
    "LLMCallerFactory",
    "LLMCallerProtocol",
    "MACPResult",
    "MemoryConfig",
    "ParallelEndEvent",
    "ParallelStartEvent",
    "PruneEvent",
    "PruningConfig",
    "RoutingPolicy",
    "RunEndEvent",
    "RunStartEvent",
    "RunnerConfig",
    "SharedMemoryPool",
    "StepContext",
    "StepResult",
    "StreamCallback",
    "StreamEvent",
    "StreamEventType",
    "StructuredLLMCallerProtocol",
    "StructuredPrompt",
    "TokenEvent",
    "ToolRegistry",
    "TopologyAction",
    "TopologyChangedEvent",
    "TopologyHook",
    "_strip_tool_metadata",
    "asyncio",
    "build_execution_order",
    "cast",
    "datetime",
    "deque",
    "extract_agent_adjacency",
    "filter_reachable_agents",
    "get_callback_manager",
    "get_incoming_agents",
    "get_parallel_groups",
    "time",
    "torch",
    "uuid",
]
