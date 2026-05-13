"""Core MACPRunner lifecycle, memory, caller selection, and graph helpers."""

from .shared import (
    UTC,
    AdaptiveScheduler,
    AgentMemory,
    Any,
    AsyncIterator,
    AsyncStructuredLLMCallerProtocol,
    Awaitable,
    BudgetTracker,
    Callable,
    CallbackManager,
    ExecutionContext,
    ExecutionMetrics,
    Handler,
    Iterator,
    LLMCallerFactory,
    MemoryConfig,
    RunnerConfig,
    SharedMemoryPool,
    StructuredLLMCallerProtocol,
    StructuredPrompt,
    datetime,
    extract_agent_adjacency,
    get_callback_manager,
    torch,
    uuid,
)


class RunnerCoreMixin:
    def __init__(
        self,
        llm_caller: Callable[[str], str] | None = None,
        async_llm_caller: Callable[[str], Awaitable[str]] | None = None,
        streaming_llm_caller: Callable[[str], Iterator[str]] | None = None,
        async_streaming_llm_caller: Callable[[str], AsyncIterator[str]] | None = None,
        token_counter: Callable[[str], int] | None = None,
        config: RunnerConfig | None = None,
        timeout: int = 60,
        memory_pool: SharedMemoryPool | None = None,
        # Multi-model support
        llm_callers: dict[str, Callable[[str], str]] | None = None,
        async_llm_callers: dict[str, Callable[[str], Awaitable[str]]] | None = None,
        llm_factory: LLMCallerFactory | None = None,
        # Tools support
        tool_registry: Any | None = None,
        # Structured prompt support (modern chat LLMs)
        structured_llm_caller: StructuredLLMCallerProtocol | None = None,
        async_structured_llm_caller: AsyncStructuredLLMCallerProtocol | None = None,
    ):
        """
        Create a MACP runner with multi-model and tools support.

        Args:
            llm_caller: Default synchronous LLM call (returns full response).
            async_llm_caller: Default asynchronous LLM call (returns full response).
            streaming_llm_caller: Synchronous streaming LLM (yields tokens).
            async_streaming_llm_caller: Asynchronous streaming LLM (async yields tokens).
            token_counter: Function to estimate tokens in text.
            config: Runner configuration (otherwise created with the given timeout).
            timeout: Default timeout (seconds), used if not specified in config.
            memory_pool: External SharedMemoryPool (if None — created automatically).

            # Multi-model support:
            llm_callers: Dict agent_id -> sync caller. Has highest priority.
            async_llm_callers: Dict agent_id -> async caller.
            llm_factory: Factory for creating callers based on agent LLM configurations.
                        Used if no explicit caller is set in llm_callers for the agent.

            # Tools support:
            tool_registry: Tool registry (ToolRegistry). Optional.
                          If an agent has tools, they are used automatically.

            # Structured prompt support:
            structured_llm_caller: Callable that receives a list of
                ``{"role": "system"|"user", "content": "..."}`` dicts
                and returns a string.  When provided the runner sends
                proper system/user roles to the LLM instead of a flat
                string — this typically produces shorter, more focused
                responses and saves tokens in long chains.
            async_structured_llm_caller: Async version of the above.

        Example:
            # Multi-model via caller dictionary
            runner = MACPRunner(
                llm_caller=default_gpt4_caller,
                llm_callers={
                    "analyzer": create_openai_caller(model="gpt-4o-mini"),
                    "expert": create_openai_caller(model="gpt-4-turbo"),
                }
            )

            # Multi-model via factory
            factory = LLMCallerFactory.create_openai_factory()
            runner = MACPRunner(llm_factory=factory)

            # Structured prompt (modern chat LLMs)
            def my_chat(messages: list[dict[str, str]]) -> str:
                return openai_client.chat.completions.create(
                    model="gpt-4", messages=messages
                ).choices[0].message.content

            runner = MACPRunner(structured_llm_caller=my_chat)

        """
        self.structured_llm_caller = structured_llm_caller
        self.async_structured_llm_caller = async_structured_llm_caller

        # When only a structured caller is provided, create a thin
        # str->str wrapper so all existing code paths that check
        # ``self.llm_caller is not None`` keep working.
        if llm_caller is None and structured_llm_caller is not None:
            _sc = structured_llm_caller  # capture for closure

            def _str_wrapper(prompt: str) -> str:
                return _sc([{"role": "user", "content": prompt}])

            llm_caller = _str_wrapper

        self.llm_caller = llm_caller
        self.async_llm_caller = async_llm_caller
        self.streaming_llm_caller = streaming_llm_caller
        self.async_streaming_llm_caller = async_streaming_llm_caller
        self.token_counter = token_counter or self._default_token_counter
        self.config = config or RunnerConfig(timeout=float(timeout))

        # Multi-model support
        self.llm_callers = llm_callers or {}
        self.async_llm_callers = async_llm_callers or {}
        self.llm_factory = llm_factory

        # Tools support
        self.tool_registry = tool_registry

        self._scheduler = (
            AdaptiveScheduler(
                policy=self.config.routing_policy,
                pruning_config=self.config.pruning_config,
            )
            if self.config.adaptive
            else None
        )

        self._callback_manager: CallbackManager | None = None
        self._budget_tracker: BudgetTracker | None = None
        self._metrics: ExecutionMetrics | None = None
        self._current_run_id: uuid.UUID | None = None

        # Memory integration
        self._memory_pool: SharedMemoryPool | None = memory_pool
        self._agent_memories: dict[str, AgentMemory] = {}

    @property
    def _run_id(self) -> uuid.UUID:
        """Return the current run ID, raising if not inside an active run."""
        rid = self._current_run_id
        if rid is None:
            msg = "No active run — call _init_run() first"
            raise RuntimeError(msg)
        return rid

    def _init_run(
        self,
        graph_name: str | None = None,  # noqa: ARG002
        num_agents: int = 0,
        query: str = "",
        execution_order: list[str] | None = None,
        callbacks: list[Handler] | None = None,
    ) -> uuid.UUID:
        """Initialize callbacks, budgets and metrics before running. Returns run_id."""
        # Merge config callbacks with per-run callbacks and context callbacks
        all_callbacks = list(self.config.callbacks)
        if callbacks:
            all_callbacks.extend(callbacks)

        # Check for context callback manager
        context_manager = get_callback_manager()
        if context_manager:
            all_callbacks.extend(context_manager.handlers)

        self._callback_manager = CallbackManager.configure(handlers=all_callbacks)

        if self.config.budget_config:
            self._budget_tracker = BudgetTracker(self.config.budget_config)
            self._budget_tracker.start()
        else:
            self._budget_tracker = None

        self._metrics = ExecutionMetrics(
            start_time=datetime.now(tz=UTC),
            total_agents=num_agents,
        )

        run_id = self._callback_manager.on_run_start(
            query=query,
            num_agents=num_agents,
            execution_order=execution_order or [],
        )
        self._current_run_id = run_id

        budget_cfg = self.config.budget_config
        if self._budget_tracker and self._callback_manager and budget_cfg:
            _original_warning = budget_cfg.on_budget_warning
            _cb = self._callback_manager
            _rid = run_id

            def _bridge_budget_warning(budget_type: str, budget: Any) -> None:
                if _original_warning:
                    _original_warning(budget_type, budget)
                _cb.on_budget_warning(
                    run_id=_rid,
                    budget_type=budget_type,
                    current=float(budget.used),
                    limit=float(budget.limit),
                    ratio=budget.usage_ratio,
                )

            budget_cfg.on_budget_warning = _bridge_budget_warning

        return run_id

    def _prepare_base_context(self, role_graph: Any) -> ExecutionContext | None:
        """
        Assemble the common initialization context from role_graph.

        Extracts task_idx, adjacency matrix, agent_ids list, query,
        and lookup dictionaries. Does not call ``_init_memory`` — each execution
        method does so itself (accounting for possible agent filtering).

        Returns:
            ``ExecutionContext`` with graph data, or ``None`` if there are no agents.

        """
        task_idx = self._get_task_index(role_graph)
        a_agents = extract_agent_adjacency(role_graph.A_com, task_idx)
        agent_ids, _ = self._get_agent_ids(role_graph, task_idx)

        if not agent_ids:
            return None

        query = role_graph.query or ""
        agent_lookup = {a.agent_id: a for a in role_graph.agents}
        agent_names = self._build_agent_names(role_graph)

        return ExecutionContext(
            task_idx=task_idx,
            a_agents=a_agents,
            agent_ids=agent_ids,
            query=query,
            agent_lookup=agent_lookup,
            agent_names=agent_names,
        )

    def _init_memory(self, agent_ids: list[str]) -> None:
        """Initialize memory for agents before execution."""
        if not self.config.enable_memory:
            return

        if self._memory_pool is None:
            self._memory_pool = SharedMemoryPool()

        mem_config = self.config.memory_config or MemoryConfig()

        for agent_id in agent_ids:
            if agent_id not in self._agent_memories:
                memory = AgentMemory(agent_id, mem_config)
                self._agent_memories[agent_id] = memory
                self._memory_pool.register(memory)

    def _get_memory_context(self, agent_id: str) -> list[dict[str, Any]]:
        """Get the latest entries from the agent's memory for context."""
        if not self.config.enable_memory or agent_id not in self._agent_memories:
            return []

        memory = self._agent_memories[agent_id]
        entries = memory.get_messages(limit=self.config.memory_context_limit)
        if self._callback_manager and entries:
            self._callback_manager.on_memory_read(
                run_id=self._run_id,
                agent_id=agent_id,
                entries_count=len(entries),
            )
        return entries

    def _save_to_memory(
        self,
        agent_id: str,
        response: str,
        incoming_ids: list[str] | None = None,
    ) -> None:
        """Save the agent's response to its memory and share with neighbors."""
        if not self.config.enable_memory or agent_id not in self._agent_memories:
            return

        memory = self._agent_memories[agent_id]
        entry = memory.add_message(role="assistant", content=response)

        if self._callback_manager:
            self._callback_manager.on_memory_write(
                run_id=self._run_id,
                agent_id=agent_id,
                key="assistant",
                value_size=len(response),
            )

        # Share with incoming agents (graph neighbors)
        if self._memory_pool and incoming_ids:
            self._memory_pool.share(agent_id, entry, to_agents=incoming_ids)

    def get_agent_memory(self, agent_id: str) -> AgentMemory | None:
        """Get the agent's memory by id (for external access)."""
        return self._agent_memories.get(agent_id)

    @property
    def memory_pool(self) -> SharedMemoryPool | None:
        """Access to the SharedMemoryPool."""
        return self._memory_pool

    # =========================================================================
    # DYNAMIC TOPOLOGY METHODS
    # =========================================================================

    def _finalize_run(
        self,
        run_id: uuid.UUID,
        *,
        success: bool,
        executed_agents: int,
        final_answer: str = "",
        error: BaseException | None = None,
        executed_agent_ids: list[str] | None = None,
        total_tokens: int = 0,
        total_time_ms: float = 0.0,
    ) -> None:
        """Finalize metrics and notify callbacks after execution."""
        if self._metrics:
            self._metrics.end_time = datetime.now(tz=UTC)
            self._metrics.executed_agents = executed_agents
            self._metrics.total_tokens = total_tokens

        effective_tokens = total_tokens or (self._metrics.total_tokens if self._metrics else 0)
        effective_time = total_time_ms or (self._metrics.duration_seconds * 1000 if self._metrics else 0)

        if self._callback_manager:
            self._callback_manager.on_run_end(
                run_id=run_id,
                output=final_answer,
                success=success,
                error=error,
                total_tokens=effective_tokens,
                total_time_ms=effective_time,
                executed_agents=executed_agent_ids or [],
            )

    @staticmethod
    def _default_token_counter(text: str) -> int:
        """Simple token estimate: 4/3 of the number of words."""
        return len(text.split()) * 4 // 3

    def _get_caller_for_agent(
        self,
        agent_id: str,
        agent: Any,
    ) -> Callable[[str], str] | None:
        """
        Get sync LLM caller for a specific agent.

        Priority:
        1. llm_callers[agent_id] — explicitly specified caller for the agent
        2. llm_factory.get_caller(agent.llm_config) — from the factory by configuration
        3. self.llm_caller — default caller

        Returns:
            LLM caller or None if none is available.

        """
        # 1. Check explicit per-agent caller
        if agent_id in self.llm_callers:
            return self.llm_callers[agent_id]

        # 2. Try factory if agent has LLM config
        if self.llm_factory and hasattr(agent, "get_llm_config"):
            llm_config = agent.get_llm_config()
            if llm_config and llm_config.is_configured():
                caller = self.llm_factory.get_caller(llm_config, agent_id)
                if caller:
                    return caller
        elif self.llm_factory and hasattr(agent, "llm_config") and agent.llm_config:
            caller = self.llm_factory.get_caller(agent.llm_config, agent_id)
            if caller:
                return caller

        # 3. Fallback to default caller
        return self.llm_caller

    def _get_async_caller_for_agent(
        self,
        agent_id: str,
        agent: Any,
    ) -> Callable[[str], Awaitable[str]] | None:
        """
        Get async LLM caller for a specific agent.

        Priority:
        1. async_llm_callers[agent_id] — explicitly specified async caller
        2. llm_factory.get_async_caller(agent.llm_config) — from the factory
        3. self.async_llm_caller — default async caller

        Note:
            May return ``None`` even when ``async_structured_llm_caller`` is
            set.  Callers must check ``self.async_structured_llm_caller``
            separately before treating ``None`` as a fatal error — see
            :meth:`_acall_llm` which dispatches to the structured path first.

        """
        # 1. Check explicit per-agent async caller
        if agent_id in self.async_llm_callers:
            return self.async_llm_callers[agent_id]

        # 2. Try factory if agent has LLM config
        if self.llm_factory and hasattr(agent, "get_llm_config"):
            llm_config = agent.get_llm_config()
            if llm_config and llm_config.is_configured():
                caller = self.llm_factory.get_async_caller(llm_config, agent_id)
                if caller:
                    return caller
        elif self.llm_factory and hasattr(agent, "llm_config") and agent.llm_config:
            caller = self.llm_factory.get_async_caller(agent.llm_config, agent_id)
            if caller:
                return caller

        # 3. Fallback to default async caller
        return self.async_llm_caller

    # ------------------------------------------------------------------
    # Structured prompt dispatch
    # ------------------------------------------------------------------

    def _call_llm(self, caller: Callable, prompt: StructuredPrompt) -> str:
        """
        Call the LLM using the best available interface.

        If a ``structured_llm_caller`` is registered, sends
        ``prompt.messages`` (proper system/user roles).
        Otherwise falls back to ``caller(prompt.text)`` (flat string).
        """
        if self.structured_llm_caller is not None:
            return self.structured_llm_caller(prompt.messages)
        return caller(prompt.text)

    async def _acall_llm(self, async_caller: Callable | None, prompt: StructuredPrompt) -> str:
        """Async version of :meth:`_call_llm`."""
        if self.async_structured_llm_caller is not None:
            return await self.async_structured_llm_caller(prompt.messages)
        if async_caller is None:
            msg = "No async LLM caller available"
            raise ValueError(msg)
        return await async_caller(prompt.text)

    def _has_any_caller(self) -> bool:
        """Check whether at least one LLM caller is available."""
        return bool(
            self.llm_caller
            or self.llm_callers
            or self.structured_llm_caller
            or (self.llm_factory and (self.llm_factory.default_caller or self.llm_factory.caller_builder))
        )

    def _has_any_async_caller(self) -> bool:
        """Check whether at least one async LLM caller is available."""
        return bool(
            self.async_llm_caller
            or self.async_llm_callers
            or self.async_structured_llm_caller
            or (self.llm_factory and (self.llm_factory.default_async_caller or self.llm_factory.async_caller_builder))
        )

    def _get_task_index(self, role_graph: Any) -> int:
        """Get the rustworkx index of the task node or raise an error."""
        if role_graph.task_node is None:
            msg = "RoleGraph has no task_node set"
            raise ValueError(msg)

        task_idx = role_graph.get_node_index(role_graph.task_node)
        if task_idx is None:
            msg = f"Task node '{role_graph.task_node}' not found"
            raise ValueError(msg)
        return task_idx

    def _get_agent_ids(
        self,
        role_graph: Any,
        task_idx: int,
    ) -> tuple[list[str], dict[str, int]]:
        """Return the list of agent_ids (excluding task) and the id->adjacency index map."""
        agent_ids = []
        id_to_idx = {}

        adj_idx = 0
        for agent in role_graph.agents:
            graph_idx = role_graph.get_node_index(agent.agent_id)
            if graph_idx == task_idx:
                continue

            agent_ids.append(agent.agent_id)
            id_to_idx[agent.agent_id] = adj_idx
            adj_idx += 1

        return agent_ids, id_to_idx

    def _extract_p_matrix(self, role_graph: Any, task_idx: int) -> torch.Tensor | None:
        """Return the probability matrix without the task row/column."""
        if role_graph.p_matrix is None:
            return None

        n_nodes = role_graph.p_matrix.shape[0]
        mask = torch.ones(n_nodes, dtype=torch.bool)
        mask[task_idx] = False
        return role_graph.p_matrix[mask][:, mask]

    def _get_edge_conditions(self, role_graph: Any) -> dict[tuple[str, str], Any]:
        """Get all edge conditions from the graph."""
        if hasattr(role_graph, "get_all_edge_conditions"):
            return role_graph.get_all_edge_conditions()
        # Fallback: check individual attributes
        conditions: dict[tuple[str, str], Any] = {}
        if hasattr(role_graph, "edge_condition_names"):
            conditions.update(role_graph.edge_condition_names)
        if hasattr(role_graph, "edge_conditions"):
            conditions.update(role_graph.edge_conditions)
        return conditions

    def _build_agent_names(self, role_graph: Any) -> dict[str, str]:
        """Map id -> display_name/role for building the prompt."""
        return {a.agent_id: a.display_name or getattr(a, "role", a.agent_id) for a in role_graph.agents}

    def _get_task_connected_agents(self, role_graph: Any) -> set[str]:
        """Get the set of agents directly connected to the task node."""
        if role_graph.task_node is None:
            return set()

        task_idx = role_graph.get_node_index(role_graph.task_node)
        if task_idx is None or role_graph.A_com is None:
            return set()

        connected = set()
        for agent in role_graph.agents:
            agent_idx = role_graph.get_node_index(agent.agent_id)
            if agent_idx is not None and agent_idx != task_idx and role_graph.A_com[task_idx, agent_idx] > 0:
                connected.add(agent.agent_id)
        return connected

    def _should_include_query(self, agent_id: str, task_connected: set[str]) -> bool:
        """Determine whether to include the query in the agent's prompt."""
        if self.config.broadcast_task_to_all:
            return True
        return agent_id in task_connected
