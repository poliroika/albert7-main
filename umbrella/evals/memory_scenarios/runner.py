"""Memory scenario runner — offline phase/tool memory audit."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from umbrella.evals.memory_scenarios.assertions import (
    assert_memory_injection_contract,
    assert_single_always_loaded_block,
    evaluate_assert_block,
    structured_facts,
)
from umbrella.evals.memory_scenarios.fake_hindsight import (
    FakeHindsightCallLog,
    FakeHindsightClient,
    default_reflect_candidate,
)
from umbrella.evals.memory_scenarios.fake_ouroboros import run_ouroboros_dedup_check
from umbrella.evals.memory_scenarios.fixtures import (
    EXPECTED_ROOT,
    REPO_ROOT,
    drive_root,
    prepare_scenario_repo,
)
from umbrella.evals.memory_scenarios.models import (
    MemoryScenario,
    MemoryScenarioResult,
    ScenarioStep,
    ScenarioStepResult,
)
from umbrella.evals.memory_scenarios.reports import (
    build_dashboard,
    print_terminal_summary,
    write_scenario_artifacts,
)
from umbrella.evals.memory_scenarios.scenario_loader import (
    load_all_scenarios,
    load_scenario,
    load_scenario_by_id,
)
from umbrella.evals.memory_scenarios.seeding import seed_from_dict
from umbrella.evals.memory_scenarios.snapshots import (
    capture_snapshot,
    write_step_snapshots,
)
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler
from umbrella.orchestrator.worker import build_phase_task, render_phase_user_prompt
from umbrella.phases.base import PhaseNode
from umbrella.phases.loader import load_manifest
from umbrella.evals.memory_scenarios.fixtures import manifest_path


class MemoryScenarioRunner:
    def __init__(
        self,
        *,
        report_root: Path | None = None,
        keep_tmp: bool = False,
        update_golden: bool = False,
        verbose: bool = False,
    ) -> None:
        self.report_root = report_root or (REPO_ROOT / ".mrt" / "memory_scenarios")
        self.keep_tmp = keep_tmp
        self.update_golden = update_golden
        self.verbose = verbose
        self._fake_hindsight: FakeHindsightClient | None = None
        self._hindsight_log: FakeHindsightCallLog | None = None

    def run(self, scenario: MemoryScenario) -> MemoryScenarioResult:
        tmp = tempfile.mkdtemp(prefix="mem-scenario-")
        repo = Path(tmp)
        failures: list[str] = []
        step_results: list[ScenarioStepResult] = []
        ws = scenario.workspace

        try:
            prepare_scenario_repo(
                repo,
                workspace_id=ws,
                workspace_fixture=scenario.seed.workspace_fixture,
                manager_fixture=scenario.seed.manager_fixture,
                extra_workspaces=scenario.seed.extra_workspaces,
            )
            raw_seed = scenario.raw_seed
            seed_from_dict(repo, ws, raw_seed)

            for key, val in scenario.env.items():
                os.environ[key] = val
            os.environ.setdefault("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")

            report_dir = self.report_root / scenario.id
            if report_dir.exists():
                shutil.rmtree(report_dir)
            report_dir.mkdir(parents=True, exist_ok=True)

            context: dict[str, Any] = {
                "repo": repo,
                "workspace_id": ws,
                "drive": drive_root(repo, ws),
                "tasks": {},
                "fake_hindsight": self._ensure_fake_hindsight(report_dir),
            }

            steps = list(scenario.steps)
            if scenario.mode == "phase_prompt_matrix" and not steps:
                phases = scenario.assertions.get("matrix_phases") or [
                    "preflight",
                    "research",
                    "plan",
                    "execute",
                    "verify",
                    "reflexion",
                ]
                run_id = str(scenario.assertions.get("run_id") or "run-scenario-matrix")
                for phase in phases:
                    steps.append(
                        ScenarioStep(
                            id=f"{phase}_prompt",
                            action="build_phase_task",
                            phase=phase,
                            run_id=run_id,
                        )
                    )

            for step in steps:
                before = capture_snapshot(repo, step.workspace_id or ws, context["drive"])
                result = self._run_step(scenario, step, context)
                after = capture_snapshot(
                    repo, step.workspace_id or ws, context["drive"]
                )
                result.palace_before = before.get("palace", {})
                result.palace_after = after.get("palace", {})
                write_step_snapshots(report_dir, step.id, before, after)
                self._write_step_artifacts(report_dir, step.id, result)
                step_results.append(result)

                assert_spec = scenario.assertions.get(step.id) or {}
                if not assert_spec and scenario.mode == "phase_prompt_matrix":
                    assert_spec = scenario.assertions.get("_default") or {}
                if assert_spec:
                    errs = evaluate_assert_block(
                        step.id, assert_spec, result, task=result.task
                    )
                    result.errors.extend(errs)
                    result.ok = result.ok and not errs

                if not result.ok:
                    failures.extend(result.errors)

            for key, spec in scenario.assertions.items():
                if key.endswith("_prompt") or key in context.get("tasks", {}):
                    continue
                if isinstance(spec, dict) and key not in {s.id for s in scenario.steps}:
                    task = context["tasks"].get(key)
                    if task:
                        sr = ScenarioStepResult(step_id=key, action="assert", ok=True, task=task)
                        errs = evaluate_assert_block(key, spec, sr, task=task)
                        if errs:
                            failures.extend(errs)
                            sr.ok = False
                            step_results.append(sr)

            if self.update_golden:
                self._update_golden(scenario.id, step_results)

            if not self.update_golden:
                failures.extend(self._check_golden(scenario.id, step_results))

            ok = not failures
            mem_result = MemoryScenarioResult(
                scenario_id=scenario.id,
                ok=ok,
                repo_root=repo,
                workspace_id=ws,
                step_results=step_results,
                invariant_failures=failures,
                report_dir=report_dir,
            )
            write_scenario_artifacts(report_dir, scenario, mem_result)
            return mem_result
        finally:
            if not self.keep_tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def _ensure_fake_hindsight(self, report_dir: Path) -> FakeHindsightClient:
        if self._fake_hindsight is None:
            self._fake_hindsight = FakeHindsightClient()
            self._hindsight_log = FakeHindsightCallLog(report_dir / "hindsight_fake_calls.jsonl")
        return self._fake_hindsight

    def _run_step(
        self,
        scenario: MemoryScenario,
        step: ScenarioStep,
        context: dict[str, Any],
    ) -> ScenarioStepResult:
        repo: Path = context["repo"]
        ws = step.workspace_id or context["workspace_id"]
        drive = context["drive"]
        if step.workspace_id and step.workspace_id != context["workspace_id"]:
            drive = drive_root(repo, ws)
        errors: list[str] = []
        result = ScenarioStepResult(step_id=step.id, action=step.action, ok=True)

        try:
            if step.action == "build_phase_task":
                task = self._build_task(
                    repo, ws, step.phase or "research", step.run_id, drive
                )
                result.task = task
                result.prompt = str(task.get("input") or "")
                result.overlays = task.get("context_overlays") or {}
                context["tasks"][step.id] = task
                _, cerrs = assert_memory_injection_contract(task)
                errors.extend(cerrs)
                bundle_path = drive / "state" / "llm_input_bundle_latest.json"
                report_path = drive / "state" / "memory_injection_report_latest.json"
                if bundle_path.is_file():
                    result.bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                if report_path.is_file():
                    result.injection_report = json.loads(
                        report_path.read_text(encoding="utf-8")
                    )

            elif step.action == "render_phase_prompt":
                manifest = load_manifest(manifest_path(step.phase or "research"))
                overlay = ProactiveMemoryCompiler().build_overlay(
                    repo_root=repo,
                    workspace_id=ws,
                    run_id=step.run_id,
                    phase_id=step.phase or "research",
                    subtask_id=None,
                    task_brief="scenario",
                    manifest=manifest,
                )
                from umbrella.memory.palace.recall import RecallBundle

                result.prompt = render_phase_user_prompt(
                    manifest, RecallBundle(), proactive_overlay=overlay
                )

            elif step.action == "palace_add":
                errors.extend(self._action_palace_add(repo, ws, drive, step))

            elif step.action == "save_umbrella_memory":
                errors.extend(self._action_save_memory(repo, ws, step))

            elif step.action == "promote_to_durable":
                errors.extend(self._action_promote(repo, ws, drive, step))

            elif step.action == "fake_hindsight_reflect":
                errors.extend(self._action_fake_reflect(repo, ws, drive, step, context))

            elif step.action == "init_ouroboros_memory":
                prior_id = step.args.get("task_step_id")
                task = context["tasks"].get(str(prior_id)) if prior_id else None
                if not task:
                    phase = step.phase or "verify"
                    task = self._build_task(repo, ws, phase, step.run_id, drive)
                result.task = task
                context["tasks"][step.id] = task
                dedup_errs = run_ouroboros_dedup_check(repo, ws, task)
                errors.extend(dedup_errs)

            elif step.action == "memory_health":
                palace = MemPalace(repo, ws)
                try:
                    health = palace.health()
                    if health.get("unavailable") and not health.get("error"):
                        errors.append("backend unavailable masked as empty")
                finally:
                    palace.close()

            else:
                errors.append(f"unsupported action: {step.action}")
        except Exception as exc:
            errors.append(f"{step.action}: {exc}")

        result.errors = errors
        result.ok = not errors
        return result

    def _build_task(
        self,
        repo: Path,
        workspace_id: str,
        phase_id: str,
        run_id: str,
        drive: Path,
    ) -> dict[str, Any]:
        manifest = load_manifest(manifest_path(phase_id))
        phase_node = PhaseNode(id=phase_id, manifest_id=phase_id, status="running")
        palace = MemPalace(repo, workspace_id)
        try:
            return build_phase_task(
                phase_node=phase_node,
                manifest=manifest,
                workspace_id=workspace_id,
                run_id=run_id,
                palace=palace,
                repo_root=repo,
                drive_root=drive,
            )
        finally:
            palace.close()

    def _action_palace_add(
        self, repo: Path, ws: str, drive: Path, step: ScenarioStep
    ) -> list[str]:
        from ouroboros.tools.phase_contract import _palace_add
        from ouroboros.tools.registry import ToolContext

        args = step.args
        ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
        ctx.task_id = f"{step.run_id}:{step.phase or 'plan'}"
        ctx.loop_state_view = {
            "phase_label": step.phase or "plan",
            "active_workspace_id": ws,
        }
        payload = json.loads(
            _palace_add(
                ctx,
                title=str(args.get("title") or "Harness title"),
                content=str(args.get("content") or "Harness body"),
                kind=str(args.get("kind") or "observation"),
                workspace_id=str(args.get("workspace_id") or ws),
                palace_path=args.get("palace_path"),
                tags=str(args.get("tags") or "observation"),
            )
        )
        if not payload.get("saved"):
            return [f"palace_add not saved: {payload}"]
        nested = repo / "workspaces" / ws / "workspaces"
        if nested.exists():
            return [f"nested workspaces path exists: {nested}"]
        return []

    def _action_save_memory(
        self, repo: Path, ws: str, step: ScenarioStep
    ) -> list[str]:
        from umbrella.deep_agent_tools.memory import save_umbrella_memory

        args = step.args
        ctx = MagicMock()
        ctx.host_repo_root = str(repo)
        ctx.repo_dir = str(repo)
        ctx.task_id = f"{step.run_id}:plan"
        first = json.loads(
            save_umbrella_memory(
                ctx,
                palace_path=str(args.get("palace_path") or f"workspaces/{ws}/plan"),
                title=str(args.get("title") or "Dup title"),
                content=str(args.get("content") or "Dup body"),
                kind="observation",
                workspace_id=ws,
                tags="observation",
            )
        )
        cid = first.get("canonical_id")
        if args.get("duplicate"):
            second = json.loads(
                save_umbrella_memory(
                    ctx,
                    palace_path=str(args.get("palace_path") or f"workspaces/{ws}/plan"),
                    title=str(args.get("title") or "Dup title"),
                    content=str(args.get("content") or "Dup body"),
                    kind="observation",
                    workspace_id=ws,
                    tags="observation",
                    metadata_extra={"canonical_id": cid},
                )
            )
            store = str(first.get("store") or "palace.idea")
            palace = MemPalace(repo, ws)
            try:
                matches = [
                    h
                    for h in palace.list_all(n=200, stores=[store])
                    if h.get("id") == cid
                ]
            finally:
                palace.close()
            if len(matches) != 1:
                return [f"expected one canonical node, got {len(matches)}"]
        return []

    def _action_promote(
        self, repo: Path, ws: str, drive: Path, step: ScenarioStep
    ) -> list[str]:
        from umbrella.deep_agent_tools.phase_contract_handlers import _promote_to_durable
        from umbrella.enforcement.ledger import append_supervisor_ledger_event

        args = step.args
        evidence = list(args.get("evidence_refs") or [])
        if args.get("append_ledger"):
            event = append_supervisor_ledger_event(
                repo_root=repo,
                workspace_id=ws,
                actor="verifier",
                phase="verify",
                tool="pytest_harness",
                result={"passed": True},
            )
            evidence = [
                {
                    "ref_type": "ledger_event",
                    "ref_id": event.event_id,
                    "hash": event.event_hash,
                    "produced_by": "verifier",
                }
            ]
        ctx = MagicMock()
        ctx.host_repo_root = str(repo)
        ctx.repo_dir = str(repo)
        ctx.drive_root = str(drive)
        ctx.loop_state_view = {"phase_label": "verify"}
        ctx.task_id = f"{step.run_id}:verify"
        ctx.umbrella_phase_id = "verify"
        payload = json.loads(
            _promote_to_durable(
                ctx,
                title=str(args.get("title") or "Harness verification"),
                content=str(args.get("content") or "Verified content."),
                workspace_id=ws,
                tags=str(args.get("tags") or "verification_report,durable"),
                evidence_refs=evidence,
                trust_level=str(args.get("trust_level") or "public_verified"),
            )
        )
        if args.get("expect_saved") is False:
            if payload.get("saved"):
                return ["promote_to_durable should have been blocked"]
            return []
        if not payload.get("saved"):
            return [f"promote_to_durable failed: {payload}"]
        return []

    def _action_fake_reflect(
        self,
        repo: Path,
        ws: str,
        drive: Path,
        step: ScenarioStep,
        context: dict[str, Any],
    ) -> list[str]:
        from umbrella.enforcement.ledger import append_supervisor_ledger_event
        from umbrella.memory.hindsight.candidates import (
            write_hindsight_candidates_as_pending_proposals,
        )

        event = append_supervisor_ledger_event(
            repo_root=repo,
            workspace_id=ws,
            actor="verifier",
            phase="verify",
            tool="harness",
            result={"passed": True},
        )
        evidence = [
            {
                "ref_type": "ledger_event",
                "ref_id": event.event_id,
                "hash": event.event_hash,
                "produced_by": "verifier",
            }
        ]
        fake: FakeHindsightClient = context["fake_hindsight"]
        candidate = default_reflect_candidate(evidence)
        fake.reflect_payload = {
            "candidates": [
                {
                    "id": candidate.candidate_id,
                    "title": candidate.title,
                    "content": candidate.content,
                }
            ]
        }
        log = self._hindsight_log
        if log:
            log.append("reflect_candidates", {"workspace_id": ws})
        result = write_hindsight_candidates_as_pending_proposals(
            drive_root=drive,
            repo_root=repo,
            workspace_id=ws,
            run_id=step.run_id,
            phase_id=step.phase or "reflexion",
            candidates=[candidate],
        )
        if result.get("queued", 0) < 1:
            return ["hindsight candidate not queued"]
        return []

    def _write_step_artifacts(
        self, report_dir: Path, step_id: str, result: ScenarioStepResult
    ) -> None:
        if result.prompt:
            (report_dir / f"prompt_{step_id}.txt").write_text(
                result.prompt, encoding="utf-8"
            )
        if result.injection_report:
            (report_dir / f"memory_injection_report_{step_id}.json").write_text(
                json.dumps(result.injection_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        proactive = (result.overlays or {}).get("proactive_memory")
        if proactive:
            (report_dir / f"proactive_overlay_{step_id}.json").write_text(
                json.dumps(proactive, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if result.bundle:
            (report_dir / f"llm_input_bundle_{step_id}.json").write_text(
                json.dumps(result.bundle, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _update_golden(self, scenario_id: str, steps: list[ScenarioStepResult]) -> None:
        dest = EXPECTED_ROOT / scenario_id
        dest.mkdir(parents=True, exist_ok=True)
        for step in steps:
            if step.task:
                facts = structured_facts(step, step.task)
                name = f"{step.step_id}.report.golden.json"
                (dest / name).write_text(
                    json.dumps(facts, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

    def _check_golden(self, scenario_id: str, steps: list[ScenarioStepResult]) -> list[str]:
        errors: list[str] = []
        expected_dir = EXPECTED_ROOT / scenario_id
        if not expected_dir.is_dir():
            return errors
        for step in steps:
            if not step.task:
                continue
            golden_path = expected_dir / f"{step.step_id}.report.golden.json"
            if not golden_path.is_file():
                continue
            expected = json.loads(golden_path.read_text(encoding="utf-8"))
            actual = structured_facts(step, step.task)
            for key in ("included_bkb_ids", "skipped_bkb_ids"):
                if key in expected and expected[key] != actual.get(key):
                    errors.append(
                        f"golden {step.step_id}.{key}: "
                        f"expected {expected[key]}, got {actual.get(key)}"
                    )
        return errors


def run_scenario_by_id(
    scenario_id: str,
    *,
    report_root: Path | None = None,
    **kwargs: Any,
) -> MemoryScenarioResult:
    scenario = load_scenario_by_id(scenario_id)
    return MemoryScenarioRunner(report_root=report_root, **kwargs).run(scenario)


def run_all_scenarios(
    *,
    report_root: Path | None = None,
    fail_fast: bool = False,
    **kwargs: Any,
) -> MemoryScenarioResult:
    results: list[MemoryScenarioResult] = []
    runner = MemoryScenarioRunner(report_root=report_root, **kwargs)
    for scenario in load_all_scenarios():
        result = runner.run(scenario)
        results.append(result)
        if fail_fast and not result.ok:
            break
    root = report_root or (REPO_ROOT / ".mrt" / "memory_scenarios")
    dashboard = build_dashboard(results, root)
    (root / "latest" / "dashboard.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "latest" / "dashboard.json").write_text(
        json.dumps(dashboard, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print_terminal_summary(results)
    ok = all(r.ok for r in results)
    return MemoryScenarioResult(
        scenario_id="__aggregate__",
        ok=ok,
        repo_root=REPO_ROOT,
        workspace_id="",
        step_results=[],
        invariant_failures=[m for r in results for m in r.invariant_failures],
        report_dir=root / "latest",
        dashboard=dashboard,
    )
