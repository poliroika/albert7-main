"""Ouroboros dedup helpers for memory scenario harness."""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from umbrella.evals.memory_scenarios.assertions import assert_single_always_loaded_block
from umbrella.evals.memory_scenarios.fixtures import drive_root


def run_ouroboros_dedup_check(
    repo: Path,
    workspace_id: str,
    task: dict[str, Any],
) -> list[str]:
    from ouroboros.context import build_llm_messages
    from ouroboros.memory_hooks import init_loop_memory

    errors: list[str] = []
    messages = [{"role": "user", "content": task["input"]}]
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive_root(repo, workspace_id),
        context_overlays=task.get("context_overlays") or {},
        umbrella_managed=True,
    )
    init_loop_memory(messages, ctx)
    if len(messages) != 1:
        errors.append(f"init_loop_memory mutated messages: len={len(messages)}")

    class _Env:
        def __init__(self) -> None:
            self.repo_dir = repo
            self.drive_root = drive_root(repo, workspace_id)

        def repo_path(self, rel: str) -> Path:
            return self.repo_dir / rel

        def drive_path(self, rel: str) -> Path:
            return self.drive_root / rel

    class _Mem:
        def ensure_files(self) -> None:
            return None

    with (
        patch("ouroboros.context._safe_read", return_value=""),
        patch("ouroboros.context._build_memory_sections", return_value=[]),
        patch("ouroboros.context._build_recent_sections", return_value=[]),
        patch("ouroboros.context._build_runtime_section", return_value=""),
        patch("ouroboros.context._build_health_invariants", return_value=""),
        patch("ouroboros.context.use_anthropic_style_cache_extensions", return_value=False),
    ):
        llm_messages, _ = build_llm_messages(env=_Env(), memory=_Mem(), task=task)

    system_text = "\n".join(
        str(m.get("content") or "")
        for m in messages + llm_messages
        if m.get("role") == "system"
    )
    if "[ALWAYS-ON CONTEXT]" in system_text:
        errors.append("Ouroboros duplicated ALWAYS-ON CONTEXT in system")
    if "## [ALWAYS-LOADED MEMORY]" in system_text:
        errors.append("Ouroboros duplicated ALWAYS-LOADED in system")
    task_input = str(task.get("input") or "")
    errors.extend(assert_single_always_loaded_block(task_input))
    return errors
