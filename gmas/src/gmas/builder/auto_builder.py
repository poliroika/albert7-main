"""
LLM-powered automatic graph assembly.

Two modes:
- **assemble_topology**: given existing AgentProfile objects, ask an LLM to
  propose the optimal workflow edges (chains, fan-out/fan-in, diamonds, etc.).
- **assemble_full**: given only a task query, ask an LLM to design the agents
  (with appropriate tools) *and* the topology from scratch.

Both modes produce a ``RoleGraph`` via the standard ``GraphBuilder`` pipeline.
"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from gmas.builder.graph_builder import BuilderConfig, GraphBuilder

if TYPE_CHECKING:
    from gmas.core.graph import RoleGraph

__all__ = [
    "AutoBuilderConfig",
    "AutoGraphBuilder",
]

_MIN_AGENTS = 2

# ---------------------------------------------------------------------------
# Built-in tool descriptions (used when user passes list[str] or nothing)
# ---------------------------------------------------------------------------

FRAMEWORK_TOOLS: dict[str, str] = {
    "web_search": "Search the web via multiple providers (DuckDuckGo, Brave, Serper, Tavily, etc.), "
    "fetch pages, interact with dynamic content via Playwright/Selenium",
    "code_interpreter": "Execute Python code in a sandboxed environment, "
    "run scripts, install packages, produce outputs",
    "shell": "Execute shell/terminal commands on the host system",
    "file_search": "Search and read local files by path or pattern",
    "computer_use": "Stateful desktop automation — screenshots, keyboard/mouse, window management, text extraction",
}

# ---------------------------------------------------------------------------
# Type aliases (mirrors runner.py conventions)
# ---------------------------------------------------------------------------

StructuredCaller = Callable[[list[dict[str, str]]], str]
AsyncStructuredCaller = Callable[[list[dict[str, str]]], Awaitable[str]]

# ---------------------------------------------------------------------------
# Pydantic response models (internal — for parsing LLM output)
# ---------------------------------------------------------------------------


class AgentSpec(BaseModel):
    """Single agent specification returned by the LLM."""

    agent_id: str
    persona: str = ""
    description: str = ""
    tools: list[str] = Field(default_factory=list)


class AgentsResponse(BaseModel):
    """LLM response for agent generation (mode 2, step 1)."""

    agents: list[AgentSpec]
    reasoning: str = ""


class TopologyResponse(BaseModel):
    """LLM response for topology generation."""

    edges: list[list[str]]  # [[src, tgt], ...]
    start_node: str | None = None
    end_node: str | None = None
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class AutoBuilderConfig(BaseModel):
    """
    Settings for :class:`AutoGraphBuilder`.

    Custom prompts:
        ``topology_prompt`` and ``agents_prompt`` let you replace the
        built-in system prompts sent to the LLM.

        The topology prompt receives the agent descriptions and task
        via the user message; only the *system* message is customisable.

        The agents prompt is formatted with ``{max_agents}`` and
        ``{tools_section}`` placeholders (use ``{{`` / ``}}`` for literal
        braces in the JSON example inside your prompt).
    """

    max_retries: int = Field(default=3, ge=1, le=10)
    max_agents: int = Field(default=10, ge=2, le=50)
    include_task_node: bool = True
    default_llm_backbone: str | None = None
    default_temperature: float | None = None
    available_tools: list[str] | dict[str, str] | None = None
    builder_config: BuilderConfig | None = None
    topology_prompt: str | None = Field(
        default=None,
        description="Custom system prompt for topology generation. If None, the built-in prompt is used.",
    )
    agents_prompt: str | None = Field(
        default=None,
        description="Custom system prompt for agent generation. "
        "Formatted with {max_agents} and {tools_section}. "
        "If None, the built-in prompt is used.",
    )

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_TOPOLOGY_SYSTEM = """\
You are an expert workflow architect for multi-agent systems.
Given a task description and a set of agents (with their tools and capabilities), \
design the optimal directed workflow graph.

TOPOLOGY PATTERNS — use the right one for the task:
- **Chain** (A->B->C): when steps are strictly sequential.
- **Fan-out** (A->B, A->C, A->D): when one agent's output feeds multiple \
  independent agents that can work IN PARALLEL.
- **Fan-in** (B->D, C->D): when multiple parallel results must be merged \
  by a single agent.
- **Diamond** (A->B, A->C, B->D, C->D): fan-out then fan-in — parallel \
  processing with aggregation.
- **Star** (A->B, A->C, A->D, B->A, C->A, D->A): hub agent coordinates others.
- **Mixed**: combine patterns as needed.

Rules:
- Every agent must participate (no isolated nodes).
- The graph must be a DAG (directed acyclic graph) — no cycles.
- USE PARALLELISM when agents can work independently on different sub-tasks.
- Agents with tools (web_search, code_interpreter, etc.) should be placed \
  where their capabilities are most useful.
- Choose one start node (entry point) and one end node (final output).

Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{
  "edges": [["source_agent_id", "target_agent_id"], ...],
  "start_node": "agent_id",
  "end_node": "agent_id",
  "reasoning": "Brief explanation: which pattern you chose and why"
}

Example -- diamond topology for a research task:
{
  "edges": [
    ["planner", "web_researcher"],
    ["planner", "data_analyst"],
    ["web_researcher", "synthesizer"],
    ["data_analyst", "synthesizer"]
  ],
  "start_node": "planner",
  "end_node": "synthesizer",
  "reasoning": "Diamond: planner splits work, web_researcher and data_analyst work in parallel, synthesizer merges"
}"""

_TOPOLOGY_USER = """\
Task: {query}

Available agents:
{agents_description}

Design the workflow graph. Choose the best topology pattern for this task \
(chain, fan-out/fan-in, diamond, mixed, etc.)."""

_AGENTS_SYSTEM = """\
You are an expert multi-agent system designer.
Given a task description, propose a minimal but sufficient set of \
specialised agents that can solve the task collaboratively.

Rules:
- Each agent must have a unique snake_case agent_id (e.g. "researcher", "code_writer").
- Provide a clear persona (role description in 3-10 words) and description (capabilities).
- Keep the number of agents between 2 and {max_agents}.
- Assign tools to agents that NEED them for their specific role. \
  Not every agent needs tools — agents without tools rely on LLM reasoning.
- Only assign tools from the available list below.
{tools_section}
Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{
  "agents": [
    {{
      "agent_id": "unique_id",
      "persona": "role in 3-10 words",
      "description": "what this agent does and how",
      "tools": ["tool_name"]
    }}
  ],
  "reasoning": "Brief explanation of your design choices"
}}"""

_AGENTS_USER = """\
Task: {query}

Design the agent team. Assign appropriate tools to agents that need them."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_json(text: str) -> str:
    """Remove markdown code fences and leading/trailing whitespace."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _describe_agents(agents: Sequence[Any]) -> str:
    """Build a human-readable description of agents for the prompt."""
    lines: list[str] = []
    for a in agents:
        parts = [f"- {a.agent_id}:"]

        persona = getattr(a, "persona", "")
        if persona:
            parts.append(f'persona="{persona}"')

        desc = getattr(a, "description", "")
        if desc:
            parts.append(f'description="{desc}"')

        tool_names: list[str] = []
        if hasattr(a, "get_tool_names"):
            tool_names = a.get_tool_names()
        elif hasattr(a, "tools") and a.tools:
            tool_names = list(a.tools)

        if tool_names:
            parts.append(f"tools={tool_names}")
        else:
            parts.append("tools=[] (LLM reasoning only)")

        lines.append(" ".join(parts))
    return "\n".join(lines)


def _build_tools_section(tools: dict[str, str]) -> str:
    """Format the available tools as a readable list for the prompt."""
    lines = ["\nAvailable tools:"]
    for name, desc in tools.items():
        lines.append(f"  - {name}: {desc}")
    return "\n".join(lines)


def _resolve_tools(raw: list[str] | dict[str, str] | None) -> dict[str, str]:
    """Normalize available_tools config into a {name: description} dict."""
    if raw is None:
        return dict(FRAMEWORK_TOOLS)
    if isinstance(raw, dict):
        return dict(raw)
    return {name: FRAMEWORK_TOOLS.get(name, "Tool") for name in raw}


# ---------------------------------------------------------------------------
# AutoGraphBuilder
# ---------------------------------------------------------------------------


class AutoGraphBuilder:
    """
    LLM-powered automatic graph assembly.

    Example — topology from existing agents::

        auto = AutoGraphBuilder(llm_caller=my_caller)
        graph = auto.assemble_topology(
            agents=[solver, reviewer],
            query="Solve and verify math problems",
        )

    Example — full assembly from scratch::

        auto = AutoGraphBuilder(
            llm_caller=my_caller,
            config=AutoBuilderConfig(max_agents=5),
        )
        graph = auto.assemble_full(query="Research market trends")

    """

    def __init__(
        self,
        llm_caller: StructuredCaller | None = None,
        async_llm_caller: AsyncStructuredCaller | None = None,
        config: AutoBuilderConfig | None = None,
    ):
        if llm_caller is None and async_llm_caller is None:
            msg = "At least one of llm_caller or async_llm_caller is required"
            raise ValueError(msg)

        self._caller = llm_caller
        self._async_caller = async_llm_caller
        self.config = config or AutoBuilderConfig()

    # ------------------------------------------------------------------
    # Sync public API
    # ------------------------------------------------------------------

    def assemble_topology(
        self,
        agents: Sequence[Any],
        query: str,
        *,
        builder_config: BuilderConfig | None = None,
        system_prompt: str | None = None,
    ) -> "RoleGraph":
        """
        Build a graph from existing agents by asking the LLM for topology.

        Args:
            agents: Pre-built ``AgentProfile`` objects.
            query: Task description for the LLM.
            builder_config: Override the default ``BuilderConfig``.
            system_prompt: Custom system prompt for the LLM. Overrides
                both per-call and ``config.topology_prompt``.

        Returns:
            Assembled ``RoleGraph``.

        """
        if self._caller is None:
            msg = "Sync llm_caller is required for assemble_topology"
            raise ValueError(msg)
        if len(agents) < _MIN_AGENTS:
            msg = "At least 2 agents are required"
            raise ValueError(msg)

        topology = self._generate_topology_sync(agents, query, system_prompt=system_prompt)
        return self._build_graph(agents, topology, query, builder_config)

    def assemble_full(
        self,
        query: str,
        *,
        builder_config: BuilderConfig | None = None,
        topology_prompt: str | None = None,
        agents_prompt: str | None = None,
    ) -> "RoleGraph":
        """
        Design agents and topology from scratch.

        Args:
            query: Task description — the LLM will propose agents and topology.
            builder_config: Override the default ``BuilderConfig``.
            topology_prompt: Custom system prompt for topology generation.
                Overrides ``config.topology_prompt``.
            agents_prompt: Custom system prompt for agent generation.
                Formatted with ``{max_agents}`` and ``{tools_section}``.
                Overrides ``config.agents_prompt``.

        Returns:
            Assembled ``RoleGraph``.

        """
        if self._caller is None:
            msg = "Sync llm_caller is required for assemble_full"
            raise ValueError(msg)

        agents_resp = self._generate_agents_sync(query, system_prompt=agents_prompt)
        agents = self._specs_to_profiles(agents_resp.agents)
        topology = self._generate_topology_sync(agents, query, system_prompt=topology_prompt)
        return self._build_graph(agents, topology, query, builder_config)

    # ------------------------------------------------------------------
    # Async public API
    # ------------------------------------------------------------------

    async def assemble_topology_async(
        self,
        agents: Sequence[Any],
        query: str,
        *,
        builder_config: BuilderConfig | None = None,
        system_prompt: str | None = None,
    ) -> "RoleGraph":
        """Async version of :meth:`assemble_topology`."""
        if self._async_caller is None:
            msg = "async_llm_caller is required for assemble_topology_async"
            raise ValueError(msg)
        if len(agents) < _MIN_AGENTS:
            msg = "At least 2 agents are required"
            raise ValueError(msg)

        topology = await self._generate_topology_async(agents, query, system_prompt=system_prompt)
        return self._build_graph(agents, topology, query, builder_config)

    async def assemble_full_async(
        self,
        query: str,
        *,
        builder_config: BuilderConfig | None = None,
        topology_prompt: str | None = None,
        agents_prompt: str | None = None,
    ) -> "RoleGraph":
        """Async version of :meth:`assemble_full`."""
        if self._async_caller is None:
            msg = "async_llm_caller is required for assemble_full_async"
            raise ValueError(msg)

        agents_resp = await self._generate_agents_async(query, system_prompt=agents_prompt)
        agents = self._specs_to_profiles(agents_resp.agents)
        topology = await self._generate_topology_async(agents, query, system_prompt=topology_prompt)
        return self._build_graph(agents, topology, query, builder_config)

    # ------------------------------------------------------------------
    # Graph construction (shared by sync & async paths)
    # ------------------------------------------------------------------

    def _build_graph(
        self,
        agents: Sequence[Any],
        topology: TopologyResponse,
        query: str,
        builder_config: BuilderConfig | None,
    ) -> "RoleGraph":
        """Assemble a RoleGraph from agents + topology via GraphBuilder."""
        cfg = (
            builder_config
            or self.config.builder_config
            or BuilderConfig(
                include_task_node=self.config.include_task_node,
            )
        )
        builder = GraphBuilder(cfg)

        agent_ids = set()
        for agent in agents:
            builder.add_agent_profile(agent)
            agent_ids.add(agent.agent_id)

        if cfg.include_task_node:
            builder.add_task(query=query)

        for edge in topology.edges:
            src, tgt = edge[0], edge[1]
            if src in agent_ids and tgt in agent_ids:
                builder.add_workflow_edge(src, tgt)

        if cfg.include_task_node:
            builder.connect_task_to_agents(bidirectional=True)

        if topology.start_node and topology.start_node in agent_ids:
            builder.set_start_node(topology.start_node)
        if topology.end_node and topology.end_node in agent_ids:
            builder.set_end_node(topology.end_node)

        return builder.build()

    # ------------------------------------------------------------------
    # Sync LLM calls with retry
    # ------------------------------------------------------------------

    def _resolve_topology_prompt(self, override: str | None) -> str:
        """Return the topology system prompt (per-call > config > default)."""
        return override or self.config.topology_prompt or _TOPOLOGY_SYSTEM

    def _resolve_agents_prompt(self, override: str | None, tools_dict: dict[str, str]) -> str:
        """
        Return the agents system prompt (per-call > config > default).

        If the resolved prompt contains ``{max_agents}`` / ``{tools_section}``
        placeholders they will be filled in; otherwise the prompt is used as-is.
        """
        base = override or self.config.agents_prompt
        if base is not None:
            tools_section = _build_tools_section(tools_dict)
            try:
                return base.format(
                    max_agents=self.config.max_agents,
                    tools_section=tools_section,
                )
            except KeyError:
                return base
        return self._build_agents_system_prompt(tools_dict)

    def _generate_topology_sync(
        self,
        agents: Sequence[Any],
        query: str,
        *,
        system_prompt: str | None = None,
    ) -> TopologyResponse:
        """Call LLM to generate topology, with retry on parse/validation errors."""
        assert self._caller is not None  # noqa: S101

        agent_ids = {a.agent_id for a in agents}
        user_msg = _TOPOLOGY_USER.format(
            query=query,
            agents_description=_describe_agents(agents),
        )
        effective_prompt = self._resolve_topology_prompt(system_prompt)

        last_error = ""
        for attempt in range(self.config.max_retries):
            messages: list[dict[str, str]] = [
                {"role": "system", "content": effective_prompt},
                {"role": "user", "content": user_msg},
            ]
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your previous response was invalid: {last_error}\nPlease fix and try again.",
                    }
                )

            raw = self._caller(messages)
            result, error = self._parse_topology(raw, agent_ids)
            if result is not None:
                return result
            last_error = error or f"Parse error on attempt {attempt + 1}"

        msg = f"Failed to generate valid topology after {self.config.max_retries} attempts: {last_error}"
        raise ValueError(msg)

    def _generate_agents_sync(
        self,
        query: str,
        *,
        system_prompt: str | None = None,
    ) -> AgentsResponse:
        """Call LLM to generate agent specs, with retry."""
        assert self._caller is not None  # noqa: S101

        tools_dict = _resolve_tools(self.config.available_tools)
        allowed = set(tools_dict.keys())
        system_msg = self._resolve_agents_prompt(system_prompt, tools_dict)
        user_msg = _AGENTS_USER.format(query=query)

        last_error = ""
        for attempt in range(self.config.max_retries):
            messages: list[dict[str, str]] = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ]
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your previous response was invalid: {last_error}\nPlease fix and try again.",
                    }
                )

            raw = self._caller(messages)
            result, error = self._parse_agents(raw, allowed)
            if result is not None:
                return result
            last_error = error or f"Parse error on attempt {attempt + 1}"

        msg = f"Failed to generate valid agents after {self.config.max_retries} attempts: {last_error}"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Async LLM calls with retry
    # ------------------------------------------------------------------

    async def _generate_topology_async(
        self,
        agents: Sequence[Any],
        query: str,
        *,
        system_prompt: str | None = None,
    ) -> TopologyResponse:
        """Async version of :meth:`_generate_topology_sync`."""
        assert self._async_caller is not None  # noqa: S101

        agent_ids = {a.agent_id for a in agents}
        user_msg = _TOPOLOGY_USER.format(
            query=query,
            agents_description=_describe_agents(agents),
        )
        effective_prompt = self._resolve_topology_prompt(system_prompt)

        last_error = ""
        for attempt in range(self.config.max_retries):
            messages: list[dict[str, str]] = [
                {"role": "system", "content": effective_prompt},
                {"role": "user", "content": user_msg},
            ]
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your previous response was invalid: {last_error}\nPlease fix and try again.",
                    }
                )

            raw = await self._async_caller(messages)
            result, error = self._parse_topology(raw, agent_ids)
            if result is not None:
                return result
            last_error = error or f"Parse error on attempt {attempt + 1}"

        msg = f"Failed to generate valid topology after {self.config.max_retries} attempts: {last_error}"
        raise ValueError(msg)

    async def _generate_agents_async(
        self,
        query: str,
        *,
        system_prompt: str | None = None,
    ) -> AgentsResponse:
        """Async version of :meth:`_generate_agents_sync`."""
        assert self._async_caller is not None  # noqa: S101

        tools_dict = _resolve_tools(self.config.available_tools)
        allowed = set(tools_dict.keys())
        system_msg = self._resolve_agents_prompt(system_prompt, tools_dict)
        user_msg = _AGENTS_USER.format(query=query)

        last_error = ""
        for attempt in range(self.config.max_retries):
            messages: list[dict[str, str]] = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ]
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your previous response was invalid: {last_error}\nPlease fix and try again.",
                    }
                )

            raw = await self._async_caller(messages)
            result, error = self._parse_agents(raw, allowed)
            if result is not None:
                return result
            last_error = error or f"Parse error on attempt {attempt + 1}"

        msg = f"Failed to generate valid agents after {self.config.max_retries} attempts: {last_error}"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Parsing and validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_topology_edges(
        edges: list[Any],
        valid_agent_ids: set[str],
    ) -> str | None:
        """Return an error string if any edge is invalid, else None."""
        bad_ids: set[str] = set()
        for edge in edges:
            if len(edge) != _MIN_AGENTS:
                return f"Edge must have exactly 2 elements, got {len(edge)}: {edge}"
            for node_id in edge:
                if node_id not in valid_agent_ids:
                    bad_ids.add(node_id)
        if bad_ids:
            return f"Unknown agent IDs in edges: {bad_ids}. Valid IDs: {valid_agent_ids}"
        all_in_edges = {node_id for edge in edges for node_id in edge}
        isolated = valid_agent_ids - all_in_edges
        if isolated:
            return f"Isolated agents (not in any edge): {isolated}"
        return None

    @staticmethod
    def _topology_has_cycle(edges: list[Any], valid_agent_ids: set[str]) -> bool:
        """Return True if the directed graph defined by edges contains a cycle."""
        adj: dict[str, list[str]] = {aid: [] for aid in valid_agent_ids}
        for src, tgt in edges:
            adj[src].append(tgt)

        visited: set[str] = set()
        in_stack: set[str] = set()

        def _dfs(node: str) -> bool:
            visited.add(node)
            in_stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor in in_stack:
                    return True
                if neighbor not in visited and _dfs(neighbor):
                    return True
            in_stack.discard(node)
            return False

        return any(start not in visited and _dfs(start) for start in valid_agent_ids)

    @staticmethod
    def _parse_topology(
        raw: str,
        valid_agent_ids: set[str],
    ) -> tuple[TopologyResponse | None, str | None]:
        """Parse and validate an LLM topology response."""
        try:
            data = json.loads(_strip_json(raw))
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON: {exc}"

        try:
            resp = TopologyResponse.model_validate(data)
        except (ValidationError, ValueError) as exc:
            return None, f"Schema validation error: {exc}"

        if not resp.edges:
            return None, "No edges provided"

        edge_err = AutoGraphBuilder._validate_topology_edges(resp.edges, valid_agent_ids)
        if edge_err:
            return None, edge_err

        if AutoGraphBuilder._topology_has_cycle(resp.edges, valid_agent_ids):
            return None, "Topology contains cycles — must be a DAG"

        if resp.start_node and resp.start_node not in valid_agent_ids:
            return None, f"start_node '{resp.start_node}' is not a valid agent ID. Valid IDs: {valid_agent_ids}"
        if resp.end_node and resp.end_node not in valid_agent_ids:
            return None, f"end_node '{resp.end_node}' is not a valid agent ID. Valid IDs: {valid_agent_ids}"

        return resp, None

    @staticmethod
    def _parse_agents(
        raw: str,
        allowed_tools: set[str] | None = None,
    ) -> tuple[AgentsResponse | None, str | None]:
        """Parse and validate an LLM agents response."""
        try:
            data = json.loads(_strip_json(raw))
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON: {exc}"

        try:
            resp = AgentsResponse.model_validate(data)
        except (ValidationError, ValueError) as exc:
            return None, f"Schema validation error: {exc}"

        if len(resp.agents) < _MIN_AGENTS:
            return None, "At least 2 agents are required"

        ids = [a.agent_id for a in resp.agents]
        if len(ids) != len(set(ids)):
            return None, f"Duplicate agent IDs: {ids}"

        if allowed_tools is not None:
            bad_tools: set[str] = set()
            for agent in resp.agents:
                for tool in agent.tools:
                    if tool not in allowed_tools:
                        bad_tools.add(tool)
            if bad_tools:
                return None, (f"Unknown tools: {bad_tools}. Allowed tools: {allowed_tools}")

        return resp, None

    # ------------------------------------------------------------------
    # Agent conversion
    # ------------------------------------------------------------------

    def _specs_to_profiles(self, specs: list[AgentSpec]) -> list[Any]:
        """Convert LLM-generated AgentSpec list to AgentProfile objects."""
        from gmas.core.agent import AgentLLMConfig, AgentProfile

        profiles: list[Any] = []
        for spec in specs:
            llm_config = None
            if self.config.default_llm_backbone:
                kwargs: dict[str, Any] = {"model_name": self.config.default_llm_backbone}
                if self.config.default_temperature is not None:
                    kwargs["temperature"] = self.config.default_temperature
                llm_config = AgentLLMConfig(**kwargs)

            profile = AgentProfile(
                agent_id=spec.agent_id,
                display_name=spec.agent_id.replace("_", " ").title(),
                persona=spec.persona,
                description=spec.description,
                tools=spec.tools,
                llm_backbone=self.config.default_llm_backbone,
                llm_config=llm_config,
            )
            profiles.append(profile)

        return profiles

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _build_agents_system_prompt(self, tools_dict: dict[str, str]) -> str:
        """Build the system prompt for agent generation."""
        tools_section = _build_tools_section(tools_dict)

        return _AGENTS_SYSTEM.format(
            max_agents=self.config.max_agents,
            tools_section=tools_section,
        )
