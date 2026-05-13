"""
LLM-based evaluation system for Umbrella.

Replaces formula-based evaluation with intelligent LLM analysis of:
- Run quality (completeness, correctness, style)
- Common issues (cut off content, missing experiments, etc.)
- Overall assessment

This makes Ouroboros/Umbrella responsible for quality judgment.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from umbrella.config import (
    LLM_EVAL_AGENT_OUTPUT_LIMIT,
    LLM_EVAL_AGENT_OUTPUT_PREVIEW_LIMIT,
    LLM_EVAL_ARTIFACT_CONTENT_LIMIT,
    LLM_EVAL_ARTIFACT_PREVIEW_LIMIT,
    LLM_EVAL_TASK_PREVIEW_LIMIT,
)
from umbrella.env import get_llm_env_config, get_openai_base_url, load_env
from umbrella.evals.models import (
    EvaluationRecord,
    TaskSuccessRating,
    OutputQualityRating,
    StabilityRating,
)
from umbrella.evals.models import generate_evaluation_id

log = logging.getLogger(__name__)


@dataclass
class LLMEvalResult:
    """Result from LLM-based evaluation."""

    task_success: TaskSuccessRating
    output_quality: OutputQualityRating
    stability: StabilityRating
    overall_score: float
    reasoning: str
    issues_found: list[str]
    strengths: list[str]


def evaluate_run_with_llm(
    run_result: dict[str, Any],
    instance_path: Path,
    repo_root: Path,
    task_input: str,
    *,
    task_class: str | None = None,
) -> EvaluationRecord:
    """Evaluate a workspace run using LLM intelligence.

    The LLM analyzes:
    - The task and whether it was completed
    - Output artifacts (reports, articles)
    - Run logs and events
    - Common failure patterns

    Args:
        run_result: Result from workspace run
        instance_path: Path to workspace instance
        repo_root: Repository root
        task_input: Original task input
        task_class: Optional task classification

    Returns:
        EvaluationRecord with LLM-based assessment
    """
    load_env(repo_root=repo_root)
    llm_model, llm_api_key, llm_base_url = get_llm_env_config()

    if not llm_api_key:
        log.warning("No LLM credentials, falling back to simple evaluation")
        return _fallback_evaluation(run_result, instance_path, task_input)

    # Gather evaluation context
    context = _build_eval_context(run_result, instance_path, task_input)

    # Call LLM for evaluation
    eval_result = _call_llm_for_evaluation(
        context=context,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
    )

    # Build evaluation record
    return EvaluationRecord(
        id=generate_evaluation_id(),
        task_id=run_result.get("task_id", "unknown"),
        workspace_id=run_result.get("workspace_id", "unknown"),
        run_id=run_result.get("run_id", "unknown"),
        instance_path=str(instance_path),
        task_class=task_class,
        task_success=eval_result.task_success,
        output_quality=eval_result.output_quality,
        stability=eval_result.stability,
        total_tokens=run_result.get("total_tokens", 0),
        total_duration_seconds=run_result.get("duration_seconds", 0),
        total_cost_usd=run_result.get("cost_usd", 0.0),
        iterations_to_completion=run_result.get("iterations"),
        iterations_limit_reached=False,
        retrieval_was_useful=True,
        retrieval_hits_used=0,
        raw_log_inspection_required=False,
        patches_applied=run_result.get("patches_applied", 0),
        patch_success_rate=1.0 if run_result.get("status") == "completed" else 0.0,
        structured_summary_sufficient=True,
        artifact_count=len(context.get("artifacts", [])),
        manager_level_issues=[],
        overall_score=eval_result.overall_score,
        evidence=[
            f"LLM evaluation: {eval_result.reasoning}",
            *eval_result.strengths,
        ],
        evaluator_notes=f"LLM-based evaluation | Issues: {', '.join(eval_result.issues_found) if eval_result.issues_found else 'None'}",
    )


def _build_eval_context(
    run_result: dict[str, Any],
    instance_path: Path,
    task_input: str,
) -> dict[str, Any]:
    """Build context for LLM evaluation."""
    context = {
        "task_input": task_input,
        "run_status": run_result.get("status", "unknown"),
        "run_id": run_result.get("run_id", "unknown"),
        "duration_seconds": run_result.get("duration_seconds", 0),
        "total_tokens": run_result.get("total_tokens", 0),
    }

    # Try to read artifacts
    reports_dir = instance_path / "reports"
    artifacts = []
    if reports_dir.exists():
        for artifact_file in reports_dir.glob("latest_*.md"):
            try:
                content = artifact_file.read_text(encoding="utf-8")
                artifacts.append(
                    {
                        "name": artifact_file.name,
                        "content": content[:LLM_EVAL_ARTIFACT_CONTENT_LIMIT],
                    }
                )
            except Exception:
                pass

    context["artifacts"] = artifacts

    # Try to read events
    events_file = instance_path / "runs" / run_result.get("run_id", "") / "events.jsonl"
    if events_file.exists():
        try:
            events_text = events_file.read_text(encoding="utf-8")
            # Last 50 events
            events_lines = events_text.strip().split("\n")[-50:]
            context["recent_events"] = events_lines
        except Exception:
            pass

    # Try to read memory/agent outputs
    memory_dir = instance_path / "runs" / run_result.get("run_id", "") / "memory"
    if memory_dir.exists():
        agent_outputs = []
        for agent_dir in memory_dir.iterdir():
            if agent_dir.is_dir():
                for output_file in agent_dir.glob("*_output.md"):
                    try:
                        content = output_file.read_text(encoding="utf-8")
                        agent_outputs.append(
                            {
                                "agent": agent_dir.name,
                                "output": content[:LLM_EVAL_AGENT_OUTPUT_LIMIT],
                            }
                        )
                    except Exception:
                        pass
        context["agent_outputs"] = agent_outputs[-5:]  # Last 5 agents

    return context


def _call_llm_for_evaluation(
    context: dict[str, Any],
    llm_model: str,
    llm_api_key: str,
    llm_base_url: str | None,
) -> LLMEvalResult:
    """Call LLM to evaluate the run."""
    import httpx

    # Build evaluation prompt
    prompt = _build_eval_prompt(context)

    messages = [
        {"role": "system", "content": _EVALUATOR_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    # Call LLM
    with httpx.Client(timeout=120.0) as client:
        result = None
        errors: list[str] = []
        for url in _candidate_chat_completion_urls(llm_base_url):
            try:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model,
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": 2000,
                    },
                    follow_redirects=True,
                )
                response.raise_for_status()
                result = response.json()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                errors.append(f"{url} -> HTTP {status_code}")
                if status_code != 404:
                    raise
            except httpx.RequestError as exc:
                errors.append(f"{url} -> {exc}")

        if result is None:
            raise RuntimeError(
                "LLM evaluator could not reach a chat completions endpoint "
                f"for base URL {get_openai_base_url(llm_base_url)}: {'; '.join(errors)}"
            )

    content = result["choices"][0]["message"]["content"]
    return _parse_llm_eval_response(content)


def _candidate_chat_completion_urls(llm_base_url: str | None) -> list[str]:
    """Build likely OpenAI-compatible chat completion endpoints for the configured base URL."""
    base = get_openai_base_url(llm_base_url).rstrip("/")
    candidates: list[str] = []

    def add(url: str) -> None:
        if url not in candidates:
            candidates.append(url)

    if base.endswith("/v1"):
        add(f"{base}/chat/completions")
        add(f"{base[:-3]}/chat/completions")
    else:
        add(f"{base}/v1/chat/completions")
        add(f"{base}/chat/completions")

    return candidates


def _build_eval_prompt(context: dict[str, Any]) -> str:
    """Build the evaluation prompt for the LLM."""
    task_input = str(context.get("task_input") or "")
    task_preview = task_input[:LLM_EVAL_TASK_PREVIEW_LIMIT]
    if len(task_input) > LLM_EVAL_TASK_PREVIEW_LIMIT:
        task_preview += "..."

    prompt_parts = [
        f"**Task:** {task_preview}",
        f"\n**Run Status:** {context['run_status']}",
        f"**Duration:** {context['duration_seconds']:.1f}s",
        f"**Tokens:** {context['total_tokens']}",
    ]

    # Add artifacts info
    if context.get("artifacts"):
        prompt_parts.append("\n**Generated Artifacts:**")
        for artifact in context["artifacts"][:3]:
            prompt_parts.append(f"- {artifact['name']}")
            if artifact.get("content"):
                preview = artifact["content"][:LLM_EVAL_ARTIFACT_PREVIEW_LIMIT]
                prompt_parts.append(f"  Preview: {preview}...")

    # Add agent outputs
    if context.get("agent_outputs"):
        prompt_parts.append("\n**Agent Outputs (last 5):**")
        for output in context["agent_outputs"]:
            prompt_parts.append(
                f"- {output['agent']}: {output['output'][:LLM_EVAL_AGENT_OUTPUT_PREVIEW_LIMIT]}..."
            )

    prompt_parts.append(
        "\n**Please evaluate this run and respond in JSON format as specified.**"
    )

    return "\n".join(prompt_parts)


_EVALUATOR_SYSTEM_PROMPT = """You are an intelligent evaluator for AI agent systems. Your job is to assess the quality of a workspace run.

Evaluate the run based on:
1. **Task Success**: Did the system complete the requested task?
   - COMPLETE: Task fully accomplished with good results
   - PARTIAL: Task partially done, or results have issues
   - FAILED: Task not accomplished or severe errors

2. **Output Quality**: How good are the outputs?
   - EXCELLENT: High quality, complete, well-structured
   - GOOD: Mostly good with minor issues
   - FAIR: Acceptable but has noticeable problems
   - POOR: Significant issues (cut off, incomplete, poorly structured)
   - UNUSABLE: Output is garbage or missing

3. **Common Issues to Check**:
   - Content cut off mid-sentence
   - Missing required sections
   - No experiments or evidence when required
   - Repetitive content
   - Hallucinations or made-up facts
   - Poor structure or formatting
   - Incomplete conclusions or references

4. **Overall Score** (0.0 to 1.0): Based on success, quality, and completeness.

Respond ONLY with valid JSON in this exact format:
{
  "task_success": "COMPLETE|PARTIAL|FAILED",
  "output_quality": "EXCELLENT|GOOD|FAIR|POOR|UNUSABLE",
  "stability": "STABLE|MOSTLY_STABLE|UNSTABLE|UNKNOWN",
  "overall_score": 0.75,
  "reasoning": "Brief explanation of the evaluation",
  "issues_found": ["issue1", "issue2"],
  "strengths": ["strength1", "strength2"]
}"""


def _parse_llm_eval_response(content: str) -> LLMEvalResult:
    """Parse LLM response into LLMEvalResult."""
    import re

    # Try to extract JSON from response
    json_match = re.search(r"\{[\s\S]*\}", content)
    if not json_match:
        log.warning("LLM response did not contain valid JSON, using fallback")
        return _fallback_llm_result(content)

    try:
        data = json.loads(json_match.group())
        return LLMEvalResult(
            task_success=TaskSuccessRating(data.get("task_success", "UNKNOWN")),
            output_quality=OutputQualityRating(data.get("output_quality", "UNKNOWN")),
            stability=StabilityRating(data.get("stability", "UNKNOWN")),
            overall_score=float(data.get("overall_score", 0.5)),
            reasoning=data.get("reasoning", "")[:500],
            issues_found=data.get("issues_found", []),
            strengths=data.get("strengths", []),
        )
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"Failed to parse LLM response: {e}, using fallback")
        return _fallback_llm_result(content)


def _fallback_llm_result(content: str) -> LLMEvalResult:
    """Fallback when LLM response can't be parsed."""
    # Simple keyword-based assessment
    content_lower = content.lower()

    if any(word in content_lower for word in ["complete", "success", "excellent"]):
        success = TaskSuccessRating.COMPLETE
        quality = OutputQualityRating.GOOD
        score = 0.8
    elif any(word in content_lower for word in ["partial", "fair", "some issues"]):
        success = TaskSuccessRating.PARTIAL
        quality = OutputQualityRating.FAIR
        score = 0.6
    else:
        success = TaskSuccessRating.FAILED
        quality = OutputQualityRating.POOR
        score = 0.3

    # Check for common issues
    issues = []
    if "cut off" in content_lower or "incomplete" in content_lower:
        issues.append("Content appears cut off or incomplete")
    if "missing" in content_lower:
        issues.append("Missing required sections")
    if "experiment" not in content_lower and "evidence" not in content_lower:
        issues.append("No experiments or evidence found")

    return LLMEvalResult(
        task_success=success,
        output_quality=quality,
        stability=StabilityRating.UNKNOWN,
        overall_score=score,
        reasoning=content[:500],
        issues_found=issues,
        strengths=[],
    )


def _fallback_evaluation(
    run_result: dict[str, Any],
    instance_path: Path,
    task_input: str,
) -> EvaluationRecord:
    """Fallback evaluation when LLM is not available."""
    return EvaluationRecord(
        id=generate_evaluation_id(),
        task_id=run_result.get("task_id", "unknown"),
        workspace_id=run_result.get("workspace_id", "unknown"),
        run_id=run_result.get("run_id", "unknown"),
        instance_path=str(instance_path),
        task_class=None,
        task_success=TaskSuccessRating.UNKNOWN,
        output_quality=OutputQualityRating.UNKNOWN,
        stability=StabilityRating.UNKNOWN,
        total_tokens=run_result.get("total_tokens", 0),
        total_duration_seconds=run_result.get("duration_seconds", 0),
        total_cost_usd=0.0,
        iterations_to_completion=run_result.get("iterations"),
        iterations_limit_reached=False,
        retrieval_was_useful=False,
        retrieval_hits_used=0,
        raw_log_inspection_required=True,
        patches_applied=0,
        patch_success_rate=0.0,
        structured_summary_sufficient=False,
        artifact_count=0,
        manager_level_issues=["LLM not available for evaluation"],
        overall_score=0.5,
        evidence=["Fallback evaluation - LLM unavailable"],
        evaluator_notes="LLM evaluation unavailable - using fallback",
    )
