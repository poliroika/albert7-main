import argparse
import asyncio
import concurrent.futures
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, cast
from unicodedata import normalize

try:
    from datasets import load_dataset
except ImportError:
    print("ERROR: datasets library not installed. Install with: pip install datasets", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm

    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False
    print("WARNING: tqdm not installed. Progress bars disabled. Install: pip install tqdm", file=sys.stderr)

try:
    from scipy.stats import chi2, norm

    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("WARNING: scipy not installed. Statistical tests limited. Install: pip install scipy", file=sys.stderr)

import contextlib

import httpx
from openai import AsyncOpenAI, OpenAI

from gmas.builder import BuilderConfig, GraphBuilder
from gmas.execution import MACPRunner, RunnerConfig, StreamEventType

try:
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class BenchmarkConfig:
    """
    All tuneable parameters in one place.

    Pass to ``run_benchmark()`` or let the CLI build it from flags.
    Every field has a sensible default so you can override only what you need.
    """

    # ── parallelism / timeouts ────────────────────────────────────────
    max_parallel: int = 50
    framework_timeout: float = 900.0
    runner_timeout: float = 180.0

    # ── LLM call defaults ─────────────────────────────────────────────
    temperature: float = 0.7
    llm_timeout: float = 300.0
    default_max_tokens: int = 8192
    llm_retries: int = 5
    jitter_range: tuple[float, float] = (0.0, 0.5)
    backoff_base: float = 2.0
    backoff_max: float = 60.0
    extra_body: dict[str, Any] = field(
        default_factory=lambda: {"chat_template_kwargs": {"enable_thinking": False}},
    )

    # ── ping (connection check) ───────────────────────────────────────
    ping_retries: int = 6
    ping_delay: float = 5.0
    ping_delay_max: float = 120.0
    ping_max_tokens: int = 64
    skip_ping: bool = False

    # ── benchmark run ─────────────────────────────────────────────────
    num_runs: int = 1
    resume: bool = True
    log_dir: Path = Path("benchmark_logs")
    checkpoint_name: str = "checkpoint_local.json"
    model_alias: str = "remote"

    # ── environment variable names ────────────────────────────────────
    env_api_key: str = "LLM_API_KEY"
    env_base_url: str = "LLM_BASE_URL"
    env_model: str = "LLM_MODEL"
    env_defaults: dict[str, str] = field(
        default_factory=lambda: {
            "LLM_API_KEY": "you_key",
            "LLM_BASE_URL": "you_url",
            "LLM_MODEL": "you_model",
        },
    )

    # ── code execution (HumanEval pass@1) ──────────────────────────
    code_exec_timeout: float = 10.0  # seconds per test-case execution

    # ── datasets ──────────────────────────────────────────────────────
    datasets_filter: list[str] | None = None
    max_samples: int | None = None  # limit samples per dataset (None = all)


_DEFAULT_CFG = BenchmarkConfig()

_ALL_DATASETS = ["mmlu-pro", "gsm8k", "bigbench", "gpqa", "human-eval", "big-bench-hard"]


# ═══════════════════════════════════════════════════════════════════════════
# Global state (logging, thread-pool cache)
# ═══════════════════════════════════════════════════════════════════════════

_EXECUTORS: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
_LOG_FH: io.TextIOWrapper | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Logging helpers
# ═══════════════════════════════════════════════════════════════════════════


def _log(msg: str = "", *, console: bool = False) -> None:
    if _LOG_FH is not None:
        _LOG_FH.write(msg + "\n")
        _LOG_FH.flush()
    if console:
        print(msg)


def _open_log(log_dir: Path) -> Path:
    global _LOG_FH
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"benchmark_run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.txt"
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
    cleaned = text.replace("\n", " ↵ ").strip()
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
    max_parallel: int = 50


# ═══════════════════════════════════════════════════════════════════════════
# LLM wrapper with tracking
# ═══════════════════════════════════════════════════════════════════════════


def _extract_message_text(content: Any) -> str:
    """Recursively extract plain text from various LLM response content formats."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return text if isinstance(text, str) else str(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content")
                if isinstance(t, str):
                    parts.append(t)
            elif hasattr(item, "text") and isinstance(item.text, str):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    for attr in ("text", "content"):
        val = getattr(content, attr, None)
        if isinstance(val, str):
            return val
    return str(content)


_RETRYABLE_KEYWORDS = (
    "timeout",
    "timed out",
    "connection error",
    "connection reset",
    "connection refused",
    "connectionerror",
    "remotedisconnected",
    "broken pipe",
    "econnreset",
    "network",
    "503",
    "502",
    "429",
    "404",
    "<!doctype html",
    "not found",
    "unavailable",
)


class TrackedLLM:
    """OpenAI-compatible LLM client that tracks token usage and call count."""

    def __init__(self, spec: ModelSpec, cfg: BenchmarkConfig, framework: str = "unknown") -> None:
        self._spec = spec
        self._cfg = cfg
        self._framework = framework
        self._client = OpenAI(
            api_key=spec.api_key,
            base_url=spec.base_url,
            timeout=cfg.llm_timeout,
            http_client=httpx.Client(transport=httpx.HTTPTransport(proxy=None)),
        )
        self._async_client: AsyncOpenAI | None = None
        self.total_tokens: int = 0
        self.call_count: int = 0

    def _get_async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                api_key=self._spec.api_key,
                base_url=self._spec.base_url,
                timeout=self._cfg.llm_timeout,
                http_client=httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(proxy=None)),
            )
        return self._async_client

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.close()
            self._async_client = None

    def reset(self) -> None:
        self.total_tokens = 0
        self.call_count = 0

    def _record(self, response: Any) -> int:
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        self.total_tokens += tokens
        self.call_count += 1
        return tokens

    def _text(self, response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            _log(f"  [WARN] LLM response has no choices! response={response}")
            return ""

        first = choices[0]
        finish_reason = getattr(first, "finish_reason", None)
        if finish_reason and finish_reason != "stop":
            _log(f"  [WARN] LLM finish_reason={finish_reason}")

        msg = getattr(first, "message", None)
        if msg is not None:
            text = _extract_message_text(getattr(msg, "content", None))
            if not text:
                reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
                if reasoning and isinstance(reasoning, str):
                    _log(f"  [INFO] msg.content empty, using reasoning_content ({len(reasoning)} chars)")
                    text = reasoning
            if not text:
                _log(
                    f"  [WARN] LLM returned empty content. "
                    f"refusal={getattr(msg, 'refusal', None)} "
                    f"tool_calls={bool(getattr(msg, 'tool_calls', None))} "
                    f"finish_reason={finish_reason}"
                )
            return text

        return _extract_message_text(getattr(first, "text", None) or first)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in _RETRYABLE_KEYWORDS)

    # ── core call logic (shared by all call styles) ───────────────────

    def _call_sync(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        agent_name: str,
    ) -> str:
        """Synchronous LLM call with retry, jitter, and logging."""
        cfg = self._cfg
        delay = cfg.backoff_base
        t0 = time.perf_counter()
        time.sleep(random.uniform(*cfg.jitter_range))

        for attempt in range(cfg.llm_retries):
            try:
                r = self._client.chat.completions.create(
                    model=self._spec.model,
                    messages=cast("Any", messages),
                    temperature=cfg.temperature,
                    max_tokens=max_tokens,
                    timeout=cfg.llm_timeout,
                    extra_body=cfg.extra_body or None,
                )
                tokens = self._record(r)
                text = self._text(r)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                if not text:
                    _log(f"  [WARN] {self._framework}/{agent_name}: empty text, tokens={tokens}")
                sys_msg = next((str(m["content"]) for m in messages if m["role"] == "system"), "")
                user_msg = next((str(m["content"]) for m in messages if m["role"] == "user"), "")
                _log_llm_call(self._framework, agent_name, sys_msg, user_msg, text, tokens, elapsed_ms)
            except Exception as exc:
                if self._is_retryable(exc) and attempt < cfg.llm_retries - 1:
                    _log(f"  [RETRY] {self._framework}/{agent_name} attempt {attempt + 1}/{cfg.llm_retries}: {exc}")
                    time.sleep(delay)
                    delay = min(delay * 2, cfg.backoff_max)
                    continue
                msg = f"LLM call failed: {exc}"
                raise RuntimeError(msg) from exc
            else:
                return text
        return ""

    async def _call_async(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        agent_name: str,
    ) -> str:
        """Asynchronous LLM call with retry, jitter, and logging."""
        cfg = self._cfg
        delay = cfg.backoff_base
        t0 = time.perf_counter()
        await asyncio.sleep(random.uniform(*cfg.jitter_range))

        for attempt in range(cfg.llm_retries):
            try:
                r = await self._get_async_client().chat.completions.create(
                    model=self._spec.model,
                    messages=cast("Any", messages),
                    temperature=cfg.temperature,
                    max_tokens=max_tokens,
                    timeout=cfg.llm_timeout,
                    extra_body=cfg.extra_body or None,
                )
                tokens = self._record(r)
                text = self._text(r)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                if not text:
                    _log(f"  [WARN] {self._framework}/{agent_name}: empty text, tokens={tokens}")
                sys_msg = next((str(m["content"]) for m in messages if m["role"] == "system"), "")
                user_msg = next((str(m["content"]) for m in messages if m["role"] == "user"), "")
                _log_llm_call(self._framework, agent_name, sys_msg, user_msg, text, tokens, elapsed_ms)
            except Exception as exc:
                if self._is_retryable(exc) and attempt < cfg.llm_retries - 1:
                    _log(f"  [RETRY] {self._framework}/{agent_name} attempt {attempt + 1}/{cfg.llm_retries}: {exc}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, cfg.backoff_max)
                    continue
                msg = f"Async LLM call failed: {exc}"
                raise RuntimeError(msg) from exc
            else:
                return text
        return ""

    # ── public API ────────────────────────────────────────────────────

    @staticmethod
    def _build_messages(system: str, user: str) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        return msgs

    def chat(self, system: str, user: str, max_tokens: int = 0, agent_name: str = "unknown") -> str:
        return self._call_sync(
            self._build_messages(system, user),
            max_tokens or self._cfg.default_max_tokens,
            agent_name,
        )

    async def achat(self, system: str, user: str, max_tokens: int = 0, agent_name: str = "unknown") -> str:
        return await self._call_async(
            self._build_messages(system, user),
            max_tokens or self._cfg.default_max_tokens,
            agent_name,
        )

    @staticmethod
    def _extract_agent_hint(messages: list[dict[str, Any]]) -> str:
        for m in messages:
            if m.get("role") == "system":
                match = re.search(r"You are (\w+)", str(m.get("content", "")))
                if match:
                    return match.group(1)
                break
        return "gMAS-agent"

    def structured_caller(self, max_tokens: int = 0):
        """Return a sync callable(messages) -> str for gMAS runner."""
        tok = max_tokens or self._cfg.default_max_tokens

        def _call(messages: list[dict[str, Any]]) -> str:
            saved = self._framework
            self._framework = "gMAS"
            try:
                return self._call_sync(messages, tok, self._extract_agent_hint(messages))
            finally:
                self._framework = saved

        return _call

    def async_structured_caller(self, max_tokens: int = 0):
        """Return an async callable(messages) -> str for gMAS runner."""
        tok = max_tokens or self._cfg.default_max_tokens

        async def _call(messages: list[dict[str, Any]]) -> str:
            saved = self._framework
            self._framework = "gMAS"
            try:
                return await self._call_async(messages, tok, self._extract_agent_hint(messages))
            finally:
                self._framework = saved

        return _call


# ═══════════════════════════════════════════════════════════════════════════
# gMAS runner helper
# ═══════════════════════════════════════════════════════════════════════════


def _log_stream_event(event: Any) -> None:
    etype = getattr(event, "event_type", "?")
    if etype == StreamEventType.RUN_START:
        _log(
            f"    [STREAM] RUN_START query={_truncate(getattr(event, 'query', ''), 100)} "
            f"agents={getattr(event, 'num_agents', '?')} "
            f"order={getattr(event, 'execution_order', [])}"
        )
    elif etype == StreamEventType.AGENT_START:
        _log(
            f"    [STREAM] AGENT_START id={getattr(event, 'agent_id', '?')} "
            f"name={getattr(event, 'agent_name', '?')} "
            f"step={getattr(event, 'step_index', '?')}"
        )
    elif etype == StreamEventType.AGENT_OUTPUT:
        content = _extract_message_text(getattr(event, "content", ""))
        _log(
            f"    [STREAM] AGENT_OUTPUT id={getattr(event, 'agent_id', '?')} "
            f"tokens={getattr(event, 'tokens_used', 0)} "
            f"time={getattr(event, 'duration_ms', 0):.0f}ms "
            f"is_final={getattr(event, 'is_final', False)}"
        )
        _log(f"             content: {_truncate(content, 400)}")
    elif etype == StreamEventType.AGENT_ERROR:
        _log(
            f"    [STREAM] AGENT_ERROR id={getattr(event, 'agent_id', '?')} "
            f"error={getattr(event, 'error_message', '?')}"
        )
    elif etype == StreamEventType.RUN_END:
        _log(
            f"    [STREAM] RUN_END success={getattr(event, 'success', '?')} "
            f"total_tokens={getattr(event, 'total_tokens', 0)} "
            f"total_time={getattr(event, 'total_duration_ms', 0):.0f}ms"
        )
    elif etype in (StreamEventType.PARALLEL_START, StreamEventType.PARALLEL_END):
        _log(f"    [STREAM] {etype} agents={getattr(event, 'agent_ids', [])}")
    else:
        _log(f"    [STREAM] {etype}")


def _collect_stream_output(event_iter, final_agent_id: str) -> tuple[str, dict[str, str]]:
    """Consume a stream of events and return (final_output, all_agent_outputs)."""
    output = ""
    run_end_answer = ""
    agent_outputs: dict[str, str] = {}

    for event in event_iter:
        _log_stream_event(event)
        if event.event_type == StreamEventType.AGENT_OUTPUT:
            aid = getattr(event, "agent_id", "")
            content = _extract_message_text(getattr(event, "content", ""))
            agent_outputs[aid] = content
            if aid == final_agent_id:
                output = content
        elif event.event_type == StreamEventType.RUN_END:
            run_end_answer = getattr(event, "final_answer", "") or ""

    if not output and run_end_answer:
        _log(f"  [WARN] final agent '{final_agent_id}' empty, using RunEndEvent.final_answer")
        output = run_end_answer
    if not output and agent_outputs:
        last_agent = list(agent_outputs)[-1]
        output = agent_outputs[last_agent]
        _log(f"  [WARN] using last agent '{last_agent}' output as fallback")

    return output, agent_outputs


async def _collect_stream_output_async(event_iter, final_agent_id: str) -> tuple[str, dict[str, str]]:
    """Async version of _collect_stream_output."""
    output = ""
    run_end_answer = ""
    agent_outputs: dict[str, str] = {}

    async for event in event_iter:
        _log_stream_event(event)
        if event.event_type == StreamEventType.AGENT_OUTPUT:
            aid = getattr(event, "agent_id", "")
            content = _extract_message_text(getattr(event, "content", ""))
            agent_outputs[aid] = content
            if aid == final_agent_id:
                output = content
        elif event.event_type == StreamEventType.RUN_END:
            run_end_answer = getattr(event, "final_answer", "") or ""

    if not output and run_end_answer:
        _log(f"  [WARN] final agent '{final_agent_id}' empty, using RunEndEvent.final_answer")
        output = run_end_answer
    if not output and agent_outputs:
        last_agent = list(agent_outputs)[-1]
        output = agent_outputs[last_agent]
        _log(f"  [WARN] using last agent '{last_agent}' output as fallback")

    return output, agent_outputs


def _run_gmas(
    llm: TrackedLLM,
    build_fn,
    final_agent_id: str,
    max_tokens: int = 0,
    *,
    parallel: bool = False,
    broadcast_task: bool = False,
) -> dict[str, Any]:
    """Build a gMAS graph, run it, and return framework-level metrics."""
    cfg = llm._cfg
    tok = max_tokens or cfg.default_max_tokens
    llm.reset()
    t0 = time.perf_counter()
    graph = build_fn()

    _log(f"\n  ── gMAS graph built: {len(graph.agents)} agents, final={final_agent_id}, parallel={parallel}")

    runner_kwargs: dict[str, Any] = {
        "structured_llm_caller": llm.structured_caller(max_tokens=tok),
        "config": RunnerConfig(
            timeout=cfg.runner_timeout,
            adaptive=False,
            enable_parallel=parallel,
            update_states=True,
            broadcast_task_to_all=broadcast_task,
        ),
    }
    if parallel:
        runner_kwargs["async_structured_llm_caller"] = llm.async_structured_caller(max_tokens=tok)

    runner = MACPRunner(**runner_kwargs)

    if parallel:

        async def _run_async() -> tuple[str, dict[str, str]]:
            try:
                return await _collect_stream_output_async(
                    runner.astream(graph, final_agent_id=final_agent_id),
                    final_agent_id,
                )
            finally:
                await llm.aclose()

        output, all_agent_outputs = asyncio.run(_run_async())
    else:
        output, all_agent_outputs = _collect_stream_output(
            runner.stream(graph, final_agent_id=final_agent_id),
            final_agent_id,
        )

    elapsed = time.perf_counter() - t0
    _log(f"  ── gMAS finished: time={elapsed:.2f}s  tokens={llm.total_tokens}  calls={llm.call_count}")
    _log(f"  ── gMAS agent outputs: {', '.join(f'{k}={len(v)}ch' for k, v in all_agent_outputs.items())}")
    _log(f"  ── gMAS final output ({final_agent_id}): {_truncate(output, 500)}")

    return {
        "framework": "gMAS",
        "time": elapsed,
        "tokens": llm.total_tokens,
        "calls": llm.call_count,
        "output": output,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Prompt helpers
# ═══════════════════════════════════════════════════════════════════════════


def _is_mc(problem: str) -> bool:
    return "Choices:" in problem or bool(re.search(r"^[A-Z]\.", problem, re.MULTILINE))


def _mc_prompt(desc: str) -> str:
    return (
        f"{desc} IMPORTANT: Your response must contain ONLY the letter of the correct "
        "answer. No explanation. Just the single letter."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Test topologies — gMAS implementations
# ═══════════════════════════════════════════════════════════════════════════


def _build_single_gmas(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    def _build():
        mc = _is_mc(problem)
        builder = GraphBuilder(BuilderConfig(include_task_node=True, validate=True))
        builder.add_task(query=problem, description="Task")
        desc = (
            _mc_prompt("Solve the task carefully and provide the correct final answer.")
            if mc
            else (
                "Solve the task carefully and provide the correct final answer. "
                "Provide the answer in a clear, task-appropriate format."
            )
        )
        builder.add_agent("solver", "Solver", "an expert problem solver", desc)
        builder.connect_task_to_agents(agent_ids=["solver"], bidirectional=False)
        return builder.build()

    return _run_gmas(llm, _build, "solver")


def _build_chain_gmas(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    def _build():
        mc = _is_mc(problem)
        builder = GraphBuilder(BuilderConfig(include_task_node=True, validate=True))
        builder.add_task(query=problem, description="Task")
        builder.add_agent(
            "analyzer",
            "Analyzer",
            "an analyst",
            "Analyze the task, identify its type, key constraints, and the best approach to solve it.",
        )
        builder.add_agent(
            "solver",
            "Solver",
            "an expert problem solver",
            "Solve the task using the proposed approach. Be accurate, complete, and logically consistent.",
        )
        builder.add_agent(
            "formatter",
            "Formatter",
            "a presenter",
            _mc_prompt("Produce the final answer in the required format.")
            if mc
            else "Produce the final answer in the required format. Provide a concise and clear final answer.",
        )
        builder.connect_task_to_agents(agent_ids=["analyzer"], bidirectional=False)
        builder.add_workflow_edge("analyzer", "solver")
        builder.add_workflow_edge("solver", "formatter")
        return builder.build()

    return _run_gmas(llm, _build, "formatter")


def _build_fanin_gmas(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    def _build():
        mc = _is_mc(problem)
        builder = GraphBuilder(BuilderConfig(include_task_node=True, validate=True))
        builder.add_task(query=problem, description="Task")
        builder.add_agent(
            "reasoner_a",
            "Reasoner A",
            "an independent reasoner",
            "Solve the task using one strong, self-consistent line of reasoning.",
        )
        builder.add_agent(
            "reasoner_b",
            "Reasoner B",
            "an independent reasoner",
            "Solve the task using a different reasoning path or perspective from Reasoner A.",
        )
        builder.add_agent(
            "aggregator",
            "Aggregator",
            "a verifier",
            _mc_prompt(
                "Compare both candidate solutions, resolve any disagreements, and provide the final correct answer."
            )
            if mc
            else "Compare both candidate solutions, resolve any disagreements, and provide the final correct answer.",
        )
        builder.connect_task_to_agents(agent_ids=["reasoner_a", "reasoner_b"], bidirectional=False)
        builder.add_workflow_edge("reasoner_a", "aggregator")
        builder.add_workflow_edge("reasoner_b", "aggregator")
        return builder.build()

    return _run_gmas(llm, _build, "aggregator", parallel=True)


def _build_fanout_gmas(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    def _build():
        mc = _is_mc(problem)
        builder = GraphBuilder(BuilderConfig(include_task_node=True, validate=True))
        builder.add_task(query=problem, description="Task")
        builder.add_agent(
            "planner",
            "Planner",
            "a solution planner",
            "Analyze the task and propose three distinct ways to approach it.",
        )
        builder.add_agent(
            "reasoner_a", "Reasoner A", "an independent reasoner", "Solve the task using the first proposed approach."
        )
        builder.add_agent(
            "reasoner_b", "Reasoner B", "an independent reasoner", "Solve the task using the second proposed approach."
        )
        builder.add_agent(
            "reasoner_c",
            "Reasoner C",
            "an independent reasoner",
            "Solve the task using the third proposed approach or a substantially different perspective.",
        )
        builder.add_agent(
            "synthesizer",
            "Synthesizer",
            "a result synthesizer",
            _mc_prompt("Compare all candidate solutions, resolve conflicts, and provide the final correct answer.")
            if mc
            else "Compare all candidate solutions, resolve conflicts, and provide the final correct answer.",
        )
        builder.connect_task_to_agents(agent_ids=["planner"], bidirectional=False)
        for src in ("reasoner_a", "reasoner_b", "reasoner_c"):
            builder.add_workflow_edge("planner", src)
            builder.add_workflow_edge(src, "synthesizer")
        return builder.build()

    return _run_gmas(llm, _build, "synthesizer", parallel=True)


# ── Generic chain builder (reused by chain-7 and chain-15) ───────────

AgentDef = tuple[str, str, str, str]  # (id, display_name, persona, description)

_CHAIN7_AGENTS: list[AgentDef] = [
    ("reader", "Reader", "a careful reader", "Restate the task clearly and identify the main question."),
    (
        "extractor",
        "Extractor",
        "a data extractor",
        "List the important facts, constraints, inputs, and required outputs.",
    ),
    (
        "classifier",
        "Classifier",
        "a task classifier",
        "Identify the task type and the main reasoning skills or tools needed.",
    ),
    (
        "strategist",
        "Strategist",
        "a solution strategist",
        "Choose the best solution strategy and outline a concise plan.",
    ),
    ("solver", "Solver", "an expert problem solver", "Execute the plan and derive the answer carefully."),
    (
        "verifier",
        "Verifier",
        "a solution verifier",
        "Check the reasoning and verify that the answer satisfies the task requirements.",
    ),
    ("reporter", "Reporter", "a technical writer", "Provide the final answer clearly and concisely."),
]

_CHAIN15_AGENTS: list[AgentDef] = [
    ("reader", "Reader", "a careful reader", "Read the task carefully and restate it without changing any facts."),
    (
        "extractor",
        "Extractor",
        "a data extractor",
        "List all important facts, constraints, inputs, and required outputs.",
    ),
    (
        "categorizer",
        "Categorizer",
        "a task categorizer",
        "Identify the task type and the core reasoning skills required.",
    ),
    (
        "assumptions",
        "Assumptions",
        "an assumption analyst",
        "State any implicit assumptions that may matter for solving the task.",
    ),
    ("planner", "Planner", "a solution planner", "Write a step-by-step solution plan before executing it."),
    (
        "decomposer",
        "Decomposer",
        "a problem decomposer",
        "Break the task into smaller subproblems or intermediate steps.",
    ),
    (
        "structurer",
        "Structurer",
        "a reasoning organizer",
        "Organize the intermediate reasoning into a clear and solvable structure.",
    ),
    ("solver", "Solver", "an expert problem solver", "Solve the task carefully using the plan."),
    (
        "checker",
        "Checker",
        "a consistency checker",
        "Check that the intermediate reasoning is internally consistent and plausible.",
    ),
    (
        "verifier_a",
        "Verifier A",
        "a solution verifier",
        "Verify the proposed answer directly against the task requirements.",
    ),
    (
        "verifier_b",
        "Verifier B",
        "a cross-checker",
        "Confirm the answer using an alternative reasoning path or independent cross-check.",
    ),
    ("interpreter", "Interpreter", "a results interpreter", "Briefly explain what the result means in plain language."),
    (
        "edge_cases",
        "Edge Cases",
        "an edge-case analyst",
        "Briefly consider important edge cases, exceptions, or possible failure modes.",
    ),
    (
        "reviewer",
        "Reviewer",
        "a critical reviewer",
        "Review the full solution and decide whether anything should be corrected.",
    ),
    ("reporter", "Reporter", "a technical writer", "Provide the final answer clearly and concisely."),
]


def _build_chain_n_gmas(llm: TrackedLLM, problem: str, agents: list[AgentDef]) -> dict[str, Any]:
    """Build a gMAS linear chain of N agents."""

    def _build():
        mc = _is_mc(problem)
        builder = GraphBuilder(BuilderConfig(include_task_node=True, validate=True))
        builder.add_task(query=problem, description="Task")
        for i, (aid, disp, persona, desc) in enumerate(agents):
            d = _mc_prompt(desc) if (mc and i == len(agents) - 1) else desc
            builder.add_agent(aid, disp, persona, d)
        builder.connect_task_to_agents(agent_ids=[agents[0][0]], bidirectional=False)
        for i in range(len(agents) - 1):
            builder.add_workflow_edge(agents[i][0], agents[i + 1][0])
        return builder.build()

    return _run_gmas(llm, _build, agents[-1][0])


def _build_chain7_gmas(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    return _build_chain_n_gmas(llm, problem, _CHAIN7_AGENTS)


def _build_chain15_gmas(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    return _build_chain_n_gmas(llm, problem, _CHAIN15_AGENTS)


# ═══════════════════════════════════════════════════════════════════════════
# Test topologies — LangGraph implementations
# ═══════════════════════════════════════════════════════════════════════════


def _langgraph_unavailable() -> dict[str, Any]:
    return {"framework": "langgraph", "error": "langgraph not installed"}


def _build_single_langgraph(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    if not LANGGRAPH_AVAILABLE:
        return _langgraph_unavailable()

    class State(TypedDict):
        input: str
        output: str

    def solver_node(state: State) -> dict[str, str]:
        system = (
            _mc_prompt("Solve the task carefully and provide the correct final answer.")
            if _is_mc(state["input"])
            else "Solve the task carefully and provide the correct final answer. "
            "Provide the answer in a clear, task-appropriate format."
        )
        return {"output": llm.chat(system, state["input"], agent_name="solver")}

    llm.reset()
    t0 = time.perf_counter()
    g = StateGraph(State)
    g.add_node("solver", solver_node)
    g.set_entry_point("solver")
    g.set_finish_point("solver")
    result = g.compile().invoke({"input": problem, "output": ""})
    elapsed = time.perf_counter() - t0
    output = result["output"]
    _log_framework_result("LangGraph", "single_agent", -1, output, elapsed, llm.total_tokens, llm.call_count)
    return {
        "framework": "langgraph",
        "time": elapsed,
        "tokens": llm.total_tokens,
        "calls": llm.call_count,
        "output": output,
    }


def _build_chain_langgraph(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    if not LANGGRAPH_AVAILABLE:
        return _langgraph_unavailable()

    class State(TypedDict):
        input: str
        step1: str
        step2: str
        output: str

    def analyzer(s: State) -> dict[str, str]:
        return {
            "step1": llm.chat(
                "Analyze the task, identify its type, key constraints, and the best approach to solve it.",
                f"Task: {s['input']}",
                agent_name="analyzer",
            )
        }

    def solver(s: State) -> dict[str, str]:
        return {
            "step2": llm.chat(
                "Solve the task using the proposed approach. Be accurate, complete, and logically consistent.",
                f"Analysis:\n{s['step1']}",
                agent_name="solver",
            )
        }

    def formatter(s: State) -> dict[str, str]:
        system = (
            _mc_prompt("Produce the final answer in the required format.")
            if _is_mc(s["input"])
            else "Produce the final answer in the required format. Provide a concise and clear final answer."
        )
        return {"output": llm.chat(system, f"Solution:\n{s['step2']}", agent_name="formatter")}

    llm.reset()
    t0 = time.perf_counter()
    g = StateGraph(State)
    for name, fn in [("analyzer", analyzer), ("solver", solver), ("formatter", formatter)]:
        g.add_node(name, fn)
    g.add_edge("analyzer", "solver")
    g.add_edge("solver", "formatter")
    g.set_entry_point("analyzer")
    g.set_finish_point("formatter")
    result = g.compile().invoke({"input": problem, "step1": "", "step2": "", "output": ""})
    elapsed = time.perf_counter() - t0
    output = result["output"]
    _log_framework_result("LangGraph", "chain_3", -1, output, elapsed, llm.total_tokens, llm.call_count)
    return {
        "framework": "langgraph",
        "time": elapsed,
        "tokens": llm.total_tokens,
        "calls": llm.call_count,
        "output": output,
    }


def _build_fanin_langgraph(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    if not LANGGRAPH_AVAILABLE:
        return _langgraph_unavailable()

    class State(TypedDict):
        input: str
        solution_a: str
        solution_b: str
        output: str

    def reasoner_a(s: State) -> dict[str, str]:
        return {
            "solution_a": llm.chat(
                "Solve the task using one strong, self-consistent line of reasoning.",
                f"Task: {s['input']}",
                agent_name="reasoner_a",
            )
        }

    def reasoner_b(s: State) -> dict[str, str]:
        return {
            "solution_b": llm.chat(
                "Solve the task using a different reasoning path or perspective from Reasoner A.",
                f"Task: {s['input']}",
                agent_name="reasoner_b",
            )
        }

    def aggregator(s: State) -> dict[str, str]:
        system = (
            _mc_prompt(
                "Compare both candidate solutions, resolve any disagreements, and provide the final correct answer."
            )
            if _is_mc(s["input"])
            else "Compare both candidate solutions, resolve any disagreements, and provide the final correct answer."
        )
        return {
            "output": llm.chat(
                system, f"Solution A:\n{s['solution_a']}\n\nSolution B:\n{s['solution_b']}", agent_name="aggregator"
            )
        }

    llm.reset()
    t0 = time.perf_counter()
    g = StateGraph(State)
    g.add_node("reasoner_a", reasoner_a)
    g.add_node("reasoner_b", reasoner_b)
    g.add_node("aggregator", aggregator)
    g.add_edge(START, "reasoner_a")
    g.add_edge(START, "reasoner_b")
    g.add_edge("reasoner_a", "aggregator")
    g.add_edge("reasoner_b", "aggregator")
    g.add_edge("aggregator", END)
    result = g.compile().invoke({"input": problem, "solution_a": "", "solution_b": "", "output": ""})
    elapsed = time.perf_counter() - t0
    output = result["output"]
    _log_framework_result("LangGraph", "fan_in", -1, output, elapsed, llm.total_tokens, llm.call_count)
    return {
        "framework": "langgraph",
        "time": elapsed,
        "tokens": llm.total_tokens,
        "calls": llm.call_count,
        "output": output,
    }


def _build_fanout_langgraph(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    if not LANGGRAPH_AVAILABLE:
        return _langgraph_unavailable()

    class State(TypedDict):
        input: str
        plan: str
        solution_a: str
        solution_b: str
        solution_c: str
        output: str

    def planner(s: State) -> dict[str, str]:
        return {
            "plan": llm.chat(
                "Analyze the task and propose three distinct ways to approach it.",
                f"Task: {s['input']}",
                agent_name="planner",
            )
        }

    def reasoner_a(s: State) -> dict[str, str]:
        return {
            "solution_a": llm.chat(
                "Solve the task using the first proposed approach.",
                f"Plan:\n{s['plan']}",
                agent_name="reasoner_a",
            )
        }

    def reasoner_b(s: State) -> dict[str, str]:
        return {
            "solution_b": llm.chat(
                "Solve the task using the second proposed approach.",
                f"Plan:\n{s['plan']}",
                agent_name="reasoner_b",
            )
        }

    def reasoner_c(s: State) -> dict[str, str]:
        return {
            "solution_c": llm.chat(
                "Solve the task using the third proposed approach or a substantially different perspective.",
                f"Plan:\n{s['plan']}",
                agent_name="reasoner_c",
            )
        }

    def synthesizer(s: State) -> dict[str, str]:
        system = (
            _mc_prompt("Compare all candidate solutions, resolve conflicts, and provide the final correct answer.")
            if _is_mc(s["input"])
            else "Compare all candidate solutions, resolve conflicts, and provide the final correct answer."
        )
        return {
            "output": llm.chat(
                system,
                f"Solution A:\n{s['solution_a']}\n\nSolution B:\n{s['solution_b']}\n\nSolution C:\n{s['solution_c']}",
                agent_name="synthesizer",
            )
        }

    llm.reset()
    t0 = time.perf_counter()
    g = StateGraph(State)
    for name, fn in [
        ("planner", planner),
        ("reasoner_a", reasoner_a),
        ("reasoner_b", reasoner_b),
        ("reasoner_c", reasoner_c),
        ("synthesizer", synthesizer),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "planner")
    for branch in ("reasoner_a", "reasoner_b", "reasoner_c"):
        g.add_edge("planner", branch)
        g.add_edge(branch, "synthesizer")
    g.add_edge("synthesizer", END)
    result = g.compile().invoke(
        {"input": problem, "plan": "", "solution_a": "", "solution_b": "", "solution_c": "", "output": ""}
    )
    elapsed = time.perf_counter() - t0
    output = result["output"]
    _log_framework_result("LangGraph", "fan_out", -1, output, elapsed, llm.total_tokens, llm.call_count)
    return {
        "framework": "langgraph",
        "time": elapsed,
        "tokens": llm.total_tokens,
        "calls": llm.call_count,
        "output": output,
    }


def _build_chain_n_langgraph(llm: TrackedLLM, problem: str, agents: list[AgentDef], test_label: str) -> dict[str, Any]:
    if not LANGGRAPH_AVAILABLE:
        return _langgraph_unavailable()

    max_tok = llm._cfg.default_max_tokens
    keys = ["input"] + [a[0] for a in agents]
    State = TypedDict("State", dict.fromkeys(keys, str))  # type: ignore[misc]
    prompts = {a[0]: a[3] for a in agents}
    last_id = agents[-1][0]

    def _make_node(agent_id: str, prev_key: str, out_key: str):
        def _node(s):
            user_msg = f"Task: {s['input']}" if prev_key == "input" else f"Previous step ({prev_key}):\n{s[prev_key]}"
            system = (
                _mc_prompt(prompts[agent_id]) if (agent_id == last_id and _is_mc(s["input"])) else prompts[agent_id]
            )
            return {out_key: llm.chat(system, user_msg, max_tokens=max_tok, agent_name=agent_id)}

        _node.__name__ = agent_id
        return _node

    llm.reset()
    t0 = time.perf_counter()
    g = StateGraph(State)
    agent_ids = [a[0] for a in agents]
    for aid, prev in zip(agent_ids, ["input", *agent_ids[:-1]], strict=False):
        g.add_node(aid, _make_node(aid, prev, aid))
    for i in range(len(agent_ids) - 1):
        g.add_edge(agent_ids[i], agent_ids[i + 1])
    g.set_entry_point(agent_ids[0])
    g.set_finish_point(agent_ids[-1])
    init = dict.fromkeys(keys, "")
    init["input"] = problem
    result = g.compile().invoke(init)
    elapsed = time.perf_counter() - t0
    output = result[last_id]
    _log_framework_result("LangGraph", test_label, -1, output, elapsed, llm.total_tokens, llm.call_count)
    return {
        "framework": "langgraph",
        "time": elapsed,
        "tokens": llm.total_tokens,
        "calls": llm.call_count,
        "output": output,
    }


def _build_chain7_langgraph(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    return _build_chain_n_langgraph(llm, problem, _CHAIN7_AGENTS, "chain_7")


def _build_chain15_langgraph(llm: TrackedLLM, problem: str) -> dict[str, Any]:
    return _build_chain_n_langgraph(llm, problem, _CHAIN15_AGENTS, "chain_15")


# ═══════════════════════════════════════════════════════════════════════════
# Test registry
# ═══════════════════════════════════════════════════════════════════════════

TESTS = [
    ("Test 1 — Single Agent", "single_agent", _build_single_langgraph, _build_single_gmas),
    ("Test 2 — Chain-3 (Analyzer->Solver->Formatter)", "chain_3", _build_chain_langgraph, _build_chain_gmas),
    ("Test 3 — Fan-in (ReasonerA+ReasonerB->Aggregator)", "fan_in", _build_fanin_langgraph, _build_fanin_gmas),
    ("Test 4 — Chain-7", "chain_7", _build_chain7_langgraph, _build_chain7_gmas),
    ("Test 5 — Fan-out (Planner->[A,B,C]->Synthesizer)", "fan_out", _build_fanout_langgraph, _build_fanout_gmas),
    ("Test 6 — Chain-15", "chain_15", _build_chain15_langgraph, _build_chain15_gmas),
]


# ═══════════════════════════════════════════════════════════════════════════
# Evaluation
# ═══════════════════════════════════════════════════════════════════════════


_MIN_SAMPLE_SIZE = 2
_ALPHABET_SIZE = 26


def _sample_std(scores: list[float]) -> float:
    n = len(scores)
    if n < _MIN_SAMPLE_SIZE:
        return 0.0
    mean = sum(scores) / n
    return math.sqrt(sum((v - mean) ** 2 for v in scores) / (n - 1))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _normalize_choice(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and 0 <= value < _ALPHABET_SIZE:
        return chr(ord("A") + value)
    text = str(value).strip().upper()
    m = re.search(r"([A-Z])", text)
    return m.group(1) if m else text or None


def _extract_choice(text: str) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)

    m = re.findall(
        r"(?:final\s+answer|answer|option|choice|correct\s+answer)\s*[:\-]?\s*\(?([A-Z])\)?",
        cleaned,
        flags=re.IGNORECASE,
    )
    if m:
        return m[-1].upper()

    upper = cleaned.upper().strip()
    for pat in [
        r"^\s*\(?([A-Z])\)?\s*[\.\)\]\}]*\s*$",
        r"\(?([A-Z])\)?\s*[\.\)\]\}]*\s*$",
        r"[\(\[\{]\s*([A-Z])\s*[\)\]\}]",
        r"\b([A-Z])\b",
    ]:
        mm = re.search(pat, upper)
        if mm:
            return mm.group(1)

    all_letters = re.findall(r"([A-Z])", upper)
    return all_letters[-1] if all_letters else None


def _extract_last_number(text: str) -> str | None:
    m = re.findall(r"-?\d+(?:\.\d+)?(?:/\d+)?", text.replace(",", ""))
    return m[-1] if m else None


def _extract_boxed(text: str) -> str | None:
    m = re.findall(r"\\boxed\{([^{}]+)\}", text)
    return m[-1].strip() if m else None


def _extract_hash(text: str) -> str | None:
    m = re.search(r"####\s*([^\n]+)", text)
    return m.group(1).strip() if m else None


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


def _run_code_in_process(code: str, timeout: float) -> tuple[bool, str]:
    """
    Execute *code* in a fresh Python subprocess with a hard timeout.

    Uses ``subprocess.Popen`` + a temp file instead of
    ``multiprocessing.Process`` so there is **no pickling** of closures
    — works reliably on every OS including Windows (spawn).

    Returns ``(passed, detail)`` where *passed* is True when the code
    exits with rc 0 and *detail* is stderr on failure.
    """
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        )
        tmp.write(code)
        tmp.flush()
        tmp.close()

        proc = subprocess.Popen(
            [sys.executable, "-u", tmp.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            return False, f"execution timed out after {timeout:.0f}s"

        if proc.returncode == 0:
            return True, ""
        return False, stderr.decode("utf-8", errors="replace")[:2000]
    except Exception as exc:
        return False, f"subprocess launch error: {exc}"
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                Path(tmp.name).unlink()


def _extract_code_block(output: str) -> str:
    """Extract the last fenced Python code block, or return raw output."""
    fenced = re.findall(r"```(?:python)?\s*\n(.*?)```", output, re.DOTALL | re.IGNORECASE)
    return fenced[-1].strip() if fenced else output.strip()


def _indent_body(prompt: str, body: str) -> str:
    """
    Ensure *body* has the indentation level expected by *prompt*.

    HumanEval prompts end with the function signature + docstring,
    so the body must be indented (typically 4 spaces).  If the LLM
    returned un-indented or differently-indented code, re-indent it
    to match the last indentation level of the prompt.
    """
    # Find the indentation of the last non-empty line in the prompt
    # (usually the closing triple-quote of the docstring).
    prompt_lines = prompt.rstrip().splitlines()
    expected_indent = "    "  # default
    for line in reversed(prompt_lines):
        stripped = line.lstrip()
        if stripped:
            expected_indent = line[: len(line) - len(stripped)]
            # The body should be at the same level as the docstring
            # content (one level deeper than `def`).
            break

    # If the body already starts with at least that much whitespace, keep it.
    body_lines = body.splitlines()
    if body_lines and body_lines[0].startswith(expected_indent):
        return body

    # Otherwise, dedent fully and re-indent.
    dedented = textwrap.dedent(body)
    return textwrap.indent(dedented, expected_indent)


def _execute_humaneval(
    output: str,
    prompt: str,
    test_code: str,
    entry_point: str,
    timeout: float,
) -> tuple[bool, str | None]:
    """
    Run pass@1 evaluation for a single HumanEval sample.

    1. Extract the generated code from the LLM output.
    2. Compose: prompt (function signature) + generated body + test assertions.
    3. Execute in a sandboxed subprocess with a hard timeout.

    Returns ``(is_correct, extracted_code)``.
    """
    generated = _extract_code_block(output)

    # The LLM might return the full function (including signature) or just
    # the body.  Detect which case we're in and assemble accordingly.
    if f"def {entry_point}" in generated:
        # Full function — use as-is.
        full_code = generated
    else:
        # Body only — re-indent to match the prompt and concatenate.
        full_code = prompt.rstrip("\n") + "\n" + _indent_body(prompt, generated)

    # Append the test harness from the dataset.
    full_code = full_code + "\n\n" + test_code + f"\n\ncheck({entry_point})\n"

    passed, detail = _run_code_in_process(full_code, timeout)
    if not passed:
        _log(f"  [EXEC] HumanEval FAIL: {detail[:300]}")
    return passed, generated


def _evaluate_output(sample: dict[str, Any], output: str) -> dict[str, Any]:
    metric = sample.get("metric_name")
    reference = sample.get("reference")
    if metric is None or (reference is None and metric != "code_exec"):
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

    if metric == "choice":
        prediction = _extract_choice(output)
        ref_norm = _normalize_choice(reference)
        is_correct = prediction is not None and prediction == ref_norm
        if prediction is None:
            _log(f"  [EVAL] Failed to extract choice. sample={idx}")
        elif is_correct:
            _log(f"  [EVAL] ✓ Correct choice={prediction} sample={idx}")
        else:
            _log(f"  [EVAL] Choice mismatch sample={idx} predicted={prediction} expected={ref_norm}")

    elif metric == "numeric":
        prediction = _extract_boxed(output) or _extract_hash(output) or _extract_last_number(output)
        is_correct = _numbers_equal(prediction, str(reference))
        _log(f"  [EVAL] numeric sample={idx} predicted={prediction} expected={reference} correct={is_correct}")

    elif metric == "math_exact":
        prediction = _extract_boxed(output) or _last_line(output)
        is_correct = _normalize_text(prediction or "") == _normalize_text(str(reference))
        _log(f"  [EVAL] math_exact sample={idx} correct={is_correct}")

    elif metric == "exact_text":
        prediction = _last_line(output)
        ref_n = _normalize_text(str(reference))
        is_correct = _normalize_text(prediction) == ref_n or ref_n in _normalize_text(output)
        _log(f"  [EVAL] exact_text sample={idx} correct={is_correct}")

    elif metric == "code_exec":
        test_code = sample.get("test_code", "")
        entry_point = sample.get("entry_point", "")
        prompt = sample.get("problem", "")
        exec_timeout = sample.get("_exec_timeout", 10.0)
        if test_code and entry_point:
            is_correct, prediction = _execute_humaneval(
                output,
                prompt,
                test_code,
                entry_point,
                exec_timeout,
            )
            _log(f"  [EVAL] code_exec (pass@1) sample={idx} correct={is_correct}")
        else:
            prediction = _extract_code_block(output)
            is_correct = False
            _log(f"  [EVAL] code_exec sample={idx} — no test harness, marking incorrect")

    else:
        prediction = _last_line(output)
        is_correct = _normalize_text(prediction) == _normalize_text(str(reference))
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


def _load_dataset_samples(dataset_name: str) -> list[dict[str, Any]]:
    _log(f"\n  Loading dataset: {dataset_name} ...")

    if dataset_name == "mmlu-pro":
        dataset = load_dataset("TIGER-Lab/MMLU-Pro", split="test")
        samples = []
        for i, item in enumerate(dataset):
            question = item.get("question", "")
            options = item.get("options", [])
            if options:
                choices_str = "\n".join(f"{chr(65 + j)}. {c}" for j, c in enumerate(options))
                problem = f"{question}\n\nChoices:\n{choices_str}"
            else:
                problem = question
            samples.append(
                {
                    "problem": problem,
                    "dataset": dataset_name,
                    "index": i,
                    "reference": _normalize_choice(item.get("answer")),
                    "metric_name": "choice",
                }
            )
        _log(f"  ✓ Loaded {len(samples)} samples from TIGER-Lab/MMLU-Pro")
        return samples

    if dataset_name == "gsm8k":
        dataset = load_dataset("openai/gsm8k", "main", split="test")
        samples = []
        for i, item in enumerate(dataset):
            samples.append(
                {
                    "problem": item.get("question", ""),
                    "dataset": dataset_name,
                    "index": i,
                    "reference": _extract_hash(item.get("answer", "")),
                    "metric_name": "numeric",
                }
            )
        _log(f"  ✓ Loaded {len(samples)} samples from openai/gsm8k")
        return samples

    if dataset_name == "bigbench":
        bb_tasks = [
            "abstract_narrative_understanding",
            "anachronisms",
            "analogical_similarity",
            "analytic_entailment",
            "cause_and_effect",
            "code_line_description",
            "conceptual_combinations",
            "conlang_translation",
            "date_understanding",
            "disambiguation_qa",
            "elementary_math_qa",
            "emoji_movie",
            "english_proverbs",
            "figure_of_speech_detection",
            "formal_fallacies_syllogisms_negation",
            "general_knowledge",
            "geometric_shapes",
            "goal_step_wikihow",
            "hyperbaton",
            "implicatures",
            "known_unknowns",
            "language_identification",
            "linguistics_puzzles",
            "logic_grid_puzzle",
            "logical_deduction",
            "movie_recommendation",
            "navigate",
            "nonsense_words_grammar",
            "novel_concepts",
            "odd_one_out",
            "operators",
            "penguins_in_a_table",
            "phrase_relatedness",
            "physical_intuition",
            "physics",
            "play_dialog_same_or_different",
            "presuppositions_as_nli",
            "reasoning_about_colored_objects",
            "ruin_names",
            "salient_translation_error_detection",
            "snarks",
            "sports_understanding",
            "strange_stories",
            "strategyqa",
            "symbol_interpretation",
            "temporal_sequences",
            "understanding_fables",
            "undo_permutation",
            "vitaminc_fact_verification",
            "winowhy",
        ]
        samples: list[dict[str, Any]] = []
        for task in bb_tasks:
            _log(f"    loading BigBench task: {task} ...")
            for item in load_dataset("tasksource/bigbench", task, split="train"):
                targets = item.get("targets", [])
                ref = targets[0] if isinstance(targets, list) and targets else item.get("target")
                samples.append(
                    {
                        "problem": item.get("inputs", item.get("input", "")),
                        "dataset": dataset_name,
                        "index": len(samples),
                        "reference": ref,
                        "metric_name": "exact_text" if ref else None,
                    }
                )
        _log(f"  ✓ Loaded {len(samples)} samples from tasksource/bigbench ({len(bb_tasks)} tasks)")
        return samples

    if dataset_name == "gpqa":
        dataset = load_dataset("Idavidrein/gpqa", "gpqa_main", split="train")
        samples = []
        for i, item in enumerate(dataset):
            correct = item.get("Correct Answer", "")
            options = [
                correct,
                item.get("Incorrect Answer 1", ""),
                item.get("Incorrect Answer 2", ""),
                item.get("Incorrect Answer 3", ""),
            ]
            rng = random.Random(i)
            rng.shuffle(options)
            correct_letter = chr(65 + options.index(correct))
            choices_str = "\n".join(f"{chr(65 + j)}. {c}" for j, c in enumerate(options))
            samples.append(
                {
                    "problem": f"{item.get('Question', '')}\n\nChoices:\n{choices_str}",
                    "dataset": dataset_name,
                    "index": i,
                    "reference": correct_letter,
                    "metric_name": "choice",
                }
            )
        _log(f"  ✓ Loaded {len(samples)} samples from Idavidrein/gpqa (gpqa_main)")
        return samples

    if dataset_name == "human-eval":
        dataset = load_dataset("openai/openai_humaneval", split="test")
        samples = []
        for i, item in enumerate(dataset):
            prompt = item.get("prompt") or item.get("instruction") or ""
            test_code = item.get("test", "")
            entry_point = item.get("entry_point", "")
            samples.append(
                {
                    "problem": prompt,
                    "dataset": dataset_name,
                    "index": i,
                    "reference": item.get("canonical_solution"),
                    "test_code": test_code,
                    "entry_point": entry_point,
                    "metric_name": "code_exec" if test_code else None,
                }
            )
        _log(f"  ✓ Loaded {len(samples)} samples from openai/openai_humaneval (pass@1)")
        return samples

    if dataset_name == "big-bench-hard":
        bbh_tasks = [
            "boolean_expressions",
            "causal_judgment",
            "date_understanding",
            "disambiguation_qa",
            "geometric_shapes",
            "logical_deduction_five_objects",
            "logical_deduction_seven_objects",
            "logical_deduction_three_objects",
            "movie_recommendation",
            "navigate",
            "reasoning_about_colored_objects",
            "ruin_names",
            "salient_translation_error_detection",
            "snarks",
            "sports_understanding",
            "temporal_sequences",
            "tracking_shuffled_objects_five_objects",
            "tracking_shuffled_objects_seven_objects",
            "tracking_shuffled_objects_three_objects",
            "web_of_lies",
            "word_sorting",
            "multistep_arithmetic_two",
            "object_counting",
            "formal_fallacies",
        ]
        samples: list[dict[str, Any]] = []
        for task in bbh_tasks:
            _log(f"    loading BBH task: {task} ...")
            for item in load_dataset("lukaemon/bbh", task, split="test"):
                ref = item.get("target", item.get("answer"))
                samples.append(
                    {
                        "problem": item.get("input", item.get("question", "")),
                        "dataset": dataset_name,
                        "index": len(samples),
                        "reference": ref,
                        "metric_name": "exact_text" if ref else None,
                    }
                )
        _log(f"  ✓ Loaded {len(samples)} samples from lukaemon/bbh ({len(bbh_tasks)} tasks)")
        return samples

    msg = f"Unknown dataset: {dataset_name}"
    raise ValueError(msg)


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
                alias, tk, ds, si = r.get("model_alias"), r.get("test"), r.get("dataset"), r.get("sample_idx")
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
    out_path = log_dir / f"benchmark_{spec.alias}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
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
            thread_name_prefix=f"bench-{spec.alias}",
        )
        _EXECUTORS[spec.alias] = ex
    return ex


def _make_llm(spec: ModelSpec, cfg: BenchmarkConfig, framework: str = "unknown") -> TrackedLLM:
    return TrackedLLM(spec, cfg, framework=framework)


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
        llm = _make_llm(spec, cfg, framework=framework_name)
        _log(f"\n  ━━ Running {framework_name} | test={test_key} | sample={sample['index']}")
        result = fn(llm, sample["problem"])
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
        sample_with_cfg = {**sample, "_exec_timeout": cfg.code_exec_timeout}
        result.update(_evaluate_output(sample_with_cfg, output))
        return result

    try:
        return await asyncio.wait_for(loop.run_in_executor(executor, _invoke), timeout=timeout_sec)
    except TimeoutError:
        return _build_error_result(
            framework_name, spec, sample, test_key, dataset, run_idx, f"timeout>{timeout_sec:.0f}s"
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
                f"| LG: {'✓' if lg_r.get('is_correct') else '✗'} pred={lg_r.get('prediction', '?')} "
                f"| gMAS: {'✓' if gm_r.get('is_correct') else '✗'} pred={gm_r.get('prediction', '?')} "
                f"| ref={sample.get('reference', '?')}"
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


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


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


def _print_summary(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    def _out(line: str = "") -> None:
        """Write to both log file and stdout (ASCII-safe)."""
        _log(line)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", errors="replace").decode("ascii"))

    bar = "-" * 120
    _out(f"\n{bar}")
    _out("  Summary  (acc = mean +/- sample-std across N problems)")
    _out(bar)
    hdr = (
        f"  {'Test':<32} {'N':>5}  {'LG t':>7}  {'gMAS t':>7}  "
        f"{'LG tok':>8}  {'gMAS tok':>8}  {'LG acc':>14}  {'gMAS acc':>15}  "
        f"{'LG err':>6}  {'gM err':>6}  {'Winner':>10}"
    )
    _out(hdr)
    _out(
        f"  {'-' * 32} {'-' * 5}  {'-' * 7}  {'-' * 7}  {'-' * 8}  {'-' * 8}  "
        f"{'-' * 14}  {'-' * 15}  {'-' * 6}  {'-' * 6}  {'-' * 10}"
    )

    for row in rows:
        winner = "gMAS" if row["gm_time"] < row["lg_time"] else "LangGraph"
        lg_err = int(row.get("lg_errors", 0))
        gm_err = int(row.get("gm_errors", 0))
        _out(
            f"  {row['summary_key']:<32} {round(row['paired_samples']):>5}  "
            f"{row['lg_time']:>6.2f}s  {row['gm_time']:>6.2f}s  "
            f"{row['lg_tok']:>8.0f}  {row['gm_tok']:>8.0f}  "
            f"{_fmt_acc(row['lg_acc'], row.get('lg_acc_std', 0.0)):>14}  "
            f"{_fmt_acc(row['gm_acc'], row.get('gm_acc_std', 0.0)):>15}  "
            f"{lg_err:>6}  {gm_err:>6}  {winner:>10}"
        )

    dt = sum((r["lg_time"] - r["gm_time"]) / max(r["lg_time"], 1e-9) for r in rows) / len(rows) * 100
    dtok = sum((r["lg_tok"] - r["gm_tok"]) / max(r["lg_tok"], 1) for r in rows) / len(rows) * 100
    total_lg_err = sum(int(r.get("lg_errors", 0)) for r in rows)
    total_gm_err = sum(int(r.get("gm_errors", 0)) for r in rows)
    _out(f"\n  Average gMAS improvement -- time: {dt:+.1f}%  tokens: {dtok:+.1f}%")
    _out(f"  Total errors -- LangGraph: {total_lg_err}  gMAS: {total_gm_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Connection check
# ═══════════════════════════════════════════════════════════════════════════


def _connect_model(spec: ModelSpec, cfg: BenchmarkConfig) -> None:
    _log(f"\n  [*] Testing connection: {spec.alias} ({spec.model} @ {spec.base_url})")
    if cfg.skip_ping:
        _log("  [!] --skip-ping: connection check skipped")
        print("  [!] --skip-ping: connection check skipped")
        return

    llm = _make_llm(spec, cfg, framework="ping")
    delay = cfg.ping_delay
    last_exc: Exception | None = None

    for attempt in range(cfg.ping_retries):
        try:
            ping = llm.chat(
                "Return plain text only. No reasoning. No extra words.",
                "Reply with exactly OK",
                max_tokens=cfg.ping_max_tokens,
                agent_name="ping",
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
    run_idx: int,
    all_results: list[dict[str, Any]],
    completed_tests: dict[str, set[str]],
    checkpoint_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    new_results: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for title, test_key, lg_fn, gmas_fn in TESTS:
        _header(f"{title} | {dataset} | run {run_idx}")
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
                    total=len(problems), initial=already, desc=f"  run{run_idx}:{test_key}", unit="exp", leave=False
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
    Run the full benchmark suite.

    Args:
        cfg: A pre-built ``BenchmarkConfig``.  If *None*, one is created from
             ``**kwargs`` — so you can call either::

                 run_benchmark(cfg=BenchmarkConfig(temperature=0.3))
                 run_benchmark(temperature=0.3, max_parallel=20)
        **kwargs: Keyword arguments forwarded to ``BenchmarkConfig`` when
                  ``cfg`` is *None*.

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

        _header("Benchmark: gMAS vs LangGraph (local vLLM)")
        _log(f"  Model        : {spec.model}")
        _log(f"  Base URL     : {spec.base_url}")
        _log(f"  Max parallel : {spec.max_parallel}")
        _log(f"  Runs         : {cfg.num_runs}")
        _log(f"  Timeout/task : {cfg.framework_timeout:.0f}s")
        _log(f"  Temperature  : {cfg.temperature}")
        _log(f"  LLM timeout  : {cfg.llm_timeout:.0f}s")
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

        if cfg.datasets_filter:
            unknown = [d for d in cfg.datasets_filter if d not in _ALL_DATASETS]
            if unknown:
                print(f"  WARNING: unknown datasets ignored: {unknown}", file=sys.stderr)
            _log(f"  Datasets     : {datasets}")

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
            _print_summary(final_rows)
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
    parser = argparse.ArgumentParser(description="Benchmark gMAS vs LangGraph (local vLLM edition)")

    # Run control
    parser.add_argument("--runs", type=int, default=1, metavar="N", help="Number of full benchmark passes (default: 1)")
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
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, ignore existing checkpoint")
    parser.add_argument("--skip-ping", action="store_true", help="Skip the initial connection check")
    parser.add_argument(
        "--datasets",
        type=str,
        default=None,
        metavar="DS1,DS2",
        help=f"Comma-separated list of datasets (default: all). Available: {', '.join(_ALL_DATASETS)}",
    )
    parser.add_argument(
        "--max-samples", type=int, default=None, metavar="N", help="Limit number of samples per dataset (default: all)"
    )

    # LLM parameters
    parser.add_argument(
        "--temperature",
        type=float,
        default=_DEFAULT_CFG.temperature,
        help=f"LLM sampling temperature (default: {_DEFAULT_CFG.temperature})",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=_DEFAULT_CFG.llm_timeout,
        help=f"LLM request timeout in seconds (default: {_DEFAULT_CFG.llm_timeout:.0f})",
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

    # Model identity
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
        max_samples=args.max_samples,
        temperature=args.temperature,
        llm_timeout=args.llm_timeout,
        default_max_tokens=args.max_tokens,
        runner_timeout=args.runner_timeout,
        model_alias=args.alias,
        log_dir=Path(args.log_dir),
    )

    run_benchmark(cfg)
