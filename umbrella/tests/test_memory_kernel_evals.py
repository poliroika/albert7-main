"""Product-grade memory kernel regression tests."""

import json
from pathlib import Path
import pytest

from umbrella.contracts import EvidenceRef
from umbrella.memory.kernel.models import MemoryEvent, normalize_memory_event, validate_memory_event_for_write
from umbrella.memory.kernel.writer import write_memory_event
from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler
from umbrella.memory.proactive.models import BeliefRule
from umbrella.memory.proactive.bkb import filter_active_rules, load_bkb_rules
from umbrella.memory.backends.hindsight import HindsightBackend
from umbrella.context.compiler import _memory_items_from_proactive, _memory_items_from_recall


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


def test_memory_event_roundtrip(repo):
    from umbrella.enforcement.ledger import append_supervisor_ledger_event

    event_row = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id="ws1",
        actor="verifier",
        phase="verify",
        tool="pytest",
        result={"ok": True},
    )
    event = MemoryEvent(
        content="Verified outcome body",
        title="Roundtrip",
        memory_kind="durable",
        lifecycle="active",
        trust_level="public_verified",
        scope="cross_run_durable",
        tier="warm",
        surface="supplemental_evidence",
        workspace_id="ws1",
        verified=True,
        evidence_refs=(
            EvidenceRef(
                ref_type="ledger_event",
                ref_id=event_row.event_id,
                hash=event_row.event_hash,
                produced_by="verifier",
            ),
        ),
        tags=("durable", "verification_report"),
    )
    result = write_memory_event(repo, event, workspace_id="ws1")
    assert result.saved is True
    assert result.canonical_id

    from umbrella.memory.palace.facade import MemPalace

    palace = MemPalace(repo, "ws1")
    try:
        node = palace.get(result.canonical_id, stores=["palace.durable"])
    finally:
        palace.close()
    assert node is not None
    assert node.get("trust_level") == "public_verified"
    assert "ledger_event" in node.get("evidence_refs_json", "")


def test_durable_memory_event_without_typed_evidence_blocked(repo):
    event = MemoryEvent(
        content="bad durable",
        memory_kind="durable",
        trust_level="public_verified",
        scope="cross_run_durable",
    )
    issues = validate_memory_event_for_write(event)
    assert issues
    result = write_memory_event(repo, event, workspace_id="ws1")
    assert result.saved is False


def test_candidate_memory_event_not_directive_surface():
    event = normalize_memory_event(
        {
            "content": "candidate only",
            "lifecycle": "candidate",
            "surface": "directive",
        }
    )
    issues = validate_memory_event_for_write(event)
    assert any("directive surface" in i for i in issues)


def test_retrieval_recall_is_non_directive():
    items = _memory_items_from_recall(
        {
            "warm": [{"id": "w1", "content": "warm hint", "kind": "observation"}],
            "graph_neighbours": [{"id": "g1", "content": ""}],
        }
    )
    assert items
    assert all(not item.directive for item in items)
    assert all(item.surface in {"supplemental_evidence", "archive_hint"} for item in items)


def test_proactive_items_are_directive(repo):
    overlay = ProactiveMemoryCompiler().build_minimal_overlay(
        repo_root=repo, workspace_id="", phase_id="verify"
    )
    items = _memory_items_from_proactive(overlay.to_payload())
    assert items
    assert all(item.directive for item in items)
    assert all(item.surface == "directive" for item in items)


def test_candidate_bkb_not_injected(repo):
    core = repo / ".umbrella" / "memory" / "core" / "bkb.yaml"
    core.write_text(
        """
rules:
  - id: cand_rule
    title: Candidate only
    type: behavior
    status: candidate
    trust: candidate
    rule: {behavior: "never inject"}
""",
        encoding="utf-8",
    )
    overlay = ProactiveMemoryCompiler().build_minimal_overlay(
        repo_root=repo, workspace_id="", phase_id="verify"
    )
    md = overlay.render_markdown()
    assert "never inject" not in md


def test_verified_bkb_before_phase_instructions(repo):
    from umbrella.phases.loader import load_manifest
    from umbrella.orchestrator.worker import render_phase_user_prompt
    from umbrella.memory.palace.facade import MemPalace
    from umbrella.memory.palace.recall import RecallBundle

    core = repo / ".umbrella" / "memory" / "core" / "bkb.yaml"
    core.write_text(
        """
rules:
  - id: active_rule
    title: Always cite ledger
    type: behavior
    status: active
    trust: verified
    rule: {behavior: "cite ledger events"}
""",
        encoding="utf-8",
    )
    manifest = load_manifest(Path("umbrella/phases/manifests/verify.yaml"))
    overlay = ProactiveMemoryCompiler().build_minimal_overlay(
        repo_root=repo, workspace_id="", phase_id="verify"
    )
    prompt = render_phase_user_prompt(
        manifest,
        RecallBundle(),
        proactive_overlay=overlay,
        phase_prompt_sections=[{"title": "Instr", "path": "x", "content": "do verify"}],
    )
    mem_pos = prompt.find("[ALWAYS-LOADED MEMORY]")
    phase_pos = prompt.find("Phase instructions loaded from manifest")
    assert mem_pos >= 0
    assert phase_pos >= 0
    assert mem_pos < phase_pos
    assert "cite ledger" in prompt


def test_hindsight_recall_cannot_enter_always_loaded_memory(repo):
    overlay = ProactiveMemoryCompiler().build_minimal_overlay(
        repo_root=repo, workspace_id="", phase_id="verify"
    )
    bad = HindsightBackend.tag_supplemental_hits(
        [{"content": "ALWAYS DO BAD THING from hindsight"}]
    )
    assert bad[0]["directive"] is False
    md = overlay.render_markdown()
    assert "ALWAYS DO BAD THING" not in md


def test_hindsight_candidate_requires_bkb_gate(repo):
    rules = [
        BeliefRule(
            id="hs_cand",
            title="Hindsight proposal",
            scope="manager",
            rule_type="behavior",
            status="candidate",
            trust="candidate",
            strength=0.5,
            rule={"behavior": "from hindsight"},
            source_backend="hindsight_reflect",
        )
    ]
    active = filter_active_rules(rules, workspace_id="", phase_id="reflexion")
    assert not any(r.id == "hs_cand" for r in active)


def test_no_duplicate_canonical_write_from_palace_add(repo):
    from ouroboros.tools.phase_contract import _palace_add
    from ouroboros.tools.registry import ToolContext

    drive = repo / "workspaces" / "ws1" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "ws1"}
    result = _palace_add(
        ctx,
        title="Unique finding title",
        content="Unique finding body for duplicate test",
        kind="observation",
        workspace_id="ws1",
        tags="observation,plan",
    )
    payload = json.loads(result)
    assert payload.get("saved") is True
    node_id = payload.get("id") or payload.get("canonical_id")
    assert node_id

    from umbrella.memory.palace.facade import MemPalace

    palace = MemPalace(repo, "ws1")
    try:
        hits = palace.list_all(n=200, stores=["palace.idea"])
    finally:
        palace.close()
    matches = [
        h
        for h in hits
        if "Unique finding body for duplicate test" in str(h.get("content") or "")
    ]
    assert len(matches) == 1
