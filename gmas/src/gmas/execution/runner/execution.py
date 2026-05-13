"""Agent execution, prompt construction, hidden-state, and parallel helpers."""

import time as _time
from typing import TYPE_CHECKING

from gmas.config.logging import logger

from .shared import (
    TOOLS_AVAILABLE,
    Any,
    ConditionContext,
    ExecutionError,
    ExecutionPlan,
    HiddenState,
    MACPResult,
    StepResult,
    StructuredPrompt,
    _strip_tool_metadata,
    asyncio,
    build_execution_order,
    get_incoming_agents,
    time,
    torch,
)

if TYPE_CHECKING:
    from . import MACPRunner


class RunnerExecutionMixin:
    def _run_agent_with_tools(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        caller: Any,
        prompt: str | StructuredPrompt,
        agent: Any,
    ) -> tuple[str, int]:
        """
        Execute an agent with automatic tools support.

        If the agent has tools, they are ALWAYS used via native function calling.
        Tools are obtained from:
        1. BaseTool objects directly in agent.tools
        2. Tool names registered in the global registry or config.tool_registry

        ``prompt`` may be a plain ``str`` (legacy) or a
        ``StructuredPrompt`` (modern).  For plain LLM calls the method
        dispatches via :meth:`_call_llm` which picks the structured
        caller when available.

        When a ``structured_llm_caller`` is registered the tool-calling
        loop also uses structured messages (system/user/tool roles) so
        the LLM receives proper role separation throughout the entire
        tool-calling conversation.

        Args:
            caller: LLM caller (must support the tools parameter)
            prompt: Agent prompt (str or StructuredPrompt)
            agent: Agent profile (AgentProfile with tools)

        Returns:
            tuple[str, int]: (response, number of tokens)

        """
        import inspect

        # Normalise prompt — always have both flat text and structured form
        prompt_text = prompt.text if isinstance(prompt, StructuredPrompt) else prompt

        # Check if the agent has tools
        if not TOOLS_AVAILABLE:
            response = self._call_llm(caller, prompt) if isinstance(prompt, StructuredPrompt) else caller(prompt)
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        # Get agent tools (method from AgentProfile)
        agent_tools = []
        if hasattr(agent, "get_tool_objects"):
            agent_tools = agent.get_tool_objects()

        if not agent_tools and hasattr(agent, "tools") and agent.tools:
            from gmas.tools import get_registry

            registry = self.config.tool_registry or get_registry()
            tool_names = [t for t in agent.tools if isinstance(t, str)]
            if tool_names:
                agent_tools = registry.get_tools(tool_names)

        if not agent_tools:
            # Agent has no tools — plain call (use structured dispatch)
            response = self._call_llm(caller, prompt) if isinstance(prompt, StructuredPrompt) else caller(prompt)
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        # Check that caller supports tools
        sig = inspect.signature(caller)
        supports_tools = "tools" in sig.parameters

        if not supports_tools:
            # Caller does not support tools — plain call
            response = self._call_llm(caller, prompt) if isinstance(prompt, StructuredPrompt) else caller(prompt)
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        # Get schemas for tools
        tool_schemas = [t.to_openai_schema() for t in agent_tools]
        if not tool_schemas:
            response = self._call_llm(caller, prompt) if isinstance(prompt, StructuredPrompt) else caller(prompt)
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        # Get registry for executing tools
        from gmas.tools import ToolCall, get_registry

        registry = self.config.tool_registry or get_registry()

        for t in agent_tools:
            if not registry.has(t.name):
                registry.register(t)

        total_tokens = 0

        if isinstance(prompt, StructuredPrompt):
            tool_messages: list[dict[str, str]] = list(prompt.messages)
        else:
            tool_messages = [{"role": "user", "content": prompt}]

        current_prompt = prompt_text

        tool_cache: dict[str, str] = {}

        logger.debug("Agent has tools: {}", [s["function"]["name"] for s in tool_schemas])

        llm_response: Any = None
        _caller_supports_structured = getattr(caller, "supports_structured", False)
        use_structured_tools = isinstance(prompt, StructuredPrompt) and (
            self.structured_llm_caller is not None or _caller_supports_structured
        )

        for iteration in range(self.config.max_tool_iterations):
            # Call LLM with tools
            logger.debug("Tool calling iteration {}", iteration + 1)

            is_last_iteration = iteration == self.config.max_tool_iterations - 1

            if use_structured_tools:
                llm_response = caller(tool_messages, tools=tool_schemas)
            else:
                llm_response = caller(current_prompt, tools=tool_schemas)

            if isinstance(llm_response, str):
                # Caller returned a string, not an LLMResponse
                return llm_response, total_tokens + self.token_counter(llm_response)

            # Token counting: use the actual prompt content sent to the LLM
            if use_structured_tools:
                prompt_tokens = sum(self.token_counter(m.get("content") or "") for m in tool_messages)
            else:
                prompt_tokens = self.token_counter(current_prompt)
            total_tokens += prompt_tokens
            if llm_response.content:
                total_tokens += self.token_counter(llm_response.content)

            if not llm_response.has_tool_calls:
                content = llm_response.content or ""
                if content:
                    logger.debug("No tool calls, returning content: {}...", content[:50])
                    return content, total_tokens
                logger.debug("No tool calls and empty content — breaking to post-loop fallback")
                break

            # On the last iteration, if the LLM returned content alongside
            # tool_calls, return the content immediately.  Otherwise fall
            # through to execute the tool calls and then force a final
            # answer — this prevents returning an empty string when the
            # model stubbornly issues tool_calls on the last turn.
            if is_last_iteration and llm_response.content:
                logger.debug("Last iteration with content, returning: {}...", llm_response.content[:50])
                return llm_response.content, total_tokens

            # Execute tool_calls with caching
            tool_results: list[str] = []
            all_cached = True  # Track if ALL calls in this iteration were cached
            for tc in llm_response.tool_calls:
                # Create a cache key from the name and arguments
                import json as json_module

                cache_key = f"{tc.name}:{json_module.dumps(tc.arguments, sort_keys=True)}"

                if cache_key in tool_cache:
                    # Already called with these arguments — use cache
                    output = tool_cache[cache_key]
                    logger.debug("Tool cache hit: {}({}) -> {}...", tc.name, tc.arguments, output[:50])
                else:
                    # New call — execute and cache
                    all_cached = False
                    logger.debug("Executing tool: {}({})", tc.name, tc.arguments)
                    tool_call = ToolCall(name=tc.name, arguments=tc.arguments)

                    _cb = getattr(self, "_callback_manager", None)
                    _rid = getattr(self, "_current_run_id", None)
                    if _cb:
                        _cb.on_tool_start(
                            run_id=_rid,
                            tool_name=tc.name,
                            arguments=tc.arguments,
                        )
                    _t0 = _time.monotonic()
                    result = registry.execute(tool_call)
                    _elapsed = (_time.monotonic() - _t0) * 1000
                    output = result.output if result.success else f"Error: {result.error}"
                    tool_cache[cache_key] = output

                    if _cb:
                        if result.success:
                            _cb.on_tool_end(
                                run_id=_rid,
                                tool_name=tc.name,
                                success=True,
                                output_size=len(output),
                                duration_ms=_elapsed,
                            )
                        else:
                            _cb.on_tool_error(
                                run_id=_rid,
                                tool_name=tc.name,
                                error_type="ToolExecutionError",
                                error_message=result.error or "",
                            )

                    logger.debug("Tool result: {}...", output[:100])

                tool_results.append(f"[{tc.name}]: {output}")

            if all_cached and llm_response.tool_calls:
                logger.warning(
                    "All {} tool calls were cached duplicates — model is looping, forcing final answer",
                    len(llm_response.tool_calls),
                )
                break

            tool_results_text = "\n".join(tool_results)

            if isinstance(prompt, StructuredPrompt):
                _used_native_format = False
                if (
                    llm_response.has_tool_calls
                    and hasattr(llm_response, "raw_response")
                    and llm_response.raw_response is not None
                ):
                    try:
                        raw_msg = llm_response.raw_response.choices[0].message
                        assistant_msg: dict[str, Any] = {
                            "role": "assistant",
                            "content": raw_msg.content,
                        }
                        if hasattr(raw_msg, "tool_calls") and raw_msg.tool_calls:
                            assistant_msg["tool_calls"] = [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                                for tc in raw_msg.tool_calls
                            ]
                        tool_messages.append(assistant_msg)

                        for i, tc in enumerate(llm_response.tool_calls):
                            raw_tc_id = raw_msg.tool_calls[i].id if raw_msg.tool_calls else tc.id
                            tool_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": raw_tc_id,
                                    "content": tool_results[i] if i < len(tool_results) else "",
                                }
                            )
                        _used_native_format = True
                    except Exception:  # noqa: BLE001
                        _used_native_format = False

                if not _used_native_format:
                    if llm_response.content:
                        tool_messages.append({"role": "assistant", "content": llm_response.content})
                    tool_messages.append({"role": "user", "content": f"Tool results:\n{tool_results_text}"})

            current_prompt += f"\n\nTool results:\n{tool_results_text}"

            # Last iteration: we've executed the tool calls and appended
            # results — now break out so the post-loop "forcing final
            # answer" logic can make one more LLM call *without* tools.
            if is_last_iteration:
                logger.debug("Last iteration — tool calls executed, will force final answer")
                break

        # ------------------------------------------------------------------
        # Post-loop: either the loop exhausted all iterations, or we broke
        # out because all tool calls were cached / last iteration.  Try to
        # return whatever content the LLM already produced; if it's empty,
        # make one final LLM call *without* tool schemas to coerce a text
        # answer.
        # ------------------------------------------------------------------
        last_content = llm_response.content if llm_response else ""
        if last_content:
            return last_content, total_tokens

        _caller_max_tokens = getattr(caller, "max_tokens", 0) or 0
        _ctx_budget = _caller_max_tokens * 3 if _caller_max_tokens else 0
        try:
            if use_structured_tools:
                clean_messages = _strip_tool_metadata(tool_messages, max_total_chars=_ctx_budget)
                clean_messages.append({"role": "user", "content": "Now provide your final answer."})
                final_resp = caller(clean_messages, tools=None)
            else:
                final_resp = caller(current_prompt, tools=None)

            if isinstance(final_resp, str):
                if final_resp:
                    return final_resp, total_tokens + self.token_counter(final_resp)
            else:
                final_content = getattr(final_resp, "content", "") or ""
                if final_content:
                    return final_content, total_tokens + self.token_counter(final_content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Final summary call failed: {}", exc)

        for msg in reversed(tool_messages if use_structured_tools else []):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"], total_tokens
        return last_content, total_tokens

    async def _run_agent_with_tools_async(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        async_caller: Any,
        prompt: str | StructuredPrompt,
        agent: Any,
    ) -> tuple[str, int]:
        """
        Async version of :meth:`_run_agent_with_tools`.

        Mirrors the synchronous tool-calling loop but ``await``-s the
        LLM calls so it works inside an ``asyncio`` event loop.

        When the async caller does not support a ``tools`` parameter
        (i.e. it is a plain ``async (str) -> str`` function), falls
        back to a simple prompt call — identical to the old behaviour.
        """
        import inspect

        prompt_text = prompt.text if isinstance(prompt, StructuredPrompt) else prompt

        if not TOOLS_AVAILABLE:
            response = (
                await self._acall_llm(async_caller, prompt)
                if isinstance(prompt, StructuredPrompt)
                else await async_caller(prompt)
            )
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        agent_tools = []
        if hasattr(agent, "get_tool_objects"):
            agent_tools = agent.get_tool_objects()

        if not agent_tools and hasattr(agent, "tools") and agent.tools:
            from gmas.tools import get_registry

            registry = self.config.tool_registry or get_registry()
            tool_names = [t for t in agent.tools if isinstance(t, str)]
            if tool_names:
                agent_tools = registry.get_tools(tool_names)

        if not agent_tools:
            response = (
                await self._acall_llm(async_caller, prompt)
                if isinstance(prompt, StructuredPrompt)
                else await async_caller(prompt)
            )
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        sig = inspect.signature(async_caller) if async_caller is not None else None
        supports_tools = sig is not None and "tools" in sig.parameters

        if not supports_tools:
            response = (
                await self._acall_llm(async_caller, prompt)
                if isinstance(prompt, StructuredPrompt)
                else await async_caller(prompt)
            )
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        tool_schemas = [t.to_openai_schema() for t in agent_tools]
        if not tool_schemas:
            response = (
                await self._acall_llm(async_caller, prompt)
                if isinstance(prompt, StructuredPrompt)
                else await async_caller(prompt)
            )
            return response, self.token_counter(prompt_text) + self.token_counter(response)

        from gmas.tools import ToolCall, get_registry

        registry = self.config.tool_registry or get_registry()

        for t in agent_tools:
            if not registry.has(t.name):
                registry.register(t)

        total_tokens = 0

        if isinstance(prompt, StructuredPrompt):
            tool_messages: list[dict[str, str]] = list(prompt.messages)
        else:
            tool_messages = [{"role": "user", "content": prompt}]

        current_prompt = prompt_text
        tool_cache: dict[str, str] = {}

        logger.debug("Agent has tools (async): {}", [s["function"]["name"] for s in tool_schemas])

        llm_response: Any = None
        _caller_supports_structured = getattr(async_caller, "supports_structured", False)
        use_structured_tools = isinstance(prompt, StructuredPrompt) and (
            self.async_structured_llm_caller is not None or _caller_supports_structured
        )

        for iteration in range(self.config.max_tool_iterations):
            logger.debug("Tool calling iteration {} (async)", iteration + 1)

            is_last_iteration = iteration == self.config.max_tool_iterations - 1

            if use_structured_tools:
                llm_response = await async_caller(tool_messages, tools=tool_schemas)
            else:
                llm_response = await async_caller(current_prompt, tools=tool_schemas)

            if isinstance(llm_response, str):
                return llm_response, total_tokens + self.token_counter(llm_response)

            if use_structured_tools:
                prompt_tokens = sum(self.token_counter(m.get("content") or "") for m in tool_messages)
            else:
                prompt_tokens = self.token_counter(current_prompt)
            total_tokens += prompt_tokens
            if llm_response.content:
                total_tokens += self.token_counter(llm_response.content)

            if not llm_response.has_tool_calls:
                content = llm_response.content or ""
                if content:
                    logger.debug("No tool calls (async), returning content: {}...", content[:50])
                    return content, total_tokens
                logger.debug("No tool calls and empty content (async) — breaking to post-loop fallback")
                break

            # On the last iteration, if the LLM returned content alongside
            # tool_calls, return the content immediately.
            if is_last_iteration and llm_response.content:
                logger.debug("Last iteration (async) with content, returning: {}...", llm_response.content[:50])
                return llm_response.content, total_tokens

            tool_results: list[str] = []
            all_cached = True
            for tc in llm_response.tool_calls:
                import json as json_module

                cache_key = f"{tc.name}:{json_module.dumps(tc.arguments, sort_keys=True)}"

                if cache_key in tool_cache:
                    output = tool_cache[cache_key]
                    logger.debug("Tool cache hit (async): {}({}) -> {}...", tc.name, tc.arguments, output[:50])
                else:
                    all_cached = False
                    logger.debug("Executing tool (async): {}({})", tc.name, tc.arguments)
                    tool_call = ToolCall(name=tc.name, arguments=tc.arguments)

                    _cb = getattr(self, "_callback_manager", None)
                    _rid = getattr(self, "_current_run_id", None)
                    if _cb:
                        _cb.on_tool_start(
                            run_id=_rid,
                            tool_name=tc.name,
                            arguments=tc.arguments,
                        )
                    _t0 = _time.monotonic()
                    result = registry.execute(tool_call)
                    _elapsed = (_time.monotonic() - _t0) * 1000
                    output = result.output if result.success else f"Error: {result.error}"
                    tool_cache[cache_key] = output

                    if _cb:
                        if result.success:
                            _cb.on_tool_end(
                                run_id=_rid,
                                tool_name=tc.name,
                                success=True,
                                output_size=len(output),
                                duration_ms=_elapsed,
                            )
                        else:
                            _cb.on_tool_error(
                                run_id=_rid,
                                tool_name=tc.name,
                                error_type="ToolExecutionError",
                                error_message=result.error or "",
                            )

                    logger.debug("Tool result (async): {}...", output[:100])

                tool_results.append(f"[{tc.name}]: {output}")

            if all_cached and llm_response.tool_calls:
                logger.warning(
                    "All {} tool calls were cached duplicates (async) — model is looping, forcing final answer",
                    len(llm_response.tool_calls),
                )
                break

            tool_results_text = "\n".join(tool_results)

            if isinstance(prompt, StructuredPrompt):
                _used_native_format = False
                if (
                    llm_response.has_tool_calls
                    and hasattr(llm_response, "raw_response")
                    and llm_response.raw_response is not None
                ):
                    try:
                        raw_msg = llm_response.raw_response.choices[0].message
                        assistant_msg: dict[str, Any] = {
                            "role": "assistant",
                            "content": raw_msg.content,
                        }
                        if hasattr(raw_msg, "tool_calls") and raw_msg.tool_calls:
                            assistant_msg["tool_calls"] = [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                                for tc in raw_msg.tool_calls
                            ]
                        tool_messages.append(assistant_msg)

                        for i, tc in enumerate(llm_response.tool_calls):
                            raw_tc_id = raw_msg.tool_calls[i].id if raw_msg.tool_calls else tc.id
                            tool_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": raw_tc_id,
                                    "content": tool_results[i] if i < len(tool_results) else "",
                                }
                            )
                        _used_native_format = True
                    except Exception:  # noqa: BLE001
                        _used_native_format = False

                if not _used_native_format:
                    if llm_response.content:
                        tool_messages.append({"role": "assistant", "content": llm_response.content})
                    tool_messages.append({"role": "user", "content": f"Tool results:\n{tool_results_text}"})

            current_prompt += f"\n\nTool results:\n{tool_results_text}"

            # Last iteration: tool calls executed and results appended —
            # break out so the post-loop logic can force a final answer.
            if is_last_iteration:
                logger.debug("Last iteration (async) — tool calls executed, will force final answer")
                break

        # ------------------------------------------------------------------
        # Post-loop: force a final text answer if the LLM never produced one.
        # ------------------------------------------------------------------
        last_content = llm_response.content if llm_response else ""
        if last_content:
            return last_content, total_tokens

        logger.debug("Forcing final answer call without tool schemas (async)")
        _caller_max_tokens = getattr(async_caller, "max_tokens", 0) or 0
        _ctx_budget = _caller_max_tokens * 3 if _caller_max_tokens else 0
        try:
            if use_structured_tools:
                clean_messages = _strip_tool_metadata(tool_messages, max_total_chars=_ctx_budget)
                clean_messages.append({"role": "user", "content": "Now provide your final answer."})
                final_resp = await async_caller(clean_messages, tools=None)
            else:
                final_resp = await async_caller(current_prompt, tools=None)

            if isinstance(final_resp, str):
                if final_resp:
                    return final_resp, total_tokens + self.token_counter(final_resp)
            else:
                final_content = getattr(final_resp, "content", "") or ""
                if final_content:
                    return final_content, total_tokens + self.token_counter(final_content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Final summary call failed (async): {}", exc)

        for msg in reversed(tool_messages if use_structured_tools else []):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"], total_tokens
        return last_content, total_tokens

    @staticmethod
    def _looks_like_sentence(text: str) -> bool:
        """
        Return ``True`` if *text* already looks like a complete sentence.

        Heuristic: the first word is a pronoun or verb-phrase opener in any
        common language, so wrapping it with "You are ..." would produce a
        grammatically broken duplicate like "You are You are ..." or
        "You are Ты — старший аналитик.".

        Covers: EN, RU, ZH, JA, KO, DE, FR, ES, PT, IT, AR, HI, TR, PL, NL.
        """
        _sentence_prefixes = (
            # English
            "you are",
            "you're",
            "i am",
            "i'm",
            "we are",
            "we're",
            "he is",
            "she is",
            "they are",
            "it is",
            # Russian
            "ты ",
            "вы ",
            "я ",
            "мы ",
            "он ",
            "она ",
            "они ",
            # German
            "du bist",
            "sie sind",
            "ich bin",
            "wir sind",
            # French
            "tu es",
            "vous êtes",
            "je suis",
            "nous sommes",
            "vous etes",
            "vous êtes",
            # Spanish
            "tú eres",
            "usted es",
            "yo soy",
            "nosotros somos",
            "tu eres",
            # Portuguese
            "você é",
            "eu sou",
            "nós somos",
            "voce e",
            # Italian
            "tu sei",
            "lei è",
            "io sono",
            "noi siamo",
            "lei e",
            # Chinese (no spaces needed — startswith works on chars)
            "你是",
            "我是",
            "您是",
            "他是",
            "她是",
            "我们是",
            # Japanese
            "あなたは",
            "私は",
            "僕は",
            "俺は",
            # Korean
            "당신은",
            "나는",
            "저는",
            # Arabic
            "أنت ",
            "أنا ",
            "نحن ",
            # Hindi
            "तुम ",
            "आप ",
            "मैं ",
            "हम ",
            # Turkish
            "sen ",
            "siz ",
            "ben ",
            # Polish
            "ty ",
            "pan ",
            "ja ",
            "wy ",
            # Dutch
            "jij bent",
            "u bent",
            "ik ben",
            "wij zijn",
        )
        low = text.lower()
        return any(low.startswith(p) for p in _sentence_prefixes)

    def _build_system_prompt_parts(self: "MACPRunner", agent: Any) -> list[str]:
        """Build system prompt parts: persona, description, tools, output_schema."""
        import json as _json

        parts: list[str] = []
        if hasattr(agent, "persona") and agent.persona:
            persona = agent.persona.strip()
            if self._looks_like_sentence(persona):
                parts.append(persona if persona[-1] in ".!?" else f"{persona}.")
            else:
                parts.append(f"You are {persona}.")
        elif hasattr(agent, "role") and agent.role:
            role = agent.role.strip()
            if self._looks_like_sentence(role):
                parts.append(role if role[-1] in ".!?" else f"{role}.")
            else:
                parts.append(f"You are a {role}.")

        if hasattr(agent, "description") and agent.description:
            parts.append(agent.description.strip())

        # ── tools → brief mention so the agent is aware of its capabilities
        tool_names: list[str] = []
        if hasattr(agent, "get_tool_names"):
            tool_names = agent.get_tool_names()
        elif hasattr(agent, "tools") and agent.tools:
            tool_names = [t if isinstance(t, str) else getattr(t, "name", str(t)) for t in agent.tools]
        if tool_names:
            parts.append(f"Available tools: {', '.join(tool_names)}.")

        # ── output_schema → compact format instruction ───────────────
        output_schema_json = self._extract_schema_json(agent, "output_schema")
        if output_schema_json:
            schema_text = _json.dumps(output_schema_json, ensure_ascii=False, separators=(",", ":"))
            parts.append(f"Respond with JSON matching: {schema_text}")

        return parts

    def _build_user_prompt_parts(
        self: "MACPRunner",
        agent: Any,
        query: str,
        incoming_messages: dict[str, str],
        agent_names: dict[str, str],
        memory_context: list[dict[str, Any]] | None,
        *,
        include_query: bool,
    ) -> list[str]:
        """Build user prompt parts: query, input_schema, memory, incoming messages."""
        import json as _json

        user_parts: list[str] = []

        # Task query is added only if include_query=True
        if include_query and query:
            user_parts.append(f"Task: {query}")

        # Compact input schema hint.
        input_schema_json = self._extract_schema_json(agent, "input_schema")
        if input_schema_json:
            schema_text = _json.dumps(input_schema_json, ensure_ascii=False, separators=(",", ":"))
            user_parts.append(f"\nInput format: {schema_text}")

        # Include memory context from SharedMemoryPool
        if memory_context:
            user_parts.append("\nPrevious context:")
            for msg in memory_context:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                user_parts.append(f"[{role}]: {content}")

        if incoming_messages:
            user_parts.append("\nMessages from other agents:")
            for sender_id, message in incoming_messages.items():
                sender_name = agent_names.get(sender_id, sender_id)
                user_parts.append(f"\n[{sender_name}]:\n{message}")
        user_parts.append("\nProvide your response:")

        return user_parts

    def _build_state_text_parts(self: "MACPRunner", agent: Any) -> list[str]:
        """Build state text parts for flat string representation."""
        agent_state: list[dict[str, Any]] = []
        if hasattr(agent, "state"):
            agent_state = list(agent.state) if agent.state else []

        state_text_parts: list[str] = []
        if agent_state:
            state_text_parts.append("\nConversation history:")
            for entry in agent_state:
                entry_role = entry.get("role", "unknown")
                entry_content = entry.get("content", "")
                if entry_content:
                    state_text_parts.append(f"[{entry_role}]: {entry_content}")

        return state_text_parts

    def _build_structured_messages(
        self: "MACPRunner",
        system_prompt: str,
        agent_state: list[dict[str, Any]],
        user_content: str,
        flat_user: str,
        use_structured_state: bool,
    ) -> list[dict[str, str]]:
        """Build structured messages list for modern chat LLMs."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        if use_structured_state:
            # Replay agent.state as proper assistant/user turns so the
            # LLM sees real conversation history with correct roles.
            for entry in agent_state:
                entry_role = entry.get("role", "user")
                entry_content = entry.get("content", "")
                if entry_content:
                    # Map roles: "agent"/"assistant" → "assistant", everything else → "user"
                    msg_role = "assistant" if entry_role in ("agent", "assistant") else "user"
                    messages.append({"role": msg_role, "content": entry_content})
            # user_content does NOT contain state (it's in separate messages above)
            messages.append({"role": "user", "content": user_content})
        else:
            # Legacy path or no state: state is inlined in user_content
            messages.append({"role": "user", "content": flat_user})

        return messages

    def _build_prompt(
        self: "MACPRunner",
        agent: Any,
        query: str,
        incoming_messages: dict[str, str],
        agent_names: dict[str, str],
        memory_context: list[dict[str, Any]] | None = None,
        *,
        include_query: bool = True,
    ) -> StructuredPrompt:
        """
        Build the agent prompt with persona/description, state, schemas, memory, and messages.

        Returns a ``StructuredPrompt`` that carries **both** representations:

        * ``prompt.text``     — legacy flat string (backward-compatible)
        * ``prompt.messages`` — ``[{"role": "system", ...}, {"role": "user", ...}]``

        The system message includes:
        - persona / role identity
        - description
        - output_schema instructions (expected response format)

        The user message includes:
        - task query
        - agent conversation state (previous turns)
        - memory context from SharedMemoryPool
        - incoming messages from other agents
        - input_schema hint (expected input structure)

        When a ``structured_llm_caller`` is registered the runner sends
        ``prompt.messages`` directly to the LLM, giving it proper
        system/user role separation.  Otherwise ``prompt.text`` is used
        with the legacy ``llm_caller(str) -> str`` interface.

        Args:
            agent: Agent object with description/persona
            query: User query string
            incoming_messages: Messages from other agents
            agent_names: Mapping of agent IDs to names
            memory_context: Optional list of memory entries
            include_query: Whether to include the task query in the prompt.
                          Controlled via config.broadcast_task_to_all.

        """
        # Build system prompt
        system_parts = self._build_system_prompt_parts(agent)
        system_prompt = "\n\n".join(system_parts) if system_parts else "You are a helpful assistant."

        # Build user prompt parts
        user_parts = self._build_user_prompt_parts(
            agent, query, incoming_messages, agent_names, memory_context, include_query=include_query
        )
        user_content = "".join(user_parts)

        # Build state text parts
        agent_state: list[dict[str, Any]] = []
        if hasattr(agent, "state"):
            agent_state = list(agent.state) if agent.state else []

        use_structured_state = bool(agent_state) and self.structured_llm_caller is not None
        state_text_parts = self._build_state_text_parts(agent)

        # Build flat string (legacy)
        flat_user = "".join(state_text_parts) + user_content if state_text_parts else user_content
        flat = f"{system_prompt}\n\n{flat_user}"

        # Build structured messages (modern)
        messages = self._build_structured_messages(
            system_prompt, agent_state, user_content, flat_user, use_structured_state
        )

        return StructuredPrompt(text=flat, messages=messages)

    @staticmethod
    def _extract_schema_json(agent: Any, attr: str) -> dict[str, Any] | None:
        """
        Extract a JSON Schema dict from an agent's schema attribute.

        Supports:
        - ``dict`` — returned as-is (already a JSON Schema)
        - Pydantic ``BaseModel`` subclass — converted via ``model_json_schema()``
        - ``None`` / missing — returns ``None``
        """
        schema = getattr(agent, attr, None)
        if schema is None:
            return None
        if isinstance(schema, dict):
            return schema
        # Pydantic model class
        try:
            from pydantic import BaseModel

            if isinstance(schema, type) and issubclass(schema, BaseModel):
                return schema.model_json_schema()
        except Exception as exc:  # noqa: BLE001
            # Pydantic may not be available or schema may not be a BaseModel
            # This is expected in some cases, so we silently continue
            _ = exc  # Suppress unused variable warning
        return None

    def _execute_step(
        self: "MACPRunner",
        step: Any,
        messages: dict[str, str],
        agent_lookup: dict[str, Any],
        agent_names: dict[str, str],
        query: str,
    ) -> StepResult:
        """
        Execute a step synchronously with retries and token counting.

        Supports multi-model: uses the caller for the specific agent.
        """
        agent = agent_lookup.get(step.agent_id)
        if agent is None:
            return StepResult(
                agent_id=step.agent_id,
                success=False,
                error=f"Agent '{step.agent_id}' not found",
            )

        # Get caller for this specific agent (multi-model support)
        caller = self._get_caller_for_agent(step.agent_id, agent)
        if caller is None:
            return StepResult(
                agent_id=step.agent_id,
                success=False,
                error=f"No LLM caller available for agent '{step.agent_id}'",
            )

        incoming = {p: messages[p] for p in step.predecessors if p in messages}
        memory_context = self._get_memory_context(step.agent_id)
        prompt = self._build_prompt(agent, query, incoming, agent_names, memory_context)

        if self._budget_tracker:
            can, reason = self._budget_tracker.can_execute(step.agent_id)
            if not can:
                if self._callback_manager:
                    self._callback_manager.on_budget_exceeded(
                        run_id=self._run_id,
                        budget_type="pre_step",
                        current=float(self._budget_tracker.global_tokens.used),
                        limit=float(self._budget_tracker.global_tokens.limit or 0),
                        action_taken="step_blocked",
                    )
                return StepResult(agent_id=step.agent_id, success=False, error=reason)

        last_error = None
        delay = self.config.retry_delay

        for attempt in range(self.config.max_retries + 1):
            try:
                # Execute with tools support
                response, tokens = self._run_agent_with_tools(
                    caller=caller,
                    prompt=prompt,
                    agent=agent,
                )

                quality = 1.0
                if self._scheduler and self._scheduler.pruning.quality_scorer:
                    quality = self._scheduler.pruning.quality_scorer(response)

                return StepResult(
                    agent_id=step.agent_id,
                    success=True,
                    response=response,
                    tokens_used=tokens,
                    quality_score=quality,
                )
            except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                last_error = str(e)
                if attempt < self.config.max_retries:
                    if self._callback_manager:
                        self._callback_manager.on_retry(
                            run_id=self._run_id,
                            agent_id=step.agent_id,
                            attempt=attempt + 1,
                            max_attempts=self.config.max_retries + 1,
                            delay_ms=delay * 1000,
                            error=last_error,
                        )
                    time.sleep(delay)
                    delay *= self.config.retry_backoff

        return StepResult(
            agent_id=step.agent_id,
            success=False,
            error=last_error,
        )

    async def _execute_step_async(
        self: "MACPRunner",
        step: Any,
        messages: dict[str, str],
        agent_lookup: dict[str, Any],
        agent_names: dict[str, str],
        query: str,
    ) -> StepResult:
        """
        Execute a step asynchronously with retries and timeout.

        Supports multi-model: uses the async caller for the specific agent.
        Uses :meth:`_run_agent_with_tools_async` so that agents with tools
        go through the full tool-calling loop (identical to the sync path).
        """
        agent = agent_lookup.get(step.agent_id)
        if agent is None:
            return StepResult(
                agent_id=step.agent_id,
                success=False,
                error=f"Agent '{step.agent_id}' not found",
            )

        # Get async caller for this specific agent (multi-model support).
        # _get_async_caller_for_agent may return None when only
        # async_structured_llm_caller is configured — _acall_llm handles
        # that case internally, so we only error when *neither* is available.
        async_caller = self._get_async_caller_for_agent(step.agent_id, agent)
        if async_caller is None and self.async_structured_llm_caller is None:
            return StepResult(
                agent_id=step.agent_id,
                success=False,
                error=f"No async LLM caller available for agent '{step.agent_id}'",
            )

        incoming = {p: messages[p] for p in step.predecessors if p in messages}
        memory_context = self._get_memory_context(step.agent_id)
        prompt = self._build_prompt(agent, query, incoming, agent_names, memory_context)

        if self._budget_tracker:
            can, reason = self._budget_tracker.can_execute(step.agent_id)
            if not can:
                if self._callback_manager:
                    self._callback_manager.on_budget_exceeded(
                        run_id=self._run_id,
                        budget_type="pre_step",
                        current=float(self._budget_tracker.global_tokens.used),
                        limit=float(self._budget_tracker.global_tokens.limit or 0),
                        action_taken="step_blocked",
                    )
                return StepResult(agent_id=step.agent_id, success=False, error=reason)

        last_error = None
        delay = self.config.retry_delay

        for attempt in range(self.config.max_retries + 1):
            try:
                response, tokens = await asyncio.wait_for(
                    self._run_agent_with_tools_async(
                        async_caller=async_caller,
                        prompt=prompt,
                        agent=agent,
                    ),
                    timeout=self.config.timeout,
                )

                quality = 1.0
                if self._scheduler and self._scheduler.pruning.quality_scorer:
                    quality = self._scheduler.pruning.quality_scorer(response)

                return StepResult(
                    agent_id=step.agent_id,
                    success=True,
                    response=response,
                    tokens_used=tokens,
                    quality_score=quality,
                )
            except TimeoutError:
                last_error = f"Timeout after {self.config.timeout}s"
            except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                last_error = str(e)

            if attempt < self.config.max_retries:
                if self._callback_manager:
                    self._callback_manager.on_retry(
                        run_id=self._run_id,
                        agent_id=step.agent_id,
                        attempt=attempt + 1,
                        max_attempts=self.config.max_retries + 1,
                        delay_ms=delay * 1000,
                        error=last_error or "",
                    )
                await asyncio.sleep(delay)
                delay *= self.config.retry_backoff

        return StepResult(
            agent_id=step.agent_id,
            success=False,
            error=last_error,
        )

    async def _execute_parallel(
        self: "MACPRunner",
        steps: list[Any],
        messages: dict[str, str],
        agent_lookup: dict[str, Any],
        agent_names: dict[str, str],
        query: str,
    ) -> list[tuple[Any, StepResult]]:
        """Execute a group of steps in parallel asynchronously."""
        tasks = [self._execute_step_async(step, messages, agent_lookup, agent_names, query) for step in steps]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = []
        for step, result in zip(steps, results, strict=False):
            if isinstance(result, Exception):
                sr = StepResult(
                    agent_id=step.agent_id,
                    success=False,
                    error=str(result),
                )
            else:
                sr = result
            output.append((step, sr))

        return output

    def _get_parallel_group(
        self: "MACPRunner",
        plan: ExecutionPlan,
        completed_agents: Any,
    ) -> list[Any]:
        """Return a group of steps ready for parallel execution."""
        del completed_agents
        group: list[Any] = []

        for step in plan.remaining_steps:
            if step.agent_id in plan.skipped or plan.is_condition_skipped(step):
                continue

            predecessors_done = all(
                p in plan.completed_step_ids or p in plan.skipped_step_ids for p in step.dependency_ids
            )

            if predecessors_done:
                group.append(step)
                if len(group) >= self.config.max_parallel_size:
                    break

        return group

    def _determine_final_agent(
        self: "MACPRunner",
        requested: str | None,
        exec_order: list[str],
        messages: dict[str, str],
    ) -> tuple[str, bool]:
        """Return ``(final_agent_id, is_fallback)``."""
        if requested and requested in messages:
            return requested, False
        if exec_order:
            return exec_order[-1], bool(requested)
        return "", bool(requested)

    def _build_agent_states(
        self: "MACPRunner",
        messages: dict[str, str],
        agent_lookup: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Build updated agent states by appending responses to history."""
        states: dict[str, list[dict[str, Any]]] = {}
        for agent_id, response in messages.items():
            agent = agent_lookup.get(agent_id)
            if agent is not None:
                new_state = list(getattr(agent, "state", []))
                new_state.append({"role": "assistant", "content": response})
                states[agent_id] = new_state
        return states

    def _collect_hidden_states(
        self: "MACPRunner",
        agent_lookup: dict[str, Any],
    ) -> dict[str, HiddenState]:
        """Collect the current hidden_state/embedding of agents into a dictionary."""
        hidden_states: dict[str, HiddenState] = {}
        for agent_id, agent in agent_lookup.items():
            hs = HiddenState()
            if hasattr(agent, "hidden_state") and agent.hidden_state is not None:
                hs.tensor = agent.hidden_state
            if hasattr(agent, "embedding") and agent.embedding is not None:
                hs.embedding = agent.embedding
            if hs.tensor is not None or hs.embedding is not None:
                hidden_states[agent_id] = hs
        return hidden_states

    def _combine_hidden_states(
        self: "MACPRunner",
        states: list[HiddenState],
    ) -> HiddenState | None:
        """Combine a list of hidden states according to the hidden_combine_strategy."""
        if not states:
            return None

        tensors = [s.tensor for s in states if s.tensor is not None]
        embeddings = [s.embedding for s in states if s.embedding is not None]

        combined = HiddenState()

        if tensors:
            combined.tensor = self._combine_tensors(tensors)

        if embeddings:
            combined.embedding = self._combine_tensors(embeddings)

        return combined if (combined.tensor is not None or combined.embedding is not None) else None

    def _combine_tensors(self: "MACPRunner", tensors: list[torch.Tensor]) -> torch.Tensor:
        """Combine a list of tensors according to the strategy (mean/sum/concat/attention)."""
        if len(tensors) == 1:
            return tensors[0]

        stacked = torch.stack(tensors)

        if self.config.hidden_combine_strategy == "mean":
            return stacked.mean(dim=0)
        if self.config.hidden_combine_strategy == "sum":
            return stacked.sum(dim=0)
        if self.config.hidden_combine_strategy == "concat":
            return torch.cat(tensors, dim=-1)
        if self.config.hidden_combine_strategy == "attention":
            weights = torch.softmax(torch.ones(len(tensors)), dim=0)
            return (stacked * weights.view(-1, *([1] * (stacked.dim() - 1)))).sum(dim=0)
        return stacked.mean(dim=0)

    def _get_incoming_hidden(
        self: "MACPRunner",
        _agent_id: str,
        incoming_ids: list[str],
        hidden_states: dict[str, HiddenState],
    ) -> HiddenState | None:
        """Get the combined hidden state of predecessors."""
        if not self.config.enable_hidden_channels:
            return None

        incoming_states = [hidden_states[aid] for aid in incoming_ids if aid in hidden_states]

        return self._combine_hidden_states(incoming_states)

    def _update_agent_hidden_state(
        self: "MACPRunner",
        agent: Any,
        response: str,
        incoming_hidden: HiddenState | None,
        hidden_encoder: Any | None = None,
    ) -> HiddenState:
        """Update the agent's hidden_state based on the response and incoming hidden state."""
        new_hidden = HiddenState()

        if hasattr(agent, "embedding") and agent.embedding is not None:
            new_hidden.embedding = agent.embedding

        if hidden_encoder is not None:
            try:
                encoded = hidden_encoder.encode([response])
                if isinstance(encoded, torch.Tensor) and encoded.numel() > 0:
                    new_hidden.tensor = encoded[0]
            except (ValueError, TypeError, RuntimeError):
                pass  # Ignore encoding errors

        if new_hidden.tensor is None and incoming_hidden is not None:
            new_hidden.tensor = incoming_hidden.tensor

        new_hidden.metadata = {
            "last_response_length": len(response),
            "has_incoming": incoming_hidden is not None,
        }

        return new_hidden

    def run_round_with_hidden(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None = None,
        hidden_encoder: Any | None = None,
    ) -> MACPResult:
        """
        Synchronous round with hidden state transfer between agents.

        Supports multi-model: each agent uses its own LLM caller.
        Supports conditional edge evaluation.
        """
        if not self._has_any_caller():
            msg = "llm_caller, llm_callers, or llm_factory is required"
            raise ValueError(msg)

        original_hidden_setting = self.config.enable_hidden_channels
        self.config.enable_hidden_channels = True

        start_time = time.time()

        try:
            base = self._prepare_base_context(role_graph)
        except Exception:
            self.config.enable_hidden_channels = original_hidden_setting
            raise

        if base is None:
            self.config.enable_hidden_channels = original_hidden_setting
            return MACPResult(messages={}, final_answer="", final_agent_id="", execution_order=[])

        task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        # Initialize memory
        self._init_memory(agent_ids)

        hidden_states = self._collect_hidden_states(agent_lookup)

        messages: dict[str, str] = {}
        step_results: dict[str, StepResult] = {}
        execution_order: list[str] = []
        fallback_attempts: dict[str, int] = {}
        topology_changed_count = 0
        fallback_count = 0
        pruned_agents: list[str] = []
        errors: list[ExecutionError] = []
        total_tokens = 0

        # Adaptive mode: plan + conditional edge evaluation after each step
        if self.config.adaptive and self._scheduler is not None:
            p_matrix = self._extract_p_matrix(role_graph, task_idx)
            edge_conditions = self._get_edge_conditions(role_graph)

            condition_ctx = ConditionContext(
                source_agent="",
                target_agent="",
                messages={},
                step_results={},
                query=query,
            )

            plan = self._scheduler.build_plan(
                a_agents,
                agent_ids,
                p_matrix,
                start_agent=None,
                end_agent=final_agent_id,
                edge_conditions=edge_conditions,
                condition_context=condition_ctx,
                filter_unreachable=True,
            )

            while not plan.is_complete:
                step = plan.get_current_step()

                if step is None:
                    break

                if plan.is_condition_skipped(step):
                    plan.advance()
                    continue

                should_prune, reason = self._scheduler.should_prune(
                    step, plan, step_results.get(execution_order[-1]) if execution_order else None
                )

                if should_prune:
                    plan.mark_skipped(step)
                    pruned_agents.append(step.agent_id)
                    errors.append(
                        ExecutionError(
                            message=f"Pruned: {reason}",
                            agent_id=step.agent_id,
                            recoverable=False,
                        )
                    )
                    continue

                agent_id = step.agent_id
                agent = agent_lookup.get(agent_id)

                if agent is None:
                    plan.advance()
                    continue

                incoming_ids = get_incoming_agents(agent_id, a_agents, agent_ids)
                incoming_messages = {aid: messages[aid] for aid in incoming_ids if aid in messages}
                incoming_hidden = self._get_incoming_hidden(agent_id, incoming_ids, hidden_states)

                memory_context = self._get_memory_context(agent_id)
                prompt = self._build_prompt(agent, query, incoming_messages, agent_names, memory_context)

                if incoming_hidden and incoming_hidden.metadata:
                    context_hint = self._format_hidden_context(incoming_hidden)

                    if context_hint:
                        suffix = f"\n\n[Context: {context_hint}]"
                        prompt = StructuredPrompt(
                            text=prompt.text + suffix,
                            messages=[
                                *prompt.messages[:-1],
                                {"role": "user", "content": prompt.messages[-1]["content"] + suffix},
                            ],
                        )

                try:
                    caller = self._get_caller_for_agent(agent_id, agent)

                    if caller is None:
                        error_msg = f"No LLM caller available for agent {agent_id}"
                        messages[agent_id] = f"[Error: {error_msg}]"
                        result = StepResult(agent_id=agent_id, success=False, error=error_msg)
                        step_results[agent_id] = result
                        execution_order.append(agent_id)
                        plan.mark_failed(step)
                        errors.append(
                            ExecutionError(
                                message=error_msg,
                                agent_id=agent_id,
                                recoverable=True,
                            )
                        )

                        # Fallback when caller is missing
                        attempts = fallback_attempts.get(agent_id, 0)
                        if self._scheduler.should_use_fallback(step, result, attempts):
                            for fb_agent in step.fallback_agents:
                                if fb_agent not in plan.completed and fb_agent not in plan.failed:
                                    plan.insert_fallback(fb_agent, plan.get_step_index(step))
                                    fallback_count += 1
                                    break
                            fallback_attempts[agent_id] = attempts + 1
                        continue

                    response, tokens = self._run_agent_with_tools(
                        caller=caller,
                        prompt=prompt,
                        agent=agent,
                    )
                    messages[agent_id] = response
                    total_tokens += tokens
                    execution_order.append(agent_id)
                    self._save_to_memory(agent_id, response, incoming_ids)

                    hidden_states[agent_id] = self._update_agent_hidden_state(
                        agent, response, incoming_hidden, hidden_encoder
                    )

                    result = StepResult(
                        agent_id=agent_id,
                        success=True,
                        response=response,
                        tokens_used=tokens,
                    )
                    step_results[agent_id] = result
                    plan.mark_completed(step, tokens)

                except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                    messages[agent_id] = f"[Error: {e}]"
                    result = StepResult(agent_id=agent_id, success=False, error=str(e))
                    step_results[agent_id] = result
                    execution_order.append(agent_id)
                    plan.mark_failed(step)
                    errors.append(
                        ExecutionError(
                            message=str(e),
                            agent_id=agent_id,
                            recoverable=True,
                        )
                    )

                    # Fallback on execution error
                    attempts = fallback_attempts.get(agent_id, 0)
                    if self._scheduler.should_use_fallback(step, result, attempts):
                        for fb_agent in step.fallback_agents:
                            if fb_agent not in plan.completed and fb_agent not in plan.failed:
                                plan.insert_fallback(fb_agent, plan.get_step_index(step))
                                fallback_count += 1
                                break
                        fallback_attempts[agent_id] = attempts + 1

                # Topology pipeline: conditional edges + user hooks → plan
                if self._run_topology_pipeline(
                    plan,
                    agent_id,
                    a_agents,
                    agent_ids,
                    step_results,
                    messages,
                    query,
                    execution_order,
                    total_tokens,
                    role_graph,
                ):
                    topology_changed_count += 1

        else:
            # Non-adaptive mode: execute the plan linearly
            exec_order = build_execution_order(a_agents, agent_ids, role_graph.role_sequence)

            for agent_id in exec_order:
                agent = agent_lookup.get(agent_id)
                if agent is None:
                    continue

                incoming_ids = get_incoming_agents(agent_id, a_agents, agent_ids)
                incoming_messages = {aid: messages[aid] for aid in incoming_ids if aid in messages}
                incoming_hidden = self._get_incoming_hidden(agent_id, incoming_ids, hidden_states)

                memory_context = self._get_memory_context(agent_id)
                prompt = self._build_prompt(agent, query, incoming_messages, agent_names, memory_context)

                if incoming_hidden and incoming_hidden.metadata:
                    context_hint = self._format_hidden_context(incoming_hidden)

                    if context_hint:
                        suffix = f"\n\n[Context: {context_hint}]"
                        prompt = StructuredPrompt(
                            text=prompt.text + suffix,
                            messages=[
                                *prompt.messages[:-1],
                                {"role": "user", "content": prompt.messages[-1]["content"] + suffix},
                            ],
                        )

                try:
                    caller = self._get_caller_for_agent(agent_id, agent)

                    if caller is None:
                        error_msg = f"No LLM caller available for agent {agent_id}"
                        messages[agent_id] = f"[Error: {error_msg}]"
                        result = StepResult(agent_id=agent_id, success=False, error=error_msg)
                        step_results[agent_id] = result
                        execution_order.append(agent_id)
                        errors.append(
                            ExecutionError(
                                message=error_msg,
                                agent_id=agent_id,
                                recoverable=True,
                            )
                        )
                        continue

                    response, tokens = self._run_agent_with_tools(
                        caller=caller,
                        prompt=prompt,
                        agent=agent,
                    )
                    messages[agent_id] = response
                    total_tokens += tokens
                    execution_order.append(agent_id)
                    self._save_to_memory(agent_id, response, incoming_ids)

                    hidden_states[agent_id] = self._update_agent_hidden_state(
                        agent, response, incoming_hidden, hidden_encoder
                    )
                    result = StepResult(
                        agent_id=agent_id,
                        success=True,
                        response=response,
                        tokens_used=tokens,
                    )
                    step_results[agent_id] = result
                except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                    messages[agent_id] = f"[Error: {e}]"
                    result = StepResult(agent_id=agent_id, success=False, error=str(e))
                    step_results[agent_id] = result
                    execution_order.append(agent_id)
                    errors.append(
                        ExecutionError(
                            message=str(e),
                            agent_id=agent_id,
                            recoverable=True,
                        )
                    )

        final_id, _final_missed = self._determine_final_agent(final_agent_id, execution_order, messages)
        agent_states = self._build_agent_states(messages, agent_lookup)

        result = MACPResult(
            messages=messages,
            final_answer=messages.get(final_id, ""),
            final_agent_id=final_id,
            execution_order=execution_order,
            topology_changed_count=topology_changed_count,
            fallback_count=fallback_count,
            agent_states=agent_states,
            step_results=step_results,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            pruned_agents=pruned_agents,
            errors=errors or None,
            hidden_states=hidden_states,
        )
        self.config.enable_hidden_channels = original_hidden_setting
        return result

    def _format_hidden_context(self: "MACPRunner", hidden: HiddenState) -> str:
        """Format hidden state metadata for inclusion in the prompt."""
        parts = []
        if hidden.metadata and "last_response_length" in hidden.metadata:
            parts.append(f"previous response length: {hidden.metadata['last_response_length']}")
        return ", ".join(parts) if parts else ""

    # =========================================================================
    # STREAMING EXECUTION METHODS
    # =========================================================================
