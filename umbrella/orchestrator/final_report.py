import json
import pathlib
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ChangedFile:
    path: str
    sha_before: str
    sha_after: str
    by_subtask_id: str


@dataclass
class CommandRun:
    event_id: str
    cmd: str
    exit_code: int
    duration_ms: int
    phase: str


@dataclass
class VerificationReport:
    report_id: str
    kind: str
    passed: bool
    details_ref: str


@dataclass
class WatcherIncident:
    event_id: str
    signal: str
    reason: str


@dataclass
class MemoryPromotion:
    from_id: str
    to_store: str
    verified: bool


@dataclass
class UnresolvedRisk:
    description: str
    severity: str
    evidence_refs: list[str]


@dataclass
class Evidence:
    changed_files: list[ChangedFile] = field(default_factory=list)
    commands_run: list[CommandRun] = field(default_factory=list)
    verification_reports: list[VerificationReport] = field(default_factory=list)
    e2e_evidence: list[dict[str, Any]] = field(default_factory=list)
    watcher_incidents: list[WatcherIncident] = field(default_factory=list)
    memory_promotions: list[MemoryPromotion] = field(default_factory=list)
    reflections_applied: list[dict[str, Any]] = field(default_factory=list)
    unresolved_risks: list[UnresolvedRisk] = field(default_factory=list)


@dataclass
class PhaseTimelineEntry:
    phase: str
    started_at: str
    ended_at: str
    outcome: str


@dataclass
class FinalReport:
    run_id: str
    workspace_id: str
    status: str
    human_summary_md: str
    claims_index: dict[str, list[str]]
    evidence: Evidence
    phase_timeline: list[PhaseTimelineEntry]
    unverified_claims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CITATION_PATTERN = re.compile(r"\[(ev|art):([^\]]+)\]")


def validate_final_report(report: FinalReport) -> list[str]:
    """Returns list of validation errors. Empty = valid."""
    errors: list[str] = []
    all_evidence_ids: set[str] = set()
    for cmd in report.evidence.commands_run:
        all_evidence_ids.add(cmd.event_id)
    for vr in report.evidence.verification_reports:
        all_evidence_ids.add(vr.report_id)
    for inc in report.evidence.watcher_incidents:
        all_evidence_ids.add(inc.event_id)

    citations_found: dict[str, list[str]] = {}
    for sentence in re.split(r"[.!?]\s+", report.human_summary_md):
        cites = _CITATION_PATTERN.findall(sentence)
        for _, ref_id in cites:
            citations_found.setdefault(ref_id, []).append(sentence[:60])
            if ref_id not in all_evidence_ids:
                errors.append(f"Citation [{ref_id}] not found in evidence")

    return errors


def build_final_report(
    run_id: str,
    workspace_id: str,
    *,
    drive_root: pathlib.Path,
    human_summary_md: str = "",
    status: str = "pass",
) -> FinalReport:
    evidence = Evidence()
    tools_path = drive_root / "logs" / "tools.jsonl"
    if tools_path.exists():
        with tools_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    if ev.get("tool") in ("shell", "terminal_session"):
                        evidence.commands_run.append(CommandRun(
                            event_id=ev.get("id", str(uuid.uuid4())),
                            cmd=ev.get("input", {}).get("cmd", ""),
                            exit_code=ev.get("exit_code", 0),
                            duration_ms=ev.get("duration_ms", 0),
                            phase=ev.get("phase", ""),
                        ))
                except Exception:
                    pass

    plan_path = drive_root / "state" / "phase_plan.json"
    timeline: list[PhaseTimelineEntry] = []
    if plan_path.exists():
        try:
            plan_data = json.loads(plan_path.read_text())
            for node in plan_data.get("nodes", []):
                timeline.append(PhaseTimelineEntry(
                    phase=node["id"],
                    started_at=str(node.get("started_at", "")),
                    ended_at=str(node.get("ended_at", "")),
                    outcome=node.get("status", "unknown"),
                ))
        except Exception:
            pass

    return FinalReport(
        run_id=run_id,
        workspace_id=workspace_id,
        status=status,
        human_summary_md=human_summary_md,
        claims_index={},
        evidence=evidence,
        phase_timeline=timeline,
    )
