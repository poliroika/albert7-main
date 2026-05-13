from pathlib import Path

from umbrella.control_plane.code_improver import (
    CodeImprovement,
    _build_improvement_context,
    apply_improvements,
)


def test_build_improvement_context_lists_tools_configs_code_and_errors(
    tmp_path: Path,
) -> None:
    instance_path = tmp_path / "world_prediction_instance"
    (instance_path / "agents").mkdir(parents=True)
    (instance_path / "prompts").mkdir(parents=True)
    (instance_path / "tools").mkdir(parents=True)
    (instance_path / "experiments").mkdir(parents=True)
    (instance_path / "interface").mkdir(parents=True)

    (instance_path / "workspace.toml").write_text(
        (
            'workspace_id = "world_prediction_instance"\n'
            'tools_allowlist_file = "tools/allowlist.toml"\n'
            'experiments_dir = "experiments"\n'
            'agents_dir = "agents"\n'
            'prompts_dir = "prompts"\n'
            'mutable_paths = ["agents", "prompts", "tools", "experiments", "interface", "workspace.toml"]\n'
        ),
        encoding="utf-8",
    )
    (instance_path / "agents" / "delivery_agent.toml").write_text(
        'max_tokens = 2000\ntools = ["save_stage_note"]\n',
        encoding="utf-8",
    )
    (instance_path / "prompts" / "delivery_agent.md").write_text(
        "Prompt text\n", encoding="utf-8"
    )
    (instance_path / "tools" / "allowlist.toml").write_text(
        "[allowed_tools]\nweb_search = true\n", encoding="utf-8"
    )
    (instance_path / "experiments" / "run_pipeline.py").write_text(
        "import missing_module\n", encoding="utf-8"
    )
    (instance_path / "interface" / "app.py").write_text(
        "print('ui hook')\n", encoding="utf-8"
    )

    context_text = _build_improvement_context(
        {
            "task_id": "task_errors",
            "instance_path": str(instance_path),
            "eval": {
                "overall_score": 0.42,
                "task_success": "partial",
                "output_quality": "fair",
                "manager_level_issues": ["delivery output too short"],
            },
            "inspection": {
                "manifest": {
                    "status": "failed",
                    "errors": ["ModuleNotFoundError: missing_module"],
                    "warnings": ["tool budget too low"],
                    "final_answer": "",
                },
                "log_summary": {
                    "status": "available",
                    "error_count": 2,
                    "warning_count": 1,
                    "tail": [
                        "Starting run",
                        "ModuleNotFoundError: missing_module",
                    ],
                },
                "error_signatures": ["ModuleNotFoundError", "tool_not_found"],
                "raw_tail": [
                    "Traceback (most recent call last):",
                    "ModuleNotFoundError: missing_module",
                ],
            },
        },
        tmp_path,
    )

    assert "tools/allowlist.toml" in context_text
    assert "workspace.toml" in context_text
    assert "agents/delivery_agent.toml" in context_text
    assert "experiments/run_pipeline.py" in context_text
    assert "interface/app.py" in context_text
    assert "ModuleNotFoundError" in context_text
    assert "tool_not_found" in context_text


def test_apply_improvements_updates_agent_tools_workspace_config_and_code(
    tmp_path: Path,
) -> None:
    instance_path = tmp_path / "agent_research_instance"
    (instance_path / "agents").mkdir(parents=True)
    (instance_path / "tools").mkdir(parents=True)
    (instance_path / "experiments").mkdir(parents=True)

    agent_path = instance_path / "agents" / "delivery_agent.toml"
    tools_path = instance_path / "tools" / "allowlist.toml"
    workspace_toml_path = instance_path / "workspace.toml"
    pipeline_path = instance_path / "experiments" / "run_pipeline.py"

    agent_path.write_text(
        'tools = ["save_stage_note"]\nmax_tokens = 2000\ntemperature = 0.1\n',
        encoding="utf-8",
    )
    tools_path.write_text("[allowed_tools]\nweb_search = true\n", encoding="utf-8")
    workspace_toml_path.write_text(
        'workspace_id = "agent_research_instance"\n', encoding="utf-8"
    )
    pipeline_path.write_text("import missing_module\n", encoding="utf-8")

    summary = apply_improvements(
        [
            CodeImprovement(
                file_path="agents/delivery_agent.toml",
                original_content=agent_path.read_text(encoding="utf-8"),
                improved_content='tools = ["save_stage_note", "search_workspace_context"]\nmax_tokens = 8000\ntemperature = 0.2\n',
                description="Increase budget and add workspace search tool",
                change_type="agent_config",
            ),
            CodeImprovement(
                file_path="tools/allowlist.toml",
                original_content=tools_path.read_text(encoding="utf-8"),
                improved_content="[allowed_tools]\nweb_search = true\nsearch_workspace_context = true\n",
                description="Allow workspace-context skill",
                change_type="tool_config",
            ),
            CodeImprovement(
                file_path="workspace.toml",
                original_content=workspace_toml_path.read_text(encoding="utf-8"),
                improved_content='workspace_id = "agent_research_instance"\nnotes = "Self-improved runtime configuration"\n',
                description="Update workspace runtime config",
                change_type="workspace_config",
            ),
            CodeImprovement(
                file_path="experiments/run_pipeline.py",
                original_content=pipeline_path.read_text(encoding="utf-8"),
                improved_content="import math\nprint('pipeline fixed')\n",
                description="Fix missing import error in pipeline",
                change_type="code_fix",
            ),
        ],
        instance_path,
    )

    assert summary["applied_count"] == 4
    assert summary["failed_count"] == 0
    assert "search_workspace_context" in agent_path.read_text(encoding="utf-8")
    assert "max_tokens = 8000" in agent_path.read_text(encoding="utf-8")
    assert "search_workspace_context = true" in tools_path.read_text(encoding="utf-8")
    assert "Self-improved runtime configuration" in workspace_toml_path.read_text(
        encoding="utf-8"
    )
    assert "pipeline fixed" in pipeline_path.read_text(encoding="utf-8")
