"""Prompt formatting helpers used by MACPRunner."""

from typing import Any

_PROMPT_REPR_PREVIEW_LENGTH = 80


def _collect_tool_parts(
    messages: list[dict[str, Any]],
    start: int,
    per_result_limit: int,
) -> tuple[list[str], int]:
    """Collect tool-result strings following an assistant message."""
    j = start
    parts: list[str] = []
    while j < len(messages) and messages[j].get("role") == "tool":
        tool_content = messages[j].get("content") or ""
        if tool_content:
            if per_result_limit and len(tool_content) > per_result_limit:
                tool_content = tool_content[:per_result_limit] + "\n...(truncated)"
            parts.append(tool_content)
        j += 1

    return parts, j


def _build_assistant_parts(msg: dict[str, Any]) -> list[str]:
    """Flatten an assistant message, including tool calls, into text parts."""
    import json as _json

    parts: list[str] = []
    content = msg.get("content") or ""
    if content:
        parts.append(content)

    for tc in msg.get("tool_calls") or []:
        func = tc.get("function", {})
        name = func.get("name", "?")
        try:
            args = _json.loads(func.get("arguments", "{}"))
        except (ValueError, TypeError):
            args = func.get("arguments", "")
        parts.append(f"[Called {name}({args})]")

    return parts


def _strip_tool_metadata(
    messages: list[dict[str, Any]],
    max_total_chars: int = 0,
) -> list[dict[str, str]]:
    """
    Convert a tool-calling conversation into plain chat messages.

    Removes ``role=tool`` entries and ``tool_calls`` keys from assistant
    messages so the resulting list is safe to send to an LLM endpoint
    without tool definitions.
    """
    tool_contents = [m["content"] for m in messages if m.get("role") == "tool" and m.get("content")]

    total_tool_chars = sum(len(c) for c in tool_contents)
    n_tools = len(tool_contents) or 1
    if max_total_chars and total_tool_chars > max_total_chars:
        per_result_limit = max(max_total_chars // n_tools, 500)
    else:
        per_result_limit = 0

    clean: list[dict[str, str]] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role in ("system", "user"):
            content = msg.get("content") or ""
            if content:
                clean.append({"role": role, "content": content})
            i += 1
            continue

        if role == "assistant":
            parts = _build_assistant_parts(msg)
            tool_parts, j = _collect_tool_parts(messages, i + 1, per_result_limit)
            parts.extend(tool_parts)

            if parts:
                text = "\n\n".join(parts)
                if clean and clean[-1]["role"] == "assistant":
                    clean[-1]["content"] += "\n\n" + text
                else:
                    clean.append({"role": "assistant", "content": text})
            i = j
            continue

        i += 1

    return clean


class StructuredPrompt:
    """
    Prompt carrying both a flat string and structured chat messages.

    The runner sends ``messages`` to structured callers and falls back to
    ``text`` for legacy string-only callers.
    """

    __slots__ = ("messages", "text")

    def __init__(self, text: str, messages: list[dict[str, str]]) -> None:
        self.text = text
        self.messages = messages

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        preview = self.text[:_PROMPT_REPR_PREVIEW_LENGTH] + (
            "..." if len(self.text) > _PROMPT_REPR_PREVIEW_LENGTH else ""
        )
        return f"StructuredPrompt(text={preview!r}, messages={len(self.messages)})"
