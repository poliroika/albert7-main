from pathlib import Path

from umbrella.verification.diff_policy import (
    run_diff_policy_guard,
    scan_unified_diff,
    scan_workspace_files,
)
from umbrella.verification.models import VerificationStatus


def test_scan_unified_diff_flags_deleted_tests_and_weak_asserts() -> None:
    diff = """diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_real():
-    assert compute(2) == 4
diff --git a/tests/test_stub.py b/tests/test_stub.py
--- /dev/null
+++ b/tests/test_stub.py
@@ -0,0 +1,2 @@
+def test_stub():
+    assert True
"""
    codes = {issue.code for issue in scan_unified_diff(diff)}
    assert "test_deleted" in codes
    assert "assert_true" in codes


def test_scan_workspace_files_flags_policy_and_shell_bypass(tmp_path: Path) -> None:
    (tmp_path / "workspace.toml").write_text("[verification]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cmd.py").write_text(
        "import subprocess\n"
        "def test_cmd():\n"
        "    subprocess.run(['python', '-c', 'pass'], check=False)\n",
        encoding="utf-8",
    )

    codes = {
        issue.code
        for issue in scan_workspace_files(
            tmp_path, changed_files=["workspace.toml", "tests/test_cmd.py"]
        )
    }

    assert "verifier_policy_changed" in codes
    assert "subprocess_check_false" in codes


def test_diff_policy_guard_fails_tamper_patterns(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "import pytest\n"
        "def test_skip():\n"
        "    pytest.skip('later')\n",
        encoding="utf-8",
    )

    result = run_diff_policy_guard(tmp_path, changed_files=["tests/test_app.py"])

    assert result.status == VerificationStatus.FAILED
    assert "pytest_skip_or_xfail" in result.summary
