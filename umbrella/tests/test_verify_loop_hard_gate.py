from umbrella.orchestrator.verify_loop import run_verify_loop


def test_missing_verify_script_is_failure_not_success(tmp_path) -> None:
    drive_root = tmp_path / ".memory" / "drive"
    drive_root.mkdir(parents=True)

    result = run_verify_loop(
        run_id="run_1",
        workspace_id="demo",
        drive_root=drive_root,
        max_attempts=1,
    )

    assert result.ok is False
    assert result.data["skipped"] is False
    assert result.errors[0].code == "VERIFY_FAILED"
