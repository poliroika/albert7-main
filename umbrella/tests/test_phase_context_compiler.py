import json
from pathlib import Path

from umbrella.context.compiler import compile_phase_context
from umbrella.context.render import bundle_to_overlay_dict, persist_llm_input_bundle
from umbrella.phases.base import PhaseNode
from umbrella.phases.registry import get_registry


def _manifest(phase_id: str):
    repo = Path(__file__).resolve().parents[2]
    return get_registry(repo / "umbrella" / "phases" / "manifests").get(phase_id)


def test_phase_context_compiler_execute_includes_active_subtask_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    active = {
        "id": "scaffold",
        "files_to_create": ["src/demo/app.py"],
        "proof": {"scope": {"changed_files_expected": ["src/demo/app.py"]}},
    }
    bundle = compile_phase_context(
        workspace_root=workspace,
        workspace_id="demo",
        run_id="run-1",
        task_id="run-1:execute",
        manifest=_manifest("execute"),
        phase_node=PhaseNode(id="execute", manifest_id="execute"),
        active_subtask=active,
        phase_prompt_sections=[{"path": "execute.system.md", "text": "Execute prompt"}],
        authoritative_artifacts=[],
        recall_bundle={"hot": [{"id": "mem-1", "content": "prior finding"}]},
        drive_root=drive,
    )
    assert bundle.active_subtask == active
    assert len(bundle.memory_items) == 1
    assert any(item.role == "active_subtask" for item in bundle.user_sections)
    assert bundle.workspace_inventory is not None
    assert "src/demo/app.py" in bundle.workspace_inventory.missing_declared_files


def test_phase_context_compiler_execute_excludes_stale_proposal_as_authoritative(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps({"run_id": "run-1", "plan": {"subtasks": [{"id": "ok"}]}}),
        encoding="utf-8",
    )
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps({"run_id": "run-1", "plan": {"subtasks": [{"id": "bad"}]}}),
        encoding="utf-8",
    )
    bundle = compile_phase_context(
        workspace_root=workspace,
        workspace_id="demo",
        run_id="run-1",
        task_id="run-1:execute",
        manifest=_manifest("execute"),
        phase_node=PhaseNode(id="execute", manifest_id="execute"),
        authoritative_artifacts=[
            {
                "path": ".memory/drive/state/phase_plan_submitted_latest.json",
                "content": (state / "phase_plan_submitted_latest.json").read_text(encoding="utf-8"),
            },
            {
                "path": ".memory/drive/state/phase_plan_proposal_latest.json",
                "content": (state / "phase_plan_proposal_latest.json").read_text(encoding="utf-8"),
            },
        ],
        drive_root=drive,
    )
    roles = [item.include_reason for item in bundle.user_sections if item.role == "authoritative_artifact"]
    assert "submitted_plan" in roles


def test_llm_input_bundle_persists_source_refs_and_hash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    bundle = compile_phase_context(
        workspace_root=workspace,
        workspace_id="demo",
        run_id="run-2",
        task_id="run-2:plan",
        manifest=_manifest("plan"),
        phase_node=PhaseNode(id="plan", manifest_id="plan"),
        phase_prompt_sections=[{"path": "plan.system.md", "text": "Plan prompt"}],
        drive_root=drive,
    )
    path = persist_llm_input_bundle(bundle, drive)
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["input_hash"] == bundle.input_hash
    assert payload["phase_id"] == "plan"
    overlay = bundle_to_overlay_dict(bundle)
    assert overlay["allowed_tools"] == []


def test_phase_context_compiler_accepts_prompt_content_key(tmp_path: Path) -> None:
    workspace = tmp_path / "repo" / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    bundle = compile_phase_context(
        workspace_root=workspace,
        workspace_id="demo",
        run_id="run-3",
        task_id="run-3:plan",
        manifest=_manifest("plan"),
        phase_node=PhaseNode(id="plan", manifest_id="plan"),
        phase_prompt_sections=[
            {"path": "umbrella/prompts/phases/plan.system.md", "content": "Plan prompt from worker"}
        ],
    )
    assert bundle.system_sections[0].text == "Plan prompt from worker"
