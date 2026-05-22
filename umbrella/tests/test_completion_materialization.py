from pathlib import Path

from umbrella.contracts import (
    CompletionContract,
    CompletedClaim,
    EvidenceRef,
    validate_completion_materialization,
    validate_done_subtasks_materialized,
)
from umbrella.phases.base import SubtaskCard


def test_mark_subtask_complete_rejects_missing_declared_created_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    completion = CompletionContract(
        subtask_id="scaffold",
        status="done",
        changed_files=("src/pkg/app.py",),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="done",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )
    active = {
        "id": "scaffold",
        "files_to_create": ["src/pkg/app.py"],
        "files_to_change": [],
        "files_affected": [],
    }
    issues = validate_completion_materialization(
        completion,
        active_subtask=active,
        workspace_root=str(workspace),
    )
    assert any(issue.code == "subtask_materialization_missing" for issue in issues)


def test_mark_subtask_complete_rejects_done_with_typed_blockers() -> None:
    completion = CompletionContract(
        subtask_id="scaffold",
        status="done",
        changed_files=("src/pkg/app.py",),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="done",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )
    issues = validate_completion_materialization(
        completion,
        active_subtask={"id": "scaffold", "files_to_create": []},
        workspace_root="",
        raw_completion={"blockers": ["missing proof"]},
    )
    assert any(issue.code == "completion_blocked_not_done" for issue in issues)


def test_runner_blocks_next_subtask_when_previous_done_files_missing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspaces" / "civilization"
    workspace.mkdir(parents=True)
    issues = validate_done_subtasks_materialized(
        subtasks=[
            SubtaskCard(
                id="scaffold",
                title="Scaffold",
                goal="Create app module",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
                files_to_create=["src/pkg/app.py"],
            )
        ],
        workspace_root=str(workspace),
    )
    assert issues
    assert issues[0].code == "subtask_materialization_missing"


def test_completion_accepts_materialized_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    target = workspace / "src" / "pkg"
    target.mkdir(parents=True)
    (target / "app.py").write_text("print('ok')\n", encoding="utf-8")
    completion = CompletionContract(
        subtask_id="scaffold",
        status="done",
        changed_files=("src/pkg/app.py",),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="done",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )
    active = {
        "id": "scaffold",
        "files_to_create": ["src/pkg/app.py"],
        "files_to_change": [],
        "files_affected": [],
    }
    issues = validate_completion_materialization(
        completion,
        active_subtask=active,
        workspace_root=str(workspace),
    )
    materialization = [
        issue for issue in issues if issue.code == "subtask_materialization_missing"
    ]
    assert not materialization


def test_mark_subtask_complete_rejects_blocker_language_in_notes() -> None:
    completion = CompletionContract(
        subtask_id="scaffold",
        status="done",
        changed_files=("src/pkg/app.py",),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="INFRASTRUCTURE BLOCKER: verification blocked; missing source files",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )
    issues = validate_completion_materialization(
        completion,
        active_subtask={"id": "scaffold", "files_to_create": ["src/pkg/app.py"]},
        workspace_root="",
    )
    assert any(issue.code == "completion_blocked_not_done" for issue in issues)


def test_mark_subtask_complete_rejects_out_of_scope_changed_files() -> None:
    completion = CompletionContract(
        subtask_id="scaffold",
        status="done",
        changed_files=("src/pkg/app.py", "frontend/src/App.tsx"),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="done",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )
    issues = validate_completion_materialization(
        completion,
        active_subtask={
            "id": "scaffold",
            "files_to_create": ["src/pkg/app.py"],
            "files_to_change": [],
        },
        workspace_root="",
    )
    assert any(
        issue.code == "scope_mismatch"
        and "frontend/src/App.tsx" in issue.message
        for issue in issues
    )


def test_mark_subtask_complete_accepts_declared_deleted_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    completion = CompletionContract(
        subtask_id="cleanup",
        status="done",
        changed_files=("src/pkg/obsolete.py",),
        deleted_files=("src/pkg/obsolete.py",),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="removed obsolete module",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )

    issues = validate_completion_materialization(
        completion,
        active_subtask={
            "id": "cleanup",
            "files_to_change": ["src/pkg/obsolete.py"],
        },
        workspace_root=str(workspace),
    )

    assert not [
        issue for issue in issues if issue.code == "subtask_materialization_missing"
    ]


def test_mark_subtask_complete_rejects_declared_deleted_file_still_present(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    target = workspace / "src" / "pkg"
    target.mkdir(parents=True)
    (target / "obsolete.py").write_text("print('old')\n", encoding="utf-8")
    completion = CompletionContract(
        subtask_id="cleanup",
        status="done",
        changed_files=("src/pkg/obsolete.py",),
        deleted_files=("src/pkg/obsolete.py",),
        completed_claims=(
            CompletedClaim(
                claim_id="claim-1",
                text="removed obsolete module",
                proof_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
            ),
        ),
        evidence_refs=(EvidenceRef(ref_type="ledger_event", ref_id="1"),),
    )

    issues = validate_completion_materialization(
        completion,
        active_subtask={
            "id": "cleanup",
            "files_to_change": ["src/pkg/obsolete.py"],
        },
        workspace_root=str(workspace),
    )

    assert any(issue.code == "subtask_materialization_present" for issue in issues)
