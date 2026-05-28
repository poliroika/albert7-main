import json
from pathlib import Path

from umbrella.context.subtask_memory import (
    SubtaskMemoryScope,
    infer_memory_scope_from_subtask,
    render_subtask_memory_scope_markdown,
    resolve_subtask_memory_chunks,
)
from umbrella.orchestrator.phase_plan import load_plan, save_plan
from umbrella.orchestrator.runner import PhaseRunner
from umbrella.orchestrator.worker import build_phase_task
from umbrella.phases.base import PhaseNode, PhasePlan, SubtaskCard
from umbrella.phases.registry import get_registry


def _manifest(phase_id: str):
    repo = Path(__file__).resolve().parents[2]
    return get_registry(repo / "umbrella" / "phases" / "manifests").get(phase_id)


def test_infer_memory_scope_from_codeptr_refs(tmp_path: Path) -> None:
    drive = tmp_path / "repo" / "workspaces" / "demo" / ".memory" / "drive"
    scope = infer_memory_scope_from_subtask(
        {
            "id": "ui",
            "codeptr_refs": [".memory/drive/memory/knowledge/inspiration/demo/knowledge.md"],
            "mcp_refs": ["browser"],
            "files_to_change": ["src/app/main.py"],
        },
        drive_root=drive if drive.parent.exists() else None,
    )
    kinds = {asset.kind for asset in scope.assets}
    assert "knowledge_md" in kinds
    assert "mcp_server" in kinds
    assert "workspace_file" in kinds


def test_nested_proof_memory_scope_is_lifted_for_execute_context() -> None:
    item = {
        "id": "gui-runtime",
        "proof": {
            "memory_scope": {
                "assets": [
                    {
                        "kind": "skill",
                        "ref": "desktop-gui-runtime",
                        "inject_mode": "on_demand",
                    }
                ],
                "notes": "Load runtime GUI automation notes before proof.",
            }
        },
    }

    scope = PhaseRunner._memory_scope_from_plan_item(item)

    assert scope is not None
    assert scope["assets"][0]["ref"] == "desktop-gui-runtime"


def test_phase_plan_roundtrip_preserves_subtask_memory_scope(tmp_path: Path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    plan = PhasePlan(
        plan_id="plan-1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[
            PhaseNode(
                id="execute",
                manifest_id="execute",
                subtasks=[
                    SubtaskCard(
                        id="gui",
                        title="GUI",
                        goal="Build GUI",
                        allowed_tools=frozenset(),
                        allowed_skills=frozenset(),
                        memory_scope={
                            "assets": [
                                {
                                    "kind": "github_snippet",
                                    "ref": "ek:github_snippet:demo_gui",
                                }
                            ],
                            "notes": "Load GUI prior art.",
                        },
                    )
                ],
            )
        ],
    )

    save_plan(plan, drive)
    loaded = load_plan(drive)

    assert loaded is not None
    execute = loaded.get_node("execute")
    assert execute is not None and execute.subtasks
    scope = execute.subtasks[0].memory_scope
    assert scope is not None
    assert scope["assets"][0]["ref"] == "ek:github_snippet:demo_gui"


def test_subtask_contract_key_includes_memory_scope() -> None:
    base = SubtaskCard(
        id="gui",
        title="GUI",
        goal="Build GUI",
        allowed_tools=frozenset(),
        allowed_skills=frozenset(),
    )
    with_scope = SubtaskCard(
        id="gui",
        title="GUI",
        goal="Build GUI",
        allowed_tools=frozenset(),
        allowed_skills=frozenset(),
        memory_scope={"assets": [{"kind": "skill", "ref": "desktop-gui"}]},
    )

    assert (
        PhaseRunner._subtask_card_contract_key(base)
        != PhaseRunner._subtask_card_contract_key(with_scope)
    )


def test_resolve_preloads_knowledge_md(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    knowledge = drive / "memory" / "knowledge" / "inspiration" / "demo"
    knowledge.mkdir(parents=True)
    md_path = knowledge / "knowledge.md"
    md_path.write_text("# Prior art\nUse Tkinter grid layout.\n", encoding="utf-8")
    from umbrella.context.subtask_memory import SubtaskMemoryAsset

    scope = SubtaskMemoryScope(
        assets=(
            SubtaskMemoryAsset(
                kind="knowledge_md",
                ref=".memory/drive/memory/knowledge/inspiration/demo/knowledge.md",
                inject_mode="preload",
            ),
        )
    )
    chunks = resolve_subtask_memory_chunks(
        scope,
        repo_root=repo,
        workspace_root=ws,
        workspace_id="demo",
        drive_root=drive,
    )
    loaded = [c for c in chunks if c.loaded and "Prior art" in c.text]
    assert loaded


def test_resolve_ek_catalog_ref(tmp_path: Path) -> None:
    from umbrella.discovery.external_catalog import register_card

    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    knowledge = drive / "memory" / "knowledge" / "inspiration" / "demo"
    knowledge.mkdir(parents=True)
    md_path = knowledge / "note.md"
    md_path.write_text("# Note\nbody\n", encoding="utf-8")

    class _Ctx:
        drive_root = drive

    storage = ".memory/drive/memory/knowledge/inspiration/demo/note.md"
    ek = register_card(
        _Ctx(),
        kind="knowledge_md",
        source_id="github:demo/note",
        storage_ref=storage,
        preview="body",
    )
    from umbrella.context.subtask_memory import SubtaskMemoryAsset, SubtaskMemoryScope

    scope = SubtaskMemoryScope(
        assets=(SubtaskMemoryAsset(kind="knowledge_md", ref=ek, inject_mode="preload"),)
    )
    chunks = resolve_subtask_memory_chunks(
        scope,
        repo_root=repo,
        workspace_root=ws,
        workspace_id="demo",
        drive_root=drive,
    )
    assert any(c.loaded and "body" in c.text for c in chunks)


def test_preload_cap_limits_bodies(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    base = drive / "memory" / "knowledge" / "inspiration"
    for idx in range(5):
        p = base / f"f{idx}"
        p.mkdir(parents=True, exist_ok=True)
        (p / "a.md").write_text(f"content {idx}\n", encoding="utf-8")
    from umbrella.context.subtask_memory import SubtaskMemoryAsset, SubtaskMemoryScope

    assets = tuple(
        SubtaskMemoryAsset(
            kind="knowledge_md",
            ref=f".memory/drive/memory/knowledge/inspiration/f{i}/a.md",
            inject_mode="preload",
        )
        for i in range(5)
    )
    chunks = resolve_subtask_memory_chunks(
        SubtaskMemoryScope(assets=assets),
        repo_root=repo,
        workspace_root=ws,
        workspace_id="demo",
        drive_root=drive,
    )
    loaded = [c for c in chunks if c.loaded and c.text.strip()]
    assert len(loaded) == 3


def test_subtask_memory_scope_does_not_duplicate_active_proof(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    chunks = resolve_subtask_memory_chunks(
        SubtaskMemoryScope(),
        repo_root=repo,
        workspace_root=ws,
        workspace_id="demo",
        drive_root=drive,
        subtask={
            "id": "scaffold",
            "proof": {"execution": {"kind": "pytest", "command": ["pytest"]}},
        },
    )

    assert all(c.ref != "subtask.proof" for c in chunks)
    assert all(c.title != "Subtask proof contract" for c in chunks)


def test_execute_task_includes_subtask_memory_section(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    knowledge = drive / "memory" / "knowledge" / "inspiration" / "demo"
    knowledge.mkdir(parents=True)
    (knowledge / "knowledge.md").write_text("# Snippet\n", encoding="utf-8")
    (ws / "TASK_MAIN.md").write_text("Build demo", encoding="utf-8")

    from umbrella.memory.palace.facade import MemPalace

    manifest = _manifest("execute")
    node = PhaseNode(
        id="execute",
        manifest_id="execute",
        subtasks=[
            SubtaskCard(
                id="scaffold",
                title="Scaffold",
                goal="Create UI",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                codeptr_refs=[".memory/drive/memory/knowledge/inspiration/demo/knowledge.md"],
                memory_scope={
                    "assets": [
                        {
                            "kind": "knowledge_md",
                            "ref": ".memory/drive/memory/knowledge/inspiration/demo/knowledge.md",
                            "inject_mode": "preload",
                        }
                    ]
                },
            )
        ],
    )
    palace = MemPalace(repo, "demo")
    try:
        task = build_phase_task(
            phase_node=node,
            manifest=manifest,
            workspace_id="demo",
            run_id="run-1",
            palace=palace,
            repo_root=repo,
            drive_root=drive,
        )
    finally:
        palace.close()
    prompt = str(task.get("input") or "")
    assert "## Subtask memory scope" in prompt
    assert "knowledge_md" in prompt
    overlays = task.get("context_overlays") or {}
    assert overlays.get("subtask_memory_scope")
    scope_file = drive / "state" / "subtask_memory_scope_scaffold.json"
    assert scope_file.is_file()


def test_execute_tool_surface_keeps_conditional_tools_out_by_default(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (ws / "TASK_MAIN.md").write_text("Build demo", encoding="utf-8")

    from umbrella.memory.palace.facade import MemPalace

    palace = MemPalace(repo, "demo")
    try:
        task = build_phase_task(
            phase_node=PhaseNode(
                id="execute",
                manifest_id="execute",
                subtasks=[
                    SubtaskCard(
                        id="scaffold",
                        title="Scaffold",
                        goal="Create UI",
                        allowed_tools=frozenset(),
                        allowed_skills=frozenset(),
                    )
                ],
            ),
            manifest=_manifest("execute"),
            workspace_id="demo",
            run_id="run-1",
            palace=palace,
            repo_root=repo,
            drive_root=drive,
        )
    finally:
        palace.close()

    allowed = set(task.get("tool_filter", {}).get("allow") or [])
    dynamic_tools = {
        "get_gmas_context",
        "search_gmas_knowledge",
        "palace_add",
        "palace_link",
        "request_extra_subtask",
        "loop_back_to",
    }
    assert not (allowed & dynamic_tools)
    allowed_block = str(task.get("input") or "").split(
        "## Your allowed tools for this phase", 1
    )[1].split("\n##", 1)[0]
    for tool in dynamic_tools:
        assert f"- {tool}" not in allowed_block


def test_execute_tool_surface_adds_declared_and_gmas_tools_dynamically(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (ws / "TASK_MAIN.md").write_text("Build GMAS demo", encoding="utf-8")

    from umbrella.memory.palace.facade import MemPalace

    palace = MemPalace(repo, "demo")
    try:
        task = build_phase_task(
            phase_node=PhaseNode(
                id="execute",
                manifest_id="execute",
                subtasks=[
                    SubtaskCard(
                        id="gmas-memory",
                        title="Model runtime memory router",
                        goal="Implement model runtime memory routing.",
                        allowed_tools=frozenset(
                            {"palace_add", "loop_back_to", "request_extra_subtask"}
                        ),
                        allowed_skills=frozenset({"gmas-overview"}),
                        memory_scope={"assets": [{"kind": "gmas_context"}]},
                    )
                ],
            ),
            manifest=_manifest("execute"),
            workspace_id="demo",
            run_id="run-1",
            palace=palace,
            repo_root=repo,
            drive_root=drive,
        )
    finally:
        palace.close()

    allowed = set(task.get("tool_filter", {}).get("allow") or [])
    assert {"get_gmas_context", "search_gmas_knowledge"} <= allowed
    assert {"palace_add", "loop_back_to", "request_extra_subtask"} <= allowed
    overlays = task.get("context_overlays") or {}
    assert "gmas-overview" in set(overlays.get("effective_allowed_skills") or [])


def test_execute_agent_words_do_not_enable_gmas_tools_without_typed_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    ws = repo / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (ws / "TASK_MAIN.md").write_text("Build an agent-like workflow", encoding="utf-8")

    from umbrella.memory.palace.facade import MemPalace

    palace = MemPalace(repo, "demo")
    try:
        task = build_phase_task(
            phase_node=PhaseNode(
                id="execute",
                manifest_id="execute",
                subtasks=[
                    SubtaskCard(
                        id="agent-router",
                        title="Agent router",
                        goal="Implement local agent and judge naming without LLM APIs.",
                        allowed_tools=frozenset(
                            {
                                "get_gmas_context",
                                "search_gmas_knowledge",
                                "register_temp_tool",
                            }
                        ),
                        allowed_skills=frozenset({"gmas-overview"}),
                    )
                ],
            ),
            manifest=_manifest("execute"),
            workspace_id="demo",
            run_id="run-1",
            palace=palace,
            repo_root=repo,
            drive_root=drive,
        )
    finally:
        palace.close()

    allowed = set(task.get("tool_filter", {}).get("allow") or [])
    assert not (allowed & {"get_gmas_context", "search_gmas_knowledge"})
    assert "register_temp_tool" not in allowed
    overlays = task.get("context_overlays") or {}
    assert "gmas-overview" not in set(overlays.get("effective_allowed_skills") or [])
