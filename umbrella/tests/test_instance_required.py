from pathlib import Path

from umbrella.control_plane import ouroboros_integration as oi


def test_require_instance_allows_legacy_workspace_without_seed_profile(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path
    (repo / "workspaces" / "legacy").mkdir(parents=True)

    monkeypatch.setattr(oi, "_record_baseline", lambda _repo: "base")
    monkeypatch.setattr(oi, "_try_create_instance", lambda *_args, **_kwargs: None)

    submitted: dict[str, object] = {}

    class _Launcher:
        def start(self) -> None:
            return None

        def submit_task(self, task: dict[str, object]) -> str:
            submitted.update(task)
            return str(task["id"])

        def wait_for_result(
            self, task_id: str, timeout: float | None = None
        ) -> dict[str, object]:
            return {"status": "complete", "events": [], "result": "done"}

    def _submit(_repo: Path, task: dict[str, object]):
        submitted.update(task)
        return str(task["id"]), _Launcher()

    monkeypatch.setattr(oi, "_submit_launcher_task", _submit)
    monkeypatch.setattr(oi, "_capture_candidate_safe", lambda **_kwargs: None)
    monkeypatch.setattr(
        oi, "_record_competency_signals", lambda *_args, **_kwargs: None
    )

    result = oi.run_ouroboros_improvement_sync(
        repo_root=repo,
        task_description="do work",
        workspace_id="legacy",
        require_instance=True,
        verify=False,
    )

    assert submitted["workspace_id"] == "legacy"
    assert result["status"] == "incomplete"


def test_require_instance_fails_when_profile_declared_but_instance_missing(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path
    ws = repo / "workspaces" / "profiled"
    ws.mkdir(parents=True)
    (ws / "seed_profile.toml").write_text("[workspace]\n", encoding="utf-8")

    monkeypatch.setattr(oi, "_record_baseline", lambda _repo: "base")
    monkeypatch.setattr(oi, "_try_create_instance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        oi, "_record_competency_signals", lambda *_args, **_kwargs: None
    )

    result = oi.run_ouroboros_improvement_sync(
        repo_root=repo,
        task_description="do work",
        workspace_id="profiled",
        require_instance=True,
        verify=False,
    )

    assert result["status"] == "error"
    assert str(result["error"]).startswith("instance_create_failed")


def test_sync_improvement_runs_internal_verification_remediation_until_pass(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path
    (repo / "workspaces" / "demo").mkdir(parents=True)

    monkeypatch.setattr(oi, "_record_baseline", lambda _repo: "base")
    monkeypatch.setattr(oi, "_try_create_instance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        oi,
        "_collect_changed_files",
        lambda *_args, **_kwargs: ["workspaces/demo/src/app.py"],
    )
    monkeypatch.setattr(oi, "_capture_candidate_safe", lambda **_kwargs: None)
    monkeypatch.setattr(
        oi, "_record_competency_signals", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(oi, "_collect_run_quality_telemetry", lambda **_kwargs: {})

    submitted: list[dict[str, object]] = []

    class _Launcher:
        def __init__(self, task: dict[str, object]) -> None:
            self.task = task

        def wait_for_result(
            self, task_id: str, timeout: float | None = None
        ) -> dict[str, object]:
            # Self-review tasks must return a contract-compliant verdict
            # so the new delivery gate doesn't downgrade us to
            # ``failed_self_review``.
            if int(self.task.get("self_review_attempt") or 0) > 0:
                final_text = (
                    "LGTM verification passed and the run delivers what was asked."
                )
            else:
                final_text = f"done {task_id}"
            return {
                "status": "complete",
                "events": [
                    {"type": "task_metrics", "tool_calls": 1},
                    {"type": "workspace_write_tools", "count": 1},
                    {"type": "send_message", "text": final_text},
                ],
                "result": "done",
            }

    def _submit(_repo: Path, task: dict[str, object]):
        submitted.append(dict(task))
        return str(task["id"]), _Launcher(task)

    verify_calls = {"count": 0}

    def _verify(*_args, **_kwargs):
        verify_calls["count"] += 1
        if verify_calls["count"] == 1:
            return {
                "passed": False,
                "pass_rate": 0.0,
                "summary": "Verification: FAIL",
                "results": [
                    {
                        "name": "pytest:tests",
                        "kind": "shell",
                        "status": "failed",
                        "optional": False,
                    }
                ],
            }
        return {
            "passed": True,
            "skipped": False,
            "pass_rate": 1.0,
            "summary": "Verification: PASS",
            "results": [],
        }

    monkeypatch.setattr(oi, "_submit_launcher_task", _submit)
    monkeypatch.setattr(oi, "_run_workspace_verification", _verify)

    result = oi.run_ouroboros_improvement_sync(
        repo_root=repo,
        task_description="do work",
        workspace_id="demo",
        require_instance=True,
        verify=True,
        verification_remediation_attempts=2,
        task_id="run123",
    )

    assert result["status"] == "verified"
    assert result["verification_remediation_attempts_used"] == 1
    assert len(submitted) == 3
    assert submitted[1]["id"] == "run123"
    assert "Verification Remediation Continuation" in str(submitted[1]["input"])
    assert submitted[2]["id"] == "run123"
    assert submitted[2].get("self_review_attempt") == 1


def test_sync_improvement_reports_exhausted_remediation(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path
    (repo / "workspaces" / "demo").mkdir(parents=True)

    monkeypatch.setattr(oi, "_record_baseline", lambda _repo: "base")
    monkeypatch.setattr(oi, "_try_create_instance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        oi,
        "_collect_changed_files",
        lambda *_args, **_kwargs: ["workspaces/demo/src/app.py"],
    )
    monkeypatch.setattr(oi, "_capture_candidate_safe", lambda **_kwargs: None)
    monkeypatch.setattr(
        oi, "_record_competency_signals", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(oi, "_collect_run_quality_telemetry", lambda **_kwargs: {})

    class _Launcher:
        def wait_for_result(
            self, task_id: str, timeout: float | None = None
        ) -> dict[str, object]:
            return {
                "status": "complete",
                "events": [
                    {"type": "task_metrics", "tool_calls": 1},
                    {"type": "workspace_write_tools", "count": 1},
                ],
                "result": "done",
            }

    submitted: list[dict[str, object]] = []

    def _submit(_repo: Path, task: dict[str, object]):
        submitted.append(dict(task))
        return str(task["id"]), _Launcher()

    def _verify(*_args, **_kwargs):
        return {
            "passed": False,
            "pass_rate": 0.0,
            "summary": "Verification: FAIL\n- pytest failed",
            "results": [
                {
                    "name": "pytest:tests",
                    "kind": "shell",
                    "status": "failed",
                    "optional": False,
                }
            ],
        }

    monkeypatch.setattr(oi, "_submit_launcher_task", _submit)
    monkeypatch.setattr(oi, "_run_workspace_verification", _verify)

    result = oi.run_ouroboros_improvement_sync(
        repo_root=repo,
        task_description="do work",
        workspace_id="demo",
        require_instance=True,
        verify=True,
        verification_remediation_attempts=1,
        task_id="run456",
    )

    assert result["status"] == "failed_verification"
    assert result["verification_remediation_attempts_used"] == 1
    assert len(submitted) == 2
    assert (
        "Verification still failing after 1 remediation attempt"
        in result["final_message"]
    )
    assert result["verification_failure_context_path"]
