"""Typed contract models for Umbrella phase gates.

These models are the boundary between model-produced tool output and hard
orchestration decisions. They intentionally store human notes separately from
machine-checkable proof fields: valid shape is not evidence.
"""

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Generic, Literal, TypeVar, cast


CURRENT_CONTRACT_VERSION = "1"

Actor = Literal["agent", "supervisor", "verifier", "watcher", "harness"]
IssueSeverity = Literal["info", "warning", "error", "blocking", "human_required"]
EvidenceRefType = Literal[
    "ledger_event",
    "verification_report",
    "test_run",
    "artifact",
    "diff",
    "memory_node",
    "harness_candidate",
    "mutation_report",
    "input_sensitivity_report",
]
ProofKind = Literal[
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
]
OracleType = Literal[
    "unit_assertions",
    "behavioral_http",
    "input_sensitivity",
    "metamorphic",
    "snapshot",
    "mutation_kill",
    "golden_file",
    "build",
    "import",
]
RequiredProperty = Literal[
    "distinct_inputs_distinct_outputs",
    "invalid_input_rejected",
    "round_trip",
    "idempotence",
    "monotonicity",
    "no_test_tampering",
    "mutation_killed",
    "runtime_started",
    "module_imports",
    "build_succeeds",
]
ReviewVerdict = Literal["ok", "revise", "abort"]
PhaseDecisionAction = Literal[
    "continue",
    "loop_back",
    "abort",
    "verify",
    "human_checkpoint",
]
TrustLevel = Literal[
    "agent_claim",
    "observed_artifact",
    "public_verified",
    "mutation_verified",
    "hidden_verified",
    "adversarial_verified",
    "contradicted",
    "retracted",
]


T = TypeVar("T")


def json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return json_ready(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_ready(item) for item in value]
    return value


def _contract_string(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _contract_optional_string(value: Any) -> str | None:
    text = _contract_string(value)
    return text or None


@dataclass(frozen=True)
class ContractEnvelope(Generic[T]):
    schema_name: str
    schema_version: str
    run_id: str
    phase: str
    actor: Actor
    payload: T

    def to_dict(self) -> dict[str, Any]:
        return json_ready(self)


@dataclass(frozen=True)
class EvidenceRef:
    ref_type: EvidenceRefType
    ref_id: str
    hash: str | None = None
    produced_by: Actor = "agent"
    phase: str | None = None
    subtask_id: str | None = None
    created_after_event: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "EvidenceRef":
        return cls(
            ref_type=cast(EvidenceRefType, _contract_string(value.get("ref_type"))),
            ref_id=_contract_string(value.get("ref_id")),
            hash=_contract_optional_string(value.get("hash")),
            produced_by=cast(
                Actor, _contract_string(value.get("produced_by", "agent"))
            ),
            phase=_contract_optional_string(value.get("phase")),
            subtask_id=_contract_optional_string(value.get("subtask_id")),
            created_after_event=_contract_optional_string(
                value.get("created_after_event")
            ),
        )


@dataclass(frozen=True)
class ArtifactDigest:
    uri: str
    digest: str
    media_type: str = ""


@dataclass(frozen=True)
class UmbrellaAttestation:
    predicate_type: str
    subject: tuple[ArtifactDigest, ...] = ()
    materials: tuple[ArtifactDigest, ...] = ()
    builder: str = ""
    invocation: dict[str, Any] = field(default_factory=dict)
    byproducts: tuple[ArtifactDigest, ...] = ()
    started_at: str = ""
    finished_at: str = ""


@dataclass(frozen=True)
class ContractIssue:
    code: str
    severity: IssueSeverity
    phase: str = ""
    subtask_id: str = ""
    path: str = ""
    message: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    suggested_action: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.severity in {"blocking", "human_required"}


@dataclass(frozen=True)
class VerificationReportRef:
    report_id: str
    report_hash: str
    workspace_hash: str
    diff_hash: str
    produced_after_event_id: str
    verifier_id: str
    passed: bool
    ledger_hash: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "VerificationReportRef":
        return cls(
            report_id=str(value.get("report_id") or ""),
            report_hash=str(value.get("report_hash") or ""),
            workspace_hash=str(value.get("workspace_hash") or ""),
            diff_hash=str(value.get("diff_hash") or ""),
            produced_after_event_id=str(value.get("produced_after_event_id") or ""),
            verifier_id=str(value.get("verifier_id") or ""),
            passed=bool(value.get("passed")),
            ledger_hash=(
                str(value.get("ledger_hash"))
                if value.get("ledger_hash") is not None
                else None
            ),
        )

    def evidence_ref(self, *, phase: str = "", subtask_id: str = "") -> EvidenceRef:
        return EvidenceRef(
            ref_type="verification_report",
            ref_id=self.report_id,
            hash=self.ledger_hash,
            produced_by="verifier",
            phase=phase or None,
            subtask_id=subtask_id or None,
            created_after_event=self.produced_after_event_id or None,
        )


@dataclass(frozen=True)
class ProofExecutionSpec:
    kind: ProofKind
    command: tuple[str, ...] = ()
    timeout_sec: int = 120
    shell: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProofExecutionSpec":
        command = value.get("command") or ()
        return cls(
            kind=cast(ProofKind, str(value.get("kind") or "command")),
            command=tuple(str(item) for item in command) if isinstance(command, list) else (),
            timeout_sec=int(value.get("timeout_sec") or 120),
            shell=bool(value.get("shell")),
        )


@dataclass(frozen=True)
class ProofOracleSpec:
    oracle_type: OracleType
    required_properties: tuple[RequiredProperty, ...] = ()
    negative_cases_required: bool = False
    input_sensitivity_required: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProofOracleSpec":
        props = value.get("required_properties") or value.get("properties") or ()
        return cls(
            oracle_type=cast(
                OracleType, str(value.get("oracle_type") or "unit_assertions")
            ),
            required_properties=tuple(
                cast(RequiredProperty, str(item)) for item in props
            ),
            negative_cases_required=bool(value.get("negative_cases_required")),
            input_sensitivity_required=bool(value.get("input_sensitivity_required")),
        )


@dataclass(frozen=True)
class ProofScopeSpec:
    files_under_test: tuple[str, ...] = ()
    changed_files_expected: tuple[str, ...] = ()
    pytest_targets: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProofScopeSpec":
        return cls(
            files_under_test=tuple(str(item) for item in value.get("files_under_test") or ()),
            changed_files_expected=tuple(
                str(item) for item in value.get("changed_files_expected") or ()
            ),
            pytest_targets=tuple(str(item) for item in value.get("pytest_targets") or ()),
        )


@dataclass(frozen=True)
class ProofAntiGamingSpec:
    allows_mock: bool = False
    allows_snapshot_update: bool = False
    allows_test_only_change: bool = False
    requires_real_runtime: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProofAntiGamingSpec":
        return cls(
            allows_mock=bool(value.get("allows_mock")),
            allows_snapshot_update=bool(value.get("allows_snapshot_update")),
            allows_test_only_change=bool(value.get("allows_test_only_change")),
            requires_real_runtime=bool(value.get("requires_real_runtime")),
        )


@dataclass(frozen=True)
class ProofSpec:
    execution: ProofExecutionSpec
    oracle: ProofOracleSpec
    scope: ProofScopeSpec
    anti_gaming: ProofAntiGamingSpec = field(default_factory=ProofAntiGamingSpec)
    human_claims: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProofSpec":
        refs = value.get("evidence_refs") or ()
        return cls(
            execution=ProofExecutionSpec.from_mapping(value.get("execution") or {}),
            oracle=ProofOracleSpec.from_mapping(value.get("oracle") or {}),
            scope=ProofScopeSpec.from_mapping(value.get("scope") or {}),
            anti_gaming=ProofAntiGamingSpec.from_mapping(value.get("anti_gaming") or {}),
            human_claims=tuple(str(item) for item in value.get("human_claims") or ()),
            evidence_refs=tuple(
                EvidenceRef.from_mapping(item)
                for item in refs
                if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True)
class SubtaskIR:
    id: str
    title: str
    goal: str
    files_to_change: tuple[str, ...] = ()
    files_to_create: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    proof: ProofSpec | None = None
    acceptance_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanIR:
    run_id: str
    workspace_id: str
    subtasks: tuple[SubtaskIR, ...] = ()


@dataclass(frozen=True)
class ReviewIssue:
    code: str
    severity: IssueSeverity
    phase: str = ""
    subtask_id: str = ""
    message: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ReviewIssue":
        refs = value.get("evidence_refs") or ()
        return cls(
            code=str(value.get("code") or ""),
            severity=cast(IssueSeverity, str(value.get("severity") or "warning")),
            phase=str(value.get("phase") or ""),
            subtask_id=str(value.get("subtask_id") or ""),
            message=str(value.get("message") or ""),
            evidence_refs=tuple(
                EvidenceRef.from_mapping(item)
                for item in refs
                if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True)
class ReviewContract:
    verdict: ReviewVerdict
    issues: tuple[ReviewIssue, ...] = ()
    loop_back_target: str = ""
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ReviewContract":
        return cls(
            verdict=cast(ReviewVerdict, str(value.get("verdict") or "")),
            issues=tuple(
                ReviewIssue.from_mapping(item)
                for item in value.get("issues") or ()
                if isinstance(item, dict)
            ),
            loop_back_target=str(value.get("loop_back_target") or ""),
            notes=str(value.get("notes") or ""),
        )


@dataclass(frozen=True)
class ResearchSummaryContract:
    architecture_id: str
    findings_ids: tuple[str, ...] = ()
    coverage_status: Literal["complete", "source_scarce", "blocked"] = "complete"
    source_scarcity_reason: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class CompletedClaim:
    claim_id: str
    text: str
    files: tuple[str, ...] = ()
    proof_refs: tuple[EvidenceRef, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CompletedClaim":
        return cls(
            claim_id=str(value.get("claim_id") or value.get("id") or ""),
            text=str(value.get("text") or value.get("claim") or ""),
            files=tuple(str(item) for item in value.get("files") or ()),
            proof_refs=tuple(
                EvidenceRef.from_mapping(item)
                for item in value.get("proof_refs") or ()
                if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True)
class CompletionContract:
    subtask_id: str
    status: Literal["done"]
    completed_claims: tuple[CompletedClaim, ...] = ()
    changed_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    verification_report: VerificationReportRef | None = None
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CompletionContract":
        report = value.get("verification_report")
        return cls(
            subtask_id=str(value.get("subtask_id") or ""),
            status="done",
            completed_claims=tuple(
                CompletedClaim.from_mapping(item)
                for item in value.get("completed_claims") or ()
                if isinstance(item, dict)
            ),
            changed_files=tuple(str(item) for item in value.get("changed_files") or ()),
            deleted_files=tuple(str(item) for item in value.get("deleted_files") or ()),
            evidence_refs=tuple(
                EvidenceRef.from_mapping(item)
                for item in value.get("evidence_refs") or ()
                if isinstance(item, dict)
            ),
            verification_report=(
                VerificationReportRef.from_mapping(report)
                if isinstance(report, dict)
                else None
            ),
            notes=str(value.get("notes") or ""),
        )


@dataclass(frozen=True)
class TaskRiskProfile:
    code_changed: bool = False
    tests_changed: bool = False
    verifier_changed: bool = False
    external_api: bool = False
    llm_or_prompt_logic: bool = False
    web_or_http_runtime: bool = False
    high_stub_risk: bool = False
    self_improvement: bool = False
    seed_promotion: bool = False


@dataclass(frozen=True)
class WorkspaceContext:
    repo_root: str
    workspace_root: str
    workspace_id: str
    current_workspace_hash: str = ""
    current_diff_hash: str = ""
    last_patch_event_id: str = ""


@dataclass(frozen=True)
class ContractBundle:
    run_id: str
    workspace_id: str
    plan: PlanIR | None = None
    reviews: tuple[ReviewContract, ...] = ()
    research_summary: ResearchSummaryContract | None = None
    completions: tuple[CompletionContract, ...] = ()
    verification_reports: tuple[VerificationReportRef, ...] = ()
    issues: tuple[ContractIssue, ...] = ()
    risk: TaskRiskProfile = field(default_factory=TaskRiskProfile)


@dataclass(frozen=True)
class PhaseDecision:
    action: PhaseDecisionAction
    target_phase: str | None = None
    blocking_issue_codes: tuple[str, ...] = ()
    reason: str = ""

