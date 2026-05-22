"""CI guard: every YAML manifest in umbrella/phases/manifests/ must be valid per schema."""
import json
import pathlib
import pytest

MANIFESTS_DIR = pathlib.Path(__file__).parent.parent / "phases" / "manifests"
SKILLS_DIR = pathlib.Path(__file__).parent.parent / "skills" / "library"
PROMPTS_DIR = pathlib.Path(__file__).parent.parent / "prompts" / "phases"


def _manifest_files():
    if not MANIFESTS_DIR.exists():
        return []
    return sorted(MANIFESTS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("manifest_path", _manifest_files(), ids=lambda p: p.name)
def test_manifest_is_valid(manifest_path):
    from umbrella.phases.loader import load_manifest, PhaseManifestError
    manifest = load_manifest(manifest_path)
    assert manifest.id, f"manifest {manifest_path.name} has empty id"
    assert manifest.version >= 1
    assert len(manifest.allowed_tools) > 0 or manifest.id == "reflexion", f"{manifest_path.name}: no allowed_tools"
    overlap = manifest.allowed_tools & manifest.forbidden_tools
    assert not overlap, f"{manifest_path.name}: allowed ∩ forbidden = {overlap}"


def test_all_manifests_loaded():
    from umbrella.phases.registry import PhaseRegistry
    reg = PhaseRegistry(MANIFESTS_DIR)
    errors = reg.validate_all()
    assert not errors, f"Manifest errors:\n" + "\n".join(errors)


def test_manifest_prompt_files_exist():
    from umbrella.phases.registry import PhaseRegistry

    repo_root = MANIFESTS_DIR.parents[2]
    reg = PhaseRegistry(MANIFESTS_DIR)
    missing: list[str] = []
    for manifest in reg.all():
        prompt_paths = [
            *manifest.prompt_files.system,
            *manifest.prompt_files.user_overlay,
            *manifest.prompt_files.charter_blocks,
        ]
        for rel_path in prompt_paths:
            if not (repo_root / rel_path).is_file():
                missing.append(f"{manifest.id}: {rel_path}")
    assert not missing, "Manifest prompt file(s) missing:\n" + "\n".join(missing)


def test_phase_ids_unique():
    from umbrella.phases.registry import PhaseRegistry
    reg = PhaseRegistry(MANIFESTS_DIR)
    ids = reg.ids()
    assert len(ids) == len(set(ids)), "Duplicate phase IDs found"


def test_expected_phases_present():
    from umbrella.phases.registry import PhaseRegistry
    reg = PhaseRegistry(MANIFESTS_DIR)
    ids = set(reg.ids())
    required = {"preflight", "research", "plan", "execute", "verify"}
    missing = required - ids
    assert not missing, f"Missing required phases: {missing}"


def test_plan_phase_requires_authoritative_plan_artifact():
    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(MANIFESTS_DIR / "plan.yaml")
    assert "propose_phase_plan" in manifest.exit_criteria.required_calls
    assert "submit_phase_plan" in manifest.exit_criteria.required_calls


def test_phase_memory_routes_subtasks_to_subtask_store():
    from umbrella.phases.loader import load_manifest

    plan = load_manifest(MANIFESTS_DIR / "plan.yaml")
    execute = load_manifest(MANIFESTS_DIR / "execute.yaml")
    subtask = load_manifest(MANIFESTS_DIR / "subtask_template.yaml")

    assert plan.memory.write_rules["subtask_card"].store == "palace.subtask"
    assert plan.memory.write_rules["subtask_card"].scope == "subtask_scoped"
    assert any(rule.store == "palace.subtask" for rule in execute.memory.hot)
    assert execute.memory.write_rules["error_record"].store == "palace.subtask"
    assert subtask.memory.write_rules["subtask_artifact"].store == "palace.subtask"


def test_plan_prompt_documents_executable_leaf_payload_contract():
    prompt = (PROMPTS_DIR / "plan.system.md").read_text(encoding="utf-8")

    assert "Each executable subtask must include" in prompt
    assert '"goal"' in prompt
    assert '"files_to_create"' in prompt
    assert '"proof"' in prompt
    assert '"command": ["python", "-m", "pytest"' in prompt
    assert "shell=true" in prompt
    assert "collect-only" in prompt
    assert "input sensitivity" in prompt
    assert "mock/fake/dry-run" in prompt


def test_agent_facing_runtime_prompts_do_not_teach_unsupported_model_alias():
    prompt_paths = [
        PROMPTS_DIR / "plan.system.md",
        PROMPTS_DIR / "plan_review.system.md",
        PROMPTS_DIR / "execute.system.md",
        PROMPTS_DIR.parent / "policies" / "llm_agent_runtime.md",
        PROMPTS_DIR.parent / "ouroboros_workspace_task.md",
        SKILLS_DIR / "gmas-overview" / "SKILL.md",
    ]

    for prompt_path in prompt_paths:
        text = prompt_path.read_text(encoding="utf-8")
        assert "OUROBOROS_LLM_MODEL" not in text, str(prompt_path)


def test_execute_prompt_frontloads_conditional_gmas_pre_write_contract():
    prompt = (PROMPTS_DIR / "execute.system.md").read_text(encoding="utf-8")
    gate = prompt.split("## Required Workflow", 1)[0]

    assert "Domain-specific GMAS/LLM-agent gate" in gate
    assert "Skip this section for ordinary non-agent, non-LLM workspaces" in gate
    assert "get_gmas_context(query=...)" in gate
    assert "search_gmas_knowledge(query=...)" in gate
    assert "before the first workspace write" in gate
    assert "Do not wait for `apply_workspace_patch`" in gate


def test_gmas_knowledge_tools_available_before_execute():
    from umbrella.phases.loader import load_manifest

    for name in ("research", "plan", "plan_review"):
        manifest = load_manifest(MANIFESTS_DIR / f"{name}.yaml")
        assert "get_gmas_context" in manifest.allowed_tools
        assert "search_gmas_knowledge" in manifest.allowed_tools


def test_workspace_execution_manifests_do_not_delegate_code_edits_to_claude():
    from umbrella.phases.loader import load_manifest

    for name in ("execute", "subtask_template"):
        manifest = load_manifest(MANIFESTS_DIR / f"{name}.yaml")
        assert "claude_code_edit" not in manifest.allowed_tools
        for rule in manifest.permissions.rules:
            if rule.action == "allow":
                assert "claude_code_edit" not in set(rule.tools or [])


def test_phase_manifest_tools_exist_in_ouroboros_registry():
    from umbrella.phases.registry import PhaseRegistry
    from umbrella.phases.tool_contract import validate_phase_tool_contract

    repo_root = MANIFESTS_DIR.parents[2]
    reg = PhaseRegistry(MANIFESTS_DIR)
    errors = validate_phase_tool_contract(reg.all(), repo_root=repo_root)
    assert not errors, "Phase tool contract errors:\n" + "\n".join(errors)


def test_phase_tool_contract_finds_nested_ouroboros_from_repo_root_only():
    import sys
    from umbrella.phases.tool_contract import registered_ouroboros_tool_names

    repo_root = MANIFESTS_DIR.parents[2]
    original_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "ouroboros" or name.startswith("ouroboros.")
    }
    try:
        for name in list(sys.modules):
            if name == "ouroboros" or name.startswith("ouroboros."):
                sys.modules.pop(name, None)
        sys.path[:] = [
            str(repo_root),
            *[
                path
                for path in original_path
                if pathlib.Path(path or ".").resolve()
                != (repo_root / "ouroboros").resolve()
            ],
        ]
        import ouroboros

        assert not hasattr(ouroboros, "tools")
        names = registered_ouroboros_tool_names(repo_root)
        assert "propose_phase_plan" in names
    finally:
        for name in list(sys.modules):
            if name == "ouroboros" or name.startswith("ouroboros."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = original_path


def test_phase_manifest_skills_exist_in_skill_library():
    from umbrella.phases.registry import PhaseRegistry

    reg = PhaseRegistry(MANIFESTS_DIR)
    missing: list[str] = []
    for manifest in reg.all():
        for skill in sorted(manifest.allowed_skills):
            skill_path = SKILLS_DIR / skill / "SKILL.md"
            if not skill_path.exists():
                missing.append(f"{manifest.id}: {skill}")
    assert not missing, "Phase manifest references missing skill(s):\n" + "\n".join(
        missing
    )


def test_phase_manifest_skill_recommendations_are_loadable():
    from umbrella.phases.registry import PhaseRegistry

    reg = PhaseRegistry(MANIFESTS_DIR)
    broken: list[str] = []
    for manifest in reg.all():
        if not manifest.allowed_skills:
            continue
        if "load_skill" not in manifest.allowed_tools:
            broken.append(f"{manifest.id}: missing load_skill in allowed_tools")
        if "load_skill" in manifest.forbidden_tools:
            broken.append(f"{manifest.id}: load_skill is forbidden")
    assert not broken, (
        "Manifests with allowed_skills must allow load_skill because the phase "
        "prompt tells agents to load recommended skills:\n" + "\n".join(broken)
    )


def test_phase_manifest_payload_is_json_serializable():
    from umbrella.phases.registry import PhaseRegistry

    reg = PhaseRegistry(MANIFESTS_DIR)
    for manifest in reg.all():
        json.dumps(manifest.to_payload(), ensure_ascii=False)
