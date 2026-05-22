"""ProactiveMemoryCompiler — assemble always-loaded overlay before agent action."""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from umbrella.memory.paths import manager_core_root, workspace_core_root
from umbrella.memory.proactive.bkb import (
    filter_active_rules,
    format_bkb_section,
    load_bkb_rules,
    resolve_bkb_conflicts,
)
from umbrella.memory.proactive.budget import (
    estimate_tokens,
    resolve_proactive_budget,
    trim_sections_to_budget,
)
from umbrella.memory.proactive.core_files import (
    ensure_core_seed_files,
    load_manager_core,
    load_workspace_core,
)
from umbrella.memory.proactive.models import OverlaySection, ProactiveMemoryOverlay
from umbrella.memory.proactive.overlays import (
    build_phase_state_section,
    phase_policy,
    select_sections_by_policy,
)


class ProactiveMemoryCompiler:
    def build_overlay(
        self,
        *,
        repo_root: Path,
        workspace_id: str,
        run_id: str,
        phase_id: str,
        subtask_id: str | None,
        task_brief: str,
        manifest: Any,
        token_budget: int | None = None,
        drive_root: Path | None = None,
    ) -> ProactiveMemoryOverlay:
        manifest_budget = int(getattr(getattr(manifest, "budgets", None), "max_tokens", 0) or 0)
        budget = token_budget or resolve_proactive_budget(
            phase=phase_id,
            manifest_budget=manifest_budget,
            env_override=os.environ.get("UMBRELLA_PROACTIVE_MEMORY_BUDGET"),
        )

        manager_root = manager_core_root(repo_root)
        ensure_core_seed_files(manager_root, kind="manager")
        sections: list[OverlaySection] = []
        sections.extend(load_manager_core(manager_root, budget_tokens=min(1500, budget // 3)))

        if workspace_id.strip():
            ws_root = workspace_core_root(repo_root, workspace_id)
            ensure_core_seed_files(ws_root, kind="workspace")
            sections.extend(load_workspace_core(ws_root, budget_tokens=min(2000, budget // 2)))

        bkb_rules = load_bkb_rules(manager_root / "bkb.yaml")
        if workspace_id.strip():
            bkb_rules.extend(load_bkb_rules(workspace_core_root(repo_root, workspace_id) / "bkb.yaml"))

        active = filter_active_rules(
            bkb_rules,
            workspace_id=workspace_id,
            phase_id=phase_id,
        )
        resolved, conflicts = resolve_bkb_conflicts(active)
        bkb_text, bkb_refs = format_bkb_section(resolved, max_chars=min(2000, budget // 2))
        if bkb_text:
            sections.append(
                OverlaySection(
                    name="BKB (verified behavior rules)",
                    content=bkb_text,
                    source_refs=bkb_refs,
                    trust="verified",
                    token_count=estimate_tokens(bkb_text),
                )
            )

        run_state = self._build_run_state(
            drive_root=drive_root,
            run_id=run_id,
            workspace_id=workspace_id,
            max_tokens=min(800, budget // 4),
        )
        if run_state:
            sections.append(
                OverlaySection(
                    name="Current run state",
                    content=run_state,
                    source_refs=[f"run:{run_id}"] if run_id else [],
                    trust="summarized",
                    token_count=estimate_tokens(run_state),
                )
            )

        phase_text = build_phase_state_section(
            phase_id=phase_id,
            manifest_description=str(getattr(manifest, "description", "") or ""),
            task_brief=task_brief,
            active_risks=self._extract_risks(sections),
            forbidden_repeats=self._extract_forbidden(resolved),
            open_threads=self._extract_open_threads(sections),
            max_tokens=min(600, budget // 5),
        )
        sections.append(
            OverlaySection(
                name="Phase commitments",
                content=phase_text,
                source_refs=[f"phase:{phase_id}"],
                trust="curated",
                token_count=estimate_tokens(phase_text),
            )
        )

        policy = phase_policy(phase_id)
        sections = select_sections_by_policy(sections, policy)
        sections = trim_sections_to_budget(sections, budget, phase_id=phase_id)

        archive_hints = self._archive_hints(
            phase_id=phase_id,
            workspace_id=workspace_id,
            verification_count=self._count_durable_verification_reports(repo_root, workspace_id),
        )

        total_tokens = sum(s.token_count for s in sections)
        overlay = ProactiveMemoryOverlay(
            sections=sections,
            conflicts=conflicts,
            archive_hints=archive_hints,
            telemetry={
                "memory_overlay_tokens": total_tokens,
                "core_memory_tokens": total_tokens,
                "bkb_rules_injected": len(resolved),
                "memory_overlay_conflicts_count": len(conflicts),
                "phase_policy": policy,
                "proactive_budget": budget,
            },
        )
        return overlay

    def build_minimal_overlay(
        self,
        *,
        repo_root: Path,
        workspace_id: str = "",
        phase_id: str = "",
    ) -> ProactiveMemoryOverlay:
        """Task-start overlay: identity + optional workspace + BKB only."""
        budget = resolve_proactive_budget(phase=phase_id, manifest_budget=0)
        return self.build_overlay(
            repo_root=repo_root,
            workspace_id=workspace_id,
            run_id="",
            phase_id=phase_id or "task_start",
            subtask_id=None,
            task_brief="",
            manifest=_MinimalManifest(),
            token_budget=min(budget, 3000),
        )

    @staticmethod
    def _build_run_state(
        *,
        drive_root: Path | None,
        run_id: str,
        workspace_id: str,
        max_tokens: int,
    ) -> str:
        if drive_root is None or not drive_root.is_dir():
            return ""
        lines: list[str] = []
        if run_id:
            lines.append(f"run_id: {run_id}")
        if workspace_id:
            lines.append(f"workspace_id: {workspace_id}")
        for rel in (
            "state/phase_plan.json",
            "TASK_MAIN.md",
        ):
            path = drive_root / rel
            if path.is_file():
                try:
                    snippet = path.read_text(encoding="utf-8", errors="replace")[:1200]
                    lines.append(f"--- {rel} ---\n{snippet}")
                except OSError:
                    pass
        text = "\n".join(lines).strip()
        max_chars = max_tokens * 4
        if len(text) > max_chars:
            text = text[: max_chars - 20].rstrip() + "\n...[run state truncated]"
        return text

    @staticmethod
    def _count_durable_verification_reports(repo_root: Path, workspace_id: str) -> int:
        try:
            from umbrella.memory.palace.facade import MemPalace

            palace = MemPalace(repo_root, workspace_id or None)
            try:
                hits = palace.search(
                    "verification",
                    stores=["palace.durable"],
                    tags_any=["verification_report"],
                    n=50,
                )
                return len(hits)
            finally:
                palace.close()
        except Exception:
            return 0

    @staticmethod
    def _archive_hints(
        *,
        phase_id: str,
        workspace_id: str,
        verification_count: int,
    ) -> list[str]:
        hints: list[str] = []
        if verification_count > 2:
            hints.append(
                f"{verification_count} verification reports in palace.durable; "
                "only promoted BKB/core lessons appear above."
            )
        if phase_id == "research":
            hints.append("Use palace_search for prior research findings (archive, not directive).")
        if workspace_id:
            hints.append(f"Workspace archive: workspaces/{workspace_id}/.memory/palace/")
        return hints[:5]

    @staticmethod
    def _extract_risks(sections: list[OverlaySection]) -> list[str]:
        for section in sections:
            if "risk" in section.name.lower():
                return [
                    line.strip("- ").strip()
                    for line in section.content.splitlines()
                    if line.strip().startswith("-")
                ][:8]
        return []

    @staticmethod
    def _extract_forbidden(rules: list[Any]) -> list[str]:
        out: list[str] = []
        for rule in rules:
            if getattr(rule, "rule_type", "") == "anti_pattern":
                forbidden = (getattr(rule, "rule", None) or {}).get("forbidden")
                if forbidden:
                    out.append(str(forbidden))
        return out[:8]

    @staticmethod
    def _extract_open_threads(sections: list[OverlaySection]) -> list[str]:
        for section in sections:
            if "open thread" in section.name.lower():
                return [
                    line.strip("- ").strip()
                    for line in section.content.splitlines()
                    if line.strip().startswith("-")
                ][:6]
        return []


class _MinimalManifest:
    description = ""
    budgets = SimpleNamespace(max_tokens=8000)
