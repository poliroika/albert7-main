r"""
Benchmark: gMAS vs LangGraph — Single-Agent with Web Search Tool.

Compares a single-agent topology where the agent is equipped with a web
search tool.  Two tool configurations are tested:

    1. **simple**      — basic web search (DuckDuckGo, no deep content fetch)
    2. **deep_search**  — web search + Playwright page fetching

Datasets:
    • GAIA  (gaia-benchmark/GAIA, validation split — level 1)
    • SWE-bench Pro (ScaleAI/SWE-bench_Pro, test split)

Both frameworks receive identical prompts, models, and tool configs so
the comparison is apples-to-apples.

Usage::

    python benchmarks/benchmark_web_tool.py \\
        --datasets gaia,swe-bench-pro \\
        --tool-configs simple,deep_search \\
        --max-samples 20 \\
        --runs 1

Environment variables (or ``.env`` at repo root / ``benchmarks/`` dir):

    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
"""

import argparse
import asyncio
import concurrent.futures
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from unicodedata import normalize

try:
    from datasets import load_dataset
except ImportError:
    print("ERROR: datasets library not installed. Install with: pip install datasets", file=sys.stderr)
    sys.exit(1)

try:
    from huggingface_hub import snapshot_download

    HF_HUB_AVAILABLE = True
except ImportError:
    HF_HUB_AVAILABLE = False

try:
    from tqdm import tqdm

    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False
    print("WARNING: tqdm not installed. Progress bars disabled.", file=sys.stderr)

from gmas.builder import GraphBuilder
from gmas.execution import MACPRunner, RunnerConfig, StreamEventType
from gmas.tools import (
    ToolRegistry,
    WebSearchTool,
    create_openai_caller,
    register_tool,
)
from gmas.tools.llm_integration import LLMResponse

try:
    from typing import TypedDict

    from langgraph.graph import StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class BenchmarkConfig:
    """All tuneable parameters for the web-tool benchmark."""

    max_parallel: int = 10
    framework_timeout: float = 900.0
    runner_timeout: float = 180.0

    temperature: float = 0.0
    default_max_tokens: int = 4000

    ping_retries: int = 6
    ping_delay: float = 5.0
    ping_delay_max: float = 120.0
    skip_ping: bool = False

    num_runs: int = 1
    resume: bool = True
    log_dir: Path = Path("benchmark_logs")
    checkpoint_name: str = "checkpoint_web_tool.json"
    model_alias: str = "remote"

    env_api_key: str = "LLM_API_KEY"
    env_base_url: str = "LLM_BASE_URL"
    env_model: str = "LLM_MODEL"
    env_defaults: dict[str, str] = field(
        default_factory=lambda: {
            "LLM_API_KEY": "your_key",
            "LLM_BASE_URL": "your_url",
            "LLM_MODEL": "your_model",
        },
    )

    datasets_filter: list[str] | None = None
    max_samples: int | None = None
    tool_configs_filter: list[str] | None = None


_DEFAULT_CFG = BenchmarkConfig()

TOOL_CONFIGS: dict[str, dict[str, Any]] = {
    "simple": {
        "max_results": 10,
        "max_content_length": 12000,
        "fetch_content": True,
        "max_fetch_pages": 10,
        "timeout": 15,
        "cache": True,
        "cache_ttl": 3600.0,
        "cache_max_entries": 2048,
    },
    "deep_search": {
        "max_results": 10,
        "max_content_length": 12000,
        "fetch_content": True,
        "max_fetch_pages": 5,
        "timeout": 25,
        "cache": True,
        "cache_ttl": 3600.0,
        "cache_max_entries": 2048,
        "deep_search": "playwright",
        "browser_config": {
            "headless": True,
            "browser": "chromium",
            "scroll_to_bottom": True,
            "max_scrolls": 3,
            "scroll_pause": 0.3,
            "disable_images": True,
        },
    },
}

_ALL_DATASETS = ["gaia", "swe-bench-pro"]
_ALL_TOOL_CONFIGS = list(TOOL_CONFIGS.keys())


# ═══════════════════════════════════════════════════════════════════════════
# Logging helpers
# ═══════════════════════════════════════════════════════════════════════════

_EXECUTORS: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
_LOG_FH: io.TextIOWrapper | None = None


def _log(msg: str = "", *, console: bool = False) -> None:
    if _LOG_FH is not None:
        _LOG_FH.write(msg + "\n")
        _LOG_FH.flush()
    if console:
        print(msg)


def _open_log(log_dir: Path) -> Path:
    global _LOG_FH
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"bench_web_tool_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.txt"
    _LOG_FH = log_path.open("w", encoding="utf-8")
    return log_path


def _close_log() -> None:
    global _LOG_FH
    if _LOG_FH is not None:
        _LOG_FH.close()
        _LOG_FH = None


def _header(title: str) -> None:
    bar = "-" * 72
    _log(f"\n{bar}\n  {title}\n{bar}")


def _truncate(text: str, max_len: int = 300) -> str:
    if not text:
        return "<empty>"
    cleaned = text.replace("\n", " | ").strip()
    return cleaned[:max_len] + "…" if len(cleaned) > max_len else cleaned


def _log_llm_call(
    framework: str,
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    response: str,
    tokens: int = 0,
    duration_ms: float = 0.0,
) -> None:
    _log(f"\n  ┌─ LLM Call [{framework}] agent={agent_name}")
    _log(f"  │  system: {_truncate(system_prompt, 200)}")
    _log(f"  │  user:   {_truncate(user_prompt, 300)}")
    _log(f"  │  tokens: {tokens}  time: {duration_ms:.0f}ms")
    _log(f"  │  response: {_truncate(response, 400)}")
    _log("  └─")


def _log_framework_result(
    framework: str,
    test_name: str,
    sample_idx: int,
    output: str,
    time_sec: float,
    tokens: int,
    calls: int,
) -> None:
    _log(f"\n  ▶ [{framework}] test={test_name} sample={sample_idx}")
    _log(f"    time={time_sec:.2f}s  tokens={tokens}  llm_calls={calls}")
    _log(f"    output: {_truncate(output, 500)}")


def _safe(text: str) -> str:
    return normalize("NFKC", text).encode("ascii", errors="replace").decode("ascii")


# ═══════════════════════════════════════════════════════════════════════════
# Environment / model spec
# ═══════════════════════════════════════════════════════════════════════════


def _load_local_env(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and not os.environ.get(key):
                os.environ[key] = value.strip().strip('"').strip("'")


def _validate_llm_config(cfg: BenchmarkConfig) -> tuple[str, str, str]:
    _load_local_env(Path(__file__).resolve().parents[1] / ".env")
    _load_local_env(Path(__file__).resolve().parent / ".env")
    api_key = os.getenv(cfg.env_api_key, cfg.env_defaults.get(cfg.env_api_key, ""))
    base_url = os.getenv(cfg.env_base_url, cfg.env_defaults.get(cfg.env_base_url, ""))
    model = os.getenv(cfg.env_model, cfg.env_defaults.get(cfg.env_model, ""))
    return api_key, base_url, model


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    model: str
    base_url: str
    api_key: str
    max_parallel: int = 10


# ═══════════════════════════════════════════════════════════════════════════
# LLM caller creation (uses the framework's own ``create_openai_caller``)
# ═══════════════════════════════════════════════════════════════════════════


def _make_openai_caller(spec: ModelSpec, cfg: BenchmarkConfig):
    """
    Create an ``OpenAICaller`` — the recommended gMAS way.

    Returns an ``OpenAICaller`` that natively supports:
      - ``caller(prompt)``             → ``str``
      - ``caller(prompt, tools=[…])``  → ``LLMResponse``
    """
    return create_openai_caller(
        api_key=spec.api_key,
        base_url=spec.base_url,
        model=spec.model,
        temperature=cfg.temperature,
        max_tokens=cfg.default_max_tokens,
        tool_choice="auto",
    )


class _InstrumentedClient:
    """Wraps an OpenAI ``client`` so we can intercept ``chat.completions.create``."""

    def __init__(self, real_client: Any, tracker: "TrackedCaller") -> None:
        self._real = real_client
        self._tracker = tracker
        self.chat = self._ChatNS(real_client.chat, tracker)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    class _ChatNS:
        def __init__(self, real_chat: Any, tracker: "TrackedCaller") -> None:
            self._real = real_chat
            self._tracker = tracker
            self.completions = self._CompletionsNS(real_chat.completions, tracker)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        class _CompletionsNS:
            def __init__(self, real_comp: Any, tracker: "TrackedCaller") -> None:
                self._real = real_comp
                self._tracker = tracker

            def create(self, **kwargs: Any) -> Any:
                mt = kwargs.get("max_tokens")
                if mt is not None and mt < 1:
                    kwargs["max_tokens"] = 1024
                response = self._real.create(**kwargs)
                self._tracker.call_count += 1
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self._tracker.total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
                return response

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)


class TrackedCaller:
    """
    Proxy around ``OpenAICaller`` that intercepts every API call to track tokens.

    Works by replacing the inner ``OpenAI`` client with ``_InstrumentedClient``
    that hooks into ``client.chat.completions.create``.  This means every
    call — with or without tools, from MACPRunner or from ``_run_tool_loop`` —
    is counted.
    """

    def __init__(self, spec: ModelSpec, cfg: BenchmarkConfig, framework: str = "unknown") -> None:
        self._spec = spec
        self._cfg = cfg
        self._framework = framework
        self._inner = _make_openai_caller(spec, cfg)
        self.total_tokens: int = 0
        self.call_count: int = 0
        self._inner.client = _InstrumentedClient(self._inner.client, self)

    def reset(self) -> None:
        self.total_tokens = 0
        self.call_count = 0

    def __call__(
        self,
        prompt: str | list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str | LLMResponse:
        """Proxy call — same signature as ``OpenAICaller.__call__``."""
        if tools:
            return self._inner(prompt, tools=tools)
        return self._inner(prompt)

    @property
    def supports_structured(self) -> bool:
        return getattr(self._inner, "supports_structured", False)

    @property
    def caller(self):
        """Return *self* so ``MACPRunner`` uses the proxy, not the raw caller."""
        return self

    def chat(self, system: str, user: str, _max_tokens: int = 0, _agent_name: str = "unknown") -> str:
        """Simple system+user chat for ping."""
        msgs: list[dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        result = self(msgs)
        return result if isinstance(result, str) else (result.content or "")


# ═══════════════════════════════════════════════════════════════════════════
# Web search tool — builds ``WebSearchTool`` exactly as in the notebook
# ═══════════════════════════════════════════════════════════════════════════


def _make_web_search_tool(tool_config_name: str) -> WebSearchTool:
    """
    Create a ``WebSearchTool`` from the named config.

    Returns a ready-to-use tool instance.  For ``deep_search`` configs
    it also calls ``warm_up()`` to pre-initialise the browser.
    """
    kwargs = dict(TOOL_CONFIGS[tool_config_name])
    tool = WebSearchTool(**kwargs)
    if kwargs.get("deep_search"):
        tool.warm_up()
    return tool


def _make_tool_registry(tool_config_name: str) -> tuple[WebSearchTool, ToolRegistry | None]:
    """
    Build a per-config tool + optional private ``ToolRegistry``.

    * **simple** → registers the tool in the global registry (``register_tool``),
      returns ``registry=None`` (the runner uses the global one).
    * **deep_search** → creates a private ``ToolRegistry`` so that
      the deep-search tool doesn't pollute the global registry.
    """
    tool = _make_web_search_tool(tool_config_name)
    if TOOL_CONFIGS[tool_config_name].get("deep_search"):
        registry = ToolRegistry()
        registry.register(tool)
        return tool, registry
    register_tool(tool)
    return tool, None


def _execute_web_search(tool: WebSearchTool, arguments: dict[str, Any] | str) -> str:
    """
    Execute a web search tool synchronously.

    ``WebSearchTool.execute()`` is a **synchronous** method that returns
    a ``ToolResult`` directly — no asyncio is needed.
    """
    result = tool.execute(**arguments) if isinstance(arguments, dict) else tool.execute(query=arguments)
    if result and result.success:
        return result.output or "No results found."
    error = getattr(result, "error", None)
    return f"Error: {error}" if error else "No results found."


# ═══════════════════════════════════════════════════════════════════════════
# System prompts
# ═══════════════════════════════════════════════════════════════════════════

_AGENT_SYSTEM_PROMPT = (
    "You are a research assistant with access to a web search tool. "
    "Use the web search tool when you need current information, facts, "
    "documentation, or any knowledge that may not be in your training data. "
    "Provide accurate, well-sourced answers. "
    "Think step by step, search when needed, then provide your final answer."
)

_AGENT_DESCRIPTION = (
    "Research assistant that uses web search to find accurate, up-to-date information and answer questions."
)


# ═══════════════════════════════════════════════════════════════════════════
# gMAS single-agent runner
# ═══════════════════════════════════════════════════════════════════════════


def _extract_event_content(event: Any) -> str:
    content = getattr(event, "content", "")
    if isinstance(content, str):
        return content
    if hasattr(content, "text"):
        return content.text
    return str(content) if content else ""


def _run_gmas_single(
    tracked: TrackedCaller,
    problem: str,
    tool_config_name: str,
) -> dict[str, Any]:
    """
    Run a single-agent gMAS graph with web search tool.

    Uses the same pattern as ``playground_web_gmas.ipynb``:
    ``register_tool`` / ``ToolRegistry`` + ``llm_caller`` + ``tools=["web_search"]``.
    """
    cfg = tracked._cfg
    _tool, registry = _make_tool_registry(tool_config_name)
    tracked.reset()
    t0 = time.perf_counter()

    builder = GraphBuilder()
    builder.add_agent(
        "researcher",
        persona=_AGENT_SYSTEM_PROMPT,
        description=_AGENT_DESCRIPTION,
        tools=["web_search"],
    )
    builder.add_task(query=problem, description="Research task")
    builder.connect_task_to_agents(agent_ids=["researcher"])
    graph = builder.build()

    _log(f"\n  ── gMAS graph built: 1 agent, tool_config={tool_config_name}")

    runner_cfg = RunnerConfig(
        max_tool_iterations=_MAX_TOOL_ITERATIONS,
        timeout=int(cfg.runner_timeout),
    )
    if registry is not None:
        runner_cfg.tool_registry = registry

    runner = MACPRunner(
        llm_caller=tracked.caller,
        config=runner_cfg,
    )

    output = ""
    all_agent_outputs: list[str] = []
    for event in runner.stream(graph):
        etype = getattr(event, "event_type", None)
        if etype == StreamEventType.AGENT_OUTPUT:
            content = _extract_event_content(event)
            if content:
                all_agent_outputs.append(content)
                output = content
                _log(f"    [gMAS] AGENT_OUTPUT: {_truncate(output, 400)}")
        elif etype == StreamEventType.RUN_END:
            fa = getattr(event, "final_answer", "") or ""
            if fa:
                output = fa
            _log(
                f"    [gMAS] RUN_END heuristic_tokens={getattr(event, 'total_tokens', 0)} "
                f"api_tokens={tracked.total_tokens} "
                f"time={getattr(event, 'total_time', 0):.1f}s "
                f"final_answer_len={len(fa)}"
            )

    if not output and all_agent_outputs:
        output = all_agent_outputs[-1]

    elapsed = time.perf_counter() - t0
    _log(f"  ── gMAS finished: time={elapsed:.2f}s  api_tokens={tracked.total_tokens} output_len={len(output)}")

    return {
        "framework": "gMAS",
        "time": elapsed,
        "tokens": tracked.total_tokens,
        "calls": tracked.call_count,
        "output": output,
    }


# ═══════════════════════════════════════════════════════════════════════════
# LangGraph single-agent runner
# ═══════════════════════════════════════════════════════════════════════════


_MAX_TOOL_ITERATIONS = 3  # same as RunnerConfig.max_tool_iterations default


def _run_tool_loop(
    caller,
    web_tool: WebSearchTool,
    tool_schemas: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    framework: str,
    max_iterations: int = _MAX_TOOL_ITERATIONS,
) -> str:
    """
    OpenAI function-calling loop for the LangGraph node.

    Mirrors ``MACPRunner._run_agent_with_tools`` exactly so that both
    frameworks go through the **same** LLM → tool → LLM cycle:

    * Every iteration calls LLM **with** tool schemas (same as MACPRunner).
    * On the last iteration, if the LLM returns content alongside
      tool_calls — return the content (same as MACPRunner).
    * On the last iteration tool results are still executed and appended
      to history, then we break.
    * After the loop, if ``last_content`` is empty — one more LLM call
      **without** tools is made to force a textual answer (same as
      MACPRunner's post-loop "force final answer" logic).
    """
    tool_cache: dict[str, str] = {}
    llm_response: str | LLMResponse = ""

    for iteration in range(max_iterations):
        is_last = iteration == max_iterations - 1

        llm_response = caller(messages, tools=tool_schemas)

        if isinstance(llm_response, str):
            return llm_response

        if not llm_response.has_tool_calls:
            return llm_response.content or ""

        if is_last and llm_response.content:
            return llm_response.content

        tool_results: list[str] = []
        all_cached = True
        for tc in llm_response.tool_calls:
            cache_key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
            if cache_key in tool_cache:
                output = tool_cache[cache_key]
            else:
                all_cached = False
                _log(f"  [{framework}-TOOL] {tc.name}({tc.arguments})")
                try:
                    output = _execute_web_search(web_tool, tc.arguments)
                except Exception as exc:
                    output = f"Error: {exc}"
                tool_cache[cache_key] = output
                _log(f"  [{framework}-TOOL] result: {_truncate(output, 300)}")
            tool_results.append(f"[{tc.name}]: {output}")

        if all_cached and llm_response.tool_calls:
            _log(f"  [{framework}] All tool calls cached — breaking loop")
            break

        _append_tool_exchange(messages, llm_response, tool_results, tool_cache)

        if is_last:
            break

    last_content = llm_response.content if hasattr(llm_response, "content") else str(llm_response)
    if last_content:
        return last_content

    _log(f"  [{framework}] Forcing final answer call without tools")
    messages.append(
        {
            "role": "user",
            "content": (
                "You have used all available tool iterations. "
                "Based on ALL the information you have gathered from the tool results above, "
                "provide your final answer now. Summarize your findings concisely. "
                "Do NOT call any tools. You MUST provide a text answer."
            ),
        }
    )
    final = caller(messages, tools=None)
    result = final if isinstance(final, str) else (final.content or "")
    if result:
        return result

    _log(f"  [{framework}] Force final answer returned empty — extracting from history")
    return _extract_best_content_from_history(messages)


def _extract_best_content_from_history(messages: list[dict[str, Any]]) -> str:
    """Last-resort: scan conversation history for the best available content."""
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "assistant" and content and not content.startswith("[Error"):
            return content
    for msg in reversed(messages):
        if msg.get("role") == "tool" and msg.get("content"):
            return msg["content"][:2000]
    return ""


def _append_tool_exchange(
    messages: list[dict[str, Any]],
    llm_response: LLMResponse,
    tool_results: list[str],
    tool_cache: dict[str, str],
) -> None:
    """Append assistant + tool messages to the conversation history."""
    if hasattr(llm_response, "raw_response") and llm_response.raw_response is not None:
        try:
            raw_msg = llm_response.raw_response.choices[0].message
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": raw_msg.content,
            }
            if hasattr(raw_msg, "tool_calls") and raw_msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": rtc.id,
                        "type": "function",
                        "function": {
                            "name": rtc.function.name,
                            "arguments": rtc.function.arguments,
                        },
                    }
                    for rtc in raw_msg.tool_calls
                ]
            messages.append(assistant_msg)
            for tc in llm_response.tool_calls:
                cache_key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_cache.get(cache_key, ""),
                    }
                )
        except (AttributeError, IndexError, TypeError, KeyError):
            pass
        else:
            return

    messages.append(
        {
            "role": "assistant",
            "content": llm_response.content or "",
        }
    )
    messages.append(
        {
            "role": "user",
            "content": "Tool results:\n" + "\n".join(tool_results),
        }
    )


def _run_langgraph_single(
    tracked: TrackedCaller,
    problem: str,
    tool_config_name: str,
) -> dict[str, Any]:
    """
    Run a single-agent LangGraph graph with web search tool.

    Uses the same ``OpenAICaller`` (via ``create_openai_caller``) and
    the same ``WebSearchTool`` as the gMAS side.  The only difference
    is orchestration: LangGraph ``StateGraph`` + ``_run_tool_loop``
    vs gMAS ``MACPRunner``.
    """
    if not LANGGRAPH_AVAILABLE:
        return {"framework": "langgraph", "error": "langgraph not installed"}

    class State(TypedDict):
        input: str
        output: str

    web_tool = _make_web_search_tool(tool_config_name)
    tool_schemas = [web_tool.to_openai_schema()]

    def researcher_node(state: State) -> dict[str, str]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": state["input"]},
        ]
        output = _run_tool_loop(
            tracked,
            web_tool,
            tool_schemas,
            messages,
            framework="LangGraph",
            max_iterations=_MAX_TOOL_ITERATIONS,
        )
        return {"output": output}

    tracked.reset()
    t0 = time.perf_counter()
    g = StateGraph(State)
    g.add_node("researcher", researcher_node)
    g.set_entry_point("researcher")
    g.set_finish_point("researcher")
    result = g.compile().invoke({"input": problem, "output": ""})
    elapsed = time.perf_counter() - t0
    output = result["output"]

    _log_framework_result(
        "LangGraph",
        "single_agent",
        -1,
        output,
        elapsed,
        tracked.total_tokens,
        tracked.call_count,
    )

    return {
        "framework": "langgraph",
        "time": elapsed,
        "tokens": tracked.total_tokens,
        "calls": tracked.call_count,
        "output": output,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Evaluation helpers
# ═══════════════════════════════════════════════════════════════════════════


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _extract_last_number(text: str) -> str | None:
    m = re.findall(r"-?\d+(?:\.\d+)?(?:/\d+)?", text.replace(",", ""))
    return m[-1] if m else None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else text.strip()


def _numbers_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Fraction(a.replace(",", "")) == Fraction(b.replace(",", ""))
    except Exception:
        return _normalize_text(a) == _normalize_text(b)


def _evaluate_output(sample: dict[str, Any], output: str) -> dict[str, Any]:
    """Evaluate model output against the sample reference."""
    metric = sample.get("metric_name")
    reference = sample.get("reference")

    if metric is None or reference is None:
        return {
            "reference_available": False,
            "metric_name": metric,
            "quality_score": None,
            "is_correct": None,
            "prediction": None,
            "reference": reference,
        }

    prediction: str | None = None
    is_correct: bool | None = None
    idx = sample.get("index", "?")

    if metric == "exact_text":
        prediction = _last_line(output)
        ref_n = _normalize_text(str(reference))
        is_correct = _normalize_text(prediction) == ref_n or ref_n in _normalize_text(output)
        _log(f"  [EVAL] exact_text sample={idx} correct={is_correct}")

    elif metric == "contains":
        prediction = output
        ref_n = _normalize_text(str(reference))
        is_correct = ref_n in _normalize_text(output)
        _log(f"  [EVAL] contains sample={idx} correct={is_correct}")

    elif metric == "numeric":
        prediction = _extract_last_number(output)
        is_correct = _numbers_equal(prediction, str(reference))
        _log(f"  [EVAL] numeric sample={idx} predicted={prediction} expected={reference} correct={is_correct}")

    else:
        prediction = _last_line(output)
        ref_n = _normalize_text(str(reference))
        is_correct = _normalize_text(prediction) == ref_n or ref_n in _normalize_text(output)
        _log(f"  [EVAL] {metric} sample={idx} correct={is_correct}")

    return {
        "reference_available": True,
        "metric_name": metric,
        "quality_score": 1.0 if is_correct else 0.0,
        "is_correct": bool(is_correct),
        "prediction": prediction,
        "reference": reference,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Dataset loading
# ═══════════════════════════════════════════════════════════════════════════


def _load_gaia_samples() -> list[dict[str, Any]]:
    """Load GAIA benchmark validation split (level 1) from HuggingFace."""
    _log("\n  Loading GAIA dataset (validation, level 1)...")

    try:
        if HF_HUB_AVAILABLE:
            data_dir = snapshot_download(repo_id="gaia-benchmark/GAIA", repo_type="dataset")
            dataset = load_dataset(data_dir, "2023_level1", split="validation")
        else:
            dataset = load_dataset("gaia-benchmark/GAIA", "2023_level1", split="validation")
    except Exception as exc:
        _log(f"  [WARN] Failed to load GAIA with level1 config, trying default: {exc}")
        try:
            dataset = load_dataset("gaia-benchmark/GAIA", split="validation")
        except Exception as exc2:
            _log(f"  [ERROR] Cannot load GAIA dataset: {exc2}")
            print(f"  [ERROR] Cannot load GAIA dataset: {exc2}", file=sys.stderr)
            return []

    samples: list[dict[str, Any]] = []
    for i, item in enumerate(dataset):
        question = item.get("Question", item.get("question", ""))
        answer = item.get("Final answer", item.get("final_answer", item.get("answer", "")))
        level = item.get("Level", item.get("level", 1))

        if not question:
            continue

        samples.append(
            {
                "problem": question,
                "dataset": "gaia",
                "index": i,
                "reference": str(answer).strip() if answer else None,
                "metric_name": "contains" if answer else None,
                "level": level,
                "task_id": item.get("task_id", f"gaia_{i}"),
            }
        )

    _log(f"  ✓ Loaded {len(samples)} samples from GAIA")
    return samples


def _load_swe_bench_pro_samples() -> list[dict[str, Any]]:
    """Load SWE-bench Pro from HuggingFace."""
    _log("\n  Loading SWE-bench Pro dataset...")

    try:
        dataset = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    except Exception as exc:
        _log(f"  [ERROR] Cannot load SWE-bench Pro: {exc}")
        print(f"  [ERROR] Cannot load SWE-bench Pro: {exc}", file=sys.stderr)
        return []

    samples: list[dict[str, Any]] = []
    for i, item in enumerate(dataset):
        problem_statement = item.get("problem_statement", "")
        repo = item.get("repo", "")
        instance_id = item.get("instance_id", f"swe_{i}")

        if not problem_statement:
            continue

        query = (
            f"Repository: {repo}\n"
            f"Instance: {instance_id}\n\n"
            f"Problem:\n{problem_statement}\n\n"
            "Analyze this software engineering issue. Use web search to find relevant "
            "documentation, similar issues, or API references. Provide a detailed "
            "analysis with a proposed solution approach."
        )

        samples.append(
            {
                "problem": query,
                "dataset": "swe-bench-pro",
                "index": i,
                "reference": None,
                "metric_name": None,
                "repo": repo,
                "instance_id": instance_id,
            }
        )

    _log(f"  ✓ Loaded {len(samples)} samples from SWE-bench Pro")
    return samples


def _load_dataset_samples(dataset_name: str) -> list[dict[str, Any]]:
    """Load samples for the given dataset name."""
    loaders: dict[str, Any] = {
        "gaia": _load_gaia_samples,
        "swe-bench-pro": _load_swe_bench_pro_samples,
    }
    loader = loaders.get(dataset_name)
    if loader is None:
        msg = f"Unknown dataset: {dataset_name}. Available: {', '.join(loaders)}"
        raise ValueError(msg)
    return loader()


# ═══════════════════════════════════════════════════════════════════════════
# Test registry
# ═══════════════════════════════════════════════════════════════════════════


def _make_test_key(tool_config_name: str) -> str:
    return f"web_search[{tool_config_name}]"


def _build_test_fns(tool_config_name: str):
    """Return (langgraph_fn, gmas_fn) callables for a given tool config."""

    def lg_fn(tracked: TrackedCaller, problem: str) -> dict[str, Any]:
        return _run_langgraph_single(tracked, problem, tool_config_name)

    def gmas_fn(tracked: TrackedCaller, problem: str) -> dict[str, Any]:
        return _run_gmas_single(tracked, problem, tool_config_name)

    return lg_fn, gmas_fn


# ═══════════════════════════════════════════════════════════════════════════
# Checkpointing
# ═══════════════════════════════════════════════════════════════════════════


def _ck_key(alias: str, test_key: str, dataset: str, sample_idx: int, run_idx: int) -> str:
    return f"{alias}:run{run_idx}:{test_key}:{dataset}:{sample_idx}"


def _save_checkpoint(path: Path, results: list[dict[str, Any]], completed: dict[str, set[str]]) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "results": results,
                "completed_tests": {k: sorted(v) for k, v in completed.items()},
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def _load_checkpoint(path: Path) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    if not path.exists():
        return [], {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        completed = {k: set(v) for k, v in data.get("completed_tests", {}).items()}
        results = data.get("results", [])
        if not completed:
            for r in results:
                alias = r.get("model_alias")
                tk, ds, si = r.get("test"), r.get("dataset"), r.get("sample_idx")
                ri = int(r.get("run_idx", 1))
                if all(x is not None for x in [alias, tk, ds, si]):
                    completed.setdefault(tk, set()).add(_ck_key(alias, tk, ds, si, ri))
    except Exception as exc:
        _log(f"WARNING: failed to load checkpoint {path}: {exc}")
        return [], {}
    else:
        return results, completed


def _save_log_json(
    spec: ModelSpec,
    results: list[dict[str, Any]],
    datasets: list[str],
    log_dir: Path,
    checkpoint_path: Path | None,
) -> Path:
    log_dir.mkdir(exist_ok=True)
    out_path = log_dir / f"bench_web_tool_{spec.alias}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "model_alias": spec.alias,
                "model": spec.model,
                "base_url": spec.base_url,
                "datasets": datasets,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    if checkpoint_path and checkpoint_path.exists():
        checkpoint_path.unlink()
    return out_path


# ═══════════════════════════════════════════════════════════════════════════
# Parallel execution
# ═══════════════════════════════════════════════════════════════════════════


def _get_executor(spec: ModelSpec) -> concurrent.futures.ThreadPoolExecutor:
    ex = _EXECUTORS.get(spec.alias)
    if ex is None:
        ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, spec.max_parallel + 4),
            thread_name_prefix=f"bench-web-{spec.alias}",
        )
        _EXECUTORS[spec.alias] = ex
    return ex


def _make_llm(spec: ModelSpec, cfg: BenchmarkConfig, framework: str = "unknown") -> TrackedCaller:
    return TrackedCaller(spec, cfg, framework=framework)


def _build_error_result(
    framework: str,
    spec: ModelSpec,
    sample: dict[str, Any],
    test_key: str,
    dataset: str,
    run_idx: int,
    error: str,
) -> dict[str, Any]:
    _log(f"  [ERROR] {framework} test={test_key} sample={sample['index']} error={error}")
    return {
        "framework": framework,
        "test": test_key,
        "dataset": dataset,
        "sample_idx": sample["index"],
        "run_idx": run_idx,
        "problem": sample["problem"][:200],
        "model_alias": spec.alias,
        "model_name": spec.model,
        "base_url": spec.base_url,
        "error": error,
        "reference_available": False,
        "quality_score": None,
        "is_correct": None,
        "metric_name": sample.get("metric_name"),
        "prediction": None,
        "reference": sample.get("reference"),
    }


async def _run_framework_with_timeout(
    spec: ModelSpec,
    cfg: BenchmarkConfig,
    framework_name: str,
    fn,
    sample: dict[str, Any],
    test_key: str,
    dataset: str,
    run_idx: int,
    timeout_sec: float,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    executor = _get_executor(spec)

    def _invoke() -> dict[str, Any]:
        tracked = _make_llm(spec, cfg, framework=framework_name)
        _log(f"\n  ━━ Running {framework_name} | test={test_key} | sample={sample['index']}")
        result = fn(tracked, sample["problem"])
        result.update(
            {
                "test": test_key,
                "dataset": dataset,
                "sample_idx": sample["index"],
                "run_idx": run_idx,
                "problem": sample["problem"][:200],
                "model_alias": spec.alias,
                "model_name": spec.model,
                "base_url": spec.base_url,
            }
        )
        if result.get("error"):
            result.setdefault("reference_available", False)
            result.setdefault("quality_score", None)
            result.setdefault("is_correct", None)
            result.setdefault("metric_name", sample.get("metric_name"))
            return result
        output = result.get("output", "")
        _log_framework_result(
            framework_name,
            test_key,
            sample["index"],
            output,
            result.get("time", 0),
            result.get("tokens", 0),
            result.get("calls", 0),
        )
        result.update(_evaluate_output(sample, output))
        return result

    try:
        return await asyncio.wait_for(loop.run_in_executor(executor, _invoke), timeout=timeout_sec)
    except TimeoutError:
        return _build_error_result(
            framework_name,
            spec,
            sample,
            test_key,
            dataset,
            run_idx,
            f"timeout>{timeout_sec:.0f}s",
        )
    except Exception as exc:
        return _build_error_result(framework_name, spec, sample, test_key, dataset, run_idx, str(exc))


async def _run_single_async(
    spec: ModelSpec,
    cfg: BenchmarkConfig,
    test_key: str,
    lg_fn,
    gmas_fn,
    sample: dict[str, Any],
    dataset: str,
    run_idx: int,
    timeout_sec: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for name, fn in [("langgraph", lg_fn), ("gMAS", gmas_fn)]:
        results[name] = await _run_framework_with_timeout(
            spec,
            cfg,
            name,
            fn,
            sample,
            test_key,
            dataset,
            run_idx,
            timeout_sec,
        )
    return results["langgraph"], results["gMAS"]


async def _run_parallel(
    spec: ModelSpec,
    cfg: BenchmarkConfig,
    test_key: str,
    lg_fn,
    gmas_fn,
    dataset: str,
    problems: list[dict[str, Any]],
    run_idx: int,
    timeout_sec: float,
    completed: set[str],
    completed_tests: dict[str, set[str]],
    progress_bar: Any,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    sem = asyncio.Semaphore(spec.max_parallel)

    async def _one(sample: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        async with sem:
            ck = _ck_key(spec.alias, test_key, dataset, sample["index"], run_idx)
            if ck in completed:
                return None
            lg_r, gm_r = await _run_single_async(
                spec,
                cfg,
                test_key,
                lg_fn,
                gmas_fn,
                sample,
                dataset,
                run_idx,
                timeout_sec,
            )
            completed.add(ck)
            completed_tests.setdefault(test_key, set()).add(ck)
            _log(
                f"\n  [RESULT] sample={sample['index']} "
                f"| LG: {'✓' if lg_r.get('is_correct') else '✗'} "
                f"| gMAS: {'✓' if gm_r.get('is_correct') else '✗'}"
            )
            if progress_bar:
                progress_bar.update(1)
            return lg_r, gm_r

    remaining = [p for p in problems if _ck_key(spec.alias, test_key, dataset, p["index"], run_idx) not in completed]
    if not remaining:
        return []
    return [r for r in await asyncio.gather(*[_one(p) for p in remaining]) if r is not None]


# ═══════════════════════════════════════════════════════════════════════════
# Summary / reporting
# ═══════════════════════════════════════════════════════════════════════════


def _sample_std(scores: list[float]) -> float:
    n = len(scores)
    if n < 2:
        return 0.0
    mean = sum(scores) / n
    return math.sqrt(sum((v - mean) ** 2 for v in scores) / (n - 1))


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _align_pairs(results: list[dict[str, Any]]) -> list[tuple[tuple[int, int], dict[str, Any], dict[str, Any]]]:
    by_key: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    for r in results:
        if r.get("error"):
            continue
        key = (int(r.get("run_idx", 1)), int(r.get("sample_idx", -1)))
        by_key.setdefault(key, {})[r.get("framework", "")] = r

    pairs = [(k, v["langgraph"], v["gMAS"]) for k, v in by_key.items() if {"langgraph", "gMAS"}.issubset(v)]
    pairs.sort(key=lambda x: x[0])
    return pairs


def _build_summary_row(
    spec: ModelSpec,
    test_key: str,
    dataset: str,
    run_idx: int,
    pairs: list[tuple[tuple[int, int], dict[str, Any], dict[str, Any]]],
    all_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not pairs:
        return None

    def acc_stats(rows):
        scored = [float(r["quality_score"]) for r in rows if r.get("quality_score") is not None]
        if not scored:
            return None, 0, 0.0
        return _mean(scored), len(scored), _sample_std(scored)

    def _count_errors(fw: str) -> int:
        return sum(
            1
            for r in all_results
            if r.get("model_alias") == spec.alias
            and r.get("test") == test_key
            and r.get("dataset") == dataset
            and int(r.get("run_idx", 1)) == run_idx
            and r.get("framework") == fw
            and r.get("error")
        )

    lg = [t[1] for t in pairs]
    gm = [t[2] for t in pairs]
    lg_acc, lg_n, lg_acc_std = acc_stats(lg)
    gm_acc, gm_n, gm_acc_std = acc_stats(gm)

    return {
        "summary_key": f"{spec.alias}:{test_key}_{dataset}",
        "summary_key_run": f"{spec.alias}:run{run_idx}:{test_key}_{dataset}",
        "model_alias": spec.alias,
        "test": test_key,
        "dataset": dataset,
        "run_idx": run_idx,
        "paired_samples": len(pairs),
        "lg_time": _mean([float(r["time"]) for r in lg]),
        "gm_time": _mean([float(r["time"]) for r in gm]),
        "lg_tok": _mean([float(r["tokens"]) for r in lg]),
        "gm_tok": _mean([float(r["tokens"]) for r in gm]),
        "lg_calls": _mean([float(r["calls"]) for r in lg]),
        "gm_calls": _mean([float(r["calls"]) for r in gm]),
        "lg_acc": lg_acc,
        "gm_acc": gm_acc,
        "lg_acc_n": lg_n,
        "gm_acc_n": gm_n,
        "lg_acc_std": lg_acc_std,
        "gm_acc_std": gm_acc_std,
        "lg_errors": _count_errors("langgraph"),
        "gm_errors": _count_errors("gMAS"),
    }


def _aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate summary rows across multiple runs."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        k = row["summary_key"]
        b = grouped.setdefault(
            k,
            {
                "summary_key": k,
                "model_alias": row["model_alias"],
                "test": row["test"],
                "dataset": row["dataset"],
                "count": 0,
                "paired_samples": 0.0,
                "lg_time": 0.0,
                "gm_time": 0.0,
                "lg_tok": 0.0,
                "gm_tok": 0.0,
                "lg_calls": 0.0,
                "gm_calls": 0.0,
                "lg_acc_sum": 0.0,
                "gm_acc_sum": 0.0,
                "lg_acc_n": 0,
                "gm_acc_n": 0,
                "lg_acc_var_sum": 0.0,
                "gm_acc_var_sum": 0.0,
                "lg_errors": 0,
                "gm_errors": 0,
            },
        )
        b["count"] += 1
        for f in ("paired_samples", "lg_time", "gm_time", "lg_tok", "gm_tok", "lg_calls", "gm_calls"):
            b[f] += float(row[f])
        b["lg_errors"] += int(row.get("lg_errors", 0))
        b["gm_errors"] += int(row.get("gm_errors", 0))
        for prefix in ("lg", "gm"):
            ni = int(row.get(f"{prefix}_acc_n", 0))
            acc = row[f"{prefix}_acc"]
            if acc is not None and ni:
                b[f"{prefix}_acc_sum"] += float(acc) * ni
                b[f"{prefix}_acc_n"] += ni
                si = float(row.get(f"{prefix}_acc_std", 0.0))
                if ni > 1:
                    b[f"{prefix}_acc_var_sum"] += (ni - 1) * si * si

    agg = []
    for b in grouped.values():
        n = max(1, b["count"])

        def _pooled_std(prefix: str, b: dict = b) -> float:
            denom = b[f"{prefix}_acc_n"] - b["count"]
            return math.sqrt(b[f"{prefix}_acc_var_sum"] / denom) if denom > 0 else 0.0

        agg.append(
            {
                "summary_key": b["summary_key"],
                "model_alias": b["model_alias"],
                "test": b["test"],
                "dataset": b["dataset"],
                "paired_samples": b["paired_samples"] / n,
                "lg_time": b["lg_time"] / n,
                "gm_time": b["gm_time"] / n,
                "lg_tok": b["lg_tok"] / n,
                "gm_tok": b["gm_tok"] / n,
                "lg_calls": b["lg_calls"] / n,
                "gm_calls": b["gm_calls"] / n,
                "lg_acc": (b["lg_acc_sum"] / b["lg_acc_n"]) if b["lg_acc_n"] else None,
                "gm_acc": (b["gm_acc_sum"] / b["gm_acc_n"]) if b["gm_acc_n"] else None,
                "lg_acc_std": _pooled_std("lg"),
                "gm_acc_std": _pooled_std("gm"),
                "lg_errors": b["lg_errors"],
                "gm_errors": b["gm_errors"],
            }
        )

    agg.sort(key=lambda x: x["summary_key"])
    return agg


def _fmt_acc(v: float | None, std: float = 0.0) -> str:
    if v is None:
        return "NA"
    pct = 100.0 * v
    return f"{pct:.2f}+/-{100.0 * std:.2f}%" if std > 0.0 else f"{pct:.2f}%"


def _print_summary(rows: list[dict[str, Any]], all_results: list[dict[str, Any]]) -> None:
    """
    Print two tables:

    1. **Paired comparison** — classic LG vs gMAS per tool-config (as before).
    2. **Flat leaderboard** — one row per (framework, tool_config, dataset)
       so you can compare gMAS[simple] vs gMAS[deep_search] vs LangGraph[simple] etc.
    """

    def _out(line: str = "") -> None:
        _log(line)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", errors="replace").decode("ascii"))

    # ── Table 1: paired LG vs gMAS ──────────────────────────────────────
    if rows:
        bar = "-" * 130
        _out(f"\n{bar}")
        _out("  Table 1 — Paired: LangGraph vs gMAS (same tool config, same samples)")
        _out(bar)
        hdr = (
            f"  {'Test':<42} {'N':>5}  {'LG t':>7}  {'gMAS t':>7}  "
            f"{'LG tok':>8}  {'gMAS tok':>8}  {'LG acc':>14}  {'gMAS acc':>15}  "
            f"{'LG err':>6}  {'gM err':>6}  {'Winner':>10}"
        )
        _out(hdr)
        _out(
            f"  {'-' * 42} {'-' * 5}  {'-' * 7}  {'-' * 7}  {'-' * 8}  {'-' * 8}  "
            f"{'-' * 14}  {'-' * 15}  {'-' * 6}  {'-' * 6}  {'-' * 10}"
        )

        for row in rows:
            winner = "gMAS" if row["gm_time"] < row["lg_time"] else "LangGraph"
            lg_err = int(row.get("lg_errors", 0))
            gm_err = int(row.get("gm_errors", 0))
            _out(
                f"  {row['summary_key']:<42} {round(row['paired_samples']):>5}  "
                f"{row['lg_time']:>6.2f}s  {row['gm_time']:>6.2f}s  "
                f"{row['lg_tok']:>8.0f}  {row['gm_tok']:>8.0f}  "
                f"{_fmt_acc(row['lg_acc'], row.get('lg_acc_std', 0.0)):>14}  "
                f"{_fmt_acc(row['gm_acc'], row.get('gm_acc_std', 0.0)):>15}  "
                f"{lg_err:>6}  {gm_err:>6}  {winner:>10}"
            )

    # ── Table 2: flat leaderboard ────────────────────────────────────────
    flat: dict[str, dict[str, Any]] = {}
    for r in all_results:
        if r.get("error"):
            continue
        fw = r.get("framework", "?")
        test = r.get("test", "?")
        ds = r.get("dataset", "?")
        key = f"{fw} | {test} | {ds}"
        bucket = flat.setdefault(
            key,
            {
                "framework": fw,
                "test": test,
                "dataset": ds,
                "times": [],
                "tokens": [],
                "calls": [],
                "scores": [],
                "n": 0,
                "errors": 0,
            },
        )
        bucket["n"] += 1
        bucket["times"].append(float(r.get("time", 0)))
        bucket["tokens"].append(float(r.get("tokens", 0)))
        bucket["calls"].append(float(r.get("calls", 0)))
        qs = r.get("quality_score")
        if qs is not None:
            bucket["scores"].append(float(qs))
    for r in all_results:
        if r.get("error"):
            fw = r.get("framework", "?")
            test = r.get("test", "?")
            ds = r.get("dataset", "?")
            key = f"{fw} | {test} | {ds}"
            if key in flat:
                flat[key]["errors"] += 1

    if flat:
        bar2 = "-" * 120
        _out(f"\n{bar2}")
        _out("  Table 2 — Leaderboard: all framework + tool configurations")
        _out(bar2)
        hdr2 = (
            f"  {'Framework':<12} {'Tool Config':<25} {'Dataset':<16} "
            f"{'N':>4}  {'Avg time':>8}  {'Avg tok':>8}  {'Accuracy':>14}  {'Errors':>6}"
        )
        _out(hdr2)
        _out(f"  {'-' * 12} {'-' * 25} {'-' * 16} {'-' * 4}  {'-' * 8}  {'-' * 8}  {'-' * 14}  {'-' * 6}")

        sorted_keys = sorted(flat.keys(), key=lambda k: (flat[k]["dataset"], flat[k]["test"], flat[k]["framework"]))
        for key in sorted_keys:
            b = flat[key]
            avg_t = _mean(b["times"])
            avg_tok = _mean(b["tokens"])
            acc = _mean(b["scores"]) if b["scores"] else None
            acc_std = _sample_std(b["scores"]) if len(b["scores"]) > 1 else 0.0
            _out(
                f"  {b['framework']:<12} {b['test']:<25} {b['dataset']:<16} "
                f"{b['n']:>4}  {avg_t:>7.2f}s  {avg_tok:>8.0f}  "
                f"{_fmt_acc(acc, acc_std):>14}  {b['errors']:>6}"
            )

        _out("")


# ═══════════════════════════════════════════════════════════════════════════
# Connection check
# ═══════════════════════════════════════════════════════════════════════════


def _connect_model(spec: ModelSpec, cfg: BenchmarkConfig) -> None:
    _log(f"\n  [*] Testing connection: {spec.alias} ({spec.model} @ {spec.base_url})")
    if cfg.skip_ping:
        _log("  [!] --skip-ping: connection check skipped")
        print("  [!] --skip-ping: connection check skipped")
        return

    tracked = _make_llm(spec, cfg, framework="ping")
    delay = cfg.ping_delay
    last_exc: Exception | None = None

    for attempt in range(cfg.ping_retries):
        try:
            ping = tracked.chat(
                "Return plain text only.",
                "Reply with exactly OK",
                _agent_name="ping",
            )
            if not (ping or "").strip():
                _log(f"  [!] {spec.alias} responded with empty content on ping; continuing anyway")
                return
            _log(f"  [+] OK -> {_safe((ping or '')[:60])}")
        except Exception as exc:
            last_exc = exc
            if attempt < cfg.ping_retries - 1:
                _log(f"  [!] Ping attempt {attempt + 1}/{cfg.ping_retries} failed: {exc}  retrying in {delay:.0f}s...")
                print(f"  [!] Ping attempt {attempt + 1}/{cfg.ping_retries} failed: {exc}  retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay = min(delay * 2, cfg.ping_delay_max)
        else:
            return

    msg = f"Cannot reach {spec.alias} after {cfg.ping_retries} ping attempts: {last_exc}"
    _log(f"  [!] WARNING: {msg}")
    print(f"  [!] WARNING: {msg}")
    print("  [!] Continuing anyway -- requests will retry on failure.")


# ═══════════════════════════════════════════════════════════════════════════
# Main orchestration
# ═══════════════════════════════════════════════════════════════════════════


def _run_all_tests(
    spec: ModelSpec,
    cfg: BenchmarkConfig,
    dataset: str,
    problems: list[dict[str, Any]],
    tool_configs: list[str],
    run_idx: int,
    all_results: list[dict[str, Any]],
    completed_tests: dict[str, set[str]],
    checkpoint_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    new_results: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for tool_config_name in tool_configs:
        test_key = _make_test_key(tool_config_name)
        title = f"Single Agent + web_search({tool_config_name})"
        _header(f"{title} | {dataset} | run {run_idx}")

        lg_fn, gmas_fn = _build_test_fns(tool_config_name)
        completed = completed_tests.get(test_key, set())
        remaining_count = len(
            [p for p in problems if _ck_key(spec.alias, test_key, dataset, p["index"], run_idx) not in completed]
        )

        if remaining_count > 0:
            _log(f"  Running {remaining_count}/{len(problems)} in parallel (max={spec.max_parallel})...")
            pbar = None
            if TQDM_AVAILABLE:
                already = len(problems) - remaining_count
                pbar = tqdm(
                    total=len(problems),
                    initial=already,
                    desc=f"  run{run_idx}:{test_key}",
                    unit="exp",
                    leave=False,
                )

            batch = asyncio.run(
                _run_parallel(
                    spec,
                    cfg,
                    test_key,
                    lg_fn,
                    gmas_fn,
                    dataset,
                    problems,
                    run_idx,
                    cfg.framework_timeout,
                    completed,
                    completed_tests,
                    pbar,
                )
            )
            if pbar:
                pbar.close()

            flat = [item for pair in batch for item in pair]
            new_results.extend(flat)
            all_results.extend(flat)

            if checkpoint_path:
                _save_checkpoint(checkpoint_path, all_results, completed_tests)
        else:
            _log(f"  All {len(problems)} already completed for this run.")

        existing = [
            r
            for r in all_results
            if r.get("model_alias") == spec.alias
            and r.get("test") == test_key
            and r.get("dataset") == dataset
            and int(r.get("run_idx", 1)) == run_idx
        ]
        pairs = _align_pairs(existing)
        if not pairs:
            _log("  No paired samples yet.")
            continue

        row = _build_summary_row(spec, test_key, dataset, run_idx, pairs, all_results)
        if row:
            summary_rows.append(row)
            _log(
                f"  Pairs: {row['paired_samples']} | "
                f"LG {row['lg_time']:.2f}s/{row['lg_tok']:.0f}tok/{_fmt_acc(row['lg_acc'])}/err={row['lg_errors']} | "
                f"gMAS {row['gm_time']:.2f}s/{row['gm_tok']:.0f}tok/{_fmt_acc(row['gm_acc'])}/err={row['gm_errors']}"
            )

    return new_results, summary_rows


def run_benchmark(cfg: BenchmarkConfig | None = None, **kwargs: Any) -> dict[str, Any]:
    """
    Run the web-tool benchmark suite.

    Args:
        cfg: A pre-built ``BenchmarkConfig``.  If *None*, one is created
             from ``**kwargs``.
        **kwargs: Keyword arguments forwarded to ``BenchmarkConfig`` when
             *cfg* is ``None``.

    """
    if cfg is None:
        cfg = BenchmarkConfig(**kwargs)

    log_dir = cfg.log_dir
    run_log_path = _open_log(log_dir)
    print(f"  [log] {run_log_path}")

    try:
        api_key, base_url, model = _validate_llm_config(cfg)
        spec = ModelSpec(
            alias=cfg.model_alias,
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_parallel=cfg.max_parallel,
        )

        _header("Benchmark: gMAS vs LangGraph — Single Agent + Web Search")
        _log(f"  Model        : {spec.model}")
        _log(f"  Base URL     : {spec.base_url}")
        _log(f"  Max parallel : {spec.max_parallel}")
        _log(f"  Runs         : {cfg.num_runs}")
        _log(f"  Timeout/task : {cfg.framework_timeout:.0f}s")
        _log(f"  Temperature  : {cfg.temperature}")
        _log(f"  Max tokens   : {cfg.default_max_tokens}")

        _connect_model(spec, cfg)

        checkpoint_path = log_dir / cfg.checkpoint_name
        all_results: list[dict[str, Any]] = []
        completed_tests: dict[str, set[str]] = {}

        if cfg.resume and checkpoint_path.exists():
            _log("\n  [*] Loading checkpoint...")
            all_results, completed_tests = _load_checkpoint(checkpoint_path)
            _log(f"  [+] {len(all_results)} saved rows, {sum(len(s) for s in completed_tests.values())} completed")

        datasets = (
            [d for d in _ALL_DATASETS if d in cfg.datasets_filter] if cfg.datasets_filter else list(_ALL_DATASETS)
        )
        tool_configs = (
            [tc for tc in _ALL_TOOL_CONFIGS if tc in cfg.tool_configs_filter]
            if cfg.tool_configs_filter
            else list(_ALL_TOOL_CONFIGS)
        )

        if cfg.datasets_filter:
            unknown = [d for d in cfg.datasets_filter if d not in _ALL_DATASETS]
            if unknown:
                print(f"  WARNING: unknown datasets ignored: {unknown}", file=sys.stderr)
        if cfg.tool_configs_filter:
            unknown = [tc for tc in cfg.tool_configs_filter if tc not in _ALL_TOOL_CONFIGS]
            if unknown:
                print(f"  WARNING: unknown tool configs ignored: {unknown}", file=sys.stderr)

        _log(f"  Datasets     : {datasets}")
        _log(f"  Tool configs : {tool_configs}")

        all_summary_rows: list[dict[str, Any]] = []
        for dataset_name in datasets:
            _header(f"Dataset: {dataset_name}")
            problems = _load_dataset_samples(dataset_name)
            if not problems:
                _log(f"  WARNING: no samples loaded from {dataset_name}, skipping.")
                continue
            if cfg.max_samples is not None and len(problems) > cfg.max_samples:
                problems = problems[: cfg.max_samples]
                _log(f"  Trimmed to {len(problems)} samples (--max-samples {cfg.max_samples})")
            _log(f"  Loaded {len(problems)} samples")

            for run_idx in range(1, cfg.num_runs + 1):
                _, rows = _run_all_tests(
                    spec,
                    cfg,
                    dataset_name,
                    problems,
                    tool_configs,
                    run_idx,
                    all_results,
                    completed_tests,
                    checkpoint_path,
                )
                all_summary_rows.extend(rows)

        final_rows = _aggregate_rows(all_summary_rows)
        json_path = _save_log_json(spec, all_results, datasets, log_dir, checkpoint_path)
        _log(f"\n  JSON results -> {json_path}")
        try:
            _print_summary(final_rows, all_results)
        except Exception as exc:
            _log(f"  [ERROR] _print_summary failed: {exc}")
        _log(f"\n  Detailed log  -> {run_log_path}")
        _log(f"  JSON results  -> {json_path}")
        try:
            print(f"\n  Detailed log  -> {run_log_path}")
            print(f"  JSON results  -> {json_path}")
        except UnicodeEncodeError:
            pass

        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "model": spec.model,
            "base_url": spec.base_url,
            "results_count": len(all_results),
            "log_file": str(run_log_path),
            "json_file": str(json_path),
            "summary_rows": final_rows,
        }
    finally:
        _close_log()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Benchmark gMAS vs LangGraph — Single Agent + Web Search Tool",
    )

    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Number of full benchmark passes (default: 1)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=_DEFAULT_CFG.max_parallel,
        help=f"Max parallel experiments (default: {_DEFAULT_CFG.max_parallel})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_CFG.framework_timeout,
        help=f"Per-framework timeout in seconds (default: {_DEFAULT_CFG.framework_timeout:.0f})",
    )
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, ignore checkpoint")
    parser.add_argument("--skip-ping", action="store_true", help="Skip connection check")
    parser.add_argument(
        "--datasets",
        type=str,
        default=None,
        metavar="DS1,DS2",
        help=f"Comma-separated datasets (default: all). Available: {', '.join(_ALL_DATASETS)}",
    )
    parser.add_argument(
        "--tool-configs",
        type=str,
        default=None,
        metavar="TC1,TC2",
        help=f"Comma-separated tool configs (default: all). Available: {', '.join(_ALL_TOOL_CONFIGS)}",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        metavar="N",
        help="Limit samples per dataset (default: all)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=_DEFAULT_CFG.temperature,
        help=f"LLM sampling temperature (default: {_DEFAULT_CFG.temperature})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=_DEFAULT_CFG.default_max_tokens,
        help=f"Default max tokens per LLM call (default: {_DEFAULT_CFG.default_max_tokens})",
    )
    parser.add_argument(
        "--runner-timeout",
        type=float,
        default=_DEFAULT_CFG.runner_timeout,
        help=f"gMAS runner timeout in seconds (default: {_DEFAULT_CFG.runner_timeout:.0f})",
    )
    parser.add_argument(
        "--alias",
        type=str,
        default=_DEFAULT_CFG.model_alias,
        help=f"Model alias for logs/checkpoints (default: {_DEFAULT_CFG.model_alias})",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=str(_DEFAULT_CFG.log_dir),
        help=f"Directory for logs and JSON results (default: {_DEFAULT_CFG.log_dir})",
    )

    args = parser.parse_args()

    cfg = BenchmarkConfig(
        num_runs=args.runs,
        max_parallel=args.parallel,
        framework_timeout=args.timeout,
        resume=not args.no_resume,
        skip_ping=args.skip_ping,
        datasets_filter=[d.strip() for d in args.datasets.split(",")] if args.datasets else None,
        tool_configs_filter=[tc.strip() for tc in args.tool_configs.split(",")] if args.tool_configs else None,
        max_samples=args.max_samples,
        temperature=args.temperature,
        default_max_tokens=args.max_tokens,
        runner_timeout=args.runner_timeout,
        model_alias=args.alias,
        log_dir=Path(args.log_dir),
    )

    run_benchmark(cfg)
