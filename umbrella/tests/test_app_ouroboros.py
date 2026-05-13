from pathlib import Path
from typing import Any

import pytest

from umbrella.app_ouroboros import (
    _apply_max_rounds_env,
    _clear_stop_requests,
    _persist_critic_artifact,
    _persist_verification_artifact,
    _resolve_launch_task,
    _summarize_result,
    main,
)


def test_summarize_result_marks_truncated_final_message():
    summary = _summarize_result({"status": "complete", "final_message": "x" * 4100})

    assert summary["final_message"].endswith("[truncated]")
    assert len(summary["final_message"]) < 4050


def test_summarize_result_preserves_verification_block():
    summary = _summarize_result(
        {
            "status": "verified",
            "verification_report": {
                "passed": True,
                "pass_rate": 1.0,
                "results": [
                    {
                        "name": "pytest",
                        "kind": "shell",
                        "status": "passed",
                        "exit_code": 0,
                    }
                ],
            },
        }
    )

    assert summary["verification"]["passed"] is True
    assert summary["verification"]["results"][0]["name"] == "pytest"


def test_resolve_launch_task_missing_task_main_is_typed(tmp_path: Path):
    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)

    class Args:
        polymarket_e2e = False
        task_file = None
        task = None

    resolved = _resolve_launch_task(Args(), workspace)

    assert resolved.task_missing is True
    assert resolved.missing_status == "missing_task_main"
    assert resolved.task_text == ""


def test_persist_verification_artifact_writes_json(tmp_path: Path):
    result = {
        "task_id": "sync_improve_abc",
        "verification_report": {"passed": True, "results": []},
    }

    _persist_verification_artifact(tmp_path, result)

    path = (
        tmp_path
        / ".umbrella"
        / "ouroboros_drive"
        / "task_results"
        / "sync_improve_abc.verification.json"
    )
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "passed" in text


def test_persist_critic_artifact_writes_json(tmp_path: Path):
    result = {
        "task_id": "sync_improve_abc",
        "workspace_id": "demo",
        "critic_review": {"verdict": "fail", "rationale": "mock scaffold"},
    }

    _persist_critic_artifact(tmp_path, result)

    path = (
        tmp_path
        / "workspaces"
        / "demo"
        / ".memory"
        / "drive"
        / "task_results"
        / "sync_improve_abc.critic.json"
    )
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "mock scaffold" in text


class _ScriptedRunner:
    """Helper that scripts a sequence of run_ouroboros_improvement_sync outcomes."""

    def __init__(self, outcomes: list[dict[str, Any]]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("Runner invoked more times than scripted outcomes")
        return self.outcomes.pop(0)


@pytest.fixture
def empty_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspaces" / "demo"
    ws.mkdir(parents=True)
    (ws / "TASK_MAIN.md").write_text("do stuff", encoding="utf-8")
    return ws


def _stub_live_mode(monkeypatch, live: bool = True):
    import umbrella.app_ouroboros as mod

    monkeypatch.setattr(
        mod,
        "_resolve_app_live_mode",
        lambda *a, **kw: (live, "stubbed"),
    )


def test_retry_loop_stops_on_verified(
    monkeypatch, empty_workspace: Path, tmp_path: Path
):
    repo_root = tmp_path
    _stub_live_mode(monkeypatch, live=False)

    runner = _ScriptedRunner(
        [
            {
                "status": "failed_verification",
                "task_id": "t1",
                "final_message": "first try",
                "verification_report": {
                    "passed": False,
                    "summary": "- [required] pytest -> failed exit=1",
                    "results": [],
                },
                "critic_review": {
                    "verdict": "fail",
                    "rationale": "Changed files contain mock or placeholder scaffold.",
                    "risks": ["mock_scaffold"],
                    "mock_hits": ["workspaces/demo/test.py: numbered news placeholder"],
                },
            },
            {
                "status": "verified",
                "task_id": "t2",
                "final_message": "ok",
                "verification_report": {"passed": True, "results": []},
            },
        ]
    )
    import umbrella.app_ouroboros as mod

    monkeypatch.setattr(mod, "run_ouroboros_improvement_sync", runner)

    exit_code = main(
        [
            str(empty_workspace),
            "--repo-root",
            str(repo_root),
            "--no-dashboard",
            "--max-verify-retries",
            "3",
        ]
    )

    assert exit_code == 0
    assert len(runner.calls) == 2
    retry_prompt = runner.calls[1]["task_description"]
    assert "Previous Verification Failure" in retry_prompt
    assert "pytest -> failed" in retry_prompt


def test_retry_loop_exhausts_attempts(
    monkeypatch, empty_workspace: Path, tmp_path: Path
):
    repo_root = tmp_path
    _stub_live_mode(monkeypatch, live=False)

    outcomes = [
        {
            "status": "failed_verification",
            "task_id": f"t{i}",
            "final_message": f"attempt {i}",
            "verification_report": {
                "passed": False,
                "summary": "fail",
                "results": [],
            },
        }
        for i in range(1, 4)
    ]
    runner = _ScriptedRunner(outcomes)

    import umbrella.app_ouroboros as mod

    monkeypatch.setattr(mod, "run_ouroboros_improvement_sync", runner)

    exit_code = main(
        [
            str(empty_workspace),
            "--repo-root",
            str(repo_root),
            "--no-dashboard",
            "--max-verify-retries",
            "2",
        ]
    )

    assert exit_code == 1
    # Retry loop now stops early when the same failed-verification signature
    # repeats, to avoid burning attempts on identical outcomes.
    assert len(runner.calls) == 2


def test_no_verify_flag_disables_verification(
    monkeypatch, empty_workspace: Path, tmp_path: Path
):
    repo_root = tmp_path
    _stub_live_mode(monkeypatch, live=False)

    runner = _ScriptedRunner(
        [
            {
                "status": "complete",
                "task_id": "t1",
                "final_message": "ok",
            }
        ]
    )
    import umbrella.app_ouroboros as mod

    monkeypatch.setattr(mod, "run_ouroboros_improvement_sync", runner)

    exit_code = main(
        [
            str(empty_workspace),
            "--repo-root",
            str(repo_root),
            "--no-dashboard",
            "--no-verify",
        ]
    )

    assert exit_code == 0
    assert len(runner.calls) == 1
    assert runner.calls[0]["verify"] is False


def test_apply_max_rounds_env_unlimited(monkeypatch):
    """``--max-rounds 0`` (or any non-positive int) maps to
    ``OUROBOROS_MAX_ROUNDS=0`` which the loop interprets as no cap. This
    test guards the contract between Umbrella CLI ("no limits") and the
    Ouroboros internal round gate.
    """
    monkeypatch.delenv("OUROBOROS_MAX_ROUNDS", raising=False)
    _apply_max_rounds_env(0)
    import os

    assert os.environ.get("OUROBOROS_MAX_ROUNDS") == "0"

    monkeypatch.delenv("OUROBOROS_MAX_ROUNDS", raising=False)
    _apply_max_rounds_env(-5)
    assert os.environ.get("OUROBOROS_MAX_ROUNDS") == "0"


def test_apply_max_rounds_env_explicit(monkeypatch):
    monkeypatch.delenv("OUROBOROS_MAX_ROUNDS", raising=False)
    _apply_max_rounds_env(750)
    import os

    assert os.environ.get("OUROBOROS_MAX_ROUNDS") == "750"


def test_apply_max_rounds_env_none_preserves_existing(monkeypatch):
    """When the user does not pass ``--max-rounds``, we must not clobber
    whatever ``OUROBOROS_MAX_ROUNDS`` is already set to (or the Ouroboros
    default of 200 if it is unset)."""
    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "123")
    _apply_max_rounds_env(None)
    import os

    assert os.environ.get("OUROBOROS_MAX_ROUNDS") == "123"


def test_clear_stop_requests_removes_dashboard_stop_files(tmp_path: Path):
    paths = [
        tmp_path / ".umbrella" / "launcher" / "stop_requested.json",
        tmp_path / ".umbrella" / "ouroboros_drive" / "state" / "stop_requested.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    _clear_stop_requests(tmp_path)

    assert all(not path.exists() for path in paths)


def test_verify_enabled_but_skipped_exits_nonzero(
    monkeypatch, empty_workspace: Path, tmp_path: Path
):
    """When verification is ON but the workspace has no spec / steps,
    CLI must fail fast (exit 1) instead of treating it as success.
    """
    repo_root = tmp_path
    _stub_live_mode(monkeypatch, live=False)

    runner = _ScriptedRunner(
        [
            {
                "status": "complete",
                "task_id": "t1",
                "final_message": "ok, but no verify spec",
                "verification_skipped": True,
            }
        ]
    )
    import umbrella.app_ouroboros as mod

    monkeypatch.setattr(mod, "run_ouroboros_improvement_sync", runner)

    exit_code = main(
        [
            str(empty_workspace),
            "--repo-root",
            str(repo_root),
            "--no-dashboard",
        ]
    )

    assert exit_code == 1
    assert len(runner.calls) == 1
    assert runner.calls[0]["verify"] is True
