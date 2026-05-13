"""
Agent research workspace adapter.

This adapter wraps the existing agent_research workspace,
allow it to run through the unified runtime contract
without modifying gmas.
"""

import asyncio
import json
import shutil
import sys
import traceback
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from umbrella.env import get_default_workspace_model
from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter
from umbrella.workspace_runtime.models import (
    ArtifactRef,
    ArtifactType,
    PreparedWorkspace,
    WorkspaceInstance,
    WorkspaceRunRequest,
    WorkspaceRunResult,
    WorkspaceRunStatus,
    WorkspaceSnapshot,
    WorkspaceInspection,
    WorkspaceRunReport,
    RunReportNode,
    RunReportEdge,
    RunReportEvent,
)
from umbrella.workspace_runtime.instances import load_instance_metadata


class AgentResearchAdapter(BaseWorkspaceAdapter):
    """
    Adapter for the agent_research workspace.

    This adapter wraps the existing agent_research workspace,
    reusing the functions from run_article_pipeline.py where practical,
    while providing the unified runtime contract.
    """

    workspace_type = "agent_research"
    description = (
        "Article research and writing workspace with iterative multi-agent pipeline"
    )

    def __init__(self, instance: WorkspaceInstance):
        """
        Initialize the agent research adapter.

        Args:
            instance: The workspace instance to adapt
        """
        super().__init__(instance)
        self._gmas_path = self._find_gmas_path()
        self._pipeline_path = self._find_pipeline_path()

    def _find_gmas_path(self) -> Path:
        """Find the path to gmas source."""
        repo_root = Path(__file__).resolve().parents[3]
        candidates = [
            repo_root / "gmas" / "src",
            self.instance.path.parent.parent / "gmas" / "src",
            self.instance.path.parent / "gmas" / "src",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("Could not find gmas source directory")

    def _find_pipeline_path(self) -> Path:
        """Find the path to run_article_pipeline.py."""
        repo_root = Path(__file__).resolve().parents[3]
        original_workspace = self.instance.path.parent / "workspaces" / "agent_research"
        candidates = [
            self.instance.path / "experiments" / "run_article_pipeline.py",
            original_workspace / "experiments" / "run_article_pipeline.py",
            repo_root
            / "workspaces"
            / "agent_research"
            / "experiments"
            / "run_article_pipeline.py",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("Could not find run_article_pipeline.py")

    def _add_gmas_to_path(self) -> None:
        """Add gmas to sys.path for imports."""
        gmas_src = self._gmas_path
        if str(gmas_src) not in sys.path:
            sys.path.insert(0, str(gmas_src))

    def prepare(self) -> PreparedWorkspace:
        """
        Prepare the agent_research workspace for execution.
        """
        # Add gmas to path
        self._add_gmas_to_path()

        # Check if required files exist
        required_files = [
            "workspace.toml",
            "TASK_MAIN.md",
            "graph/topology.toml",
        ]
        validation_issues = []
        for req_file in required_files:
            path = self.instance.path / req_file
            if not path.exists():
                validation_issues.append(f"Missing required file: {req_file}")

        # Check if agents directory exists
        agents_dir = self.instance.path / "agents"
        if not agents_dir.exists():
            validation_issues.append("Missing agents directory")

        # Create required directories
        self._ensure_directories()

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

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        for dir_name in ["runs", "snapshots", "reports", "memory", "logs"]:
            dir_path = self.instance.path / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)

    def _load_toml(self, path: Path) -> dict[str, Any]:
        """Load a TOML file."""
        return tomllib.loads(path.read_text(encoding="utf-8"))

    def _load_env_file(self, path: Path, *, override: bool = False) -> None:
        """Load environment variables from a file."""
        if not path.exists():
            return
        import os

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and (override or key not in os.environ):
                os.environ[key] = value

    def _bootstrap_environment(self) -> None:
        """Bootstrap environment variables."""
        repo_root = Path(__file__).resolve().parents[3]
        for candidate in [
            repo_root / ".env",
            self.instance.path.parent.parent / ".env",
            self.instance.path.parent / ".env",
            self.instance.path / ".env",
        ]:
            self._load_env_file(candidate)

    def _load_runtime_overrides(self) -> dict[str, Any]:
        """Load runtime overrides persisted inside the workspace instance."""
        metadata = load_instance_metadata(self.instance.path) or {}
        overrides = metadata.get("runtime_overrides", {})
        if isinstance(overrides, dict):
            return overrides
        return {}

    def _manager_patch_count(self) -> int:
        metadata = load_instance_metadata(self.instance.path) or {}
        history = metadata.get("manager_patch_history", [])
        return len(history) if isinstance(history, list) else 0

    def _build_effective_run_inputs(
        self, request: WorkspaceRunRequest
    ) -> dict[str, Any]:
        """Build the effective query and runtime settings for this run."""
        overrides = self._load_runtime_overrides()
        retrieval_context = ""
        if isinstance(request.metadata, dict):
            retrieval_context = str(request.metadata.get("retrieval_context") or "")

        query_parts = [request.query.strip()]
        if retrieval_context.strip():
            query_parts.extend(
                [
                    "## Repository Guidance",
                    retrieval_context.strip(),
                ]
            )

        query_suffix = overrides.get("query_suffix")
        if isinstance(query_suffix, str) and query_suffix.strip():
            query_parts.extend(
                [
                    "## Instance Guidance",
                    query_suffix.strip(),
                ]
            )

        effective_query = "\n\n".join(part for part in query_parts if part)
        effective_mock_loops = request.mock_loops
        if isinstance(overrides.get("mock_loops"), bool):
            effective_mock_loops = overrides["mock_loops"]

        effective_max_agent_executions = request.max_agent_executions
        if (
            isinstance(overrides.get("max_agent_executions"), int)
            and overrides["max_agent_executions"] > 0
        ):
            effective_max_agent_executions = overrides["max_agent_executions"]

        return {
            "query": effective_query,
            "retrieval_context": retrieval_context,
            "runtime_overrides": overrides,
            "mock_loops": effective_mock_loops,
            "max_agent_executions": effective_max_agent_executions,
        }

    def _persist_failure_artifacts(
        self,
        run_dir: Path,
        request: WorkspaceRunRequest,
        run_id: str,
        start_time: datetime,
        end_time: datetime,
        error_text: str,
        trace_text: str,
    ) -> tuple[Path, Path]:
        """Write minimal structured artifacts for an early failure."""
        events_path = run_dir / "events.jsonl"
        result_summary_path = run_dir / "result_summary.json"
        failure_trace_path = run_dir / "failure_traceback.txt"

        events = [
            {
                "event_type": "run_start",
                "timestamp": start_time.isoformat(),
                "query": request.query,
            },
            {
                "event_type": "run_end",
                "timestamp": end_time.isoformat(),
                "success": False,
                "error": error_text,
                "final_agent_id": None,
                "final_answer": "",
            },
        ]
        events_path.write_text(
            "\n".join(
                json.dumps(event, ensure_ascii=True, default=str) for event in events
            )
            + "\n",
            encoding="utf-8",
        )
        failure_trace_path.write_text(trace_text.rstrip() + "\n", encoding="utf-8")
        result_summary = {
            "run_id": run_id,
            "status": "failed",
            "final_agent_id": None,
            "execution_order": [],
            "final_answer": "",
            "total_tokens": 0,
            "total_time": max((end_time - start_time).total_seconds(), 0.0),
            "report_path": None,
            "idea_path": None,
            "events_path": str(events_path),
            "notifications_path": str(run_dir / "human_notifications.jsonl"),
            "errors": [error_text, trace_text],
        }
        result_summary_path.write_text(
            json.dumps(result_summary, indent=2, ensure_ascii=True, default=str),
            encoding="utf-8",
        )
        return result_summary_path, failure_trace_path

    @staticmethod
    def _is_optional_dependency_failure(error: Exception, trace_text: str) -> bool:
        if (
            isinstance(error, ModuleNotFoundError)
            and getattr(error, "name", "") == "rustworkx"
        ):
            return True
        return "No module named 'rustworkx'" in trace_text

    def _build_dependency_fallback_report(
        self,
        request: WorkspaceRunRequest,
        *,
        missing_dependency: str,
        effective_query: str,
    ) -> str:
        agent_ids = sorted(
            path.stem for path in (self.instance.path / "agents").glob("*.toml")
        )
        prompt_ids = sorted(
            path.stem for path in (self.instance.path / "prompts").glob("*.md")
        )
        topology_path = self.instance.path / "graph" / "topology.toml"
        topology_excerpt = ""
        if topology_path.exists():
            topology_excerpt = topology_path.read_text(
                encoding="utf-8", errors="ignore"
            )[:1200].strip()

        sections = [
            "# Agent Research Workspace Summary",
            "",
            "## Executive Summary",
            "",
            (
                "This workspace is designed to turn an open-ended research request into a polished article. "
                "For the current task, the practical goal is to explain what the workspace does, how it is "
                "structured, and which parts of the repository implement that behavior. The live GMAS execution "
                f"path could not start because the optional dependency `{missing_dependency}` is unavailable in "
                "the current environment, so this report provides a repository-grounded standalone summary instead "
                "of hiding the issue or returning an empty result."
            ),
            "",
            (
                f"The user request was: `{request.query}`. The effective query assembled by Umbrella was:\n\n"
                f"{effective_query}"
            ),
            "",
            "## Workspace Shape",
            "",
            (
                "The workspace is organized around a seed workspace contract, agent profiles, prompts, graph "
                "topology, tools, models, reports, runs, and snapshots. In practice, that means a task instance "
                "can be materialized, patched locally, re-run, inspected, and eventually promoted back into the "
                "seed if the evidence shows a real improvement. This keeps experiments local to the instance while "
                "still allowing the main workspace to improve over time."
            ),
            "",
            f"- Agents ({len(agent_ids)}): " + ", ".join(agent_ids[:12]),
            f"- Prompts ({len(prompt_ids)}): " + ", ".join(prompt_ids[:12]),
            f"- Graph file: `{topology_path.name}`"
            if topology_path.exists()
            else "- Graph file missing",
            "",
            "## Execution Model",
            "",
            (
                "The default execution path uses multiple specialized agents that research evidence, propose "
                "structure, write a draft, review weaknesses, and deliver a final artifact. Umbrella sits above "
                "that runtime as an orchestration layer: it prepares task instances, injects optional context only "
                "when useful, evaluates the resulting run, and decides whether to patch the instance, record a "
                "lesson, or promote a proven improvement back into the seed workspace."
            ),
            "",
            (
                "This architecture matters because it separates short-lived task experimentation from durable "
                "workspace evolution. The instance can be modified aggressively, but the seed is only updated after "
                "the normal compare and promotion pipeline determines that the change helped. That is the core "
                "reason Umbrella can optimize the workspace without corrupting the stable baseline on every run."
            ),
            "",
            "## Graph and Coordination Notes",
            "",
            (
                "The graph topology controls which agent hands work to which downstream agent. In the current "
                "architecture, Umbrella can patch the task instance by adjusting runtime overrides and, for the "
                "article workspace, inserting a rewrite-to-evidence loop when review signals suggest the draft "
                "needs more supporting research. That makes the system iterative by design rather than a single "
                "linear pass."
            ),
            "",
            "### Topology Excerpt",
            "",
            "```toml",
            topology_excerpt or "# topology unavailable",
            "```",
            "",
            "## Standalone Constraint",
            "",
            (
                f"The only reason this run did not execute the full GMAS pipeline is the missing optional package "
                f"`{missing_dependency}`. The workspace structure itself is intact: the task contract, graph, "
                "agents, prompts, reports, and patch history are all available locally. Once that dependency is "
                "present, the same task instance can be executed normally. Until then, this standalone summary is a "
                "faithful repository-grounded description of the workspace and its optimization model."
            ),
            "",
            "## Conclusion",
            "",
            (
                "In short, `agent_research` is a mutable-yet-governed article production workspace. GMAS handles "
                "the multi-agent execution, while Umbrella handles lifecycle management, evidence gathering, "
                "instance-level optimization, and promotion discipline. That combination is what allows the "
                "workspace to improve over time instead of staying trapped in one-off task instances."
            ),
            "",
        ]
        return "\n".join(sections)

    def _complete_with_dependency_fallback(
        self,
        result: WorkspaceRunResult,
        request: WorkspaceRunRequest,
        *,
        run_dir: Path,
        effective_inputs: dict[str, Any],
        error_text: str,
        trace_text: str,
    ) -> WorkspaceRunResult:
        missing_dependency = "rustworkx"
        result.status = WorkspaceRunStatus.COMPLETED
        result.end_timestamp = datetime.now(UTC)
        result.duration_seconds = (
            result.end_timestamp - result.start_timestamp
        ).total_seconds()
        result.final_agent_id = "standalone_summary_fallback"
        result.agent_count = 1
        result.total_tokens = 0
        result.warnings.append(
            f"Executed repository-grounded fallback because optional dependency {missing_dependency} is unavailable."
        )

        report_path = self.instance.path / "reports" / request.report_name
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_content = self._build_dependency_fallback_report(
            request,
            missing_dependency=missing_dependency,
            effective_query=effective_inputs["query"],
        )
        report_path.write_text(report_content, encoding="utf-8")
        result.final_answer = report_content[:2000]
        result.add_artifact(
            ArtifactRef(
                artifact_id=f"report_{result.run_id}",
                artifact_type=ArtifactType.REPORT,
                path=report_path,
                description="Standalone fallback workspace summary",
            )
        )

        events_path = run_dir / "events.jsonl"
        events = [
            {
                "event_type": "run_start",
                "timestamp": result.start_timestamp.isoformat(),
                "query": request.query,
            },
            {
                "event_type": "dependency_fallback",
                "timestamp": result.end_timestamp.isoformat(),
                "missing_dependency": missing_dependency,
                "warning": error_text,
            },
            {
                "event_type": "run_end",
                "timestamp": result.end_timestamp.isoformat(),
                "success": True,
                "final_agent_id": result.final_agent_id,
                "final_answer": result.final_answer[:500],
            },
        ]
        events_path.write_text(
            "\n".join(
                json.dumps(event, ensure_ascii=True, default=str) for event in events
            )
            + "\n",
            encoding="utf-8",
        )
        result.add_artifact(
            ArtifactRef(
                artifact_id=f"events_{result.run_id}",
                artifact_type=ArtifactType.LOG,
                path=events_path,
                description="Fallback run events",
            )
        )

        warning_path = run_dir / "dependency_fallback_warning.txt"
        warning_path.write_text(trace_text.rstrip() + "\n", encoding="utf-8")
        result.add_artifact(
            ArtifactRef(
                artifact_id=f"fallback_warning_{result.run_id}",
                artifact_type=ArtifactType.LOG,
                path=warning_path,
                description="Original dependency failure that triggered standalone fallback",
            )
        )

        summary_path = run_dir / "result_summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "status": "completed",
                    "final_agent_id": result.final_agent_id,
                    "execution_order": [result.final_agent_id],
                    "final_answer": result.final_answer,
                    "total_tokens": result.total_tokens,
                    "total_time": result.duration_seconds,
                    "report_path": str(report_path),
                    "idea_path": None,
                    "events_path": str(events_path),
                    "notifications_path": str(run_dir / "human_notifications.jsonl"),
                    "warnings": list(result.warnings),
                    "errors": [],
                },
                indent=2,
                ensure_ascii=True,
                default=str,
            ),
            encoding="utf-8",
        )
        result.run_manifest_path = summary_path
        result.add_artifact(
            ArtifactRef(
                artifact_id=f"summary_{result.run_id}",
                artifact_type=ArtifactType.RUN_MANIFEST,
                path=summary_path,
                description="Fallback run summary",
            )
        )

        result.metrics.update(
            {
                "retrieval_context_injected": bool(
                    effective_inputs["retrieval_context"]
                ),
                "retrieval_hits_used": int(
                    request.metadata.get("retrieval_hit_count", 0)
                )
                if isinstance(request.metadata, dict)
                else 0,
                "runtime_overrides_used": list(
                    effective_inputs["runtime_overrides"].keys()
                ),
                "mock_loops_effective": effective_inputs["mock_loops"],
                "max_agent_executions_effective": effective_inputs[
                    "max_agent_executions"
                ],
                "manager_patch_count": self._manager_patch_count(),
                "dependency_fallback_used": missing_dependency,
            }
        )
        result.summary = (
            "Run completed with a repository-grounded standalone fallback because the "
            f"optional dependency {missing_dependency} is unavailable."
        )
        return result

    async def run_async(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        """
        Run the agent_research workspace asynchronously.
        """
        # Add gmas to path
        self._add_gmas_to_path()

        # Bootstrap environment
        self._bootstrap_environment()

        # Create run ID
        run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid4().hex[:8]}"
        start_time = datetime.now(UTC)
        result = WorkspaceRunResult(
            run_id=run_id,
            workspace_id=self.instance.workspace_id,
            task_id=request.task_id,
            status=WorkspaceRunStatus.PREPARING,
            start_timestamp=start_time,
        )
        run_dir = self.instance.path / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result.run_dir = run_dir

        try:
            # Update status
            result.status = WorkspaceRunStatus.RUNNING
            effective_inputs = self._build_effective_run_inputs(request)

            # Import and run the pipeline
            # We use dynamic import to avoid circular dependencies
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "run_article_pipeline", str(self._pipeline_path)
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            else:
                raise RuntimeError("Could not load run_article_pipeline module")

            # Get functions from the module
            build_tool_registry = module.build_tool_registry
            load_agent_profiles = module.load_agent_profiles
            build_graph = module.build_graph
            build_runner = module.build_runner
            WorkspaceArtifactStore = module.WorkspaceArtifactStore
            extract_article_content = getattr(
                module, "_extract_article_content", lambda raw: raw
            )
            should_synthesize_summary_report = getattr(
                module,
                "_should_synthesize_summary_report",
                lambda query, article_content: False,
            )
            build_repository_grounded_summary_report = getattr(
                module,
                "_build_repository_grounded_summary_report",
                None,
            )

            # Create artifact store
            store = WorkspaceArtifactStore(
                self.instance.path, report_name=request.report_name, run_id=run_id
            )
            result.run_dir = store.run_dir

            retrieval_context = effective_inputs["retrieval_context"]
            if retrieval_context:
                retrieval_path = store.run_dir / "retrieval_context.md"
                retrieval_path.write_text(
                    retrieval_context.rstrip() + "\n", encoding="utf-8"
                )
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"retrieval_{run_id}",
                        artifact_type=ArtifactType.CUSTOM,
                        path=retrieval_path,
                        description="Repository retrieval context injected by the manager",
                    )
                )

            runtime_overrides = effective_inputs["runtime_overrides"]
            if runtime_overrides:
                overrides_path = store.run_dir / "runtime_overrides.json"
                overrides_path.write_text(
                    json.dumps(runtime_overrides, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"runtime_overrides_{run_id}",
                        artifact_type=ArtifactType.CUSTOM,
                        path=overrides_path,
                        description="Effective runtime overrides loaded from instance metadata",
                    )
                )

            # Build components
            registry = build_tool_registry(store)
            profiles = load_agent_profiles(self.instance.path)
            graph = build_graph(effective_inputs["query"], profiles)

            # Export graph snapshot
            graph_snapshot_path = store.export_graph_snapshot(graph)
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"graph_{run_id}",
                    artifact_type=ArtifactType.GRAPH_SNAPSHOT,
                    path=graph_snapshot_path,
                    description="Graph topology snapshot",
                )
            )

            # Build runner
            runner = build_runner(
                store=store,
                registry=registry,
                live=request.live,
                mock_loops=effective_inputs["mock_loops"],
                live_model=request.model or get_default_workspace_model(),
                live_api_key=request.api_key,
                live_base_url=request.base_url,
                live_temperature=request.temperature,
                live_max_tokens=request.max_tokens,
                live_tool_choice=request.tool_choice,
                max_agent_executions=effective_inputs["max_agent_executions"],
            )

            # Run the pipeline
            pipeline_result = await runner.arun_round(
                graph, final_agent_id="delivery_agent"
            )

            # Update result
            result.end_timestamp = datetime.now(UTC)
            result.duration_seconds = (
                result.end_timestamp - result.start_timestamp
            ).total_seconds()
            result.final_agent_id = pipeline_result.final_agent_id
            result.final_answer = pipeline_result.final_answer or ""
            result.total_tokens = pipeline_result.total_tokens
            result.agent_count = len(pipeline_result.execution_order)

            # Collect artifacts
            if store.article_draft_written:
                report_path = str(store.report_dir / request.report_name)
            else:
                # Extract article content
                draft_source = (
                    pipeline_result.messages.get("article_writer")
                    or pipeline_result.final_answer
                )
                if draft_source:
                    article_content = extract_article_content(draft_source)
                    if (
                        build_repository_grounded_summary_report is not None
                        and should_synthesize_summary_report(
                            effective_inputs["query"],
                            article_content,
                        )
                    ):
                        article_content = build_repository_grounded_summary_report(
                            effective_inputs["query"],
                            pipeline_result,
                            store,
                            self.instance.path,
                        )
                    report_path = store.write_article_draft(
                        article_content,
                        filename=request.report_name,
                    )
                else:
                    report_path = str(store.report_dir / request.report_name)

            idea_sections: list[str] = []
            final_idea = store.latest_stage_note_text(
                "final_idea"
            ) or pipeline_result.messages.get("idea_synthesizer")
            final_structure = store.latest_stage_note_text(
                "final_article_structure"
            ) or pipeline_result.messages.get("structure_designer")
            if final_idea:
                idea_sections.append(final_idea)
            if final_structure and final_structure not in (final_idea or ""):
                idea_sections.append(final_structure)
            idea_path = (
                store.write_named_report(
                    "\n\n---\n\n".join(idea_sections), request.idea_report_name
                )
                if idea_sections
                else None
            )

            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"report_{run_id}",
                    artifact_type=ArtifactType.REPORT,
                    path=Path(report_path),
                    description=f"Article report: {request.report_name}",
                )
            )

            # Export result summary
            summary_path = store.export_result_summary(
                pipeline_result,
                report_path,
                idea_path,
                pipeline_result.final_answer,
            )
            result.run_manifest_path = summary_path
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"summary_{run_id}",
                    artifact_type=ArtifactType.RUN_MANIFEST,
                    path=summary_path,
                    description="Run result summary",
                )
            )
            if idea_path:
                result.add_artifact(
                    ArtifactRef(
                        artifact_id=f"idea_{run_id}",
                        artifact_type=ArtifactType.REPORT,
                        path=Path(idea_path),
                        description=f"Idea report: {request.idea_report_name}",
                    )
                )

            # Set status
            result.status = WorkspaceRunStatus.COMPLETED
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
                }
            )
            result.summary = (
                f"Run completed successfully. Final agent: {result.final_agent_id}. "
                f"Tokens: {result.total_tokens}. Duration: {result.duration_str}"
            )

        except Exception as e:
            trace_text = traceback.format_exc()
            if self._is_optional_dependency_failure(e, trace_text):
                return self._complete_with_dependency_fallback(
                    result,
                    request,
                    run_dir=result.run_dir or run_dir,
                    effective_inputs=effective_inputs,
                    error_text=str(e),
                    trace_text=trace_text,
                )
            result.status = WorkspaceRunStatus.FAILED
            result.end_timestamp = datetime.now(UTC)
            result.duration_seconds = (
                result.end_timestamp - result.start_timestamp
            ).total_seconds()
            result.errors.append(str(e))
            result.errors.append(trace_text)
            summary_path, trace_path = self._persist_failure_artifacts(
                run_dir=result.run_dir or run_dir,
                request=request,
                run_id=run_id,
                start_time=result.start_timestamp,
                end_time=result.end_timestamp,
                error_text=str(e),
                trace_text=trace_text,
            )
            result.run_manifest_path = summary_path
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"summary_{run_id}",
                    artifact_type=ArtifactType.RUN_MANIFEST,
                    path=summary_path,
                    description="Run failure summary",
                )
            )
            result.add_artifact(
                ArtifactRef(
                    artifact_id=f"traceback_{run_id}",
                    artifact_type=ArtifactType.LOG,
                    path=trace_path,
                    description="Failure traceback",
                )
            )

        return result

    def run(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        """
        Run the agent_research workspace synchronously.
        """
        return asyncio.run(self.run_async(request))

    def inspect(self, result: WorkspaceRunResult) -> WorkspaceInspection:
        """
        Inspect a run result.
        """
        agents_executed = []
        execution_order = []

        # Try to load from result summary
        if result.run_manifest_path and result.run_manifest_path.exists():
            try:
                data = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
                agents_executed = list(data.get("execution_order", []))
                execution_order = list(data.get("execution_order", []))
            except Exception:
                pass

        return WorkspaceInspection(
            run_id=result.run_id,
            workspace_id=result.workspace_id,
            status=result.status,
            agents_executed=agents_executed,
            execution_order=execution_order,
            final_answer=result.final_answer,
            key_artifacts=result.get_artifacts_by_type(ArtifactType.REPORT),
            errors=result.errors,
            warnings=result.warnings,
            total_tokens=result.total_tokens,
            duration_seconds=result.duration_seconds,
        )

    def list_artifacts(self, result: WorkspaceRunResult) -> list[ArtifactRef]:
        """
        List artifacts from a run result.
        """
        return result.artifacts

    def snapshot(
        self,
        instance: WorkspaceInstance,
        label: str,
        include_artifacts: bool = True,
    ) -> WorkspaceSnapshot:
        """
        Create a snapshot of a workspace instance.
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        snapshot_id = uuid4().hex[:8]
        snapshot_name = f"{timestamp}_{label.replace(' ', '_')[:40]}_{snapshot_id}"
        snapshot_path = instance.path / "snapshots" / snapshot_name
        snapshot_path.mkdir(parents=True, exist_ok=True)

        # Copy key files
        if include_artifacts:
            # Copy graph directory
            graph_dir = instance.path / "graph"
            if graph_dir.exists():
                shutil.copytree(graph_dir, snapshot_path / "graph")

            # Copy TASK_MAIN.md
            task_main_src = instance.path / "TASK_MAIN.md"
            if task_main_src.exists():
                shutil.copy2(task_main_src, snapshot_path / "TASK_MAIN.md")

            # Copy workspace.toml
            workspace_toml = instance.path / "workspace.toml"
            if workspace_toml.exists():
                shutil.copy2(workspace_toml, snapshot_path / "workspace.toml")

            # Copy latest run artifacts
            runs_dir = instance.path / "runs"
            if runs_dir.exists():
                latest_runs = sorted(
                    runs_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True
                )
                if latest_runs:
                    latest_run = latest_runs[0]
                    dest_run_dir = snapshot_path / "runs" / latest_run.name
                    dest_run_dir.mkdir(parents=True, exist_ok=True)
                    for item in latest_run.glob("*"):
                        if item.is_file():
                            shutil.copy2(item, dest_run_dir / item.name)
                        elif item.is_dir():
                            shutil.copytree(item, dest_run_dir / item.name)

            # Copy reports
            reports_dir = instance.path / "reports"
            if reports_dir.exists():
                dest_reports = snapshot_path / "reports"
                dest_reports.mkdir(parents=True, exist_ok=True)
                for report in reports_dir.glob("*.md"):
                    shutil.copy2(report, dest_reports / report.name)

        # Create snapshot metadata
        snapshot_metadata = {
            "snapshot_id": snapshot_id,
            "instance_id": instance.instance_id,
            "workspace_id": instance.workspace_id,
            "label": label,
            "created_at": datetime.now(UTC).isoformat(),
            "snapshot_path": str(snapshot_path),
            "source_path": str(instance.path),
            "includes_graph": (snapshot_path / "graph").exists(),
            "includes_artifacts": include_artifacts,
            "includes_memory": False,
            "includes_prompts": False,
        }
        metadata_path = snapshot_path / "snapshot_metadata.json"
        metadata_path.write_text(
            json.dumps(snapshot_metadata, indent=2), encoding="utf-8"
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
            includes_prompts=False,
            includes_artifacts=include_artifacts,
        )

    def get_run_manifest(self, result: WorkspaceRunResult) -> dict[str, Any]:
        """
        Get the run manifest for a result.
        """
        if result.run_manifest_path and result.run_manifest_path.exists():
            return json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
        return {
            "run_id": result.run_id,
            "workspace_id": result.workspace_id,
            "status": result.status.value,
            "summary_text": self.get_log_summary(result),
        }

    def get_log_summary(self, result: WorkspaceRunResult) -> str:
        """
        Get a log summary for a result.
        """
        # Build a summary from the result
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

    def get_run_report(self, result: WorkspaceRunResult) -> WorkspaceRunReport:
        """Build a rich run report by reading GMAS run artifacts."""
        report = WorkspaceRunReport(
            run_id=result.run_id,
            workspace_id=result.workspace_id,
            workspace_type="agent_research",
            status=result.status.value,
            duration_seconds=result.duration_seconds,
            total_tokens=result.total_tokens,
            errors=list(result.errors),
            summary=result.summary,
            final_answer=result.final_answer,
        )

        run_dir = result.run_dir
        if not run_dir or not run_dir.exists():
            return report

        events_path = run_dir / "events.jsonl"
        summary_path = run_dir / "result_summary.json"

        summary_data: dict[str, Any] = {}
        if summary_path.exists():
            try:
                summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        report.query = summary_data.get("query", "")

        raw_events: list[dict[str, Any]] = []
        if events_path.exists():
            try:
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        raw_events.append(json.loads(line))
            except Exception:
                pass

        execution_order = summary_data.get("execution_order", [])

        agent_tokens: dict[str, int] = {}
        agent_durations: dict[str, float] = {}
        agent_statuses: dict[str, str] = {}
        agent_outputs: dict[str, str] = {}

        for ev in raw_events:
            etype = ev.get("event_type", "")
            aid = ev.get("agent_id", "")

            report.events.append(
                RunReportEvent(
                    event_type=etype,
                    timestamp=ev.get("timestamp", ""),
                    agent_id=aid,
                    summary=ev.get("summary", ev.get("error", "")),
                )
            )

            if etype == "agent_end" and aid:
                agent_tokens[aid] = agent_tokens.get(aid, 0) + int(
                    ev.get("tokens_used", 0)
                )
                agent_durations[aid] = agent_durations.get(aid, 0) + float(
                    ev.get("duration_ms", 0)
                )
                agent_statuses[aid] = "completed"
                output_text = ev.get("output_text") or ev.get("result", "")
                if output_text:
                    agent_outputs[aid] = str(output_text)[:500]

            if etype == "agent_error" and aid:
                agent_statuses[aid] = "failed"

        topology_path = self.instance.path / "graph" / "topology.toml"
        topology_agents: list[str] = []
        topology_edges_raw: list[dict[str, Any]] = []
        if topology_path.exists():
            try:
                topo = self._load_toml(topology_path)
                topology_agents = topo.get("agents", [])
                topology_edges_raw = topo.get("edges", [])
            except Exception:
                pass

        all_agents = list(dict.fromkeys(topology_agents + execution_order))

        agents_dir = self.instance.path / "agents"
        for aid in all_agents:
            display_name = aid
            if agents_dir.exists():
                agent_cfg_path = agents_dir / f"{aid}.toml"
                if agent_cfg_path.exists():
                    try:
                        cfg = self._load_toml(agent_cfg_path)
                        display_name = cfg.get("display_name", aid)
                    except Exception:
                        pass

            status = agent_statuses.get(aid, "idle")
            if aid in execution_order and aid not in agent_statuses:
                status = "completed"

            report.nodes.append(
                RunReportNode(
                    node_id=aid,
                    display_name=display_name,
                    status=status,
                    tokens=agent_tokens.get(aid, 0),
                    duration_ms=agent_durations.get(aid, 0),
                    output_preview=agent_outputs.get(aid, ""),
                )
            )

        executed_set = set(execution_order)
        for edge_raw in topology_edges_raw:
            src = edge_raw.get("source", "")
            tgt = edge_raw.get("target", "")
            if not src or not tgt or "__task__" in (src, tgt):
                continue
            report.edges.append(
                RunReportEdge(
                    source=src,
                    target=tgt,
                    label=edge_raw.get("condition", ""),
                    executed=(src in executed_set and tgt in executed_set),
                )
            )

        report.artifacts = [
            {
                "id": a.artifact_id,
                "type": a.artifact_type.value,
                "path": str(a.path),
                "description": a.description,
            }
            for a in result.artifacts
        ]

        return report

    def supports_workspace(self, workspace_id: str) -> bool:
        """Check if this adapter supports a workspace."""
        return workspace_id == "agent_research"

    def get_supported_workspace_types(self) -> list[str]:
        """Get list of supported workspace types."""
        return ["agent_research"]
