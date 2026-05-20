import json

from ouroboros.tools.phase_contract import _palace_add, _palace_search
from ouroboros.tools.registry import ToolContext
from umbrella.memory.palace.facade import MemPalace


def _ctx(tmp_path, *, workspace_id="trust_ws", task_id="phase_web_c7817420:research"):
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / workspace_id / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = task_id
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": task_id.split(":", 1)[-1],
        "active_workspace_id": workspace_id,
    }
    return ctx, drive


def _append_tool_row(drive, *, task_id, tool, result, args=None):
    with (drive / "logs" / "tools.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "task_id": task_id,
                    "tool": tool,
                    "args": args or {},
                    "result_preview": json.dumps(result),
                }
            )
            + "\n"
        )


def test_palace_add_observation_verified_outcome_stays_untrusted(tmp_path):
    ctx, _drive = _ctx(tmp_path)

    result = _palace_add(
        ctx,
        title="Research progress status update",
        content=(
            "Research progress: current synthesis says discovery is underway "
            "and finding attempts still need provenance."
        ),
        kind="observation",
        workspace_id="trust_ws",
        evidence_kind="verified_outcome",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "observation"
    assert payload["verified"] is False
    assert payload["source_path"] == "tool:palace_add"

    ctx.task_id = "phase_web_c7817420:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "trust_ws"}
    trusted = json.loads(
        _palace_search(
            ctx,
            query="finding attempts still need provenance",
            workspace_id="trust_ws",
            include_unverified=False,
        )
    )
    assert trusted["palace_memory"] == []

    unverified = json.loads(
        _palace_search(
            ctx,
            query="finding attempts still need provenance",
            workspace_id="trust_ws",
            include_unverified=True,
        )
    )
    assert any(
        hit["id"] == payload["legacy"]["id"]
        for hit in unverified["unverified_candidates"]["palace_memory"]
    )


def test_palace_add_research_finding_with_current_source_remains_verified(tmp_path):
    ctx, drive = _ctx(tmp_path, task_id="phase_web_c7817420:research")
    _append_tool_row(
        drive,
        task_id="phase_web_c7817420:research",
        tool="search_gmas_knowledge",
        result={
            "status": "ok",
            "query": "GMAS bot turns",
            "recommended_pattern": "Use explicit action contracts.",
        },
    )

    result = _palace_add(
        ctx,
        title="GMAS action contract finding",
        content="GMAS bot turns should use explicit action contracts.",
        kind="research_finding",
        workspace_id="trust_ws",
        tags="research_finding,gmas",
        source_id="search_gmas_knowledge",
        evidence_kind="verified_outcome",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["verified"] is True
    stored = MemPalace(tmp_path, "trust_ws").get(payload["id"])
    assert stored["verified"] is True
