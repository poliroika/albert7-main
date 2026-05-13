"""
gMAS vs gMAS Benchmark — 6 A/B tests comparing framework features
on BIG-Bench Hard (BBH) tasks from ``lukaemon/bbh``.

Tests:
  1. Early Stopping On vs Off
  2. Disabled Nodes On vs Off
  3. Filter Unreachable On vs Off
  4. Adaptive Topology vs Static Topology
  5. Hidden Channels On vs Off
  6. Multi-Model (strong+weak) vs Single-Model (weak only)

Each test runs a multi-agent reasoning pipeline on real BBH samples and
compares accuracy, latency, and token usage between variants.
"""

import argparse
import contextlib
import json
import os
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI
from scipy.stats import wilcoxon

from gmas.builder import BuilderConfig, GraphBuilder
from gmas.core import NodeEncoder
from gmas.execution import EarlyStopCondition, MACPRunner, RunnerConfig, TopologyAction

try:
    from datasets import load_dataset
except ImportError:
    print("ERROR: 'datasets' library is required. Install: pip install datasets", file=sys.stderr)
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════


def _load_env_file(path: Path) -> None:
    """Read a .env file into ``os.environ`` (existing vars are NOT overwritten)."""
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


_load_env_file(Path(__file__).resolve().parent / ".env")
_load_env_file(Path(__file__).resolve().parents[1] / ".env")


def _env(name: str, default: str = "") -> str:
    """Read ``BENCH_<name>`` from the environment."""
    return os.environ.get(f"BENCH_{name}", default)


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _env_float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


# ═══════════════════════════════════════════════════════════════════════════
# BBH Dataset
# ═══════════════════════════════════════════════════════════════════════════

# All 23 BIG-Bench Hard tasks from lukaemon/bbh
BBH_ALL_TASKS = [
    "boolean_expressions",
    "causal_judgment",
    "date_understanding",
    "disambiguation_qa",
    "formal_fallacies",
    "geometric_shapes",
    "logical_deduction_five_objects",
    "logical_deduction_seven_objects",
    "logical_deduction_three_objects",
    "movie_recommendation",
    "multistep_arithmetic_two",
    "navigate",
    "object_counting",
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
]

# Default: run ALL tasks.  Pass --bbh-tasks to restrict.
BBH_DEFAULT_TASKS = BBH_ALL_TASKS


@dataclass
class BBHSample:
    """A single BIG-Bench Hard sample with question and reference answer."""

    task: str
    question: str
    reference: str
    index: int


def load_bbh_samples(
    tasks: list[str] | None = None,
    max_samples: int | None = None,
    seed: int = 42,
) -> list[BBHSample]:
    """
    Load BBH samples from HuggingFace ``lukaemon/bbh``.

    Args:
        tasks: BBH task names to load (default: all 23 tasks).
        max_samples: If set, randomly subsample to at most this many examples.
                     ``None`` (default) loads the entire dataset.
        seed: Random seed for reproducible subsampling.

    """
    tasks = tasks or BBH_DEFAULT_TASKS
    all_samples: list[BBHSample] = []

    for task in tasks:
        print(f"  Loading BBH task: {task} ...")
        ds = load_dataset("lukaemon/bbh", task, split="test")

        for item in ds:
            ref = item.get("target", item.get("answer", ""))
            question = item.get("input", item.get("question", ""))
            if question and ref:
                all_samples.append(
                    BBHSample(
                        task=task,
                        question=question.strip(),
                        reference=str(ref).strip(),
                        index=len(all_samples),
                    )
                )

    # Shuffle for fair distribution across tasks
    rng = random.Random(seed)
    rng.shuffle(all_samples)

    # Subsample only if explicitly requested
    selected = all_samples[:max_samples] if max_samples is not None and max_samples < len(all_samples) else all_samples

    # Re-index
    for i, s in enumerate(selected):
        s.index = i

    total = len(all_samples)
    used = len(selected)
    label = "ALL" if max_samples is None else f"{used}/{total}"
    print(f"  [OK] Loaded {label} ({used}) BBH samples from {len(tasks)} tasks")
    return selected


# ═══════════════════════════════════════════════════════════════════════════
# Token tracker & LLM client
# ═══════════════════════════════════════════════════════════════════════════


class TokenTracker:
    """Accumulates token usage across LLM invocations."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.total_tokens: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.call_count: int = 0
        self.per_call: list[dict[str, Any]] = []

    def record(self, usage: Any, latency_s: float) -> None:
        t = usage.total_tokens if usage and usage.total_tokens else 0
        p = usage.prompt_tokens if usage and usage.prompt_tokens else 0
        c = usage.completion_tokens if usage and usage.completion_tokens else 0

        self.total_tokens += t
        self.prompt_tokens += p
        self.completion_tokens += c
        self.call_count += 1
        self.per_call.append({"total": t, "prompt": p, "completion": c, "latency_s": latency_s})


class BenchLLM:
    """
    Thin OpenAI-compatible LLM client with token tracking.

    Parameters fall back to ``BENCH_*`` env vars when not supplied explicitly.
    """

    def __init__(
        self,
        *,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.model = model or _env("MODEL")
        self.temperature = temperature if temperature is not None else _env_float("TEMPERATURE", 0.7)
        self.max_tokens = max_tokens if max_tokens is not None else _env_int("MAX_TOKENS", 4096)
        self.client = OpenAI(
            base_url=base_url or _env("BASE_URL"),
            api_key=api_key or _env("API_KEY"),
            timeout=timeout if timeout is not None else _env_float("TIMEOUT", 120),
        )
        self.tracker = TokenTracker()

    def call(self, system: str, user: str) -> str:
        start = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        elapsed = time.perf_counter() - start
        self.tracker.record(resp.usage, elapsed)
        return resp.choices[0].message.content or ""

    def ping(self) -> bool:
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Reply OK"}],
                max_tokens=5,
            )
            return bool(resp.usage is not None and resp.usage.total_tokens > 0)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Result models
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RunResult:
    """Result of a single benchmark run for one variant."""

    experiment: str
    variant: str
    run_idx: int
    latency_ms: float
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int
    output: str
    agents_executed: int = 0
    early_stopped: bool = False
    error: str | None = None
    correct: bool = False
    reference: str = ""
    question: str = ""


@dataclass
class VariantStats:
    """Aggregated statistics for all runs of a single variant."""

    experiment: str
    variant: str
    n_runs: int
    n_correct: int
    n_errors: int
    accuracy: float
    acc_std: float
    lat_avg: float
    lat_std: float
    lat_median: float
    lat_p95: float
    tok_avg: float
    tok_std: float
    prompt_tok_avg: float
    compl_tok_avg: float
    calls_avg: float
    calls_std: float
    agents_avg: float
    agents_std: float


# ═══════════════════════════════════════════════════════════════════════════
# Agent topology — chain of 5 reasoning agents
# ═══════════════════════════════════════════════════════════════════════════

AGENT_ROLES = [
    ("decomposer", "Decomposer", "Break the problem into logical sub-steps."),
    ("reasoner", "Reasoner", "Apply step-by-step reasoning to each sub-step."),
    ("verifier", "Verifier", "Verify the reasoning and check for errors."),
    ("synthesizer", "Synthesizer", "Combine verified reasoning into a coherent answer."),
    ("formatter", "Formatter", "Extract and format the final answer."),
]

AGENT_IDS = [r[0] for r in AGENT_ROLES]
CHAIN_EDGES = [(AGENT_IDS[i], AGENT_IDS[i + 1]) for i in range(len(AGENT_IDS) - 1)]

# Disabled nodes for Test 2
DISABLED_NODES = {"verifier", "synthesizer"}

# Isolated nodes for Test 3
ISOLATED_NODES = [
    {"id": "orphan_1", "name": "Orphan 1", "desc": "Isolated agent 1"},
    {"id": "orphan_2", "name": "Orphan 2", "desc": "Isolated agent 2"},
]

# Test 6: multi-model routing
STRONG_AGENTS = {"decomposer", "reasoner"}
WEAK_AGENTS = {"verifier", "synthesizer", "formatter"}

SYSTEM_PROMPTS: dict[str, str] = {
    "decomposer": (
        "You are a problem decomposer. Break the given problem into clear logical sub-steps. "
        "Output a numbered list of sub-steps."
    ),
    "reasoner": (
        "You are a step-by-step reasoner. For each sub-step provided, apply careful logical "
        "reasoning. Show your work clearly."
    ),
    "verifier": (
        "You are a verification agent. Check the reasoning for logical errors, missed cases, "
        "or incorrect assumptions. Point out any issues."
    ),
    "synthesizer": (
        "You are a synthesizer. Combine all verified reasoning steps into a single coherent answer. Be concise."
    ),
    "formatter": (
        "You are a final answer formatter. Extract the final answer from the reasoning. "
        "Output ONLY the final answer, nothing else. No explanation."
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# Graph builders
# ═══════════════════════════════════════════════════════════════════════════


def _build_chain_graph(
    query: str,
    *,
    disabled_nodes: set[str] | None = None,
    isolated_nodes: list[dict] | None = None,
) -> Any:
    """Build a chain topology graph for a given BBH question."""
    builder = GraphBuilder(config=BuilderConfig(include_task_node=True, validate=True))
    builder.add_task("__task__", query=query, description="BBH reasoning task")

    for aid, name, desc in AGENT_ROLES:
        builder.add_agent(aid, name, aid, desc)

    builder.connect_task_to_agents(agent_ids=[AGENT_IDS[0]], bidirectional=False)

    for src, dst in CHAIN_EDGES:
        builder.add_workflow_edge(src, dst)

    if isolated_nodes:
        for node in isolated_nodes:
            builder.add_agent(node["id"], node["name"], node["id"], node["desc"])

    graph = builder.build()

    if disabled_nodes:
        for node_id in disabled_nodes:
            graph.disabled_nodes.add(node_id)

    return graph


def _build_parallel_graph(query: str) -> Any:
    """Build a parallel topology for hidden-channels test."""
    builder = GraphBuilder(config=BuilderConfig(include_task_node=True, validate=True))
    builder.add_task("__task__", query=query, description="BBH reasoning task")

    # Three parallel reasoners + synthesizer + formatter
    parallel_agents = [
        ("logical_reasoner", "Logical Reasoner", "logical_reasoner", "Apply formal logical reasoning."),
        (
            "intuitive_reasoner",
            "Intuitive Reasoner",
            "intuitive_reasoner",
            "Apply intuitive / common-sense reasoning.",
        ),
        (
            "analytical_reasoner",
            "Analytical Reasoner",
            "analytical_reasoner",
            "Apply analytical / mathematical reasoning.",
        ),
    ]
    parallel_ids = [a[0] for a in parallel_agents]

    for aid, name, persona, desc in parallel_agents:
        builder.add_agent(aid, name, persona, desc)

    builder.add_agent("synthesizer", "Synthesizer", "synthesizer", "Combine reasoning from all agents.")
    builder.add_agent("formatter", "Formatter", "formatter", "Extract the final answer.")

    builder.connect_task_to_agents(agent_ids=parallel_ids, bidirectional=False)

    for pid in parallel_ids:
        builder.add_workflow_edge(pid, "synthesizer")
    builder.add_workflow_edge("synthesizer", "formatter")

    return builder.build()


_CACHED_ENCODER: NodeEncoder | None = None


def _get_encoder() -> NodeEncoder:
    """Return a cached NodeEncoder to avoid reloading the model on every sample."""
    global _CACHED_ENCODER
    if _CACHED_ENCODER is None:
        _CACHED_ENCODER = NodeEncoder(model_name=_env("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"))
    return _CACHED_ENCODER


def _attach_embeddings(graph: Any) -> Any:
    """Encode agent role texts and attach embeddings (uses cached encoder)."""
    encoder = _get_encoder()

    texts = []
    for agent_id in graph.node_ids:
        agent = next((a for a in graph.agents if getattr(a, "agent_id", None) == agent_id), None)
        texts.append(agent.to_text() if agent and hasattr(agent, "to_text") else agent_id)

    embeddings = encoder.encode(texts)

    for i, agent_id in enumerate(graph.node_ids):
        for j, a in enumerate(graph.agents):
            if getattr(a, "agent_id", None) == agent_id:
                graph.agents[j] = a.with_embedding(embeddings[i])
                break

    return graph


# ═══════════════════════════════════════════════════════════════════════════
# Accuracy scoring (aligned with benchmark_vs_langgraph.py evaluation)
# ═══════════════════════════════════════════════════════════════════════════


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _last_line(text: str) -> str:
    """Extract the last non-empty line (models often put the answer there)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else text.strip()


def check_accuracy(output: str, reference: str) -> bool:
    """
    Check if the model output matches the BBH reference answer.

    Uses the same ``exact_text`` metric as the langgraph benchmark:
    1. Normalize both strings (lowercase, collapse whitespace).
    2. Check if the last line of the output matches the reference exactly.
    3. Fall back to containment check (reference found anywhere in output).
    """
    if not output or not reference:
        return False

    ref_n = _normalize_text(reference)
    out_n = _normalize_text(output)

    # Last-line exact match (most reliable for BBH)
    prediction = _normalize_text(_last_line(output))
    if prediction == ref_n:
        return True

    # Containment: reference appears somewhere in the full output
    return ref_n in out_n


# ═══════════════════════════════════════════════════════════════════════════
# Statistics helpers
# ═══════════════════════════════════════════════════════════════════════════


def _wilcoxon_paired(a: list[float], b: list[float], p_threshold: float = 0.05) -> dict:
    """
    Wilcoxon signed-rank test for paired continuous data (latency / tokens).

    Falls back gracefully when all differences are zero (identical pairs).
    """
    diffs = [ai - bi for ai, bi in zip(a, b, strict=False)]
    # If all diffs are zero, there's no difference to test
    if all(d == 0 for d in diffs):
        return {"statistic": 0.0, "p_value": 1.0, "significant": False, "test": "wilcoxon"}
    try:
        stat, p = wilcoxon(diffs, alternative="two-sided")
    except ValueError:
        # e.g. too few non-zero differences
        return {"statistic": 0.0, "p_value": 1.0, "significant": False, "test": "wilcoxon"}
    return {"statistic": float(stat), "p_value": float(p), "significant": bool(p < p_threshold), "test": "wilcoxon"}


def _mcnemar_test(a_correct: list[bool], b_correct: list[bool], p_threshold: float = 0.05) -> dict:
    """
    McNemar test for paired binary accuracy data.

    Compares discordant pairs: cases where A got it right but B didn't, and vice versa.
    Uses exact binomial test when discordant count < 25, chi-square otherwise.
    """
    # Count discordant pairs
    b_only = sum(1 for ac, bc in zip(a_correct, b_correct, strict=False) if not ac and bc)  # B right, A wrong
    a_only = sum(1 for ac, bc in zip(a_correct, b_correct, strict=False) if ac and not bc)  # A right, B wrong

    n_discordant = a_only + b_only
    if n_discordant == 0:
        return {
            "a_only": a_only,
            "b_only": b_only,
            "p_value": 1.0,
            "significant": False,
            "test": "mcnemar",
        }

    # Exact binomial test for small samples, chi-square for large
    if n_discordant < 25:
        try:
            from scipy.stats import binomtest  # type: ignore[attr-defined]  # scipy >= 1.7

            result = binomtest(a_only, n_discordant, 0.5, alternative="two-sided")
            p = result.pvalue
        except ImportError:
            # scipy < 1.7 fallback
            from scipy.stats import binom_test  # type: ignore[attr-defined]

            p = binom_test(a_only, n_discordant, 0.5)
        return {
            "a_only": a_only,
            "b_only": b_only,
            "p_value": float(p),
            "significant": bool(p < p_threshold),
            "test": "mcnemar_exact",
        }

    # Chi-square approximation for larger samples
    chi2 = (abs(a_only - b_only) - 1) ** 2 / n_discordant  # continuity correction
    from scipy.stats import chi2 as chi2_dist

    p = 1 - chi2_dist.cdf(chi2, df=1)
    return {
        "a_only": a_only,
        "b_only": b_only,
        "chi2": float(chi2),
        "p_value": float(p),
        "significant": bool(p < p_threshold),
        "test": "mcnemar_chi2",
    }


def compute_stats(results: list[RunResult]) -> VariantStats:
    if not results:
        msg = "compute_stats requires at least one RunResult"
        raise ValueError(msg)

    ok = [r for r in results if r.error is None]
    n_errors = len(results) - len(ok)
    n_correct = sum(1 for r in ok if r.correct)

    if not ok:
        return VariantStats(
            experiment=results[0].experiment,
            variant=results[0].variant,
            n_runs=0,
            n_correct=0,
            n_errors=n_errors,
            accuracy=0.0,
            acc_std=0.0,
            lat_avg=0,
            lat_std=0,
            lat_median=0,
            lat_p95=0,
            tok_avg=0,
            tok_std=0,
            prompt_tok_avg=0,
            compl_tok_avg=0,
            calls_avg=0,
            calls_std=0,
            agents_avg=0,
            agents_std=0,
        )

    lats = [r.latency_ms for r in ok]
    toks = [float(r.total_tokens) for r in ok]
    ptoks = [float(r.prompt_tokens) for r in ok]
    ctoks = [float(r.completion_tokens) for r in ok]
    calls = [float(r.llm_calls) for r in ok]
    agents = [float(r.agents_executed) for r in ok]
    correct_flags = [float(r.correct) for r in ok]
    sl = sorted(lats)

    accuracy = n_correct / len(ok) if ok else 0.0
    _std = statistics.stdev if len(ok) > 1 else lambda _: 0.0

    return VariantStats(
        experiment=ok[0].experiment,
        variant=ok[0].variant,
        n_runs=len(ok),
        n_correct=n_correct,
        n_errors=n_errors,
        accuracy=accuracy,
        acc_std=_std(correct_flags),
        lat_avg=statistics.mean(lats),
        lat_std=_std(lats),
        lat_median=statistics.median(lats),
        lat_p95=sl[min(int(len(sl) * 0.95), len(sl) - 1)],
        tok_avg=statistics.mean(toks),
        tok_std=_std(toks),
        prompt_tok_avg=statistics.mean(ptoks),
        compl_tok_avg=statistics.mean(ctoks),
        calls_avg=statistics.mean(calls),
        calls_std=_std(calls),
        agents_avg=statistics.mean(agents),
        agents_std=_std(agents),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Runner helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_runner(
    llm: BenchLLM,
    *,
    adaptive: bool = False,
    enable_hidden: bool = False,
    early_stop_fn: Any | None = None,
    enable_dynamic_topology: bool = False,
    topology_hooks: list | None = None,
) -> MACPRunner:
    """Build a MACPRunner wired to *llm* with the given feature flags."""
    runner_config = RunnerConfig(
        timeout=_env_float("TIMEOUT", 120),
        adaptive=adaptive,
        enable_parallel=True,
        enable_hidden_channels=enable_hidden,
        enable_dynamic_topology=enable_dynamic_topology,
        early_stop_conditions=[early_stop_fn] if early_stop_fn else [],
        topology_hooks=topology_hooks or [],
    )

    def default_caller(prompt: str) -> str:
        return llm.call("", prompt)

    callers: dict[str, Any] = {}
    for aid, sys_prompt in SYSTEM_PROMPTS.items():
        _sys = sys_prompt

        def _caller(prompt: str, _s: str = _sys) -> str:
            return llm.call(_s, prompt)

        callers[aid] = _caller

    return MACPRunner(llm_caller=default_caller, llm_callers=callers, config=runner_config)


def _run_sample(
    llm: BenchLLM,
    runner: Any,
    graph: Any,
    sample: BBHSample,
    experiment: str,
    variant: str,
    *,
    final_id: str = "formatter",
    filter_unreachable: bool = False,
) -> RunResult:
    """Execute one runner round on a BBH sample and return a RunResult."""
    llm.tracker.reset()
    start = time.perf_counter()

    try:
        result = runner.run_round(
            graph,
            final_agent_id=final_id,
            filter_unreachable=filter_unreachable,
        )
        ms = (time.perf_counter() - start) * 1000
        output = result.final_answer or ""
        correct = check_accuracy(output, sample.reference)

        return RunResult(
            experiment=experiment,
            variant=variant,
            run_idx=sample.index,
            latency_ms=ms,
            total_tokens=llm.tracker.total_tokens,
            prompt_tokens=llm.tracker.prompt_tokens,
            completion_tokens=llm.tracker.completion_tokens,
            llm_calls=llm.tracker.call_count,
            output=output,
            agents_executed=len(result.execution_order),
            early_stopped=getattr(result, "early_stopped", False),
            correct=correct,
            reference=sample.reference,
            question=sample.question[:200],
        )

    except Exception as e:
        ms = (time.perf_counter() - start) * 1000
        return RunResult(
            experiment,
            variant,
            sample.index,
            ms,
            0,
            0,
            0,
            0,
            "",
            error=str(e),
            reference=sample.reference,
            question=sample.question[:200],
        )


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Early Stopping On vs Off
# ═══════════════════════════════════════════════════════════════════════════


def _custom_early_stop(ctx: Any) -> bool:
    response = getattr(ctx, "response", "") or ""
    keywords = ["final answer", "therefore", "the answer is", "result:"]
    return any(kw in response.lower() for kw in keywords)


_early_stop_conditions = EarlyStopCondition.combine_any(
    conditions=[
        EarlyStopCondition.on_agent_count(3),
        EarlyStopCondition.on_token_limit(6_000),
        EarlyStopCondition.on_custom(_custom_early_stop),
    ]
)


def t1_early_stop_on(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question)
    runner = _make_runner(llm, early_stop_fn=_early_stop_conditions)
    return _run_sample(llm, runner, graph, sample, "early_stopping", "On")


def t1_early_stop_off(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question)
    runner = _make_runner(llm)
    return _run_sample(llm, runner, graph, sample, "early_stopping", "Off")


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Disabled Nodes On vs Off
# ═══════════════════════════════════════════════════════════════════════════


def t2_disabled_on(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question, disabled_nodes=DISABLED_NODES)
    runner = _make_runner(llm)
    return _run_sample(llm, runner, graph, sample, "disabled_nodes", "On")


def t2_disabled_off(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question)
    runner = _make_runner(llm)
    return _run_sample(llm, runner, graph, sample, "disabled_nodes", "Off")


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Filter Unreachable On vs Off
# ═══════════════════════════════════════════════════════════════════════════


def t3_filter_on(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question, isolated_nodes=ISOLATED_NODES)
    runner = _make_runner(llm)
    return _run_sample(llm, runner, graph, sample, "filter_unreachable", "On", filter_unreachable=True)


def t3_filter_off(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question, isolated_nodes=ISOLATED_NODES)
    runner = _make_runner(llm)
    return _run_sample(llm, runner, graph, sample, "filter_unreachable", "Off", filter_unreachable=False)


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Adaptive Topology vs Static Topology
# ═══════════════════════════════════════════════════════════════════════════


def _topology_hook(step_context: Any, graph: Any) -> TopologyAction | None:
    if step_context.total_tokens > 5_000:
        return TopologyAction(skip_agents=step_context.remaining_agents)

    keywords = ["final answer", "the answer is", "therefore"]
    if step_context.agent_id == "reasoner" and any(kw in step_context.response.lower() for kw in keywords):
        return TopologyAction(skip_agents=step_context.remaining_agents)

    return None


def t4_adaptive(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question)
    runner = _make_runner(
        llm,
        adaptive=True,
        enable_dynamic_topology=True,
        topology_hooks=[_topology_hook],
    )
    return _run_sample(llm, runner, graph, sample, "adaptive_topology", "Adaptive")


def t4_static(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_chain_graph(sample.question)
    runner = _make_runner(llm, adaptive=False, enable_dynamic_topology=False)
    return _run_sample(llm, runner, graph, sample, "adaptive_topology", "Static")


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: Hidden Channels On vs Off
# ═══════════════════════════════════════════════════════════════════════════

_PARALLEL_PROMPTS: dict[str, str] = {
    "logical_reasoner": "You are a logical reasoner. Apply formal logic step by step.",
    "intuitive_reasoner": "You are an intuitive reasoner. Use common sense and intuition.",
    "analytical_reasoner": "You are an analytical reasoner. Use mathematical / analytical thinking.",
    "synthesizer": SYSTEM_PROMPTS["synthesizer"],
    "formatter": SYSTEM_PROMPTS["formatter"],
}


def t5_hidden_on(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _attach_embeddings(_build_parallel_graph(sample.question))

    def default_caller(prompt: str) -> str:
        return llm.call("", prompt)

    callers: dict[str, Any] = {}
    for aid, sys_prompt in _PARALLEL_PROMPTS.items():
        _sys = sys_prompt

        def _caller(prompt: str, _s: str = _sys) -> str:
            return llm.call(_s, prompt)

        callers[aid] = _caller

    runner = MACPRunner(
        llm_caller=default_caller,
        llm_callers=callers,
        config=RunnerConfig(
            timeout=_env_float("TIMEOUT", 120),
            adaptive=False,
            enable_hidden_channels=True,
            pass_embeddings=True,
        ),
    )

    llm.tracker.reset()
    start = time.perf_counter()

    try:
        result = runner.run_round_with_hidden(graph, final_agent_id="formatter")
        ms = (time.perf_counter() - start) * 1000
        output = result.final_answer or ""
        correct = check_accuracy(output, sample.reference)

        return RunResult(
            experiment="hidden_channels",
            variant="On",
            run_idx=sample.index,
            latency_ms=ms,
            total_tokens=llm.tracker.total_tokens,
            prompt_tokens=llm.tracker.prompt_tokens,
            completion_tokens=llm.tracker.completion_tokens,
            llm_calls=llm.tracker.call_count,
            output=output,
            agents_executed=len(result.execution_order),
            correct=correct,
            reference=sample.reference,
            question=sample.question[:200],
        )

    except Exception as e:
        ms = (time.perf_counter() - start) * 1000
        return RunResult(
            "hidden_channels",
            "On",
            sample.index,
            ms,
            0,
            0,
            0,
            0,
            "",
            error=str(e),
            reference=sample.reference,
            question=sample.question[:200],
        )


def t5_hidden_off(llm: BenchLLM, sample: BBHSample) -> RunResult:
    graph = _build_parallel_graph(sample.question)

    def default_caller(prompt: str) -> str:
        return llm.call("", prompt)

    callers: dict[str, Any] = {}
    for aid, sys_prompt in _PARALLEL_PROMPTS.items():
        _sys = sys_prompt

        def _caller(prompt: str, _s: str = _sys) -> str:
            return llm.call(_s, prompt)

        callers[aid] = _caller

    runner = MACPRunner(
        llm_caller=default_caller,
        llm_callers=callers,
        config=RunnerConfig(
            timeout=_env_float("TIMEOUT", 120),
            adaptive=False,
            enable_hidden_channels=False,
        ),
    )

    return _run_sample(llm, runner, graph, sample, "hidden_channels", "Off")


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Multi-Model (strong+weak) vs Single-Model (weak only)
#
# The idea: multi-model routing lets critical agents use a strong model
# while cheap agents use a weak one.  The baseline (Single) uses ONLY the
# weak model for everything, so we expect Multi to win on accuracy while
# Single wins on cost/speed.
# ═══════════════════════════════════════════════════════════════════════════


def t6_multi_model(_llm: BenchLLM, sample: BBHSample) -> RunResult:
    """Strong model for decomposer+reasoner, weak model for the rest."""
    graph = _build_chain_graph(sample.question)

    llm_strong = BenchLLM(
        model=_env("MODEL_STRONG") or _env("MODEL"),
        base_url=_env("BASE_URL_STRONG") or _env("BASE_URL"),
        api_key=_env("API_KEY_STRONG") or _env("API_KEY"),
    )
    llm_weak = BenchLLM(
        model=_env("MODEL_WEAK") or _env("MODEL"),
        base_url=_env("BASE_URL_WEAK") or _env("BASE_URL"),
        api_key=_env("API_KEY_WEAK") or _env("API_KEY"),
    )

    combined = TokenTracker()

    def _make_caller(bench_llm: BenchLLM, agent_id: str):
        sys_prompt = SYSTEM_PROMPTS.get(agent_id, "")

        def caller(prompt: str) -> str:
            result = bench_llm.call(sys_prompt, prompt)
            if bench_llm.tracker.per_call:
                last = bench_llm.tracker.per_call[-1]
                combined.total_tokens += last["total"]
                combined.prompt_tokens += last["prompt"]
                combined.completion_tokens += last["completion"]
                combined.call_count += 1
                combined.per_call.append(last)
            return result

        return caller

    callers = {}
    for agent_id in AGENT_IDS:
        if agent_id in STRONG_AGENTS:
            callers[agent_id] = _make_caller(llm_strong, agent_id)
        else:
            callers[agent_id] = _make_caller(llm_weak, agent_id)

    runner = MACPRunner(
        llm_caller=_make_caller(llm_strong, ""),
        llm_callers=callers,
        config=RunnerConfig(timeout=_env_float("TIMEOUT", 120), adaptive=True, enable_parallel=True),
    )

    llm_strong.tracker.reset()
    llm_weak.tracker.reset()
    start = time.perf_counter()

    try:
        result = runner.run_round(graph, final_agent_id="formatter")
        ms = (time.perf_counter() - start) * 1000
        output = result.final_answer or ""
        correct = check_accuracy(output, sample.reference)

        return RunResult(
            experiment="multi_model",
            variant="Multi",
            run_idx=sample.index,
            latency_ms=ms,
            total_tokens=combined.total_tokens,
            prompt_tokens=combined.prompt_tokens,
            completion_tokens=combined.completion_tokens,
            llm_calls=combined.call_count,
            output=output,
            agents_executed=len(result.execution_order),
            correct=correct,
            reference=sample.reference,
            question=sample.question[:200],
        )

    except Exception as e:
        ms = (time.perf_counter() - start) * 1000
        return RunResult(
            "multi_model",
            "Multi",
            sample.index,
            ms,
            0,
            0,
            0,
            0,
            "",
            error=str(e),
            reference=sample.reference,
            question=sample.question[:200],
        )


def t6_single_model(_llm: BenchLLM, sample: BBHSample) -> RunResult:
    """Baseline: ALL agents use the WEAK model only."""
    graph = _build_chain_graph(sample.question)

    llm_weak = BenchLLM(
        model=_env("MODEL_WEAK") or _env("MODEL"),
        base_url=_env("BASE_URL_WEAK") or _env("BASE_URL"),
        api_key=_env("API_KEY_WEAK") or _env("API_KEY"),
    )
    runner = _make_runner(llm_weak, adaptive=True)
    return _run_sample(llm_weak, runner, graph, sample, "multi_model", "Single(weak)")


# ═══════════════════════════════════════════════════════════════════════════
# Test registry
# ═══════════════════════════════════════════════════════════════════════════

TESTS = [
    ("1_early_stopping", "Early Stopping On vs Off", t1_early_stop_on, t1_early_stop_off),
    ("2_disabled_nodes", "Disabled Nodes On vs Off", t2_disabled_on, t2_disabled_off),
    ("3_filter_unreachable", "Filter Unreachable On vs Off", t3_filter_on, t3_filter_off),
    ("4_adaptive_topology", "Adaptive Topology vs Static", t4_adaptive, t4_static),
    ("5_hidden_channels", "Hidden Channels On vs Off", t5_hidden_on, t5_hidden_off),
    ("6_multi_model", "Multi-Model (strong+weak) vs Single-Model (weak)", t6_multi_model, t6_single_model),
]


# ═══════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ═══════════════════════════════════════════════════════════════════════════


def _fmt_run(r: RunResult) -> str:
    if r.error:
        return f"ERR: {r.error[:80]}"
    parts = [
        f"tok={r.total_tokens}",
        f"t={r.latency_ms:.0f}ms",
        f"agents={r.agents_executed}",
        "Y" if r.correct else "N",
    ]
    if r.early_stopped:
        parts.append("early")
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def run_benchmark(
    n_runs: int | None = None,
    test_ids: list[int] | None = None,
    max_samples: int | None = None,
    bbh_tasks: list[str] | None = None,
    skip_ping: bool = False,
    workers: int = 1,
) -> None:
    """Run selected A/B tests on BBH samples, compute stats, save results."""
    print("=" * 60)
    print("gMAS vs gMAS Benchmark (BIG-Bench Hard)")
    print("=" * 60)

    model_strong = _env("MODEL_STRONG") or _env("MODEL")
    model_weak = _env("MODEL_WEAK") or _env("MODEL")

    n_runs = n_runs if n_runs is not None else _env_int("N_RUNS", 5)
    if max_samples is None:
        v = _env("MAX_SAMPLES")
        max_samples = int(v) if v else None

    # Resolve BBH tasks from CLI or env
    if bbh_tasks is None:
        v = _env("BBH_TASKS")
        if v:
            bbh_tasks = [t.strip() for t in v.split(",")]

    print(f"Model (strong): {model_strong}")
    print(f"Model (weak):   {model_weak}")
    print(f"Runs per test:  {n_runs}")
    print(f"Max samples:    {max_samples or 'ALL (full dataset)'}")
    print(f"Workers:        {workers}")
    print()

    # Connectivity check
    llm = BenchLLM()
    if skip_ping:
        print("(ping check skipped)")
    elif not llm.ping():
        print("ERROR: LLM endpoint is not reachable.")
        return

    # Load BBH dataset
    print("\n--- Loading BIG-Bench Hard dataset ---")
    samples = load_bbh_samples(tasks=bbh_tasks, max_samples=max_samples)
    if not samples:
        print("ERROR: no BBH samples loaded.")
        return

    # Select tests
    selected = TESTS
    if test_ids:
        selected = [TESTS[i - 1] for i in test_ids if 1 <= i <= len(TESTS)]

    all_results: dict[str, dict[str, list[RunResult]]] = {}
    all_stats: list[tuple[str, VariantStats, VariantStats]] = []

    def _run_one_sample(
        variant_a_fn,
        variant_b_fn,
        sample: BBHSample,
        run_idx: int,
    ) -> tuple[RunResult, RunResult]:
        """Run A and B for a single sample. Each call gets its own BenchLLM."""
        llm_local = BenchLLM()
        if (sample.index + run_idx) % 2 == 0:
            ra = variant_a_fn(llm_local, sample)
            rb = variant_b_fn(llm_local, sample)
        else:
            rb = variant_b_fn(llm_local, sample)
            ra = variant_a_fn(llm_local, sample)
        return ra, rb

    for test_key, test_name, variant_a_fn, variant_b_fn in selected:
        print(f"\n{'=' * 60}")
        print(f"  {test_name}")
        print(f"{'=' * 60}")

        results_a: list[RunResult] = []
        results_b: list[RunResult] = []

        for run_idx in range(n_runs):
            print(f"\n  --- Run {run_idx + 1}/{n_runs} ---")

            if workers <= 1:
                for sample in samples:
                    ra, rb = _run_one_sample(variant_a_fn, variant_b_fn, sample, run_idx)
                    results_a.append(ra)
                    results_b.append(rb)
                    print(f"    [{sample.task}] A({ra.variant}): {_fmt_run(ra)} | B({rb.variant}): {_fmt_run(rb)}")
            else:
                futures = {}
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for sample in samples:
                        fut = pool.submit(
                            _run_one_sample,
                            variant_a_fn,
                            variant_b_fn,
                            sample,
                            run_idx,
                        )
                        futures[fut] = sample

                    batch: list[tuple[int, BBHSample, RunResult, RunResult]] = []
                    for fut in as_completed(futures):
                        sample = futures[fut]
                        ra, rb = fut.result()
                        batch.append((sample.index, sample, ra, rb))

                batch.sort(key=lambda x: x[0])
                for _, sample, ra, rb in batch:
                    results_a.append(ra)
                    results_b.append(rb)
                    print(f"    [{sample.task}] A({ra.variant}): {_fmt_run(ra)} | B({rb.variant}): {_fmt_run(rb)}")

        sa = compute_stats(results_a)
        sb = compute_stats(results_b)
        all_stats.append((test_name, sa, sb))
        all_results[test_key] = {sa.variant: results_a, sb.variant: results_b}

        # Print summary
        print("\n  -- Stats --")
        for s in [sa, sb]:
            print(
                f"  {s.variant:15s}: acc={s.accuracy:.1%}±{s.acc_std:.2f} ({s.n_correct}/{s.n_runs})  "
                f"err={s.n_errors}  "
                f"lat={s.lat_avg:.0f}±{s.lat_std:.0f}ms  "
                f"tok={s.tok_avg:.0f}±{s.tok_std:.0f}  "
                f"calls={s.calls_avg:.1f}±{s.calls_std:.1f}  agents={s.agents_avg:.1f}±{s.agents_std:.1f}"
            )

        if sa.tok_avg > 0 and sb.tok_avg > 0:
            tok_delta = (sb.tok_avg - sa.tok_avg) / sb.tok_avg * 100
            lat_delta = (sb.lat_avg - sa.lat_avg) / sb.lat_avg * 100 if sb.lat_avg > 0 else 0
            acc_delta = sa.accuracy - sb.accuracy
            print(f"\n  Token delta:    {tok_delta:+.1f}% ({'A saves' if tok_delta > 0 else 'B saves'})")
            print(f"  Latency delta:  {lat_delta:+.1f}% ({'A faster' if lat_delta > 0 else 'B faster'})")
            print(f"  Accuracy delta: {acc_delta:+.1%} ({'A better' if acc_delta > 0 else 'B better'})")

        # --- Paired statistical tests ---
        # Build aligned paired arrays (only where both A and B succeeded)
        paired_ok = [
            (ra, rb) for ra, rb in zip(results_a, results_b, strict=False) if ra.error is None and rb.error is None
        ]

        p_thresh = _env_float("P_VALUE_THRESHOLD", 0.05)
        min_stats = _env_int("MIN_SAMPLES_FOR_STATS", 3)

        if len(paired_ok) >= min_stats:
            la = [ra.latency_ms for ra, _ in paired_ok]
            lb = [rb.latency_ms for _, rb in paired_ok]
            wl = _wilcoxon_paired(la, lb, p_thresh)
            sig = "SIGNIFICANT" if wl["significant"] else "NOT significant"
            print(f"  Latency Wilcoxon: p={wl['p_value']:.6f} -- {sig}")

            ta = [float(ra.total_tokens) for ra, _ in paired_ok]
            tb = [float(rb.total_tokens) for _, rb in paired_ok]
            wt = _wilcoxon_paired(ta, tb, p_thresh)
            sig_t = "SIGNIFICANT" if wt["significant"] else "NOT significant"
            print(f"  Tokens  Wilcoxon: p={wt['p_value']:.6f} -- {sig_t}")

            # McNemar test for paired binary accuracy
            ac = [ra.correct for ra, _ in paired_ok]
            bc = [rb.correct for _, rb in paired_ok]
            mn = _mcnemar_test(ac, bc, p_thresh)
            sig_m = "SIGNIFICANT" if mn["significant"] else "NOT significant"
            print(
                f"  Accuracy McNemar: p={mn['p_value']:.6f} -- {sig_m}  (A-only={mn['a_only']}, B-only={mn['b_only']})"
            )

    print(f"\n{'=' * 60}")
    _save_all(all_results, all_stats, n_runs, samples)


# ═══════════════════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════════════════


def _config_for_report() -> dict[str, Any]:
    """Collect current env config for JSON reports (masks API keys)."""
    _mask_prefix = 4

    def _mask(key: str) -> str:
        return (key[:_mask_prefix] + "****") if len(key) > _mask_prefix else "****"

    return {
        "base_url": _env("BASE_URL"),
        "model": _env("MODEL"),
        "model_strong": _env("MODEL_STRONG") or _env("MODEL"),
        "base_url_strong": _env("BASE_URL_STRONG") or _env("BASE_URL"),
        "model_weak": _env("MODEL_WEAK") or _env("MODEL"),
        "base_url_weak": _env("BASE_URL_WEAK") or _env("BASE_URL"),
        "api_key": _mask(_env("API_KEY")),
        "embedding_model": _env("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        "n_runs": _env_int("N_RUNS", 5),
        "temperature": _env_float("TEMPERATURE", 0.7),
        "max_tokens": _env_int("MAX_TOKENS", 4096),
        "timeout": _env_float("TIMEOUT", 120),
    }


def _save_json(
    log_dir: Path,
    now: datetime,
    all_results: dict[str, dict[str, list[RunResult]]],
    samples: list[BBHSample],
) -> Path:
    p_thresh = _env_float("P_VALUE_THRESHOLD", 0.05)
    min_stats = _env_int("MIN_SAMPLES_FOR_STATS", 3)

    jdata: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "config": _config_for_report(),
        "dataset": {
            "name": "BIG-Bench Hard (lukaemon/bbh)",
            "n_samples": len(samples),
            "tasks": sorted({s.task for s in samples}),
        },
        "tests": {},
    }

    for test_key, by_variant in all_results.items():
        td: dict[str, Any] = {}
        variant_results_list: list[list[RunResult]] = []

        for variant_name, results in by_variant.items():
            variant_results_list.append(results)
            s = compute_stats(results)
            td[variant_name] = {
                "stats": {
                    "n": s.n_runs,
                    "n_correct": s.n_correct,
                    "n_errors": s.n_errors,
                    "accuracy": {"mean": s.accuracy, "std": s.acc_std},
                    "latency_ms": {"avg": s.lat_avg, "std": s.lat_std, "median": s.lat_median, "p95": s.lat_p95},
                    "tokens": {
                        "avg": s.tok_avg,
                        "std": s.tok_std,
                        "prompt_avg": s.prompt_tok_avg,
                        "compl_avg": s.compl_tok_avg,
                    },
                    "calls": {"avg": s.calls_avg, "std": s.calls_std},
                    "agents": {"avg": s.agents_avg, "std": s.agents_std},
                },
                "runs": [
                    {
                        "idx": r.run_idx,
                        "correct": r.correct,
                        "reference": r.reference,
                        "lat_ms": r.latency_ms,
                        "tok": r.total_tokens,
                        "agents": r.agents_executed,
                        "early_stopped": r.early_stopped,
                        "err": r.error,
                        "output": r.output[:300],
                    }
                    for r in results
                ],
            }

        # Paired statistical tests
        _n_variants = 2
        if len(variant_results_list) == _n_variants:
            ra_list, rb_list = variant_results_list
            paired_ok = [
                (ra, rb) for ra, rb in zip(ra_list, rb_list, strict=False) if ra.error is None and rb.error is None
            ]
            if len(paired_ok) >= min_stats:
                la = [ra.latency_ms for ra, _ in paired_ok]
                lb = [rb.latency_ms for _, rb in paired_ok]
                ta = [float(ra.total_tokens) for ra, _ in paired_ok]
                tb = [float(rb.total_tokens) for _, rb in paired_ok]
                ac = [ra.correct for ra, _ in paired_ok]
                bc = [rb.correct for _, rb in paired_ok]

                td["paired_tests"] = {
                    "n_paired": len(paired_ok),
                    "latency_wilcoxon": _wilcoxon_paired(la, lb, p_thresh),
                    "tokens_wilcoxon": _wilcoxon_paired(ta, tb, p_thresh),
                    "accuracy_mcnemar": _mcnemar_test(ac, bc, p_thresh),
                }

        jdata["tests"][test_key] = td

    ts = now.strftime("%Y%m%d_%H%M%S")
    jp = log_dir / f"bench_features_{ts}.json"
    jp.write_text(json.dumps(jdata, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  JSON saved: {jp}")
    return jp


def _save_markdown(
    log_dir: Path,
    now: datetime,
    all_stats: list[tuple[str, VariantStats, VariantStats]],
    all_results: dict[str, dict[str, list[RunResult]]],
    n_runs: int,
    n_samples: int,
) -> Path:
    model_strong = _env("MODEL_STRONG") or _env("MODEL")
    model_weak = _env("MODEL_WEAK") or _env("MODEL")

    md = [
        "# gMAS Feature Benchmark (BIG-Bench Hard)\n",
        f"**Date:** {now.isoformat()}  ",
        f"**Model (strong):** `{model_strong}`  ",
        f"**Model (weak):** `{model_weak}`  ",
        f"**Runs:** {n_runs} × {n_samples} samples  ",
        f"**Temperature:** {_env_float('TEMPERATURE', 0.7)}  **Max tokens:** {_env_int('MAX_TOKENS', 4096)}\n",
        "## Results\n",
        "| Experiment | Variant | Accuracy (±std) | Correct | Errors "
        "| Latency avg±std | Tokens avg±std | Calls avg±std | Agents avg±std |",
        "|------------|---------|-----------------|---------|--------|"
        "-----------------|----------------|---------------|----------------|",
    ]

    for test_name, sa, sb in all_stats:
        label = test_name
        for s in [sa, sb]:
            md.append(
                f"| {label} | {s.variant} | "
                f"{s.accuracy:.1%}±{s.acc_std:.2f} | "
                f"{s.n_correct}/{s.n_runs} | "
                f"{s.n_errors} | "
                f"{s.lat_avg:.0f}±{s.lat_std:.0f} | "
                f"{s.tok_avg:.0f}±{s.tok_std:.0f} | "
                f"{s.calls_avg:.1f}±{s.calls_std:.1f} | "
                f"{s.agents_avg:.1f}±{s.agents_std:.1f} |"
            )
            label = ""

    md.append("\n## Comparison\n")

    for name, sa, sb in all_stats:
        acc_delta = sa.accuracy - sb.accuracy
        tok_delta = (sb.tok_avg - sa.tok_avg) / sb.tok_avg * 100 if sb.tok_avg > 0 else 0
        md.append(
            f"- **{name}**: "
            f"A({sa.variant}) acc={sa.accuracy:.1%}±{sa.acc_std:.2f} "
            f"correct={sa.n_correct}/{sa.n_runs} err={sa.n_errors}, "
            f"B({sb.variant}) acc={sb.accuracy:.1%}±{sb.acc_std:.2f} "
            f"correct={sb.n_correct}/{sb.n_runs} err={sb.n_errors} -- "
            f"d_acc={acc_delta:+.1%}, d_tok={tok_delta:+.1f}%"
        )

    # Paired statistical tests section
    md.append("\n## Statistical Tests (Paired)\n")
    md.append(
        "Design: paired (same sample for both variants). "
        "Continuous metrics use **Wilcoxon signed-rank**; "
        "binary accuracy uses **McNemar's test**.\n"
    )
    md.append("| Experiment | Metric | Test | p-value | Significant |")
    md.append("|------------|--------|------|---------|-------------|")

    p_thresh = _env_float("P_VALUE_THRESHOLD", 0.05)
    min_stats = _env_int("MIN_SAMPLES_FOR_STATS", 3)

    test_keys = list(all_results.keys())
    for i, (name, _sa, _sb) in enumerate(all_stats):
        test_key = test_keys[i] if i < len(test_keys) else None
        if test_key is None:
            continue
        variants = list(all_results[test_key].values())
        if len(variants) != 2:
            continue
        ra_list, rb_list = variants
        paired_ok = [
            (ra, rb) for ra, rb in zip(ra_list, rb_list, strict=False) if ra.error is None and rb.error is None
        ]
        if len(paired_ok) < min_stats:
            md.append(f"| {name} | -- | -- | -- | too few paired samples ({len(paired_ok)}) |")
            continue

        la = [ra.latency_ms for ra, _ in paired_ok]
        lb = [rb.latency_ms for _, rb in paired_ok]
        wl = _wilcoxon_paired(la, lb, p_thresh)

        ta = [float(ra.total_tokens) for ra, _ in paired_ok]
        tb = [float(rb.total_tokens) for _, rb in paired_ok]
        wt = _wilcoxon_paired(ta, tb, p_thresh)

        ac = [ra.correct for ra, _ in paired_ok]
        bc = [rb.correct for _, rb in paired_ok]
        mn = _mcnemar_test(ac, bc, p_thresh)

        _sig = lambda d: "Yes" if d["significant"] else "No"
        md.append(f"| {name} | Latency | {wl['test']} | {wl['p_value']:.6f} | {_sig(wl)} |")
        md.append(f"| | Tokens | {wt['test']} | {wt['p_value']:.6f} | {_sig(wt)} |")
        md.append(
            f"| | Accuracy | {mn['test']} | {mn['p_value']:.6f} "
            f"| {_sig(mn)} (A-only={mn['a_only']}, B-only={mn['b_only']}) |"
        )

    md.append("\n## Expected Outcomes\n")
    md.append("| Test | Expected |")
    md.append("|------|----------|")
    md.append("| Early Stopping | Fewer tokens, similar or slightly lower accuracy |")
    md.append("| Disabled Nodes | Fewer tokens/agents, possibly lower accuracy |")
    md.append("| Filter Unreachable | Same accuracy, fewer wasted resources |")
    md.append("| Adaptive Topology | Better efficiency, similar accuracy |")
    md.append("| Hidden Channels | Better context -> higher accuracy |")
    md.append("| Multi-Model vs Weak-only | Multi wins on accuracy, weak wins on cost |")

    ts = now.strftime("%Y%m%d_%H%M%S")
    mp = log_dir / f"bench_features_{ts}.md"
    mp.write_text("\n".join(md), encoding="utf-8")
    print(f"  Markdown saved: {mp}")
    return mp


def _save_all(
    all_results: dict[str, dict[str, list[RunResult]]],
    all_stats: list[tuple[str, VariantStats, VariantStats]],
    n_runs: int,
    samples: list[BBHSample],
) -> None:
    now = datetime.now(UTC)
    log_dir = Path(__file__).resolve().parent.parent / "benchmark_logs" / "gmas_vs_gmas"
    log_dir.mkdir(parents=True, exist_ok=True)

    _save_json(log_dir, now, all_results, samples)
    _save_markdown(log_dir, now, all_stats, all_results, n_runs, len(samples))


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gMAS Feature Benchmark (BIG-Bench Hard)")
    parser.add_argument("--runs", type=int, default=None, help="Runs per test (default from .env)")
    parser.add_argument("--tests", type=str, default=None, help="Comma-separated test numbers 1-6 (default: all)")
    parser.add_argument("--max-samples", type=int, default=None, help="Max BBH samples (default: full dataset)")
    parser.add_argument(
        "--bbh-tasks",
        type=str,
        default=None,
        help="Comma-separated BBH task names (default: all 23 tasks)",
    )
    parser.add_argument("--skip-ping", action="store_true", help="Skip LLM connectivity check")
    parser.add_argument("--workers", type=int, default=3, help="Parallel workers per test (default: 3)")
    args = parser.parse_args()

    ids = [int(x.strip()) for x in args.tests.split(",")] if args.tests else None
    tasks = [t.strip() for t in args.bbh_tasks.split(",")] if args.bbh_tasks else None

    run_benchmark(
        n_runs=args.runs,
        test_ids=ids,
        max_samples=args.max_samples,
        bbh_tasks=tasks,
        skip_ping=args.skip_ping,
        workers=args.workers,
    )
