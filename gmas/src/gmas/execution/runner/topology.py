"""Dynamic topology and execution-plan mutation helpers for MACPRunner."""

from typing import TYPE_CHECKING

from .shared import (
    _MIN_EDGE_WEIGHT,
    Any,
    ConditionContext,
    EarlyStopCondition,
    ExecutionPlan,
    StepContext,
    StepResult,
    TopologyAction,
    deque,
)

if TYPE_CHECKING:
    from . import MACPRunner


class RunnerTopologyMixin:
    def _check_early_stop(
        self: "MACPRunner",
        agent_id: str,
        response: str | None,
        messages: dict[str, str],
        execution_order: list[str],
        remaining_agents: list[str],
        query: str,
        total_tokens: int,
    ) -> tuple[bool, str]:
        """
        Check early stop conditions.

        Returns:
            (should_stop, reason)

        """
        if not self.config.early_stop_conditions:
            return False, ""

        ctx = StepContext(
            agent_id=agent_id,
            response=response,
            messages=messages,
            execution_order=execution_order,
            remaining_agents=remaining_agents,
            query=query,
            total_tokens=total_tokens,
        )

        for condition in self.config.early_stop_conditions:
            if isinstance(condition, EarlyStopCondition):
                should_stop, reason = condition.should_stop(ctx)
                if should_stop:
                    return True, reason

        return False, ""

    def _apply_topology_hooks(
        self: "MACPRunner",
        agent_id: str,
        response: str | None,
        step_result: StepResult | None,
        messages: dict[str, str],
        execution_order: list[str],
        remaining_agents: list[str],
        query: str,
        total_tokens: int,
        role_graph: Any,
    ) -> TopologyAction | None:
        """
        Apply sync topology hooks and collect actions.

        Returns:
            Combined TopologyAction or None.

        """
        if not self.config.enable_dynamic_topology or not self.config.topology_hooks:
            return None

        ctx = StepContext(
            agent_id=agent_id,
            response=response,
            step_result=step_result,
            messages=messages,
            execution_order=execution_order,
            remaining_agents=remaining_agents,
            query=query,
            total_tokens=total_tokens,
        )

        combined_action = TopologyAction()

        for hook in self.config.topology_hooks:
            try:
                action = hook(ctx, role_graph)
                if action is not None:
                    combined_action = self._merge_topology_actions(combined_action, action)
            except (ValueError, TypeError, KeyError, RuntimeError):
                pass  # Ignore hook errors

        if self._has_topology_action(combined_action):
            return combined_action

        return None

    async def _apply_async_topology_hooks(
        self: "MACPRunner",
        agent_id: str,
        response: str | None,
        step_result: StepResult | None,
        messages: dict[str, str],
        execution_order: list[str],
        remaining_agents: list[str],
        query: str,
        total_tokens: int,
        role_graph: Any,
    ) -> TopologyAction | None:
        """Apply async topology hooks and collect actions."""
        if not self.config.enable_dynamic_topology or not self.config.async_topology_hooks:
            return None

        ctx = StepContext(
            agent_id=agent_id,
            response=response,
            step_result=step_result,
            messages=messages,
            execution_order=execution_order,
            remaining_agents=remaining_agents,
            query=query,
            total_tokens=total_tokens,
        )

        combined_action = TopologyAction()

        for hook in self.config.async_topology_hooks:
            try:
                action = await hook(ctx, role_graph)
                if action is not None:
                    combined_action = self._merge_topology_actions(combined_action, action)
            except (ValueError, TypeError, KeyError, RuntimeError):
                pass  # Ignore async hook errors

        if self._has_topology_action(combined_action):
            return combined_action

        return None

    def _merge_topology_actions(
        self: "MACPRunner",
        base: TopologyAction,
        new: TopologyAction,
    ) -> TopologyAction:
        """Merge two TopologyAction objects."""
        return TopologyAction(
            early_stop=base.early_stop or new.early_stop,
            early_stop_reason=new.early_stop_reason or base.early_stop_reason,
            add_edges=base.add_edges + new.add_edges,
            remove_edges=base.remove_edges + new.remove_edges,
            skip_agents=list(set(base.skip_agents + new.skip_agents)),
            force_agents=list(set(base.force_agents + new.force_agents)),
            condition_skip_agents=list(set(base.condition_skip_agents + new.condition_skip_agents)),
            condition_unskip_agents=list(set(base.condition_unskip_agents + new.condition_unskip_agents)),
            insert_chains=base.insert_chains + new.insert_chains,
            new_end_agent=new.new_end_agent or base.new_end_agent,
            trigger_rebuild=base.trigger_rebuild or new.trigger_rebuild,
        )

    def _apply_graph_modifications(
        self: "MACPRunner",
        role_graph: Any,
        action: TopologyAction,
    ) -> int:
        """Apply modifications to the graph and return the number of changes."""
        modifications = 0

        # Remove edges
        for src, tgt in action.remove_edges:
            if role_graph.remove_edge(src, tgt):
                modifications += 1

        # Add edges
        for src, tgt, weight in action.add_edges:
            if role_graph.add_edge(src, tgt, weight):
                modifications += 1

        return modifications

    @staticmethod
    def _has_topology_action(action: TopologyAction) -> bool:
        """Check whether the TopologyAction contains any actions."""
        return bool(
            action.early_stop
            or action.add_edges
            or action.remove_edges
            or action.skip_agents
            or action.force_agents
            or action.condition_skip_agents
            or action.condition_unskip_agents
            or action.insert_chains
            or action.new_end_agent
            or action.trigger_rebuild
        )

    def _build_conditional_edge_action(  # noqa: PLR0912
        self: "MACPRunner",
        last_agent: str,
        agent_ids: list[str],
        step_results: dict[str, StepResult],
        messages: dict[str, str],
        query: str,
        remaining_ids: set[str],
        plan: ExecutionPlan | None = None,
        *,
        messages_history: dict[str, list[str]] | None = None,
        step_results_history: dict[str, list[Any]] | None = None,
    ) -> TopologyAction | None:
        """
        Built-in topology hook: evaluate conditional edges and return TopologyAction.

        When ``plan`` is provided the method can distinguish a genuinely pending
        source (scheduled before the target) from a loopback source (scheduled
        after the target).  Loopback sources are irrelevant for the current
        iteration and are NOT treated as "pending".

        ``messages_history`` / ``step_results_history`` are forwarded into
        :class:`ConditionContext` so user-defined conditions can inspect
        previous iterations of the same agent.
        """
        if self._scheduler is None:
            return None

        edge_conditions = self._scheduler._last_edge_conditions  # noqa: SLF001
        if not edge_conditions or not messages:
            return None

        evaluator = self._scheduler.condition_evaluator
        executed_agents = set(step_results.keys())

        step_position: dict[str, int] = {}
        if plan is not None:
            for idx, step in enumerate(plan.steps):
                if step.agent_id not in step_position:
                    step_position[step.agent_id] = idx

        skip: list[str] = []
        unskip: list[str] = []
        chains: list[tuple[str, str]] = []

        for (source, target), condition in edge_conditions.items():
            if source != last_agent:
                continue
            if target not in agent_ids:
                continue

            ctx = ConditionContext(
                source_agent=source,
                target_agent=target,
                messages=messages,
                step_results=step_results,
                query=query,
                messages_history=messages_history or {},
                step_results_history=step_results_history or {},
            )

            if evaluator.evaluate(condition, ctx):
                unskip.append(target)
                if target not in remaining_ids:
                    chains.append((target, last_agent))
            elif target in remaining_ids:
                target_pos = step_position.get(target)
                has_pending_incoming = False
                has_passed_incoming = False
                for (src, tgt), cond in edge_conditions.items():
                    if tgt != target or src == source:
                        continue
                    if src not in executed_agents:
                        if target_pos is not None:
                            src_pos = step_position.get(src)
                            if src_pos is not None and src_pos > target_pos:
                                continue
                        has_pending_incoming = True
                        break
                    other_ctx = ConditionContext(
                        source_agent=src,
                        target_agent=target,
                        messages=messages,
                        step_results=step_results,
                        query=query,
                        messages_history=messages_history or {},
                        step_results_history=step_results_history or {},
                    )
                    if evaluator.evaluate(cond, other_ctx):
                        has_passed_incoming = True
                        break

                if not has_pending_incoming and not has_passed_incoming:
                    skip.append(target)

        if not skip and not unskip and not chains:
            return None

        return TopologyAction(
            condition_skip_agents=skip,
            condition_unskip_agents=unskip,
            insert_chains=chains,
        )

    def _apply_topology_to_plan(
        self: "MACPRunner",
        plan: ExecutionPlan,
        action: TopologyAction,
        a_agents: Any,
        agent_ids: list[str],
    ) -> bool:
        """
        Apply a TopologyAction to the ExecutionPlan.

        Single method for modifying the plan from any source:
        user hooks, built-in conditional edge hook, etc.

        Args:
            plan: Current execution plan.
            action: Action to apply.
            a_agents: Adjacency matrix (for BFS chains).
            agent_ids: List of agent IDs.

        Returns:
            True if the plan was modified.

        """
        changed = False
        edge_conditions = self._scheduler._last_edge_conditions if self._scheduler else {}  # noqa: SLF001

        # 1. Condition skip + cascade skip of unconditional descendants
        for agent_id in action.condition_skip_agents:
            plan.apply_condition_skip(agent_id)
            changed = True
            # Cascading condition_skip of unconditional descendants (BFS)
            self._cascade_condition_skip(
                plan,
                agent_id,
                a_agents,
                agent_ids,
                edge_conditions,
            )

        # 2. Condition unskip + cascade unskip of unconditional descendants
        for agent_id in action.condition_unskip_agents:
            plan.clear_condition_skip(agent_id)
            changed = True
            self._cascade_condition_unskip(
                plan,
                agent_id,
                a_agents,
                agent_ids,
                edge_conditions,
            )

        # 3. Skip (permanent skip)
        remaining_ids = {s.agent_id for s in plan.steps[plan.current_index :]}
        for agent_id in action.skip_agents:
            if agent_id in remaining_ids:
                plan.apply_condition_skip(agent_id)
                changed = True

        # 4. Force agents — add to the plan if not already there
        for agent_id in action.force_agents:
            if agent_id not in remaining_ids and agent_id in agent_ids:
                plan.clear_condition_skip(agent_id)
                added = plan.insert_conditional_step(agent_id=agent_id, predecessors=[], dependency_step_ids=[])
                if added is not None:
                    remaining_ids.add(agent_id)
                    changed = True

        # 5. Insert chains — add the agent + its unconditional chain
        predecessor_steps = {predecessor: plan.get_latest_step(predecessor) for _, predecessor in action.insert_chains}
        for target, predecessor in action.insert_chains:
            predecessor_step = predecessor_steps.get(predecessor)
            if predecessor_step is None:
                continue
            plan.clear_condition_skip(target)
            added = plan.insert_conditional_step(
                agent_id=target,
                predecessors=[predecessor],
                dependency_step_ids=[predecessor_step.step_id],
            )
            if added is not None:
                remaining_ids.add(target)
                changed = True
                # BFS — add or update the unconditional chain after target
                self._insert_unconditional_chain(
                    plan,
                    added,
                    a_agents,
                    agent_ids,
                    edge_conditions,
                    remaining_ids,
                )

        return changed

    @staticmethod
    def _cascade_condition_skip(
        plan: ExecutionPlan,
        skipped_agent: str,
        a_agents: Any,
        agent_ids: list[str],
        edge_conditions: dict[tuple[str, str], Any],
    ) -> None:
        """
        BFS: cascading condition_skip of unconditional descendants of skipped_agent.

        If an agent is condition_skipped, its unconditional descendants should also be
        skipped (if they have no other incoming data paths).
        """
        queue = deque([skipped_agent])
        visited = {skipped_agent}
        remaining_ids = {s.agent_id for s in plan.steps[plan.current_index :]}

        while queue:
            current = queue.popleft()
            if current not in agent_ids:
                continue

            current_idx = agent_ids.index(current)
            for j, aid in enumerate(agent_ids):
                if aid in visited or aid not in remaining_ids:
                    continue

                weight = a_agents[current_idx, j]
                if hasattr(weight, "item"):
                    weight = weight.item()
                if weight <= _MIN_EDGE_WEIGHT:
                    continue

                # Skip conditional edges — they are handled separately
                if (current, aid) in edge_conditions:
                    continue

                # Check: does aid have other incoming unconditional edges
                # from agents that are NOT condition_skipped?
                has_other_source = False
                for k, src in enumerate(agent_ids):
                    if src == current or src in plan.condition_skipped:
                        continue
                    w = a_agents[k, j]
                    if hasattr(w, "item"):
                        w = w.item()
                    if w > _MIN_EDGE_WEIGHT and (src, aid) not in edge_conditions:
                        has_other_source = True
                        break

                if not has_other_source:
                    visited.add(aid)
                    plan.apply_condition_skip(aid)
                    queue.append(aid)

    @staticmethod
    def _cascade_condition_unskip(
        plan: ExecutionPlan,
        unskipped_agent: str,
        a_agents: Any,
        agent_ids: list[str],
        edge_conditions: dict[tuple[str, str], Any],
    ) -> None:
        """BFS: clear condition_skip on unconditional descendants of an activated agent."""
        queue = deque([unskipped_agent])
        visited = {unskipped_agent}
        remaining_ids = {s.agent_id for s in plan.steps[plan.current_index :]}

        while queue:
            current = queue.popleft()
            if current not in agent_ids:
                continue

            current_idx = agent_ids.index(current)
            for j, aid in enumerate(agent_ids):
                if aid in visited or aid not in remaining_ids:
                    continue

                weight = a_agents[current_idx, j]
                if hasattr(weight, "item"):
                    weight = weight.item()
                if weight <= _MIN_EDGE_WEIGHT:
                    continue

                if (current, aid) in edge_conditions:
                    continue

                visited.add(aid)
                plan.clear_condition_skip(aid)
                queue.append(aid)

    @staticmethod
    def _insert_unconditional_chain(
        plan: ExecutionPlan,
        start_step: Any,
        a_agents: Any,
        agent_ids: list[str],
        edge_conditions: dict[tuple[str, str], Any],
        remaining_ids: set[str],
    ) -> None:
        """
        BFS: add a chain of unconditionally linked agents to the plan after start_agent.

        Traverses edges without conditions and adds all subsequent agents to the plan.
        """
        if isinstance(start_step, str):
            start_step = plan.get_latest_step(start_step)
        if start_step is None:
            return

        queue = deque([start_step])
        visited = {start_step.step_id}

        while queue:
            current_step = queue.popleft()
            current = current_step.agent_id
            if current not in agent_ids:
                continue

            current_idx = agent_ids.index(current)
            for j, aid in enumerate(agent_ids):
                weight = a_agents[current_idx, j]
                if hasattr(weight, "item"):
                    weight = weight.item()
                if weight <= _MIN_EDGE_WEIGHT:
                    continue

                # Skip conditional edges — they are handled separately
                if (current, aid) in edge_conditions:
                    continue

                plan.clear_condition_skip(aid)
                next_step = plan.insert_conditional_step(
                    agent_id=aid,
                    predecessors=[current],
                    dependency_step_ids=[current_step.step_id],
                )
                if next_step is None:
                    continue

                remaining_ids.add(aid)

                has_other_source = RunnerTopologyMixin._has_other_unconditional_source(
                    current=current,
                    target=aid,
                    target_index=j,
                    plan=plan,
                    a_agents=a_agents,
                    agent_ids=agent_ids,
                    edge_conditions=edge_conditions,
                )

                if next_step.step_id not in visited:
                    visited.add(next_step.step_id)
                    if not has_other_source:
                        queue.append(next_step)

    @staticmethod
    def _has_other_unconditional_source(
        current: str,
        target: str,
        target_index: int,
        plan: ExecutionPlan,
        a_agents: Any,
        agent_ids: list[str],
        edge_conditions: dict[tuple[str, str], Any],
    ) -> bool:
        """Check whether the target still has another active unconditional source."""
        for k, src in enumerate(agent_ids):
            if src == current or src in plan.condition_skipped:
                continue
            other_weight = a_agents[k, target_index]
            if hasattr(other_weight, "item"):
                other_weight = other_weight.item()
            if other_weight > _MIN_EDGE_WEIGHT and (src, target) not in edge_conditions:
                return True
        return False

    def _run_topology_pipeline(
        self: "MACPRunner",
        plan: ExecutionPlan,
        last_agent: str,
        a_agents: Any,
        agent_ids: list[str],
        step_results: dict[str, StepResult],
        messages: dict[str, str],
        query: str,
        execution_order: list[str],
        total_tokens: int,
        role_graph: Any,
        *,
        messages_history: dict[str, list[str]] | None = None,
        step_results_history: dict[str, list[StepResult]] | None = None,
    ) -> bool:
        """
        Unified sync pipeline: built-in conditional hook + user hooks → plan.

        Called after each step in adaptive methods.
        Combines all TopologyAction objects and applies them to the plan.

        Returns:
            True if the plan was modified.

        """
        remaining = [s.agent_id for s in plan.remaining_steps]

        # 1. Built-in hook: conditional edges
        # Only include agent_ids from *unresolved* steps so that
        # already-completed loop iterations don't block re-insertion.
        remaining_ids = {s.agent_id for s in plan.steps[plan.current_index :] if not plan.is_step_resolved(s)}
        cond_action = self._build_conditional_edge_action(
            last_agent,
            agent_ids,
            step_results,
            messages,
            query,
            remaining_ids,
            plan=plan,
            messages_history=messages_history,
            step_results_history=step_results_history,
        )

        # 2. User topology hooks
        user_action = self._apply_topology_hooks(
            last_agent,
            messages.get(last_agent),
            step_results.get(last_agent),
            messages,
            execution_order,
            remaining,
            query,
            total_tokens,
            role_graph,
        )

        # 3. Combine actions
        combined = TopologyAction()
        if cond_action is not None:
            combined = self._merge_topology_actions(combined, cond_action)
        if user_action is not None:
            combined = self._merge_topology_actions(combined, user_action)

        if not self._has_topology_action(combined):
            return False

        # 4. Graph modification (add_edges / remove_edges)
        if combined.add_edges or combined.remove_edges:
            self._apply_graph_modifications(role_graph, combined)

        # 5. Plan modification
        return self._apply_topology_to_plan(plan, combined, a_agents, agent_ids)

    async def _arun_topology_pipeline(
        self: "MACPRunner",
        plan: ExecutionPlan,
        last_agent: str,
        a_agents: Any,
        agent_ids: list[str],
        step_results: dict[str, StepResult],
        messages: dict[str, str],
        query: str,
        execution_order: list[str],
        total_tokens: int,
        role_graph: Any,
        *,
        messages_history: dict[str, list[str]] | None = None,
        step_results_history: dict[str, list[StepResult]] | None = None,
    ) -> bool:
        """
        Unified async pipeline: built-in conditional hook + async user hooks → plan.

        Returns:
            True if the plan was modified.

        """
        remaining = [s.agent_id for s in plan.remaining_steps]

        # 1. Built-in hook: conditional edges (sync — fast)
        remaining_ids = {s.agent_id for s in plan.steps[plan.current_index :] if not plan.is_step_resolved(s)}
        cond_action = self._build_conditional_edge_action(
            last_agent,
            agent_ids,
            step_results,
            messages,
            query,
            remaining_ids,
            plan=plan,
            messages_history=messages_history,
            step_results_history=step_results_history,
        )

        # 2. User async topology hooks
        user_action = await self._apply_async_topology_hooks(
            last_agent,
            messages.get(last_agent),
            step_results.get(last_agent),
            messages,
            execution_order,
            remaining,
            query,
            total_tokens,
            role_graph,
        )

        # 3. Combine actions
        combined = TopologyAction()
        if cond_action is not None:
            combined = self._merge_topology_actions(combined, cond_action)
        if user_action is not None:
            combined = self._merge_topology_actions(combined, user_action)

        if not self._has_topology_action(combined):
            return False

        # 4. Graph modification
        if combined.add_edges or combined.remove_edges:
            self._apply_graph_modifications(role_graph, combined)

        # 5. Plan modification
        return self._apply_topology_to_plan(plan, combined, a_agents, agent_ids)
