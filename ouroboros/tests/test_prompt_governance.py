import json
import pathlib
import tempfile

from umbrella.orchestration.context_overlays import build_prompt_governance_overlay
from ouroboros.tools.registry import ToolRegistry


REPO = pathlib.Path(__file__).resolve().parents[2]


def test_prompt_governance_section_mentions_core_prompt_surfaces():
    with tempfile.TemporaryDirectory() as tmp:
        section = build_prompt_governance_overlay(REPO)

    assert "Prompt Stack Governance" in section
    assert "ouroboros_system_prompt" in section
    assert "ouroboros_context_assembly" in section
    assert "ouroboros_task_planner_prompts" in section
    assert "umbrella_delivery_critic" in section
    assert "umbrella_workspace_task_wrapper" in section


def test_list_prompt_surfaces_tool_returns_formal_stack():
    with tempfile.TemporaryDirectory() as tmp:
        registry = ToolRegistry(repo_dir=REPO, drive_root=pathlib.Path(tmp))
        payload = json.loads(registry.execute("list_prompt_surfaces", {}))

    surface_ids = {item["id"] for item in payload}
    assert "ouroboros_system_prompt" in surface_ids
    assert "ouroboros_bible" in surface_ids
    assert "ouroboros_task_planner_prompts" in surface_ids
    assert "umbrella_delivery_critic" in surface_ids
    assert "umbrella_workspace_task_wrapper" in surface_ids


def test_prompt_patch_proposal_roundtrip_with_checkpoint_resolution():
    context_surface_path = REPO / "ouroboros" / "ouroboros" / "context.py"
    current_text = context_surface_path.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        registry = ToolRegistry(repo_dir=REPO, drive_root=pathlib.Path(tmp))
        proposal_payload = json.loads(
            registry.execute(
                "propose_prompt_patch",
                {
                    "surface_id": "ouroboros_context_assembly",
                    "rationale": "Tighten manager context assembly after prompt-routing drift",
                    "expected_behavioral_effect": "Reduce noisy context and make prompt rewrites explicit",
                    "evidence": [
                        "Repeated prompt routing drift across manager iterations"
                    ],
                    "proposed_content": current_text + "\n# prompt governance test\n",
                },
            )
        )

        assert proposal_payload["proposal_id"]
        assert proposal_payload["manager_checkpoint_id"]
        assert proposal_payload["human_checkpoint_id"]

        saved_proposal = json.loads(
            registry.execute(
                "get_prompt_patch_proposal",
                {"proposal_id": proposal_payload["proposal_id"]},
            )
        )
        assert saved_proposal["surface"]["id"] == "ouroboros_context_assembly"
        assert saved_proposal["requires_human_checkpoint"] is True

        decision_payload = json.loads(
            registry.execute(
                "resolve_prompt_checkpoint",
                {
                    "checkpoint_id": proposal_payload["human_checkpoint_id"],
                    "approved": True,
                    "response": "Approved for restart-based rollout",
                },
            )
        )

    assert decision_payload["decision"]["approved"] is True
    assert decision_payload["resume"]["resumed"] is True
