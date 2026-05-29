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
    assert "loop_back_to" in manifest.allowed_tools
    assert "palace_add" not in manifest.allowed_tools
    assert manifest.exit_criteria.min_palace_writes == ()
    assert "harness_run" not in manifest.allowed_tools
    assert "enable_tools" not in manifest.allowed_tools
    assert "register_temp_tool" not in manifest.allowed_tools


def test_phase_plan_tool_schema_exposes_context_fields():
    from umbrella.deep_agent_tools.phase_contract_tools import get_tools

    tools = {tool.name: tool.schema for tool in get_tools()}
    plan_schema = tools["propose_phase_plan"]["parameters"]["properties"]["plan"]
    subtask_props = plan_schema["properties"]["subtasks"]["items"]["properties"]

    assert plan_schema["required"] == ["subtasks"]
    assert plan_schema["properties"]["subtasks"]["minItems"] == 1
    assert "phases" not in plan_schema["properties"]
    assert "steps" not in plan_schema["properties"]
    assert "proof" in subtask_props
    assert "memory_scope" in subtask_props
    assert "allowed_tools" in subtask_props
    assert "allowed_skills" in subtask_props
    assert "codeptr_refs" in subtask_props
    assert "mcp_refs" in subtask_props


def test_execute_schedules_subtask_review_manifest():
    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(MANIFESTS_DIR / "execute.yaml")
    assert manifest.mini_review_after == "subtask_review"


def test_execute_manifests_expose_single_typed_plan_revision_tool():
    from umbrella.phases.loader import load_manifest

    for name in ("execute", "subtask_template"):
        manifest = load_manifest(MANIFESTS_DIR / f"{name}.yaml")
        assert "apply_plan_revision_patch" in manifest.allowed_tools
        assert "mutate_phase_plan" not in manifest.allowed_tools


def test_mutate_phase_plan_schema_does_not_teach_legacy_migration_fields():
    from umbrella.deep_agent_tools.phase_control_tools import get_tools

    tools = {tool.name: tool.schema for tool in get_tools()}
    schema_text = json.dumps(tools["mutate_phase_plan"], ensure_ascii=False)

    assert "contract_migration_reason" not in schema_text
    assert "contract_migration_files" not in schema_text


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


def test_plan_prompt_requires_generated_oracle_claims_for_generated_tests():
    prompt = (PROMPTS_DIR / "plan.system.md").read_text(encoding="utf-8")

    assert '"generated_test_contract"' in prompt
    assert '"oracle_claims"' in prompt
    assert '"claim_id"' in prompt
    assert '"test_refs"' in prompt
    assert "at most 6 generated oracle claims" in prompt


def test_plan_prompt_does_not_target_workspace_toml_as_subtask_file():
    prompt = (PROMPTS_DIR / "plan.system.md").read_text(encoding="utf-8")

    assert "Do not declare `workspace.toml`" in prompt
    assert "declare `workspace.toml` and `pyproject.toml`" not in prompt


def test_plan_review_coverage_means_dimension_checked():
    prompt = (PROMPTS_DIR / "plan_review.system.md").read_text(encoding="utf-8")

    assert 'Coverage keys mean "this dimension was checked"' in prompt
    assert "including dimensions where blockers were found" in prompt


def test_prompts_route_domain_rules_through_harness_profiles():
    plan_prompt = (PROMPTS_DIR / "plan.system.md").read_text(encoding="utf-8")
    review_prompt = (PROMPTS_DIR / "plan_review.system.md").read_text(encoding="utf-8")
    execute_prompt = (PROMPTS_DIR / "execute.system.md").read_text(encoding="utf-8")

    assert "proof.harness_profile" in plan_prompt
    assert "Umbrella harness profile catalog" in plan_prompt
    assert "desktop_gui_runtime" in plan_prompt
    assert "memory_scope" in plan_prompt
    assert "known `proof.harness_profile`" in review_prompt
    assert "desktop_gui_runtime" in review_prompt
    assert "active Umbrella harness contract" in execute_prompt
    assert "desktop_gui_runtime" in execute_prompt
    assert "tk.Tk()" not in execute_prompt


def test_plan_review_prompt_accepts_no_test_tampering_property():
    prompt = (PROMPTS_DIR / "plan_review.system.md").read_text(encoding="utf-8")

    assert "`no_test_tampering` is a valid `oracle.required_properties` entry" in prompt
    assert "do not reject it merely because it appears in `required_properties`" in prompt
    assert "Pure test-verification subtasks" in prompt
    assert "Do not ask to remove `no_test_tampering`" in prompt


def test_review_prompts_expose_bad_generated_oracle_issue_codes():
    plan_review_prompt = (PROMPTS_DIR / "plan_review.system.md").read_text(encoding="utf-8")
    subtask_review_prompt = (PROMPTS_DIR / "subtask_review.system.md").read_text(encoding="utf-8")

    for prompt in (plan_review_prompt, subtask_review_prompt):
        assert "bad_generated_oracle" in prompt
        assert "invalid_generated_test_contract" in prompt
        assert "required_deltas" in prompt


def test_plan_review_prompt_matches_desktop_runtime_validator():
    prompt = (PROMPTS_DIR / "plan_review.system.md").read_text(encoding="utf-8")

    assert "`proof.execution.kind` is `command`" in prompt
    assert "Do not request `pytest` as the primary proof command" in prompt
    assert "harness_options.assert_command" in prompt


def test_research_prompt_tells_agents_not_to_mark_pending_probe_unavailable():
    prompt = (PROMPTS_DIR / "research.system.md").read_text(encoding="utf-8")

    assert "omit that capability's `available` field" in prompt
    assert "Do not set `available=false` for a pending probe" in prompt
    assert "omit `capabilities.desktop_gui_runtime.available`" in prompt


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


def test_execute_prompt_does_not_frontload_conditional_gmas_pre_write_contract():
    prompt = (PROMPTS_DIR / "execute.system.md").read_text(encoding="utf-8")
    gate = prompt.split("## Required Workflow", 1)[0]

    assert "Domain-specific GMAS/LLM-agent gate" not in gate
    assert "get_gmas_context(query=...)" not in gate
    assert "search_gmas_knowledge(query=...)" not in gate


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


def test_review_and_verify_manifests_have_read_only_diagnostics():
    from umbrella.phases.loader import load_manifest

    for name in ("subtask_review", "final_review", "verify"):
        manifest = load_manifest(MANIFESTS_DIR / f"{name}.yaml")
        assert "read_file" in manifest.allowed_tools
        assert "list_files" in manifest.allowed_tools
        assert "read_drive_log" in manifest.allowed_tools
        assert "read_terminal_scrollback" in manifest.allowed_tools

    final_review = load_manifest(MANIFESTS_DIR / "final_review.yaml")
    assert "run_workspace_verify" in final_review.allowed_tools
    assert "run_workspace_verify" in final_review.exit_criteria.required_prior_calls
    assert "run_real_e2e" in final_review.exit_criteria.required_prior_calls
    assert "submit_verification" not in final_review.allowed_tools
    assert "promote_to_durable" not in final_review.allowed_tools
    assert "verification-protocol" not in final_review.allowed_skills
    assert "final-review-verification" in final_review.allowed_skills


def test_phase_manifest_tools_exist_in_ouroboros_registry():
    from umbrella.phases.registry import PhaseRegistry
    from umbrella.phases.tool_contract import validate_phase_tool_contract

    repo_root = MANIFESTS_DIR.parents[2]
    reg = PhaseRegistry(MANIFESTS_DIR)
    errors = validate_phase_tool_contract(reg.all(), repo_root=repo_root)
    assert not errors, "Phase tool contract errors:\n" + "\n".join(errors)


def test_phase_tool_contract_includes_umbrella_only_tools_when_registry_shadowed():
    import sys
    from umbrella.phases.tool_contract import validate_phase_tool_contract
    from umbrella.phases.registry import PhaseRegistry

    repo_root = MANIFESTS_DIR.parents[2]
    reg = PhaseRegistry(MANIFESTS_DIR)
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
        sys.path[:] = [str(repo_root), *original_path]
        import ouroboros

        assert not hasattr(ouroboros, "tools")
        errors = validate_phase_tool_contract(reg.all(), repo_root=repo_root)
        assert not errors, "\n".join(errors)
    finally:
        for name in list(sys.modules):
            if name == "ouroboros" or name.startswith("ouroboros."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = original_path


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
