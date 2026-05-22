"""Production readiness fixes for proactive memory and phase tools."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from umbrella.deep_agent_tools.phase_control_actions import (
    _accept_bkb_proposal,
    _submit_reflection,
)
from umbrella.deep_agent_tools.phase_contract_handlers import _promote_to_durable
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler
from umbrella.memory.proactive.promotion import (
    ProposedBkbPatch,
    accept_bkb_patch,
    validate_patch_evidence,
)
from umbrella.memory.paths import manager_core_root
from umbrella.phases.loader import load_manifest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces").mkdir()
    core = tmp_path / ".umbrella" / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00_identity.md").write_text("# Identity\nAlways verify.\n", encoding="utf-8")
    (core / "10_operating_principles.md").write_text("# Principles\n", encoding="utf-8")
    (core / "bkb.yaml").write_text("rules: []\n", encoding="utf-8")
    return tmp_path


def _artifact_evidence(repo: Path, name: str = "evidence.txt") -> list[dict]:
    path = repo / name
    path.write_text("verified evidence", encoding="utf-8")
    return [
        {
            "ref_type": "artifact",
            "ref_id": name,
            "produced_by": "supervisor",
        }
    ]


def test_promote_to_durable_verify_no_name_error(repo):
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.drive_root = str(repo / "drive")
    ctx.loop_state_view = {"phase_label": "verify"}
    ctx.task_id = "task-1"
    result = _promote_to_durable(
        ctx,
        title="Verification passed",
        content="All checks green with no unresolved blockers.",
        workspace_id="ws1",
        tags="verification_report,durable",
    )
    assert "NameError" not in result


def _seed_verifier_ledger(repo: Path, workspace_id: str = "ws1"):
    from umbrella.enforcement.ledger import append_supervisor_ledger_event

    return append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id=workspace_id,
        actor="verifier",
        phase="verify",
        tool="run_workspace_verify",
        result={"passed": True},
    )


def test_promote_to_durable_with_typed_evidence_succeeds(repo):
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.drive_root = str(repo / "drive")
    ctx.loop_state_view = {"phase_label": "verify"}
    ctx.task_id = "run-1:verify"
    event = _seed_verifier_ledger(repo, "ws1")
    evidence = [
        {
            "ref_type": "ledger_event",
            "ref_id": event.event_id,
            "hash": event.event_hash,
            "produced_by": "verifier",
        }
    ]
    result = _promote_to_durable(
        ctx,
        title="Verification passed",
        content="All checks green with no unresolved blockers.",
        workspace_id="ws1",
        tags="verification_report,durable",
        evidence_refs=evidence,
        trust_level="public_verified",
    )
    payload = json.loads(result)
    assert payload.get("saved") is True
    assert payload.get("durable_store") == "palace.durable"
    node_id = str(payload.get("durable_node_id") or payload.get("canonical_id") or "").strip()
    assert node_id
    palace = MemPalace(repo, "ws1")
    try:
        node = palace.get(node_id, stores=["palace.durable"])
    finally:
        palace.close()
    assert node is not None
    assert node.get("trust_level") == "public_verified"
    assert "ledger_event" in node.get("evidence_refs_json", "")


def test_promote_to_durable_rejects_fake_ledger_event(repo):
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.drive_root = str(repo / "drive")
    ctx.loop_state_view = {"phase_label": "verify"}
    ctx.task_id = "run-1:verify"
    palace = MemPalace(repo, "ws1")
    try:
        before = len(palace.list_all(n=200, stores=["palace.durable"]))
    finally:
        palace.close()
    result = _promote_to_durable(
        ctx,
        title="Verification passed",
        content="All checks green.",
        workspace_id="ws1",
        tags="verification_report,durable",
        evidence_refs=[
            {
                "ref_type": "ledger_event",
                "ref_id": "fake_tools_42",
                "produced_by": "verifier",
            }
        ],
        trust_level="public_verified",
    )
    payload = json.loads(result)
    assert payload.get("saved") is False
    assert payload.get("reason") == "invalid_evidence_refs"
    palace = MemPalace(repo, "ws1")
    try:
        after = len(palace.list_all(n=200, stores=["palace.durable"]))
    finally:
        palace.close()
    assert after == before


def test_promote_to_durable_blocked_without_durable_node(repo):
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.drive_root = str(repo / "drive")
    ctx.loop_state_view = {"phase_label": "verify"}
    ctx.task_id = "run-1:verify"
    palace = MemPalace(repo, "ws1")
    try:
        before = len(palace.list_all(n=200, stores=["palace.durable"]))
    finally:
        palace.close()
    result = _promote_to_durable(
        ctx,
        title="Verification passed",
        content="All checks green.",
        workspace_id="ws1",
        tags="durable",
    )
    payload = json.loads(result)
    assert payload.get("saved") is False
    palace = MemPalace(repo, "ws1")
    try:
        after = len(palace.list_all(n=200, stores=["palace.durable"]))
    finally:
        palace.close()
    assert after == before
    assert "durable_node_id" not in payload


def test_get_umbrella_memory_without_mempalace(repo, monkeypatch):
    from umbrella.deep_agent_tools import memory as memory_tools

    monkeypatch.setattr(memory_tools, "_legacy_palace_available", lambda: False)
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.loop_state_view = {}
    raw = memory_tools.get_umbrella_memory(ctx, query="nonexistent-query-xyz", limit=5)
    payload = json.loads(raw)
    assert "WARNING" not in raw
    assert payload.get("source") in {"canonical_mempalace", "jsonl_fallback"}
    assert payload.get("palace_memory") == []


def test_get_umbrella_memory_reports_backend_unavailable(repo, monkeypatch):
    from umbrella.deep_agent_tools import memory as memory_tools

    monkeypatch.delenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", raising=False)
    monkeypatch.setattr(memory_tools, "_legacy_palace_available", lambda: False)
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.loop_state_view = {}
    try:
        import chromadb  # noqa: F401
    except ImportError:
        raw = memory_tools.get_umbrella_memory(ctx, query="test", limit=3)
        payload = json.loads(raw)
        assert payload.get("stats", {}).get("ok") is False
    else:
        pytest.skip("chromadb installed — backend availability test not applicable")


def test_canonical_search_filters_stale_run(repo):
    from umbrella.deep_agent_tools.memory import (
        _canonical_mempalace_search,
        _is_stale_run_scoped_memory,
    )

    stale_hit = {"scope": "run_scoped", "run_id": "old-run", "verified": True}
    current_hit = {"scope": "run_scoped", "run_id": "new-run", "verified": True}
    assert _is_stale_run_scoped_memory(stale_hit, "new-run")
    assert not _is_stale_run_scoped_memory(current_hit, "new-run")

    palace = MemPalace(repo, "ws-stale")
    try:
        node_id = palace.add(
            store="palace.idea",
            content="current run scoped note",
            tier="hot",
            scope="run_scoped",
            tags=["test"],
            run_id="new-run",
            phase="execute",
            kind="observation",
        )
        hits, health = _canonical_mempalace_search(
            repo,
            workspace_id="ws-stale",
            query="",
            limit=10,
            include_unverified=False,
            current_run_id="new-run",
        )
        assert health.get("ok") is True
        if hits:
            assert all("run_id" in h for h in hits)
            assert node_id in {h.get("id") for h in hits}
    finally:
        palace.close()


def test_overlay_respects_global_budget(repo):
    manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=repo,
        workspace_id="",
        run_id="r1",
        phase_id="research",
        subtask_id=None,
        task_brief="x" * 5000,
        manifest=manifest,
        token_budget=1800,
    )
    tokens = overlay.telemetry.get("memory_overlay_tokens", 0)
    assert tokens <= 1800 + 50


@pytest.mark.parametrize("phase_id", ["task_start", "research", "execute", "verify", "reflexion"])
def test_overlay_always_includes_mandatory_core(repo, phase_id):
    manifest_path = Path(f"umbrella/phases/manifests/{phase_id}.yaml")
    if not manifest_path.is_file():
        manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    else:
        manifest = load_manifest(manifest_path)
    if phase_id == "task_start":
        overlay = ProactiveMemoryCompiler().build_minimal_overlay(
            repo_root=repo, phase_id=phase_id
        )
    else:
        overlay = ProactiveMemoryCompiler().build_overlay(
            repo_root=repo,
            workspace_id="",
            run_id="r",
            phase_id=phase_id,
            subtask_id=None,
            task_brief="brief",
            manifest=manifest,
        )
    names = [s.name.lower() for s in overlay.sections]
    assert any("identity" in n or "constitution" in n for n in names)
    assert any("phase commitment" in n for n in names)


def test_bkb_patch_rejects_string_evidence(repo):
    patch = ProposedBkbPatch(
        patch_id="p1",
        rules=[{"id": "r1", "title": "T", "type": "behavior", "rule": {}}],
        source_evidence=["tools_42"],  # type: ignore[list-item]
        actor="supervisor",
    )
    with pytest.raises(ValueError, match="typed"):
        validate_patch_evidence(patch, repo_root=repo)


def test_bkb_patch_rejects_fake_artifact(repo):
    patch = ProposedBkbPatch(
        patch_id="p3",
        rules=[{"id": "r3", "title": "T", "type": "behavior", "rule": {}}],
        source_evidence=[
            {
                "ref_type": "artifact",
                "ref_id": "missing_file.json",
                "produced_by": "supervisor",
            }
        ],
        actor="supervisor",
    )
    with pytest.raises(ValueError, match="ledger-backed evidence or an existing artifact"):
        validate_patch_evidence(patch, repo_root=repo)


def test_bkb_patch_accepts_real_artifact(repo):
    patch = ProposedBkbPatch(
        patch_id="p2",
        rules=[
            {
                "id": "bkb_ev",
                "title": "Evidence lesson",
                "type": "lesson",
                "rule": {"behavior": "cite ledger"},
            }
        ],
        source_evidence=_artifact_evidence(repo),
        actor="supervisor",
    )
    accept_bkb_patch(repo, patch)
    text = (manager_core_root(repo) / "bkb.yaml").read_text(encoding="utf-8")
    assert "bkb_ev" in text
    lessons = (manager_core_root(repo) / "20_manager_lessons.md").read_text(encoding="utf-8")
    assert "bkb_ev" in lessons


def test_workspace_artifact_evidence_under_workspaces_dir(repo):
    ws_dir = repo / "workspaces" / "ws1"
    ws_dir.mkdir(parents=True)
    ws_core = ws_dir / ".memory" / "core"
    ws_core.mkdir(parents=True)
    (ws_core / "bkb.yaml").write_text("rules: []\n", encoding="utf-8")
    (ws_dir / "evidence.txt").write_text("workspace artifact", encoding="utf-8")
    patch = ProposedBkbPatch(
        patch_id="ws_art",
        rules=[{"id": "ws_rule", "title": "T", "type": "behavior", "rule": {}}],
        source_evidence=[
            {
                "ref_type": "artifact",
                "ref_id": "evidence.txt",
                "produced_by": "supervisor",
            }
        ],
        actor="supervisor",
        workspace_id="ws1",
    )
    accept_bkb_patch(repo, patch, target="workspace")
    text = (repo / "workspaces" / "ws1" / ".memory" / "core" / "bkb.yaml").read_text(
        encoding="utf-8"
    )
    assert "ws_rule" in text


def test_reflexion_runner_bkb_hook_rejects_invalid_patch(repo, tmp_path):
    from umbrella.memory.proactive.phase_hooks import process_reflexion_bkb_patch

    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    patch_doc = {
        "patch_id": "hook_reject",
        "status": "candidate",
        "actor": "supervisor",
        "workspace_id": "",
        "rules": [{"id": "bad_rule", "title": "T", "type": "behavior", "rule": {}}],
        "source_evidence": [
            {
                "ref_type": "artifact",
                "ref_id": "missing_evidence.txt",
                "produced_by": "supervisor",
            }
        ],
    }
    (state / "proposed_bkb_patch.json").write_text(
        json.dumps(patch_doc, ensure_ascii=False),
        encoding="utf-8",
    )
    result = process_reflexion_bkb_patch(
        repo_root=repo,
        drive_root=drive,
        workspace_id="",
    )
    assert result is not None
    assert result.get("accepted") is False
    assert result.get("reason")
    updated = json.loads((state / "proposed_bkb_patch.json").read_text(encoding="utf-8"))
    assert updated.get("status") == "rejected"


def test_reflexion_runner_bkb_hook_accepts_patch(repo, tmp_path):
    from umbrella.memory.proactive.phase_hooks import process_reflexion_bkb_patch

    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (repo / "reflection_evidence.txt").write_text("ok", encoding="utf-8")
    patch_doc = {
        "patch_id": "hook_patch",
        "status": "candidate",
        "actor": "supervisor",
        "workspace_id": "",
        "rules": [
            {
                "id": "hook_rule",
                "title": "Hook rule",
                "type": "behavior",
                "rule": {"behavior": "supervisor only"},
            }
        ],
        "source_evidence": _artifact_evidence(repo, "reflection_evidence.txt"),
    }
    (state / "proposed_bkb_patch.json").write_text(
        json.dumps(patch_doc, ensure_ascii=False),
        encoding="utf-8",
    )
    result = process_reflexion_bkb_patch(
        repo_root=repo,
        drive_root=drive,
        workspace_id="",
    )
    assert result is not None
    assert result.get("accepted") is True
    updated = json.loads((state / "proposed_bkb_patch.json").read_text(encoding="utf-8"))
    assert updated.get("status") == "accepted"
    bkb_text = (manager_core_root(repo) / "bkb.yaml").read_text(encoding="utf-8")
    assert "hook_rule" in bkb_text


def test_bkb_proposal_flow_submit_then_accept(repo, tmp_path):
    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.drive_root = drive
    ctx.loop_state_view = {"phase_label": "reflexion"}
    ctx.task_id = "run-reflex:reflexion"

    evidence = _artifact_evidence(repo, "reflection_evidence.txt")
    submit_result = _submit_reflection(
        ctx,
        text="Migration failed due to type mismatch.",
        applies_to_phase="execute",
        evidence_refs=evidence,
        proposed_bkb_rules=[
            {
                "id": "bkb_flow_rule",
                "title": "Type check before migrate",
                "type": "behavior",
                "rule": {"behavior": "validate types first"},
            }
        ],
    )
    assert "ERROR" not in submit_result
    patch_path = state / "proposed_bkb_patch.json"
    assert patch_path.is_file()
    doc = json.loads(patch_path.read_text(encoding="utf-8"))
    assert doc.get("actor") == "supervisor"

    accept_result = _accept_bkb_proposal(ctx)
    assert "ERROR" not in accept_result
    bkb_text = (manager_core_root(repo) / "bkb.yaml").read_text(encoding="utf-8")
    assert "bkb_flow_rule" in bkb_text
