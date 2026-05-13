"""
Minimal single-agent observer built on the framework computer_use tool.

Run:
    python -m examples.computer_use_observer
"""

import os
from pathlib import Path

from gmas.builder import build_property_graph
from gmas.core.agent import AgentProfile
from gmas.execution import MACPRunner, RunnerConfig
from gmas.tools import ComputerUseTool, ToolRegistry, create_openai_caller
from gmas.utils import configure_console, load_dotenv_file


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    msg = f"Missing required environment variable: {name}"
    raise RuntimeError(msg)


def main() -> None:
    configure_console()
    load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")

    llm = create_openai_caller(
        api_key=_require_env("LLM_API_KEY"),
        base_url=_require_env("LLM_BASE_URL"),
        model=_require_env("LLM_MODEL"),
        temperature=0.0,
        max_tokens=700,
        tool_choice="auto",
        http_proxy=os.environ.get("LLM_HTTP_PROXY"),
    )

    agent = AgentProfile(
        agent_id="observer",
        display_name="Observer",
        persona="a desktop observer who only describes what is visible on the current screen",
        description=(
            "Use computer_use only for passive observation. "
            "Allowed operations: start, observe, close. "
            "Do not click, type, scroll, navigate, open apps, press hotkeys, or change anything on the computer. "
            "After observation, write a short factual description of what the user is doing right now."
        ),
        tools=["computer_use"],
    )

    graph = build_property_graph(
        [agent],
        workflow_edges=[],
        query=(
            "Look at the current screen and briefly describe what the user is doing right now. "
            "Do not change anything on the computer. Write the final answer in Russian. "
            "If it is unclear from the observation, say that."
        ),
        include_task_node=True,
    )

    with ComputerUseTool(runtime_name="windows_native") as computer_use:
        registry = ToolRegistry().register(computer_use)
        runner = MACPRunner(
            llm_caller=llm,  # ty:ignore[invalid-argument-type]
            config=RunnerConfig(tool_registry=registry, max_tool_iterations=3),
        )
        result = runner.run_round(graph)

    print(result.final_answer.strip())


if __name__ == "__main__":
    main()
