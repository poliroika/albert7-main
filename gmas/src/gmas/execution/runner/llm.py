"""LLM caller helpers used by MACPRunner."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from gmas.core.agent import AgentLLMConfig

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam


LLMCallerProtocol = Callable[[str], str]
AsyncLLMCallerProtocol = Callable[[str], Awaitable[str]]
StructuredLLMCallerProtocol = Callable[[list[dict[str, str]]], str]
AsyncStructuredLLMCallerProtocol = Callable[[list[dict[str, str]]], Awaitable[str]]


class LLMCallerFactory:
    """
    Factory for creating LLM callers based on agent configuration.

    Supports caller caching and a default configuration merged into each
    agent-specific configuration.
    """

    def __init__(
        self,
        default_caller: LLMCallerProtocol | None = None,
        default_async_caller: AsyncLLMCallerProtocol | None = None,
        default_config: AgentLLMConfig | None = None,
        caller_builder: Callable[[AgentLLMConfig], LLMCallerProtocol] | None = None,
        async_caller_builder: Callable[[AgentLLMConfig], AsyncLLMCallerProtocol] | None = None,
    ) -> None:
        self.default_caller = default_caller
        self.default_async_caller = default_async_caller
        self.default_config = default_config
        self.caller_builder = caller_builder
        self.async_caller_builder = async_caller_builder
        self._caller_cache: dict[str, LLMCallerProtocol] = {}
        self._async_caller_cache: dict[str, AsyncLLMCallerProtocol] = {}

    def _config_key(self, config: AgentLLMConfig) -> str:
        return f"{config.base_url}|{config.model_name}|{config.api_key}"

    def _merge_config(self, config: AgentLLMConfig) -> AgentLLMConfig:
        if not self.default_config:
            return config
        return AgentLLMConfig(
            model_name=config.model_name or self.default_config.model_name,
            base_url=config.base_url or self.default_config.base_url,
            api_key=config.api_key or self.default_config.api_key,
            max_tokens=config.max_tokens if config.max_tokens is not None else self.default_config.max_tokens,
            temperature=config.temperature if config.temperature is not None else self.default_config.temperature,
            timeout=config.timeout if config.timeout is not None else self.default_config.timeout,
            top_p=config.top_p if config.top_p is not None else self.default_config.top_p,
            stop_sequences=config.stop_sequences or self.default_config.stop_sequences,
            extra_params={**self.default_config.extra_params, **config.extra_params},
        )

    def get_caller(
        self,
        config: AgentLLMConfig | None = None,
        _agent_id: str | None = None,
    ) -> LLMCallerProtocol | None:
        if config is None or not config.is_configured():
            return self.default_caller

        config = self._merge_config(config)
        cache_key = self._config_key(config)

        if cache_key in self._caller_cache:
            return self._caller_cache[cache_key]

        if self.caller_builder:
            caller = self.caller_builder(config)
            self._caller_cache[cache_key] = caller
            return caller

        return self.default_caller

    def get_async_caller(
        self,
        config: AgentLLMConfig | None = None,
        _agent_id: str | None = None,
    ) -> AsyncLLMCallerProtocol | None:
        if config is None or not config.is_configured():
            return self.default_async_caller

        config = self._merge_config(config)
        cache_key = self._config_key(config)

        if cache_key in self._async_caller_cache:
            return self._async_caller_cache[cache_key]

        if self.async_caller_builder:
            caller = self.async_caller_builder(config)
            self._async_caller_cache[cache_key] = caller
            return caller

        return self.default_async_caller

    @classmethod
    def create_openai_factory(
        cls,
        default_api_key: str | None = None,
        default_model: str = "gpt-4",
        default_base_url: str = "https://api.openai.com/v1",
        default_temperature: float = 0.7,
        default_max_tokens: int = 2000,
    ) -> "LLMCallerFactory":
        import os

        if default_api_key and default_api_key.startswith("$"):
            default_api_key = os.environ.get(default_api_key[1:])

        default_config = AgentLLMConfig(
            model_name=default_model,
            base_url=default_base_url,
            api_key=default_api_key,
            temperature=default_temperature,
            max_tokens=default_max_tokens,
        )

        return cls(
            default_config=default_config,
            caller_builder=_create_openai_caller_from_config,
            async_caller_builder=_create_async_openai_caller_from_config,
        )


def _create_openai_caller_from_config(config: AgentLLMConfig) -> LLMCallerProtocol:
    """Create an OpenAI-compatible sync caller from configuration."""
    try:
        from openai import OpenAI
    except ImportError as e:
        msg = "openai package required. Install with: pip install openai"
        raise ImportError(msg) from e

    api_key = config.resolve_api_key()
    client = OpenAI(
        api_key=api_key,
        base_url=config.base_url,
        timeout=config.timeout or 60.0,
    )

    gen_params = config.to_generation_params()
    model = config.model_name or "gpt-4"

    def caller(prompt: str) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **gen_params,
        )
        return response.choices[0].message.content or ""

    return caller


def _create_async_openai_caller_from_config(config: AgentLLMConfig) -> AsyncLLMCallerProtocol:
    """Create an OpenAI-compatible async caller from configuration."""
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        msg = "openai package required. Install with: pip install openai"
        raise ImportError(msg) from e

    api_key = config.resolve_api_key()
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=config.base_url,
        timeout=config.timeout or 60.0,
    )

    gen_params = config.to_generation_params()
    model = config.model_name or "gpt-4"

    async def caller(prompt: str) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **gen_params,
        )
        return response.choices[0].message.content or ""

    return caller


def create_openai_caller(
    api_key: str | None = None,
    model: str = "gpt-4",
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> LLMCallerProtocol:
    """Create a simple OpenAI caller compatible with ``Callable[[str], str]``."""
    config = AgentLLMConfig(
        model_name=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return _create_openai_caller_from_config(config)


def create_openai_structured_caller(
    api_key: str | None = None,
    model: str = "gpt-4",
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> StructuredLLMCallerProtocol:
    """Create an OpenAI structured caller for chat-style messages."""
    try:
        from openai import OpenAI
    except ImportError as e:
        msg = "openai package required. Install with: pip install openai"
        raise ImportError(msg) from e

    client = OpenAI(api_key=api_key, base_url=base_url)

    def caller(messages: list[dict[str, str]]) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=cast("list[ChatCompletionMessageParam]", messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    return caller


def create_openai_async_structured_caller(
    api_key: str | None = None,
    model: str = "gpt-4",
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> AsyncStructuredLLMCallerProtocol:
    """Create an async OpenAI structured caller."""
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        msg = "openai package required. Install with: pip install openai"
        raise ImportError(msg) from e

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def caller(messages: list[dict[str, str]]) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=cast("list[ChatCompletionMessageParam]", messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    return caller
