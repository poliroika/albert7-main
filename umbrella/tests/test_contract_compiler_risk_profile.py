from pathlib import Path

from umbrella.contracts.capability_declaration import persist_capability_declaration
from umbrella.contracts.compiler import ContractCompiler
from umbrella.contracts.models import (
    PlanIR,
    ProofExecutionSpec,
    ProofOracleSpec,
    ProofScopeSpec,
    ProofSpec,
    SubtaskIR,
)


def _compiler(tmp_path: Path) -> ContractCompiler:
    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    persist_capability_declaration(
        drive,
        {
            "status": "submitted",
            "run_id": "run-1",
            "workspace_id": "calc",
            "capabilities": {
                "python": {"available": True},
                "network": {"available": True},
            },
            "notes": "Network availability is an environment capability, not a web-app domain.",
        },
    )
    return ContractCompiler(repo_root=tmp_path, drive_root=drive, workspace_id="calc")


def test_network_availability_does_not_imply_web_runtime_risk(tmp_path: Path) -> None:
    plan = PlanIR(
        run_id="run-1",
        workspace_id="calc",
        subtasks=(
            SubtaskIR(
                id="desktop-gui",
                title="desktop gui",
                goal="build tkinter calculator",
                files_to_create=("src/calculator/gui.py",),
                proof=ProofSpec(
                    execution=ProofExecutionSpec(
                        kind="pytest",
                        command=("python", "-m", "pytest", "tests/test_gui.py"),
                    ),
                    oracle=ProofOracleSpec(oracle_type="unit_assertions"),
                    scope=ProofScopeSpec(
                        files_under_test=("src/calculator/gui.py",),
                        changed_files_expected=("src/calculator/gui.py",),
                    ),
                    harness_profile="desktop_gui_headless",
                    required_capabilities=("python",),
                ),
            ),
        ),
    )

    risk = _compiler(tmp_path)._build_risk_profile(plan)

    assert risk.external_api is False
    assert risk.web_or_http_runtime is False


def test_explicit_network_or_browser_usage_sets_runtime_risk(tmp_path: Path) -> None:
    plan = PlanIR(
        run_id="run-1",
        workspace_id="calc",
        subtasks=(
            SubtaskIR(
                id="web-smoke",
                title="web smoke",
                goal="boot web UI",
                files_to_create=("app.py",),
                proof=ProofSpec(
                    execution=ProofExecutionSpec(
                        kind="http_boot",
                        command=("python", "app.py"),
                    ),
                    oracle=ProofOracleSpec(oracle_type="behavioral_http"),
                    scope=ProofScopeSpec(
                        files_under_test=("app.py",),
                        changed_files_expected=("app.py",),
                    ),
                    required_capabilities=("network", "browser_ui"),
                ),
            ),
        ),
    )

    risk = _compiler(tmp_path)._build_risk_profile(plan)

    assert risk.external_api is True
    assert risk.web_or_http_runtime is True
