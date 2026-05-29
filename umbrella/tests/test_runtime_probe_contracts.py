from umbrella.contracts.runtime_probes import run_capability_probes, validate_probe_spec


def test_desktop_gui_headless_allows_import_only_toolkit_probe() -> None:
    spec = {
        "kind": "command",
        "command": ["python", "-c", "import tkinter"],
        "expect_exit": 0,
    }

    assert validate_probe_spec(spec, capability_tag="desktop_gui_headless") is None


def test_desktop_gui_runtime_still_rejects_import_only_probe() -> None:
    spec = {
        "kind": "command",
        "intent": "real_gui_root_lifecycle",
        "command": ["python", "-c", "import tkinter"],
        "expect_exit": 0,
    }

    issue = validate_probe_spec(spec, capability_tag="desktop_gui_runtime")

    assert issue is not None
    assert "only imports modules" in issue


def test_desktop_gui_runtime_requires_typed_probe_intent() -> None:
    spec = {
        "kind": "command",
        "command": [
            "python",
            "-c",
            "import tkinter as tk; root=tk.Tk(); root.update(); root.destroy()",
        ],
        "expect_exit": 0,
    }

    issue = validate_probe_spec(spec, capability_tag="desktop_gui_runtime")

    assert issue is not None
    assert "intent=real_gui_root_lifecycle" in issue


def test_desktop_gui_runtime_accepts_real_lifecycle_intent() -> None:
    spec = {
        "kind": "command",
        "intent": "real_gui_root_lifecycle",
        "command": [
            "python",
            "-c",
            "import tkinter as tk; root=tk.Tk(); root.update(); root.destroy()",
        ],
        "expect_exit": 0,
    }

    assert validate_probe_spec(spec, capability_tag="desktop_gui_runtime") is None


def test_generic_probe_still_rejects_import_only_proof() -> None:
    spec = {
        "kind": "command",
        "command": ["python", "-c", "import tkinter"],
        "expect_exit": 0,
    }

    issue = validate_probe_spec(spec, capability_tag="python")

    assert issue is not None
    assert "only imports modules" in issue


def test_capability_probe_records_execution_environment_binding(tmp_path) -> None:
    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)

    result = run_capability_probes(
        {
            "python_command": {
                "probe": {
                    "kind": "command",
                    "execution_environment_id": "workspace_python",
                    "command": ["python", "-c", "print('ok')"],
                    "expect_exit": 0,
                }
            }
        },
        workspace_root=workspace,
    )

    entry = result["python_command"]
    assert entry["available"] is True
    assert entry["execution_environment_id"] == "workspace_python"
    assert entry["python_executable"]
    assert entry["env_hash"]
