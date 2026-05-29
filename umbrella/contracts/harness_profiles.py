"""Typed harness profile catalog for phase context and enforcement.

Harness profiles are small orchestration contracts. They are not skills and not
domain memory: planning sees a compact catalog, execution receives only the
profiles selected for the active subtask, and guards consume validator flags.
"""


from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


HARNESS_PROFILE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class HarnessProfile:
    id: str
    title: str
    summary: str
    applies_to: tuple[str, ...] = ()
    planner_contract: tuple[str, ...] = ()
    execute_contract: tuple[str, ...] = ()
    validator_flags: tuple[str, ...] = ()
    memory_scope_hints: tuple[str, ...] = ()
    probe_required_capabilities: tuple[str, ...] = ()

    def catalog_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "applies_to": list(self.applies_to),
            "probe_required_capabilities": list(self.probe_required_capabilities),
        }

    def contract_payload(self, *, reason: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "reason": reason,
            "planner_contract": list(self.planner_contract),
            "execute_contract": list(self.execute_contract),
            "validator_flags": list(self.validator_flags),
            "memory_scope_hints": list(self.memory_scope_hints),
            "probe_required_capabilities": list(self.probe_required_capabilities),
        }


_PROFILES: dict[str, HarnessProfile] = {
    "python_src_layout": HarnessProfile(
        id="python_src_layout",
        title="Python src layout",
        summary=(
            "Greenfield Python application/library work uses one canonical "
            "src/<package>/ package root, tests under tests/, and package "
            "entrypoints under the package rather than root ad-hoc scripts."
        ),
        applies_to=("greenfield Python packages", "Python CLI/apps/libraries"),
        planner_contract=(
            "Declare production modules under exactly one src/<package>/ root.",
            "Use src/<package>/cli.py or src/<package>/__main__.py plus project scripts for launch entrypoints.",
            "Keep tests under tests/ and make proofs runnable from the workspace root.",
        ),
        execute_contract=(
            "If an accepted path conflicts with src layout policy, mutate the subtask scope instead of retrying the same write.",
            "Do not add one-off diagnostic Python scripts to the workspace root, docs/, or src/scripts/.",
        ),
        validator_flags=("python_src_layout",),
        memory_scope_hints=("load package layout notes before editing package roots",),
    ),
    "desktop_gui_headless": HarnessProfile(
        id="desktop_gui_headless",
        title="Desktop GUI headless proof",
        summary=(
            "Native desktop GUI work is proven through headless behavioral "
            "tests of state, callbacks, labels, and launch wiring; real display "
            "roots are reserved for optional smoke/e2e when runtime supports it."
        ),
        applies_to=("tkinter", "PyQt/PySide", "wxPython", "native desktop GUI"),
        planner_contract=(
            "Set proof.harness_profile to desktop_gui_headless for native GUI unit-proof leaves.",
            "Plan a testable adapter/model/controller boundary for labels, callbacks, state transitions, and invalid-input behavior.",
            "Use real-display launch only as a separate smoke/e2e check when capability probes show it is available.",
        ),
        execute_contract=(
            "Keep unit proof headless: assert behavior through adapter/model/controller APIs.",
            "Do not create a real native GUI root inside proof tests; repair production design instead of skipping tests.",
            "If display launch is needed, make it a separate runtime smoke proof gated by capability.",
        ),
        validator_flags=("no_native_gui_root_in_unit_proof",),
        memory_scope_hints=("prefer local GUI architecture notes and controller contracts",),
    ),
    "desktop_gui_runtime": HarnessProfile(
        id="desktop_gui_runtime",
        title="Desktop GUI runtime proof",
        summary=(
            "Native desktop GUI smoke/e2e work launches the real application "
            "under an explicit display/automation capability, exercises visible "
            "UI behavior, captures command/log/screenshot evidence, and cleans "
            "up the process."
        ),
        applies_to=(
            "tkinter real-window smoke",
            "PyQt/PySide real-window smoke",
            "wxPython real-window smoke",
            "native desktop GUI automation",
        ),
        planner_contract=(
            "Set proof.harness_profile to desktop_gui_runtime only when capability_declaration marks desktop_gui_runtime available.",
            "Include proof.required_capabilities with desktop_gui_runtime plus the language/runtime capabilities needed by the proof command.",
            "Use proof.execution.command as the managed launch command; put structured readiness, optional assert/interaction/driver command, evidence expectations, timeout, and cleanup notes in proof.harness_options.",
            "Keep runtime smoke/e2e separate from headless controller/model unit-proof leaves.",
        ),
        execute_contract=(
            "Real native GUI roots are allowed only inside this runtime smoke/e2e proof path.",
            "Launch the app through the accepted proof command, wait for readiness, perform visible interactions, and assert observable UI/output behavior.",
            "Capture machine-readable evidence such as command output, screenshot path, event log, or toolkit state; clean up windows/processes on timeout.",
            "If the display/automation capability is absent, mutate the plan back to a headless GUI proof or loop to research/capability declaration instead of faking the runtime.",
        ),
        validator_flags=("native_gui_runtime_proof",),
        memory_scope_hints=(
            "preload GUI runtime/tooling notes and active harness_options before writing runtime proof",
            "load any skill or MCP refs declared by the planner for desktop automation",
        ),
        probe_required_capabilities=("desktop_gui_runtime",),
    ),
    "web_ui_browser": HarnessProfile(
        id="web_ui_browser",
        title="Web UI browser proof",
        summary=(
            "Web UI work uses bootable local runtime proof and browser/http "
            "checks for visible behavior, navigation, and interactions."
        ),
        applies_to=("localhost web apps", "browser UI", "HTTP UI flows"),
        planner_contract=(
            "Use http_boot or behavioral_http/browser proof for user-facing web work.",
            "Declare server start, readiness, and interaction targets explicitly.",
        ),
        execute_contract=(
            "Prefer the browser/runtime proof path over import-only or DOM-free claims.",
            "Keep screenshots/logs/evidence tied to the active subtask proof.",
        ),
        validator_flags=("web_ui_browser_runtime",),
    ),
    "llm_runtime": HarnessProfile(
        id="llm_runtime",
        title="LLM runtime contract",
        summary=(
            "LLM/agent behavior uses the probed inherited runtime and retrieved "
            "API facts; it must not silently replace model behavior with mocks, "
            "static choices, or hardcoded fallbacks."
        ),
        applies_to=("LLM agents", "judges", "GMAS", "prompt/runtime behavior"),
        planner_contract=(
            "Declare required runtime capabilities and behavioral proof for LLM/agent leaves.",
            "Use retrieval-backed API symbols rather than guessed imports or constructors.",
        ),
        execute_contract=(
            "Call the GMAS/LLM knowledge tools before the first write on matching subtasks.",
            "Use public runtime aliases and fail explicitly when required runtime is absent.",
        ),
        validator_flags=("llm_runtime_real_capability",),
        memory_scope_hints=("preload matching GMAS/API references when available",),
    ),
    "cli_app": HarnessProfile(
        id="cli_app",
        title="CLI application proof",
        summary=(
            "Command-line apps expose a package entrypoint/script and prove "
            "observable command behavior through argv-level tests."
        ),
        applies_to=("CLI apps", "console scripts", "package launchers"),
        planner_contract=(
            "Prefer project scripts or python -m <package> over root main.py in greenfield src layouts.",
            "Use behavioral CLI tests that assert stdout/stderr/exit behavior.",
        ),
        execute_contract=(
            "Keep launch wiring thin and test command behavior through stable argv entrypoints.",
            "Do not close CLI leaves with import-only proof.",
        ),
        validator_flags=("cli_behavioral_entrypoint",),
    ),
}


def all_harness_profiles() -> tuple[HarnessProfile, ...]:
    return tuple(_PROFILES.values())


def get_harness_profile(profile_id: str) -> HarnessProfile | None:
    return _PROFILES.get(str(profile_id or "").strip())


def known_harness_profile_ids() -> tuple[str, ...]:
    return tuple(_PROFILES.keys())


def validator_flags_for_profile(profile_id: str) -> frozenset[str]:
    profile = get_harness_profile(profile_id)
    if profile is None:
        return frozenset()
    return frozenset(profile.validator_flags)


def probe_required_capability_ids() -> frozenset[str]:
    required: set[str] = set()
    for profile in all_harness_profiles():
        required.update(profile.probe_required_capabilities)
    return frozenset(required)


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("/")


def _iter_path_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = _norm_path(value)
        if text:
            yield text
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        for item in value:
            text = _norm_path(item)
            if text:
                yield text


def _subtask_paths(subtask: dict[str, Any] | None) -> list[str]:
    if not isinstance(subtask, dict):
        return []
    paths: list[str] = []
    for key in ("files_to_create", "files_to_change", "files_affected"):
        paths.extend(_iter_path_values(subtask.get(key)))
    proof = subtask.get("proof")
    scope = proof.get("scope") if isinstance(proof, dict) else None
    if isinstance(scope, dict):
        for key in ("files_under_test", "changed_files_expected", "pytest_targets"):
            paths.extend(_iter_path_values(scope.get(key)))
    return paths


def _proof_payload(subtask: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(subtask, dict):
        return {}
    proof = subtask.get("proof")
    return proof if isinstance(proof, dict) else {}


def explicit_harness_ids_from_subtask(subtask: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(subtask, dict):
        return ()
    raw_values: list[Any] = [
        subtask.get("harness_profile"),
        subtask.get("harness_profiles"),
        subtask.get("harness_id"),
    ]
    harness = subtask.get("harness")
    if isinstance(harness, dict):
        raw_values.append(harness.get("id"))
    proof = _proof_payload(subtask)
    raw_values.extend(
        [
            proof.get("harness_profile"),
            proof.get("harness_profiles"),
            proof.get("harness_id"),
        ]
    )
    proof_harness = proof.get("harness")
    if isinstance(proof_harness, dict):
        raw_values.append(proof_harness.get("id"))
    ids: list[str] = []
    for raw in raw_values:
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text in _PROFILES and text not in ids:
                ids.append(text)
    return tuple(ids)


def _safe_default_harness_ids_for_subtask(
    subtask: dict[str, Any] | None,
) -> tuple[str, ...]:
    if not isinstance(subtask, dict):
        return ()
    ids: list[str] = []
    paths = _subtask_paths(subtask)
    if any(path.endswith(".py") for path in paths) or "pyproject.toml" in paths:
        ids.append("python_src_layout")
    return tuple(ids)


def active_harness_ids_for_subtask(
    subtask: dict[str, Any] | None,
) -> tuple[str, ...]:
    ids: list[str] = []
    for profile_id in explicit_harness_ids_from_subtask(subtask):
        if profile_id not in ids:
            ids.append(profile_id)
    for profile_id in _safe_default_harness_ids_for_subtask(subtask):
        if profile_id not in ids:
            ids.append(profile_id)
    return tuple(ids)


def suggest_harness_ids_for_planning(
    subtask: dict[str, Any] | None,
    *,
    phase_id: str = "",
    capability_envelope: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    if not isinstance(subtask, dict):
        return ()
    ids = list(explicit_harness_ids_from_subtask(subtask))
    paths = _subtask_paths(subtask)
    proof = _proof_payload(subtask)
    execution = proof.get("execution") if isinstance(proof.get("execution"), dict) else {}
    command = " ".join(str(part) for part in execution.get("command") or [])
    required_cap_ids = {
        str(item).strip().lower()
        for item in (proof.get("required_capabilities") or [])
        if str(item).strip()
    }
    required_caps = " ".join(sorted(required_cap_ids))
    harness_options = proof.get("harness_options")
    harness_options_text = ""
    if isinstance(harness_options, dict):
        harness_options_text = " ".join(
            str(value) for value in harness_options.values() if value is not None
        )
    text = " ".join(
        [
            str(phase_id),
            str(subtask.get("id") or ""),
            str(subtask.get("title") or ""),
            str(subtask.get("goal") or ""),
            " ".join(paths),
            command,
            required_caps,
            harness_options_text,
        ]
    ).lower()

    def add(profile_id: str) -> None:
        if profile_id not in ids and profile_id in _PROFILES:
            ids.append(profile_id)

    if any(path.endswith(".py") for path in paths) or "pyproject.toml" in paths:
        add("python_src_layout")
    runtime_gui_markers = (
        "desktop_gui_runtime" in text
        or "gui_runtime" in text
        or "gui automation" in text
        or "gui_automation" in text
        or "native gui runtime" in text
        or "real-window" in text
        or "real window" in text
        or "display server" in text
        or "screenshot" in text
        or "pyautogui" in text
        or ("click" in text and ("window" in text or "gui" in text))
    )
    gui_markers = (
        "tkinter" in text
        or "customtkinter" in text
        or "pyqt" in text
        or "pyside" in text
        or "wxpython" in text
        or "desktop gui" in text
        or "native gui" in text
        or (
            any(path.endswith("/gui.py") or path.endswith("gui.py") for path in paths)
            and "pytest" in command.lower()
            and "http" not in text
        )
    )
    if runtime_gui_markers:
        add("desktop_gui_runtime")
    if gui_markers and "desktop_gui_runtime" not in ids:
        add("desktop_gui_headless")
    if (
        "playwright" in text
        or "browser" in text
        or "localhost" in text
        or "http_boot" in text
        or "behavioral_http" in text
    ):
        add("web_ui_browser")
    if required_cap_ids & {"llm_api", "multi_agent_gmas", "gmas"}:
        add("llm_runtime")
    if any(
        marker in text
        for marker in (
            "cli.py",
            "__main__.py",
            "project.scripts",
            "console_script",
            "entrypoint",
            "command line",
        )
    ):
        add("cli_app")

    caps = capability_envelope or {}
    if isinstance(caps, dict):
        if _capability_envelope_includes_model_runtime(caps):
            add("llm_runtime")
    return tuple(ids[:3])


def infer_harness_ids_for_subtask(
    subtask: dict[str, Any] | None,
    *,
    phase_id: str = "",
    capability_envelope: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Backward-compatible planner suggestion helper.

    This is intentionally not an execute/control-plane authority. Execute
    contexts and validator flags use explicit proof.harness_profile plus safe
    structural defaults only.
    """

    return suggest_harness_ids_for_planning(
        subtask,
        phase_id=phase_id,
        capability_envelope=capability_envelope,
    )


def _capability_entry_available(value: Any) -> bool:
    if isinstance(value, dict):
        available = value.get("available")
        if available is True:
            return True
        if str(available).strip().lower() in {"true", "yes", "1"}:
            return True
        return False
    return value is True or str(value).strip().lower() in {"true", "yes", "1"}


def _capability_envelope_includes_model_runtime(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if key_text in {"llm_api", "multi_agent_gmas", "gmas"}:
                if _capability_entry_available(item):
                    return True
                continue
            if _capability_envelope_includes_model_runtime(item):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(_capability_envelope_includes_model_runtime(item) for item in value)
    text = str(value or "").strip().lower()
    return text in {"llm_api", "multi_agent_gmas", "gmas"}


def build_harness_contract_payload(
    *,
    phase_id: str,
    active_subtask: dict[str, Any] | None = None,
    capability_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase = str(phase_id or "")
    if phase in {"plan", "plan_review"}:
        caps = capability_envelope if isinstance(capability_envelope, dict) else {}
        include_model_runtime = _capability_envelope_includes_model_runtime(caps)
        profiles = [
            profile.catalog_payload()
            for profile in all_harness_profiles()
            if include_model_runtime or profile.id != "llm_runtime"
        ]
        return {
            "schema_version": HARNESS_PROFILE_SCHEMA_VERSION,
            "mode": "catalog",
            "selected_ids": [],
            "reason": "planning phases receive compact selectable profile catalog",
            "profiles": profiles,
        }
    if phase == "execute" and active_subtask:
        selected = active_harness_ids_for_subtask(active_subtask)
        return {
            "schema_version": HARNESS_PROFILE_SCHEMA_VERSION,
            "mode": "active",
            "selected_ids": list(selected),
            "reason": (
                "active execute subtask explicit profile selection plus safe "
                "structural defaults"
            ),
            "profiles": [
                _PROFILES[profile_id].contract_payload(reason="selected for active subtask")
                for profile_id in selected
                if profile_id in _PROFILES
            ],
        }
    return {
        "schema_version": HARNESS_PROFILE_SCHEMA_VERSION,
        "mode": "none",
        "selected_ids": [],
        "reason": "no harness profile applies to this phase context",
        "profiles": [],
    }


def render_harness_contract_markdown(payload: dict[str, Any]) -> str:
    mode = str(payload.get("mode") or "none")
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
    if not profiles or mode == "none":
        return ""
    lines: list[str] = []
    if mode == "catalog":
        lines.append("Planner harness profile catalog:")
        lines.append(
            "Set `proof.harness_profile` to one matching id when a subtask needs profile-specific tools, memory, or guards. Do not apply inactive profiles."
        )
        for profile in profiles:
            suffix = ""
            probe_required = profile.get("probe_required_capabilities") or []
            if probe_required:
                suffix = " Probe-required capabilities: " + ", ".join(
                    f"`{item}`" for item in probe_required
                ) + "."
            lines.append(f"- `{profile.get('id')}`: {profile.get('summary')}{suffix}")
        return "\n".join(lines)
    lines.append("Active harness contract:")
    lines.append(
        "Umbrella selected these profiles for the current subtask. They guide proof shape and validator flags, but the agent still decides the implementation."
    )
    for profile in profiles:
        lines.append(f"### {profile.get('id')}: {profile.get('title')}")
        summary = str(profile.get("summary") or "").strip()
        if summary:
            lines.append(summary)
        execute_contract = profile.get("execute_contract") or []
        for item in execute_contract[:4]:
            lines.append(f"- {item}")
        flags = profile.get("validator_flags") or []
        if flags:
            lines.append("Validator flags: " + ", ".join(f"`{flag}`" for flag in flags))
        probe_required = profile.get("probe_required_capabilities") or []
        if probe_required:
            lines.append(
                "Probe-required capabilities: "
                + ", ".join(f"`{item}`" for item in probe_required)
            )
        memory_hints = profile.get("memory_scope_hints") or []
        if memory_hints:
            lines.append("Memory hints: " + "; ".join(str(item) for item in memory_hints[:2]))
    return "\n".join(lines)


def validator_flags_from_harness_payload(payload: dict[str, Any] | None) -> frozenset[str]:
    if not isinstance(payload, dict):
        return frozenset()
    flags: set[str] = set()
    for profile in payload.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        for flag in profile.get("validator_flags") or []:
            text = str(flag or "").strip()
            if text:
                flags.add(text)
    return frozenset(flags)


def validator_flags_from_overlays(overlays: dict[str, Any] | None) -> frozenset[str]:
    if not isinstance(overlays, dict):
        return frozenset()
    direct = overlays.get("harness_contract")
    if isinstance(direct, dict):
        flags = validator_flags_from_harness_payload(direct)
        if flags:
            return flags
    bundle = overlays.get("llm_input_bundle")
    if isinstance(bundle, dict):
        return validator_flags_from_harness_payload(bundle.get("harness_contract"))
    return frozenset()


def validator_flags_for_subtask(subtask: dict[str, Any] | None) -> frozenset[str]:
    flags: set[str] = set()
    for profile_id in active_harness_ids_for_subtask(subtask):
        flags.update(validator_flags_for_profile(profile_id))
    return frozenset(flags)
