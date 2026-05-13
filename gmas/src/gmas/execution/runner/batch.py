"""Batch execution entrypoints and adaptive/simple execution paths."""

from typing import TYPE_CHECKING

from .shared import (
    Any,
    ConditionContext,
    ExecutionError,
    Handler,
    MACPResult,
    StepResult,
    asyncio,
    build_execution_order,
    filter_reachable_agents,
    get_incoming_agents,
    time,
    torch,
)

if TYPE_CHECKING:
    from . import MACPRunner


class RunnerBatchMixin:
    def run_round(
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None = None,
        start_agent_id: str | None = None,
        *,
        update_states: bool | None = None,
        filter_unreachable: bool = False,
        callbacks: list[Handler] | None = None,
    ) -> MACPResult:
        """
        Run a synchronous round (simple or adaptive strategy).

        Args:
            role_graph: Role graph to execute.
            final_agent_id: ID of final agent (overrides role_graph.end_node).
            start_agent_id: ID of start agent (overrides role_graph.start_node).
            update_states: Whether to update agent states.
            filter_unreachable: Exclude isolated nodes from gmas.execution.
            callbacks: Per-run callback handlers (merged with config.callbacks).

        Returns:
            MACPResult with execution results.

        """
        if not self._has_any_caller():
            msg = "llm_caller, llm_callers, or llm_factory is required for synchronous execution"
            raise ValueError(msg)

        # Get start/end from params or graph
        effective_start = start_agent_id or getattr(role_graph, "start_node", None)
        effective_end = final_agent_id or getattr(role_graph, "end_node", None)

        if self.config.adaptive:
            return self._run_adaptive(
                role_graph,
                effective_end,
                effective_start,
                update_states=update_states,
                filter_unreachable=filter_unreachable,
                callbacks=callbacks,
            )
        return self._run_simple(
            role_graph,
            effective_end,
            effective_start,
            update_states=update_states,
            filter_unreachable=filter_unreachable,
            callbacks=callbacks,
        )

    async def arun_round(
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None = None,
        start_agent_id: str | None = None,
        *,
        update_states: bool | None = None,
        filter_unreachable: bool = False,
        callbacks: list[Handler] | None = None,
    ) -> MACPResult:
        """
        Run an async round (simple or adaptive strategy).

        Args:
            role_graph: Role graph to execute.
            final_agent_id: ID of final agent (overrides role_graph.end_node).
            start_agent_id: ID of start agent (overrides role_graph.start_node).
            update_states: Whether to update agent states.
            filter_unreachable: Exclude isolated nodes from gmas.execution.
            callbacks: Per-run callback handlers (merged with config.callbacks).

        Returns:
            MACPResult with execution results.

        """
        if not self._has_any_async_caller():
            msg = "async_llm_caller, async_llm_callers, or llm_factory is required for async execution"
            raise ValueError(msg)

        # Get start/end from params or graph
        effective_start = start_agent_id or getattr(role_graph, "start_node", None)
        effective_end = final_agent_id or getattr(role_graph, "end_node", None)

        if self.config.adaptive:
            return await self._arun_adaptive(
                role_graph,
                effective_end,
                effective_start,
                update_states=update_states,
                filter_unreachable=filter_unreachable,
                callbacks=callbacks,
            )
        return await self._arun_simple(
            role_graph,
            effective_end,
            effective_start,
            update_states=update_states,
            filter_unreachable=filter_unreachable,
            callbacks=callbacks,
        )

    def _run_simple(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        start_agent_id: str | None,
        *,
        update_states: bool | None = None,
        filter_unreachable: bool = True,
        callbacks: list[Handler] | None = None,
    ) -> MACPResult:
        """
        Sequential execution in topological order without adaptation.

        Supports multi-model: each agent uses its own LLM caller.
        Supports filtering of isolated nodes to save tokens.
        """
        if not self._has_any_caller():
            msg = "llm_caller, llm_callers, or llm_factory is required for synchronous execution"
            raise ValueError(msg)

        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            return MACPResult(messages={}, final_answer="", final_agent_id="", execution_order=[])
        _task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        # Filter isolated nodes
        excluded_agents: list[str] = []
        effective_agent_ids = agent_ids
        effective_a = a_agents

        if filter_unreachable and (start_agent_id is not None or final_agent_id is not None):
            relevant, excluded_agents = filter_reachable_agents(a_agents, agent_ids, start_agent_id, final_agent_id)
            if relevant and len(relevant) < len(agent_ids):
                indices = [agent_ids.index(aid) for aid in relevant]
                indices_t = torch.tensor(indices, dtype=torch.long)
                effective_a = a_agents[indices_t][:, indices_t]
                effective_agent_ids = relevant

        exec_order = build_execution_order(effective_a, effective_agent_ids, role_graph.role_sequence)

        # Initialize memory (with effective agents after filtering)
        self._init_memory(effective_agent_ids)

        # Initialize callbacks
        run_id = self._init_run(
            graph_name=getattr(role_graph, "name", None),
            num_agents=len(effective_agent_ids),
            query=query,
            execution_order=exec_order,
            callbacks=callbacks,
        )

        task_connected = self._get_task_connected_agents(role_graph)

        messages: dict[str, str] = {}
        total_tokens = 0
        actual_exec_order: list[str] = []
        early_stopped = False
        early_stop_reason: str | None = None
        topology_modifications = 0
        skipped_by_hooks: set[str] = set()
        run_error: BaseException | None = None

        # Get disabled nodes from graph
        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        try:
            for step_idx, agent_id in enumerate(exec_order):
                # Check if agent was skipped by hooks
                if agent_id in skipped_by_hooks:
                    if self._callback_manager:
                        self._callback_manager.on_prune(
                            run_id=run_id,
                            agent_id=agent_id,
                            reason="skipped_by_hook",
                        )
                    continue

                # Check if node is disabled
                if agent_id in disabled_nodes:
                    if agent_id not in excluded_agents:
                        excluded_agents.append(agent_id)
                    if self._callback_manager:
                        self._callback_manager.on_prune(
                            run_id=run_id,
                            agent_id=agent_id,
                            reason="disabled_node",
                        )
                    continue

                agent = agent_lookup.get(agent_id)
                if agent is None:
                    continue

                incoming_ids = get_incoming_agents(agent_id, effective_a, effective_agent_ids)
                incoming_messages = {aid: messages[aid] for aid in incoming_ids if aid in messages}

                include_query = self._should_include_query(agent_id, task_connected)
                memory_context = self._get_memory_context(agent_id)
                prompt = self._build_prompt(
                    agent, query, incoming_messages, agent_names, memory_context, include_query=include_query
                )
                prompt_text = prompt.text

                # Notify callbacks of agent start
                if self._callback_manager:
                    self._callback_manager.on_agent_start(
                        run_id=run_id,
                        agent_id=agent_id,
                        agent_name=agent_names.get(agent_id, agent_id),
                        step_index=step_idx,
                        prompt=prompt_text[: self.config.prompt_preview_length],
                        predecessors=incoming_ids,
                    )

                agent_start_time = time.time()
                try:
                    # Get caller for this specific agent (multi-model support)
                    caller = self._get_caller_for_agent(agent_id, agent)
                    if caller is None:
                        error_msg = f"No LLM caller available for agent {agent_id}"
                        messages[agent_id] = f"[Error: {error_msg}]"
                        actual_exec_order.append(agent_id)
                        if self._callback_manager:
                            self._callback_manager.on_agent_error(
                                run_id=run_id,
                                error=ValueError(error_msg),
                                agent_id=agent_id,
                                error_type="NoCallerError",
                            )
                        continue

                    # Execute LLM caller with tools support
                    response, agent_tokens = self._run_agent_with_tools(
                        caller=caller,
                        prompt=prompt,
                        agent=agent,
                    )

                    agent_duration_ms = (time.time() - agent_start_time) * 1000

                    messages[agent_id] = response
                    total_tokens += agent_tokens
                    self._save_to_memory(agent_id, response, incoming_ids)
                    actual_exec_order.append(agent_id)

                    if self._budget_tracker:
                        self._budget_tracker.record_usage(
                            node_id=agent_id,
                            prompt_tokens=agent_tokens // 2,
                            completion_tokens=agent_tokens - agent_tokens // 2,
                            latency_seconds=agent_duration_ms / 1000,
                        )

                    # Notify callbacks of agent end (sync simple)
                    if self._callback_manager:
                        is_final = agent_id == final_agent_id or (final_agent_id is None and agent_id == exec_order[-1])
                        self._callback_manager.on_agent_end(
                            run_id=run_id,
                            agent_id=agent_id,
                            output=response,
                            agent_name=agent_names.get(agent_id, agent_id),
                            step_index=step_idx,
                            tokens_used=agent_tokens,
                            duration_ms=agent_duration_ms,
                            is_final=is_final,
                        )

                except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                    messages[agent_id] = f"[Error: {e}]"
                    actual_exec_order.append(agent_id)
                    if self._callback_manager:
                        self._callback_manager.on_agent_error(
                            run_id=run_id,
                            error=e,
                            agent_id=agent_id,
                            error_type=type(e).__name__,
                        )

                # Check early stopping
                remaining = [a for a in exec_order if a not in messages and a not in skipped_by_hooks]
                should_stop, reason = self._check_early_stop(
                    agent_id,
                    messages.get(agent_id),
                    messages,
                    actual_exec_order,
                    remaining,
                    query,
                    total_tokens,
                )
                if should_stop:
                    early_stopped = True
                    early_stop_reason = reason
                    break

                # Apply topology hooks
                if self.config.enable_dynamic_topology:
                    action = self._apply_topology_hooks(
                        agent_id,
                        messages.get(agent_id),
                        None,
                        messages,
                        actual_exec_order,
                        remaining,
                        query,
                        total_tokens,
                        role_graph,
                    )
                    if action is not None:
                        if action.early_stop:
                            early_stopped = True
                            early_stop_reason = action.early_stop_reason
                            break
                        if action.skip_agents:
                            skipped_by_hooks.update(action.skip_agents)
                        _old_remaining = list(remaining)
                        mods = self._apply_graph_modifications(role_graph, action)
                        topology_modifications += mods
                        if mods and self._callback_manager:
                            _new_remaining = [a for a in exec_order if a not in messages and a not in skipped_by_hooks]
                            self._callback_manager.on_topology_changed(
                                run_id=run_id,
                                reason="topology_hook",
                                old_remaining=_old_remaining,
                                new_remaining=_new_remaining,
                                change_count=topology_modifications,
                            )

        except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
            run_error = e

        final_id, final_missed = self._determine_final_agent(final_agent_id, actual_exec_order, messages)
        final_answer = messages.get(final_id, "")
        run_success = run_error is None and not final_missed

        elapsed_ms = (time.time() - start_time) * 1000

        # Finalize callbacks
        self._finalize_run(
            run_id=run_id,
            success=run_success,
            executed_agents=len(actual_exec_order),
            final_answer=final_answer,
            error=run_error,
            executed_agent_ids=actual_exec_order,
            total_tokens=total_tokens,
            total_time_ms=elapsed_ms,
        )

        if run_error:
            raise run_error

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        missed_reason = (
            f"requested final agent '{final_agent_id}' was not executed" if final_missed else early_stop_reason
        )

        return MACPResult(
            messages=messages,
            final_answer=messages.get(final_id, ""),
            final_agent_id=final_id,
            execution_order=actual_exec_order,
            agent_states=agent_states,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            pruned_agents=excluded_agents or None,
            early_stopped=early_stopped or final_missed,
            early_stop_reason=missed_reason,
            topology_modifications=topology_modifications,
        )

    async def _arun_simple(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        start_agent_id: str | None,
        *,
        update_states: bool | None = None,
        filter_unreachable: bool = True,
        callbacks: list[Handler] | None = None,
    ) -> MACPResult:
        """
        Async sequential execution without adaptation.

        Supports multi-model: each agent uses its own LLM caller.
        Supports filtering of isolated nodes to save tokens.
        """
        if not self._has_any_async_caller():
            msg = "async_llm_caller, async_llm_callers, or llm_factory is required for async execution"
            raise ValueError(msg)

        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            return MACPResult(messages={}, final_answer="", final_agent_id="", execution_order=[])
        _task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        # Filter isolated nodes
        excluded_agents: list[str] = []
        effective_agent_ids = agent_ids
        effective_a = a_agents

        if filter_unreachable and (start_agent_id is not None or final_agent_id is not None):
            relevant, excluded_agents = filter_reachable_agents(a_agents, agent_ids, start_agent_id, final_agent_id)
            if relevant and len(relevant) < len(agent_ids):
                indices = [agent_ids.index(aid) for aid in relevant]
                indices_t = torch.tensor(indices, dtype=torch.long)
                effective_a = a_agents[indices_t][:, indices_t]
                effective_agent_ids = relevant

        exec_order = build_execution_order(effective_a, effective_agent_ids, role_graph.role_sequence)

        # Initialize memory (with effective agents after filtering)
        self._init_memory(effective_agent_ids)

        # Initialize callbacks
        run_id = self._init_run(
            graph_name=getattr(role_graph, "name", None),
            num_agents=len(effective_agent_ids),
            query=query,
            execution_order=exec_order,
            callbacks=callbacks,
        )

        task_connected = self._get_task_connected_agents(role_graph)

        messages: dict[str, str] = {}
        total_tokens = 0
        actual_exec_order: list[str] = []
        early_stopped = False
        early_stop_reason: str | None = None
        topology_modifications = 0
        skipped_by_hooks: set[str] = set()
        run_error: BaseException | None = None

        # Get disabled nodes from graph
        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        try:
            for step_idx, agent_id in enumerate(exec_order):
                # Check if agent was skipped by hooks
                if agent_id in skipped_by_hooks:
                    if self._callback_manager:
                        self._callback_manager.on_prune(
                            run_id=run_id,
                            agent_id=agent_id,
                            reason="skipped_by_hook",
                        )
                    continue

                # Check if node is disabled
                if agent_id in disabled_nodes:
                    if agent_id not in excluded_agents:
                        excluded_agents.append(agent_id)
                    if self._callback_manager:
                        self._callback_manager.on_prune(
                            run_id=run_id,
                            agent_id=agent_id,
                            reason="disabled_node",
                        )
                    continue

                agent = agent_lookup.get(agent_id)
                if agent is None:
                    continue

                incoming_ids = get_incoming_agents(agent_id, effective_a, effective_agent_ids)
                incoming_messages = {aid: messages[aid] for aid in incoming_ids if aid in messages}

                include_query = self._should_include_query(agent_id, task_connected)
                memory_context = self._get_memory_context(agent_id)
                prompt = self._build_prompt(
                    agent, query, incoming_messages, agent_names, memory_context, include_query=include_query
                )

                # Notify callbacks of agent start
                # prompt is now a StructuredPrompt
                prompt_text = prompt.text

                if self._callback_manager:
                    self._callback_manager.on_agent_start(
                        run_id=run_id,
                        agent_id=agent_id,
                        agent_name=agent_names.get(agent_id, agent_id),
                        step_index=step_idx,
                        prompt=prompt_text[: self.config.prompt_preview_length],
                        predecessors=incoming_ids,
                    )

                agent_start_time = time.time()
                try:
                    # Get async caller for this specific agent (multi-model support).
                    # When only async_structured_llm_caller is configured,
                    # async_caller may be None — _acall_llm handles that.
                    async_caller = self._get_async_caller_for_agent(agent_id, agent)
                    if async_caller is None and self.async_structured_llm_caller is None:
                        error_msg = f"No async LLM caller available for agent {agent_id}"
                        messages[agent_id] = f"[Error: {error_msg}]"
                        actual_exec_order.append(agent_id)
                        if self._callback_manager:
                            self._callback_manager.on_agent_error(
                                run_id=run_id,
                                error=ValueError(error_msg),
                                agent_id=agent_id,
                                error_type="NoCallerError",
                            )
                        continue

                    response, agent_tokens = await asyncio.wait_for(
                        self._run_agent_with_tools_async(
                            async_caller=async_caller,
                            prompt=prompt,
                            agent=agent,
                        ),
                        timeout=self.config.timeout,
                    )
                    agent_duration_ms = (time.time() - agent_start_time) * 1000

                    messages[agent_id] = response
                    total_tokens += agent_tokens
                    self._save_to_memory(agent_id, response, incoming_ids)
                    actual_exec_order.append(agent_id)

                    if self._budget_tracker:
                        self._budget_tracker.record_usage(
                            node_id=agent_id,
                            prompt_tokens=agent_tokens // 2,
                            completion_tokens=agent_tokens - agent_tokens // 2,
                            latency_seconds=agent_duration_ms / 1000,
                        )

                    # Notify callbacks of agent end (async simple)
                    if self._callback_manager:
                        is_final = agent_id == final_agent_id or (final_agent_id is None and agent_id == exec_order[-1])
                        self._callback_manager.on_agent_end(
                            run_id=run_id,
                            agent_id=agent_id,
                            output=response,
                            agent_name=agent_names.get(agent_id, agent_id),
                            step_index=step_idx,
                            tokens_used=agent_tokens,
                            duration_ms=agent_duration_ms,
                            is_final=is_final,
                        )

                except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
                    messages[agent_id] = f"[Error: {e}]"
                    actual_exec_order.append(agent_id)
                    if self._callback_manager:
                        self._callback_manager.on_agent_error(
                            run_id=run_id,
                            error=e,
                            agent_id=agent_id,
                            error_type=type(e).__name__,
                        )

                # Check early stopping
                remaining = [a for a in exec_order if a not in messages and a not in skipped_by_hooks]
                should_stop, reason = self._check_early_stop(
                    agent_id,
                    messages.get(agent_id),
                    messages,
                    actual_exec_order,
                    remaining,
                    query,
                    total_tokens,
                )
                if should_stop:
                    early_stopped = True
                    early_stop_reason = reason
                    break

                # Apply async topology hooks
                if self.config.enable_dynamic_topology:
                    action = await self._apply_async_topology_hooks(
                        agent_id,
                        messages.get(agent_id),
                        None,
                        messages,
                        actual_exec_order,
                        remaining,
                        query,
                        total_tokens,
                        role_graph,
                    )
                    if action is not None:
                        if action.early_stop:
                            early_stopped = True
                            early_stop_reason = action.early_stop_reason
                            break
                        if action.skip_agents:
                            skipped_by_hooks.update(action.skip_agents)
                        _old_remaining = list(remaining)
                        mods = self._apply_graph_modifications(role_graph, action)
                        topology_modifications += mods
                        if mods and self._callback_manager:
                            _new_remaining = [a for a in exec_order if a not in messages and a not in skipped_by_hooks]
                            self._callback_manager.on_topology_changed(
                                run_id=run_id,
                                reason="topology_hook",
                                old_remaining=_old_remaining,
                                new_remaining=_new_remaining,
                                change_count=topology_modifications,
                            )

        except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
            run_error = e

        final_id, final_missed = self._determine_final_agent(final_agent_id, actual_exec_order, messages)
        final_answer = messages.get(final_id, "")
        run_success = run_error is None and not final_missed
        elapsed_ms = (time.time() - start_time) * 1000

        # Finalize callbacks
        self._finalize_run(
            run_id=run_id,
            success=run_success,
            executed_agents=len(actual_exec_order),
            final_answer=final_answer,
            error=run_error,
            executed_agent_ids=actual_exec_order,
            total_tokens=total_tokens,
            total_time_ms=elapsed_ms,
        )

        if run_error:
            raise run_error

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        missed_reason = (
            f"requested final agent '{final_agent_id}' was not executed" if final_missed else early_stop_reason
        )

        return MACPResult(
            messages=messages,
            final_answer=final_answer,
            final_agent_id=final_id,
            execution_order=actual_exec_order,
            agent_states=agent_states,
            total_tokens=total_tokens,
            total_time=time.time() - start_time,
            pruned_agents=excluded_agents or None,
            early_stopped=early_stopped or final_missed,
            early_stop_reason=missed_reason,
            topology_modifications=topology_modifications,
        )

    def _run_adaptive(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        start_agent_id: str | None,
        *,
        update_states: bool | None = None,
        filter_unreachable: bool = True,
        callbacks: list[Handler] | None = None,
    ) -> MACPResult:
        """
        Adaptive sync execution with conditional edges and fallback.

        Supports multi-model: each agent uses its own LLM caller.
        Supports filtering of isolated nodes to save tokens.
        Per-call ``callbacks`` are merged with ``RunnerConfig.callbacks``
        and context-manager callbacks (similar to ``_run_simple``).
        """
        if not self._has_any_caller():
            msg = "llm_caller, llm_callers, or llm_factory is required for synchronous execution"
            raise ValueError(msg)
        if self._scheduler is None:
            msg = "Scheduler not initialized for adaptive mode"
            raise ValueError(msg)

        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            return MACPResult(messages={}, final_answer="", final_agent_id="", execution_order=[])
        task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        # Initialize memory
        self._init_memory(agent_ids)

        p_matrix = self._extract_p_matrix(role_graph, task_idx)

        # Get conditions from graph for conditional routing
        edge_conditions = self._get_edge_conditions(role_graph)

        # Initial context for conditions
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
            start_agent=start_agent_id,
            end_agent=final_agent_id,
            edge_conditions=edge_conditions,
            condition_context=condition_ctx,
            filter_unreachable=filter_unreachable,
        )
        plan.max_iterations = self.config.max_loop_iterations

        # Initialize callbacks (per-call + config + context-manager)
        run_id = self._init_run(
            graph_name=getattr(role_graph, "name", None),
            num_agents=len(agent_ids),
            query=query,
            execution_order=plan.execution_order,
            callbacks=callbacks,
        )

        if self._callback_manager:
            self._callback_manager.on_plan_created(
                run_id=run_id,
                num_steps=len(plan.steps),
                execution_order=[s.agent_id for s in plan.steps],
            )

        messages: dict[str, str] = {}
        step_results: dict[str, StepResult] = {}
        step_results_by_step: dict[str, StepResult] = {}
        messages_by_step: dict[str, str] = {}
        messages_history: dict[str, list[str]] = {}
        step_results_history: dict[str, list[StepResult]] = {}
        execution_order: list[str] = []
        fallback_attempts: dict[str, int] = {}
        topology_changed_count = 0
        fallback_count = 0
        pruned_agents: list[str] = []
        errors: list[ExecutionError] = []
        step_idx = 0
        run_error: BaseException | None = None

        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        try:
            while not plan.is_complete:
                step = plan.get_current_step()
                if step is None:
                    break

                # Skip disabled nodes
                if step.agent_id in disabled_nodes:
                    plan.mark_skipped(step.agent_id)
                    pruned_agents.append(step.agent_id)
                    if self._callback_manager:
                        self._callback_manager.on_prune(
                            run_id=run_id,
                            agent_id=step.agent_id,
                            reason="disabled_node",
                        )
                    continue

                # Skip agents whose conditions were not met
                if plan.is_condition_skipped(step):
                    plan.advance()
                    continue

                should_prune, reason = self._scheduler.should_prune(
                    step, plan, step_results.get(execution_order[-1]) if execution_order else None
                )

                if should_prune:
                    plan.mark_skipped(step)
                    pruned_agents.append(step.agent_id)
                    if self._callback_manager:
                        self._callback_manager.on_prune(
                            run_id=run_id,
                            agent_id=step.agent_id,
                            reason=reason or "scheduler_prune",
                        )
                    errors.append(
                        ExecutionError(
                            message=f"Pruned: {reason}",
                            agent_id=step.agent_id,
                            recoverable=False,
                        )
                    )
                    continue

                # Notify callbacks of agent start
                agent_name = agent_names.get(step.agent_id, step.agent_id)
                if self._callback_manager:
                    _agent_obj = agent_lookup.get(step.agent_id)
                    _prompt_preview_text = ""
                    if _agent_obj is not None:
                        _inc = {p: messages[p] for p in step.predecessors if p in messages}
                        _mem = self._get_memory_context(step.agent_id)
                        _sp = self._build_prompt(_agent_obj, query, _inc, agent_names, _mem)
                        _prompt_preview_text = _sp.text
                    self._callback_manager.on_agent_start(
                        run_id=run_id,
                        agent_id=step.agent_id,
                        agent_name=agent_name,
                        step_index=step_idx,
                        prompt=_prompt_preview_text[: self.config.prompt_preview_length],
                        predecessors=step.predecessors,
                    )

                agent_start_time = time.time()
                result = self._execute_step(step, messages, agent_lookup, agent_names, query)

                step_results[step.agent_id] = result
                step_results_by_step[step.step_id] = result
                step_results_history.setdefault(step.agent_id, []).append(result)
                execution_order.append(step.agent_id)

                if result.success:
                    response = result.response or ""
                    messages[step.agent_id] = response
                    messages_by_step[step.step_id] = response
                    messages_history.setdefault(step.agent_id, []).append(response)
                    plan.mark_completed(step, result.tokens_used)
                    self._save_to_memory(step.agent_id, response, step.predecessors)

                    if self._budget_tracker:
                        self._budget_tracker.record_usage(
                            node_id=step.agent_id,
                            prompt_tokens=result.tokens_used // 2,
                            completion_tokens=result.tokens_used - result.tokens_used // 2,
                            latency_seconds=(time.time() - agent_start_time),
                        )

                    # Notify callbacks of agent end
                    if self._callback_manager:
                        self._callback_manager.on_agent_end(
                            run_id=run_id,
                            agent_id=step.agent_id,
                            output=result.response or "",
                            agent_name=agent_name,
                            step_index=step_idx,
                            tokens_used=result.tokens_used,
                            duration_ms=(time.time() - agent_start_time) * 1000,
                            is_final=False,
                        )
                else:
                    plan.mark_failed(step)
                    errors.append(
                        ExecutionError(
                            message=result.error or "Unknown error",
                            agent_id=step.agent_id,
                            recoverable=True,
                        )
                    )

                    # Notify callbacks of agent error
                    if self._callback_manager:
                        self._callback_manager.on_agent_error(
                            run_id=run_id,
                            error=Exception(result.error or "Unknown error"),
                            agent_id=step.agent_id,
                            error_type="ExecutionError",
                        )

                    attempts = fallback_attempts.get(step.agent_id, 0)
                    if self._scheduler.should_use_fallback(step, result, attempts):
                        for fb_agent in step.fallback_agents:
                            if fb_agent not in plan.completed and fb_agent not in plan.failed:
                                plan.insert_fallback(fb_agent, plan.get_step_index(step))
                                fallback_count += 1
                                if self._callback_manager:
                                    self._callback_manager.on_fallback(
                                        run_id=run_id,
                                        failed_agent_id=step.agent_id,
                                        fallback_agent_id=fb_agent,
                                        reason=result.error or "execution failed",
                                    )
                                break
                        fallback_attempts[step.agent_id] = attempts + 1

                # Topology pipeline: conditional edges + user hooks → plan
                _old_remaining = [
                    f"{s.step_id}({s.agent_id})"
                    for s in plan.steps[plan.current_index :]
                    if not plan.is_step_resolved(s)
                ]
                if self._run_topology_pipeline(
                    plan,
                    step.agent_id,
                    a_agents,
                    agent_ids,
                    step_results,
                    messages,
                    query,
                    execution_order,
                    plan.tokens_used,
                    role_graph,
                    messages_history=messages_history,
                    step_results_history=step_results_history,
                ):
                    topology_changed_count += 1
                    if self._callback_manager:
                        _new_remaining = [
                            f"{s.step_id}({s.agent_id})"
                            for s in plan.steps[plan.current_index :]
                            if not plan.is_step_resolved(s)
                        ]
                        self._callback_manager.on_topology_changed(
                            run_id=run_id,
                            reason="topology_pipeline",
                            old_remaining=_old_remaining,
                            new_remaining=_new_remaining,
                            change_count=topology_changed_count,
                        )

                step_idx += 1

        except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
            run_error = e

        final_id, final_missed = self._determine_final_agent(final_agent_id, execution_order, messages)
        final_answer = messages.get(final_id, "")
        run_success = run_error is None and not final_missed
        elapsed_ms = (time.time() - start_time) * 1000

        self._finalize_run(
            run_id=run_id,
            success=run_success,
            executed_agents=len(execution_order),
            final_answer=final_answer,
            error=run_error,
            executed_agent_ids=execution_order,
            total_tokens=plan.tokens_used,
            total_time_ms=elapsed_ms,
        )

        if run_error:
            raise run_error

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        missed_reason = f"requested final agent '{final_agent_id}' was not executed" if final_missed else None

        return MACPResult(
            messages=messages,
            final_answer=final_answer,
            final_agent_id=final_id,
            execution_order=execution_order,
            agent_states=agent_states,
            step_results=step_results,
            step_results_by_step=step_results_by_step,
            messages_by_step=messages_by_step,
            total_tokens=plan.tokens_used,
            total_time=time.time() - start_time,
            topology_changed_count=topology_changed_count,
            fallback_count=fallback_count,
            pruned_agents=pruned_agents,
            errors=errors or None,
            early_stopped=final_missed,
            early_stop_reason=missed_reason,
        )

    async def _arun_adaptive(  # noqa: PLR0912, PLR0915
        self: "MACPRunner",
        role_graph: Any,
        final_agent_id: str | None,
        start_agent_id: str | None,
        *,
        update_states: bool | None = None,
        filter_unreachable: bool = True,
        callbacks: list[Handler] | None = None,
    ) -> MACPResult:
        """
        Adaptive async execution with parallelism and conditional edges.

        Supports multi-model: each agent uses its own LLM caller.
        Supports filtering of isolated nodes to save tokens.
        Per-call ``callbacks`` are merged with ``RunnerConfig.callbacks``
        and context-manager callbacks (similar to ``_arun_simple``).
        """
        if not self._has_any_async_caller():
            msg = "async_llm_caller, async_llm_callers, or llm_factory is required for async execution"
            raise ValueError(msg)
        if self._scheduler is None:
            msg = "Scheduler not initialized for adaptive mode"
            raise ValueError(msg)

        start_time = time.time()

        base = self._prepare_base_context(role_graph)
        if base is None:
            return MACPResult(messages={}, final_answer="", final_agent_id="", execution_order=[])
        task_idx, a_agents, agent_ids, query, agent_lookup, agent_names = base

        # Initialize memory
        self._init_memory(agent_ids)

        p_matrix = self._extract_p_matrix(role_graph, task_idx)

        # Get conditions from graph for conditional routing
        edge_conditions = self._get_edge_conditions(role_graph)

        # Context for conditions
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
            start_agent=start_agent_id,
            end_agent=final_agent_id,
            edge_conditions=edge_conditions,
            condition_context=condition_ctx,
            filter_unreachable=filter_unreachable,
        )
        plan.max_iterations = self.config.max_loop_iterations

        # Initialize callbacks (per-call + config + context-manager)
        run_id = self._init_run(
            graph_name=getattr(role_graph, "name", None),
            num_agents=len(agent_ids),
            query=query,
            execution_order=plan.execution_order,
            callbacks=callbacks,
        )

        if self._callback_manager:
            self._callback_manager.on_plan_created(
                run_id=run_id,
                num_steps=len(plan.steps),
                execution_order=[s.agent_id for s in plan.steps],
            )

        messages: dict[str, str] = {}
        step_results: dict[str, StepResult] = {}
        step_results_by_step: dict[str, StepResult] = {}
        messages_by_step: dict[str, str] = {}
        messages_history: dict[str, list[str]] = {}
        step_results_history: dict[str, list[StepResult]] = {}
        execution_order: list[str] = []
        fallback_attempts: dict[str, int] = {}
        topology_changed_count = 0
        fallback_count = 0
        pruned_agents: list[str] = []
        errors: list[ExecutionError] = []
        step_idx = 0
        run_error: BaseException | None = None

        disabled_nodes: set[str] = getattr(role_graph, "disabled_nodes", set())

        try:
            while not plan.is_complete:
                parallel_group = self._get_parallel_group(plan, messages.keys())

                if not parallel_group:
                    break

                valid_steps = []
                for step in parallel_group:
                    # Skip disabled nodes
                    if step.agent_id in disabled_nodes:
                        plan.mark_skipped(step.agent_id)
                        pruned_agents.append(step.agent_id)
                        if self._callback_manager:
                            self._callback_manager.on_prune(
                                run_id=run_id,
                                agent_id=step.agent_id,
                                reason="disabled_node",
                            )
                        continue

                    # Skip agents whose conditions were not met
                    if plan.is_condition_skipped(step):
                        plan.advance()
                        continue

                    should_prune, reason = self._scheduler.should_prune(step, plan, None)
                    if should_prune:
                        plan.mark_skipped(step)
                        pruned_agents.append(step.agent_id)
                        if self._callback_manager:
                            self._callback_manager.on_prune(
                                run_id=run_id,
                                agent_id=step.agent_id,
                                reason=reason or "scheduler_prune",
                            )
                        errors.append(
                            ExecutionError(
                                message=f"Pruned: {reason}",
                                agent_id=step.agent_id,
                                recoverable=False,
                            )
                        )
                    else:
                        valid_steps.append(step)

                if not valid_steps:
                    # All steps in the group were skipped or pruned — plan may stall.
                    # If plan.is_complete is already True — we'll exit on the next iteration;
                    # otherwise explicitly break to avoid an infinite loop.
                    break

                # Notify callbacks of agent start for all steps in group
                group_start_times: dict[str, float] = {}
                for step in valid_steps:
                    agent_name = agent_names.get(step.agent_id, step.agent_id)
                    if self._callback_manager:
                        _agent_obj = agent_lookup.get(step.agent_id)
                        _prompt_preview_text = ""
                        if _agent_obj is not None:
                            _inc = {p: messages[p] for p in step.predecessors if p in messages}
                            _mem = self._get_memory_context(step.agent_id)
                            _sp = self._build_prompt(_agent_obj, query, _inc, agent_names, _mem)
                            _prompt_preview_text = _sp.text
                        self._callback_manager.on_agent_start(
                            run_id=run_id,
                            agent_id=step.agent_id,
                            agent_name=agent_name,
                            step_index=step_idx,
                            prompt=_prompt_preview_text[: self.config.prompt_preview_length],
                            predecessors=step.predecessors,
                        )
                    group_start_times[step.agent_id] = time.time()
                    step_idx += 1

                _is_parallel_group = self.config.enable_parallel and len(valid_steps) > 1
                if _is_parallel_group and self._callback_manager:
                    self._callback_manager.on_parallel_start(
                        run_id=run_id,
                        agent_ids=[s.agent_id for s in valid_steps],
                        group_index=step_idx,
                    )

                if _is_parallel_group:
                    results = await self._execute_parallel(valid_steps, messages, agent_lookup, agent_names, query)
                else:
                    results = []
                    for step in valid_steps:
                        r = await self._execute_step_async(step, messages, agent_lookup, agent_names, query)
                        results.append((step, r))

                for step, result in results:
                    step_results[step.agent_id] = result
                    step_results_by_step[step.step_id] = result
                    step_results_history.setdefault(step.agent_id, []).append(result)
                    execution_order.append(step.agent_id)
                    agent_name = agent_names.get(step.agent_id, step.agent_id)
                    agent_start_time = group_start_times.get(step.agent_id, time.time())

                    if result.success:
                        response = result.response or ""
                        messages[step.agent_id] = response
                        messages_by_step[step.step_id] = response
                        messages_history.setdefault(step.agent_id, []).append(response)
                        plan.mark_completed(step, result.tokens_used)
                        self._save_to_memory(step.agent_id, response, step.predecessors)

                        if self._budget_tracker:
                            self._budget_tracker.record_usage(
                                node_id=step.agent_id,
                                prompt_tokens=result.tokens_used // 2,
                                completion_tokens=result.tokens_used - result.tokens_used // 2,
                                latency_seconds=(time.time() - agent_start_time),
                            )

                        # Notify callbacks of agent end
                        if self._callback_manager:
                            self._callback_manager.on_agent_end(
                                run_id=run_id,
                                agent_id=step.agent_id,
                                output=result.response or "",
                                agent_name=agent_name,
                                step_index=len(execution_order) - 1,
                                tokens_used=result.tokens_used,
                                duration_ms=(time.time() - agent_start_time) * 1000,
                                is_final=False,
                            )
                    else:
                        plan.mark_failed(step)
                        errors.append(
                            ExecutionError(
                                message=result.error or "Unknown error",
                                agent_id=step.agent_id,
                                recoverable=True,
                            )
                        )

                        # Notify callbacks of agent error
                        if self._callback_manager:
                            self._callback_manager.on_agent_error(
                                run_id=run_id,
                                error=Exception(result.error or "Unknown error"),
                                agent_id=step.agent_id,
                                error_type="ExecutionError",
                            )

                        attempts = fallback_attempts.get(step.agent_id, 0)
                        if self._scheduler.should_use_fallback(step, result, attempts):
                            for fb_agent in step.fallback_agents:
                                if fb_agent not in plan.completed and fb_agent not in plan.failed:
                                    plan.insert_fallback(fb_agent, plan.get_step_index(step))
                                    fallback_count += 1
                                    if self._callback_manager:
                                        self._callback_manager.on_fallback(
                                            run_id=run_id,
                                            failed_agent_id=step.agent_id,
                                            fallback_agent_id=fb_agent,
                                            reason=result.error or "execution failed",
                                        )
                                    break
                            fallback_attempts[step.agent_id] = attempts + 1

                if _is_parallel_group and self._callback_manager:
                    self._callback_manager.on_parallel_end(
                        run_id=run_id,
                        agent_ids=[s.agent_id for s in valid_steps],
                        group_index=step_idx,
                        successful=[s.agent_id for s, r in results if r.success],
                        failed=[s.agent_id for s, r in results if not r.success],
                    )

                # Topology pipeline for each executed agent in the group
                for step, _result in results:
                    _old_remaining = [
                        f"{s.step_id}({s.agent_id})"
                        for s in plan.steps[plan.current_index :]
                        if not plan.is_step_resolved(s)
                    ]
                    if await self._arun_topology_pipeline(
                        plan,
                        step.agent_id,
                        a_agents,
                        agent_ids,
                        step_results,
                        messages,
                        query,
                        execution_order,
                        plan.tokens_used,
                        role_graph,
                        messages_history=messages_history,
                        step_results_history=step_results_history,
                    ):
                        topology_changed_count += 1
                        if self._callback_manager:
                            _new_remaining = [
                                f"{s.step_id}({s.agent_id})"
                                for s in plan.steps[plan.current_index :]
                                if not plan.is_step_resolved(s)
                            ]
                            self._callback_manager.on_topology_changed(
                                run_id=run_id,
                                reason="topology_pipeline",
                                old_remaining=_old_remaining,
                                new_remaining=_new_remaining,
                                change_count=topology_changed_count,
                            )

        except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
            run_error = e

        final_id, final_missed = self._determine_final_agent(final_agent_id, execution_order, messages)
        final_answer = messages.get(final_id, "")
        run_success = run_error is None and not final_missed
        elapsed_ms = (time.time() - start_time) * 1000

        self._finalize_run(
            run_id=run_id,
            success=run_success,
            executed_agents=len(execution_order),
            final_answer=final_answer,
            error=run_error,
            executed_agent_ids=execution_order,
            total_tokens=plan.tokens_used,
            total_time_ms=elapsed_ms,
        )

        if run_error:
            raise run_error

        do_update = update_states if update_states is not None else self.config.update_states
        agent_states = self._build_agent_states(messages, agent_lookup) if do_update else None

        missed_reason = f"requested final agent '{final_agent_id}' was not executed" if final_missed else None

        return MACPResult(
            messages=messages,
            final_answer=final_answer,
            final_agent_id=final_id,
            execution_order=execution_order,
            agent_states=agent_states,
            step_results=step_results,
            step_results_by_step=step_results_by_step,
            messages_by_step=messages_by_step,
            total_tokens=plan.tokens_used,
            total_time=time.time() - start_time,
            topology_changed_count=topology_changed_count,
            fallback_count=fallback_count,
            pruned_agents=pruned_agents,
            errors=errors or None,
            early_stopped=final_missed,
            early_stop_reason=missed_reason,
        )
