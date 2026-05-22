"""Supervisor post-phase hooks for verify durable memory and reflexion BKB proposals."""

import json
import logging
from pathlib import Path
from typing import Any

from umbrella.contracts import EvidenceRef, VerificationReportRef, json_ready
from umbrella.contracts.validators import validate_verification_report_ref
from umbrella.memory.proactive.promotion import (
    ProposedBkbPatch,
    accept_bkb_patch,
    reject_bkb_patch,
)

log = logging.getLogger(__name__)


def _read_control_records(
    drive_root: Path,
    *,
    task_id: str,
    phase_started_at: float | None,
) -> list[dict[str, Any]]:
    state_dir = drive_root / "state"
    records: list[dict[str, Any]] = []
    ledger = state_dir / "phase_control_signals.jsonl"
    if ledger.is_file():
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    records.append(row)
        except OSError:
            log.debug("Failed to read phase control ledger", exc_info=True)
    single = state_dir / "phase_control_signal.json"
    if single.is_file():
        try:
            row = json.loads(single.read_text(encoding="utf-8"))
            if isinstance(row, dict):
                records.append(row)
        except (OSError, json.JSONDecodeError):
            pass

    filtered: list[dict[str, Any]] = []
    for row in records:
        row_task = str(row.get("task_id") or "")
        if task_id and row_task and row_task != task_id:
            continue
        ts = row.get("timestamp") or row.get("ts")
        if phase_started_at is not None and ts is not None:
            try:
                if float(ts) < float(phase_started_at):
                    continue
            except (TypeError, ValueError):
                pass
        filtered.append(row)
    return filtered


def promote_durable_row_is_valid(payload: dict[str, Any]) -> bool:
    if payload.get("saved") is not True:
        return False
    store = str(payload.get("durable_store") or payload.get("store") or "")
    node_id = str(payload.get("durable_node_id") or payload.get("canonical_id") or "").strip()
    return store == "palace.durable" and bool(node_id)


def has_valid_promote_durable(
    tools_log_path: Path,
    *,
    task_id: str,
) -> bool:
    if not task_id or not tools_log_path.is_file():
        return False
    try:
        for line in tools_log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("tool") or "") != "promote_to_durable":
                continue
            if str(row.get("task_id") or "") != task_id:
                continue
            payload = row.get("result")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            if isinstance(payload, dict) and promote_durable_row_is_valid(payload):
                return True
    except OSError:
        log.debug("Failed reading tools log for promote_to_durable", exc_info=True)
    return False


def mirror_verify_durable_if_needed(
    *,
    repo_root: Path,
    drive_root: Path,
    workspace_id: str,
    task_id: str,
    phase_started_at: float | None,
    tools_log_path: Path,
) -> str | None:
    """Write palace.durable verification_report when agent did not promote."""
    if has_valid_promote_durable(tools_log_path, task_id=task_id):
        return None

    report_ref: VerificationReportRef | None = None
    details = ""
    for row in reversed(_read_control_records(drive_root, task_id=task_id, phase_started_at=phase_started_at)):
        if str(row.get("kind") or "") != "submit_verification":
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if str(payload.get("status") or "") != "pass":
            continue
        raw_ref = payload.get("verification_report_ref")
        if isinstance(raw_ref, dict):
            report_ref = VerificationReportRef.from_mapping(raw_ref)
            details = str(payload.get("details") or "")
            break
    if report_ref is None:
        return None

    from umbrella.contracts import build_workspace_context
    from umbrella.memory.paths import workspace_root as ws_root_for

    ws_root = ws_root_for(repo_root, workspace_id) if workspace_id else repo_root
    context = build_workspace_context(
        repo_root=str(repo_root.resolve()),
        workspace_root=str(ws_root.resolve()),
        workspace_id=workspace_id,
    )
    issues = validate_verification_report_ref(
        report_ref,
        context=context,
        phase="verify",
    )
    if issues:
        log.warning("Skipping auto durable mirror: %s", issues[0].message)
        return None

    evidence = report_ref.evidence_ref(phase="verify")
    body = details.strip() or f"Verification report {report_ref.report_id}"
    try:
        from umbrella.memory.palace.facade import MemPalace

        palace = MemPalace(repo_root, workspace_id or None)
        try:
            node_id = palace.add(
                store="palace.durable",
                content=body,
                tier="warm",
                scope="cross_run_durable",
                tags=["durable", "verification_report"],
                phase="verify",
                verified=True,
                kind="verification_report",
                extra={"title": "Verification report", "trust_level": "public_verified"},
            )
        finally:
            palace.close()
    except Exception as exc:
        log.warning("Auto durable mirror failed: %s", exc)
        return None
    return node_id


def process_reflexion_bkb_patch(
    *,
    repo_root: Path,
    drive_root: Path,
    workspace_id: str,
) -> dict[str, Any] | None:
    """Supervisor accept/reject of proposed_bkb_patch.json after reflexion."""
    patch_path = drive_root / "state" / "proposed_bkb_patch.json"
    if not patch_path.is_file():
        return None
    try:
        doc = json.loads(patch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Invalid proposed_bkb_patch.json: %s", exc)
        return None
    if not isinstance(doc, dict):
        return None
    if str(doc.get("status") or "") != "candidate":
        return None

    ws = str(doc.get("workspace_id") or workspace_id or "")
    patch = ProposedBkbPatch(
        patch_id=str(doc.get("patch_id") or ""),
        rules=list(doc.get("rules") or []),
        source_evidence=list(doc.get("source_evidence") or []),
        actor="supervisor",
        run_id=str(doc.get("run_id") or ""),
        phase_id=str(doc.get("phase_id") or "reflexion"),
        workspace_id=ws,
    )
    target = "workspace" if ws else "manager"
    try:
        result = accept_bkb_patch(
            repo_root,
            patch,
            target=target,
            drive_root=drive_root,
        )
        doc["status"] = "accepted"
    except ValueError as exc:
        reject_bkb_patch(
            repo_root,
            patch,
            reason=str(exc),
            target=target,
        )
        doc["status"] = "rejected"
        doc["reject_reason"] = str(exc)
        result = {"accepted": False, "reason": str(exc)}
    try:
        patch_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return result
