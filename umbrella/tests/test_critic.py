from pathlib import Path

from umbrella.control_plane.critic import critic_review


def test_critic_fails_shallow_verification(tmp_path: Path) -> None:
    verdict = critic_review(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task1",
        task_description="do work",
        changed_files=["app.py"],
        verification_report={
            "passed": True,
            "results": [{"kind": "import_check", "status": "passed"}],
        },
        final_message="done",
    )

    assert verdict["verdict"] == "fail"
    assert "shallow_verification" in verdict["risks"]


def test_critic_passes_behavioral_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_ENABLE_CRITIC_LLM", "0")
    verdict = critic_review(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task1",
        task_description="do work",
        changed_files=["app.py"],
        verification_report={
            "passed": True,
            "results": [
                {
                    "kind": "behavioral_http",
                    "status": "passed",
                    "request_payload_count": 2,
                }
            ],
        },
        final_message="done",
    )

    assert verdict["verdict"] == "pass"


def test_critic_fails_without_diverse_inputs(tmp_path: Path) -> None:
    verdict = critic_review(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task1",
        task_description="do work",
        changed_files=["app.py"],
        verification_report={
            "passed": True,
            "results": [{"kind": "behavioral_http", "status": "passed"}],
        },
        final_message="done",
    )

    assert verdict["verdict"] == "fail"
    assert "diverse_inputs" in verdict["missing_checks"]


def test_critic_fails_on_mock_scaffold_in_changed_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "pipeline.py").write_text(
        "def build():\n    return {'cards': [{'title': 'News 1', 'point': 'Point 1'}]}\n",
        encoding="utf-8",
    )

    verdict = critic_review(
        repo_root=tmp_path,
        workspace_id="demo",
        task_id="task1",
        task_description="do work",
        changed_files=["workspaces/demo/pipeline.py"],
        verification_report={
            "passed": True,
            "results": [
                {
                    "kind": "behavioral_http",
                    "status": "passed",
                    "request_payload_count": 2,
                }
            ],
        },
        final_message="done",
    )

    assert verdict["verdict"] == "fail"
    assert "mock_scaffold" in verdict["risks"]
