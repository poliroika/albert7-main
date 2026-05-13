"""
Meta-Harness runner.

Runs the outer optimization loop: for each iteration, launches Ouroboros
to propose a harness change, evaluates the candidate on a search set,
and decides whether to promote.

Usage:
    uv run python run_meta_harness.py --workspace agent_research --iterations 5
    uv run python run_meta_harness.py --experiment latest --resume
    uv run python run_meta_harness.py --evaluate-only <candidate_id>
"""

import logging
import sys
import time
from pathlib import Path

_repo_root = str(Path(__file__).resolve().parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

from umbrella.env import load_env, get_llm_env_config

repo_root = Path(__file__).resolve().parent
load_env(repo_root=repo_root)

llm_model, llm_api_key, llm_base_url = get_llm_env_config()
if not llm_api_key:
    raise RuntimeError("No LLM API key found in .env - set LLM_API_KEY")

from umbrella.config import load_runtime_config
from umbrella.control_plane.ouroboros_integration import run_ouroboros_improvement_sync
from umbrella.meta_harness.evaluator import evaluate_candidate_on_search_set
from umbrella.meta_harness.models import ExperimentStatus, MetaPromotionEligibility
from umbrella.meta_harness.promotion import apply_candidate_patch, decide_candidate_promotion
from umbrella.meta_harness.search_sets import (
    build_search_set_from_memory,
    build_search_set_from_workspaces,
    load_search_set,
    merge_search_sets,
)
from umbrella.meta_harness.store import get_default_store
from umbrella.orchestration.ouroboros_task import read_workspace_task


def _build_proposer_prompt(
    *,
    repo_root: Path,
    workspace_id: str,
    experiment_id: str,
    iteration: int,
    task_text: str,
    meta_harness_context: str,
) -> str:
    prompt_path = repo_root / "umbrella" / "prompts" / "meta_harness_proposer.md"
    if not prompt_path.exists():
        return task_text

    template = prompt_path.read_text(encoding="utf-8")
    return template.format(
        repo_root=str(repo_root),
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        iteration=iteration,
        task_text=task_text,
        meta_harness_context=meta_harness_context,
    )


def _build_meta_context(store, experiment_id: str) -> str:
    """Build compact context from previous candidates."""
    lines = []
    try:
        pairs = store.top_candidates(experiment_id, n=5, sort_by="score")
        if pairs:
            lines.append("### Previous Candidates (top by score)")
            for cand, ev in pairs:
                score = f"{ev.avg_score:.3f}" if ev else "n/a"
                lines.append(
                    f"- {cand.candidate_id}: score={score} status={cand.run_status} "
                    f"writes={cand.write_calls}"
                )

        failures = store.get_failures(experiment_id)
        if failures:
            lines.append("\n### Failed Candidates (do not repeat)")
            for cand, ev in failures[:5]:
                lines.append(f"- {cand.candidate_id}: {cand.error[:200]}")
    except Exception:
        lines.append("_Previous experience unavailable._")

    return "\n".join(lines) if lines else "_No previous candidates._"


def run_meta_harness(
    workspace_id: str = "agent_research",
    max_iterations: int = 5,
    quality_threshold: float = 0.70,
    search_set_path: Path | None = None,
    heldout_set_path: Path | None = None,
    timeout_hours: float = 24.0,
    max_budget_usd: float | None = None,
    resume_experiment: str | None = None,
    evaluate_only: str | None = None,
) -> None:
    """Run the Meta-Harness optimization loop."""
    store = get_default_store(repo_root)

    # Load or build search set
    if search_set_path and search_set_path.exists():
        search_set = load_search_set(search_set_path)
    else:
        memory_set = build_search_set_from_memory(repo_root, limit=15)
        workspace_set = build_search_set_from_workspaces(repo_root, limit=10)
        search_set = merge_search_sets(memory_set, workspace_set, name="auto_search_set")

    heldout_set = None
    if heldout_set_path and heldout_set_path.exists():
        heldout_set = load_search_set(heldout_set_path)

    # Evaluate-only mode
    if evaluate_only:
        log.info("Evaluate-only mode for candidate %s", evaluate_only)
        evaluation = evaluate_candidate_on_search_set(
            repo_root, evaluate_only, search_set, store=store,
        )
        log.info("Evaluation complete: avg_score=%.3f", evaluation.avg_score)
        return

    # Get or create experiment
    if resume_experiment:
        experiment = store.get_experiment(resume_experiment)
        if experiment is None:
            log.error("Experiment %s not found", resume_experiment)
            return
    else:
        experiment = store.get_or_create_experiment(
            name=f"meta_harness_{workspace_id}",
            workspace_id=workspace_id,
            search_set=search_set,
            heldout_set=heldout_set,
            max_iterations=max_iterations,
            max_budget_usd=max_budget_usd or 0.0,
        )

    load_runtime_config(overrides={
        "max_iterations": None,
        "max_duration_seconds": None,
        "max_budget_usd": max_budget_usd,
        "quality_completion_threshold": quality_threshold,
        "human_review_stages": [],
        "human_review_timeout_seconds": 0,
    })

    workspace_path = repo_root / "workspaces" / workspace_id
    task_text = read_workspace_task(workspace_path)
    timeout_seconds = None if timeout_hours <= 0 else timeout_hours * 3600

    log.info("=" * 70)
    log.info("META-HARNESS OPTIMIZATION LOOP")
    log.info("=" * 70)
    log.info("Experiment: %s", experiment.id)
    log.info("Workspace: %s", workspace_id)
    log.info("Search set: %d tasks", search_set.size)
    log.info("Max iterations: %d", max_iterations)
    log.info("=" * 70)

    total_cost = 0.0

    try:
        for iteration in range(1, max_iterations + 1):
            if max_budget_usd and total_cost >= max_budget_usd:
                log.info("Budget exhausted: $%.2f / $%.2f", total_cost, max_budget_usd)
                break

            log.info("")
            log.info("=" * 70)
            log.info("ITERATION %d / %d", iteration, max_iterations)
            log.info("=" * 70)

            meta_context = _build_meta_context(store, experiment.id)
            task_prompt = _build_proposer_prompt(
                repo_root=repo_root,
                workspace_id=workspace_id,
                experiment_id=experiment.id,
                iteration=iteration,
                task_text=task_text,
                meta_harness_context=meta_context,
            )

            start_time = time.time()
            result = run_ouroboros_improvement_sync(
                repo_root=repo_root,
                task_description=task_prompt,
                workspace_id=workspace_id,
                use_live_llm=True,
                timeout_seconds=timeout_seconds,
                experiment_id=experiment.id,
                candidate_isolation=True,
                verify=True,
            )
            elapsed = time.time() - start_time

            cost = float(result.get("cost_usd", 0) or 0)
            total_cost += cost
            candidate_id = result.get("candidate_id")

            log.info("Iteration %d: status=%s elapsed=%.1fs cost=$%.4f candidate=%s",
                     iteration, result.get("status"), elapsed, cost, candidate_id)

            # Evaluate candidate
            if candidate_id:
                log.info("Evaluating candidate %s on search set...", candidate_id)
                search_eval = evaluate_candidate_on_search_set(
                    repo_root, candidate_id, search_set, store=store,
                )
                log.info("Search eval: avg_score=%.3f complete=%d/%d",
                         search_eval.avg_score, search_eval.tasks_complete, search_eval.tasks_total)

                # Held-out evaluation for promising candidates
                heldout_eval = None
                if heldout_set and search_eval.avg_score > 0.5:
                    log.info("Evaluating on held-out set...")
                    heldout_eval = evaluate_candidate_on_search_set(
                        repo_root, candidate_id, heldout_set, store=store,
                    )
                    log.info("Held-out eval: avg_score=%.3f", heldout_eval.avg_score)

                # Promotion decision
                decision = decide_candidate_promotion(
                    repo_root,
                    candidate_id,
                    baseline_candidate_id=experiment.baseline_candidate_id or None,
                    search_eval=search_eval,
                    heldout_eval=heldout_eval,
                    store=store,
                )
                log.info("Promotion decision: %s - %s", decision.decision, decision.reasoning)

                if decision.decision == MetaPromotionEligibility.PROMOTE:
                    applied = apply_candidate_patch(repo_root, candidate_id, store=store)
                    if applied:
                        log.info("Applied promoted candidate %s patch to worktree", candidate_id)
                    else:
                        log.warning("Candidate %s promoted but patch apply failed", candidate_id)
                    if search_eval.avg_score > experiment.best_score:
                        experiment.best_score = search_eval.avg_score
                        experiment.best_candidate_id = candidate_id

            experiment.iterations_completed = iteration
            experiment.total_cost_usd = total_cost
            store.update_experiment(experiment)

            time.sleep(2)

    except KeyboardInterrupt:
        log.info("Stopped by user after %d iterations", experiment.iterations_completed)

    experiment.status = ExperimentStatus.COMPLETED
    store.update_experiment(experiment)

    log.info("")
    log.info("=" * 70)
    log.info("META-HARNESS COMPLETE")
    log.info("Iterations: %d", experiment.iterations_completed)
    log.info("Best score: %.3f", experiment.best_score)
    log.info("Best candidate: %s", experiment.best_candidate_id)
    log.info("Total cost: $%.2f", total_cost)
    log.info("=" * 70)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Meta-Harness optimization runner")
    parser.add_argument("--workspace", default="agent_research", help="Workspace to optimize for")
    parser.add_argument("--iterations", type=int, default=5, help="Max iterations")
    parser.add_argument("--quality-threshold", type=float, default=0.70)
    parser.add_argument("--search-set", type=Path, default=None, help="Path to search set JSON")
    parser.add_argument("--heldout-set", type=Path, default=None, help="Path to held-out set JSON")
    parser.add_argument("--timeout-hours", type=float, default=24.0)
    parser.add_argument("--max-budget", type=float, default=None)
    parser.add_argument("--resume", type=str, default=None, help="Resume experiment ID")
    parser.add_argument("--evaluate-only", type=str, default=None, help="Evaluate a specific candidate")

    args = parser.parse_args()

    run_meta_harness(
        workspace_id=args.workspace,
        max_iterations=args.iterations,
        quality_threshold=args.quality_threshold,
        search_set_path=args.search_set,
        heldout_set_path=args.heldout_set,
        timeout_hours=args.timeout_hours,
        max_budget_usd=args.max_budget,
        resume_experiment=args.resume,
        evaluate_only=args.evaluate_only,
    )
