"""Canonical contract paths for recovery and plan-revision deltas.

LLM/watchers may propose typed-looking objects, but path authority belongs to
the PlanIR/ProofContract schema.  This registry is the compiler boundary: any
machine-control delta must normalize here before it can route a phase or block
plan validation.
"""


from dataclasses import dataclass
from typing import Any


CANONICAL_CONTRACT_PATHS = frozenset(
    {
        "proof",
        "proof.execution",
        "proof.execution.kind",
        "proof.execution.command",
        "proof.execution.timeout_sec",
        "proof.execution.execution_environment_id",
        "proof.scope",
        "proof.scope.pytest_targets",
        "proof.scope.files_under_test",
        "proof.scope.changed_files_expected",
        "proof.oracle",
        "proof.oracle.required_properties",
        "proof.oracle.oracle_type",
        "proof.harness_profile",
        "proof.harness_options",
        "proof.required_capabilities",
        "proof.generated_test_contract",
        "proof.generated_test_contract.oracle_claims",
        "proof.generated_test_contract.proof_budget",
        "proof.anti_gaming.allows_test_only_change",
        "proof.anti_gaming.allows_mock",
        "proof.anti_gaming.allows_snapshot_update",
        "proof.anti_gaming.requires_real_runtime",
        "generated_test_contract",
        "generated_test_contract.oracle_claims",
        "files_to_change",
        "files_to_create",
        "files_affected",
        "acceptance_claims",
    }
)

CONTRACT_PATH_ALIASES = {
    "pytest_targets": "proof.scope.pytest_targets",
    "files_under_test": "proof.scope.files_under_test",
    "changed_files_expected": "proof.scope.changed_files_expected",
    "required_properties": "proof.oracle.required_properties",
    "proof.pytest_targets": "proof.scope.pytest_targets",
    "proof.files_under_test": "proof.scope.files_under_test",
    "proof.changed_files_expected": "proof.scope.changed_files_expected",
    "proof.required_properties": "proof.oracle.required_properties",
    "proof.oracle.properties": "proof.oracle.required_properties",
    "proof.kind": "proof.execution.kind",
    "proof.command": "proof.execution.command",
    "proof.timeout_sec": "proof.execution.timeout_sec",
    "harness_profile": "proof.harness_profile",
    "harness_options": "proof.harness_options",
    "required_capabilities": "proof.required_capabilities",
}


@dataclass(frozen=True)
class CanonicalPath:
    path: str

    def __str__(self) -> str:
        return self.path


@dataclass(frozen=True)
class InvalidContractPath(Exception):
    raw_path: str
    message: str
    suggestion: str | None = None
    code: str = "invalid_contract_path"

    def __str__(self) -> str:
        suffix = f"; suggested canonical path: {self.suggestion}" if self.suggestion else ""
        return f"{self.message}: {self.raw_path}{suffix}"

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "path": self.raw_path,
            "message": str(self),
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        return payload


def _clean_path(path: str) -> str:
    text = str(path or "").strip().strip("`'\"")
    while text.startswith("."):
        text = text[1:]
    return text.strip()


def _strip_index_segments(path: str) -> str:
    """Drop bracketed index segments for suggestion purposes only."""

    text = _clean_path(path)
    result: list[str] = []
    depth = 0
    for char in text:
        if char == "[":
            depth += 1
            continue
        if char == "]" and depth:
            depth -= 1
            continue
        if depth:
            continue
        result.append(char)
    return "".join(result)


def _has_forbidden_path_syntax(path: str) -> bool:
    text = _clean_path(path)
    if not text:
        return False
    if any(char in text for char in "[]$*"):
        return True
    if ".." in text or "/" in text or "\\" in text:
        return True
    return False


def suggest_contract_path(path: str) -> str | None:
    text = _strip_index_segments(path)
    if not text:
        return None
    if text in CONTRACT_PATH_ALIASES:
        return CONTRACT_PATH_ALIASES[text]
    if text in CANONICAL_CONTRACT_PATHS:
        return text
    parts = [part for part in text.split(".") if part]
    while len(parts) > 1:
        parts.pop()
        candidate = ".".join(parts)
        if candidate in CONTRACT_PATH_ALIASES:
            return CONTRACT_PATH_ALIASES[candidate]
        if candidate in CANONICAL_CONTRACT_PATHS:
            return candidate
    return None


def normalize_contract_path(path: str) -> CanonicalPath:
    text = _clean_path(path)
    if not text:
        raise InvalidContractPath(text, "contract path is required")
    if _has_forbidden_path_syntax(text):
        raise InvalidContractPath(
            text,
            "contract path uses unsupported indexed/json/path syntax",
            suggestion=suggest_contract_path(text),
        )
    canonical = CONTRACT_PATH_ALIASES.get(text, text)
    if canonical in CANONICAL_CONTRACT_PATHS:
        return CanonicalPath(canonical)
    raise InvalidContractPath(
        text,
        "unknown contract path",
        suggestion=suggest_contract_path(text),
    )


def is_canonical_contract_path(path: str) -> bool:
    return _clean_path(path) in CANONICAL_CONTRACT_PATHS


def validate_delta_path(delta: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(delta, dict):
        raise InvalidContractPath("", "contract delta must be an object")
    normalized = dict(delta)
    canonical = normalize_contract_path(str(normalized.get("path") or ""))
    normalized["path"] = canonical.path
    return normalized


__all__ = [
    "CANONICAL_CONTRACT_PATHS",
    "CONTRACT_PATH_ALIASES",
    "CanonicalPath",
    "InvalidContractPath",
    "is_canonical_contract_path",
    "normalize_contract_path",
    "suggest_contract_path",
    "validate_delta_path",
]
