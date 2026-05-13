from pathlib import Path

from umbrella.orchestration.ouroboros_task import (
    polymarket_e2e_task,
    render_retry_prompt,
    render_verification_remediation_prompt,
    render_workspace_prompt,
)


def test_workspace_prompt_requires_long_run_completion_contract(tmp_path: Path):
    prompt = render_workspace_prompt(
        repo_root=tmp_path,
        workspace_id="demo",
        task_text="Build a live public-data system.",
        quality_threshold=0.95,
        include_prior_knowledge=False,
    )

    assert "Execution Flow (strict)" in prompt
    assert "Plan first" in prompt
    assert "Implement fully" in prompt
    assert "Test and fix" in prompt
    assert "Final verification" in prompt
    assert "Finish only on proof" in prompt
    assert "web_search" not in prompt
    assert "Completion Contract" in prompt


def test_retry_prompt_empty_on_first_attempt():
    assert (
        render_retry_prompt(
            attempt=1,
            max_attempts=3,
            previous_status="",
            verification_report=None,
        )
        == ""
    )


def test_retry_prompt_includes_verification_summary(tmp_path: Path):
    retry_context = render_retry_prompt(
        attempt=2,
        max_attempts=3,
        previous_status="failed_verification",
        verification_report={
            "passed": False,
            "pass_rate": 0.5,
            "summary": "Verification: **FAIL**\n- [required] `pytest test_smoke.py` -> failed exit=1",
        },
        previous_final_message="I think this is done now.",
    )

    assert "Previous Verification Failure" in retry_context
    assert "Strict Retry Mode" in retry_context
    assert "Do not start new features or side quests" in retry_context
    assert "attempt 1/3" in retry_context
    assert "pytest test_smoke.py" in retry_context
    assert "I think this is done now." in retry_context

    rendered = render_workspace_prompt(
        repo_root=tmp_path,
        workspace_id="demo",
        task_text="Build system.",
        quality_threshold=0.95,
        retry_context=retry_context,
        include_prior_knowledge=False,
    )
    assert "Previous Verification Failure" in rendered


def test_retry_prompt_calls_out_toml_parse_repair():
    retry_context = render_retry_prompt(
        attempt=2,
        max_attempts=21,
        previous_status="failed_verification",
        verification_report={
            "passed": False,
            "repairable": True,
            "summary": (
                "Verification spec is invalid and must be repaired before checks can run.\n"
                "workspace.toml: Invalid TOML: Invalid hex value (at line 17, column 66)"
            ),
        },
    )

    assert "Mandatory TOML Repair" in retry_context
    assert "C:/Users/..." in retry_context
    assert "C:\\\\Users\\\\..." in retry_context
    assert 'kind = "file_exists"' in retry_context


def test_verification_remediation_prompt_is_focused_continuation():
    prompt = render_verification_remediation_prompt(
        original_task="Build the workspace deliverable.",
        verification_report={
            "summary": "Verification: **FAIL**",
            "results": [
                {
                    "name": "pytest:tests",
                    "kind": "shell",
                    "status": "failed",
                    "summary": "pytest failed",
                    "stdout_tail": "ModuleNotFoundError: No module named 'src'",
                    "optional": False,
                }
            ],
        },
        attempt=1,
        max_attempts=3,
        failure_context_path="workspaces/demo/.memory/drive/state/verification_failure_context.json",
    )

    # Prompt should make it crystal-clear this is the SAME run, not
    # a restart, and instruct the agent to fix the failing checks
    # only (no broad refactors / new features).
    assert "SAME RUN" in prompt or "Same Run" in prompt
    assert (
        "Fix only the failing checks" in prompt
        or "Fix the failing verification steps only" in prompt
    )
    assert "pytest:tests" in prompt
    assert "ModuleNotFoundError" in prompt
    assert "verification_failure_context.json" in prompt


def test_retry_prompt_stays_verification_only_even_if_critic_payload_is_present():
    retry_context = render_retry_prompt(
        attempt=2,
        max_attempts=3,
        previous_status="failed_verification",
        verification_report={
            "passed": True,
            "summary": "Verification: **PASS** (5/5 required steps passed)",
        },
        critic_review={
            "verdict": "fail",
            "rationale": "Changed files contain mock or placeholder scaffold.",
            "risks": ["mock_scaffold"],
            "missing_checks": [],
            "mock_hits": ["workspaces/demo/test.py: numbered news placeholder"],
        },
        previous_final_message="done",
    )

    assert "Previous Verification Failure" in retry_context
    assert "Critic Gate Failed" not in retry_context
    assert "mock_scaffold" not in retry_context
    assert "numbered news placeholder" not in retry_context


def test_polymarket_task_is_live_first_not_mock_only():
    task = polymarket_e2e_task()

    assert "live data collection" in task
    assert "Do not create or use mock data, stubs, or fallback fake markets" in task
    assert "Do not finish with a mock-only implementation" in task
