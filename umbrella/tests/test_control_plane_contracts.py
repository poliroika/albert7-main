"""Tests for capability declaration and generic runtime probes."""

import sys
from pathlib import Path

from umbrella.contracts.capability_declaration import (
    build_declaration_from_probes,
    declaration_ready_for_handoff,
    load_capability_declaration,
    persist_capability_declaration,
    proof_required_capabilities,
    validate_proof_against_capabilities,
)
from umbrella.contracts.models import (
    ProofAntiGamingSpec,
    ProofExecutionSpec,
    ProofOracleSpec,
    ProofScopeSpec,
    ProofSpec,
)
from umbrella.contracts.runtime_probes import (
    baseline_runtime_capabilities,
    execute_probe,
    persist_runtime_capabilities,
    run_capability_probes,
)
from umbrella.contracts.validators import validate_proof_spec


def test_proof_required_capabilities_explicit_only() -> None:
    proof = ProofSpec(
        execution=ProofExecutionSpec(kind="http_boot", command=("python", "app.py")),
        oracle=ProofOracleSpec(oracle_type="behavioral_http"),
        scope=ProofScopeSpec(),
        required_capabilities=("network", "browser_ui"),
    )
    assert proof_required_capabilities(proof) == frozenset({"network", "browser_ui"})


def test_proof_without_explicit_capabilities_only_subprocess_when_command() -> None:
    proof = ProofSpec(
        execution=ProofExecutionSpec(kind="pytest", command=("python", "-m", "pytest", "-q")),
        oracle=ProofOracleSpec(oracle_type="unit_assertions"),
        scope=ProofScopeSpec(),
    )
    assert proof_required_capabilities(proof) == frozenset({"subprocess"})


def test_validate_proof_spec_rejects_undeclared_capability() -> None:
    proof = ProofSpec(
        execution=ProofExecutionSpec(kind="command", command=("pytest", "-q")),
        oracle=ProofOracleSpec(oracle_type="unit_assertions"),
        scope=ProofScopeSpec(),
        required_capabilities=("network",),
    )
    issues = validate_proof_spec(
        proof,
        runtime_capabilities={"python": True, "subprocess": True},
    )
    assert any(item.code == "capability_probe_failed" for item in issues)


def test_run_capability_probes_executes_agent_command(tmp_path: Path) -> None:
    caps = run_capability_probes(
        {
            "python": {
                "probe": {
                    "kind": "command",
                    "command": [sys.executable, "-c", "print(1)"],
                    "expect_exit": 0,
                }
            }
        },
        workspace_root=tmp_path,
    )
    assert caps["python"]["available"] is True


def test_capability_declaration_persist_and_handoff(tmp_path: Path) -> None:
    caps = baseline_runtime_capabilities()
    persist_runtime_capabilities(tmp_path, caps)
    payload = build_declaration_from_probes(
        run_id="run-1",
        workspace_id="ws",
        probed=caps,
        actor="agent",
        status="submitted",
        notes="Discovery notes with enough detail for plan gating.",
        constraints=["headless runner"],
    )
    persist_capability_declaration(tmp_path, payload)
    loaded = load_capability_declaration(tmp_path)
    assert loaded is not None
    assert declaration_ready_for_handoff(loaded)


def test_validate_proof_against_capabilities_message() -> None:
    proof = {
        "required_capabilities": ["network"],
        "execution": {"kind": "command", "command": ["true"]},
        "oracle": {"oracle_type": "unit_assertions"},
        "anti_gaming": {},
    }
    issue = validate_proof_against_capabilities(proof, {"network": False, "python": True})
    assert issue is not None
    assert "network" in issue


def test_submit_capability_declaration_writes_canonical_state_path(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "gui": {"available": True, "source": "research", "reason": "tkinter"},
            "desktop_gui_headless": {
                "available": True,
                "source": "declared",
                "reason": "Tkinter controller/import proof can run headlessly.",
            },
        },
        constraints=["Python 3"],
        notes="Tkinter GUI calculator is feasible with the standard library.",
        discovery_channels=["web_search", "github_project_search"],
    )

    assert result.startswith("OK:")
    canonical = drive / "state" / "capability_declaration.json"
    nested = drive / "state" / "state" / "capability_declaration.json"
    assert canonical.is_file()
    assert not nested.is_file()
    loaded = load_capability_declaration(drive)
    assert loaded is not None
    assert loaded.status == "submitted"
    assert declaration_ready_for_handoff(loaded)


def test_submit_capability_declaration_tool_schema_requires_caps_and_notes() -> None:
    from umbrella.deep_agent_tools.phase_control_tools import get_tools

    tool = next(item for item in get_tools() if item.name == "submit_capability_declaration")
    required = set(tool.schema["parameters"]["required"])

    assert {"capabilities", "notes"} <= required


def test_submit_capability_declaration_rejects_unprobed_desktop_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "desktop_gui_runtime": {
                "available": True,
                "source": "declared",
                "reason": "Tkinter import check passed.",
            }
        },
        notes="Real desktop GUI runtime must be probe-backed under its own slug.",
    )

    assert result.startswith("ERROR:")
    assert "desktop_gui_runtime" in result
    assert "successful probe" in result


def test_submit_capability_declaration_rejects_desktop_runtime_notes_without_entry(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        notes=(
            "Simple Tkinter calculator needs desktop GUI runtime for "
            "real-window smoke proof."
        ),
    )

    assert result.startswith("ERROR:")
    assert "desktop_gui_runtime" in result
    assert "missing from capabilities" in result


def test_submit_capability_declaration_rejects_unverified_unavailable_desktop_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "desktop_gui_runtime": {
                "available": False,
                "source": "standard_library",
                "reason": (
                    "Tkinter should be available on Windows, but the display "
                    "automation path needs verification."
                ),
            },
            "desktop_gui_headless": {
                "available": True,
                "source": "standard_library",
                "reason": "Tkinter can be imported without opening a window.",
            },
        },
        notes=(
            "Calculator application requires GUI capabilities. "
            "desktop_gui_runtime needs verification of the display path."
        ),
    )

    assert result.startswith("ERROR:")
    assert "desktop_gui_runtime" in result
    assert "needs verification" in result
    assert "same submit_capability_declaration call" in result


def test_submit_capability_declaration_rejects_native_gui_without_usable_harness_capability(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "python": {
                "available": True,
                "source": "host auto-detect",
                "reason": "Python is the standard language for GUI work.",
            },
            "desktop_gui_headless": {
                "available": False,
                "source": "declared",
                "reason": "Not detected and not required for this task.",
            },
        },
        notes=(
            "Research recommends Python with Tkinter for a simple GUI "
            "application because it is built in."
        ),
    )

    assert result.startswith("ERROR:")
    assert "GUI harness capability" in result


def test_submit_capability_declaration_rejects_import_only_desktop_runtime_probe(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "desktop_gui_runtime": {
                "available": False,
                "reason": "Need a real runtime check.",
            }
        },
        probes={
            "desktop_gui_runtime": {
                "kind": "command",
                "command": [sys.executable, "-c", "import tkinter; print('tk ok')"],
                "expect_exit": 0,
            }
        },
        notes="Desktop GUI runtime needs a same-slug real-window probe.",
    )

    assert result.startswith("ERROR:")
    assert "Import-only" in result


def test_submit_capability_declaration_accepts_probe_backed_desktop_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext
    from umbrella.contracts import runtime_probes

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    def fake_execute_probe(
        spec: dict,
        *,
        workspace_root: Path,
        capability_tag: str = "",
        timeout_sec: float = 5.0,
    ) -> tuple[bool, str]:
        assert capability_tag == "desktop_gui_runtime"
        assert workspace_root == tmp_path / "workspaces" / "calc"
        assert timeout_sec >= 1.0
        assert spec
        return True, ""

    monkeypatch.setattr(runtime_probes, "execute_probe", fake_execute_probe)

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "desktop_gui_runtime": {
                "available": True,
                "reason": "Probe confirms the requested runtime path can execute.",
            }
        },
        probes={
            "desktop_gui_runtime": {
                "kind": "command",
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "import tkinter as tk; "
                        "root = tk.Tk(); root.update(); root.destroy()"
                    ),
                ],
                "expect_exit": 0,
            }
        },
        notes="Desktop GUI runtime capability is backed by a same-slug probe.",
    )

    assert result.startswith("OK:")
    loaded = load_capability_declaration(drive)
    assert loaded is not None
    assert loaded.capabilities["desktop_gui_runtime"].source == "probe"
    assert loaded.probe_audit["desktop_gui_runtime"] is True


def test_submit_capability_declaration_rejects_declared_probe_mismatch(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext
    from umbrella.contracts import runtime_probes

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a GUI calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    def fake_execute_probe(
        spec: dict,
        *,
        workspace_root: Path,
        capability_tag: str = "",
        timeout_sec: float = 5.0,
    ) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(runtime_probes, "execute_probe", fake_execute_probe)

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "desktop_gui_runtime": {
                "available": False,
                "reason": "GUI runtime still needs verification.",
            }
        },
        probes={
            "desktop_gui_runtime": {
                "kind": "command",
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "import tkinter as tk; "
                        "root = tk.Tk(); root.update(); root.destroy()"
                    ),
                ],
                "expect_exit": 0,
            }
        },
        notes="Desktop GUI runtime is still not verified.",
    )

    assert result.startswith("ERROR:"), result
    assert "declared available=false" in result
    assert "probe succeeded" in result
    assert not (drive / "state" / "capability_declaration.json").exists()


def test_submit_capability_declaration_accepts_attempt_suffix_task_id(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research:1779697135441"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "gui": {"available": True, "source": "research", "reason": "tkinter"},
            "desktop_gui_headless": {
                "available": True,
                "source": "declared",
                "reason": "Tkinter controller/import proof can run headlessly.",
            },
        },
        constraints=["Python 3"],
        notes="Tkinter GUI calculator is feasible with the standard library.",
        discovery_channels=["web_search", "github_project_search"],
    )

    assert result.startswith("OK:")


def test_submit_capability_declaration_normalizes_discovery_alias_rows(
    tmp_path: Path, monkeypatch,
) -> None:
    from umbrella.deep_agent_tools import phase_control_research
    from umbrella.deep_agent_tools.phase_control_common import ToolContext

    drive = tmp_path / "workspaces" / "calc" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "workspaces" / "calc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspaces" / "calc" / "TASK_MAIN.md").write_text(
        "Build a calculator.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UMBRELLA_REPO_ROOT", str(tmp_path))
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_test:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "calc"}

    result = phase_control_research._submit_capability_declaration(
        ctx,
        status="submitted",
        capabilities={
            "tkinter": {"available": True, "source": "research"},
            "desktop_gui_headless": {
                "available": True,
                "source": "declared",
                "reason": "Tkinter controller/import proof can run headlessly.",
            },
        },
        notes="Tkinter GUI calculator is feasible with current research evidence.",
        discovery_channels=[
            {"channel": "github", "search": "python calculator tkinter", "results": 3}
        ],
        discoveries=[
            {"channel": "web", "search": "tkinter calculator tutorial", "results": 3}
        ],
    )

    assert result.startswith("OK:")
    loaded = load_capability_declaration(drive)
    assert loaded is not None
    tools = {row["tool"] for row in loaded.discovery_channels}
    assert tools == {"github_project_search", "web_search"}


def test_research_summary_tool_schema_requires_notes() -> None:
    from umbrella.deep_agent_tools.phase_control_tools import get_tools

    summary_tool = next(tool for tool in get_tools() if tool.name == "submit_research_summary")
    params = summary_tool.schema["parameters"]

    assert "notes" in params["required"]
    assert params["properties"]["notes"]["minLength"] == 20
