"""
Ouroboros continuous improvement runner.

Reads the workspace TASK_MAIN.md, renders the Ouroboros prompt, and runs
the agent in a loop until manual stop or budget exhausted.  After each
successful iteration the runner attempts auto-promotion of changed files
from the workspace back into the seed.
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any

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

log.info(f"Using LLM: {llm_model} @ {llm_base_url}")

from umbrella.config import load_runtime_config
from umbrella.control_plane.ouroboros_integration import run_ouroboros_improvement_sync
from umbrella.meta_harness.promotion import apply_candidate_patch
from umbrella.orchestration.ouroboros_task import (
    read_workspace_task,
    render_retry_prompt,
    render_workspace_prompt,
)


def _try_meta_harness_promotion(
    workspace_id: str,
    changes_made: list[str],
    result: dict,
) -> bool:
    """Gated promotion via Meta-Harness evaluation + decision.

    Runs a workspace-candidate evaluation *before* the promotion decision
    so the gate has a real score to work with.
    """
    candidate_id = result.get("candidate_id")
    if not candidate_id:
        log.info("No candidate_id in result; skipping meta-harness promotion")
        return False

    try:
        from umbrella.meta_harness.evaluator import evaluate_candidate_on_search_set
        from umbrella.meta_harness.models import MetaPromotionEligibility
        from umbrella.meta_harness.promotion import decide_candidate_promotion
        from umbrella.meta_harness.search_sets import (
            build_search_set_from_workspaces,
        )
        from umbrella.meta_harness.store import get_default_store

        store = get_default_store(repo_root)

        search_set = build_search_set_from_workspaces(repo_root, limit=10)
        search_eval = evaluate_candidate_on_search_set(
            repo_root, candidate_id, search_set, store=store,
        )
        log.info(
            "Pre-decision eval: avg_score=%.3f complete=%d/%d",
            search_eval.avg_score, search_eval.tasks_complete, search_eval.tasks_total,
        )

        decision = decide_candidate_promotion(
            repo_root,
            candidate_id,
            search_eval=search_eval,
            store=store,
        )
        log.info("Meta-harness promotion decision: %s - %s", decision.decision, decision.reasoning)

        if decision.decision == MetaPromotionEligibility.PROMOTE:
            applied = apply_candidate_patch(repo_root, candidate_id, store=store)
            if applied:
                log.info("Applied promoted candidate %s patch", candidate_id)
            else:
                _try_auto_promote(workspace_id, changes_made)
            return True
        return False
    except Exception as exc:
        log.warning("Meta-harness promotion check failed: %s", exc)
        return False


def _try_auto_promote(workspace_id: str, changes_made: list[str]) -> None:
    """Best-effort promotion of changed workspace files into the seed."""
    if not changes_made:
        return
    try:
        from umbrella.evals.promotion import promote_changed_files_to_seed

        seed_path = repo_root / "workspaces" / workspace_id
        if not seed_path.exists():
            log.warning("Seed path not found for promotion: %s", seed_path)
            return

        # If the agent wrote directly into the seed (no separate instance
        # workspace), source==target and shutil.copy2 fails with
        # "are the same file".  Nothing to copy in that case.
        instance_path = seed_path
        try:
            if seed_path.resolve() == instance_path.resolve():
                log.info(
                    "Auto-promotion skipped: workspace already lives at seed path %s",
                    seed_path,
                )
                return
        except OSError:
            pass

        # ``changes_made`` may carry repo-relative paths (e.g.
        # ``workspaces/<id>/main.py``), workspace-relative paths
        # (``main.py``) or even absolute paths. Normalize everything to
        # an absolute path so the downstream normalizer can take a clean
        # ``relative_to(instance_root)`` instead of producing the
        # ``workspaces/<id>/workspaces/<id>/...`` doubling we used to see
        # in the warning logs.
        changed_paths: list[Path] = []
        for raw in changes_made:
            candidate = Path(raw)
            if candidate.is_absolute():
                changed_paths.append(candidate)
                continue
            repo_relative = repo_root / candidate
            workspace_relative = seed_path / candidate
            if repo_relative.exists() and not workspace_relative.exists():
                changed_paths.append(repo_relative)
            else:
                changed_paths.append(workspace_relative)

        promoted = promote_changed_files_to_seed(
            seed_path=seed_path,
            instance_path=seed_path,
            changed_files=changed_paths,
        )
        if promoted:
            log.info("Auto-promoted %d file(s) to seed", len(promoted))
        else:
            log.info("No files eligible for promotion")
    except Exception as exc:
        log.warning("Auto-promotion failed (non-fatal): %s", exc)


def _failed_verification_signature(result: dict[str, Any]) -> tuple[Any, ...]:
    warnings = tuple(sorted(str(w) for w in (result.get("completion_warnings") or [])))
    verification_report = result.get("verification_report") or {}
    failed_steps: list[str] = []
    for item in verification_report.get("results") or []:
        if not isinstance(item, dict):
            continue
        if bool(item.get("optional")):
            continue
        status = str(item.get("status") or "").lower()
        if status in {"failed", "skipped"}:
            failed_steps.append(str(item.get("name") or "?"))
    if failed_steps:
        return ("failed_steps", tuple(sorted(failed_steps)))
    if warnings:
        return ("warnings", warnings)
    summary = str(verification_report.get("summary") or "").strip()
    if summary:
        return ("summary", summary)
    return ("status_only", "failed_verification")


def _verification_failure_is_repairable_config(result: dict[str, Any]) -> bool:
    verification_report = result.get("verification_report") or {}
    if not isinstance(verification_report, dict):
        return False
    if verification_report.get("repairable") or verification_report.get("spec_error"):
        return True
    summary = str(verification_report.get("summary") or "").lower()
    return (
        "verification spec is invalid" in summary
        or "no verification steps declared or auto-detected" in summary
    )


def continuous_improvement_loop(
    workspace_id: str = "agent_research",
    max_iterations: int | None = None,
    quality_threshold: float = 0.70,
    auto_promote: bool = True,
    max_budget_usd: float | None = None,
    timeout_hours: float = 24.0,
    promotion_mode: str = "gated",
    verify: bool = True,
    max_verify_retries: int = 20,
    stop_on_verified: bool = True,
) -> None:
    """Run Ouroboros improvement loop driven by TASK_MAIN.md.

    When ``verify`` is True, each iteration runs runtime verification after
    Ouroboros stops and retries (feeding the failure report back into the
    prompt) up to ``max_verify_retries`` times.  Promotion is only attempted
    if the final status of an iteration is ``verified``.

    By default the loop exits immediately after the first verified iteration
    (``stop_on_verified=True``) to avoid endless reruns after success.
    """

    load_runtime_config(overrides={
        "max_iterations": None,
        "max_duration_seconds": None,
        "max_budget_usd": max_budget_usd,
        "quality_completion_threshold": quality_threshold,
        "human_review_stages": [],
        "human_review_timeout_seconds": 0,
        "self_improve_after_stalled_iterations": 2,
        "self_improve_max_total_iterations": 1000,
        "instance_cleanup_enabled": True,
        "keep_recent_runs_per_instance": 3,
    })

    workspace_path = repo_root / "workspaces" / workspace_id
    task_text = read_workspace_task(workspace_path)
    timeout_seconds = None if timeout_hours <= 0 else timeout_hours * 3600
    max_verify_retries = max(0, int(max_verify_retries))
    attempts_per_iteration = 1 if not verify else max_verify_retries + 1

    log.info("=" * 70)
    log.info("OUROBOROS CONTINUOUS IMPROVEMENT")
    log.info("=" * 70)
    log.info(f"Workspace: {workspace_id}")
    log.info(f"Quality threshold: {quality_threshold}")
    log.info(f"Auto-promote: {auto_promote}")
    log.info(f"Max budget: ${max_budget_usd or 'unlimited'}")
    log.info(f"Task source: TASK_MAIN.md ({len(task_text)} chars)")
    log.info("=" * 70)

    iteration = 0
    total_cost_usd = 0.0
    last_failed_signature: tuple[Any, ...] | None = None
    repeated_failed_count = 0

    try:
        while True:
            if max_iterations is not None and iteration >= max_iterations:
                log.info(f"Reached max iterations ({max_iterations})")
                break

            if max_budget_usd is not None and total_cost_usd >= max_budget_usd:
                log.info(f"Budget exhausted: ${total_cost_usd:.2f} / ${max_budget_usd:.2f}")
                break

            iteration += 1
            log.info("")
            log.info("=" * 70)
            log.info(f"ITERATION {iteration}")
            log.info("=" * 70)

            start_time = time.time()

            result = None
            previous_status = ""
            previous_verification_report: dict | None = None
            previous_final_message = ""

            for attempt in range(1, attempts_per_iteration + 1):
                retry_context = render_retry_prompt(
                    attempt=attempt,
                    max_attempts=attempts_per_iteration,
                    previous_status=previous_status,
                    verification_report=previous_verification_report,
                    previous_final_message=previous_final_message,
                )
                task_prompt = render_workspace_prompt(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_text=task_text,
                    quality_threshold=quality_threshold,
                    retry_context=retry_context,
                )

                log.info(
                    "Iteration %d attempt %d/%d", iteration, attempt, attempts_per_iteration
                )
                result = run_ouroboros_improvement_sync(
                    repo_root=repo_root,
                    task_description=task_prompt,
                    workspace_id=workspace_id,
                    use_live_llm=True,
                    timeout_seconds=timeout_seconds,
                    verify=verify,
                )

                if not isinstance(result, dict):
                    break
                status_attempt = result.get("status", "unknown")
                if status_attempt == "verified" or (not verify and status_attempt == "complete"):
                    break
                if status_attempt == "failed_verification" and attempt < attempts_per_iteration:
                    previous_status = status_attempt
                    previous_verification_report = result.get("verification_report")
                    previous_final_message = str(result.get("final_message") or "")
                    continue
                break

            elapsed = time.time() - start_time
            cost_usd = 0.0
            if isinstance(result, dict):
                cost_usd = float(result.get("cost_usd", 0) or 0)
            total_cost_usd += cost_usd

            log.info(f"Iteration {iteration} completed in {elapsed:.1f}s")
            log.info(f"Cost: ${cost_usd:.4f} | Total: ${total_cost_usd:.2f}")

            if isinstance(result, dict):
                status = result.get("status", "unknown")
                log.info(f"Status: {status}")

                promotable = status == "verified" or (not verify and status == "complete")
                if promotable:
                    changes = result.get("changes_made", [])
                    write_calls = result.get("workspace_write_tool_calls", 0)
                    log.info(f"Workspace write calls: {write_calls}, changes: {len(changes)}")

                    if status == "verified":
                        try:
                            from umbrella.memory.reflection import run_reflection_phase

                            reflection = run_reflection_phase(
                                repo_root=repo_root,
                                workspace_id=workspace_id,
                                task_id=str(result.get("task_id") or f"iter_{iteration}"),
                                verification_report=result.get("verification_report"),
                                tool_call_count=int(result.get("llm_tool_invocations") or 0),
                                final_message=str(result.get("final_message") or ""),
                                changes_made=[str(p) for p in changes],
                                critic_review=result.get("critic_review"),
                            )
                            log.info(
                                "Reflection phase status=%s lesson=%s skill=%s signal=%s",
                                reflection.status,
                                reflection.lesson_id or "-",
                                reflection.skill_slug or "-",
                                reflection.signal_id or "-",
                            )
                        except Exception as exc:
                            log.warning("Reflection phase failed (non-fatal): %s", exc)

                    if auto_promote and changes:
                        if promotion_mode == "gated":
                            _try_meta_harness_promotion(workspace_id, changes, result)
                        elif promotion_mode == "legacy":
                            _try_auto_promote(workspace_id, changes)
                elif status == "failed_verification":
                    log.warning(
                        "Iteration %d did not pass runtime verification; promotion skipped",
                        iteration,
                    )
                    signature = _failed_verification_signature(result)
                    if signature == last_failed_signature:
                        repeated_failed_count += 1
                    else:
                        repeated_failed_count = 1
                        last_failed_signature = signature
                    if repeated_failed_count >= 2 and not _verification_failure_is_repairable_config(result):
                        log.warning(
                            "Detected repeated failed-verification signature %s; "
                            "stopping loop to avoid verification livelock.",
                            signature,
                        )
                        break
                elif status == "error":
                    error = result.get("error", "unknown error")
                    log.warning(f"Error: {error}")
                    repeated_failed_count = 0
                    last_failed_signature = None
                else:
                    repeated_failed_count = 0
                    last_failed_signature = None

                if stop_on_verified and status == "verified":
                    log.info(
                        "Verified status reached on iteration %d; stopping improvement loop.",
                        iteration,
                    )
                    break

            time.sleep(2)

    except KeyboardInterrupt:
        log.info("")
        log.info("=" * 70)
        log.info("STOPPED BY USER")
        log.info(f"Completed {iteration} iterations")
        log.info(f"Total cost: ${total_cost_usd:.2f}")
        log.info("=" * 70)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ouroboros continuous improvement")
    parser.add_argument("--workspace", default="agent_research", help="Workspace to improve")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max iterations (default: unlimited)")
    parser.add_argument("--quality-threshold", type=float, default=0.70, help="Quality threshold")
    parser.add_argument("--max-budget", type=float, default=None, help="Max budget in USD")
    parser.add_argument("--timeout-hours", type=float, default=24.0, help="Timeout per iteration in hours")
    parser.add_argument("--no-auto-promote", action="store_true", help="Disable auto-promotion")
    parser.add_argument("--promotion-mode", choices=["legacy", "gated", "off"], default="gated",
                        help="Promotion mode: legacy (auto), gated (meta-harness), off (none)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip runtime verification (promotion gated only by self-report)")
    parser.add_argument("--max-verify-retries", type=int, default=20,
                        help="Retries per iteration when verification fails (default 20)")
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Keep running additional iterations after success. "
            "By default the loop stops immediately once verification passes."
        ),
    )

    args = parser.parse_args()

    effective_promote = not args.no_auto_promote
    effective_mode = args.promotion_mode
    if args.no_auto_promote:
        effective_mode = "off"

    continuous_improvement_loop(
        workspace_id=args.workspace,
        max_iterations=args.max_iterations,
        quality_threshold=args.quality_threshold,
        auto_promote=effective_promote,
        max_budget_usd=args.max_budget,
        timeout_hours=args.timeout_hours,
        promotion_mode=effective_mode,
        verify=not args.no_verify,
        max_verify_retries=args.max_verify_retries,
        stop_on_verified=not args.continuous,
    )
