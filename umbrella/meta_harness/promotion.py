"""Gated promotion for Meta-Harness candidates.

A candidate is promoted only when:
- Search-set score delta >= threshold
- No critical held-out regression
- Validation commands pass
- No suspicious hardcode detected
- Raw traces and diagnosis exist
- gmas/ changes require human approval
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path

from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
    CandidateStatus,
    MetaPromotionDecision,
    MetaPromotionEligibility,
)
from umbrella.meta_harness.store import MetaHarnessStore, get_default_store

log = logging.getLogger(__name__)

SCORE_DELTA_THRESHOLD = 0.05
HELDOUT_REGRESSION_THRESHOLD = -0.10

GMAS_PATHS = ("gmas/",)

HARDCODE_PATTERNS = [
    re.compile(r"market_id\s*=\s*['\"]0x[a-f0-9]+", re.IGNORECASE),
    re.compile(r"/workspaces/[a-z_]+/instances/[a-z0-9_]+", re.IGNORECASE),
    re.compile(r"answer\s*=\s*['\"][^'\"]{50,}", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Hardcode detection
# ---------------------------------------------------------------------------


def _extract_added_lines(diff_text: str) -> str:
    """Return only ``+`` lines from a unified diff (actual new code)."""
    added: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return "\n".join(added)


def _detect_suspicious_hardcode(
    changed_files: list[str],
    candidate_dir: Path | None,
) -> list[str]:
    """Scan added lines in diffs for hardcoded values that suggest overfitting."""
    suspicious: list[str] = []

    if candidate_dir is None:
        return suspicious

    diff_path = candidate_dir / "diffs" / "worktree.diff"
    if not diff_path.exists():
        return suspicious

    try:
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return suspicious

    added_content = _extract_added_lines(diff_text)
    if not added_content:
        return suspicious

    for pattern in HARDCODE_PATTERNS:
        matches = pattern.findall(added_content)
        if matches:
            suspicious.append(
                f"Pattern '{pattern.pattern}' found {len(matches)} time(s) in added lines"
            )

    return suspicious


def _check_gmas_changes(changed_files: list[str]) -> list[str]:
    """Return files that touch gmas/ (requires human approval)."""
    blocked = []
    for f in changed_files:
        normalized = f.replace("\\", "/")
        for prefix in GMAS_PATHS:
            if normalized.startswith(prefix):
                blocked.append(f)
                break
    return blocked


def _check_scope(changed_files: list[str]) -> bool:
    """Verify changed files are within allowed harness/workspace scope."""
    allowed_prefixes = (
        "umbrella/",
        "ouroboros/",
        "workspaces/",
        "run_",
        "docs/",
        "tests/",
    )
    for f in changed_files:
        normalized = f.replace("\\", "/")
        if not any(normalized.startswith(p) for p in allowed_prefixes):
            if normalized not in ("pyproject.toml", "README.md"):
                return False
    return True


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


def decide_candidate_promotion(
    repo_root: Path,
    candidate_id: str,
    *,
    baseline_candidate_id: str | None = None,
    search_eval: CandidateEval | None = None,
    heldout_eval: CandidateEval | None = None,
    store: MetaHarnessStore | None = None,
) -> MetaPromotionDecision:
    """Decide whether a candidate should be promoted."""
    if store is None:
        store = get_default_store(repo_root)

    candidate = store.find_candidate(candidate_id)
    if candidate is None:
        return MetaPromotionDecision(
            candidate_id=candidate_id,
            decision=MetaPromotionEligibility.INSUFFICIENT_DATA,
            reasoning="Candidate not found",
        )

    if search_eval is None:
        search_eval = store.get_eval(candidate_id)

    evidence: list[str] = []
    candidate_dir = store.find_candidate_dir(candidate_id)

    # 1. Score threshold
    baseline_score = 0.0
    if baseline_candidate_id:
        baseline_eval = store.get_eval(baseline_candidate_id)
        if baseline_eval:
            baseline_score = baseline_eval.avg_score

    candidate_score = search_eval.avg_score if search_eval else 0.0
    score_delta = candidate_score - baseline_score
    passes_score = score_delta >= SCORE_DELTA_THRESHOLD or (
        not baseline_candidate_id and candidate_score > 0.5
    )
    evidence.append(
        f"Score delta: {score_delta:.3f} (threshold: {SCORE_DELTA_THRESHOLD})"
    )

    # 2. Held-out check
    passes_heldout = True
    if heldout_eval:
        if baseline_candidate_id:
            heldout_baseline = store.get_eval(baseline_candidate_id)
            if heldout_baseline:
                heldout_delta = heldout_eval.avg_score - heldout_baseline.avg_score
                if heldout_delta < HELDOUT_REGRESSION_THRESHOLD:
                    passes_heldout = False
                    evidence.append(f"Held-out regression: {heldout_delta:.3f}")
        evidence.append(f"Held-out score: {heldout_eval.avg_score:.3f}")

    # 3. Validation
    passes_validation = True
    if search_eval and search_eval.tasks_failed > search_eval.tasks_total * 0.5:
        passes_validation = False
        evidence.append(
            f"Too many failures: {search_eval.tasks_failed}/{search_eval.tasks_total}"
        )

    # 4. Hardcode audit
    suspicious = _detect_suspicious_hardcode(candidate.changed_files, candidate_dir)
    passes_hardcode = len(suspicious) == 0
    if suspicious:
        evidence.extend(suspicious)

    # 5. Scope audit
    blocked_gmas = _check_gmas_changes(candidate.changed_files)
    passes_scope = _check_scope(candidate.changed_files) and len(blocked_gmas) == 0
    if blocked_gmas:
        evidence.append(f"gmas/ changes require human approval: {blocked_gmas}")

    # 6. Traces exist
    has_traces = candidate_dir is not None and (candidate_dir / "execution").exists()
    if not has_traces:
        evidence.append("No execution traces found")

    # 7. Runtime verification gate
    passes_runtime_verification = True
    runtime_failed_tasks: list[str] = []
    if search_eval is not None:
        for task_result in search_eval.task_results:
            if task_result.status == "skipped":
                continue
            if task_result.runtime_verification_skipped:
                continue
            if not task_result.runtime_verification_passed:
                passes_runtime_verification = False
                runtime_failed_tasks.append(task_result.task_id)
                summary_line = (task_result.verification_summary or "").splitlines()
                preview = summary_line[0] if summary_line else "verification failed"
                evidence.append(
                    f"Runtime verification failed for {task_result.task_id}: {preview}"
                )
    if candidate.run_status in {"failed_verification", "failed_hygiene"}:
        passes_runtime_verification = False
        evidence.append(f"Candidate self-reported {candidate.run_status}")

    # Decision
    if blocked_gmas:
        decision = MetaPromotionEligibility.NEEDS_REVIEW
        reasoning = "Changes touch gmas/ - requires human approval"
    elif not passes_runtime_verification:
        decision = MetaPromotionEligibility.REJECT
        reasoning = (
            "Runtime verification failed for task(s): "
            + ", ".join(runtime_failed_tasks)
            if runtime_failed_tasks
            else "Runtime verification failed"
        )
    elif not passes_score:
        decision = MetaPromotionEligibility.REJECT
        reasoning = (
            f"Score delta {score_delta:.3f} below threshold {SCORE_DELTA_THRESHOLD}"
        )
    elif not passes_heldout:
        decision = MetaPromotionEligibility.REJECT
        reasoning = "Held-out regression detected"
    elif not passes_validation:
        decision = MetaPromotionEligibility.REJECT
        reasoning = "Too many task failures"
    elif not passes_hardcode:
        decision = MetaPromotionEligibility.REJECT
        reasoning = f"Suspicious hardcode patterns: {suspicious}"
    elif not passes_scope:
        decision = MetaPromotionEligibility.REJECT
        reasoning = "Changed files outside allowed scope"
    elif not has_traces:
        decision = MetaPromotionEligibility.INSUFFICIENT_DATA
        reasoning = "No execution traces for causal diagnosis"
    else:
        decision = MetaPromotionEligibility.PROMOTE
        reasoning = f"Score improved by {score_delta:.3f}, all checks passed"

    promotion_decision = MetaPromotionDecision(
        candidate_id=candidate_id,
        decision=decision,
        reasoning=reasoning,
        evidence=evidence,
        score_delta=score_delta,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        passes_score_threshold=passes_score,
        passes_heldout_check=passes_heldout,
        passes_validation=passes_validation,
        passes_hardcode_audit=passes_hardcode,
        passes_scope_audit=passes_scope,
        passes_runtime_verification=passes_runtime_verification,
        suspicious_patterns=suspicious,
        blocked_files=blocked_gmas,
    )

    store.save_promotion_decision(promotion_decision)

    if decision == MetaPromotionEligibility.PROMOTE:
        candidate.status = CandidateStatus.PROMOTED
    elif decision == MetaPromotionEligibility.REJECT:
        candidate.status = CandidateStatus.REJECTED
    store.save_candidate(candidate)

    return promotion_decision


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------


def apply_candidate_patch(
    repo_root: Path,
    candidate_id: str,
    *,
    store: MetaHarnessStore | None = None,
) -> bool:
    """Apply a promoted candidate's stored diff to the live worktree.

    Reads the ``diffs/worktree.diff`` saved during candidate capture and
    applies it via ``git apply``.  Returns True on success.
    """
    if store is None:
        store = get_default_store(repo_root)

    cand_dir = store.find_candidate_dir(candidate_id)
    if cand_dir is None:
        log.warning(
            "apply_candidate_patch: candidate dir not found for %s", candidate_id
        )
        return False

    candidate = store.find_candidate(candidate_id)

    diff_path = cand_dir / "diffs" / "worktree.diff"
    if not diff_path.exists() or diff_path.stat().st_size == 0:
        return _apply_candidate_workspace_files(repo_root, candidate)

    try:
        from umbrella.meta_harness.capture import _DIFF_TRUNCATED_MARKER

        head = diff_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        head = ""
    except Exception:
        head = ""
    if _DIFF_TRUNCATED_MARKER and _DIFF_TRUNCATED_MARKER in head:
        log.warning(
            "apply_candidate_patch: stored diff for %s is marked truncated/unsafe; "
            "skipping git apply to avoid corrupt-patch errors.",
            candidate_id,
        )
        return _apply_candidate_workspace_files(repo_root, candidate)

    _run_kw = dict(
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    try:
        result = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=nowarn", str(diff_path)],
            **_run_kw,
        )
        if result.returncode == 0:
            log.info("apply_candidate_patch: successfully applied %s", candidate_id)
            return True

        log.warning(
            "git apply --3way failed (rc=%d) for %s, trying without --3way: %s",
            result.returncode,
            candidate_id,
            result.stderr[:500],
        )
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(diff_path)],
            **_run_kw,
        )
        if result.returncode == 0:
            log.info("apply_candidate_patch: applied (plain) %s", candidate_id)
            return True

        log.error(
            "apply_candidate_patch: git apply failed for %s: %s",
            candidate_id,
            result.stderr[:500],
        )
        return _apply_candidate_workspace_files(repo_root, candidate)
    except Exception:
        log.error("apply_candidate_patch crashed for %s", candidate_id, exc_info=True)
        return _apply_candidate_workspace_files(repo_root, candidate)


def _apply_candidate_workspace_files(
    repo_root: Path,
    candidate: CandidateManifest | None,
) -> bool:
    """Fallback promotion for harness candidates captured from workspace copies."""
    if candidate is None:
        return False
    instance_path = Path(str(candidate.instance_path or ""))
    if not instance_path.is_absolute():
        instance_path = repo_root / instance_path
    if not instance_path.exists() or not instance_path.is_dir():
        log.warning(
            "apply_candidate_patch: no diff and no candidate instance path for %s",
            candidate.candidate_id,
        )
        return False
    workspace_id = (
        str(candidate.workspace_id or "").strip().replace("\\", "/").strip("/")
    )
    if not workspace_id or ".." in Path(workspace_id).parts:
        return False
    live_root = (repo_root / "workspaces" / workspace_id).resolve()
    try:
        live_root.relative_to((repo_root / "workspaces").resolve())
    except ValueError:
        return False

    copied = 0
    for raw in candidate.changed_files:
        normalized = str(raw or "").replace("\\", "/")
        prefix = f"workspaces/{workspace_id}/"
        if not normalized.startswith(prefix):
            continue
        rel = normalized[len(prefix) :]
        if not rel or ".." in Path(rel).parts:
            continue
        source = (instance_path / rel).resolve()
        target = (live_root / rel).resolve()
        try:
            target.relative_to(live_root)
        except ValueError:
            continue
        if not source.exists() or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    if copied:
        log.info(
            "apply_candidate_patch: copied %d file(s) from isolated workspace for %s",
            copied,
            candidate.candidate_id,
        )
        return True
    log.warning(
        "apply_candidate_patch: no copyable files for %s", candidate.candidate_id
    )
    return False
