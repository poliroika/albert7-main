"""
Tool integration with LLMs via native function calling.

This module provides:
- LLMResponse and LLMToolCall for parsing LLM responses
- OpenAICaller — a SINGLE caller for all cases (with and without tools)
- Response parsers for OpenAI and Anthropic

Usage:
    # One caller for everything
    caller = create_openai_caller(api_key="...", model="gpt-4")

    # Without tools — returns str
    response = caller("Hello!")

    # With tools — returns LLMResponse
    response = caller("Calculate fib(10)", tools=[...])
    if response.has_tool_calls:
        for tc in response.tool_calls:
            print(tc.name, tc.arguments)
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from gmas.config.logging import logger

from .base import ToolCall


@dataclass
class LLMToolCall:
    """
    Structured tool call from the LLM.

    Represents a tool call returned by the LLM via native function calling.
    """

    id: str
    name: str
    arguments: dict[str, Any]

    def to_tool_call(self) -> ToolCall:
        """Convert to ToolCall for execution."""
        return ToolCall(name=self.name, arguments=self.arguments)


@dataclass
class LLMResponse:
    """
    LLM response with tool call support.

    Attributes:
        content: Text content of the response.
        tool_calls: List of tool calls (if requested by the LLM).
        raw_response: Original API response (for debugging).

    """

    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    raw_response: Any = None

    @property
    def has_tool_calls(self) -> bool:
        """Whether there are tool calls."""
        return len(self.tool_calls) > 0

    def get_tool_calls(self) -> list[ToolCall]:
        """Get ToolCall objects for execution."""
        return [tc.to_tool_call() for tc in self.tool_calls]


def parse_openai_response(response: Any) -> LLMResponse:
    """
    Parse an OpenAI API response into LLMResponse.

    Supports both the new format (tool_calls) and legacy (function_call).

    Args:
        response: Response from the OpenAI ChatCompletion API.

    Returns:
        LLMResponse with parsed data.

    """
    message = response.choices[0].message

    tool_calls = []

    # New format: tool_calls
    if hasattr(message, "tool_calls") and message.tool_calls:
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}

            tool_calls.append(
                LLMToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                )
            )

    # Legacy format: function_call
    elif hasattr(message, "function_call") and message.function_call:
        fc = message.function_call
        try:
            args = json.loads(fc.arguments) if fc.arguments else {}
        except json.JSONDecodeError:
            args = {}

        tool_calls.append(
            LLMToolCall(
                id="legacy_call",
                name=fc.name,
                arguments=args,
            )
        )

    return LLMResponse(
        content=message.content or "",
        tool_calls=tool_calls,
        raw_response=response,
    )


def parse_anthropic_response(response: Any) -> LLMResponse:
    """
    Parse an Anthropic API response into LLMResponse.

    Args:
        response: Response from the Anthropic Messages API.

    Returns:
        LLMResponse with parsed data.

    """
    tool_calls = []
    content_parts = []

    for block in response.content:
        if block.type == "text":
            content_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                LLMToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                )
            )

    return LLMResponse(
        content="\n".join(content_parts),
        tool_calls=tool_calls,
        raw_response=response,
    )


class OpenAICaller:
    """
    SINGLE LLM caller for OpenAI — works both with and without tools.

    This is the RECOMMENDED way to create callers for agents.

    - Without tools: returns str (like a regular caller)
    - With tools: returns LLMResponse with tool_calls

    Example:
        from openai import OpenAI

        client = OpenAI(api_key="...")
        caller = OpenAICaller(client, model="gpt-4")

        # Without tools — regular text response
        response = caller("Hello!")  # -> str

        # With tools — LLMResponse with tool_calls
        response = caller("Calculate fib(15)", tools=[...])  # -> LLMResponse
        if response.has_tool_calls:
            for tc in response.tool_calls:
                print(f"Call {tc.name} with {tc.arguments}")

    """

    def __init__(
        self,
        client: Any,  # OpenAI client
        model: str = "gpt-4",
        temperature: float = 0.1,  # Low temperature for determinism
        max_tokens: int = 2048,
        system_prompt: str | None = None,
        tool_choice: str = "auto",  # "auto" = LLM decides, "required" = mandatory
        max_retries: int = 5,
        retry_base_delay: float = 2.0,
    ):
        """
        Create a universal OpenAI caller.

        Args:
            client: OpenAI client instance.
            model: Model name.
            temperature: Generation temperature (default 0.1 for determinism).
            max_tokens: Maximum tokens in the response.
            system_prompt: System prompt (optional).
            tool_choice: Tool usage policy:
                - "auto": LLM decides whether to use tools (default)
                - "required": LLM MUST call a tool
            max_retries: Maximum number of retries for transient errors (default 5).
            retry_base_delay: Base delay in seconds for exponential backoff (default 2.0).

        """
        self.client = client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt
        self.tool_choice = tool_choice
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.supports_structured = True

    def __call__(
        self,
        prompt: str | list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str | LLMResponse:
        """
        Call the OpenAI API.

        Args:
            prompt: User prompt (string) or list of chat messages
                    (list of {"role": ..., "content": ...} dicts).
            tools: Tools in OpenAI format (optional).

        Returns:
            - str: if tools are not passed
            - LLMResponse: if tools are passed

        """
        if isinstance(prompt, list):
            messages = list(prompt)
            if self.system_prompt and not any(m.get("role") == "system" for m in messages):
                messages.insert(0, {"role": "system", "content": self.system_prompt})
        else:
            messages = []
            if self.system_prompt:
                messages.append({"role": "system", "content": self.system_prompt})
            messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = self.tool_choice

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(**kwargs)

                if tools:
                    return parse_openai_response(response)

                return response.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                retryable = False
                exc_str = str(exc)
                if hasattr(exc, "status_code"):
                    retryable = exc.status_code in (429, 500, 502, 503, 504)
                elif "502" in exc_str or "503" in exc_str or "504" in exc_str or "timeout" in exc_str.lower():
                    retryable = True

                if retryable and attempt < self.max_retries:
                    delay = self.retry_base_delay * (2**attempt)
                    logger.warning(
                        "LLM call failed (attempt {}/{}): {} — retrying in {:.1f}s",
                        attempt + 1,
                        self.max_retries + 1,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                raise

        if last_exc:
            raise last_exc
        msg = "LLM call failed after all retries"
        raise RuntimeError(msg)


# Alias for backward compatibility
OpenAIToolsCaller = OpenAICaller


def create_openai_caller(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "gpt-4",
    temperature: float = 0.1,  # Low temperature by default
    max_tokens: int = 2048,
    system_prompt: str | None = None,
    tool_choice: str = "auto",
    http_proxy: str | None = None,
) -> OpenAICaller:
    """
    Create a universal OpenAI caller.

    This is the RECOMMENDED way to create callers for agents.
    Works both with and without tools.

    Args:
        api_key: OpenAI API key (or from an environment variable).
        base_url: Base URL (for compatible APIs).
        model: Model name.
        temperature: Generation temperature (default 0.1 for determinism).
        max_tokens: Maximum tokens.
        system_prompt: System prompt.
        tool_choice: Tool usage policy:
            - "auto": LLM decides whether to use tools (default)
            - "required": LLM MUST call a tool
        http_proxy: HTTP/HTTPS/SOCKS5 proxy URL, e.g. "http://127.0.0.1:8080"
            or "socks5://127.0.0.1:1080". If not set, falls back to the
            ``LLM_HTTP_PROXY`` / ``HTTPS_PROXY`` / ``HTTP_PROXY`` environment
            variables (checked in that order).

    Returns:
        Ready-to-use OpenAICaller.

    Example:
        # One caller for all agents
        caller = create_openai_caller(
            api_key="sk-...",
            model="gpt-4",
        )

        # Without tools — plain text
        response = caller("Hello!")  # -> str

        # With tools — LLMResponse
        response = caller("Calculate fib(10)", tools=[...])
        if response.has_tool_calls:
            ...

    """
    try:
        from openai import OpenAI
    except ImportError as e:
        msg = "openai package required: pip install openai"
        raise ImportError(msg) from e

    import os

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    # Resolve proxy: explicit arg → env vars
    resolved_proxy = (
        http_proxy or os.environ.get("LLM_HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    )

    try:
        import httpx

        http_client_kwargs: dict[str, Any] = {"trust_env": False}
        if resolved_proxy:
            http_client_kwargs["proxy"] = resolved_proxy
        kwargs["http_client"] = httpx.Client(**http_client_kwargs)
    except ImportError:
        if resolved_proxy:
            logger.warning("httpx not installed — proxy setting {!r} will be ignored", resolved_proxy)

    client = OpenAI(**kwargs)

    return OpenAICaller(
        client=client,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        tool_choice=tool_choice,
    )


# Alias for backward compatibility
create_openai_tools_caller = create_openai_caller
