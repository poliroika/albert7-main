import json
from types import SimpleNamespace
from pathlib import Path

from umbrella.context.compiler import compile_phase_context
from umbrella.context.render import bundle_to_overlay_dict, persist_llm_input_bundle
from umbrella.orchestrator.worker import (
    authoritative_artifacts_for_phase,
    render_phase_user_prompt,
)
from umbrella.phases.base import PhaseNode
from umbrella.phases.registry import get_registry


def _manifest(phase_id: str):
    repo = Path(__file__).resolve().parents[2]
    return get_registry(repo / "umbrella" / "phases" / "manifests").get(phase_id)


def _empty_recall():
    return SimpleNamespace(always_on=[], hot=[], warm=[])


def test_plan_review_prompt_references_handoff_without_inlining(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps({"run_id": "run-1", "plan": {"notes": "BIG_PLAN_BODY"}}),
        encoding="utf-8",
    )
    (state / "research_summary_latest.json").write_text(
        json.dumps({"run_id": "run-1", "notes": "BIG_RESEARCH_BODY"}),
        encoding="utf-8",
    )

    artifacts = authoritative_artifacts_for_phase(
        manifest_id="plan_review",
        drive_root=drive,
        run_id="run-1",
    )
    prompt = render_phase_user_prompt(
        _manifest("plan_review"),
        _empty_recall(),
        authoritative_artifacts=artifacts,
    )

    assert "BIG_PLAN_BODY" not in prompt
    assert "BIG_RESEARCH_BODY" not in prompt
    assert "READ REQUIRED" in prompt
    assert 'read_file(file_path=".memory/drive/state/phase_plan_submitted_latest.json")' in prompt


def test_research_review_prompt_references_summary_without_inlining(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps({"run_id": "run-2", "notes": "BIG_RESEARCH_BODY"}),
        encoding="utf-8",
    )

    artifacts = authoritative_artifacts_for_phase(
        manifest_id="research_review",
        drive_root=drive,
        run_id="run-2",
    )
    prompt = render_phase_user_prompt(
        _manifest("research_review"),
        _empty_recall(),
        authoritative_artifacts=artifacts,
    )

    assert "BIG_RESEARCH_BODY" not in prompt
    assert "READ REQUIRED" in prompt
    assert 'read_file(file_path=".memory/drive/state/research_summary_latest.json")' in prompt


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
        capability_envelope={
            "phase": "execute",
            "workspace_write": {"allowed": True},
            "shell": {"allowed": True},
            "memory_write": {"allowed_kinds": ["observation"]},
            "verification": {"candidate_workspace_writable": True},
            "runtime_capabilities": {"python": True},
            "active_subtask": {
                "id": "scaffold",
                "proof_contract": active["proof"],
                "oracle_freeze_policy": {"no_test_tampering": False},
            },
        },
        drive_root=drive,
    )
    assert bundle.active_subtask_id == "scaffold"
    assert bundle.active_subtask == active
    assert len(bundle.memory_items) == 1
    assert any(item.role == "phase_envelope" for item in bundle.user_sections)
    assert any(item.id == "current_phase_envelope" for item in bundle.user_sections)
    assert bundle.workspace_inventory is not None
    assert "src/demo/app.py" in bundle.workspace_inventory.missing_declared_files
    overlay = bundle_to_overlay_dict(bundle)
    assert overlay["active_subtask_id"] == "scaffold"
    assert overlay["active_subtask"]["id"] == "scaffold"
    capability_envelope = overlay["capability_envelope"]
    assert capability_envelope["runtime_capabilities"] == {"python": True}
    assert capability_envelope["active_subtask"]["id"] == "scaffold"
    assert capability_envelope["active_subtask"]["proof_contract"] == active["proof"]


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
