"""Hindsight integration points stay gated and proposal-only."""

from unittest.mock import MagicMock

from umbrella.enforcement.ledger import append_supervisor_ledger_event
from umbrella.memory.backends.base import ReflectionCandidate
from umbrella.memory.hindsight.candidates import (
    write_hindsight_candidates_as_pending_proposals,
)
from umbrella.memory.proactive.phase_hooks import process_reflexion_bkb_patch
from umbrella.memory.proactive.promotion import ProposedBkbPatch, accept_bkb_patch


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True)
    return tmp_path


def _evidence(repo):
    event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id="ws1",
        actor="verifier",
        phase="verify",
        tool="pytest",
        result={"passed": True},
    )
    return {
        "ref_type": "ledger_event",
        "ref_id": event.event_id,
        "hash": event.event_hash,
        "produced_by": "verifier",
    }


def test_accept_bkb_patch_dual_writes_to_hindsight_after_canonical(
    tmp_path, monkeypatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "dual")
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_ENABLED", "1")
    retained: list[object] = []

    def capture(**kwargs):
        retained.append(kwargs["lesson"])
        return {"ok": True}

    monkeypatch.setattr(
        "umbrella.memory.proactive.promotion.retain_hindsight_lesson_best_effort",
        capture,
    )
    patch = ProposedBkbPatch(
        patch_id="patch1",
        workspace_id="ws1",
        run_id="run1",
        phase_id="reflexion",
        actor="supervisor",
        source_evidence=[_evidence(repo)],
        rules=[
            {
                "id": "rule1",
                "title": "Use typed evidence",
                "scope": "workspace",
                "type": "behavior",
                "rule": {"behavior": "Require typed evidence for durable memory."},
            }
        ],
    )
    result = accept_bkb_patch(repo, patch, target="workspace")
    assert result["accepted"] is True
    assert retained
    lesson = retained[0]
    assert getattr(lesson, "kind") == "accepted_bkb_rule"
    assert getattr(lesson, "evidence_refs")
    assert (repo / "workspaces" / "ws1" / ".memory" / "core" / "bkb.yaml").is_file()


def test_hindsight_candidate_written_to_queue_not_bkb(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    drive = repo / "workspaces" / "ws1" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    candidate = ReflectionCandidate(
        candidate_id="c1",
        kind="anti_pattern",
        title="Do not retain drafts",
        content="Draft notes must stay out of durable archive.",
        confidence=0.8,
        scope="workspace",
        evidence_refs=[_evidence(repo)],
    )
    result = write_hindsight_candidates_as_pending_proposals(
        drive_root=drive,
        repo_root=repo,
        workspace_id="ws1",
        run_id="run1",
        phase_id="reflexion",
        candidates=[candidate],
    )
    assert result["queued"] == 1
    assert list((drive / "state" / "bkb_proposals").glob("*.candidate.json"))
    bkb_path = repo / "workspaces" / "ws1" / ".memory" / "core" / "bkb.yaml"
    assert not bkb_path.exists()


def test_duplicate_hindsight_candidate_skipped(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    drive = repo / "workspaces" / "ws1" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    candidate = ReflectionCandidate(
        candidate_id="c1",
        kind="lesson",
        title="Verify first",
        content="Run verification before claiming done.",
        confidence=0.8,
        scope="workspace",
        evidence_refs=[_evidence(repo)],
    )
    first = write_hindsight_candidates_as_pending_proposals(
        drive_root=drive,
        repo_root=repo,
        workspace_id="ws1",
        run_id="run1",
        phase_id="reflexion",
        candidates=[candidate],
    )
    second = write_hindsight_candidates_as_pending_proposals(
        drive_root=drive,
        repo_root=repo,
        workspace_id="ws1",
        run_id="run1",
        phase_id="reflexion",
        candidates=[candidate],
    )
    assert first["queued"] == 1
    assert second["duplicates_skipped"] == 1


def test_reflexion_hindsight_candidates_are_not_auto_accepted(
    tmp_path, monkeypatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    drive = repo / "workspaces" / "ws1" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_REFLECT_ENABLED", "1")
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "dual")
    candidate = ReflectionCandidate(
        candidate_id="c1",
        kind="behavior",
        title="Keep candidates gated",
        content="Hindsight candidates remain pending proposals.",
        confidence=0.9,
        scope="workspace",
        evidence_refs=[_evidence(repo)],
    )
    backend = MagicMock()
    backend.reflect_candidates.return_value = [candidate]
    monkeypatch.setattr(
        "umbrella.memory.proactive.phase_hooks.create_durable_backend",
        lambda **_: backend,
    )

    result = process_reflexion_bkb_patch(
        repo_root=repo,
        drive_root=drive,
        workspace_id="ws1",
        run_id="run1",
    )
    assert result["hindsight_candidates"]["queued"] == 1
    assert not (repo / "workspaces" / "ws1" / ".memory" / "core" / "bkb.yaml").exists()


def test_hindsight_candidate_auto_accept_env_still_uses_bkb_gate(
    tmp_path, monkeypatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    drive = repo / "workspaces" / "ws1" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_REFLECT_ENABLED", "1")
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "dual")
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_AUTO_ACCEPT_CANDIDATES", "1")
    candidate = ReflectionCandidate(
        candidate_id="c1",
        kind="behavior",
        title="Auto accept with evidence",
        content="Only typed, valid evidence may enter BKB.",
        confidence=0.9,
        scope="workspace",
        evidence_refs=[_evidence(repo)],
    )
    backend = MagicMock()
    backend.reflect_candidates.return_value = [candidate]
    monkeypatch.setattr(
        "umbrella.memory.proactive.phase_hooks.create_durable_backend",
        lambda **_: backend,
    )

    result = process_reflexion_bkb_patch(
        repo_root=repo,
        drive_root=drive,
        workspace_id="ws1",
        run_id="run1",
    )
    assert result["hindsight_candidates"]["auto_accept"]["accepted"] == 1
    bkb_path = repo / "workspaces" / "ws1" / ".memory" / "core" / "bkb.yaml"
    assert "Only typed, valid evidence" in bkb_path.read_text(encoding="utf-8")


def test_invalid_hindsight_candidate_needs_evidence(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    drive = repo / "workspaces" / "ws1" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    candidate = ReflectionCandidate(
        candidate_id="c1",
        kind="lesson",
        title="Missing evidence",
        content="This should not be queued as acceptable.",
        confidence=0.4,
        scope="workspace",
        evidence_refs=[{"ref_id": "fake"}],
    )
    result = write_hindsight_candidates_as_pending_proposals(
        drive_root=drive,
        repo_root=repo,
        workspace_id="ws1",
        run_id="run1",
        phase_id="reflexion",
        candidates=[candidate],
    )
    assert result["needs_evidence"] == 1
    assert not list((drive / "state" / "bkb_proposals").glob("*.candidate.json"))


def test_hindsight_candidate_with_fake_typed_evidence_fails_bkb_gate(
    tmp_path, monkeypatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    fake_ref = {
        "ref_type": "ledger_event",
        "ref_id": "not-in-ledger",
        "produced_by": "verifier",
    }
    patch = ProposedBkbPatch(
        patch_id="fake-hs",
        workspace_id="ws1",
        run_id="run1",
        phase_id="reflexion",
        actor="supervisor",
        source_evidence=[fake_ref],
        rules=[
            {
                "id": "fake_rule",
                "title": "Fake evidence rule",
                "scope": "workspace",
                "type": "behavior",
                "rule": {"behavior": "should not enter BKB"},
                "source_backend": "hindsight",
            }
        ],
    )
    try:
        accept_bkb_patch(repo, patch, target="workspace")
    except ValueError as exc:
        assert "does not exist in supervisor ledger" in str(exc)
    else:
        raise AssertionError("fake Hindsight evidence entered BKB")
    bkb_path = repo / "workspaces" / "ws1" / ".memory" / "core" / "bkb.yaml"
    assert not bkb_path.exists()
