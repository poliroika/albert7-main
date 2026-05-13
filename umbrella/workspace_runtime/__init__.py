"""
Unified workspace runtime: task contracts, lifecycle entrypoints, adapters.
"""

from umbrella.workspace_runtime.runner import (
    archive_instance,
    build_task_brief_for_instance,
    create_instance_and_run,
    get_adapter_for_instance,
    inspect_run,
    load_task_main_document,
    prepare_instance,
    register_adapter,
    run_workspace,
    snapshot_instance,
)
from umbrella.workspace_runtime.task_main import (
    build_task_brief_for_workspace,
    load_task_main_for_workspace,
    validate_task_main_for_workspace,
)

__all__ = [
    "archive_instance",
    "build_task_brief_for_instance",
    "build_task_brief_for_workspace",
    "create_instance_and_run",
    "get_adapter_for_instance",
    "inspect_run",
    "load_task_main_document",
    "load_task_main_for_workspace",
    "prepare_instance",
    "register_adapter",
    "run_workspace",
    "snapshot_instance",
    "validate_task_main_for_workspace",
]
