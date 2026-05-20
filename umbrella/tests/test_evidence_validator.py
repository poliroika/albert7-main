"""Tests for the FinalReport evidence validator."""
import pytest
from umbrella.orchestrator.final_report import (
    FinalReport, Evidence, CommandRun, VerificationReport,
    WatcherIncident, validate_final_report, build_final_report,
)


def _make_report(summary: str, commands=None, verifications=None) -> FinalReport:
    evidence = Evidence(
        commands_run=commands or [],
        verification_reports=verifications or [],
    )
    return FinalReport(
        run_id="run-test",
        workspace_id="ws-test",
        status="pass",
        human_summary_md=summary,
        claims_index={},
        evidence=evidence,
        phase_timeline=[],
    )


def test_valid_report_with_citations():
    report = _make_report(
        "The API was built [ev:cmd-1]. Tests passed [ev:verify-1].",
        commands=[CommandRun("cmd-1", "pytest tests/", 0, 1234, "execute")],
        verifications=[VerificationReport("verify-1", "workspace_verify", True, "")],
    )
    errors = validate_final_report(report)
    assert not errors


def test_missing_citation_fails():
    report = _make_report(
        "The API was built [ev:nonexistent-id].",
        commands=[CommandRun("cmd-1", "pytest", 0, 100, "execute")],
    )
    errors = validate_final_report(report)
    assert any("nonexistent-id" in e for e in errors)


def test_no_citations_in_summary_ok():
    report = _make_report(
        "This run completed successfully. The system is ready.",
    )
    errors = validate_final_report(report)
    assert not errors


def test_report_to_dict_structure():
    report = _make_report("Done.")
    d = report.to_dict()
    assert "run_id" in d
    assert "status" in d
    assert "evidence" in d
    assert "phase_timeline" in d
    assert "human_summary_md" in d


def test_build_final_report_from_drive(tmp_path):
    import json
    drive = tmp_path / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "logs" / "tools.jsonl").write_text(
        json.dumps({"id": "ev-1", "tool": "shell", "input": {"cmd": "pytest"}, "exit_code": 0, "duration_ms": 500, "phase": "execute"}) + "\n"
    )
    plan = {
        "nodes": [
            {"id": "research", "started_at": 1000.0, "ended_at": 1100.0, "status": "done"},
        ]
    }
    (drive / "state" / "phase_plan.json").write_text(json.dumps(plan))
    report = build_final_report("run-1", "ws-1", drive_root=drive)
    assert report.run_id == "run-1"
    assert len(report.evidence.commands_run) == 1
    assert report.evidence.commands_run[0].cmd == "pytest"
    assert len(report.phase_timeline) == 1
