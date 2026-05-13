"""Public MACPRunner facade built from gmas.execution mixins."""

from gmas.utils.memory import AgentMemory, MemoryConfig, SharedMemoryPool

from ..budget import BudgetConfig
from ..errors import ErrorPolicy, ExecutionMetrics
from ..streaming import AsyncStreamCallback, StreamCallback, StreamEvent, StreamEventType
from .batch import RunnerBatchMixin
from .core import RunnerCoreMixin
from .execution import RunnerExecutionMixin
from .llm import (
    AsyncLLMCallerProtocol,
    AsyncStructuredLLMCallerProtocol,
    LLMCallerFactory,
    LLMCallerProtocol,
    StructuredLLMCallerProtocol,
    _create_async_openai_caller_from_config,
    _create_openai_caller_from_config,
    create_openai_async_structured_caller,
    create_openai_caller,
    create_openai_structured_caller,
)
from .prompting import StructuredPrompt, _strip_tool_metadata
from .shared import TOOLS_AVAILABLE, ToolRegistry
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
from .stream import RunnerStreamMixin
from .topology import RunnerTopologyMixin

_COMPAT_REEXPORTS = (
    TOOLS_AVAILABLE,
    _create_openai_caller_from_config,
    _create_async_openai_caller_from_config,
)


class MACPRunner(
    RunnerStreamMixin,
    RunnerBatchMixin,
    RunnerExecutionMixin,
    RunnerTopologyMixin,
    RunnerCoreMixin,
):
    """MACP protocol executor assembled from focused execution mixins."""


__all__ = [
    "AgentMemory",
    "AsyncLLMCallerProtocol",
    "AsyncStreamCallback",
    "AsyncStructuredLLMCallerProtocol",
    "AsyncTopologyHook",
    "BudgetConfig",
    "EarlyStopCondition",
    "ErrorPolicy",
    "ExecutionContext",
    "ExecutionMetrics",
    "HiddenState",
    "LLMCallerFactory",
    "LLMCallerProtocol",
    "MACPResult",
    "MACPRunner",
    "MemoryConfig",
    "RunnerConfig",
    "SharedMemoryPool",
    "StepContext",
    "StreamCallback",
    "StreamEvent",
    "StreamEventType",
    "StructuredLLMCallerProtocol",
    "StructuredPrompt",
    "ToolRegistry",
    "TopologyAction",
    "TopologyHook",
    "_strip_tool_metadata",
    "create_openai_async_structured_caller",
    "create_openai_caller",
    "create_openai_structured_caller",
]
