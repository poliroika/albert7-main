"""LLM-based compression of large GMAS source chunks for retrieval context."""

import logging
from pathlib import Path

from umbrella.env import get_default_workspace_model, load_env

log = logging.getLogger(__name__)

_FALLBACK_MARKER = "\n\n# [truncated by fallback — gmas_summarizer]\n"


def summarize_chunk(
    text: str,
    target_tokens: int,
    file_path: Path | str,
    *,
    model: str | None = None,
) -> str:
    """Compress Python/Markdown source while preserving APIs and usage patterns.

    Uses the same LLM stack as workspace flows (``get_llm_client`` + main model).
    On failure, truncates to ~``target_tokens`` heuristic characters.
    """
    load_env()
    if not text or not str(text).strip():
        return ""
    tgt = max(200, int(target_tokens))
    fp = Path(file_path)

    try:
        from umbrella.control_plane.code_analyzer import get_llm_client

        client = get_llm_client()
        if client is None:
            return _fallback_truncate(text, tgt)

        mdl = (model or get_default_workspace_model()).strip()
        prompt = (
            "Compress the following source file excerpt for an engineer who will "
            "implement against this library. Preserve:\n"
            "- import lines and public module structure\n"
            "- public class/function signatures and their docstrings\n"
            "- key usage patterns and config knobs\n"
            "Drop: repetitive boilerplate, long private helpers, and verbose comments.\n"
            f"Target at most ~{tgt} tokens of prose plus code. File: `{fp.as_posix()}`\n\n"
            "---\n"
            f"{text}\n"
            "---"
        )
        msg, _ = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=mdl,
        )
        out = (msg.get("content") or "").strip()
        if out:
            return out
    except Exception as exc:
        log.debug("gmas_summarizer LLM failed: %s", exc, exc_info=True)

    return _fallback_truncate(text, tgt)


def _fallback_truncate(text: str, target_tokens: int) -> str:
    max_chars = max(800, target_tokens * 4)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + _FALLBACK_MARKER
