from umbrella.contracts.runtime_probes import validate_probe_spec


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
        "command": ["python", "-c", "import tkinter"],
        "expect_exit": 0,
    }

    issue = validate_probe_spec(spec, capability_tag="desktop_gui_runtime")

    assert issue is not None
    assert "desktop_gui_runtime probe must exercise real native GUI runtime" in issue


def test_generic_probe_still_rejects_import_only_proof() -> None:
    spec = {
        "kind": "command",
        "command": ["python", "-c", "import tkinter"],
        "expect_exit": 0,
    }

    issue = validate_probe_spec(spec, capability_tag="python")

    assert issue is not None
    assert "only imports modules" in issue
