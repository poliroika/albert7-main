"""Runtime deadline helpers for Ouroboros task loops."""

import time
from typing import Any, Dict, Optional, Tuple


DEADLINE_BEFORE_NEXT_ROUND = (
    "Ouroboros runtime deadline reached before the next LLM/tool round. "
    "Return the current findings and resume later if more work is needed."
)

DEADLINE_AFTER_LLM_RESPONSE = (
    "Ouroboros runtime deadline reached after the LLM response; "
    "tool calls were not executed past the deadline."
)


def check_runtime_deadline(
    deadline_monotonic: float | None,
    accumulated_usage: dict[str, Any],
    llm_trace: dict[str, Any],
    finish_reason: str,
    content: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    if deadline_monotonic is None or time.monotonic() < deadline_monotonic:
        return None
    if content and content.strip():
        llm_trace["assistant_notes"].append(content.strip()[:320])
    return finish_reason, accumulated_usage, llm_trace
