"""
Evaluation workspace adapter.

Minimal adapter for the evaluation seed workspace. Runs a straightforward
eval/review pipeline without the full article-research graph, proving that
the runtime adapter registry generalises beyond a single workspace type.
"""

import time
import uuid

from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter
from umbrella.workspace_runtime.models import (
    ArtifactRef,
    ArtifactType,
    WorkspaceRunRequest,
    WorkspaceRunResult,
    WorkspaceRunStatus,
)


class EvaluationAdapter(BaseWorkspaceAdapter):
    """Adapter for the evaluation seed workspace."""

    def run(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        run_id = f"eval_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        start = time.time()

        run_dir = self.instance.path / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        report_path = run_dir / "evaluation_report.md"
        report_path.write_text(
            f"# Evaluation Report\n\n"
            f"- **Task**: {request.query[:200]}\n"
            f"- **Run ID**: {run_id}\n"
            f"- **Mock mode**: {request.mock_loops}\n\n"
            f"## Assessment\n\n"
            f"Evaluation workspace executed successfully.\n",
            encoding="utf-8",
        )

        duration = time.time() - start

        return WorkspaceRunResult(
            workspace_id=self.instance.workspace_id,
            task_id=request.task_id,
            run_id=run_id,
            status=WorkspaceRunStatus.COMPLETED,
            final_answer=f"Evaluation completed for: {request.query[:100]}",
            duration_seconds=duration,
            total_tokens=0,
            artifacts=[
                ArtifactRef(
                    artifact_id=f"{run_id}_report",
                    artifact_type=ArtifactType.REPORT,
                    path=report_path,
                    description="Evaluation report",
                ),
            ],
        )

    def supports_workspace(self, workspace_id: str) -> bool:
        return workspace_id == "evaluation"

    def get_supported_workspace_types(self) -> list[str]:
        return ["evaluation"]
