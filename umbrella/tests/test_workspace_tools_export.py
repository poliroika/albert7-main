"""Workspace tool surface exports used by ouroboros registry."""


def test_replace_workspace_file_exported_from_workspace_tools() -> None:
    from umbrella.deep_agent_tools import workspace_tools as wt

    assert hasattr(wt, "replace_workspace_file")
