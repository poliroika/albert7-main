"""Deterministic checks for generated oracle/test contracts.

The LLM may propose generated tests, but conflicting test expectations are a
contract problem, not an implementation problem. This module turns typed
``GeneratedTestContract`` data into machine-readable ContractIssues so the
control plane can route to plan revision without scraping review prose.
"""

from __future__ import annotations

import json
import re
from typing import Any

from umbrella.contracts.models import ContractIssue, EvidenceRef

BAD_ORACLE_REVIEW_CODES = frozenset(
    {
        "bad_generated_oracle",
        "plan_contract_issue",
        "inconsistent_generated_oracle",
        "oracle_domain_mismatch",
        "contradictory_required_behavior",
        "invalid_generated_test_contract",
    }
)

_VALID_CLAIM_SOURCES = frozenset(
    {
        "task_requirement",
        "interface_model",
        "reference_behavior",
        "harness_contract",
    }
)
_DEFAULT_MAX_GENERATED_TESTS_PER_SUBTASK = 6


def extract_failed_pytest_node_ids(output: str) -> tuple[str, ...]:
    """Extract pytest node ids from machine-formatted failure summary text."""

    nodes: list[str] = []
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("FAILED "):
            continue
        node = stripped[len("FAILED ") :].split(" - ", 1)[0].strip()
        if node and node not in nodes:
            nodes.append(node.replace("\\", "/"))
    return tuple(nodes)


def generated_oracle_contract_issues(
    contract: Any,
    *,
    subtask_id: str = "",
    failed_node_ids: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> list[ContractIssue]:
    """Return blocking issues for contradictory generated oracle claims."""

    if not isinstance(contract, dict) or not contract:
        return []
    claims = [
        item
        for item in contract.get("oracle_claims") or ()
        if isinstance(item, dict)
    ]
    if not claims:
        return [
            _issue(
                "invalid_generated_test_contract",
                subtask_id=subtask_id,
                message="generated_test_contract must include oracle_claims[].",
                required_deltas=[
                    {
                        "op": "add",
                        "path": "proof.generated_test_contract.oracle_claims",
                        "replacement": [],
                    }
                ],
                evidence_refs=evidence_refs,
            )
        ]

    issues: list[ContractIssue] = []
    budget = contract.get("proof_budget") if isinstance(contract.get("proof_budget"), dict) else {}
    max_claims = int(
        budget.get("max_generated_tests_per_subtask")
        or _DEFAULT_MAX_GENERATED_TESTS_PER_SUBTASK
    )
    allow_expanded = bool(budget.get("allow_expanded_generated_tests"))
    if len(claims) > max_claims and not allow_expanded:
        issues.append(
            _issue(
                "invalid_generated_test_contract",
                subtask_id=subtask_id,
                message=(
                    "Generated oracle exceeds the default proof budget: "
                    f"{len(claims)} claims > {max_claims}. Add a typed "
                    "proof_budget override or split/escalate proof layers after "
                    "core proofs pass."
                ),
                invalid_values=[f"oracle_claim_count:{len(claims)}"],
                required_deltas=[
                    {
                        "op": "replace",
                        "path": "proof.generated_test_contract.proof_budget",
                        "replacement": {
                            "max_generated_tests_per_subtask": max_claims,
                            "allow_expanded_generated_tests": True,
                            "override_reason": "complex_workspace_policy",
                        },
                    }
                ],
                evidence_refs=evidence_refs,
            )
        )

    seen_ids: set[str] = set()
    for index, claim in enumerate(claims, start=1):
        claim_id = _claim_id(claim, index)
        if not str(claim.get("claim_id") or "").strip():
            issues.append(
                _issue(
                    "invalid_generated_test_contract",
                    subtask_id=subtask_id,
                    message="Every generated oracle claim needs a stable claim_id.",
                    invalid_values=[f"claim_index:{index}"],
                    required_deltas=[
                        {
                            "op": "replace",
                            "path": f"proof.generated_test_contract.oracle_claims.{index}.claim_id",
                            "replacement": claim_id,
                        }
                    ],
                    evidence_refs=evidence_refs,
                )
            )
        elif claim_id in seen_ids:
            issues.append(
                _issue(
                    "invalid_generated_test_contract",
                    subtask_id=subtask_id,
                    message=f"Duplicate generated oracle claim_id `{claim_id}`.",
                    invalid_values=[claim_id],
                    required_deltas=[
                        {
                            "op": "replace",
                            "path": "proof.generated_test_contract.oracle_claims",
                            "values": [claim_id],
                        }
                    ],
                    evidence_refs=evidence_refs,
                )
            )
        seen_ids.add(claim_id)
        source = str(claim.get("source") or "").strip()
        if source not in _VALID_CLAIM_SOURCES:
            issues.append(
                _issue(
                    "invalid_generated_test_contract",
                    subtask_id=subtask_id,
                    message=(
                        f"Generated oracle claim `{claim_id}` lacks a valid "
                        "source."
                    ),
                    invalid_values=[claim_id],
                    required_deltas=[
                        {
                            "op": "replace",
                            "path": f"proof.generated_test_contract.oracle_claims.{claim_id}.source",
                            "values": [claim_id],
                        }
                    ],
                    evidence_refs=evidence_refs,
                )
            )

    scoped_claims = _claims_for_failures(claims, failed_node_ids)
    issues.extend(
        _same_input_conflicts(
            scoped_claims,
            subtask_id=subtask_id,
            evidence_refs=evidence_refs,
        )
    )
    issues.extend(
        _interface_domain_conflicts(
            contract,
            scoped_claims,
            subtask_id=subtask_id,
            evidence_refs=evidence_refs,
        )
    )
    return issues


def contract_issues_payload(issues: list[ContractIssue]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for issue in issues:
        payload: dict[str, Any] = {
            "code": issue.code,
            "severity": issue.severity,
            "target_subtask_id": issue.target_subtask_id or issue.subtask_id,
            "contract_path": issue.contract_path,
            "invalid_values": list(issue.invalid_values),
            "required_deltas": list(issue.required_deltas),
            "message": issue.message,
        }
        if issue.target_path:
            payload["target_path"] = issue.target_path
        if issue.failure_hash:
            payload["failure_hash"] = issue.failure_hash
        if issue.evidence_refs:
            payload["evidence_refs"] = [
                f"{ref.ref_type}:{ref.ref_id}" for ref in issue.evidence_refs
            ]
        payloads.append({key: value for key, value in payload.items() if value})
    return payloads


def _claims_for_failures(
    claims: list[dict[str, Any]], failed_node_ids: tuple[str, ...]
) -> list[dict[str, Any]]:
    if not failed_node_ids:
        return claims
    failed = {item.replace("\\", "/") for item in failed_node_ids if item}
    matched: list[dict[str, Any]] = []
    for claim in claims:
        refs = {
            str(item).replace("\\", "/")
            for item in (claim.get("test_refs") or [])
            if str(item).strip()
        }
        if refs and refs.intersection(failed):
            matched.append(claim)
    return matched or claims


def _same_input_conflicts(
    claims: list[dict[str, Any]],
    *,
    subtask_id: str,
    evidence_refs: tuple[str, ...],
) -> list[ContractIssue]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in claims:
        subject = _claim_subject(claim)
        if not subject:
            continue
        for input_key in _claim_input_keys(claim):
            grouped.setdefault((subject, input_key), []).append(claim)

    issues: list[ContractIssue] = []
    for (subject, input_key), group in grouped.items():
        acceptance = {
            state
            for state in (_claim_acceptance(item) for item in group)
            if state is not None
        }
        outputs = {
            _json_key(value)
            for item in group
            for value in _claim_expected_outputs(item)
        }
        if acceptance == {True, False}:
            ids = [_claim_id(item, idx) for idx, item in enumerate(group, start=1)]
            issues.append(
                _issue(
                    "bad_generated_oracle",
                    subtask_id=subtask_id,
                    message=(
                        "Generated oracle has contradictory accept/reject "
                        f"claims for `{subject}` with input `{input_key}`."
                    ),
                    invalid_values=[f"{subject}:{input_key}", *ids],
                    required_deltas=[
                        {
                            "op": "remove",
                            "path": "proof.generated_test_contract.oracle_claims",
                            "values": ids,
                        }
                    ],
                    evidence_refs=evidence_refs,
                )
            )
        if len(outputs) > 1 and False not in acceptance:
            ids = [_claim_id(item, idx) for idx, item in enumerate(group, start=1)]
            issues.append(
                _issue(
                    "contradictory_required_behavior",
                    subtask_id=subtask_id,
                    message=(
                        "Generated oracle assigns multiple incompatible "
                        f"outputs to `{subject}` with input `{input_key}`."
                    ),
                    invalid_values=[f"{subject}:{input_key}", *sorted(outputs)],
                    required_deltas=[
                        {
                            "op": "remove",
                            "path": "proof.generated_test_contract.oracle_claims",
                            "values": ids,
                        }
                    ],
                    evidence_refs=evidence_refs,
                )
            )
    return issues


def _interface_domain_conflicts(
    contract: dict[str, Any],
    claims: list[dict[str, Any]],
    *,
    subtask_id: str,
    evidence_refs: tuple[str, ...],
) -> list[ContractIssue]:
    interface = contract.get("interface_model")
    if not isinstance(interface, dict):
        return []
    events = interface.get("events") or interface.get("apis") or []
    domains: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("name") or event.get("event") or event.get("api") or "").strip()
        if name:
            domains[name] = event

    issues: list[ContractIssue] = []
    for claim in claims:
        subject = _claim_subject(claim)
        domain = domains.get(subject)
        if not domain:
            continue
        accepted = _claim_acceptance(claim)
        if accepted is not True:
            continue
        invalid_hits = [
            value
            for value in _claim_raw_input_values(claim)
            if _value_is_invalid_for_domain(value, domain)
        ]
        if not invalid_hits:
            continue
        claim_id = _claim_id(claim, 1)
        issues.append(
            _issue(
                "oracle_domain_mismatch",
                subtask_id=subtask_id,
                message=(
                    f"Generated oracle claim `{claim_id}` accepts input outside "
                    f"the declared `{subject}` interface domain."
                ),
                invalid_values=[_json_key(item) for item in invalid_hits],
                required_deltas=[
                    {
                        "op": "remove",
                        "path": "proof.generated_test_contract.oracle_claims",
                        "values": [claim_id],
                    }
                ],
                evidence_refs=evidence_refs,
            )
        )
    return issues


def _claim_id(claim: dict[str, Any], index: int) -> str:
    return str(claim.get("claim_id") or f"claim_{index}").strip()


def _claim_subject(claim: dict[str, Any]) -> str:
    for key in ("subject", "event", "api", "handler", "name"):
        value = str(claim.get(key) or "").strip()
        if value:
            return value
    return ""


def _claim_input_keys(claim: dict[str, Any]) -> list[str]:
    if isinstance(claim.get("input_sequence"), list):
        return ["sequence:" + _json_key(claim.get("input_sequence"))]
    values = _claim_raw_input_values(claim)
    if not values:
        return ["<unspecified>"]
    return [_json_key(value) for value in values]


def _claim_raw_input_values(claim: dict[str, Any]) -> list[Any]:
    if isinstance(claim.get("input_values"), list):
        return list(claim.get("input_values") or [])
    for key in ("input_value", "input", "value", "argv", "request"):
        if key in claim:
            return [claim.get(key)]
    return []


def _claim_acceptance(claim: dict[str, Any]) -> bool | None:
    for key in ("accepted", "valid"):
        if isinstance(claim.get(key), bool):
            return bool(claim.get(key))
    for key in ("expectation", "expected_behavior", "behavior"):
        value = claim.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
        if normalized in {"accept", "accepted", "valid", "success", "succeeds"}:
            return True
        if normalized in {"reject", "rejected", "invalid", "error", "no-op", "noop"}:
            return False
    return None


def _claim_expected_outputs(claim: dict[str, Any]) -> list[Any]:
    outputs: list[Any] = []
    for key in (
        "expected_output",
        "expected_display",
        "expected_status",
        "expected_result",
        "expected_value",
    ):
        if key in claim:
            outputs.append(claim.get(key))
    return outputs


def _json_key(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(value)


def _value_is_invalid_for_domain(value: Any, domain: dict[str, Any]) -> bool:
    valid_values = domain.get("valid_values")
    if isinstance(valid_values, list) and valid_values:
        if value not in valid_values and _json_key(value) not in {
            _json_key(item) for item in valid_values
        }:
            return True
    invalid_values = domain.get("invalid_values")
    if not isinstance(invalid_values, list):
        return False
    if value in invalid_values or _json_key(value) in {_json_key(item) for item in invalid_values}:
        return True
    for rule in invalid_values:
        if _range_rule_matches(value, str(rule)):
            return True
    return False


def _range_rule_matches(value: Any, rule: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    match = re.fullmatch(r"\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\s*", rule)
    if not match:
        return False
    op, raw_threshold = match.groups()
    threshold = float(raw_threshold)
    if op == "<":
        return number < threshold
    if op == "<=":
        return number <= threshold
    if op == ">":
        return number > threshold
    return number >= threshold


def _issue(
    code: str,
    *,
    subtask_id: str,
    message: str,
    invalid_values: list[str] | None = None,
    required_deltas: list[dict[str, Any]] | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> ContractIssue:
    refs: list[EvidenceRef] = []
    for raw in evidence_refs:
        ref_type, _, ref_id = str(raw).partition(":")
        if ref_id:
            refs.append(
                EvidenceRef(
                    ref_type=ref_type,  # type: ignore[arg-type]
                    ref_id=ref_id,
                    produced_by="verifier",
                    subtask_id=subtask_id or None,
                )
            )
    return ContractIssue(
        code=code,
        severity="blocking",
        subtask_id=subtask_id,
        target_subtask_id=subtask_id,
        contract_path="proof.generated_test_contract.oracle_claims",
        invalid_values=tuple(invalid_values or ()),
        required_deltas=tuple(dict(item) for item in (required_deltas or ())),
        message=message,
        evidence_refs=tuple(refs),
    )
