from umbrella.utils.tool_logs import is_effective_write_tool_log_row


def test_commit_disabled_without_write_is_not_effective():
    assert not is_effective_write_tool_log_row(
        {"result_preview": "GIT_COMMIT_DISABLED_BY_POLICY: local commits are disabled."}
    )


def test_commit_disabled_after_write_is_effective():
    assert is_effective_write_tool_log_row(
        {
            "result_preview": (
                "OK: wrote workspaces/demo/app.py; local git commit skipped by policy. "
                "GIT_COMMIT_DISABLED_BY_POLICY: local commits are disabled."
            )
        }
    )


def test_json_blocked_write_tool_result_is_not_effective():
    assert not is_effective_write_tool_log_row(
        {"result_preview": '{"status": "blocked", "reason": "read_before_patch_required"}'}
    )


def test_json_applied_write_tool_result_is_effective():
    assert is_effective_write_tool_log_row(
        {"result_preview": '{"status": "applied", "applied": ["src/app.py"]}'}
    )
