"""
AutoGraphBuilder -- LLM-powered automatic graph assembly.

Demonstrates two modes with a REAL LLM (no mocks):
  1. assemble_topology -- agents are given, LLM designs the workflow
     (can be chain, diamond, fan-out/fan-in, etc.)
  2. assemble_full -- LLM designs agents (with tools!) AND topology
     from scratch.

Configuration is loaded from .env file at project root.
Expected keys: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

Run:
    python -m examples.auto_builder_example
"""

import os
import sys
import time
from pathlib import Path

from gmas.builder import AutoBuilderConfig, AutoGraphBuilder
from gmas.core.agent import AgentProfile
from gmas.execution import create_openai_structured_caller
from gmas.utils import load_dotenv_file

load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")

# -- Config ----------------------------------------------------------------

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


def _check_api_key() -> None:
    if not LLM_API_KEY:
        print(
            "ERROR: LLM_API_KEY is not set.\n"
            f"  Looked for .env at: {Path(__file__).resolve().parents[1] / '.env'}\n"
            "  Add LLM_API_KEY=... to .env or set it as an env variable.",
            file=sys.stderr,
        )
        sys.exit(1)


def _configure_network_env() -> None:
    """Keep the example off inherited system proxies."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def _make_caller():
    """Create a real structured LLM caller."""
    return create_openai_structured_caller(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
        temperature=0.4,
        max_tokens=2000,
    )


# -- Helpers ---------------------------------------------------------------


def _header(title: str) -> None:
    print(f"\n{'-' * 60}\n  {title}\n{'-' * 60}")


def _log_step(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _print_graph(graph) -> None:
    """Print a compact summary of the assembled graph."""
    agent_ids = [a.agent_id for a in graph.agents if not getattr(a, "query", None) and a.agent_id != "__task__"]
    print(f"  Agents     : {agent_ids}")
    print(f"  Num nodes  : {graph.num_nodes}")
    print(f"  Num edges  : {graph.num_edges}")
    print(f"  Start node : {graph.start_node}")
    print(f"  End node   : {graph.end_node}")
    print(f"  Query      : {graph.query!r}")

    if hasattr(graph, "A_com") and graph.A_com.numel() > 0:
        n = graph.A_com.shape[0]
        edges_found = []
        id_list = graph.node_ids
        for i in range(n):
            for j in range(n):
                if graph.A_com[i, j].item() > 0:
                    src = id_list[i] if i < len(id_list) else str(i)
                    tgt = id_list[j] if j < len(id_list) else str(j)
                    if src != "__task__" and tgt != "__task__":
                        edges_found.append(f"{src} -> {tgt}")
        if edges_found:
            print(f"  Workflow   : {', '.join(edges_found)}")

    for a in graph.agents:
        if a.agent_id == "__task__":
            continue
        tools = []
        if hasattr(a, "get_tool_names"):
            tools = a.get_tool_names()
        elif hasattr(a, "tools") and a.tools:
            tools = list(a.tools)
        if tools:
            print(f"    {a.agent_id} tools: {tools}")


# -- Mode 1: Topology from existing agents ---------------------------------


def example_assemble_topology(caller):
    """
    Given pre-built agents (some with tools), the LLM proposes the optimal
    workflow topology -- can be a chain, diamond, fan-out/fan-in, etc.
    """
    agents = [
        AgentProfile(
            agent_id="planner",
            display_name="Task Planner",
            persona="a strategic task planner",
            description="Breaks down the task into sub-tasks for other agents",
        ),
        AgentProfile(
            agent_id="web_researcher",
            display_name="Web Researcher",
            persona="an expert web researcher",
            description="Searches the web for up-to-date information",
            tools=["web_search"],
        ),
        AgentProfile(
            agent_id="data_analyst",
            display_name="Data Analyst",
            persona="a quantitative data analyst",
            description="Analyzes numerical data, computes statistics, creates charts",
            tools=["code_interpreter"],
        ),
        AgentProfile(
            agent_id="synthesizer",
            display_name="Report Synthesizer",
            persona="a report synthesizer",
            description="Merges research and analysis into a coherent report",
        ),
        AgentProfile(
            agent_id="reviewer",
            display_name="Quality Reviewer",
            persona="a quality reviewer",
            description="Reviews the final report for accuracy and completeness",
        ),
    ]

    _log_step("assemble_topology: building AutoGraphBuilder")
    auto = AutoGraphBuilder(llm_caller=caller)
    _log_step("assemble_topology: requesting topology")
    graph = auto.assemble_topology(
        agents=agents,
        query="Research and report on the current state of quantum computing",
    )
    _log_step("assemble_topology: topology received")

    _print_graph(graph)

    errors = graph.verify_integrity(raise_on_error=False)
    if errors:
        msg = f"Integrity errors: {errors}"
        raise ValueError(msg)
    print("  Integrity  : PASSED")

    return graph


# -- Mode 2: Full assembly from scratch ------------------------------------


def example_assemble_full(caller):
    """
    LLM designs agents (with appropriate tools) AND the topology from scratch.
    """
    config = AutoBuilderConfig(
        max_agents=6,
        available_tools=["web_search", "code_interpreter", "shell"],
        default_llm_backbone=LLM_MODEL,
        default_temperature=0.7,
    )

    _log_step("assemble_full: building AutoGraphBuilder")
    auto = AutoGraphBuilder(llm_caller=caller, config=config)
    _log_step("assemble_full: requesting agent specs and topology")
    graph = auto.assemble_full(
        query="Build a Python script that fetches current weather data from a public API and produces a summary report",
    )
    _log_step("assemble_full: graph design received")

    _print_graph(graph)

    errors = graph.verify_integrity(raise_on_error=False)
    if errors:
        msg = f"Integrity errors: {errors}"
        raise ValueError(msg)
    print("  Integrity  : PASSED")

    # Check at least some agents got tools
    agents_with_tools = [a for a in graph.agents if a.agent_id != "__task__" and getattr(a, "tools", None)]
    print(f"  Agents with tools: {len(agents_with_tools)}")

    return graph


# -- Entry point -----------------------------------------------------------


def main():
    _check_api_key()
    _configure_network_env()

    print(f"  LLM model  : {LLM_MODEL}")
    print(f"  LLM base   : {LLM_BASE_URL}")
    print(f"  API key    : {LLM_API_KEY[:8]}...{LLM_API_KEY[-4:]}")
    print("  Proxy env  : disabled")

    caller = _make_caller()

    examples = [
        ("Mode 1: Topology from existing agents (real LLM)", example_assemble_topology),
        ("Mode 2: Full assembly from scratch (real LLM)", example_assemble_full),
    ]

    for title, fn in examples:
        _header(title)
        started = time.perf_counter()
        _log_step(f"starting: {title}")
        try:
            fn(caller)
        except Exception as exc:
            _log_step(f"failed after {time.perf_counter() - started:.2f}s: {type(exc).__name__}: {exc}")
            raise
        _log_step(f"finished in {time.perf_counter() - started:.2f}s")

    print(f"\n{'-' * 60}")
    print("  All AutoGraphBuilder examples completed successfully!")
    print(f"{'-' * 60}\n")


if __name__ == "__main__":
    main()
