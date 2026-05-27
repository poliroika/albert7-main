from pathlib import Path

from umbrella.contracts.capability_declaration import persist_capability_declaration
from umbrella.contracts.platform_context import overlay_hints_from_declaration


def test_architecture_author_skill_does_not_enable_gmas_policy(tmp_path: Path) -> None:
    drive = tmp_path / ".memory" / "drive"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persist_capability_declaration(
        drive,
        {
            "status": "submitted",
            "recommended_skills": ["architecture-author"],
            "capabilities": {"python": {"available": True}},
        },
    )

    hints = overlay_hints_from_declaration(drive, workspace)

    assert hints["recommended_skills"] == ["architecture-author"]
    assert hints["detected_domains"] == []


def test_gmas_skill_still_enables_gmas_policy(tmp_path: Path) -> None:
    drive = tmp_path / ".memory" / "drive"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persist_capability_declaration(
        drive,
        {
            "status": "submitted",
            "recommended_skills": ["gmas-overview"],
            "capabilities": {"python": {"available": True}},
        },
    )

    hints = overlay_hints_from_declaration(drive, workspace)

    assert hints["detected_domains"] == ["multi_agent_gmas"]
