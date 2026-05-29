"""Contract validators used by Umbrella hard gates."""

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

_COMPLETION_BLOCKER_PHRASES = (
    "infrastructure blocker",
    "verification blocked",
    "missing source file",
    "missing source files",
    "unresolved blocker",
    "blockers remain",
    "blockers present",
    "blocker still open",
    "blockers blocking",
    "cannot close",
    "cannot complete",
    "cannot mark",
)

from umbrella.analysis.shell_commands import validate_argv
from umbrella.contracts.evidence import (
    EvidenceResolver,
    LEDGER_BACKED_EVIDENCE_REF_TYPES,
)
from umbrella.contracts.hashing import diff_hash
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
from umbrella.contracts.runtime_probes import (
    effective_runtime_capabilities,
    proof_requires_capability,
)
from umbrella.contracts.schemas import VALID_REVIEW_CODES
from umbrella.contracts.layout_policy import (
    is_python_implementation_path,
    validate_plan_layout_policy,
)
from umbrella.contracts.harness_profiles import known_harness_profile_ids


ALLOWED_SCHEMA_NAMES = {
    "plan",
    "review",
    "research_summary",
    "completion",
    "verification_report",
}
_ALLOWED_PROOF_KINDS = frozenset(
    {
        "pytest",
        "verification_step",
        "http_boot",
        "behavioral_http",
        "input_sensitivity",
        "mutation_smoke",
        "metamorphic",
        "property_test",
        "import_check",
        "build",
        "command",
    }
)
_CANDIDATE_CONTROL_DIRS = {
    ".git",
    ".memory",
    ".umbrella",
    ".umbrella_scratch",
}
_CANDIDATE_CONTROL_ROOT_FILES = {
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
_MOCK_PROOF_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("unittest.mock", ("unittest.mock",)),
    ("pytest-mock", ("pytest-mock", "pytest_mock")),
    ("MagicMock", ("magicmock",)),
    ("mock", ("mock", "mocks", "mocked", "mocking", "mocker")),
    ("mocker", ("mocker", "mocker.")),
    ("monkeypatch", ("monkeypatch", "monkeypatching")),
    ("fake", ("fake", "fakes", "faked", "faking")),
    ("stub", ("stub", "stubs", "stubbed", "stubbing")),
    (
        "simulated",
        (
            "simulation mode",
            "simulated mode",
            "simulated display",
            "simulated runtime",
            "simulated environment",
            "simulated provider",
            "simulated service",
        ),
    ),
    ("dry-run", ("dry-run", "dry run", "dry-runs", "dry runs")),
    ("no-op", ("no-op", "no op", "no-ops", "no ops")),
    ("test double", ("test double", "test doubles", "boundary double", "boundary doubles")),
)
_RUNTIME_SIMULATION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mock", ("mock", "mocks", "mocked", "mocking", "mocker")),
    ("fake", ("fake", "fakes", "faked", "faking")),
    ("stub", ("stub", "stubs", "stubbed", "stubbing")),
    (
        "simulated",
        (
            "simulation mode",
            "simulated mode",
            "simulated display",
            "simulated runtime",
            "simulated environment",
            "simulated gui",
            "simulated window",
        ),
    ),
    ("dry-run", ("dry-run", "dry run", "dry-runs", "dry runs")),
    ("no-op", ("no-op", "no op", "no-ops", "no ops")),
    ("test double", ("test double", "test doubles", "boundary double", "boundary doubles")),
)
_RUNTIME_READINESS_KEYS = ("readiness", "readiness_probe", "readiness_probes")
_RUNTIME_DRIVER_KEYS = ("assert_command", "interaction_command", "driver_command")
_RUNTIME_FREEFORM_DRIVER_KEYS = (
    "interaction",
    "interaction_test",
    "test_interaction",
    "expected_behavior",
    "expected_evidence",
    "evidence_expectations",
)
_RUNTIME_STARTED_ONLY_PROPERTIES = frozenset(
    {"runtime_started", "module_imports", "build_succeeds", "no_test_tampering"}
)
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
    "unknown_harness_profile",
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


def _iter_contract_text(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            yield value
        return
    if isinstance(value, (int, float, bool)):
        yield str(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.strip():
                yield key
            yield from _iter_contract_text(item)
        return
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_contract_text(item)


def _proof_planned_runtime_text(proof: ProofSpec) -> str:
    return "\n".join(
        [
            *proof.execution.command,
            *_iter_contract_text(proof.harness_options),
        ]
    ).lower()


def _phrase_matches(text: str, phrase: str) -> bool:
    raw = str(text or "").lower()
    needle = str(phrase or "").lower().strip()
    if not needle:
        return False
    if any(not (ch.isalnum() or ch.isspace()) for ch in needle) and needle in raw:
        return True
    normalized = "".join(ch if ch.isalnum() else " " for ch in raw)
    normalized_needle = "".join(ch if ch.isalnum() else " " for ch in needle)
    return f" {normalized_needle.strip()} " in f" {normalized} "


def _matching_pattern(
    text: str, patterns: tuple[tuple[str, tuple[str, ...]], ...]
) -> str:
    for label, needles in patterns:
        if any(_phrase_matches(text, needle) for needle in needles):
            return label
    return ""


def _runtime_readiness_value(options: dict[str, Any]) -> Any:
    for key in _RUNTIME_READINESS_KEYS:
        if key in options:
            return options.get(key)
    return None


def _runtime_readiness_issue(value: Any) -> str:
    if value is None:
        return ""
    specs: list[Any]
    if isinstance(value, dict):
        specs = [value]
    elif isinstance(value, list):
        specs = value
    elif isinstance(value, str):
        return (
            "desktop_gui_runtime readiness must be a structured object/list, "
            "not free-form text. Use {'type':'process_alive'} or "
            "{'type':'log_contains','text':'READY'}; a plain string is "
            "interpreted as log text and can create false timeouts."
        )
    else:
        return "desktop_gui_runtime readiness must be an object or list of objects."
    allowed = {
        "process_alive",
        "alive",
        "wait",
        "wait_seconds",
        "log_contains",
        "stdout_contains",
        "log_regex",
        "stdout_regex",
    }
    for spec in specs:
        if not isinstance(spec, dict):
            return "desktop_gui_runtime readiness entries must be objects."
        kind = str(spec.get("type") or spec.get("kind") or "process_alive").strip()
        if kind not in allowed:
            return (
                f"desktop_gui_runtime readiness type `{kind}` is not supported; "
                "use process_alive, wait_seconds, log_contains, or log_regex."
            )
        if kind in {"log_contains", "stdout_contains"} and not str(
            spec.get("text") or spec.get("contains") or ""
        ).strip():
            return "desktop_gui_runtime log_contains readiness requires `text`."
        if kind in {"log_regex", "stdout_regex"} and not str(
            spec.get("pattern") or spec.get("regex") or ""
        ).strip():
            return "desktop_gui_runtime log_regex readiness requires `pattern`."
    return ""


def _runtime_driver_command(options: dict[str, Any]) -> Any:
    for key in _RUNTIME_DRIVER_KEYS:
        value = options.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (list, tuple)) and any(str(item).strip() for item in value):
            return value
    return None


def validate_proof_spec(
    proof: ProofSpec,
    *,
    phase: str = "",
    subtask_id: str = "",
    resolver: EvidenceResolver | None = None,
    runtime_capabilities: dict[str, bool] | None = None,
    drive_root: Path | None = None,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    execution = proof.execution
    oracle = proof.oracle
    scope = proof.scope
    anti = proof.anti_gaming
    caps = runtime_capabilities or {}
    planned_runtime_text = _proof_planned_runtime_text(proof)
    execution_command_text = "\n".join(str(item) for item in execution.command).lower()
    if execution.kind not in _ALLOWED_PROOF_KINDS:
        issues.append(
            _issue(
                "unknown_proof_kind",
                (
                    f"Unknown proof.execution.kind `{execution.kind}`; "
                    "subtask proofs must use a typed ProofSpec proof kind. "
                    "Workspace verification-only checks such as file_exists "
                    "cannot replace behavioral proof."
                ),
                phase=phase,
                subtask_id=subtask_id,
            )
        )
    if proof.harness_profile and proof.harness_profile not in known_harness_profile_ids():
        issues.append(
            _issue(
                "unknown_harness_profile",
                (
                    f"Unknown proof.harness_profile `{proof.harness_profile}`; "
                    "use a known harness profile id or omit the field."
                ),
                phase=phase,
                subtask_id=subtask_id,
            )
        )
    if proof.harness_profile == "desktop_gui_runtime":
        options = proof.harness_options if isinstance(proof.harness_options, dict) else {}
        if execution.kind != "command":
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof.execution.kind must be "
                        "`command` because run_subtask_proof manages the GUI "
                        "launch lifecycle directly. Use pytest for headless "
                        "controller proof, not for the managed runtime launch."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        if options.get("managed_runtime") is not True:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof.harness_options must set "
                        "managed_runtime=true so launch, readiness, driver "
                        "evidence, and cleanup are owned by run_subtask_proof."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        required = {str(item).strip().lower() for item in proof.required_capabilities}
        missing_required = [
            tag for tag in ("desktop_gui_runtime", "subprocess") if tag not in required
        ]
        if missing_required:
            issues.append(
                _issue(
                    "capability_probe_failed",
                    (
                        "desktop_gui_runtime proof requires explicit "
                        "proof.required_capabilities for "
                        + ", ".join(missing_required)
                        + ". Research must declare/probe display automation "
                        "availability before planning real native GUI proof."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        if not proof.harness_options:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof must include "
                        "proof.harness_options describing launch, interaction "
                        "driver, evidence, timeout, and cleanup expectations."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        if "subprocess." in execution_command_text or "from subprocess import" in execution_command_text:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof.execution.command must be "
                        "the direct managed launch command. Do not wrap the "
                        "GUI launch in an inline subprocess; put post-readiness "
                        "checks in harness_options.assert_command, "
                        "interaction_command, or driver_command."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        readiness_value = _runtime_readiness_value(options)
        if readiness_value is None:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof.harness_options must include "
                        "structured readiness such as {'type':'process_alive'} "
                        "or {'type':'log_contains','text':'READY'}."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        readiness_issue = _runtime_readiness_issue(readiness_value)
        if readiness_issue:
            issues.append(
                _issue(
                    "weak_proof",
                    readiness_issue,
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        if not any(options.get(key) for key in ("cleanup", "cleanup_command", "teardown")):
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof.harness_options must include "
                        "cleanup instructions so managed runtime processes/windows "
                        "cannot leak across subtasks."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        required_props = {str(item) for item in oracle.required_properties}
        needs_driver = bool(required_props - _RUNTIME_STARTED_ONLY_PROPERTIES)
        driver_command = _runtime_driver_command(options)
        if needs_driver and not driver_command:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof claims behavior beyond "
                        "runtime_started/module_imports/build_succeeds but "
                        "does not provide harness_options.assert_command, "
                        "interaction_command, or driver_command. Put the "
                        "machine interaction/assertion in one of those argv "
                        "fields so run_subtask_proof can execute it after "
                        "readiness."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        elif any(
            str(key) in options
            for key in _RUNTIME_FREEFORM_DRIVER_KEYS
        ) and not driver_command:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof describes interaction/evidence "
                        "as free-form harness_options text but provides no "
                        "machine driver command. Use assert_command, "
                        "interaction_command, or driver_command as an argv "
                        "command, and keep prose only as supplemental notes."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
        simulated_runtime = _matching_pattern(
            planned_runtime_text,
            _RUNTIME_SIMULATION_PATTERNS,
        )
        if simulated_runtime:
            issues.append(
                _issue(
                    "weak_proof",
                    (
                        "desktop_gui_runtime proof cannot describe a mock, fake, "
                        "stubbed, dry-run, or simulated display/runtime path "
                        f"(`{simulated_runtime}`). Use a real launch/readiness/"
                        "interaction/cleanup contract, or mutate to a headless "
                        "harness profile instead."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
    if proof.harness_profile == "desktop_gui_headless" and anti.requires_real_runtime:
        issues.append(
            _issue(
                "weak_proof",
                (
                    "desktop_gui_headless proof cannot claim "
                    "anti_gaming.requires_real_runtime=true. Use headless "
                    "adapter/controller proof with requires_real_runtime=false, "
                    "or split out a separate desktop_gui_runtime leaf for real "
                    "window launch evidence."
                ),
                phase=phase,
                subtask_id=subtask_id,
            )
        )
    if not caps and drive_root is not None:
        caps = effective_runtime_capabilities(drive_root)
    if caps:
        capability_issue = proof_requires_capability(proof, caps)
        if capability_issue:
            issues.append(
                _issue(
                    "capability_probe_failed",
                    capability_issue,
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
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
        issues.append(_issue("collect_only_proof", "pytest --collect-only only collects pytest tests; it is not behavioral proof.", phase=phase, subtask_id=subtask_id))
    mock_pattern = _matching_pattern(planned_runtime_text, _MOCK_PROOF_PATTERNS)
    if not anti.allows_mock and mock_pattern:
        issues.append(
            _issue(
                "weak_proof",
                (
                    "proof.anti_gaming.allows_mock=false but the planned proof "
                    f"command/options mention `{mock_pattern}`. Either remove "
                    "the mock/fake/simulated proof path, or explicitly use a "
                    "headless boundary-doubles proof that does not claim real "
                    "runtime evidence."
                ),
                phase=phase,
                subtask_id=subtask_id,
            )
        )
    if anti.requires_real_runtime and anti.allows_mock:
        issues.append(_issue("real_runtime_proof_cannot_allow_mock", "Real runtime proof cannot allow mocks.", phase=phase, subtask_id=subtask_id))
    if execution.kind == "pytest" and not scope.pytest_targets:
        issues.append(_issue("missing_pytest_targets", "pytest proof requires explicit pytest_targets.", phase=phase, subtask_id=subtask_id))
    if (
        execution.kind == "pytest"
        and "no_test_tampering" in oracle.required_properties
    ):
        forbidden = frozenset({"-k", "--keyword", "-m", "--deselect", "--ignore", "--ignore-glob"})
        tail_start = 0
        for index, token in enumerate(execution.command):
            if str(token).strip().lower() == "pytest":
                tail_start = index + 1
                break
        for token in execution.command[tail_start:]:
            lowered = str(token).strip().lower()
            if lowered in forbidden or any(
                lowered == prefix or lowered.startswith(prefix + "=")
                for prefix in ("--deselect", "--ignore", "--ignore-glob")
            ):
                issues.append(
                    _issue(
                        "proof_selection_filter_forbidden",
                        "no_test_tampering pytest proofs cannot use -k, -m, "
                        "--deselect, --ignore, or --ignore-glob; fix code or tests instead.",
                        phase=phase,
                        subtask_id=subtask_id,
                    )
                )
                break
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
        non_test_expected = {path for path in expected if not _path_looks_like_test(path)}
        if (
            "no_test_tampering" in oracle.required_properties
            and non_test_expected
            and not (under_test & non_test_expected)
        ):
            issues.append(
                _issue(
                    "proof_scope_mismatch",
                    (
                        "no_test_tampering proof must include at least one "
                        "non-test changed file in files_under_test; test-file "
                        "overlap alone does not prove the runtime artifact."
                    ),
                    phase=phase,
                    subtask_id=subtask_id,
                )
            )
    if resolver is not None and proof.evidence_refs:
        issues.extend(resolver.validate_refs(proof.evidence_refs, phase=phase, subtask_id=subtask_id))
    return issues


_REVIEW_PHASES_REQUIRE_COVERAGE = frozenset(
    {"plan_review", "subtask_review", "research_review"}
)


def _review_coverage_message(phase: str) -> str:
    if phase == "research_review":
        return (
            "Research review must include a complete coverage checklist with "
            "all dimensions set to true for verdict `ok`. In research_review, "
            "`true` means the dimension was evaluated and has no blocking "
            "research handoff issue, including when the dimension is not "
            "directly applicable. Use verdict `revise` with a typed blocking "
            "issue instead of false/null when a dimension is actually unsafe."
        )
    return "Review must include coverage checklist with all dimensions set to true."


def validate_review_contract(review: ReviewContract, *, phase: str = "") -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if review.verdict not in {"ok", "revise", "abort"}:
        issues.append(_issue("invalid_review_verdict", "Review verdict must be ok/revise/abort.", phase=phase))
    loop_target = str(review.loop_back_target or "").strip().split(":", 1)[0]
    if phase == "subtask_review" and loop_target == "plan":
        issues.append(
            _issue(
                "invalid_review_loop_back_target",
                (
                    "subtask_review may request implementation revision in execute; "
                    "plan revisions require a typed control-plane "
                    "RecoveryDecision/PlanRevisionPatch."
                ),
                phase=phase,
            )
        )
    if phase in _REVIEW_PHASES_REQUIRE_COVERAGE:
        if review.coverage is None or not review.coverage.is_complete():
            issues.append(
                _issue(
                    "review_coverage_incomplete",
                    _review_coverage_message(phase),
                    phase=phase,
                )
            )
        elif review.loop_back_target and review.coverage and not review.coverage.is_complete():
            issues.append(
                _issue(
                    "review_single_pass_required",
                    "loop_back_target requires a complete coverage checklist in one submit.",
                    phase=phase,
                )
            )
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
    if first == "workspaces":
        hint = "/".join(parts[2:]) if len(parts) >= 3 else ""
        hint_text = f" Use `{hint}` instead." if hint else ""
        return _issue(
            "candidate_path_outside_workspace",
            (
                f"Candidate plan path `{path}` is repository-relative. "
                "Phase plan file paths are already relative to the active "
                "workspace, so do not prefix them with `workspaces/<id>/`."
                f"{hint_text}"
            ),
            phase=phase,
            subtask_id=subtask_id,
        )
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


def _path_looks_like_test(path: str) -> bool:
    normalised = str(path or "").replace("\\", "/").lower().strip("/")
    if not normalised:
        return False
    parts = [part for part in normalised.split("/") if part]
    if any(part in {"test", "tests", "__tests__"} for part in parts[:-1]):
        return True
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or name.endswith((".test.js", ".test.jsx", ".test.ts", ".test.tsx"))
        or name.endswith((".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx"))
    )


def validate_plan_test_tampering_policy(
    plan: PlanIR, *, phase: str = "plan"
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for subtask in plan.subtasks:
        proof = subtask.proof
        changed_paths = [*subtask.files_to_create, *subtask.files_to_change]
        if proof is not None:
            changed_paths.extend(proof.scope.changed_files_expected)
        test_paths = sorted({path for path in changed_paths if _path_looks_like_test(path)})
        if not test_paths or proof is None:
            continue
        if "no_test_tampering" in proof.oracle.required_properties:
            continue
        issues.append(
            _issue(
                "test_tampering_risk",
                (
                    f"Subtask changes test path(s) {test_paths}; add "
                    "`no_test_tampering` to this same subtask's "
                    "proof.oracle.required_properties."
                ),
                phase=phase,
                subtask_id=subtask.id,
            )
        )
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


def _plan_capability_text_parts(plan: PlanIR) -> list[str]:
    parts: list[str] = []
    if plan.notes:
        parts.append(plan.notes)
    for subtask in plan.subtasks:
        parts.extend(
            item
            for item in (
                subtask.title,
                subtask.goal,
                *subtask.acceptance_claims,
            )
            if str(item or "").strip()
        )
        parts.extend(_iter_contract_text(subtask.memory_scope))
        proof = subtask.proof
        if proof is None:
            continue
        parts.extend(str(item) for item in proof.human_claims if str(item).strip())
        parts.extend(_iter_contract_text(proof.harness_options))
    return parts


def validate_plan_capability_consistency(
    plan: PlanIR,
    declaration: Any,
    *,
    phase: str = "plan",
) -> list[ContractIssue]:
    if declaration is None:
        return []
    from umbrella.contracts.capability_declaration import (
        capability_text_contradiction_errors,
        declaration_ready_for_handoff,
    )

    if not declaration_ready_for_handoff(declaration):
        return []
    payload = declaration.to_dict()
    caps = payload.get("capabilities")
    if not isinstance(caps, dict):
        return []
    errors = capability_text_contradiction_errors(
        caps,
        _plan_capability_text_parts(plan),
        text_label="phase plan text",
    )
    return [
        _issue(
            "capability_probe_failed",
            "Phase plan contradicts submitted capability_declaration: " + error,
            phase=phase,
        )
        for error in errors
    ]


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
    blob = str(text or "").strip().lower()
    if not blob:
        return False
    if any(phrase in blob for phrase in _COMPLETION_BLOCKER_PHRASES):
        return True
    for verb in ("cannot close", "cannot complete", "cannot mark"):
        if verb in blob and ("done" in blob or "complete" in blob):
            return True
    return False


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
    completion_context = context
    if completion.changed_files:
        completion_context = replace(
            context,
            current_diff_hash=diff_hash(
                Path(context.workspace_root),
                completion.changed_files,
            ),
        )
    resolver = EvidenceResolver(completion_context)
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
                allow_workspace_hash_mismatch_if_diff_matches=True,
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


# Planning-phase exits must not re-validate execute/verify completion evidence.
# After loop_back_to(plan), the workspace often grows while the latest
# mark_subtask_complete proof still carries an older workspace_hash.
_PLANNING_EXIT_PHASES = frozenset(
    {"preflight", "research", "research_review", "plan", "plan_review"}
)


class ContractValidator:
    @staticmethod
    def validate(
        bundle: ContractBundle,
        *,
        context: WorkspaceContext | None = None,
        exit_phase: str = "",
        runtime_capabilities: dict[str, bool] | None = None,
        drive_root: Path | None = None,
    ) -> list[ContractIssue]:
        issues: list[ContractIssue] = list(bundle.issues)
        proofs: list[ProofSpec] = []
        resolver = EvidenceResolver(context) if context is not None else None
        skip_cross_phase_evidence = exit_phase in _PLANNING_EXIT_PHASES
        caps = runtime_capabilities or {}
        if not caps and drive_root is not None:
            caps = effective_runtime_capabilities(drive_root)
        from umbrella.contracts.capability_declaration import (
            declaration_ready_for_handoff,
            load_capability_declaration,
        )

        declaration = load_capability_declaration(drive_root)
        declaration_ready = declaration_ready_for_handoff(declaration)
        if bundle.plan is not None:
            if not declaration_ready:
                issues.append(
                    _issue(
                        "missing_capability_declaration",
                        "Plan validation requires a submitted capability_declaration from research.",
                        phase="plan",
                    )
                )
            else:
                issues.extend(
                    validate_plan_capability_consistency(bundle.plan, declaration)
                )
        if bundle.plan is not None:
            issues.extend(validate_plan_candidate_paths(bundle.plan))
            issues.extend(validate_plan_test_tampering_policy(bundle.plan))
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
                            runtime_capabilities=caps,
                            drive_root=drive_root,
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
        if context is not None and not skip_cross_phase_evidence:
            for completion in bundle.completions:
                issues.extend(validate_completion_contract(completion, context=context))
            for report in bundle.verification_reports:
                issues.extend(
                    validate_verification_report_ref(report, context=context)
                )
        issues.extend(validate_risk_profile(bundle.risk, proofs))
        return issues
