"""Central truncation / preview limits (env-tunable).

Large tool payloads are still bounded here for logs and JSONL; the agent
can call ``compact_context`` when the chat history grows. Model input
budgets are separate (``OUROBOROS_MODEL_CONTEXT_TOKENS``, GMAS token caps).
"""

import os


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (ValueError, TypeError):
        return max(minimum, default)


# tools.jsonl ``result_preview`` and similar durable logs
TOOL_LOG_PREVIEW_CHARS = _int_env("OUROBOROS_TOOL_LOG_PREVIEW_CHARS", 16000, minimum=2000)

# Tool message content returned to the LLM in the active chat
TOOL_RESULT_TO_MODEL_CHARS = _int_env(
    "OUROBOROS_TOOL_RESULT_TO_MODEL_CHARS", 48000, minimum=4000
)

# Per-tool line in llm_trace / completion nudges
TOOL_TRACE_SNIPPET_CHARS = _int_env(
    "OUROBOROS_TOOL_TRACE_SNIPPET_CHARS", 2400, minimum=400
)

# Large string fields inside logged tool args
TOOL_ARGS_LOG_CHARS = _int_env("OUROBOROS_TOOL_ARGS_LOG_CHARS", 12000, minimum=500)

# Default for ``truncate_for_log`` when max_chars is omitted
TRUNCATE_FOR_LOG_DEFAULT = _int_env(
    "OUROBOROS_TRUNCATE_FOR_LOG_DEFAULT", 16000, minimum=1000
)

# Task text persisted in events.jsonl
TASK_TEXT_LOG_CHARS = _int_env("OUROBOROS_TASK_TEXT_LOG_CHARS", 16000, minimum=1000)

# web_search / deep_search / github excerpt payloads
DISCOVERY_CONTENT_CHARS = _int_env(
    "OUROBOROS_DISCOVERY_CONTENT_CHARS", 12000, minimum=2000
)

# Inline recall block in system message (not full Palace bodies)
RECALL_DRAWER_PREVIEW_CHARS = _int_env(
    "OUROBOROS_RECALL_DRAWER_PREVIEW_CHARS", 1200, minimum=200
)
RECALL_LESSON_PREVIEW_CHARS = _int_env(
    "OUROBOROS_RECALL_LESSON_PREVIEW_CHARS", 2000, minimum=200
)
RECALL_BLOCK_MAX_CHARS = _int_env("OUROBOROS_RECALL_BLOCK_MAX_CHARS", 8000, minimum=1000)

# Planner objective digest from chat
TASK_BRIEF_MAX_CHARS = _int_env("OUROBOROS_TASK_BRIEF_MAX_CHARS", 8000, minimum=500)

# phase_impasse.json and similar error artifacts
PHASE_ERROR_ARTIFACT_CHARS = _int_env(
    "OUROBOROS_PHASE_ERROR_ARTIFACT_CHARS", 8000, minimum=500
)

# Watcher recent-tool snippets in lessons
WATCHER_TOOL_SNIPPET_CHARS = _int_env(
    "OUROBOROS_WATCHER_TOOL_SNIPPET_CHARS", 800, minimum=160
)
WATCHER_TOOL_ARGS_SNIPPET_CHARS = _int_env(
    "OUROBOROS_WATCHER_TOOL_ARGS_SNIPPET_CHARS", 480, minimum=80
)

# Palace / memory tool responses in Umbrella
MEMORY_HIT_PREVIEW_CHARS = _int_env(
    "OUROBOROS_MEMORY_HIT_PREVIEW_CHARS", 8000, minimum=500
)
