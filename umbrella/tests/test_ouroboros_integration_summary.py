from umbrella.control_plane.ouroboros_integration import (
    _apply_delivery_contract_gate,
    _collect_run_quality_telemetry,
    _has_actionable_remediation_context,
    _is_hygiene_only_failure,
    _log_phase_boundary_event,
    _max_hygiene_remediations,
    _persist_verification_failure_context,
    _persist_canonical_task_result,
    _persist_final_gate_report,
    _resolve_final_status,
    _truncate_final_message,
)

import json

from umbrella.control_plane.remediation_planner import (
    synthesise_verification_remediation_plan,
)


def test_truncate_final_message_marks_truncation():
    text = _truncate_final_message("x" * 4100)

    assert text.endswith("[truncated]")
    assert len(text) < 4050


def test_umbrella_synthesises_verification_remediation_plan(tmp_path):
    plan_id = synthesise_verification_remediation_plan(
        drive_root=tmp_path,
        task_id="sync-1",
        workspace_id="demo",
        remediation_attempt=2,
    )

    assert plan_id == "sync-1"
    payload = json.loads((tmp_path / "task_plans" / "sync-1.json").read_text())
    assert payload["workspace_id"] == "demo"
    assert "External verification remediation cycle #2" in payload["objective_digest"]
    assert (
        payload["subtasks"][0]["title"]
        == "Fix failing verification checks (remediation #2)"
    )
    assert "run_workspace_verify" in payload["subtasks"][0]["success_check"]


def test_workspace_toml_can_override_gmas_policy(tmp_path):
    """workspace.toml can force GMAS on or explicitly opt out; when the
    policy is absent, normal task-domain detection decides."""
    from umbrella.integration.ouroboros_bridge import _workspace_gmas_policy

    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    assert _workspace_gmas_policy(tmp_path, "demo") is None

    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    assert _workspace_gmas_policy(tmp_path, "demo") is True

    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = false\n",
        encoding="utf-8",
    )
    assert _workspace_gmas_policy(tmp_path, "demo") is False


def test_collect_run_quality_telemetry_records_domains_model_and_repairs(tmp_path):
    workspace = tmp_path / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (drive / "memory" / "knowledge").mkdir(parents=True)
    (drive / "state" / "active_skills.json").write_text(
        json.dumps(
            {"entry": {"workspace_id": "demo", "domains": ["multi_agent_gmas"]}}
        ),
        encoding="utf-8",
    )
    (drive / "memory" / "knowledge" / "gmas_active_context.md").write_text(
        "context", encoding="utf-8"
    )
    (drive / "logs" / "events.jsonl").write_text(
        json.dumps({"type": "llm_round", "model": "gemma-4", "phase": "planner"})
        + "\n"
        + json.dumps({"type": "tool_args_repair", "repaired": False})
        + "\n",
        encoding="utf-8",
    )

    telemetry = _collect_run_quality_telemetry(
        repo_root=tmp_path,
        workspace_id="demo",
        critic_payload={"verdict": "fail"},
    )

    assert telemetry["active_domains"] == ["multi_agent_gmas"]
    assert telemetry["gmas_context_present"] is True
    assert telemetry["models"] == ["gemma-4"]
    assert telemetry["phases"] == ["planner"]
    assert telemetry["tool_arg_unrepairable"] == 1
    assert telemetry["degraded_tool_call_quality"] is True
    assert telemetry["missing_external_discovery_warning"] is False


def test_collect_run_quality_telemetry_warns_when_substantial_run_skips_external_discovery(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "events.jsonl").write_text(
        "\n".join(
            json.dumps({"type": "llm_round", "model": "glm", "phase": "planner"})
            for _ in range(21)
        )
        + "\n",
        encoding="utf-8",
    )

    telemetry = _collect_run_quality_telemetry(
        repo_root=tmp_path,
        workspace_id="demo",
    )

    assert telemetry["external_discovery_total"] == 0
    assert telemetry["missing_external_discovery_warning"] is True


def test_resolve_final_status_failed_required_subtask_downgrades_run():
    """A passing verification cannot mask a planner contract violation.

    When the planner left a required subtask in ``failed`` state the run
    must terminate with a delivery-contract failure (``incomplete_subtasks``)
    so the web UI surfaces it as ``failed`` and promotion is blocked,
    regardless of the runtime verification result.
    """

    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": False},
        critic_payload={"verdict": "fail"},
        failed_required_subtask=True,
        no_writes=False,
    )
    assert status == "incomplete_subtasks"
    assert "failed_required_subtask" in warnings


def test_resolve_final_status_skipped_verification_is_failed_with_warning():
    """A workspace without a real verification step is a repairable gate
    failure, so the remediation loop should add a smoke check instead of
    letting the run finish green without proof.
    """
    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": True},
        critic_payload={"verdict": "pass"},
        failed_required_subtask=False,
        no_writes=False,
    )
    assert status == "failed_verification"
    assert warnings == ["verification_skipped_no_spec"]


def test_resolve_final_status_skipped_verification_with_no_writes_is_incomplete():
    """If verification was skipped *and* the agent did not write
    anything, the run is honestly ``incomplete`` (no deliverable, no
    proof) rather than silently green."""
    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": True},
        critic_payload={"verdict": "pass"},
        failed_required_subtask=False,
        no_writes=True,
    )
    assert status == "incomplete"
    assert warnings == ["verification_skipped_no_spec"]


def test_resolve_final_status_demotes_to_hygiene_when_sweep_missing_required():
    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": False},
        failed_required_subtask=False,
        no_writes=False,
        sweep_payload={
            "missing_required": ["README.md"],
            "leftover_noise": [],
            "removed": [],
        },
    )
    assert status == "failed_hygiene"
    assert "sweep_missing_required" in warnings


def test_resolve_final_status_runtime_optional_missing_is_warning_only():
    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": False},
        failed_required_subtask=False,
        no_writes=False,
        sweep_payload={
            "missing_required": ["highscore.txt"],
            "leftover_noise": [],
            "removed": [],
        },
    )
    assert status == "verified"
    assert "sweep_missing_runtime_optional" in warnings
    assert "sweep_missing_required" not in warnings


def test_resolve_final_status_records_sweep_warnings_but_stays_verified():
    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": False},
        failed_required_subtask=False,
        no_writes=False,
        sweep_payload={
            "missing_required": [],
            "leftover_noise": ["debug_x.py"],
            "removed": ["debug_y.py"],
        },
    )
    assert status == "verified"
    assert "sweep_leftover_noise" in warnings
    assert "sweep_auto_cleaned" in warnings


def test_empty_passed_verification_has_no_actionable_remediation_context():
    assert (
        _has_actionable_remediation_context(
            final_status="failed_verification",
            verification_payload={"passed": True, "skipped": False, "results": []},
            sweep_payload={},
        )
        is False
    )


def test_hygiene_failure_has_actionable_remediation_context_from_sweep():
    assert (
        _has_actionable_remediation_context(
            final_status="failed_hygiene",
            verification_payload={"passed": True, "skipped": False, "results": []},
            sweep_payload={
                "blocking_noise": [
                    {
                        "path": "result.txt",
                        "severity": "block",
                        "category": "noise.artifacts",
                    }
                ]
            },
        )
        is True
    )


def test_hygiene_only_failure_classifier_requires_clean_verification():
    assert (
        _is_hygiene_only_failure(
            final_status="failed_hygiene",
            completion_warnings=["sweep_blocking_noise"],
            verification_payload={"passed": True, "skipped": False, "results": []},
            sweep_payload={
                "blocking_noise": [
                    {
                        "path": "debug_probe.py",
                        "severity": "block",
                        "category": "noise.artifacts",
                    }
                ]
            },
        )
        is True
    )
    assert (
        _is_hygiene_only_failure(
            final_status="failed_hygiene",
            completion_warnings=["sweep_blocking_noise"],
            verification_payload={
                "passed": False,
                "results": [{"name": "tests", "kind": "shell", "status": "failed"}],
            },
            sweep_payload={
                "blocking_noise": [
                    {
                        "path": "debug_probe.py",
                        "severity": "block",
                        "category": "noise.artifacts",
                    }
                ]
            },
        )
        is False
    )


def test_max_hygiene_remediations_reads_env_without_crashing(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MAX_HYGIENE_REMEDIATIONS", "2")

    assert _max_hygiene_remediations() == 2


def test_summarise_solution_idea_prefers_archived_original_plan(tmp_path):
    from umbrella.control_plane.ouroboros_integration import _summarise_solution_idea

    plans_dir = tmp_path / "workspaces" / "demo" / ".memory" / "drive" / "task_plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "task1.json").write_text(
        json.dumps(
            {
                "task_id": "task1",
                "objective_digest": "verification remediation",
                "subtasks": [
                    {"status": "done", "title": "Fix failing verification"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (plans_dir / "task1.before_remediation_1.123.json").write_text(
        json.dumps(
            {
                "task_id": "task1",
                "objective_digest": "build news pipeline",
                "subtasks": [
                    {"status": "done", "title": "Parse requirements"},
                    {"status": "done", "title": "Implement CLI"},
                    {"status": "done", "title": "Generate PPTX"},
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = _summarise_solution_idea(tmp_path, "demo", "task1", ["src/demo.py"])

    assert "Шаги исходного плана" in summary
    assert "Generate PPTX" in summary
    assert "Fix failing verification" not in summary


def test_resolve_final_status_block_level_noise_fails_verification():
    # Tier 4.1: block-level noise (ad-hoc root scripts, extraction
    # artifacts, handoff docs) must trip ``failed_verification`` so the
    # remediation loop fires instead of the run silently passing with
    # junk in the deliverable.
    status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": False},
        failed_required_subtask=False,
        no_writes=False,
        sweep_payload={
            "missing_required": [],
            "leftover_noise": [],
            "removed": ["extract_docx.py"],
            "blocking_noise": [
                {
                    "path": "extract_docx.py",
                    "severity": "block",
                    "category": "noise.scripts",
                },
            ],
        },
    )
    assert status == "failed_hygiene"
    assert "sweep_blocking_noise" in warnings


def test_persist_final_gate_report_writes_markdown(tmp_path):
    path = _persist_final_gate_report(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task123",
        final_status="verified",
        verification_payload={"passed": True, "summary": "Verification: PASS"},
        critic_payload={"verdict": "pass"},
        completion_warnings=[],
    )
    text = (
        tmp_path
        / "workspaces"
        / "demo"
        / ".memory"
        / "drive"
        / "task_results"
        / "task123.verification.md"
    ).read_text(encoding="utf-8")
    assert path.endswith("task123.verification.md")
    assert "Verification Report" in text
    assert "final_status: `verified`" in text


def test_persist_canonical_task_result_overwrites_raw_agent_text(tmp_path):
    task_id = "sync_improve_web_abc"
    task_results = (
        tmp_path / "workspaces" / "demo" / ".memory" / "drive" / "task_results"
    )
    task_results.mkdir(parents=True)
    raw_path = task_results / f"{task_id}.json"
    raw_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "status": "completed",
                "result": "<tool_call>read_workspace_file...</tool_call>",
                "model": "glm-test",
            }
        ),
        encoding="utf-8",
    )

    path = _persist_canonical_task_result(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id=task_id,
        result={
            "status": "verified",
            "final_message": "## Проверено\n\nВсе проверки прошли.",
            "verification_passed": True,
            "verification_report": {
                "passed": True,
                "pass_rate": 1.0,
                "skipped": False,
                "summary": "Verification: PASS",
            },
        },
    )

    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert path.endswith(f"{task_id}.json")
    assert payload["status"] == "verified"
    assert payload["result"].startswith("## Проверено")
    assert payload["agent_raw_result"].startswith("<tool_call>")
    assert payload["model"] == "glm-test"
    assert payload["verification"]["passed"] is True


def test_persist_verification_failure_context_writes_jsonl_state_and_markdown(tmp_path):
    block = _persist_verification_failure_context(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task123",
        remediation_attempt=1,
        max_attempts=3,
        verification_payload={
            "passed": False,
            "pass_rate": 0.5,
            "summary": "Verification: FAIL",
            "results": [
                {
                    "name": "pytest:tests",
                    "kind": "shell",
                    "status": "failed",
                    "summary": "pytest failed",
                    "stdout_tail": "ModuleNotFoundError",
                    "optional": False,
                }
            ],
        },
        changed_files=["workspaces/demo/src/app.py"],
    )

    state_path = (
        tmp_path
        / "workspaces"
        / "demo"
        / ".memory"
        / "drive"
        / "state"
        / "verification_failure_context.json"
    )
    log_path = (
        tmp_path
        / "workspaces"
        / "demo"
        / ".memory"
        / "drive"
        / "logs"
        / "verification_failures.jsonl"
    )
    md_path = (
        tmp_path
        / "workspaces"
        / "demo"
        / ".memory"
        / "drive"
        / "memory"
        / "verification_failure_context.md"
    )

    assert state_path.exists()
    assert log_path.exists()
    assert md_path.exists()
    assert block["failures"][0]["name"] == "pytest:tests"
    assert "Verification: FAIL" in md_path.read_text(encoding="utf-8")


def test_persist_verification_failure_context_includes_hygiene_cleanup_targets(
    tmp_path,
):
    block = _persist_verification_failure_context(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task123",
        remediation_attempt=1,
        max_attempts=3,
        verification_payload={
            "passed": True,
            "pass_rate": 1.0,
            "summary": "Verification: PASS",
            "results": [],
        },
        sweep_payload={
            "status": "failed",
            "summary": "BLOCKING noise: result.txt",
            "blocking_noise": [
                {
                    "path": "result.txt",
                    "severity": "block",
                    "category": "noise.artifacts",
                },
                {
                    "path": "src/demo/analyze_spec.py",
                    "severity": "block",
                    "category": "noise.scripts",
                },
            ],
        },
        completion_warnings=["sweep_blocking_noise"],
        failure_kind="hygiene",
        changed_files=["workspaces/demo/result.txt"],
    )

    md_path = (
        tmp_path
        / "workspaces"
        / "demo"
        / ".memory"
        / "drive"
        / "memory"
        / "verification_failure_context.md"
    )
    text = md_path.read_text(encoding="utf-8")

    assert block["passed"] is True
    assert block["failures"] == []
    assert block["failure_kind"] == "hygiene"
    assert {item["path"] for item in block["cleanup_targets"]} == {
        "result.txt",
        "src/demo/analyze_spec.py",
    }
    assert "Hygiene / Final Sweep Issues" in text
    assert "delete_workspace_file" in text


def test_log_phase_boundary_event_accepts_metadata_for_hygiene_skip(tmp_path):
    _log_phase_boundary_event(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task123",
        event_type="hygiene_remediation_skipped",
        metadata={
            "cleanup_targets": [{"path": "result.txt", "reason": "noise"}],
            "max_hygiene_remediations": 2,
        },
    )

    events_path = (
        tmp_path / "workspaces" / "demo" / ".memory" / "drive" / "logs" / "events.jsonl"
    )
    payload = json.loads(events_path.read_text(encoding="utf-8").splitlines()[-1])

    assert payload["type"] == "hygiene_remediation_skipped"
    assert payload["metadata"]["cleanup_targets"][0]["path"] == "result.txt"
    assert payload["metadata"]["max_hygiene_remediations"] == 2


def test_resolve_final_status_failed_required_subtask_is_terminal():
    final_status, warnings = _resolve_final_status(
        verification_payload={"passed": True, "skipped": False},
        failed_required_subtask=True,
        no_writes=False,
        sweep_payload=None,
    )

    assert final_status == "incomplete_subtasks"
    assert "failed_required_subtask" in warnings


def test_delivery_gate_downgrades_verified_when_phase_impasse_artifact_exists(tmp_path):
    drive_root = tmp_path
    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "phase_impasse.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "phase_impasse",
                "task_id": "task-xyz",
                "phase": "subtask_1",
                "tool": "mark_subtask_complete",
                "repeat_count": 3,
            }
        ),
        encoding="utf-8",
    )

    new_status, new_warnings = _apply_delivery_contract_gate(
        final_status="verified",
        completion_warnings=[],
        quality_telemetry={
            "active_domains": [],
            "missing_external_discovery_warning": False,
            "external_discovery_tool_calls": {},
        },
        drive_root=drive_root,
        task_id="task-xyz",
    )

    assert new_status == "phase_impasse"
    assert "phase_impasse" in new_warnings


def test_delivery_gate_demotes_self_review_contract_failure(tmp_path):
    new_status, new_warnings = _apply_delivery_contract_gate(
        final_status="verified",
        completion_warnings=["self_review_contract_failed"],
        quality_telemetry={
            "active_domains": [],
            "missing_external_discovery_warning": False,
            "external_discovery_tool_calls": {},
        },
        drive_root=tmp_path,
        task_id="task-xyz",
    )

    assert new_status == "failed_self_review"
    assert "self_review_contract_failed" in new_warnings


def test_delivery_gate_requires_external_discovery_for_active_domain(tmp_path):
    new_status, new_warnings = _apply_delivery_contract_gate(
        final_status="verified",
        completion_warnings=[],
        quality_telemetry={
            "active_domains": ["multi_agent_gmas"],
            "missing_external_discovery_warning": True,
            "external_discovery_tool_calls": {
                "deep_search": 0,
                "github_project_search": 0,
                "github_extract_snippets": 0,
                "mcp_discover": 0,
                "web_fetch": 0,
            },
        },
        drive_root=tmp_path,
        task_id="task-xyz",
    )

    assert new_status == "incomplete_discovery"
    assert "missing_external_discovery" in new_warnings


def test_delivery_gate_keeps_verified_when_discovery_present(tmp_path):
    new_status, new_warnings = _apply_delivery_contract_gate(
        final_status="verified",
        completion_warnings=[],
        quality_telemetry={
            "active_domains": ["multi_agent_gmas"],
            "missing_external_discovery_warning": False,
            "external_discovery_tool_calls": {
                "deep_search": 1,
                "github_project_search": 0,
                "github_extract_snippets": 0,
                "mcp_discover": 0,
                "web_fetch": 0,
            },
        },
        drive_root=tmp_path,
        task_id="task-xyz",
    )

    assert new_status == "verified"
    assert "missing_external_discovery" not in new_warnings


def test_delivery_gate_does_not_touch_failed_verification(tmp_path):
    new_status, new_warnings = _apply_delivery_contract_gate(
        final_status="failed_verification",
        completion_warnings=[],
        quality_telemetry={
            "active_domains": ["multi_agent_gmas"],
            "missing_external_discovery_warning": True,
            "external_discovery_tool_calls": {},
        },
        drive_root=tmp_path,
        task_id="task-xyz",
    )

    assert new_status == "failed_verification"
    assert new_warnings == []
