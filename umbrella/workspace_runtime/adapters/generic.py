"""
Generic workspace adapter.

This adapter is the universal fallback for workspaces that follow the normal
Umbrella workspace layout but do not have a hand-written adapter yet.
"""

import asyncio
import importlib.util
import inspect
import json
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from umbrella.env import get_default_workspace_model, get_llm_env_config, load_env
from umbrella.workspace_registry.discovery import load_workspace_config
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


class GenericWorkspaceAdapter(BaseWorkspaceAdapter):
    """Universal fallback adapter for convention-based workspaces."""

    workspace_type = "generic"
    description = "Convention-based workspace adapter"

    def __init__(self, instance: WorkspaceInstance):
        super().__init__(instance)
        self._repo_root = Path(__file__).resolve().parents[3]
        self._workspace_ref = load_workspace_config(
            self.instance.path / "workspace.toml"
        )
        self._experiments_dir = self.instance.path / (
            self._workspace_ref.experiments_dir
            if self._workspace_ref
            else "experiments"
        )
        self._pipeline_path = self._find_pipeline_path()

    def _add_repo_paths(self) -> None:
        gmas_src = self._repo_root / "gmas" / "src"
        if gmas_src.exists() and str(gmas_src) not in sys.path:
            sys.path.insert(0, str(gmas_src))
        if str(self._repo_root) not in sys.path:
            sys.path.insert(0, str(self._repo_root))

    def _bootstrap_environment(self) -> None:
        load_env(repo_root=self._repo_root, extra_search_roots=(self.instance.path,))

    def _ensure_directories(self) -> None:
        for dir_name in ["runs", "snapshots", "reports", "memory", "logs"]:
            (self.instance.path / dir_name).mkdir(parents=True, exist_ok=True)

    def _find_pipeline_path(self) -> Path | None:
        if not self._experiments_dir.exists():
            return None

        seed_key = self.instance.seed_workspace_id or self.instance.workspace_id
        candidate_names = [
            "run_pipeline.py",
            f"run_{seed_key}_pipeline.py",
            f"run_{self.instance.workspace_id}_pipeline.py",
        ]

        for candidate_name in candidate_names:
            candidate = self._experiments_dir / candidate_name
            if candidate.exists():
                return candidate

        pipeline_candidates = sorted(self._experiments_dir.glob("run_*pipeline.py"))
        if pipeline_candidates:
            return pipeline_candidates[0]

        run_candidates = sorted(
            path
            for path in self._experiments_dir.glob("run_*.py")
            if path.name not in {"__init__.py", "interactive.py"}
        )
        if run_candidates:
            return run_candidates[0]

        return None

    def prepare(self) -> PreparedWorkspace:
        self._add_repo_paths()
        self._ensure_directories()

        validation_issues: list[str] = []
        if not (self.instance.path / "workspace.toml").exists():
            validation_issues.append("Missing required file: workspace.toml")
        if self._pipeline_path is None:
            validation_issues.append("Missing executable pipeline in experiments/")

        task_contract_candidates = []
        if self._workspace_ref is not None:
            task_contract_candidates.append(
                self.instance.path / self._workspace_ref.task_main_file
            )
        task_contract_candidates.append(self.instance.path / "TASK_MAIN.md")
        if not any(path.exists() for path in task_contract_candidates):
            expected_name = (
                self._workspace_ref.task_main_file
                if self._workspace_ref is not None
                and self._workspace_ref.task_main_file
                else "TASK_MAIN.md"
            )
            validation_issues.append(f"Missing task contract file: {expected_name}")

        if self._workspace_ref and self._workspace_ref.graph_file:
            graph_path = self.instance.path / self._workspace_ref.graph_file
            if not graph_path.exists():
                validation_issues.append(
                    f"Missing graph file: {self._workspace_ref.graph_file}"
                )

        if self._workspace_ref and self._workspace_ref.agents_dir:
            agents_path = self.instance.path / self._workspace_ref.agents_dir
            if not agents_path.exists():
                validation_issues.append(
                    f"Missing agents directory: {self._workspace_ref.agents_dir}"
                )

        graph_path = (
            self.instance.path / self._workspace_ref.graph_file
            if self._workspace_ref and self._workspace_ref.graph_file
            else None
        )

        return PreparedWorkspace(
            instance=self.instance,
            config_valid=len(validation_issues) == 0,
            validation_issues=validation_issues,
            profiles_loaded=True,
            graph_path=graph_path,
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

        effective_max_agent_executions = request.max_agent_executions
        if (
            isinstance(overrides.get("max_agent_executions"), int)
            and overrides["max_agent_executions"] > 0
        ):
            effective_max_agent_executions = overrides["max_agent_executions"]

        effective_mock_loops = request.mock_loops
        if isinstance(overrides.get("mock_loops"), bool):
            effective_mock_loops = overrides["mock_loops"]

        return {
            "query": "\n\n".join(part for part in query_parts if part),
            "retrieval_context": retrieval_context,
            "runtime_overrides": overrides,
            "max_agent_executions": effective_max_agent_executions,
            "mock_loops": effective_mock_loops,
            "live": bool(request.live and not effective_mock_loops),
        }

    def _load_pipeline_module(self) -> Any:
        if self._pipeline_path is None:
            raise RuntimeError("Could not resolve a pipeline script for this workspace")

        spec = importlib.util.spec_from_file_location(
            f"workspace_pipeline_{self.instance.instance_id}",
            str(self._pipeline_path),
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load workspace pipeline module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _build_pipeline_args(
        self,
        fn: Any,
        request: WorkspaceRunRequest,
        effective_inputs: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        signature = inspect.signature(fn)
        env_model, env_api_key, env_base_url = get_llm_env_config()
        candidate_values: dict[str, Any] = {
            "query": effective_inputs["query"],
            "task_input": effective_inputs["query"],
            "instruction": effective_inputs["query"],
            "prompt": effective_inputs["query"],
            "live": effective_inputs["live"],
            "mock_loops": effective_inputs["mock_loops"],
            "model": request.model or env_model or get_default_workspace_model(),
            "live_model": request.model or env_model or get_default_workspace_model(),
            "api_key": request.api_key or env_api_key,
            "live_api_key": request.api_key or env_api_key,
            "base_url": request.base_url or env_base_url,
            "live_base_url": request.base_url or env_base_url,
            "temperature": request.temperature,
            "live_temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "live_max_tokens": request.max_tokens,
            "tool_choice": request.tool_choice,
            "live_tool_choice": request.tool_choice,
            "max_agent_executions": effective_inputs["max_agent_executions"],
            "workspace_root": self.instance.path,
            "instance_path": self.instance.path,
            "report_name": request.report_name,
            "idea_report_name": request.idea_report_name,
            "task_id": request.task_id,
            "metadata": request.metadata,
            "request": request,
        }

        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        first_positional_bound = False
        for name, parameter in signature.parameters.items():
            if name == "self":
                continue
            if (
                not first_positional_bound
                and parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                and name in {"query", "task_input", "instruction", "prompt", "request"}
            ):
                args.append(candidate_values[name])
                first_positional_bound = True
                continue
            if name in candidate_values:
                kwargs[name] = candidate_values[name]
        return args, kwargs

    def _invoke_pipeline(
        self,
        module: Any,
        request: WorkspaceRunRequest,
        effective_inputs: dict[str, Any],
    ) -> Any:
        fn = getattr(module, "run_pipeline", None) or getattr(
            module, "run_workspace", None
        )
        if fn is None:
            raise RuntimeError(
                f"Workspace pipeline {self._pipeline_path.name if self._pipeline_path else '<missing>'} "
                "does not expose run_pipeline(...) or run_workspace(...)."
            )

        args, kwargs = self._build_pipeline_args(fn, request, effective_inputs)
        if inspect.iscoroutinefunction(fn):
            return fn(*args, **kwargs)
        return asyncio.to_thread(fn, *args, **kwargs)

    def _collect_report_candidates(
        self, run_dir: Path, payload: dict[str, Any]
    ) -> list[Path]:
        candidates: list[Path] = []
        report_path = payload.get("report_path")
        if report_path:
            candidates.append(Path(report_path))
        reports_dir = self.instance.path / "reports"
        if reports_dir.exists():
            candidates.extend(
                sorted(
                    reports_dir.glob("*.md"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
        candidates.extend(
            sorted(
                run_dir.glob("*.md"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        )
        deduped: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _write_run_manifest(self, run_dir: Path, payload: dict[str, Any]) -> Path:
        manifest_path = run_dir / "result_summary.json"
        manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return manifest_path

    async def run_async(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        self._add_repo_paths()
        self._bootstrap_environment()

        start_time = datetime.now(UTC)
        result = WorkspaceRunResult(
            run_id=f"{start_time.strftime('%Y%m%dT%H%M%S%fZ')}_{uuid4().hex[:8]}",
            workspace_id=self.instance.workspace_id,
            task_id=request.task_id,
            status=WorkspaceRunStatus.PREPARING,
            start_timestamp=start_time,
        )

        run_dir = self.instance.path / "runs" / result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result.run_dir = run_dir
        effective_inputs = self._build_effective_run_inputs(request)

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
            payload = await self._invoke_pipeline(module, request, effective_inputs)
            if not isinstance(payload, dict):
                payload = {
                    "status": "completed",
                    "final_answer": str(payload),
                    "execution_order": [],
                    "total_tokens": 0,
                }

            payload_run_id = str(payload.get("run_id") or result.run_id)
            payload_run_dir = Path(payload.get("run_dir") or run_dir)
            payload_run_dir.mkdir(parents=True, exist_ok=True)

            result.run_id = payload_run_id
            result.run_dir = payload_run_dir
            result.end_timestamp = datetime.now(UTC)
            result.duration_seconds = (
                result.end_timestamp - result.start_timestamp
            ).total_seconds()
            result.final_agent_id = str(payload.get("final_agent_id") or "")
            result.final_answer = str(payload.get("final_answer") or "")
            result.total_tokens = int(payload.get("total_tokens") or 0)
            execution_order = payload.get("execution_order")
            if isinstance(execution_order, list):
                result.agent_count = len(execution_order)
            else:
                execution_order = []

            normalized_status = str(payload.get("status") or "completed").lower()
            result.status = (
                WorkspaceRunStatus.COMPLETED
                if normalized_status
                in {"completed", "complete", "success", "succeeded"}
                else WorkspaceRunStatus.FAILED
            )
            if result.status == WorkspaceRunStatus.FAILED and payload.get("error"):
                result.errors.append(str(payload["error"]))
            if payload.get("traceback"):
                result.errors.append(str(payload["traceback"]))

            for report_path in self._collect_report_candidates(
                payload_run_dir, payload
            ):
                if report_path.exists():
                    result.add_artifact(
                        ArtifactRef(
                            artifact_id=f"report_{result.run_id}_{len(result.get_artifacts_by_type(ArtifactType.REPORT)) + 1}",
                            artifact_type=ArtifactType.REPORT,
                            path=report_path,
                            description=f"Workspace report: {report_path.name}",
                        )
                    )

            events_path = payload_run_dir / "events.jsonl"
            if events_path.exists():
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"events_{result.run_id}",
                        artifact_type=ArtifactType.LOG,
                        path=events_path,
                        description="Workspace run events",
                    )
                )

            snapshot_candidates = sorted(
                (self.instance.path / "snapshots").glob(f"*{result.run_id}*")
            )
            for snapshot_path in snapshot_candidates[:3]:
                artifact_type = (
                    ArtifactType.GRAPH_SNAPSHOT
                    if snapshot_path.suffix == ".json"
                    else ArtifactType.CUSTOM
                )
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"snapshot_{result.run_id}_{snapshot_path.name}",
                        artifact_type=artifact_type,
                        path=snapshot_path,
                        description=f"Workspace snapshot: {snapshot_path.name}",
                    )
                )

            manifest_payload = {
                "run_id": result.run_id,
                "workspace_id": result.workspace_id,
                "status": result.status.value,
                "query": effective_inputs["query"],
                "final_agent_id": result.final_agent_id,
                "final_answer": result.final_answer,
                "execution_order": execution_order,
                "total_tokens": result.total_tokens,
                "report_path": payload.get("report_path") or "",
                "errors": list(result.errors),
                "warnings": list(result.warnings),
                "runtime_overrides_used": runtime_overrides,
                "retrieval_context_injected": bool(retrieval_context),
                "source_pipeline": str(self._pipeline_path)
                if self._pipeline_path
                else "",
            }
            result.run_manifest_path = self._write_run_manifest(
                payload_run_dir, manifest_payload
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"summary_{result.run_id}",
                    artifact_type=ArtifactType.RUN_MANIFEST,
                    path=result.run_manifest_path,
                    description="Workspace run summary",
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
                    "mock_loops_effective": effective_inputs["mock_loops"],
                    "max_agent_executions_effective": effective_inputs[
                        "max_agent_executions"
                    ],
                    "manager_patch_count": self._manager_patch_count(),
                    "source_pipeline": str(self._pipeline_path)
                    if self._pipeline_path
                    else "",
                    "generic_adapter_used": True,
                }
            )
            result.summary = (
                f"Workspace run completed via generic adapter using {self._pipeline_path.name if self._pipeline_path else 'unknown pipeline'}."
                if result.status == WorkspaceRunStatus.COMPLETED
                else "Workspace run failed via generic adapter."
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
                    "source_pipeline": str(self._pipeline_path)
                    if self._pipeline_path
                    else "",
                },
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"summary_{result.run_id}",
                    artifact_type=ArtifactType.RUN_MANIFEST,
                    path=result.run_manifest_path,
                    description="Workspace run failure summary",
                )
            )
            result.summary = "Workspace run failed via generic adapter."

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

        for relative_name in ["graph", "agents", "prompts", "models", "tools"]:
            source_dir = instance.path / relative_name
            if source_dir.exists():
                shutil.copytree(source_dir, snapshot_path / relative_name)

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
                    "adapter": "generic",
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
        del workspace_id
        return True

    def get_supported_workspace_types(self) -> list[str]:
        return ["*"]
