"""Streaming execution entrypoints and event-producing execution paths."""

from typing import TYPE_CHECKING, cast

from .shared import (
    AgentErrorEvent,
    AgentOutputEvent,
    AgentStartEvent,
    Any,
    AsyncIterator,
    ConditionContext,
    ExecutionError,
    FallbackEvent,
    Iterator,
    MACPResult,
    ParallelEndEvent,
    ParallelStartEvent,
    PruneEvent,
    RunEndEvent,
    RunStartEvent,
    StepResult,
    StreamEvent,
    StructuredPrompt,
    TokenEvent,
    TopologyChangedEvent,
    asyncio,
    build_execution_order,
    get_incoming_agents,
    get_parallel_groups,
    time,
    uuid,
)

if TYPE_CHECKING:
    from . import MACPRunner


class RunnerStreamMixin:
    def stream(
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None = None,
        *,
        update_states: bool | None = None,
    ) -> Iterator[StreamEvent]:
        """
        Stream execution events for real-time output.

        Yields events as agents are executed, allowing real-time monitoring
        and display of intermediate results.

        Args:
            role_graph: The RoleGraph to execute
            final_agent_id: Override which agent produces final answer
            update_states: Whether to update agent states after execution

        Yields:
            StreamEvent instances for each execution phase

        Example:
            for event in runner.stream(graph):
                if event.event_type == StreamEventType.AGENT_OUTPUT:
                    print(f"{event.agent_id}: {event.content}")
                elif event.event_type == StreamEventType.TOKEN:
                    print(event.token, end="", flush=True)

        """
        if not self._has_any_caller() and self.streaming_llm_caller is None:
            msg = "llm_caller, llm_callers, llm_factory, or streaming_llm_caller required for streaming"
            raise ValueError(msg)

        if self.config.adaptive:
            yield from self._stream_adaptive(role_graph, final_agent_id, update_states=update_states)
        else:
            yield from self._stream_simple(role_graph, final_agent_id, update_states=update_states)

    async def astream(
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None = None,
        *,
        update_states: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        Async streaming execution for real-time output.

        Async version of stream() for use in async contexts.

        Args:
            role_graph: The RoleGraph to execute
            final_agent_id: Override which agent produces final answer
            update_states: Whether to update agent states after execution

        Yields:
            StreamEvent instances for each execution phase

        Example:
            async for event in runner.astream(graph):
                match event.event_type:
                    case StreamEventType.AGENT_START:
                        print(f"Agent {event.agent_id} started")
                    case StreamEventType.AGENT_OUTPUT:
                        print(f"Output: {event.content}")

        """
        if not self._has_any_async_caller() and self.async_streaming_llm_caller is None:
            msg = "async_llm_caller, async_llm_callers, llm_factory, or async_streaming_llm_caller required"
            raise ValueError(msg)

        if self.config.adaptive:
            async for event in self._astream_adaptive(role_graph, final_agent_id, update_states=update_states):
                yield event
        else:
            async for event in self._astream_simple(role_graph, final_agent_id, update_states=update_states):
                yield event

    def _stream_simple(  # noqa: PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        *,
        update_states: bool | None,
    ) -> Iterator[StreamEvent]:
        """Simple sequential streaming execution."""
        run_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            yield RunEndEvent(
                run_id=run_id, success=True, final_answer="", final_agent_id="", total_time=time.time() - start_time
            )
            return

        task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base
        del task_idx  # not used directly in simple streaming

        exec_order = build_execution_order(a_agents, agent_ids, role_graph.role_sequence)
        self._init_memory(agent_ids)

        task_connected = self._get_task_connected_agents(role_graph)

        # Emit run start
        yield RunStartEvent(
            run_id=run_id,
            query=query,
            num_agents=len(exec_order),
            execution_order=exec_order,
            config_summary={
                "adaptive": False,
                "timeout": self.config.timeout,
                "enable_memory": self.config.enable_memory,
                "broadcast_task_to_all": self.config.broadcast_task_to_all,
            },
        )

        messages: dict[str, str] = {}
        total_tokens = 0
        errors: list[str] = []

        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        for step_idx, agent_id in enumerate(exec_order):
            if agent_id in disabled_nodes:
                continue

            agent = agent_lookup.get(agent_id)
            if agent is None:
                continue

            agent_name = agent_names.get(agent_id, agent_id)
            incoming_ids = get_incoming_agents(agent_id, a_agents, agent_ids)
            incoming_messages = {aid: messages[aid] for aid in incoming_ids if aid in messages}

            include_query = self._should_include_query(agent_id, task_connected)
            memory_context = self._get_memory_context(agent_id)
            prompt = self._build_prompt(
                agent, query, incoming_messages, agent_names, memory_context, include_query=include_query
            )

            # prompt is now a StructuredPrompt — use .text for preview/streaming
            prompt_text = prompt.text

            # Emit agent start
            yield AgentStartEvent(
                run_id=run_id,
                agent_id=agent_id,
                agent_name=agent_name,
                step_index=step_idx,
                predecessors=incoming_ids,
                prompt_preview=prompt_text[: self.config.prompt_preview_length],
            )

            step_start = time.time()

            try:
                # Get caller for this specific agent (multi-model support)
                caller = self._get_caller_for_agent(agent_id, agent)
                if caller is None:
                    error_msg = f"No LLM caller available for agent {agent_id}"
                    errors.append(f"{agent_id}: {error_msg}")
                    messages[agent_id] = f"[Error: {error_msg}]"
                    yield AgentErrorEvent(
                        run_id=run_id,
                        agent_id=agent_id,
                        error_type="ValueError",
                        error_message=error_msg,
                        will_retry=False,
                    )
                    continue

                # Use streaming LLM if available and enabled
                if self.streaming_llm_caller and self.config.enable_token_streaming:
                    response_parts: list[str] = []
                    token_idx = 0

                    for token in self.streaming_llm_caller(prompt_text):
                        response_parts.append(token)
                        yield TokenEvent(
                            run_id=run_id,
                            agent_id=agent_id,
                            token=token,
                            token_index=token_idx,
                            is_first=(token_idx == 0),
                            is_last=False,
                        )
                        token_idx += 1

                    # Mark last token
                    if response_parts:
                        yield TokenEvent(
                            run_id=run_id,
                            agent_id=agent_id,
                            token="",
                            token_index=token_idx,
                            is_first=False,
                            is_last=True,
                        )

                    response = "".join(response_parts)
                    tokens = self.token_counter(prompt_text) + self.token_counter(response)
                else:
                    # Use regular LLM caller for this agent (with tools support)
                    # prompt (StructuredPrompt) is passed through — _run_agent_with_tools
                    # dispatches via _call_llm when structured_llm_caller is available
                    response, tokens = self._run_agent_with_tools(
                        caller=caller,
                        prompt=prompt,
                        agent=agent,
                    )

                messages[agent_id] = response
                total_tokens += tokens
                self._save_to_memory(agent_id, response, incoming_ids)

                is_final = (step_idx == len(exec_order) - 1) or (agent_id == final_agent_id)

                yield AgentOutputEvent(
                    run_id=run_id,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    content=response,
                    tokens_used=tokens,
                    duration_ms=(time.time() - step_start) * 1000,
                    is_final=is_final,
                )

            except (TimeoutError, ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                error_msg = str(e)
                errors.append(f"{agent_id}: {error_msg}")
                messages[agent_id] = f"[Error: {e}]"

                yield AgentErrorEvent(
                    run_id=run_id,
                    agent_id=agent_id,
                    error_type=type(e).__name__,
                    error_message=error_msg,
                    will_retry=False,
                )

        final_id, final_missed = self._determine_final_agent(final_agent_id, exec_order, messages)

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        yield RunEndEvent(
            run_id=run_id,
            success=len(errors) == 0 and not final_missed,
            final_answer=messages.get(final_id, ""),
            final_agent_id=final_id,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            executed_agents=list(messages.keys()),
            errors=errors,
            agent_states=agent_states,
        )

    async def _astream_simple(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        *,
        update_states: bool | None,
    ) -> AsyncIterator[StreamEvent]:
        """
        Async streaming execution with optional parallel support.

        When ``config.enable_parallel`` is ``True``, independent agents
        (those whose predecessors have all completed) are executed
        concurrently via ``asyncio.gather``.  This is determined by
        :func:`get_parallel_groups` which partitions the topological
        order into dependency-based levels.

        When ``enable_parallel`` is ``False`` (or the graph is purely
        sequential), agents are executed one-by-one — identical to the
        synchronous ``_stream_simple`` but using async I/O.
        """
        run_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            yield RunEndEvent(
                run_id=run_id, success=True, final_answer="", final_agent_id="", total_time=time.time() - start_time
            )
            return

        _task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        exec_order = build_execution_order(a_agents, agent_ids, role_graph.role_sequence)
        self._init_memory(agent_ids)

        # When parallel execution is enabled, partition agents into
        # dependency-based groups so independent agents run concurrently.
        if self.config.enable_parallel:
            groups = get_parallel_groups(a_agents, agent_ids)
        else:
            # Each agent is its own group — strictly sequential.
            groups = [[aid] for aid in exec_order]

        yield RunStartEvent(
            run_id=run_id,
            query=query,
            num_agents=len(exec_order),
            execution_order=exec_order,
            config_summary={
                "parallel": self.config.enable_parallel,
            },
        )

        messages: dict[str, str] = {}
        total_tokens = 0
        errors: list[str] = []
        step_idx = 0

        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        for group_idx, group in enumerate(groups):
            # Filter out unknown and disabled agents
            group_agents = [aid for aid in group if agent_lookup.get(aid) is not None and aid not in disabled_nodes]
            if not group_agents:
                continue

            # Emit ParallelStartEvent when the group has >1 agent
            is_parallel_group = self.config.enable_parallel and len(group_agents) > 1
            if is_parallel_group:
                yield ParallelStartEvent(
                    run_id=run_id,
                    agent_ids=group_agents,
                    group_index=group_idx,
                )

            # Emit AgentStartEvent for every agent in the group
            agent_prompts: dict[str, StructuredPrompt] = {}
            for agent_id in group_agents:
                agent = agent_lookup[agent_id]
                agent_name = agent_names.get(agent_id, agent_id)
                incoming_ids = get_incoming_agents(agent_id, a_agents, agent_ids)
                incoming_messages = {aid: messages[aid] for aid in incoming_ids if aid in messages}
                memory_context = self._get_memory_context(agent_id)
                prompt = self._build_prompt(agent, query, incoming_messages, agent_names, memory_context)
                agent_prompts[agent_id] = prompt

                yield AgentStartEvent(
                    run_id=run_id,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    step_index=step_idx,
                    predecessors=incoming_ids,
                    prompt_preview=prompt.text[: self.config.prompt_preview_length],
                )
                step_idx += 1

            # Execute the group.
            if is_parallel_group:
                # Run all agents in the group concurrently
                async def _call_agent(aid: str, prompt: StructuredPrompt) -> tuple[str, str | None, int, str | None]:
                    """Returns (agent_id, response, tokens, error)."""
                    try:
                        _agent = agent_lookup[aid]
                        _caller = self._get_async_caller_for_agent(aid, _agent)
                        resp, toks = await asyncio.wait_for(
                            self._run_agent_with_tools_async(
                                async_caller=_caller,
                                prompt=prompt,
                                agent=_agent,
                            ),
                            timeout=self.config.timeout,
                        )
                    except (
                        TimeoutError,
                        ExecutionError,
                        ValueError,
                        TypeError,
                        KeyError,
                        RuntimeError,
                        OSError,
                    ) as exc:
                        return (aid, None, 0, str(exc))
                    else:
                        return (aid, resp, toks, None)

                results = await asyncio.gather(*[_call_agent(aid, agent_prompts[aid]) for aid in group_agents])

                successful: list[str] = []
                failed: list[str] = []
                for agent_id, response, tokens, error in results:
                    agent_name = agent_names.get(agent_id, agent_id)
                    if error is None and response is not None:
                        messages[agent_id] = response
                        total_tokens += tokens
                        incoming_ids = get_incoming_agents(agent_id, a_agents, agent_ids)
                        self._save_to_memory(agent_id, response, incoming_ids)
                        successful.append(agent_id)

                        yield AgentOutputEvent(
                            run_id=run_id,
                            agent_id=agent_id,
                            agent_name=agent_name,
                            content=response,
                            tokens_used=tokens,
                            is_final=(agent_id == final_agent_id),
                        )
                    else:
                        errors.append(f"{agent_id}: {error}")
                        messages[agent_id] = f"[Error: {error}]"
                        failed.append(agent_id)

                        yield AgentErrorEvent(
                            run_id=run_id,
                            agent_id=agent_id,
                            error_type="ExecutionError",
                            error_message=error or "Unknown error",
                        )

                yield ParallelEndEvent(
                    run_id=run_id,
                    agent_ids=group_agents,
                    group_index=group_idx,
                    successful=successful,
                    failed=failed,
                )
            else:
                # Sequential execution — one agent at a time
                for agent_id in group_agents:
                    agent_name = agent_names.get(agent_id, agent_id)
                    prompt = agent_prompts[agent_id]
                    step_start = time.time()

                    try:
                        # Use async streaming LLM if available
                        if self.async_streaming_llm_caller and self.config.enable_token_streaming:
                            response_parts: list[str] = []
                            token_idx = 0

                            async for token in self.async_streaming_llm_caller(prompt.text):
                                response_parts.append(token)
                                yield TokenEvent(
                                    run_id=run_id,
                                    agent_id=agent_id,
                                    token=token,
                                    token_index=token_idx,
                                    is_first=(token_idx == 0),
                                    is_last=False,
                                )
                                token_idx += 1

                            if response_parts:
                                yield TokenEvent(
                                    run_id=run_id,
                                    agent_id=agent_id,
                                    token="",
                                    token_index=token_idx,
                                    is_first=False,
                                    is_last=True,
                                )

                            response = "".join(response_parts)
                            tokens = self.token_counter(prompt.text) + self.token_counter(response)
                        else:
                            _agent = agent_lookup[agent_id]
                            _caller = self._get_async_caller_for_agent(agent_id, _agent)
                            if _caller is None and self.async_structured_llm_caller is None:
                                error_msg = f"No async LLM caller available for agent {agent_id}"
                                errors.append(f"{agent_id}: {error_msg}")
                                messages[agent_id] = f"[Error: {error_msg}]"
                                yield AgentErrorEvent(
                                    run_id=run_id,
                                    agent_id=agent_id,
                                    error_type="ValueError",
                                    error_message=error_msg,
                                )
                                continue
                            response, tokens = await asyncio.wait_for(
                                self._run_agent_with_tools_async(
                                    async_caller=_caller,
                                    prompt=prompt,
                                    agent=_agent,
                                ),
                                timeout=self.config.timeout,
                            )

                        messages[agent_id] = response
                        total_tokens += tokens
                        incoming_ids = get_incoming_agents(agent_id, a_agents, agent_ids)
                        self._save_to_memory(agent_id, response, incoming_ids)

                        is_final = (agent_id == final_agent_id) or (
                            group_idx == len(groups) - 1 and agent_id == group_agents[-1]
                        )

                        yield AgentOutputEvent(
                            run_id=run_id,
                            agent_id=agent_id,
                            agent_name=agent_name,
                            content=response,
                            tokens_used=tokens,
                            duration_ms=(time.time() - step_start) * 1000,
                            is_final=is_final,
                        )

                    except (TimeoutError, ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                        error_msg = str(e)
                        errors.append(f"{agent_id}: {error_msg}")
                        messages[agent_id] = f"[Error: {e}]"

                        yield AgentErrorEvent(
                            run_id=run_id,
                            agent_id=agent_id,
                            error_type=type(e).__name__,
                            error_message=error_msg,
                        )

        final_id, final_missed = self._determine_final_agent(final_agent_id, exec_order, messages)

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        yield RunEndEvent(
            run_id=run_id,
            success=len(errors) == 0 and not final_missed,
            final_answer=messages.get(final_id, ""),
            final_agent_id=final_id,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            executed_agents=list(messages.keys()),
            errors=errors,
            agent_states=agent_states,
        )

    def _stream_adaptive(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        *,
        update_states: bool | None,
    ) -> Iterator[StreamEvent]:
        """Adaptive streaming execution with conditional edges, pruning, and fallback."""
        if self._scheduler is None:
            msg = "Scheduler not initialized for adaptive mode"
            raise ValueError(msg)

        run_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            yield RunEndEvent(run_id=run_id, success=True, total_time=0)
            return

        task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        self._init_memory(agent_ids)
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
            end_agent=final_agent_id,
            edge_conditions=edge_conditions,
            condition_context=condition_ctx,
        )

        yield RunStartEvent(
            run_id=run_id,
            query=query,
            num_agents=len(agent_ids),
            execution_order=plan.execution_order,
            config_summary={"adaptive": True, "policy": self.config.routing_policy.value},
        )

        messages: dict[str, str] = {}
        step_results: dict[str, StepResult] = {}
        execution_order: list[str] = []
        fallback_attempts: dict[str, int] = {}
        topology_changed_count = 0
        errors: list[str] = []
        total_tokens = 0
        step_idx = 0

        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        while not plan.is_complete:
            step = plan.get_current_step()
            if step is None:
                break

            # Skip disabled nodes
            if step.agent_id in disabled_nodes:
                plan.mark_skipped(step.agent_id)
                continue

            # Skip agents whose conditions were not met
            if plan.is_condition_skipped(step):
                plan.advance()
                continue

            # Check for pruning
            should_prune, reason = self._scheduler.should_prune(
                step, plan, step_results.get(execution_order[-1]) if execution_order else None
            )

            if should_prune:
                plan.mark_skipped(step)

                yield PruneEvent(
                    run_id=run_id,
                    agent_id=step.agent_id,
                    reason=reason,
                )
                continue

            agent = agent_lookup.get(step.agent_id)
            if agent is None:
                plan.advance()
                continue

            agent_name = agent_names.get(step.agent_id, step.agent_id)
            incoming = {p: messages[p] for p in step.predecessors if p in messages}
            memory_context = self._get_memory_context(step.agent_id)
            prompt = self._build_prompt(agent, query, incoming, agent_names, memory_context)

            yield AgentStartEvent(
                run_id=run_id,
                agent_id=step.agent_id,
                agent_name=agent_name,
                step_index=step_idx,
                predecessors=step.predecessors,
                prompt_preview=prompt.text[: self.config.prompt_preview_length],
            )

            step_start = time.time()
            result = self._execute_step(step, messages, agent_lookup, agent_names, query)
            step_results[step.agent_id] = result
            execution_order.append(step.agent_id)

            if result.success:
                messages[step.agent_id] = result.response or ""
                plan.mark_completed(step, result.tokens_used)
                total_tokens += result.tokens_used
                self._save_to_memory(step.agent_id, result.response or "", step.predecessors)

                yield AgentOutputEvent(
                    run_id=run_id,
                    agent_id=step.agent_id,
                    agent_name=agent_name,
                    content=result.response or "",
                    tokens_used=result.tokens_used,
                    duration_ms=(time.time() - step_start) * 1000,
                )
            else:
                plan.mark_failed(step)
                errors.append(f"{step.agent_id}: {result.error}")

                yield AgentErrorEvent(
                    run_id=run_id,
                    agent_id=step.agent_id,
                    error_type="ExecutionError",
                    error_message=result.error or "Unknown error",
                )

                # Handle fallback
                attempts = fallback_attempts.get(step.agent_id, 0)
                if self._scheduler.should_use_fallback(step, result, attempts):
                    for fb_agent in step.fallback_agents:
                        if fb_agent not in plan.completed and fb_agent not in plan.failed:
                            plan.insert_fallback(fb_agent, plan.get_step_index(step))

                            yield FallbackEvent(
                                run_id=run_id,
                                failed_agent_id=step.agent_id,
                                fallback_agent_id=fb_agent,
                                attempt=attempts + 1,
                            )
                            break
                    fallback_attempts[step.agent_id] = attempts + 1

            # Topology pipeline: conditional edges + user hooks → plan
            old_remaining = [s.agent_id for s in plan.remaining_steps]
            if self._run_topology_pipeline(
                plan,
                step.agent_id,
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
                new_remaining = [s.agent_id for s in plan.remaining_steps]
                if old_remaining != new_remaining:
                    yield TopologyChangedEvent(
                        run_id=run_id,
                        reason="Topology pipeline: conditional edges",
                        old_remaining=old_remaining,
                        new_remaining=new_remaining,
                        change_count=topology_changed_count,
                    )

            step_idx += 1

        final_id, final_missed = self._determine_final_agent(final_agent_id, execution_order, messages)

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        yield RunEndEvent(
            run_id=run_id,
            success=len(errors) == 0 and not final_missed,
            final_answer=messages.get(final_id, ""),
            final_agent_id=final_id,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            executed_agents=execution_order,
            errors=errors,
            agent_states=agent_states,
        )

    async def _astream_adaptive(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        *,
        update_states: bool | None,
    ) -> AsyncIterator[StreamEvent]:
        """Async adaptive streaming with parallel execution support."""
        if self._scheduler is None:
            msg = "Scheduler not initialized for adaptive mode"
            raise ValueError(msg)

        run_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            yield RunEndEvent(run_id=run_id, success=True, total_time=0)
            return

        task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        self._init_memory(agent_ids)
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
            end_agent=final_agent_id,
            edge_conditions=edge_conditions,
            condition_context=condition_ctx,
        )

        yield RunStartEvent(
            run_id=run_id,
            query=query,
            num_agents=len(agent_ids),
            execution_order=plan.execution_order,
            config_summary={
                "adaptive": True,
                "parallel": self.config.enable_parallel,
                "policy": self.config.routing_policy.value,
            },
        )

        messages: dict[str, str] = {}
        step_results: dict[str, StepResult] = {}
        execution_order: list[str] = []
        fallback_attempts: dict[str, int] = {}
        topology_changed_count = 0
        errors: list[str] = []
        total_tokens = 0
        group_idx = 0

        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        while not plan.is_complete:
            parallel_group = self._get_parallel_group(plan, messages.keys())
            if not parallel_group:
                break

            # Filter condition-skipped, disabled, and pruned steps
            valid_steps = []
            for step in parallel_group:
                # Skip disabled nodes
                if step.agent_id in disabled_nodes:
                    plan.mark_skipped(step.agent_id)
                    continue

                # Skip agents whose conditions were not met
                if plan.is_condition_skipped(step):
                    plan.advance()
                    continue

                should_prune, reason = self._scheduler.should_prune(step, plan, None)
                if should_prune:
                    plan.mark_skipped(step)
                    yield PruneEvent(run_id=run_id, agent_id=step.agent_id, reason=reason)
                else:
                    valid_steps.append(step)

            if not valid_steps:
                # All steps in the group are skipped or pruned — break to avoid stalling.
                break

            # Emit parallel start event
            if self.config.enable_parallel and len(valid_steps) > 1:
                yield ParallelStartEvent(
                    run_id=run_id,
                    agent_ids=[s.agent_id for s in valid_steps],
                    group_index=group_idx,
                )

            # Emit agent start events
            for step in valid_steps:
                agent_name = agent_names.get(step.agent_id, step.agent_id)
                incoming = {p: messages[p] for p in step.predecessors if p in messages}
                agent = agent_lookup.get(step.agent_id)
                if agent:
                    memory_context = self._get_memory_context(step.agent_id)
                    prompt = self._build_prompt(agent, query, incoming, agent_names, memory_context)
                    yield AgentStartEvent(
                        run_id=run_id,
                        agent_id=step.agent_id,
                        agent_name=agent_name,
                        predecessors=step.predecessors,
                        prompt_preview=prompt.text[: self.config.prompt_preview_length],
                    )

            # Execute steps (parallel or sequential)
            if self.config.enable_parallel and len(valid_steps) > 1:
                results = await self._execute_parallel(valid_steps, messages, agent_lookup, agent_names, query)
            else:
                results = []
                for step in valid_steps:
                    r = await self._execute_step_async(step, messages, agent_lookup, agent_names, query)
                    results.append((step, r))

            # Process results and emit events
            successful: list[str] = []
            failed: list[str] = []

            for step, result in results:
                step_results[step.agent_id] = result
                execution_order.append(step.agent_id)
                agent_name = agent_names.get(step.agent_id, step.agent_id)

                if result.success:
                    messages[step.agent_id] = result.response or ""
                    plan.mark_completed(step, result.tokens_used)
                    total_tokens += result.tokens_used
                    self._save_to_memory(step.agent_id, result.response or "", step.predecessors)
                    successful.append(step.agent_id)

                    yield AgentOutputEvent(
                        run_id=run_id,
                        agent_id=step.agent_id,
                        agent_name=agent_name,
                        content=result.response or "",
                        tokens_used=result.tokens_used,
                    )
                else:
                    plan.mark_failed(step)
                    errors.append(f"{step.agent_id}: {result.error}")
                    failed.append(step.agent_id)

                    yield AgentErrorEvent(
                        run_id=run_id,
                        agent_id=step.agent_id,
                        error_message=result.error or "Unknown error",
                    )

                    # Handle fallback
                    attempts = fallback_attempts.get(step.agent_id, 0)
                    if self._scheduler.should_use_fallback(step, result, attempts):
                        for fb_agent in step.fallback_agents:
                            if fb_agent not in plan.completed and fb_agent not in plan.failed:
                                plan.insert_fallback(fb_agent, plan.get_step_index(step))
                                yield FallbackEvent(
                                    run_id=run_id,
                                    failed_agent_id=step.agent_id,
                                    fallback_agent_id=fb_agent,
                                    attempt=attempts + 1,
                                )
                                break
                        fallback_attempts[step.agent_id] = attempts + 1

            # Emit parallel end event
            if self.config.enable_parallel and len(valid_steps) > 1:
                yield ParallelEndEvent(
                    run_id=run_id,
                    agent_ids=[s.agent_id for s in valid_steps],
                    group_index=group_idx,
                    successful=successful,
                    failed=failed,
                )

            # Topology pipeline for each executed agent in the group
            old_remaining = [s.agent_id for s in plan.remaining_steps]
            for step, _result in results:
                if await self._arun_topology_pipeline(
                    plan,
                    step.agent_id,
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

            new_remaining = [s.agent_id for s in plan.remaining_steps]
            if old_remaining != new_remaining:
                yield TopologyChangedEvent(
                    run_id=run_id,
                    reason="Topology pipeline: conditional edges",
                    old_remaining=old_remaining,
                    new_remaining=new_remaining,
                    change_count=topology_changed_count,
                )

            group_idx += 1

        final_id, final_missed = self._determine_final_agent(final_agent_id, execution_order, messages)

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        yield RunEndEvent(
            run_id=run_id,
            success=len(errors) == 0 and not final_missed,
            final_answer=messages.get(final_id, ""),
            final_agent_id=final_id,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            executed_agents=execution_order,
            errors=errors,
            agent_states=agent_states,
        )

    def stream_to_result(
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None = None,
        *,
        update_states: bool | None = None,
    ) -> tuple[Iterator[StreamEvent], MACPResult]:
        """
        Stream execution and also return final MACPResult.

        Useful when you want both streaming display and complete result.

        Returns:
            Tuple of (event iterator, MACPResult)

        Example:
            stream, result_future = runner.stream_to_result(graph)
            for event in stream:
                print(event)
            result = result_future  # Available after stream exhausted

        """
        events: list[StreamEvent] = []
        messages: dict[str, str] = {}
        final_answer = ""
        final_agent = ""
        execution_order: list[str] = []
        total_tokens = 0
        total_time = 0.0
        errors_list: list[str] = []

        def collecting_stream() -> Iterator[StreamEvent]:
            nonlocal final_answer, final_agent, total_tokens, total_time, errors_list

            for event in self.stream(role_graph, final_agent_id, update_states=update_states):
                events.append(event)

                if isinstance(event, AgentOutputEvent):
                    messages[event.agent_id] = event.content
                    execution_order.append(event.agent_id)

                elif isinstance(event, RunEndEvent):
                    final_answer = event.final_answer
                    final_agent = event.final_agent_id
                    total_tokens = event.total_tokens
                    total_time = event.total_time
                    errors_list = event.errors

                yield event

        stream = collecting_stream()

        # Create a lazy result that becomes valid after stream is exhausted
        class LazyResult:
            def __init__(self, runner: "MACPRunner"):
                self._runner = runner
                self._result: MACPResult | None = None

            def __getattr__(self, name: str) -> Any:
                if self._result is None:
                    self._result = MACPResult(
                        messages=messages,
                        final_answer=final_answer,
                        final_agent_id=final_agent,
                        execution_order=execution_order,
                        total_tokens=total_tokens,
                        total_time=total_time,
                        errors=[ExecutionError(message=e, agent_id="", recoverable=False) for e in errors_list]
                        if errors_list
                        else None,
                    )
                return getattr(self._result, name)

        lazy_result = cast("MACPResult", LazyResult(self))
        return stream, lazy_result
