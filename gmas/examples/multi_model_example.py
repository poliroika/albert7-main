"""
Multi-model setup — two agents backed by different LLMs.

Agent 1 (Doctor)    — a stronger model that suggests immunity-boosting foods.
Agent 2 (Organiser) — a lighter model that picks the single best option.

Demonstrates:
  - Per-agent LLM configuration (different endpoints, models, temperatures)
  - LLMCallerFactory dispatching callers by agent
  - Sequential workflow: Doctor → Organiser

Configure your models via environment variables:
    DOCTOR_API_KEY / DOCTOR_BASE_URL / DOCTOR_MODEL
    ORGANIZER_API_KEY / ORGANIZER_BASE_URL / ORGANIZER_MODEL

Run:
    python -m examples.multi_model_example
"""

import os
import time
from pathlib import Path

from gmas.builder import GraphBuilder
from gmas.execution import LLMCallerFactory, MACPRunner, RunnerConfig
from gmas.utils import configure_console, load_dotenv_file

load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")


REQUEST_TIMEOUT_S = 15.0

# ── Model configurations ─────────────────────────────────────────────────────

DOCTOR_CONFIG = {
    "api_key": os.getenv("DOCTOR_API_KEY", os.getenv("LLM_API_KEY", "your-doctor-api-key")),
    "base_url": os.getenv("DOCTOR_BASE_URL", os.getenv("LLM_BASE_URL", "http://localhost:8000/v1")),
    "model_name": os.getenv("DOCTOR_MODEL", os.getenv("LLM_MODEL", "gpt-4o")),
}

ORGANIZER_CONFIG = {
    "api_key": os.getenv("ORGANIZER_API_KEY", os.getenv("LLM_API_KEY", "your-organizer-api-key")),
    "base_url": os.getenv("ORGANIZER_BASE_URL", os.getenv("LLM_BASE_URL", "http://localhost:8001/v1")),
    "model_name": os.getenv("ORGANIZER_MODEL", os.getenv("LLM_MODEL", "gpt-4o-mini")),
}


# ── Graph construction ────────────────────────────────────────────────────────


def _build_graph():
    builder = GraphBuilder()

    builder.add_agent(
        "doctor",
        display_name="Nutrition Doctor",
        persona="You are an experienced nutritional doctor.",
        description=("Suggest 3–5 foods that boost immunity, with a brief explanation of each benefit."),
        llm_backbone=DOCTOR_CONFIG["model_name"],
        base_url=DOCTOR_CONFIG["base_url"],
        api_key=DOCTOR_CONFIG["api_key"],
        temperature=0.7,
        max_tokens=1000,
        timeout=REQUEST_TIMEOUT_S,
    )

    builder.add_agent(
        "organizer",
        display_name="Organiser",
        persona="You are a practical organiser.",
        description=(
            "Choose THE SINGLE best option from those proposed. "
            "Answer briefly: 'Best choice: [food] — [1–2 sentence justification]'"
        ),
        llm_backbone=ORGANIZER_CONFIG["model_name"],
        base_url=ORGANIZER_CONFIG["base_url"],
        api_key=ORGANIZER_CONFIG["api_key"],
        temperature=0.1,
        max_tokens=200,
        timeout=REQUEST_TIMEOUT_S,
    )

    builder.add_workflow_edge("doctor", "organizer")
    builder.add_task(query="What food is best for boosting immunity?")
    builder.connect_task_to_agents()

    return builder.build()


def _log_step(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _configure_network_env() -> None:
    """Keep the example off inherited system proxies."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    configure_console()
    _configure_network_env()

    print(f"Configured request timeout: {REQUEST_TIMEOUT_S:.1f}s")
    print("Configured proxy env: disabled")
    graph = _build_graph()

    print("Agents and their models:")
    for agent in graph.agents:
        if hasattr(agent, "llm_config") and agent.llm_config:
            model = agent.llm_config.model_name or "?"
            print(f"  {agent.agent_id:<12} → {model}")

    factory = LLMCallerFactory.create_openai_factory()
    runner = MACPRunner(llm_factory=factory, config=RunnerConfig(timeout=REQUEST_TIMEOUT_S + 5.0))

    print("\nRunning consultation…")
    started = time.perf_counter()
    _log_step("run_round: doctor -> organizer")
    try:
        result = runner.run_round(graph, final_agent_id="organizer")
    except Exception as exc:
        _log_step(f"run_round failed after {time.perf_counter() - started:.2f}s: {type(exc).__name__}: {exc}")
        raise
    _log_step(f"run_round finished in {time.perf_counter() - started:.2f}s")

    print("\n" + "=" * 50)
    print("RESULTS")
    print("=" * 50)

    if "doctor" in result.messages:
        print("\nDoctor's recommendations:")
        print(result.messages["doctor"])

    if "organizer" in result.messages:
        print("\nOrganiser's pick:")
        print(result.messages["organizer"])

    print(f"\nTotal tokens : {result.total_tokens}")
    print(f"Total time   : {result.total_time:.2f}s")
    print(f"Final answer : {result.final_answer}")


if __name__ == "__main__":
    main()
