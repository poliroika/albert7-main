"""Final report API endpoints."""

import pathlib
from typing import Any

from umbrella.orchestrator.final_report import build_final_report, validate_final_report
from umbrella.utils.result_envelope import ResultEnvelope


def get_run_report(run_id: str, workspace_id: str, *, drive_root: pathlib.Path) -> dict[str, Any]:
    report = build_final_report(run_id, workspace_id, drive_root=drive_root)
    errors = validate_final_report(report)
    return ResultEnvelope.success(data={
        "report": report.to_dict(),
        "validation_errors": errors,
    }, run_id=run_id).to_dict()
