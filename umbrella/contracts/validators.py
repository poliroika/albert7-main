"""Contract validators used by Umbrella hard gates."""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_COMPLETION_UNRESOLVED_BLOCKER_RE = re.compile(
    r"(?is)"
    r"(?:\binfrastructure\s+blocker\b|"
    r"\bverification\s+blocked\b|"
    r"\bmissing\s+source\s+files?\b|"
    r"\bunresolved\s+blocker\b|"
    r"\bblockers?\s+(?:remain|present|still\s+open|blocking)\b|"
    r"\bcannot\s+(?:close|complete|mark)\b[^.]{0,80}\b(?:done|complete)\b)"
)

from umbrella.analysis.shell_commands import validate_argv
from umbrella.contracts.evidence import (
    EvidenceResolver,
    LEDGER_BACKED_EVIDENCE_REF_TYPES,
)
from umbrella.contracts.models import (
    CURRENT_CONTRACT_VERSION,
    CompletionContract,
    ContractBundle,
    ContractEnvelope,
    ContractIssue,
    IssueSeverity,
    PlanIR,
    ProofSpec,
    ReviewContract,
    SubtaskIR,
    TaskRiskProfile,
    VerificationReportRef,
    WorkspaceContext,
)
from umbrella.contracts.schemas import VALID_REVIEW_CODES
from umbrella.contracts.layout_policy import (
    is_python_implementation_path,
    validate_plan_layout_policy,
)


ALLOWED_SCHEMA_NAMES = {
    "plan",
    "review",
    "research_summary",
    "completion",
    "verification_report",
}
_CANDIDATE_CONTROL_DIRS = {
    ".git",
    ".memory",
    ".umbrella",
    ".umbrella_scratch",
}
_CANDIDATE_CONTROL_ROOT_FILES = {
    "workspace.toml",
    "verification.toml",
    "verify.sh",
}
_PRODUCTION_SOURCE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}
_REVIEW_CODES_REQUIRE_REVISION = {
    "missing_proof",
    "weak_proof",
    "manual_proof",
    "unavailable_proof_target",
    "test_tampering_risk",
    "scope_mismatch",
    "policy_violation",
    "insufficient_research_evidence",
    "requires_human_checkpoint",
    "stale_proof_ref",
    "fake_evidence_ref",
    "invalid_evidence_ref",
    "invalid_python_c_proof",
    "non_ledger_evidence_ref",
    "shell_operator_in_argv",
    "proof_after_patch_missing",
    "proof_scope_mismatch",
    "claim_without_proof",
    "test_tampering_detected",
    "verifier_mutation_attempt",
    "memory_without_verified_evidence",
    "legacy_contract_used",
    "llm_judge_only_evidence",
    "greenfield_python_src_layout_policy",
}


def validate_envelope(envelope: ContractEnvelope) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if envelope.schema_name not in ALLOWED_SCHEMA_NAMES:
        issues.append(
            ContractIssue(
                code="unknown_contract_schema",
                severity="blocking",
                phase=envelope.phase,
                message=f"Unknown contract schema `{envelope.schema_name}`.",
            )
        )
    if envelope.schema_version != CURRENT_CONTRACT_VERSION:
        issues.append(
            ContractIssue(
                code="unknown_contract_version",
                severity="blocking",
                phase=envelope.phase,
                message=(
                    f"Unsupported contract version `{envelope.schema_version}`; "
                    f"expected `{CURRENT_CONTRACT_VERSION}`."
                ),
            )
        )
    return issues


def _issue(
    code: str,
    message: str,
    *,
    phase: str = "",
    subtask_id: str = "",
    severity: IssueSeverity = "blocking",
) -> ContractIssue:
    return ContractIssue(
        code=code,
        severity=severity,
        phase=phase,
        subtask_id=subtask_id,
        message=message,
    )


def validate_proof_spec(
    proof: ProofSpec,
    *,
    phase: str = "",
    subtask_id: str = "",
    resolver: EvidenceResolver | None = None,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    execution = proof.execution
    oracle = proof.oracle
    scope = proof.scope
    anti = proof.anti_gaming
    for shell_issue in validate_argv(execution.command, shell=execution.shell):
        issues.append(
            _issue(
                shell_issue.code,
                shell_issue.message,
                phase=phase,
                subtask_id=subtask_id,
            )
        )
    if any("--collect-only" in part.lower() for part in execution.command):
        issues.append(_issue("collect_only_proof", "pytest --collect-only is not behavioral proof.", phase=phase, subtask_id=subtask_id))
    if anti.requires_real_runtime and anti.allows_mock:
        issues.append(_issue("real_runtime_proof_cannot_allow_mock", "Real runtime proof cannot allow mocks.", phase=phase, subtask_id=subtask_id))
    if execution.kind == "pytest" and not scope.pytest_targets:
        issues.append(_issue("missing_pytest_targets", "pytest proof requires explicit pytest_targets.", phase=phase, subtask_id=subtask_id))
    if execution.kind in {"input_sensitivity", "behavioral_http"}:
        has_signal = (
            oracle.input_sensitivity_required
            or "distinct_inputs_distinct_outputs" in oracle.required_properties
            or "invalid_input_rejected" in oracle.required_properties
        )
        if not has_signal:
            issues.append(_issue("missing_behavioral_oracle", "Behavioral proof requires input-sensitivity or negative-case properties.", phase=phase, subtask_id=subtask_id))
    if execution.kind == "mutation_smoke" and (
        oracle.oracle_type != "mutation_kill"
        and "mutation_killed" not in oracle.required_properties
    ):
        issues.append(_issue("missing_mutation_oracle", "mutation_smoke proof requires a mutation-kill oracle.", phase=phase, subtask_id=subtask_id))
    if proof.human_claims and not oracle.required_properties:
        issues.append(_issue("human_claims_without_machine_oracle", "Human-readable claims must be backed by machine-checkable required_properties.", phase=phase, subtask_id=subtask_id))
    if scope.files_under_test and scope.changed_files_expected:
        under_test = {item.replace("\\", "/") for item in scope.files_under_test}
        expected = {item.replace("\\", "/") for item in scope.changed_files_expected}
        if not (under_test & expected):
            issues.append(_issue("proof_scope_mismatch", "files_under_test must overlap changed_files_expected.", phase=phase, subtask_id=subtask_id))
    if resolver is not None and proof.evidence_refs:
        issues.extend(resolver.validate_refs(proof.evidence_refs, phase=phase, subtask_id=subtask_id))
    return issues


def validate_review_contract(review: ReviewContract, *, phase: str = "") -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if review.verdict not in {"ok", "revise", "abort"}:
        issues.append(_issue("invalid_review_verdict", "Review verdict must be ok/revise/abort.", phase=phase))
    for item in review.issues:
        if item.code not in VALID_REVIEW_CODES:
            issues.append(
                ContractIssue(
                    code="unknown_review_issue_code",
                    severity="blocking",
                    phase=phase or item.phase,
                    subtask_id=item.subtask_id,
                    message=f"Unknown review issue code `{item.code}`.",
                    evidence_refs=item.evidence_refs,
                )
            )
        if item.severity == "human_required" and item.code != "requires_human_checkpoint":
            issues.append(
                ContractIssue(
                    code="invalid_human_required_issue",
                    severity="blocking",
                    phase=phase or item.phase,
                    subtask_id=item.subtask_id,
                    message="human_required severity is reserved for explicit human checkpoints.",
                    evidence_refs=item.evidence_refs,
                )
            )
    ok_blockers = [
        item
        for item in review.issues
        if review.verdict == "ok"
        and (
            item.severity in {"error", "blocking", "human_required"}
            or item.code in _REVIEW_CODES_REQUIRE_REVISION
        )
    ]
    if ok_blockers:
        blocker = ok_blockers[0]
        issues.append(
            _issue(
                "review_ok_with_blocking_issue",
                (
                    "Review verdict `ok` cannot carry typed blocker "
                    f"`{blocker.code}`; use `revise`/`abort` or keep "
                    "nonblocking recommendations in notes."
                ),
                phase=phase or blocker.phase,
                subtask_id=blocker.subtask_id,
            )
        )
    if review.verdict in {"revise", "abort"} and not any(
        issue.severity in {"blocking", "human_required", "error"}
        for issue in review.issues
    ):
        issues.append(_issue("review_without_machine_issue", "revise/abort requires at least one typed error/blocking issue.", phase=phase))
    return issues


def _normalise_candidate_plan_path(value: str) -> str:
    text = str(value or "").strip().strip("`'\"")
    if not text:
        return ""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _is_windows_absolute_path(value: str) -> bool:
    return (
        len(value) >= 3
        and value[1] == ":"
        and value[0].isalpha()
        and value[2] in {"/", "\\"}
    )


def _candidate_plan_path_issue(path: str, *, phase: str, subtask_id: str) -> ContractIssue | None:
    normalised = _normalise_candidate_plan_path(path)
    if not normalised:
        return None
    if normalised.startswith("/") or _is_windows_absolute_path(normalised):
        return _issue(
            "candidate_path_outside_workspace",
            f"Candidate plan path `{path}` must be workspace-relative.",
            phase=phase,
            subtask_id=subtask_id,
        )
    parts = [part for part in normalised.split("/") if part]
    if any(part == ".." for part in parts):
        return _issue(
            "candidate_path_outside_workspace",
            f"Candidate plan path `{path}` must not escape the workspace.",
            phase=phase,
            subtask_id=subtask_id,
        )
    first = parts[0] if parts else ""
    if first in _CANDIDATE_CONTROL_DIRS:
        return _issue(
            "candidate_control_path_forbidden",
            f"Candidate plan path `{path}` targets supervisor/control state.",
            phase=phase,
            subtask_id=subtask_id,
        )
    if len(parts) == 1 and parts[0] in _CANDIDATE_CONTROL_ROOT_FILES:
        return _issue(
            "candidate_control_path_forbidden",
            f"Candidate plan path `{path}` is supervisor/evaluator configuration, not generated project work.",
            phase=phase,
            subtask_id=subtask_id,
        )
    return None


def validate_plan_candidate_paths(plan: PlanIR, *, phase: str = "plan") -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for subtask in plan.subtasks:
        declared_paths = [
            *subtask.files_to_change,
            *subtask.files_to_create,
        ]
        if subtask.proof is not None:
            declared_paths.extend(subtask.proof.scope.files_under_test)
            declared_paths.extend(subtask.proof.scope.changed_files_expected)
        for path in declared_paths:
            issue = _candidate_plan_path_issue(
                path,
                phase=phase,
                subtask_id=subtask.id,
            )
            if issue is not None:
                issues.append(issue)
    return issues


def _path_extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _is_production_source_path(path: str) -> bool:
    normalised = _normalise_candidate_plan_path(path)
    if not normalised:
        return False
    if not is_python_implementation_path(normalised):
        return False
    return _path_extension(normalised) in _PRODUCTION_SOURCE_EXTENSIONS


def _is_package_init_path(path: str) -> bool:
    normalised = _normalise_candidate_plan_path(path)
    return normalised == "src/__init__.py" or normalised.endswith("/__init__.py")


def validate_subtask_proof_strength(
    subtask: SubtaskIR,
    *,
    phase: str = "plan",
) -> list[ContractIssue]:
    if subtask.proof is None:
        return []
    declared_paths = (*subtask.files_to_create, *subtask.files_to_change)
    production_paths = [
        path
        for path in declared_paths
        if _is_production_source_path(path) and not _is_package_init_path(path)
    ]
    if production_paths and subtask.proof.execution.kind == "import_check":
        return [
            _issue(
                "weak_proof",
                (
                    "Production source leaves cannot use import-only proof. "
                    "Use pytest, input-sensitivity, metamorphic, mutation, "
                    "property, HTTP, or another behavioral oracle tied to the "
                    "changed source."
                ),
                phase=phase,
                subtask_id=subtask.id,
            )
        ]
    return []


def _completion_text_blobs(
    completion: CompletionContract,
    *,
    raw_completion: dict | None,
) -> str:
    parts: list[str] = [str(completion.notes or "")]
    if isinstance(raw_completion, dict):
        for key in ("summary", "notes", "status_notes"):
            value = raw_completion.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    for claim in completion.completed_claims:
        parts.append(str(claim.text or ""))
    return "\n".join(parts)


def _completion_has_unresolved_blocker_language(text: str) -> bool:
    blob = str(text or "").strip()
    if not blob:
        return False
    return bool(_COMPLETION_UNRESOLVED_BLOCKER_RE.search(blob))


def _normalised_string_list(value: Any) -> set[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    return {
        norm
        for item in values
        if (norm := _normalise_candidate_plan_path(str(item)))
    }


def validate_completion_materialization(
    completion: CompletionContract,
    *,
    active_subtask: dict | None,
    workspace_root: str,
    raw_completion: dict | None = None,
    phase: str = "",
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if active_subtask is None:
        return issues
    subtask_id = str(active_subtask.get("id") or completion.subtask_id or "")
    declared_created = _normalised_string_list(active_subtask.get("files_to_create"))
    declared_deleted = _normalised_string_list(active_subtask.get("files_to_delete"))
    declared: set[str] = set()
    for key in ("files_to_create", "files_to_change", "files_affected"):
        declared.update(_normalised_string_list(active_subtask.get(key)))
    proof = active_subtask.get("proof")
    if isinstance(proof, dict):
        scope = proof.get("scope")
        if isinstance(scope, dict):
            for key in ("files_under_test", "changed_files_expected"):
                items = scope.get(key)
                if isinstance(items, (list, tuple)):
                    for item in items:
                        norm = _normalise_candidate_plan_path(str(item))
                        if norm:
                            declared.add(norm)
    declared.update(declared_deleted)

    blockers = []
    if isinstance(raw_completion, dict):
        raw_blockers = raw_completion.get("blockers")
        if isinstance(raw_blockers, list):
            blockers = [str(item) for item in raw_blockers if str(item).strip()]
    status_done = str(completion.status or "").lower() in {"done", "ok", "complete"}
    if blockers and status_done:
        issues.append(
            _issue(
                "completion_blocked_not_done",
                "Completion cannot be marked done while blockers are present.",
                phase=phase,
                subtask_id=subtask_id,
            )
        )
    elif status_done and _completion_has_unresolved_blocker_language(
        _completion_text_blobs(completion, raw_completion=raw_completion)
    ):
        issues.append(
            _issue(
                "completion_blocked_not_done",
                "Completion cannot be marked done while notes or claims still describe "
                "unresolved blockers (for example infrastructure blocker, verification "
                "blocked, or missing source files). Use typed `blockers` or keep the "
                "subtask pending until the blocker is resolved.",
                phase=phase,
                subtask_id=subtask_id,
            )
        )

    deleted = set(completion.deleted_files)
    if isinstance(raw_completion, dict):
        for key in ("deleted_files", "removed_files", "expected_absent_files"):
            deleted.update(_normalised_string_list(raw_completion.get(key)))
    deleted.update(declared_deleted)
    deleted = {
        norm
        for path in deleted
        if (norm := _normalise_candidate_plan_path(str(path)))
    }
    root = Path(workspace_root) if workspace_root else None
    changed = [
        _normalise_candidate_plan_path(path)
        for path in completion.changed_files
        if _normalise_candidate_plan_path(path)
    ]
    if declared and changed:
        if not any(path in declared for path in changed):
            issues.append(
                _issue(
                    "scope_mismatch",
                    "Completion changed_files must overlap the active subtask declared scope.",
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        extra = sorted({path for path in changed if path not in declared})
        if extra:
            issues.append(
                _issue(
                    "scope_mismatch",
                    "Completion changed_files include paths outside the active subtask "
                    f"declared scope: {', '.join(extra)}.",
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )

    for path in active_subtask.get("files_to_create") or []:
        norm = _normalise_candidate_plan_path(str(path))
        if not norm or not root:
            continue
        target = root / norm
        if not target.is_file() or target.stat().st_size == 0:
            issues.append(
                _issue(
                    "subtask_materialization_missing",
                    f"Declared created file `{norm}` is missing or empty on disk.",
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )

    for path in deleted:
        if not root:
            continue
        target = root / path
        if target.exists():
            issues.append(
                _issue(
                    "subtask_materialization_present",
                    f"Completion declares file `{path}` deleted, but it still exists on disk.",
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )

    for path in changed:
        if not root or path in deleted or path not in declared_created:
            continue
        target = root / path
        if not target.is_file() or target.stat().st_size == 0:
            issues.append(
                _issue(
                    "subtask_materialization_missing",
                    f"Completion changed file `{path}` is missing or empty on disk.",
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
    return issues


def validate_done_subtasks_materialized(
    *,
    subtasks: list[Any],
    workspace_root: str,
    phase: str = "execute",
) -> list[ContractIssue]:
    """Block advancing execute when a done subtask still lacks declared files on disk."""
    issues: list[ContractIssue] = []
    root = Path(workspace_root) if workspace_root else None
    if root is None:
        return issues
    for item in subtasks:
        if isinstance(item, dict):
            status = str(item.get("status") or "").lower()
            subtask_id = str(item.get("id") or "")
            files_to_create = item.get("files_to_create") or []
        else:
            status = str(getattr(item, "status", "") or "").lower()
            subtask_id = str(getattr(item, "id", "") or "")
            files_to_create = getattr(item, "files_to_create", None) or []
        if status not in {"done", "ok", "complete"}:
            continue
        for path in files_to_create:
            norm = _normalise_candidate_plan_path(str(path))
            if not norm:
                continue
            target = root / norm
            if not target.is_file() or target.stat().st_size == 0:
                issues.append(
                    _issue(
                        "subtask_materialization_missing",
                        f"Subtask `{subtask_id}` is marked done but declared file `{norm}` is missing or empty.",
                        phase=phase,
                        subtask_id=subtask_id,
                    )
                )
    return issues


def validate_completion_contract(
    completion: CompletionContract,
    *,
    context: WorkspaceContext,
    phase: str = "",
) -> list[ContractIssue]:
    resolver = EvidenceResolver(context)
    issues: list[ContractIssue] = []
    if not completion.subtask_id:
        issues.append(_issue("missing_subtask_id", "CompletionContract.subtask_id is required.", phase=phase))
    if not completion.completed_claims:
        issues.append(_issue("claim_without_proof", "Completion requires at least one completed claim.", phase=phase, subtask_id=completion.subtask_id))
    all_refs = list(completion.evidence_refs)
    for claim in completion.completed_claims:
        if not claim.proof_refs:
            issues.append(_issue("claim_without_proof", f"Claim `{claim.claim_id}` has no proof refs.", phase=phase, subtask_id=completion.subtask_id))
        all_refs.extend(claim.proof_refs)
    if completion.verification_report is not None:
        issues.extend(
            resolver.validate_verification_report_ref(
                completion.verification_report,
                phase=phase,
                subtask_id=completion.subtask_id,
            )
        )
    elif not all_refs:
        issues.append(_issue("missing_evidence_ref", "Completion requires ledger-backed evidence refs.", phase=phase, subtask_id=completion.subtask_id))
    for ref in all_refs:
        if ref.ref_type not in LEDGER_BACKED_EVIDENCE_REF_TYPES:
            issues.append(
                _issue(
                    "non_ledger_evidence_ref",
                    (
                        "Completion proof refs must point to supervisor ledger-backed "
                        "evidence such as ledger_event, verification_report, test_run, "
                        f"mutation_report, or input_sensitivity_report; got `{ref.ref_type or '<missing>'}`."
                    ),
                    phase=phase,
                    subtask_id=completion.subtask_id,
                )
            )
    issues.extend(
        resolver.validate_refs(tuple(all_refs), phase=phase, subtask_id=completion.subtask_id)
    )
    return issues


def validate_verification_report_ref(
    report: VerificationReportRef,
    *,
    context: WorkspaceContext,
    phase: str = "verify",
) -> list[ContractIssue]:
    return EvidenceResolver(context).validate_verification_report_ref(report, phase=phase)


def validate_risk_profile(risk: TaskRiskProfile, proofs: Iterable[ProofSpec]) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    proof_list = list(proofs)
    kinds = {proof.execution.kind for proof in proof_list}
    if risk.verifier_changed:
        issues.append(
            ContractIssue(
                code="requires_human_checkpoint",
                severity="human_required",
                message="Verifier/policy changes require a human checkpoint.",
            )
        )
    if risk.tests_changed and not any(
        "no_test_tampering" in proof.oracle.required_properties for proof in proof_list
    ):
        issues.append(_issue("test_tampering_risk", "tests_changed requires no_test_tampering proof property."))
    if (risk.llm_or_prompt_logic or risk.high_stub_risk) and not (
        {"input_sensitivity", "metamorphic", "mutation_smoke"} & kinds
    ):
        issues.append(_issue("missing_behavioral_oracle", "LLM/high-stub-risk work requires input_sensitivity, metamorphic, or mutation_smoke proof."))
    if risk.web_or_http_runtime and not ({"http_boot", "behavioral_http"} & kinds):
        issues.append(_issue("missing_runtime_proof", "web_or_http_runtime requires http_boot or behavioral_http proof."))
    return issues


class ContractValidator:
    @staticmethod
    def validate(bundle: ContractBundle, *, context: WorkspaceContext | None = None) -> list[ContractIssue]:
        issues: list[ContractIssue] = list(bundle.issues)
        proofs: list[ProofSpec] = []
        resolver = EvidenceResolver(context) if context is not None else None
        if bundle.plan is not None:
            issues.extend(validate_plan_candidate_paths(bundle.plan))
            issues.extend(validate_plan_layout_policy(bundle.plan, context=context))
            for subtask in bundle.plan.subtasks:
                if subtask.proof is None:
                    issues.append(_issue("missing_proof", "Subtask has no typed proof.", subtask_id=subtask.id))
                else:
                    proofs.append(subtask.proof)
                    issues.extend(
                        validate_proof_spec(
                            subtask.proof,
                            phase="plan",
                            subtask_id=subtask.id,
                            resolver=resolver,
                        )
                    )
                    issues.extend(validate_subtask_proof_strength(subtask, phase="plan"))
        for review in bundle.reviews:
            issues.extend(validate_review_contract(review))
            for item in review.issues:
                issues.append(
                    ContractIssue(
                        code=item.code,
                        severity=item.severity,
                        phase=item.phase,
                        subtask_id=item.subtask_id,
                        message=item.message,
                        evidence_refs=item.evidence_refs,
                    )
                )
        if context is not None:
            for completion in bundle.completions:
                issues.extend(validate_completion_contract(completion, context=context))
            for report in bundle.verification_reports:
                issues.extend(validate_verification_report_ref(report, context=context))
        issues.extend(validate_risk_profile(bundle.risk, proofs))
        return issues
