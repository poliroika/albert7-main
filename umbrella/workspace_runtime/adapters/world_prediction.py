"""
World prediction workspace adapter.

This adapter brings the forecasting workspace into the shared runtime contract
so Umbrella can prepare, run, inspect, and optimize it the same way as other
seed workspaces.
"""

import asyncio
import importlib.util
import json
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from umbrella.env import get_default_workspace_model, get_llm_env_config, load_env
from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter
from umbrella.workspace_runtime.instances import load_instance_metadata
from umbrella.workspace_runtime.models import (
    ArtifactRef,
    ArtifactType,
    PreparedWorkspace,
    WorkspaceInspection,
    WorkspaceInstance,
    WorkspaceRunRequest,
    WorkspaceRunResult,
    WorkspaceRunStatus,
    WorkspaceSnapshot,
)


def _forecast_reports_slug(forecast_id: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in forecast_id.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "forecast"


class WorldPredictionAdapter(BaseWorkspaceAdapter):
    """Adapter for the world_prediction seed workspace."""

    workspace_type = "world_prediction"
    description = "World event forecasting workspace with a GMAS prediction pipeline"

    def __init__(self, instance: WorkspaceInstance):
        super().__init__(instance)
        self._repo_root = Path(__file__).resolve().parents[3]
        self._gmas_path = self._repo_root / "gmas" / "src"
        self._pipeline_path = self._find_pipeline_path()

    def _find_pipeline_path(self) -> Path:
        candidates = [
            self.instance.path / "experiments" / "run_prediction_pipeline.py",
            self._repo_root
            / "workspaces"
            / "world_prediction"
            / "experiments"
            / "run_prediction_pipeline.py",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("Could not find run_prediction_pipeline.py")

    def _add_gmas_to_path(self) -> None:
        if self._gmas_path.exists() and str(self._gmas_path) not in sys.path:
            sys.path.insert(0, str(self._gmas_path))
        if str(self._repo_root) not in sys.path:
            sys.path.insert(0, str(self._repo_root))

    def _bootstrap_environment(self) -> None:
        load_env(repo_root=self.instance.path, extra_search_roots=(self._repo_root,))

    def _ensure_directories(self) -> None:
        for dir_name in ["runs", "snapshots", "reports", "memory", "logs", "data"]:
            (self.instance.path / dir_name).mkdir(parents=True, exist_ok=True)

    def prepare(self) -> PreparedWorkspace:
        self._add_gmas_to_path()
        self._ensure_directories()

        validation_issues: list[str] = []
        for required_file in [
            "workspace.toml",
            "graph/topology.toml",
            "experiments/run_prediction_pipeline.py",
        ]:
            if not (self.instance.path / required_file).exists():
                validation_issues.append(f"Missing required file: {required_file}")

        if not (self.instance.path / "TASK_MAIN.md").exists():
            validation_issues.append("Missing task contract file: TASK_MAIN.md")

        if not (self.instance.path / "agents").exists():
            validation_issues.append("Missing agents directory")

        return PreparedWorkspace(
            instance=self.instance,
            config_valid=len(validation_issues) == 0,
            validation_issues=validation_issues,
            profiles_loaded=True,
            graph_path=self.instance.path / "graph" / "topology.toml",
            tools_registered=True,
            ready=len(validation_issues) == 0,
            not_ready_reason="; ".join(validation_issues)
            if validation_issues
            else None,
        )

    def _load_runtime_overrides(self) -> dict[str, Any]:
        metadata = load_instance_metadata(self.instance.path) or {}
        overrides = metadata.get("runtime_overrides", {})
        return overrides if isinstance(overrides, dict) else {}

    def _manager_patch_count(self) -> int:
        metadata = load_instance_metadata(self.instance.path) or {}
        history = metadata.get("manager_patch_history", [])
        return len(history) if isinstance(history, list) else 0

    def _build_effective_run_inputs(
        self, request: WorkspaceRunRequest
    ) -> dict[str, Any]:
        overrides = self._load_runtime_overrides()
        retrieval_context = ""
        if isinstance(request.metadata, dict):
            retrieval_context = str(request.metadata.get("retrieval_context") or "")

        query_parts = [request.query.strip()]
        if retrieval_context.strip():
            query_parts.extend(["## Repository Guidance", retrieval_context.strip()])

        query_suffix = overrides.get("query_suffix")
        if isinstance(query_suffix, str) and query_suffix.strip():
            query_parts.extend(["## Instance Guidance", query_suffix.strip()])

        effective_query = "\n\n".join(part for part in query_parts if part)
        effective_max_agent_executions = request.max_agent_executions
        if (
            isinstance(overrides.get("max_agent_executions"), int)
            and overrides["max_agent_executions"] > 0
        ):
            effective_max_agent_executions = overrides["max_agent_executions"]

        effective_live = bool(request.live and not request.mock_loops)
        return {
            "query": effective_query,
            "retrieval_context": retrieval_context,
            "runtime_overrides": overrides,
            "max_agent_executions": effective_max_agent_executions,
            "live": effective_live,
        }

    def _load_pipeline_module(self) -> Any:
        spec = importlib.util.spec_from_file_location(
            "run_prediction_pipeline", str(self._pipeline_path)
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load run_prediction_pipeline module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _write_run_manifest(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        manifest_path = run_dir / "result_summary.json"
        manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return manifest_path

    async def run_async(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        self._add_gmas_to_path()
        self._bootstrap_environment()

        start_time = datetime.now(UTC)
        result = WorkspaceRunResult(
            run_id=f"{start_time.strftime('%Y%m%dT%H%M%S%fZ')}_{uuid4().hex[:8]}",
            workspace_id=self.instance.workspace_id,
            task_id=request.task_id,
            status=WorkspaceRunStatus.PREPARING,
            start_timestamp=start_time,
        )

        effective_inputs = self._build_effective_run_inputs(request)
        run_dir = self.instance.path / "runs" / result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result.run_dir = run_dir

        retrieval_context = effective_inputs["retrieval_context"]
        runtime_overrides = effective_inputs["runtime_overrides"]

        if retrieval_context:
            retrieval_path = run_dir / "retrieval_context.md"
            retrieval_path.write_text(
                retrieval_context.rstrip() + "\n", encoding="utf-8"
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"retrieval_{result.run_id}",
                    artifact_type=ArtifactType.CUSTOM,
                    path=retrieval_path,
                    description="Repository retrieval context injected by the manager",
                )
            )

        if runtime_overrides:
            overrides_path = run_dir / "runtime_overrides.json"
            overrides_path.write_text(
                json.dumps(
                    runtime_overrides, indent=2, ensure_ascii=False, default=str
                ),
                encoding="utf-8",
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"runtime_overrides_{result.run_id}",
                    artifact_type=ArtifactType.CUSTOM,
                    path=overrides_path,
                    description="Effective runtime overrides loaded from instance metadata",
                )
            )

        try:
            result.status = WorkspaceRunStatus.RUNNING
            module = self._load_pipeline_module()
            env_model, env_api_key, env_base_url = get_llm_env_config()

            outcome = await module.run_pipeline(
                effective_inputs["query"],
                live=effective_inputs["live"],
                live_model=request.model or env_model or get_default_workspace_model(),
                live_api_key=request.api_key or env_api_key,
                live_base_url=request.base_url or env_base_url,
                live_temperature=request.temperature,
                live_max_tokens=request.max_tokens,
                live_tool_choice=request.tool_choice,
                max_agent_executions=effective_inputs["max_agent_executions"],
            )

            result.run_id = str(outcome.get("run_id") or result.run_id)
            run_dir = Path(outcome.get("run_dir") or run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            result.run_dir = run_dir
            result.end_timestamp = datetime.now(UTC)
            result.duration_seconds = (
                result.end_timestamp - result.start_timestamp
            ).total_seconds()
            result.final_agent_id = str(outcome.get("final_agent_id") or "")
            result.final_answer = str(outcome.get("final_answer") or "")
            result.total_tokens = int(outcome.get("total_tokens") or 0)

            execution_order = outcome.get("execution_order")
            if isinstance(execution_order, list):
                result.agent_count = len(execution_order)
            else:
                execution_order = []

            report_path_value = outcome.get("report_path")
            if report_path_value:
                report_path = Path(report_path_value)
            else:
                fid = str(outcome.get("forecast_id") or "").strip()
                slug = _forecast_reports_slug(fid) if fid else "forecast"
                report_path = (
                    self.instance.path / "reports" / slug / "latest_prediction.md"
                )
            if report_path.exists():
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"report_{result.run_id}",
                        artifact_type=ArtifactType.REPORT,
                        path=report_path,
                        description="Prediction report",
                    )
                )

            events_path = run_dir / "events.jsonl"
            if events_path.exists():
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"events_{result.run_id}",
                        artifact_type=ArtifactType.LOG,
                        path=events_path,
                        description="Prediction run events",
                    )
                )

            graph_candidates: list[Path] = []
            fid = str(outcome.get("forecast_id") or "").strip()
            if fid:
                graph_candidates.append(
                    self.instance.path
                    / "snapshots"
                    / _forecast_reports_slug(fid)
                    / f"{result.run_id}_graph.json"
                )
            graph_candidates.append(
                self.instance.path / "snapshots" / f"{result.run_id}_graph.json"
            )
            graph_snapshot_path = next(
                (p for p in graph_candidates if p.exists()), None
            )
            if graph_snapshot_path:
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"graph_{result.run_id}",
                        artifact_type=ArtifactType.GRAPH_SNAPSHOT,
                        path=graph_snapshot_path,
                        description="Prediction graph snapshot",
                    )
                )

            if str(outcome.get("status")).lower() == "completed":
                result.status = WorkspaceRunStatus.COMPLETED
                result.summary = (
                    f"Prediction run completed successfully. Final agent: {result.final_agent_id}. "
                    f"Tokens: {result.total_tokens}. Duration: {result.duration_str}"
                )
            else:
                result.status = WorkspaceRunStatus.FAILED
                if outcome.get("error"):
                    result.errors.append(str(outcome["error"]))
                if outcome.get("traceback"):
                    result.errors.append(str(outcome["traceback"]))
                result.summary = "Prediction run failed"

            manifest_payload = {
                "run_id": result.run_id,
                "workspace_id": result.workspace_id,
                "status": result.status.value,
                "query": effective_inputs["query"],
                "final_agent_id": result.final_agent_id,
                "final_answer": result.final_answer,
                "execution_order": execution_order,
                "total_tokens": result.total_tokens,
                "report_path": str(report_path) if report_path else "",
                "errors": list(result.errors),
                "runtime_overrides_used": runtime_overrides,
                "retrieval_context_injected": bool(retrieval_context),
            }
            result.run_manifest_path = self._write_run_manifest(
                run_dir, manifest_payload
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"summary_{result.run_id}",
                    artifact_type=ArtifactType.RUN_MANIFEST,
                    path=result.run_manifest_path,
                    description="Prediction run summary",
                )
            )
            result.metrics.update(
                {
                    "retrieval_context_injected": bool(retrieval_context),
                    "retrieval_hits_used": int(
                        request.metadata.get("retrieval_hit_count", 0)
                    )
                    if isinstance(request.metadata, dict)
                    else 0,
                    "runtime_overrides_used": list(runtime_overrides.keys()),
                    "max_agent_executions_effective": effective_inputs[
                        "max_agent_executions"
                    ],
                    "live_effective": effective_inputs["live"],
                    "manager_patch_count": self._manager_patch_count(),
                }
            )

        except Exception as exc:
            result.status = WorkspaceRunStatus.FAILED
            result.end_timestamp = datetime.now(UTC)
            result.duration_seconds = (
                result.end_timestamp - result.start_timestamp
            ).total_seconds()
            trace_text = traceback.format_exc()
            result.errors.append(str(exc))
            result.errors.append(trace_text)

            trace_path = run_dir / "failure_traceback.txt"
            trace_path.write_text(trace_text.rstrip() + "\n", encoding="utf-8")
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"traceback_{result.run_id}",
                    artifact_type=ArtifactType.LOG,
                    path=trace_path,
                    description="Failure traceback",
                )
            )

            result.run_manifest_path = self._write_run_manifest(
                run_dir,
                {
                    "run_id": result.run_id,
                    "workspace_id": result.workspace_id,
                    "status": result.status.value,
                    "query": effective_inputs["query"],
                    "final_agent_id": "",
                    "final_answer": "",
                    "execution_order": [],
                    "total_tokens": 0,
                    "report_path": "",
                    "errors": list(result.errors),
                },
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"summary_{result.run_id}",
                    artifact_type=ArtifactType.RUN_MANIFEST,
                    path=result.run_manifest_path,
                    description="Prediction run failure summary",
                )
            )
            result.summary = "Prediction run failed"

        return result

    def run(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        return asyncio.run(self.run_async(request))

    def inspect(self, result: WorkspaceRunResult) -> WorkspaceInspection:
        execution_order: list[str] = []
        if result.run_manifest_path and result.run_manifest_path.exists():
            try:
                data = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
                raw_execution_order = data.get("execution_order", [])
                if isinstance(raw_execution_order, list):
                    execution_order = [str(item) for item in raw_execution_order]
            except Exception:
                execution_order = []

        return WorkspaceInspection(
            run_id=result.run_id,
            workspace_id=result.workspace_id,
            status=result.status,
            agents_executed=execution_order,
            execution_order=execution_order,
            final_answer=result.final_answer,
            key_artifacts=result.get_artifacts_by_type(ArtifactType.REPORT),
            errors=result.errors,
            warnings=result.warnings,
            total_tokens=result.total_tokens,
            duration_seconds=result.duration_seconds,
        )

    def snapshot(
        self,
        instance: WorkspaceInstance,
        label: str,
        include_artifacts: bool = True,
    ) -> WorkspaceSnapshot:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        snapshot_id = uuid4().hex[:8]
        snapshot_name = f"{timestamp}_{label.replace(' ', '_')[:40]}_{snapshot_id}"
        snapshot_path = instance.path / "snapshots" / snapshot_name
        snapshot_path.mkdir(parents=True, exist_ok=True)

        for relative_dir in ["graph", "agents", "prompts", "models"]:
            source_dir = instance.path / relative_dir
            if source_dir.exists():
                shutil.copytree(source_dir, snapshot_path / relative_dir)

        for filename in ["TASK_MAIN.md", "workspace.toml", "policies.toml"]:
            source_file = instance.path / filename
            if source_file.exists():
                shutil.copy2(source_file, snapshot_path / filename)

        if include_artifacts:
            for relative_dir in ["reports", "runs"]:
                source_dir = instance.path / relative_dir
                if source_dir.exists():
                    shutil.copytree(
                        source_dir, snapshot_path / relative_dir, dirs_exist_ok=True
                    )

        metadata_path = snapshot_path / "snapshot_metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "instance_id": instance.instance_id,
                    "workspace_id": instance.workspace_id,
                    "label": label,
                    "created_at": datetime.now(UTC).isoformat(),
                    "snapshot_path": str(snapshot_path),
                    "source_path": str(instance.path),
                    "includes_artifacts": include_artifacts,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        return WorkspaceSnapshot(
            snapshot_id=snapshot_id,
            instance_id=instance.instance_id,
            workspace_id=instance.workspace_id,
            label=label,
            snapshot_path=snapshot_path,
            source_path=instance.path,
            includes_graph=(snapshot_path / "graph").exists(),
            includes_memory=False,
            includes_prompts=(snapshot_path / "prompts").exists(),
            includes_artifacts=include_artifacts,
        )

    def get_run_manifest(self, result: WorkspaceRunResult) -> dict[str, Any]:
        if result.run_manifest_path and result.run_manifest_path.exists():
            return json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
        return super().get_run_manifest(result)

    def get_log_summary(self, result: WorkspaceRunResult) -> str:
        summary_lines = [
            f"Run ID: {result.run_id}",
            f"Workspace: {result.workspace_id}",
            f"Status: {result.status.value}",
            f"Duration: {result.duration_str}",
            f"Tokens: {result.total_tokens}",
            f"Agents: {result.agent_count}",
        ]
        if result.errors:
            summary_lines.append(f"Errors: {len(result.errors)}")
        if result.final_answer:
            summary_lines.append(f"Final answer: {result.final_answer[:200]}...")
        return "\n".join(summary_lines)

    def supports_workspace(self, workspace_id: str) -> bool:
        return workspace_id == "world_prediction"

    def get_supported_workspace_types(self) -> list[str]:
        return ["world_prediction"]
