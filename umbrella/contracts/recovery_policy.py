"""Deterministic recovery options derived from typed issues.

Reviewers/watchers diagnose problems.  They do not own PlanIR paths.  This
module maps issue codes plus current control-plane knowledge into canonical
recovery options that the runner can route and validators can enforce.
"""

from dataclasses import dataclass
from typing import Any, Mapping

from umbrella.contracts.models import ContractDelta, json_ready


@dataclass(frozen=True)
class RecoveryOption:
    code: str
    target_subtask_id: str
    reason_code: str
    required_plan_changes: tuple[dict[str, Any], ...] = ()
    required_deltas: tuple[ContractDelta, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "target_subtask_id": self.target_subtask_id,
            "reason_code": self.reason_code,
        }
        if self.required_plan_changes:
            payload["required_plan_changes"] = [
                json_ready(item) for item in self.required_plan_changes
            ]
        if self.required_deltas:
            payload["required_deltas"] = [
                item.to_payload() for item in self.required_deltas
            ]
        if self.evidence_refs:
            payload["evidence_refs"] = list(self.evidence_refs)
        return payload


def derive_recovery_options(
    issue: Mapping[str, Any],
    *,
    runtime_capability_available: bool = False,
) -> tuple[RecoveryOption, ...]:
    code = str(issue.get("code") or "").strip()
    target_subtask_id = str(
        issue.get("target_subtask_id") or issue.get("subtask_id") or ""
    ).strip()
    evidence_refs = tuple(
        str(item).strip()
        for item in (issue.get("evidence_refs") or [])
        if str(item).strip()
    ) if isinstance(issue.get("evidence_refs"), list) else ()
    if code != "headless_proof_uses_real_gui_root":
        return ()

    options = [
        RecoveryOption(
            code="replace_with_headless_controller_proof",
            target_subtask_id=target_subtask_id,
            reason_code=code,
            required_plan_changes=(
                {
                    "id": "headless-real-root-proof-target",
                    "target_subtask_id": target_subtask_id,
                    "severity": "blocking",
                    "reason_code": code,
                    "source": "RecoveryPolicy.headless_proof_uses_real_gui_root",
                    "path": "proof.scope.pytest_targets",
                    "op": "semantic_diff",
                    "evidence_refs": list(evidence_refs),
                },
            ),
            evidence_refs=evidence_refs,
        )
    ]
    if runtime_capability_available:
        options.append(
            RecoveryOption(
                code="upgrade_to_desktop_gui_runtime_proof",
                target_subtask_id=target_subtask_id,
                reason_code=code,
                required_deltas=(
                    ContractDelta(
                        op="replace",
                        path="proof.harness_profile",
                        value="desktop_gui_runtime",
                        target_subtask_id=target_subtask_id,
                        source_issue_code=code,
                    ),
                    ContractDelta(
                        op="add",
                        path="proof.required_capabilities",
                        values=("desktop_gui_runtime",),
                        target_subtask_id=target_subtask_id,
                        source_issue_code=code,
                    ),
                ),
                evidence_refs=evidence_refs,
            )
        )
    return tuple(options)


__all__ = ["RecoveryOption", "derive_recovery_options"]
