"""
Workspace adapters package.

This package contains adapters for different workspace types.

Currently supports:
- agent_research (article research pipeline)
- evaluation (evaluation and benchmarking)
"""

from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter
from umbrella.workspace_runtime.adapters.agent_research import AgentResearchAdapter
from umbrella.workspace_runtime.adapters.evaluation import EvaluationAdapter
from umbrella.workspace_runtime.adapters.generic import GenericWorkspaceAdapter
from umbrella.workspace_runtime.adapters.world_prediction import WorldPredictionAdapter

__all__ = [
    "BaseWorkspaceAdapter",
    "AgentResearchAdapter",
    "EvaluationAdapter",
    "GenericWorkspaceAdapter",
    "WorldPredictionAdapter",
]
