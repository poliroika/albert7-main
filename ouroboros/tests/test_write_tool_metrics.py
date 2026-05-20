import unittest

from ouroboros.agent import _is_effective_write_tool_call


class TestWriteToolMetrics(unittest.TestCase):
    def test_noop_commit_does_not_count_as_effective_write(self):
        write_tools = frozenset({"commit_workspace_changes", "repo_write_commit"})

        self.assertFalse(
            _is_effective_write_tool_call(
                {
                    "tool": "commit_workspace_changes",
                    "result": "GIT_NO_CHANGES: nothing to commit for this workspace.",
                    "is_error": False,
                },
                write_tools,
            )
        )
        self.assertFalse(
            _is_effective_write_tool_call(
                {
                    "tool": "repo_write_commit",
                    "result": "WARNING: workspace commit error",
                    "is_error": False,
                },
                write_tools,
            )
        )
        self.assertTrue(
            _is_effective_write_tool_call(
                {
                    "tool": "commit_workspace_changes",
                    "result": '{"status": "committed_locally"}',
                    "is_error": False,
                },
                write_tools,
            )
        )
        self.assertFalse(
            _is_effective_write_tool_call(
                {
                    "tool": "repo_write_commit",
                    "result": "GIT_COMMIT_DISABLED_BY_POLICY: local commits are disabled.",
                    "is_error": False,
                },
                write_tools,
            )
        )
        self.assertTrue(
            _is_effective_write_tool_call(
                {
                    "tool": "repo_write_commit",
                    "result": "OK: wrote workspaces/demo/app.py; local git commit skipped by policy.",
                    "is_error": False,
                },
                write_tools,
            )
        )


if __name__ == "__main__":
    unittest.main()
