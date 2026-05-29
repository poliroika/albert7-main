from __future__ import annotations

from types import SimpleNamespace

from umbrella.context.compiler import compile_phase_context
from umbrella.context.render import bundle_to_overlay_dict
from umbrella.contracts.harness_profiles import (
    build_harness_contract_payload,
    get_harness_profile,
    probe_required_capability_ids,
    render_harness_contract_markdown,
    validator_flags_from_harness_payload,
)
from umbrella.contracts.models import (
    ProofAntiGamingSpec,
    ProofExecutionSpec,
    ProofOracleSpec,
    ProofScopeSpec,
    ProofSpec,
)
from umbrella.contracts.validators import validate_proof_spec


def _node(phase_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=phase_id)


def test_plan_context_includes_compact_harness_catalog(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    bundle = compile_phase_context(
        workspace_root=workspace,
        workspace_id="demo",
        run_id="run1",
        task_id="run1:plan",
        manifest=_node("plan"),
        phase_node=_node("plan"),
    )
    overlay = bundle_to_overlay_dict(bundle)

    assert overlay["harness_contract"]["mode"] == "catalog"
    profile_ids = {
        profile["id"] for profile in overlay["harness_contract"]["profiles"]
    }
    assert "desktop_gui_headless" in profile_ids
    assert "desktop_gui_runtime" in profile_ids
    assert any(item["role"] == "harness_contract" for item in overlay["contract_items"])


def test_execute_context_selects_active_subtask_harness(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    active_subtask = {
        "id": "gui-controller",
        "title": "GUI controller behavior",
        "goal": "Implement headless controller state transitions.",
        "files_to_create": ["src/app/gui.py", "tests/test_gui.py"],
        "proof": {
            "harness_profile": "desktop_gui_headless",
            "execution": {
                "kind": "pytest",
                "command": ["python", "-m", "pytest", "tests/test_gui.py", "-q"],
            },
            "oracle": {"required_properties": ["no_test_tampering"]},
            "scope": {
                "files_under_test": ["src/app/gui.py"],
                "changed_files_expected": ["src/app/gui.py", "tests/test_gui.py"],
                "pytest_targets": ["tests/test_gui.py"],
            },
        },
    }

    bundle = compile_phase_context(
        workspace_root=workspace,
        workspace_id="demo",
        run_id="run1",
        task_id="run1:execute:1",
        manifest=_node("execute"),
        phase_node=_node("execute"),
        active_subtask=active_subtask,
    )
    overlay = bundle_to_overlay_dict(bundle)

    harness = overlay["harness_contract"]
    assert harness["mode"] == "active"
    assert "desktop_gui_headless" in harness["selected_ids"]
    assert "python_src_layout" in harness["selected_ids"]
    assert "no_native_gui_root_in_unit_proof" in validator_flags_from_harness_payload(harness)
    assert "real native GUI root" in overlay["contract_items"][0]["text"]


def test_execute_context_selects_runtime_gui_without_headless_guard(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    active_subtask = {
        "id": "gui-runtime",
        "title": "GUI real-window smoke",
        "goal": "Launch the native GUI and click an addition flow.",
        "files_to_create": ["src/app/gui.py", "tests/test_gui_runtime.py"],
        "proof": {
            "harness_profile": "desktop_gui_runtime",
            "required_capabilities": ["python", "subprocess", "desktop_gui_runtime"],
            "harness_options": {
                "launch_command": ["python", "-m", "app"],
                "interaction_driver": "toolkit event script",
                "evidence": ["stdout", "screenshot"],
                "cleanup": "destroy window after assertion",
            },
            "execution": {
                "kind": "pytest",
                "command": ["python", "-m", "pytest", "tests/test_gui_runtime.py", "-q"],
            },
            "oracle": {"required_properties": ["runtime_started"]},
            "scope": {
                "files_under_test": ["src/app/gui.py"],
                "changed_files_expected": ["src/app/gui.py", "tests/test_gui_runtime.py"],
                "pytest_targets": ["tests/test_gui_runtime.py"],
            },
        },
    }

    payload = build_harness_contract_payload(
        phase_id="execute",
        active_subtask=active_subtask,
    )

    assert "desktop_gui_runtime" in payload["selected_ids"]
    assert "desktop_gui_headless" not in payload["selected_ids"]
    assert "no_native_gui_root_in_unit_proof" not in validator_flags_from_harness_payload(payload)
    assert "native_gui_runtime_proof" in validator_flags_from_harness_payload(payload)


def test_agent_words_do_not_select_llm_runtime_harness_without_typed_capability():
    payload = build_harness_contract_payload(
        phase_id="execute",
        active_subtask={
            "id": "agent-router",
            "title": "Agent judge router",
            "goal": "Implement local agent and judge naming without LLM APIs.",
            "files_to_change": ["src/agent_router.py"],
            "proof": {
                "execution": {
                    "kind": "pytest",
                    "command": ["python", "-m", "pytest", "tests/test_agent_router.py", "-q"],
                },
                "scope": {"files_under_test": ["src/agent_router.py"]},
            },
        },
    )

    assert "llm_runtime" not in payload["selected_ids"]


def test_required_llm_capability_does_not_select_runtime_harness_without_profile():
    payload = build_harness_contract_payload(
        phase_id="execute",
        active_subtask={
            "id": "llm-runtime",
            "title": "Model runtime integration",
            "files_to_change": ["src/model_runtime.py"],
            "proof": {
                "required_capabilities": ["llm_api"],
                "execution": {
                    "kind": "pytest",
                    "command": ["python", "-m", "pytest", "tests/test_model_runtime.py", "-q"],
                },
            },
        },
    )

    assert "llm_runtime" not in payload["selected_ids"]


def test_explicit_llm_profile_selects_llm_runtime_harness():
    payload = build_harness_contract_payload(
        phase_id="execute",
        active_subtask={
            "id": "llm-runtime",
            "title": "Model runtime integration",
            "files_to_change": ["src/model_runtime.py"],
            "proof": {
                "harness_profile": "llm_runtime",
                "required_capabilities": ["llm_api"],
                "execution": {
                    "kind": "pytest",
                    "command": ["python", "-m", "pytest", "tests/test_model_runtime.py", "-q"],
                },
            },
        },
    )

    assert "llm_runtime" in payload["selected_ids"]


def test_gui_click_words_do_not_select_runtime_harness_without_profile():
    payload = build_harness_contract_payload(
        phase_id="execute",
        active_subtask={
            "id": "calculator-gui",
            "title": "Implement Tkinter GUI window",
            "goal": "Create calculator GUI with buttons, display, and click handlers.",
            "files_to_create": ["src/calculator/gui.py", "tests/test_gui.py"],
            "files_to_change": ["src/calculator/__init__.py"],
            "proof": {
                "required_capabilities": ["python"],
                "execution": {
                    "kind": "pytest",
                    "command": ["python", "-m", "pytest", "tests/test_gui.py", "-q"],
                },
                "oracle": {"required_properties": ["no_test_tampering"]},
                "scope": {
                    "files_under_test": ["src/calculator/gui.py", "tests/test_gui.py"],
                    "changed_files_expected": [
                        "src/calculator/gui.py",
                        "src/calculator/__init__.py",
                        "tests/test_gui.py",
                    ],
                    "pytest_targets": ["tests/test_gui.py::test_button_updates_display"],
                },
            },
        },
    )

    assert "desktop_gui_runtime" not in payload["selected_ids"]
    assert "desktop_gui_headless" not in payload["selected_ids"]
    assert "python_src_layout" in payload["selected_ids"]


def test_harness_markdown_is_compact_for_prompt_injection():
    payload = build_harness_contract_payload(phase_id="plan")
    markdown = render_harness_contract_markdown(payload)

    assert "Planner harness profile catalog" in markdown
    assert "`desktop_gui_headless`" in markdown
    assert "`desktop_gui_runtime`" in markdown
    assert len(markdown) < 3200


def test_desktop_runtime_profile_declares_probe_required_capability():
    profile = get_harness_profile("desktop_gui_runtime")

    assert profile is not None
    assert profile.probe_required_capabilities == ("desktop_gui_runtime",)
    assert "desktop_gui_runtime" in probe_required_capability_ids()


def test_desktop_gui_runtime_requires_explicit_capability_contract():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_gui_runtime.py", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py", "tests/test_gui_runtime.py"),
            pytest_targets=("tests/test_gui_runtime.py",),
        ),
        harness_profile="desktop_gui_runtime",
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="runtime")
    codes = {issue.code for issue in issues}

    assert "capability_probe_failed" in codes
    assert "weak_proof" in codes


def test_desktop_gui_runtime_rejects_pytest_as_managed_launch():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_runtime_smoke.py", "-v"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started", "module_imports", "no_test_tampering"),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/main.py", "tests/test_runtime_smoke.py"),
            changed_files_expected=("src/app/main.py", "tests/test_runtime_smoke.py"),
            pytest_targets=("tests/test_runtime_smoke.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(
            allows_mock=False,
            requires_real_runtime=True,
        ),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "managed_runtime": True,
            "notes": "Test script creates a real window and destroys it.",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    messages = "\n".join(
        issue.message
        for issue in validate_proof_spec(proof, phase="plan", subtask_id="runtime")
    )

    assert "proof.execution.kind must be `command`" in messages
    assert "must include structured readiness" in messages
    assert "must include cleanup instructions" in messages


def test_allows_mock_false_rejects_mocked_proof_command():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=(
                "python",
                "-c",
                "from unittest.mock import Mock; app = build_app(display=Mock())",
            ),
        ),
        oracle=ProofOracleSpec(
            oracle_type="build",
            required_properties=("build_succeeds",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(allows_mock=False),
        harness_profile="desktop_gui_headless",
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui")

    assert any(
        issue.code == "weak_proof" and "allows_mock=false" in issue.message
        for issue in issues
    )


def test_allows_mock_false_rejects_mock_word_variants_in_options():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_gui.py", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("no_test_tampering",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py", "tests/test_gui.py"),
            pytest_targets=("tests/test_gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(allows_mock=False),
        harness_profile="desktop_gui_headless",
        harness_options={
            "notes": "Mocks Tkinter widgets to partition headless behavior."
        },
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui")

    assert any(
        issue.code == "weak_proof" and "allows_mock=false" in issue.message
        for issue in issues
    )


def test_allows_mock_false_rejects_simulated_runtime_mode():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_gui.py", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("no_test_tampering",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py", "tests/test_gui.py"),
            pytest_targets=("tests/test_gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(allows_mock=False),
        harness_profile="desktop_gui_headless",
        harness_options={
            "notes": "Runs the GUI under a simulated display mode.",
        },
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui")

    assert any(
        issue.code == "weak_proof" and "allows_mock=false" in issue.message
        for issue in issues
    )


def test_desktop_gui_headless_cannot_claim_real_runtime():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_gui.py", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("no_test_tampering",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py", "tests/test_gui.py"),
            pytest_targets=("tests/test_gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(requires_real_runtime=True),
        harness_profile="desktop_gui_headless",
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui")

    assert any(
        issue.code == "weak_proof"
        and "desktop_gui_headless proof cannot claim" in issue.message
        for issue in issues
    )


def test_desktop_gui_runtime_rejects_mock_display_contract():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=("python", "-m", "pytest", "tests/test_gui_e2e.py", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py", "tests/test_gui_e2e.py"),
            pytest_targets=("tests/test_gui_e2e.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(
            allows_mock=False,
            requires_real_runtime=True,
        ),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "launch_command": ["python", "run_app.py"],
            "readiness_probe": "window_visible",
            "interaction_driver": "tkinter_event_injection",
            "cleanup": "process_terminate",
            "notes": "Test creates a mock display environment before launching.",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")
    messages = "\n".join(issue.message for issue in issues)

    assert "desktop_gui_runtime proof cannot describe" in messages
    assert "allows_mock=false" in messages


def test_desktop_gui_runtime_rejects_freeform_readiness_probe():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=("python", "-c", "print('READY')"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(requires_real_runtime=True),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "readiness_probe": "window appears with title Calculator",
            "cleanup": "terminate process",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")

    assert any(
        issue.code == "weak_proof" and "readiness must be a structured" in issue.message
        for issue in issues
    )


def test_desktop_gui_runtime_requires_driver_for_behavioral_oracle():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=("python", "-c", "print('READY')"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started", "distinct_inputs_distinct_outputs"),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(requires_real_runtime=True),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "readiness": {"type": "log_contains", "text": "READY"},
            "interaction_test": "Click 7 + 5 = and expect 12",
            "cleanup": "terminate process",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")

    assert any(
        issue.code == "weak_proof" and "does not provide" in issue.message
        for issue in issues
    )


def test_desktop_gui_runtime_no_test_tampering_meta_does_not_require_driver():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=("python", "run_app.py"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started", "no_test_tampering"),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py", "tests/test_gui_e2e.py"),
            changed_files_expected=("tests/test_gui_e2e.py",),
            pytest_targets=("tests/test_gui_e2e.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(
            allows_mock=False,
            requires_real_runtime=True,
        ),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "managed_runtime": True,
            "readiness": {"type": "process_alive"},
            "cleanup": "process_terminate",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    messages = "\n".join(
        issue.message
        for issue in validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")
    )

    assert "does not provide harness_options.assert_command" not in messages


def test_desktop_gui_runtime_rejects_nested_subprocess_launch():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=(
                "python",
                "-c",
                "import subprocess; subprocess.run(['python', '-m', 'app'])",
            ),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(requires_real_runtime=True),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "readiness": {"type": "process_alive"},
            "cleanup": "terminate process",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")

    assert any(
        issue.code == "weak_proof" and "direct managed launch command" in issue.message
        for issue in issues
    )


def test_desktop_gui_runtime_accepts_structured_readiness_and_driver():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=("python", "-c", "print('READY')"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started", "distinct_inputs_distinct_outputs"),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py",),
            changed_files_expected=("src/app/gui.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(requires_real_runtime=True),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "managed_runtime": True,
            "readiness": {"type": "log_contains", "text": "READY"},
            "driver_command": ["python", "tests/drive_gui.py"],
            "cleanup": "terminate process",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    messages = "\n".join(
        issue.message
        for issue in validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")
    )

    assert "desktop_gui_runtime readiness" not in messages
    assert "does not provide" not in messages


def test_desktop_gui_runtime_allows_real_driver_that_simulates_user_clicks():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="command",
            command=("python", "run_app.py"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("runtime_started", "distinct_inputs_distinct_outputs", "no_test_tampering"),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app/gui.py", "tests/test_gui_e2e.py"),
            changed_files_expected=("tests/test_gui_e2e.py",),
            pytest_targets=("tests/test_gui_e2e.py",),
        ),
        anti_gaming=ProofAntiGamingSpec(
            allows_mock=False,
            requires_real_runtime=True,
        ),
        harness_profile="desktop_gui_runtime",
        harness_options={
            "managed_runtime": True,
            "readiness": {"type": "process_alive"},
            "driver_command": ["python", "tests/drive_gui.py"],
            "cleanup": "process_terminate",
            "notes": "Launch the real Tkinter window, simulate button clicks through the driver, and assert the display text.",
        },
        required_capabilities=("python", "subprocess", "desktop_gui_runtime"),
    )

    messages = "\n".join(
        issue.message
        for issue in validate_proof_spec(proof, phase="plan", subtask_id="gui-e2e")
    )

    assert "desktop_gui_runtime proof cannot describe" not in messages
    assert "allows_mock=false" not in messages
    assert "does not provide harness_options.assert_command" not in messages


def test_unknown_harness_profile_is_a_typed_contract_issue():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_x.py", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("distinct_inputs_distinct_outputs",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app.py",),
            changed_files_expected=("src/app.py", "tests/test_x.py"),
            pytest_targets=("tests/test_x.py",),
        ),
        harness_profile="not_a_profile",
    )

    issues = validate_proof_spec(proof, phase="plan", subtask_id="s1")

    assert [issue.code for issue in issues] == ["unknown_harness_profile"]
