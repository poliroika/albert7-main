from pathlib import Path

from umbrella.contracts import (
    ContractBundle,
    ContractValidator,
    build_workspace_context,
    compile_phase_plan,
)


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def _layout_issues(raw_plan: dict, tmp_path: Path, setup_workspace=None) -> list:
    workspace = tmp_path / "workspaces" / "civilization"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='civ'\n", encoding="utf-8")
    (workspace / "workspace.toml").write_text(
        "[policies]\ngreenfield_python_src_layout = true\n",
        encoding="utf-8",
    )
    if setup_workspace is not None:
        setup_workspace(workspace)
    plan_ir, compile_issues = compile_phase_plan(
        raw_plan, workspace_id="civilization", run_id="run-1"
    )
    context = build_workspace_context(
        repo_root=tmp_path,
        workspace_root=workspace,
        workspace_id="civilization",
    )
    return list(compile_issues) + ContractValidator.validate(
        ContractBundle(run_id="run-1", workspace_id="civilization", plan=plan_ir),
        context=context,
    )


def _pytest_proof(*, changed_files: list[str], target: str) -> dict:
    return {
        "execution": {
            "kind": "pytest",
            "command": ["python", "-m", "pytest", target, "-q"],
            "shell": False,
        },
        "oracle": {
            "oracle_type": "unit_assertions",
            "required_properties": ["invalid_input_rejected"],
        },
        "scope": {
            "files_under_test": changed_files,
            "changed_files_expected": changed_files,
            "pytest_targets": [target],
        },
        "anti_gaming": {
            "requires_real_runtime": True,
            "allows_mock": False,
        },
    }


def test_contract_validator_rejects_greenfield_backend_src_layout(tmp_path: Path) -> None:
    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "project-scaffold",
                "title": "Project scaffold",
                "goal": "Create Python backend scaffold.",
                "files_to_create": [
                    "pyproject.toml",
                    "backend/src/app.py",
                    "tests/test_app.py",
                ],
                "proof": _pytest_proof(
                    changed_files=["backend/src/app.py"],
                    target="tests/test_app.py",
                ),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path)
    assert "greenfield_python_src_layout_policy" in _codes(issues)
    assert any("backend/src/app.py" in (issue.message or "") for issue in issues)


def test_contract_validator_rejects_bare_src_python_file(tmp_path: Path) -> None:
    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "scaffold",
                "files_to_create": ["pyproject.toml", "src/app.py", "tests/test_app.py"],
                "proof": _pytest_proof(changed_files=["src/app.py"], target="tests/test_app.py"),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path)
    assert "greenfield_python_src_layout_policy" in _codes(issues)
    assert any("bare" in (issue.message or "").lower() for issue in issues)


def test_contract_validator_rejects_parallel_src_package_roots(tmp_path: Path) -> None:
    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "api",
                "files_to_create": ["pyproject.toml", "src/api/app.py", "tests/test_api.py"],
                "proof": _pytest_proof(changed_files=["src/api/app.py"], target="tests/test_api.py"),
            },
            {
                "id": "agents",
                "files_to_create": ["src/agents/runner.py", "tests/test_agents.py"],
                "proof": _pytest_proof(
                    changed_files=["src/agents/runner.py"],
                    target="tests/test_agents.py",
                ),
            },
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path)
    assert "greenfield_python_src_layout_policy" in _codes(issues)
    assert any("one canonical" in (issue.message or "").lower() for issue in issues)


def test_contract_validator_accepts_canonical_src_package_layout(tmp_path: Path) -> None:
    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "scaffold",
                "files_to_create": [
                    "pyproject.toml",
                    "src/civilization/backend/app.py",
                    "src/civilization/agents/runner.py",
                    "tests/test_app.py",
                ],
                "proof": _pytest_proof(
                    changed_files=["src/civilization/backend/app.py"],
                    target="tests/test_app.py",
                ),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path)
    assert "greenfield_python_src_layout_policy" not in _codes(issues)


def test_layout_policy_allows_existing_backend_root_on_disk(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "civilization"
    backend = workspace / "backend"
    backend.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='civ'\n", encoding="utf-8")
    (backend / "app.py").write_text("print('legacy')\n", encoding="utf-8")
    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "repair",
                "files_to_change": ["backend/app.py"],
                "proof": _pytest_proof(
                    changed_files=["backend/app.py"],
                    target="tests/test_app.py",
                ),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path)
    assert "greenfield_python_src_layout_policy" not in _codes(issues)


def test_layout_policy_allows_existing_src_package_root_on_disk(tmp_path: Path) -> None:
    def seed(workspace: Path) -> None:
        package = workspace / "src" / "civilization"
        package.mkdir(parents=True)
        (package / "app.py").write_text("print('legacy')\n", encoding="utf-8")

    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "repair",
                "files_to_change": ["src/civilization/app.py"],
                "proof": _pytest_proof(
                    changed_files=["src/civilization/app.py"],
                    target="tests/test_app.py",
                ),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path, setup_workspace=seed)
    assert "greenfield_python_src_layout_policy" not in _codes(issues)


def test_layout_policy_rejects_bare_src_file_when_src_package_exists(
    tmp_path: Path,
) -> None:
    def seed(workspace: Path) -> None:
        package = workspace / "src" / "civilization"
        package.mkdir(parents=True)
        (package / "app.py").write_text("print('legacy')\n", encoding="utf-8")

    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "bad-src",
                "files_to_create": ["src/new_app.py"],
                "proof": _pytest_proof(
                    changed_files=["src/new_app.py"],
                    target="tests/test_app.py",
                ),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path, setup_workspace=seed)
    assert "greenfield_python_src_layout_policy" in _codes(issues)
    assert any("bare" in (issue.message or "").lower() for issue in issues)


def test_layout_policy_rejects_parallel_src_root_when_src_package_exists(
    tmp_path: Path,
) -> None:
    def seed(workspace: Path) -> None:
        package = workspace / "src" / "civilization"
        package.mkdir(parents=True)
        (package / "app.py").write_text("print('legacy')\n", encoding="utf-8")

    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "bad-parallel",
                "files_to_create": ["src/agents/runner.py"],
                "proof": _pytest_proof(
                    changed_files=["src/agents/runner.py"],
                    target="tests/test_agents.py",
                ),
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path, setup_workspace=seed)
    assert "greenfield_python_src_layout_policy" in _codes(issues)
    assert any("existing canonical" in (issue.message or "").lower() for issue in issues)


def test_backend_src_counts_as_production_for_weak_proof(tmp_path: Path) -> None:
    raw_plan = {
        "workspace_id": "civilization",
        "subtasks": [
            {
                "id": "scaffold",
                "files_to_create": ["pyproject.toml", "backend/src/app.py"],
                "proof": {
                    "execution": {"kind": "import_check", "command": ["python", "-c", "pass"]},
                    "oracle": {"oracle_type": "unit_assertions", "required_properties": ["x"]},
                    "scope": {
                        "files_under_test": ["backend/src/app.py"],
                        "changed_files_expected": ["backend/src/app.py"],
                    },
                    "anti_gaming": {"requires_real_runtime": True, "allows_mock": False},
                },
            }
        ],
    }
    issues = _layout_issues(raw_plan, tmp_path)
    assert "weak_proof" in _codes(issues)
    assert "greenfield_python_src_layout_policy" in _codes(issues)
