from umbrella.orchestration.status import write_status


def test_running_status_clears_stale_result_fields(tmp_path):
    write_status(
        tmp_path,
        active=False,
        status="complete",
        task_id="old_task",
        result={"status": "complete"},
        error="old error",
    )

    payload = write_status(tmp_path, active=True, status="running", workspace_id="demo")

    assert payload["status"] == "running"
    assert "result" not in payload
    assert "task_id" not in payload
    assert "error" not in payload
