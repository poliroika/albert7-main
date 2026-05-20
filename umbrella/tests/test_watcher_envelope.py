from umbrella.permissions.watcher_envelope import get_watcher_envelope

def test_watcher_cannot_edit():
    env = get_watcher_envelope()
    for tool in ["apply_workspace_patch", "shell", "repo_write_commit", "claude_code_edit", "sandbox_self_edit"]:
        result = env.check(tool)
        assert not result, f"Watcher should not be allowed to call {tool}"

def test_watcher_can_search():
    env = get_watcher_envelope()
    assert env.check("palace_search")
    assert env.check("read_drive_log")
    assert env.check("read_terminal_scrollback")

def test_watcher_can_signal():
    env = get_watcher_envelope()
    assert env.check("request_abort_phase")
    assert env.check("request_restart_phase")
    assert env.check("inject_lesson")
