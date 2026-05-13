"""
Real self-improvement: Umbrella rewrites instance code to improve performance.

This module enables Umbrella to rewrite:
1. Workspace code (agents, prompts, configs)
2. Manager code (its own prompts, policies)

Crucially, the changes stay inside the active instance until the normal
rerun/evaluation/promotion pipeline proves that they are actually better.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
import tomllib

from umbrella.env import get_llm_env_config, get_openai_base_url, load_env

log = logging.getLogger(__name__)

_DEFAULT_MUTABLE_ROOTS = (
    "agents",
    "prompts",
    "graph",
    "tools",
    "models",
    "evals",
    "experiments",
    "src",
    "ui",
    "interface",
    "dashboard",
    "frontend",
    "backend",
)
_DEFAULT_MUTABLE_FILES = (
    "workspace.toml",
    "policies.toml",
    "TASK_MAIN.md",
)
_CONFIG_PATH_KEYS = (
    "graph_file",
    "tools_allowlist_file",
    "models_file",
    "policies_file",
    "task_main_file",
)
_DIR_PATH_KEYS = (
    "agents_dir",
    "prompts_dir",
    "experiments_dir",
    "evals_dir",
    "runs_dir",
    "snapshots_dir",
    "reports_dir",
)
_TEXTUAL_SUFFIXES = {
    ".py",
    ".toml",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".ini",
    ".cfg",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".html",
}
_IGNORED_SCAN_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "runs",
    "reports",
    "snapshots",
    "logs",
    "memory",
    "instances",
}
_MAX_CONTEXT_FILES = 40
_MAX_RAW_TAIL_LINES = 20


@dataclass
class CodeImprovement:
    """A specific code improvement to apply."""

    file_path: str
    original_content: str
    improved_content: str
    description: str
    change_type: str  # "agent_config", "prompt", "graph", "policy", etc.


@dataclass
class ImprovementPlan:
    """Plan for improving the system."""

    task_id: str
    issue_description: str
    improvements: list[CodeImprovement] = field(default_factory=list)
    reasoning: str = ""
    expected_impact: str = ""


def _load_workspace_mutable_surfaces(
    instance_path: Path,
) -> tuple[list[str], list[str]]:
    """Return mutable roots and pinned files for the active instance."""
    mutable_roots: set[str] = set()
    pinned_files: set[str] = {
        relative_path
        for relative_path in _DEFAULT_MUTABLE_FILES
        if (instance_path / relative_path).exists()
    }

    workspace_toml_path = instance_path / "workspace.toml"
    if workspace_toml_path.exists():
        pinned_files.add("workspace.toml")
        try:
            workspace_data = tomllib.loads(
                workspace_toml_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            log.debug("Failed to parse workspace.toml for mutable surfaces: %s", exc)
            workspace_data = {}

        mutable_paths = workspace_data.get("mutable_paths")
        if isinstance(mutable_paths, list):
            for value in mutable_paths:
                if isinstance(value, str) and value.strip():
                    normalized = Path(value).as_posix()
                    relative_path = Path(normalized)
                    if relative_path.suffix:
                        pinned_files.add(normalized)
                        parent = relative_path.parent.as_posix()
                        if parent not in {"", "."}:
                            mutable_roots.add(parent)
                    else:
                        mutable_roots.add(normalized)

        for key in _CONFIG_PATH_KEYS:
            value = workspace_data.get(key)
            if isinstance(value, str) and value.strip():
                normalized = Path(value).as_posix()
                pinned_files.add(normalized)
                parent = Path(normalized).parent.as_posix()
                if parent not in {"", "."}:
                    mutable_roots.add(parent)

        for key in _DIR_PATH_KEYS:
            value = workspace_data.get(key)
            if isinstance(value, str) and value.strip():
                mutable_roots.add(Path(value).as_posix())

    for root in _DEFAULT_MUTABLE_ROOTS:
        if (instance_path / root).exists():
            mutable_roots.add(root)

    return sorted(mutable_roots), sorted(pinned_files)


def _collect_modifiable_files(instance_path: Path) -> list[str]:
    """Collect concrete mutable files that the optimizer can edit."""
    mutable_roots, pinned_files = _load_workspace_mutable_surfaces(instance_path)
    files: list[str] = []
    seen: set[str] = set()

    def _add(relative_path: str) -> None:
        normalized = Path(relative_path).as_posix()
        if normalized in seen:
            return
        seen.add(normalized)
        files.append(normalized)

    for relative_path in pinned_files:
        if (instance_path / relative_path).exists():
            _add(relative_path)

    for root in mutable_roots:
        path = instance_path / root
        if not path.exists():
            continue
        if path.is_file():
            _add(root)
            continue

        for child in sorted(path.rglob("*")):
            if len(files) >= _MAX_CONTEXT_FILES:
                break
            if not child.is_file():
                continue
            relative = child.relative_to(instance_path)
            if any(part in _IGNORED_SCAN_DIRS for part in relative.parts):
                continue
            if child.suffix.lower() not in _TEXTUAL_SUFFIXES:
                continue
            _add(relative.as_posix())

        if len(files) >= _MAX_CONTEXT_FILES:
            break

    return files[:_MAX_CONTEXT_FILES]


def analyze_and_improve(
    task_id: str,
    context: dict[str, Any],
    repo_root: Path,
) -> list[CodeImprovement]:
    """Analyze performance and generate concrete improvements."""
    load_env(repo_root=repo_root)
    llm_model, llm_api_key, llm_base_url = get_llm_env_config()

    if not llm_api_key:
        log.warning("No LLM available for improvement analysis")
        return []

    context_text = _build_improvement_context(context, repo_root)
    plan = _ask_llm_for_improvements(
        context=context_text,
        task_id=task_id,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
    )
    return plan.improvements


def _build_improvement_context(context: dict[str, Any], repo_root: Path) -> str:
    """Build context for LLM analysis."""
    del repo_root
    parts: list[str] = []

    if "task_id" in context:
        parts.append(f"Task: {context['task_id']}")

    if "eval" in context:
        eval_data = context["eval"]
        parts.append("\n**Recent Eval:**")
        parts.append(f"- Score: {eval_data.get('overall_score', 'N/A')}")
        parts.append(f"- Status: {eval_data.get('task_success', 'N/A')}")
        parts.append(f"- Quality: {eval_data.get('output_quality', 'N/A')}")
        parts.append(f"- Issues: {eval_data.get('manager_level_issues', [])}")

    if "comparison" in context:
        comparison = context["comparison"]
        parts.append("\n**Comparison:**")
        parts.append(f"- Score delta: {comparison.get('score_delta', 'N/A')}")
        parts.append(f"- Outcome: {comparison.get('overall_improvement', 'N/A')}")

    if "last_decision" in context:
        decision = context["last_decision"]
        parts.append("\n**Last Decision:**")
        parts.append(f"- Action: {decision.get('action_type', 'N/A')}")
        parts.append(f"- Reason: {decision.get('description', 'N/A')}")

    inspection = (
        context.get("inspection", {})
        if isinstance(context.get("inspection"), dict)
        else {}
    )
    manifest = (
        inspection.get("manifest", {})
        if isinstance(inspection.get("manifest"), dict)
        else {}
    )
    if manifest:
        parts.append("\n**Latest Run Manifest:**")
        parts.append(f"- Status: {manifest.get('status', 'N/A')}")
        parts.append(f"- Errors: {manifest.get('errors', [])}")
        parts.append(f"- Warnings: {manifest.get('warnings', [])}")
        parts.append(f"- Final answer: {manifest.get('final_answer', 'N/A')}")

    log_summary = (
        inspection.get("log_summary", {})
        if isinstance(inspection.get("log_summary"), dict)
        else {}
    )
    if log_summary:
        parts.append("\n**Log Summary:**")
        parts.append(f"- Status: {log_summary.get('status', 'N/A')}")
        parts.append(f"- Error count: {log_summary.get('error_count', 0)}")
        parts.append(f"- Warning count: {log_summary.get('warning_count', 0)}")
        tail_lines = log_summary.get("tail", [])
        if isinstance(tail_lines, list) and tail_lines:
            parts.append("- Recent lines:")
            for line in tail_lines[-5:]:
                parts.append(f"  - {str(line)[:240]}")

    error_signatures: list[str] = []
    for source in (
        context.get("error_signatures"),
        inspection.get("error_signatures"),
        context.get("run_errors"),
    ):
        if isinstance(source, list):
            for item in source:
                text = str(item).strip()
                if text and text not in error_signatures:
                    error_signatures.append(text)

    if error_signatures:
        parts.append("\n**Error Signatures:**")
        for signature in error_signatures[:12]:
            parts.append(f"- {signature}")

    raw_tail = inspection.get("raw_tail")
    if isinstance(raw_tail, list) and raw_tail:
        parts.append("\n**Raw Log Tail (truncated):**")
        for line in raw_tail[-_MAX_RAW_TAIL_LINES:]:
            parts.append(f"- {str(line)[:240]}")

    if "instance_path" in context:
        instance_path = Path(context["instance_path"])
        if instance_path.exists():
            parts.append(f"\n**Instance:** {instance_path}")
            mutable_roots, pinned_files = _load_workspace_mutable_surfaces(
                instance_path
            )
            if mutable_roots or pinned_files:
                parts.append("\n**Mutable Surfaces:**")
                if mutable_roots:
                    parts.append("- Roots: " + ", ".join(mutable_roots))
                if pinned_files:
                    parts.append("- Key files: " + ", ".join(pinned_files))

            modifiable_files = _collect_modifiable_files(instance_path)
            if modifiable_files:
                parts.append("\n**Concrete Modifiable Files:**")
                for file_path in modifiable_files:
                    parts.append(f"- {file_path}")

    return "\n".join(parts)


def _ask_llm_for_improvements(
    context: str,
    task_id: str,
    llm_model: str,
    llm_api_key: str,
    llm_base_url: str | None,
) -> ImprovementPlan:
    """Ask an LLM to generate an improvement plan."""
    import httpx

    prompt = f"""Analyze this performance and suggest specific code improvements:

{context}

**Your task:**
Identify 1-3 specific improvements that would make the system work better.
If the context shows concrete errors, failed imports, missing tools, or bad runtime limits,
prioritize fixing the causal files before suggesting generic tuning.
Focus on CONCRETE changes to:
- Agent configurations (`temperature`, `max_tokens`, `tool_choice`, `model`, `tools`)
- Tool allowlists and limits (for example `tools/allowlist.toml`)
- Workspace/runtime configuration (`workspace.toml`, `policies.toml`, model budgets)
- Prompts (system prompts, task descriptions)
- Graph topology (edges, conditions)
- Workspace code and execution scripts (`experiments/*.py`, `src/**/*.py`, lightweight UI/interface files)

Rules:
- Only modify files inside the active instance root.
- Prefer the smallest change that directly addresses the observed failure or bottleneck.
- It is valid to fix code errors, import/path issues, misconfigured tools, weak prompts, token limits, or missing interface/workspace integration.

For each improvement, provide:
1. file_path: Which file to modify (relative to instance root)
2. original_content: Current content (or relevant section)
3. improved_content: New content
4. description: What this changes and why
5. change_type: Type of change

Respond ONLY with valid JSON in this format:
{{
  "reasoning": "Brief explanation of your analysis",
  "expected_impact": "What impact these changes should have",
  "improvements": [
    {{
      "file_path": "agents/article_writer.toml",
      "original_content": "...",
      "improved_content": "...",
      "description": "Increase temperature for more creativity",
      "change_type": "agent_config"
    }}
  ]
}}
"""

    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert AI system optimizer. Analyze performance data "
                "and suggest specific, testable code improvements."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{get_openai_base_url(llm_base_url)}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": llm_model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 3000,
                },
            )
            response.raise_for_status()
            result = response.json()
    except Exception as exc:
        log.warning("LLM improvement request failed: %s", exc)
        return ImprovementPlan(
            task_id=task_id,
            issue_description="LLM analysis failed",
            improvements=[],
        )

    content = result["choices"][0]["message"]["content"]
    return _parse_improvement_response(content)


def _parse_improvement_response(content: str) -> ImprovementPlan:
    """Parse LLM response into ImprovementPlan."""
    import re

    json_match = re.search(r"\{[\s\S]*\}", content)
    if not json_match:
        log.warning("No JSON found in LLM response")
        return ImprovementPlan(
            task_id="unknown", issue_description="Parse failed", improvements=[]
        )

    try:
        data = json.loads(json_match.group())
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("Failed to parse improvement response: %s", exc)
        return ImprovementPlan(
            task_id="unknown", issue_description="Parse failed", improvements=[]
        )

    improvements = [
        CodeImprovement(
            file_path=imp_data.get("file_path", ""),
            original_content=imp_data.get("original_content", ""),
            improved_content=imp_data.get("improved_content", ""),
            description=imp_data.get("description", ""),
            change_type=imp_data.get("change_type", "unknown"),
        )
        for imp_data in data.get("improvements", [])
    ]

    return ImprovementPlan(
        task_id="unknown",
        issue_description=data.get("reasoning", "")[:500],
        improvements=improvements,
        reasoning=data.get("reasoning", ""),
        expected_impact=data.get("expected_impact", ""),
    )


def apply_improvements(
    improvements: list[CodeImprovement],
    instance_path: Path,
) -> dict[str, Any]:
    """Apply improvements to the active instance workspace."""
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for improvement in improvements:
        try:
            target_path = (instance_path / improvement.file_path).resolve()
            backup_path: Path | None = None

            if not str(target_path).startswith(str(instance_path)):
                log.warning(
                    "Security: Skipping %s (outside instance)", improvement.file_path
                )
                failed.append(
                    {"file": improvement.file_path, "error": "Outside instance"}
                )
                continue

            if target_path.exists():
                backup_path = target_path.with_suffix(
                    f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
                )
                import shutil

                shutil.copy2(target_path, backup_path)
                log.info("Backed up %s to %s", target_path, backup_path)

            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(improvement.improved_content, encoding="utf-8")

            applied.append(
                {
                    "file": improvement.file_path,
                    "description": improvement.description,
                    "change_type": improvement.change_type,
                    "backup": str(backup_path) if backup_path else None,
                }
            )
            log.info(
                "Applied improvement to %s: %s",
                improvement.file_path,
                improvement.description,
            )
        except Exception as exc:
            log.warning(
                "Failed to apply improvement to %s: %s", improvement.file_path, exc
            )
            failed.append({"file": improvement.file_path, "error": str(exc)})

    summary = {
        "timestamp": datetime.now().isoformat(),
        "applied_count": len(applied),
        "failed_count": len(failed),
        "applied": applied,
        "failed": failed,
        "changed_files": [entry["file"] for entry in applied],
    }

    summary_file = instance_path / "umbrella_improvements.jsonl"
    with open(summary_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary) + "\n")

    return summary


def improve_system_from_context(
    task_id: str,
    instance_path: Path,
    repo_root: Path,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Analyze the current state and apply improvements to the active instance."""
    log.info("Analyzing for improvements on task %s", task_id)

    improvements = analyze_and_improve(task_id, context, repo_root)
    if not improvements:
        log.info("No improvements generated")
        return {
            "applied_count": 0,
            "failed_count": 0,
            "applied": [],
            "failed": [],
            "changed_files": [],
        }

    log.info("Applying %s improvements", len(improvements))
    summary = apply_improvements(improvements, instance_path)

    log.info(
        "Improvement summary: %s applied, %s failed. Promotion is deferred until the "
        "instance proves the change in the normal evaluation cycle.",
        summary["applied_count"],
        summary["failed_count"],
    )
    return summary
