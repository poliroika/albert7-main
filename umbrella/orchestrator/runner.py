import json
import logging
import os
import pathlib
import re
import time
import uuid
from typing import Any, Callable, Iterator

from umbrella.phases.base import (
    Budgets,
    PhasePlan,
    PhaseNode,
    PhaseResult,
    PlanEdit,
    SubtaskCard,
    WatcherSignal,
)
from umbrella.phases.registry import get_registry
from umbrella.orchestrator.phase_plan import build_default_plan, save_plan, load_plan
from umbrella.orchestrator.watcher import WatcherPollLoop
from umbrella.env import watcher_budget_enforcement_enabled
from umbrella.orchestrator.worker import build_phase_task
from umbrella.memory.palace.facade import MemPalace
from umbrella.utils.result_envelope import ResultEnvelope, ErrorCode
from umbrella.utils.tool_logs import is_effective_write_tool_log_row
from umbrella.deep_agent_tools.research_provenance import (
    research_scarcity_handoff_issue,
)
from umbrella.contracts import (
    ContractBundle,
    ContractCompiler,
    ContractValidator,
    PhaseDecisionEngine,
    ProofSpec,
    build_workspace_context,
    compile_phase_plan,
    json_ready,
    validate_done_subtasks_materialized,
)

log = logging.getLogger(__name__)



class _LauncherHandle:
    """Adapter so callers can do `handle.wait()` against the legacy launcher."""

    def __init__(self, launcher: Any, task_id: str, timeout: float | None):
        self._launcher = launcher
        self._task_id = task_id
        self._timeout = timeout

    def wait(self, timeout: float | None = None) -> dict[str, Any] | None:
        wait_timeout = self._timeout if timeout is None else timeout
        result = self._launcher.wait_for_result(self._task_id, timeout=wait_timeout)
        if result is None:
            if timeout is not None:
                return None
            return {"status": "error", "error": "launcher timeout", "task_id": self._task_id}
        return result


class _DefaultLauncher:
    """Thin wrapper around `OuroborosLauncher` that returns a wait()-able handle."""

    def __init__(self, repo_root: pathlib.Path, workspace_id: str):
        from umbrella.integration.ouroboros_launcher import OuroborosLauncher

        self._launcher = OuroborosLauncher(repo_root=repo_root, workspace_id=workspace_id)
        self._launcher.start()

    def submit_task(self, task: dict[str, Any], timeout: float | None = None) -> _LauncherHandle:
        task_id = self._launcher.submit_task(task)
        return _LauncherHandle(self._launcher, task_id, timeout)

    def stop(self) -> None:
        try:
            self._launcher.stop()
        except Exception:
            log.debug("Launcher stop failed", exc_info=True)


class PhaseRunner:
    """Orchestrates a task across phases. Each phase runs the Ouroboros agent via a launcher."""

    def __init__(
        self,
        *,
        repo_root: pathlib.Path,
        workspace_id: str,
        drive_root: pathlib.Path | None = None,
        launcher: Any = None,
        palace: MemPalace | None = None,
        phase_timeout_seconds: float | None = None,
        on_envelope: Callable[[ResultEnvelope], None] | None = None,
        candidates_per_phase: int = 1,
    ) -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self._drive_root = drive_root or (
            repo_root / "workspaces" / workspace_id / ".memory" / "drive"
        )
        self._launcher = launcher
        self._owns_launcher = False
        self._palace = palace or MemPalace(repo_root, workspace_id)
        self._registry = get_registry(repo_root / "umbrella" / "phases" / "manifests")
        self._watcher = WatcherPollLoop(self._drive_root)
        self._phase_timeout_seconds = phase_timeout_seconds
        self._on_envelope = on_envelope
        self._candidates_per_phase = max(1, int(candidates_per_phase))

    def _ensure_launcher(self) -> Any:
        if self._launcher is None:
            self._launcher = _DefaultLauncher(self._repo_root, self._workspace_id)
            self._owns_launcher = True
        return self._launcher

    def _emit(self, env: ResultEnvelope) -> ResultEnvelope:
        if self._on_envelope:
            try:
                self._on_envelope(env)
            except Exception:
                log.debug("on_envelope callback failed", exc_info=True)
        return env

    def _stop_requested(self) -> bool:
        """Check the canonical stop-file location."""
        stop_path = self._drive_root / "state" / "stop_requested.json"
        return stop_path.exists()

    def _clear_pending_phase_signal(self) -> None:
        try:
            (self._drive_root / "state" / "phase_control_signal.json").unlink(
                missing_ok=True
            )
        except OSError:
            log.debug("Failed to clear stale phase control signal", exc_info=True)

    def _write_phase_budget_file(self, phase_id: str, budgets: Budgets) -> None:
        import dataclasses

        budget_path = self._drive_root / "state" / f"{phase_id}.budget.json"
        if not watcher_budget_enforcement_enabled():
            try:
                budget_path.unlink(missing_ok=True)
            except OSError:
                log.debug(
                    "Failed to remove watcher budget file for %s",
                    phase_id,
                    exc_info=True,
                )
            return

        payload = {
            key: value
            for key, value in dataclasses.asdict(budgets).items()
            if value is not None
        }
        if not payload:
            try:
                budget_path.unlink(missing_ok=True)
            except OSError:
                log.debug(
                    "Failed to remove empty watcher budget file for %s",
                    phase_id,
                    exc_info=True,
                )
            return
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = budget_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, budget_path)

    def _apply_pending_watcher_signal(
        self,
        *,
        signal: WatcherSignal,
        phase_node: PhaseNode,
        plan: PhasePlan,
        run_id: str,
        outcome: dict[str, Any],
    ) -> tuple[PhaseResult | None, ResultEnvelope | None]:
        self._watcher.mark_processed(signal.signal_id)
        kind = signal.kind
        reason = signal.reason or f"watcher:{kind}"

        if kind == "abort_phase":
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            return None, self._emit(
                ResultEnvelope.failure(
                    ErrorCode.WATCHER_ABORT,
                    signal.reason,
                    run_id=run_id,
                    phase=phase_node.id,
                )
            )

        if kind == "force_verify":
            result, envelope = self._finish_phase_loop_back(
                phase_node=phase_node,
                plan=plan,
                run_id=run_id,
                outcome=outcome,
                loop_back_target=phase_node.id,
                retry_reason=f"watcher:force_verify: {signal.reason}".strip(),
            )
            target = plan.get_node(result.loop_back_target or phase_node.id)
            if target is not None:
                overlay = dict(target.overlay or {})
                overlay["watcher_force_verify"] = True
                overlay["required_next_actions"] = [
                    "run_subtask_proof",
                    "run_workspace_verify",
                ]
                target.overlay = overlay
                save_plan(plan, self._drive_root)
            return result, envelope

        if kind == "mutate_phase_plan":
            target_id = "plan" if plan.get_node("plan") is not None else phase_node.id
            result, envelope = self._finish_phase_loop_back(
                phase_node=phase_node,
                plan=plan,
                run_id=run_id,
                outcome=outcome,
                loop_back_target=target_id,
                retry_reason=f"watcher:mutate_phase_plan: {signal.reason}".strip(),
            )
            target = plan.get_node(target_id)
            if target is not None:
                overlay = dict(target.overlay or {})
                overlay["watcher_mutate_phase_plan_request"] = signal.payload or {}
                target.overlay = overlay
                save_plan(plan, self._drive_root)
            return result, envelope

        if kind in {"restart_phase", "inject_lesson"}:
            return self._finish_phase_loop_back(
                phase_node=phase_node,
                plan=plan,
                run_id=run_id,
                outcome=outcome,
                loop_back_target=phase_node.id,
                retry_reason=reason,
            )

        return None, None

    @staticmethod
    def _tool_row_json_payload(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.get("result_preview") or row.get("result") or {}
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _gmas_tool_row_successful_context(row: dict[str, Any]) -> bool:
        payload = PhaseRunner._tool_row_json_payload(row)
        status = str(payload.get("status") or "").strip().lower()
        if status and status != "ok":
            return False
        if payload.get("error"):
            return False
        return bool(
            status == "ok"
            or payload.get("recommended_pattern")
            or payload.get("key_files")
            or payload.get("retrieval_excerpt")
        )

    @staticmethod
    def _gmas_tool_row_subtask_id(row: dict[str, Any]) -> str:
        for source in (
            row.get("args") if isinstance(row.get("args"), dict) else {},
            PhaseRunner._tool_row_json_payload(row),
        ):
            if not isinstance(source, dict):
                continue
            for key in ("active_subtask_id", "subtask_id", "current_subtask_id"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _promote_to_durable_tool_row_is_valid(row: dict[str, Any]) -> bool:
        from umbrella.memory.proactive.phase_hooks import promote_durable_row_is_valid

        payload = PhaseRunner._tool_row_json_payload(row)
        return promote_durable_row_is_valid(payload)

    def _tool_log_has_tool(
        self,
        *,
        task_id: str,
        tool_names: set[str],
        active_subtask_id: str = "",
        require_successful_context: bool = False,
    ) -> bool:
        path = self._drive_root / "logs" / "tools.jsonl"
        if not task_id or not path.exists():
            return False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if str(row.get("tool") or "") in tool_names:
                    if (
                        require_successful_context
                        and not self._gmas_tool_row_successful_context(row)
                    ):
                        continue
                    if active_subtask_id and (
                        self._gmas_tool_row_subtask_id(row) != active_subtask_id
                    ):
                        continue
                    return True
        except OSError:
            log.debug("Failed to inspect tools log for %s", task_id, exc_info=True)
        return False

    @staticmethod
    def _gmas_subtask_requires_context(raw_card: dict[str, Any]) -> bool:
        parts: list[str] = []
        for key in (
            "id",
            "subtask_id",
            "title",
            "name",
            "goal",
            "description",
            "proof",
            "files_to_create",
            "files_to_change",
            "files_affected",
        ):
            parts.append(str(raw_card.get(key) or ""))
        return bool(
            re.search(
                r"(?i)\b(?:gmas|llm|multi[-_\s]?agent|agent|agents|bot|bots|"
                r"ai[-_\s]?opponent|model[-_\s]?driven|judge)\b",
                "\n".join(parts),
            )
        )

    def _read_phase_control_records(
        self,
        *,
        task_id: str,
        phase_started_at: float | None,
    ) -> list[dict[str, Any]]:
        state_dir = self._drive_root / "state"
        records: list[dict[str, Any]] = []
        for line_path in (state_dir / "phase_control_signals.jsonl",):
            if not line_path.exists():
                continue
            try:
                for line in line_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        records.append(row)
            except OSError:
                log.debug("Failed to read phase control ledger", exc_info=True)
        single = state_dir / "phase_control_signal.json"
        if single.exists():
            try:
                row = json.loads(single.read_text(encoding="utf-8"))
                if isinstance(row, dict):
                    records.append(row)
            except (OSError, json.JSONDecodeError):
                log.debug("Failed to read current phase control signal", exc_info=True)

        filtered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in records:
            row_task_id = str(row.get("task_id") or "")
            if task_id and row_task_id and row_task_id != task_id:
                continue
            created = row.get("created_at")
            if (
                phase_started_at is not None
                and isinstance(created, (int, float))
                and float(created) < float(phase_started_at)
            ):
                continue
            signal_id = str(row.get("signal_id") or "").strip()
            if signal_id:
                dedupe_key = "signal:" + signal_id
            else:
                try:
                    dedupe_key = "row:" + json.dumps(
                        row,
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    )
                except TypeError:
                    dedupe_key = "row:" + repr(sorted(row.items()))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            filtered.append(row)
        return filtered

    @staticmethod
    def _micro_review_revision_reason(payload: dict[str, Any]) -> str:
        parts: list[str] = []
        issues = payload.get("issues")
        if isinstance(issues, list):
            for item in issues[:5]:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "").strip()
                message = str(item.get("message") or "").strip()
                if code or message:
                    parts.append(": ".join(part for part in (code, message) if part))
        notes = str(payload.get("notes") or "").strip()
        if notes:
            parts.append(notes)
        if not parts:
            return "micro review requested revisions"
        details = "; ".join(parts)
        if len(details) > 4000:
            details = details[:3997].rstrip() + "..."
        return "micro review requested revisions: " + details

    def _latest_revision_contract(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = str(outcome.get("task_id") or "")
        rows = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        for row in reversed(rows):
            if str(row.get("kind") or "") != "submit_micro_review":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if str((payload or {}).get("verdict") or "").strip().lower() != "revise":
                continue
            revisions = payload.get("revisions")
            items: list[str] = []
            if isinstance(revisions, list):
                items = [str(item).strip() for item in revisions if str(item).strip()]
            notes = str((payload or {}).get("notes") or "").strip()
            return {
                "source_phase": phase_node.id,
                "source_task_id": task_id,
                "verdict": "revise",
                "revisions": items,
                "notes": notes,
            }
        return {}

    @staticmethod
    def _merged_revision_contract(
        existing: Any,
        latest: dict[str, Any],
    ) -> dict[str, Any]:
        if not latest:
            return {}
        contracts: list[dict[str, Any]] = []
        if isinstance(existing, dict):
            contracts.append(existing)
        contracts.append(latest)
        revisions: list[str] = []
        notes: list[str] = []
        sources: list[dict[str, str]] = []
        for contract in contracts:
            raw_revisions = contract.get("revisions")
            if isinstance(raw_revisions, list):
                for item in raw_revisions:
                    text = str(item or "").strip()
                    if text and text not in revisions:
                        revisions.append(text)
            note = str(contract.get("notes") or "").strip()
            if note and note not in notes:
                notes.append(note)
            source_phase = str(contract.get("source_phase") or "").strip()
            source_task_id = str(contract.get("source_task_id") or "").strip()
            if source_phase or source_task_id:
                source = {
                    "source_phase": source_phase,
                    "source_task_id": source_task_id,
                }
                if source not in sources:
                    sources.append(source)
        merged = dict(latest)
        merged["revisions"] = revisions
        if notes:
            merged["notes"] = "\n\n".join(notes)
        if sources:
            merged["sources"] = sources
        return merged

    def _outcome_model_response_failure(self, outcome: dict[str, Any]) -> str:
        from ouroboros.model_failure import is_model_response_failure

        result_text = str(
            outcome.get("result")
            or outcome.get("final_message")
            or outcome.get("error")
            or ""
        )
        if is_model_response_failure(result_text):
            return result_text.splitlines()[0].strip() or "model response failure"
        task_id = str(outcome.get("task_id") or "").strip()
        if not task_id:
            return ""
        from umbrella.artifacts.task_ids import task_artifact_stem

        result_file = (
            self._drive_root
            / "task_results"
            / f"{task_artifact_stem(task_id)}.json"
        )
        if not result_file.exists():
            return ""
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception:
            log.debug("Failed to read task result for model-failure check", exc_info=True)
            return ""
        if str(payload.get("status") or "").lower() == "failed":
            persisted = str(payload.get("result") or "")
            if is_model_response_failure(persisted):
                return persisted.splitlines()[0].strip() or "model response failure"
        return ""

    def _phase_completion_failure(
        self,
        *,
        phase_node: PhaseNode,
        plan: PhasePlan,
        manifest: Any,
        outcome: dict[str, Any],
    ) -> str:
        required = list(getattr(manifest.exit_criteria, "required_calls", ()) or ())
        if not required:
            return ""
        task_id = str(outcome.get("task_id") or "")
        records = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        by_kind: dict[str, dict[str, Any]] = {}
        for row in records:
            kind = str(row.get("kind") or "")
            if kind:
                by_kind[kind] = row
        missing = [name for name in required if name not in by_kind]
        if missing:
            return (
                "phase exit criteria missing required call(s): "
                + ", ".join(sorted(missing))
            )

        preflight = by_kind.get("submit_preflight_report")
        if preflight and (preflight.get("payload") or {}).get("status") == "blocked":
            blockers = (preflight.get("payload") or {}).get("blockers") or []
            return "preflight reported blocked: " + ", ".join(map(str, blockers))

        verification = by_kind.get("submit_verification")
        if verification and (verification.get("payload") or {}).get("status") != "pass":
            if self._phase_loop_back_target(
                phase_node=phase_node,
                outcome=outcome,
            ):
                return ""
            details = str((verification.get("payload") or {}).get("details") or "")
            return f"verification did not pass: {details[:500]}"

        palace_rules = list(
            getattr(manifest.exit_criteria, "required_palace_writes", ()) or ()
        ) + list(getattr(manifest.exit_criteria, "min_palace_writes", ()) or ())
        for rule in palace_rules:
            needed = max(1, int(getattr(rule, "n", 1) or 1))
            tools = self._palace_write_tools_for_rule(manifest=manifest, rule=rule)
            count = self._phase_required_palace_write_count(
                task_id=task_id,
                rule=rule,
            )
            if count < needed:
                tag_hint = f" tag={rule.tag}" if getattr(rule, "tag", None) else ""
                tool_hint = "/".join(tools) if tools else "palace_add"
                return (
                    "phase exit criteria missing palace writes: "
                    f"{tool_hint} {count}/{needed} for store={rule.store}{tag_hint}. "
                    f"Call {tool_hint} with concrete, non-placeholder content and "
                    "wait for accepted/saved results before calling the phase "
                    "completion tool again."
                )

        if phase_node.id == "research":
            task_run_id = str(outcome.get("run_id") or "").strip()
            if not task_run_id and ":" in task_id:
                task_run_id = task_id.split(":", 1)[0]
            summary_failure = self._latest_research_summary_handoff_failure(
                run_id=task_run_id or None,
                min_valid_findings=self._research_summary_min_valid_findings_for_manifest(
                    manifest
                ),
            )
            if summary_failure:
                return summary_failure

        micro_review = by_kind.get("submit_micro_review")
        payload = micro_review.get("payload") if micro_review else {}
        if not isinstance(payload, dict):
            payload = {}
        verdict = str((payload or {}).get("verdict") or "").strip().lower()
        if micro_review and verdict == "abort":
            return "micro review aborted the phase"
        if micro_review and verdict == "revise":
            target = self._phase_loop_back_target(
                phase_node=phase_node,
                outcome=outcome,
            )
            if target and plan.get_node(target) is not None:
                return self._micro_review_revision_reason(payload)

            explicit_target = ""
            loop_signal = by_kind.get("loop_back_to")
            if loop_signal:
                loop_payload = loop_signal.get("payload")
                if isinstance(loop_payload, dict):
                    explicit_target = str(loop_payload.get("phase") or "").strip()
            fallback_target = explicit_target or self._default_review_loop_back_target(
                phase_node.id
            )
            if fallback_target and plan.get_node(fallback_target) is not None:
                # A capped repeated review can intentionally continue only when
                # _phase_loop_back_target suppressed a known noisy review class.
                return ""
            return (
                "micro review requested revisions but no accepted loop_back_to "
                "signal was recorded"
            )

        if phase_node.id in {"plan", "plan_review"}:
            task_run_id = str(outcome.get("run_id") or "").strip()
            if not task_run_id and ":" in task_id:
                task_run_id = task_id.split(":", 1)[0]
            floor_failure = self._latest_phase_plan_execution_floor_failure(
                run_id=task_run_id or None,
            )
            if floor_failure:
                return floor_failure

        task_run_id = str(outcome.get("run_id") or "").strip()
        if not task_run_id and ":" in task_id:
            task_run_id = task_id.split(":", 1)[0]
        contract_failure = self._phase_contract_decision_failure(
            phase=phase_node.id,
            manifest=manifest,
            run_id=task_run_id,
        )
        if contract_failure:
            return contract_failure

        return ""

    def _phase_contract_decision_failure(
        self,
        *,
        phase: str,
        manifest: Any,
        run_id: str = "",
    ) -> str:
        bundle = ContractCompiler.from_run(
            repo_root=self._repo_root,
            drive_root=self._drive_root,
            workspace_id=self._workspace_id,
            run_id=run_id,
        )
        context = build_workspace_context(
            repo_root=self._repo_root,
            workspace_root=self._repo_root / "workspaces" / self._workspace_id,
            workspace_id=self._workspace_id,
        )
        issues = ContractValidator.validate(bundle, context=context)
        decision = PhaseDecisionEngine.decide(
            phase=phase,
            issues=issues,
            manifest=manifest,
            risk=bundle.risk,
        )
        if decision.action == "continue":
            return ""
        reason = decision.reason or ", ".join(decision.blocking_issue_codes)
        if decision.action == "human_checkpoint":
            return f"contract decision human_checkpoint: {reason}"
        if decision.action == "abort":
            return f"contract decision abort: {reason}"
        if decision.action == "loop_back":
            target = decision.target_phase or phase
            return f"contract decision loop_back to {target}: {reason}"
        return f"contract decision {decision.action}: {reason}"

    @staticmethod
    def _text_excerpt(value: Any, *, limit: int = 6000) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        if limit < 200:
            return text[:limit].rstrip()
        head = max(1, limit // 2)
        tail = max(1, limit - head - 40)
        return (
            text[:head].rstrip()
            + "\n...[truncated retry context]...\n"
            + text[-tail:].lstrip()
        )

    @staticmethod
    def _subtask_retry_payload(card: SubtaskCard) -> dict[str, Any]:
        return {
            "id": card.id,
            "title": card.title,
            "status": card.status,
            "goal": card.goal,
            "proof": json_ready(card.proof) if card.proof is not None else None,
            "files_to_create": list(card.files_to_create or []),
            "files_to_change": list(card.files_to_change or []),
            "files_affected": list(card.files_affected or []),
        }

    def _phase_retry_context(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
        retry_reason: str,
    ) -> dict[str, Any]:
        task_id = str(outcome.get("task_id") or "").strip()
        result_text = (
            outcome.get("result")
            or outcome.get("final_message")
            or outcome.get("message")
            or outcome.get("error")
            or ""
        )
        context: dict[str, Any] = {
            "source_phase": phase_node.id,
            "source_task_id": task_id,
            "retry_reason": retry_reason,
        }
        status = str(outcome.get("status") or outcome.get("outcome") or "").strip()
        if status:
            context["last_task_status"] = status
        excerpt = self._text_excerpt(result_text, limit=6000)
        if excerpt:
            context["last_task_result_excerpt"] = excerpt
        if task_id:
            context["full_task_result_hint"] = (
                "Full task result is available through get_task_result("
                f'task_id="{task_id}") or under .memory/drive/task_results/.'
            )
        if phase_node.id == "execute" and phase_node.subtasks:
            pending = [card for card in phase_node.subtasks if card.status != "done"]
            if pending:
                context["next_pending_subtask"] = self._subtask_retry_payload(pending[0])
                context["pending_subtask_ids"] = [card.id for card in pending]
        return context

    def _mirror_phase_retry_context_to_palace(
        self,
        *,
        phase_node: PhaseNode,
        run_id: str,
        retry_context: dict[str, Any],
    ) -> None:
        if not retry_context:
            return
        subtask_id = ""
        pending = retry_context.get("next_pending_subtask")
        if isinstance(pending, dict):
            subtask_id = str(pending.get("id") or "").strip()
        try:
            self._palace.add(
                store="palace.subtask" if subtask_id else "palace.phase",
                content=json.dumps(
                    {
                        "artifact": "phase_retry_context",
                        "run_id": run_id,
                        "workspace_id": self._workspace_id,
                        **retry_context,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                tier="hot",
                scope="subtask_scoped" if subtask_id else "run_scoped",
                tags=[
                    "phase_retry",
                    "execution_failure",
                    "retry_context",
                ],
                phase=phase_node.id,
                subtask_id=subtask_id or None,
                run_id=run_id,
                verified=True,
                source_path=".memory/drive/task_results",
                extra={
                    "source_task_id": str(
                        retry_context.get("source_task_id") or ""
                    ),
                    "retry_reason": str(
                        retry_context.get("retry_reason") or ""
                    )[:500],
                },
            )
        except Exception:
            log.debug("Failed to mirror phase retry context to palace", exc_info=True)

    def _finish_phase_loop_back(
        self,
        *,
        phase_node: PhaseNode,
        plan: PhasePlan,
        run_id: str,
        outcome: dict[str, Any],
        loop_back_target: str,
        retry_reason: str,
    ) -> tuple[PhaseResult, ResultEnvelope]:
        phase_node.status = "done"
        phase_node.ended_at = time.time()
        overlay: dict[str, Any] = {"retry_reason": retry_reason}
        retry_context = self._phase_retry_context(
            phase_node=phase_node,
            outcome=outcome,
            retry_reason=retry_reason,
        )
        if retry_context:
            overlay["retry_context"] = retry_context
        revision_contract = self._latest_revision_contract(
            phase_node=phase_node,
            outcome=outcome,
        )
        target = plan.get_node(loop_back_target)
        existing_contract: Any = None
        if target is not None and isinstance(target.overlay, dict):
            existing_contract = target.overlay.get("revision_contract")
        if revision_contract:
            overlay["revision_contract"] = self._merged_revision_contract(
                existing_contract,
                revision_contract,
            )
        phase_node.overlay = dict(overlay)
        if target is not None and target.id != phase_node.id:
            target.overlay = dict(overlay)
        self._mirror_phase_retry_context_to_palace(
            phase_node=phase_node,
            run_id=run_id,
            retry_context=retry_context,
        )
        save_plan(plan, self._drive_root)
        result = PhaseResult(
            phase_id=phase_node.id,
            outcome="loop_back",
            loop_back_target=loop_back_target,
        )
        envelope = self._emit(ResultEnvelope.success(
            data={
                "event": "phase_done",
                "phase": phase_node.id,
                "outcome": result.outcome,
                "retry_reason": retry_reason,
                "events": outcome.get("event_count", 0),
            },
            run_id=run_id,
            phase=phase_node.id,
            took_ms=int(
                (phase_node.ended_at - (phase_node.started_at or phase_node.ended_at))
                * 1000
            ),
        ))
        return result, envelope

    def _phase_tool_success_count(self, *, task_id: str, tool_name: str) -> int:
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if str(row.get("tool") or "") != tool_name:
                    continue
                if is_effective_write_tool_log_row(row):
                    count += 1
        except OSError:
            log.debug("Failed to read tools log for %s count", tool_name, exc_info=True)
        return count

    @staticmethod
    def _palace_write_tools_for_rule(*, manifest: Any, rule: Any) -> tuple[str, ...]:
        allowed = set(getattr(manifest, "allowed_tools", set()) or set())
        forbidden = set(getattr(manifest, "forbidden_tools", set()) or set())
        tools: list[str] = []
        if "palace_add" in allowed and "palace_add" not in forbidden:
            tools.append("palace_add")
        if (
            getattr(rule, "store", "") == "palace.run"
            and "submit_research_summary" in allowed
            and "submit_research_summary" not in forbidden
        ):
            tools.append("submit_research_summary")
        if (
            getattr(rule, "store", "") == "palace.run"
            and "propose_phase_plan" in allowed
            and "propose_phase_plan" not in forbidden
        ):
            tools.append("propose_phase_plan")
        if (
            getattr(rule, "store", "") == "palace.durable"
            and "promote_to_durable" in allowed
            and "promote_to_durable" not in forbidden
        ):
            tools.append("promote_to_durable")
        return tuple(tools or ("palace_add",))

    @staticmethod
    def _tool_row_tags(row: dict[str, Any]) -> set[str]:
        args = row.get("args")
        if not isinstance(args, dict):
            args = {}
        raw = args.get("tags") or args.get("tag") or ""
        values: list[str] = []
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.replace(";", ",").split(","))
            values.extend(part.strip() for part in raw.split())
        elif isinstance(raw, (list, tuple, set)):
            values.extend(str(part).strip() for part in raw)
        return {value for value in values if value}

    @classmethod
    def _tool_row_has_tag(
        cls,
        row: dict[str, Any],
        tag: str | None,
        *,
        allow_missing_tags: bool,
    ) -> bool:
        if not tag:
            return True
        tags = cls._tool_row_tags(row)
        if not tags:
            return allow_missing_tags
        return tag in tags

    @staticmethod
    def _row_json_payload(row: dict[str, Any], *keys: str) -> dict[str, Any]:
        for key in keys:
            value = row.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def _tool_log_rows_for_task(self, *, task_id: str) -> list[dict[str, Any]]:
        if not task_id:
            return []
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                rows.append(row)
        except OSError:
            return rows
        return rows

    def _accepted_palace_add_aliases_for_task(self, *, task_id: str) -> dict[str, str]:
        accepted: dict[str, str] = {}
        for row in self._tool_log_rows_for_task(task_id=task_id):
            try:
                if str(row.get("tool") or "") != "palace_add":
                    continue
                result = self._row_json_payload(row, "result_preview", "result")
                if result.get("saved") is not True:
                    continue
                primary_ids: list[str] = []
                for key in ("id", "memory_id", "artifact_id"):
                    value = str(result.get(key) or "").strip()
                    if value:
                        primary_ids.append(value)
                aliases = list(primary_ids)
                legacy = result.get("legacy")
                if isinstance(legacy, dict):
                    value = str(legacy.get("id") or "").strip()
                    if value:
                        aliases.append(value)
                canonical = primary_ids[0] if primary_ids else (aliases[0] if aliases else "")
                if not canonical:
                    continue
                for alias in aliases:
                    accepted[alias] = canonical
            except Exception:
                continue
        return accepted

    def _accepted_palace_add_ids_for_task(self, *, task_id: str) -> set[str]:
        return set(self._accepted_palace_add_aliases_for_task(task_id=task_id))

    def _research_summary_tool_row_is_valid(
        self, row: dict[str, Any], *, task_id: str
    ) -> bool:
        args = row.get("args")
        if not isinstance(args, dict):
            args = self._row_json_payload(row, "args")
        architecture_id = str(args.get("architecture_id") or "").strip()
        if not architecture_id:
            return False
        raw_findings = args.get("findings_ids")
        if not isinstance(raw_findings, list):
            return False
        findings = [str(item).strip() for item in raw_findings if str(item).strip()]
        if not findings:
            return False
        accepted = self._accepted_palace_add_ids_for_task(task_id=task_id)
        return any(item in accepted for item in findings)

    def _phase_required_palace_write_count(self, *, task_id: str, rule: Any) -> int:
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                tool_name = str(row.get("tool") or "")
                if not is_effective_write_tool_log_row(row):
                    continue
                if tool_name == "palace_add":
                    if self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=True,
                    ):
                        count += 1
                    continue
                if (
                    tool_name == "submit_research_summary"
                    and getattr(rule, "store", "") == "palace.run"
                    and self._research_summary_tool_row_is_valid(
                        row,
                        task_id=task_id,
                    )
                    and self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=True,
                    )
                ):
                    count += 1
                    continue
                if (
                    tool_name == "propose_phase_plan"
                    and getattr(rule, "store", "") == "palace.run"
                    and self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=True,
                    )
                ):
                    count += 1
                    continue
                if (
                    tool_name == "promote_to_durable"
                    and getattr(rule, "store", "") == "palace.durable"
                    and self._promote_to_durable_tool_row_is_valid(row)
                    and self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=False,
                    )
                ):
                    count += 1
        except OSError:
            log.debug("Failed to read tools log for palace write count", exc_info=True)
        return count

    @staticmethod
    def _default_review_loop_back_target(phase_id: str) -> str:
        return {
            "research_review": "research",
            "plan_review": "plan",
            "subtask_review": "execute",
            "final_review": "execute",
            "verify": "execute",
        }.get(phase_id, "")

    def _phase_loop_back_target(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
    ) -> str:
        task_id = str(outcome.get("task_id") or "")
        records = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        for row in reversed(records):
            if str(row.get("kind") or "") != "loop_back_to":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            target = str((payload or {}).get("phase") or "").strip()
            if target:
                return target
        for row in reversed(records):
            if str(row.get("kind") or "") != "submit_micro_review":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if str((payload or {}).get("verdict") or "") == "revise":
                explicit_target = str((payload or {}).get("loop_back_target") or "").strip()
                return explicit_target or self._default_review_loop_back_target(phase_node.id)
        for row in reversed(records):
            kind = str(row.get("kind") or "")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if (
                kind == "submit_final_review"
                and str((payload or {}).get("outcome") or "") == "loop_back"
            ):
                return self._default_review_loop_back_target(phase_node.id)
            if (
                kind == "submit_verification"
                and str((payload or {}).get("status") or "") == "fail"
            ):
                return self._default_review_loop_back_target(phase_node.id)
        return ""

    @staticmethod
    def _research_summary_min_valid_findings_for_manifest(manifest: Any) -> int:
        criteria = getattr(manifest, "exit_criteria", None)
        rules = list(getattr(criteria, "required_palace_writes", ()) or ()) + list(
            getattr(criteria, "min_palace_writes", ()) or ()
        )
        required = 1
        for rule in rules:
            if str(getattr(rule, "store", "") or "") != "palace.run":
                continue
            try:
                n = max(1, int(getattr(rule, "n", 1) or 1))
            except (TypeError, ValueError):
                n = 1
            # Research min_palace_writes is a finding floor. The summary is the
            # handoff after those accepted palace_add findings, not a substitute
            # for one of them.
            required = max(required, n)
        return required

    def _latest_research_summary_handoff_failure(
        self, *, run_id: str | None, min_valid_findings: int = 1
    ) -> str:
        path = self._drive_root / "state" / "research_summary_latest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "latest research summary artifact is missing or unreadable"
        if run_id and str(data.get("run_id") or "") not in {"", run_id}:
            return "latest research summary artifact belongs to a different run"
        architecture_id = str(data.get("architecture_id") or "").strip()
        findings = data.get("findings_ids")
        if not isinstance(findings, list):
            findings = []
        concrete_findings = [
            str(item).strip() for item in findings if str(item).strip()
        ]
        if not architecture_id:
            return "latest research summary is missing architecture_id"
        task_id = str(data.get("task_id") or "").strip()
        if not task_id and run_id:
            task_id = f"{run_id}:research"
        accepted_aliases = self._accepted_palace_add_aliases_for_task(task_id=task_id)
        valid_canonical: list[str] = []
        seen_canonical: set[str] = set()
        for item in concrete_findings:
            canonical = accepted_aliases.get(item)
            if canonical and canonical not in seen_canonical:
                seen_canonical.add(canonical)
                valid_canonical.append(canonical)
        if len(valid_canonical) < min_valid_findings:
            rows = self._tool_log_rows_for_task(task_id=task_id)
            scarcity_issue = research_scarcity_handoff_issue(
                rows,
                accepted_count=len(valid_canonical),
                min_findings=min_valid_findings,
                coverage_status=str(data.get("coverage_status") or ""),
            )
            if not scarcity_issue:
                return ""
            return (
                "latest research summary references "
                f"{len(valid_canonical)}/{min_valid_findings} accepted palace_add "
                "finding id(s); use the id or legacy.id returned by palace_add, "
                "not invented finding labels. "
                f"{scarcity_issue}"
            )
        return ""

    def _latest_research_summary_has_handoff_floor(
        self, *, run_id: str | None
    ) -> bool:
        return (
            self._latest_research_summary_handoff_failure(
                run_id=run_id,
                min_valid_findings=1,
            )
            == ""
        )

    def _latest_phase_plan_has_execution_floor(self, *, run_id: str | None) -> bool:
        return self._latest_phase_plan_execution_floor_failure(run_id=run_id) == ""

    def _phase_plan_review_payload(self, *, run_id: str | None) -> tuple[dict[str, Any], str]:
        for filename in (
            "phase_plan_submitted_latest.json",
            "phase_plan_proposal_latest.json",
        ):
            path = self._drive_root / "state" / filename
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if run_id and str(data.get("run_id") or "") not in {"", run_id}:
                continue
            return data, filename
        return {}, ""

    def _phase_plan_execution_payload(self, *, run_id: str | None) -> tuple[dict[str, Any], str]:
        filename = "phase_plan_submitted_latest.json"
        for filename in (filename,):
            path = self._drive_root / "state" / filename
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if run_id and str(data.get("run_id") or "") not in {"", run_id}:
                continue
            return data, filename
        return {}, ""

    def _latest_phase_plan_execution_floor_failure(
        self, *, run_id: str | None
    ) -> str:
        data, source = self._phase_plan_execution_payload(run_id=run_id)
        if not data:
            return "submitted phase plan artifact is missing or unreadable"
        if run_id and str(data.get("run_id") or "") not in {"", run_id}:
            return "submitted phase plan artifact belongs to a different run"
        plan = data.get("plan") if isinstance(data, dict) else None
        if not isinstance(plan, dict):
            return "submitted phase plan artifact does not contain a plan object"
        plan, embedded_issue = self._coerce_embedded_phase_plan(plan)
        if embedded_issue:
            return embedded_issue
        plan_ir, compile_issues = compile_phase_plan(
            plan,
            run_id=str(data.get("run_id") or run_id or ""),
            workspace_id=str(data.get("workspace_id") or self._workspace_id),
        )
        context = build_workspace_context(
            repo_root=self._repo_root,
            workspace_root=self._repo_root / "workspaces" / self._workspace_id,
            workspace_id=self._workspace_id,
        )
        contract_issues = ContractValidator.validate(
            ContractBundle(
                run_id=str(data.get("run_id") or run_id or ""),
                workspace_id=self._workspace_id,
                plan=plan_ir,
                issues=tuple(compile_issues),
            ),
            context=context,
        )
        if contract_issues:
            issue = contract_issues[0]
            return f"latest phase plan contract rejected: {issue.code}: {issue.message}"
        return ""

    def _merge_persisted_plan_state(self, plan: PhasePlan) -> PhasePlan:
        """Refresh in-memory phase state after phase tools mutate phase_plan.json."""
        try:
            persisted = load_plan(self._drive_root)
        except Exception:
            return plan
        if (
            persisted is None
            or persisted.run_id != plan.run_id
            or persisted.workspace_id != plan.workspace_id
        ):
            return plan
        plan.nodes = persisted.nodes
        plan.version = persisted.version
        plan.edits_log = persisted.edits_log
        return plan

    @staticmethod
    def _iter_plan_child_dicts(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            return [item for item in raw.values() if isinstance(item, dict)]
        return []

    @classmethod
    def _execution_items_from_plan(cls, plan: dict[str, Any]) -> list[dict[str, Any]]:
        raw = plan.get("subtasks")
        if isinstance(raw, (list, dict)):
            return cls._iter_plan_child_dicts(raw)
        return []

    @classmethod
    def _coerce_embedded_phase_plan(cls, plan: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if isinstance(plan.get("plan"), (dict, str)):
            return (
                plan,
                "contract v1 rejects nested or serialized plan wrappers; submit the typed plan object directly",
            )
        return plan, ""
    def _subtask_card_from_plan_item(
        self,
        item: dict[str, Any],
        *,
        idx: int,
        previous_status: dict[str, str],
    ) -> SubtaskCard:
        title = str(item.get("title") or item.get("name") or f"Subtask {idx + 1}").strip()
        subtask_id = str(
            item.get("id") or item.get("subtask_id") or f"subtask_{idx + 1:02d}"
        ).strip()
        return SubtaskCard(
            id=subtask_id,
            title=title,
            goal=str(item.get("goal") or item.get("description") or title),
            allowed_tools=frozenset(
                str(tool)
                for tool in (item.get("allowed_tools") or item.get("tools") or [])
                if str(tool).strip()
            ),
            allowed_skills=frozenset(
                str(skill)
                for skill in (item.get("allowed_skills") or item.get("skills") or [])
                if str(skill).strip()
            ),
            proof=(
                ProofSpec.from_mapping(item["proof"])
                if isinstance(item.get("proof"), dict)
                else None
            ),
            codeptr_refs=[str(value) for value in (item.get("codeptr_refs") or [])],
            mcp_refs=[str(value) for value in (item.get("mcp_refs") or [])],
            files_to_create=self._first_plan_string_list(
                item,
                "files_to_create",
                "file_to_create",
                "new_files",
                "new_file",
                "files_to_add",
            ),
            files_to_change=self._first_plan_string_list(
                item,
                "files_to_change",
                "file_to_change",
                "files_to_modify",
                "files_to_update",
                "target_files",
                "target_file",
            ),
            files_affected=self._first_plan_string_list(
                item,
                "files_affected",
                "files",
                "paths",
            ),
            dependencies=self._first_plan_string_list(
                item,
                "dependencies",
                "depends_on",
                "requires",
            ),
            status=previous_status.get(subtask_id, "pending"),  # type: ignore[arg-type]
        )

    @classmethod
    def _first_plan_string_list(cls, item: dict[str, Any], *keys: str) -> list[str]:
        for key in keys:
            values = cls._plan_string_list(item.get(key))
            if values:
                return values
        return []

    @classmethod
    def _plan_string_list(cls, raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            text = raw.strip()
            return [text] if text else []
        if isinstance(raw, dict):
            for key in ("path", "file_path", "file", "target", "value", "name", "id"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return [value.strip()]
            return []
        if isinstance(raw, (list, tuple, set, frozenset)):
            values: list[str] = []
            for item in raw:
                values.extend(cls._plan_string_list(item))
            return values
        text = str(raw).strip()
        return [text] if text else []

    def _sync_execute_subtasks_from_latest_plan(
        self,
        plan: PhasePlan,
        *,
        run_id: str,
    ) -> bool:
        """Project the accepted plan artifact into the executable phase plan.

        The model-authored plan is stored as an artifact for review, but the
        runner needs concrete SubtaskCard state so execute can work one bounded
        subtask at a time and resume after loop-backs.
        """
        plan_node = plan.get_node("plan")
        if plan_node is not None and plan_node.status != "done":
            return False
        review_node = plan.get_node("plan_review")
        if review_node is not None and review_node.status != "done":
            return False
        if self._latest_phase_plan_execution_floor_failure(run_id=run_id):
            return False
        payload, _source = self._phase_plan_execution_payload(run_id=run_id)
        if not payload:
            return False
        proposed = payload.get("plan") if isinstance(payload, dict) else None
        if not isinstance(proposed, dict):
            return False
        raw_subtasks = self._execution_items_from_plan(proposed)
        if not raw_subtasks:
            return False
        execute = plan.get_node("execute")
        if execute is None:
            return False

        previous_status = {
            card.id: card.status
            for card in (execute.subtasks or [])
            if isinstance(card, SubtaskCard)
        }
        cards = [
            self._subtask_card_from_plan_item(
                item,
                idx=idx,
                previous_status=previous_status,
            )
            for idx, item in enumerate(raw_subtasks)
            if isinstance(item, dict)
        ]
        if not cards:
            return False
        old_ids = [card.id for card in (execute.subtasks or [])]
        new_ids = [card.id for card in cards]
        if old_ids == new_ids and [
            self._subtask_card_contract_key(card)
            for card in (execute.subtasks or [])
        ] == [self._subtask_card_contract_key(card) for card in cards]:
            return False
        execute.subtasks = cards
        plan.version += 1
        plan.edits_log.append(
            PlanEdit(
                timestamp=time.time(),
                actor="runner",
                patch={
                    "sync_execute_subtasks_from_plan_id": payload.get("plan_id"),
                    "subtask_ids": new_ids,
                },
            )
        )
        return True

    @staticmethod
    def _subtask_card_contract_key(card: SubtaskCard) -> tuple[Any, ...]:
        return (
            card.id,
            card.title,
            card.goal,
            tuple(sorted(card.allowed_tools or ())),
            tuple(sorted(card.allowed_skills or ())),
            json.dumps(json_ready(card.proof), sort_keys=True)
            if card.proof is not None
            else "",
            tuple(card.codeptr_refs or ()),
            tuple(card.mcp_refs or ()),
            tuple(card.files_to_create or ()),
            tuple(card.files_to_change or ()),
            tuple(card.files_affected or ()),
            tuple(card.dependencies or ()),
        )

    @staticmethod
    def _incomplete_subtasks(node: PhaseNode | None) -> list[SubtaskCard]:
        if node is None or not node.subtasks:
            return []
        return [card for card in node.subtasks if card.status != "done"]

    @staticmethod
    def _phase_allows_workspace_writes(manifest: Any) -> bool:
        write_tools = {
            "apply_workspace_patch",
            "delete_workspace_file",
            "repo_write_commit",
            "update_workspace_seed",
            "update_workspace_from_instance",
            "commit_workspace_changes",
        }
        allowed_tools = set(getattr(manifest, "allowed_tools", set()) or set())
        return bool(allowed_tools & write_tools)

    @staticmethod
    def _safe_phase_id_part(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
        return normalized.strip("_") or "subtask"

    def _latest_completed_subtask_from_phase(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
    ) -> SubtaskCard | None:
        if phase_node.id != "execute" or not phase_node.subtasks:
            return None
        task_id = str(outcome.get("task_id") or "")
        rows = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        by_id = {card.id: card for card in phase_node.subtasks}
        for row in reversed(rows):
            if str(row.get("kind") or "") != "mark_subtask_complete":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            status = str((payload or {}).get("status") or "").strip().lower()
            if status and status not in {"done", "ok", "complete", "completed"}:
                continue
            subtask_id = str((payload or {}).get("subtask_id") or "").strip()
            if not subtask_id:
                contract = payload.get("completion_contract")
                if isinstance(contract, dict):
                    subtask_id = str(contract.get("subtask_id") or "").strip()
            card = by_id.get(subtask_id)
            if card is not None and card.status == "done":
                return card
        return None

    def _schedule_subtask_review(
        self,
        *,
        plan: PhasePlan,
        execute_node: PhaseNode,
        completed_subtask: SubtaskCard,
        review_manifest_id: str,
        run_id: str,
    ) -> str:
        safe_id = self._safe_phase_id_part(completed_subtask.id)
        review_node_id = f"{review_manifest_id}:{safe_id}"
        existing = plan.get_node(review_node_id)
        if existing is not None:
            return existing.id
        review_node = PhaseNode(
            id=review_node_id,
            manifest_id=review_manifest_id,
            status="pending",
            parent_phase_id=execute_node.id,
            overlay={
                "subtask_id": completed_subtask.id,
                "review_target": "latest_completion_contract",
                "execute_phase_id": execute_node.id,
                "run_id": run_id,
            },
        )
        try:
            index = plan.nodes.index(execute_node)
        except ValueError:
            index = max(0, len(plan.nodes) - 1)
        plan.nodes.insert(index + 1, review_node)
        plan.version += 1
        plan.edits_log.append(
            PlanEdit(
                timestamp=time.time(),
                actor="runner",
                patch={
                    "insert_phase": review_node_id,
                    "manifest_id": review_manifest_id,
                    "after": execute_node.id,
                    "subtask_id": completed_subtask.id,
                },
            )
        )
        return review_node_id

    def _reviewed_subtask_id(self, phase_node: PhaseNode) -> str:
        overlay = phase_node.overlay if isinstance(phase_node.overlay, dict) else {}
        return str((overlay or {}).get("subtask_id") or "").strip()

    def _set_reviewed_subtask_verdict(
        self,
        *,
        plan: PhasePlan,
        review_node: PhaseNode,
        verdict: str,
    ) -> None:
        if verdict not in {"ok", "revise", "abort"}:
            return
        subtask_id = self._reviewed_subtask_id(review_node)
        execute_id = ""
        if isinstance(review_node.overlay, dict):
            execute_id = str(review_node.overlay.get("execute_phase_id") or "").strip()
        execute = plan.get_node(execute_id or review_node.parent_phase_id or "execute")
        if not subtask_id or execute is None or not execute.subtasks:
            return
        for card in execute.subtasks:
            if card.id != subtask_id:
                continue
            card.review_verdict = verdict
            if verdict == "revise":
                card.status = "pending"
            plan.version += 1
            plan.edits_log.append(
                PlanEdit(
                    timestamp=time.time(),
                    actor="runner",
                    patch={
                        "subtask_review_verdict": verdict,
                        "subtask_id": subtask_id,
                        "review_phase": review_node.id,
                    },
                )
            )
            return

    def _resume_execute_after_subtask_review(
        self,
        *,
        plan: PhasePlan,
        review_node: PhaseNode,
    ) -> None:
        execute_id = ""
        if isinstance(review_node.overlay, dict):
            execute_id = str(review_node.overlay.get("execute_phase_id") or "").strip()
        execute = plan.get_node(execute_id or review_node.parent_phase_id or "execute")
        if execute is None or not self._incomplete_subtasks(execute):
            return
        execute.status = "pending"
        execute.started_at = None
        execute.ended_at = None
        overlay = dict(execute.overlay or {})
        overlay["last_subtask_review_phase"] = review_node.id
        overlay["last_reviewed_subtask_id"] = self._reviewed_subtask_id(review_node)
        execute.overlay = overlay
        plan.version += 1
        plan.edits_log.append(
            PlanEdit(
                timestamp=time.time(),
                actor="runner",
                patch={
                    "resume_phase": execute.id,
                    "after_subtask_review": review_node.id,
                },
            )
        )

    def _done_subtasks_materialization_failure(self, phase_node: PhaseNode) -> str:
        if phase_node.id != "execute" or not phase_node.subtasks:
            return ""
        workspace_root = self._repo_root / "workspaces" / self._workspace_id
        issues = validate_done_subtasks_materialized(
            subtasks=list(phase_node.subtasks),
            workspace_root=str(workspace_root),
            phase="execute",
        )
        if not issues:
            return ""
        issue = issues[0]
        return f"subtask materialization missing: {issue.code}: {issue.message}"

    def _phase_effective_write_count(self, *, task_id: str) -> int:
        write_tools = {
            "apply_workspace_patch",
            "update_workspace_seed",
            "update_workspace_from_instance",
            "delete_workspace_file",
            "commit_workspace_changes",
            "repo_write_commit",
        }
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if (
                    str(row.get("tool") or "") in write_tools
                    and is_effective_write_tool_log_row(row)
                ):
                    count += 1
        except OSError:
            log.debug("Failed to read tools log for write count", exc_info=True)
        return count

    def run(
        self,
        task_input: str,
        *,
        phases: list[str] | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
        stream: bool = False,
    ) -> Iterator[ResultEnvelope]:
        run_id = run_id or str(uuid.uuid4())
        loaded_plan = load_plan(self._drive_root)
        if (
            loaded_plan is not None
            and loaded_plan.run_id == run_id
            and loaded_plan.workspace_id == self._workspace_id
        ):
            plan = loaded_plan
        else:
            plan = build_default_plan(self._workspace_id, run_id=run_id, phases=phases)
        save_plan(plan, self._drive_root)

        manifest_errors = self._registry.validate_all()
        if not manifest_errors:
            try:
                from umbrella.phases.tool_contract import validate_phase_tool_contract

                manifest_errors.extend(
                    validate_phase_tool_contract(
                        self._registry.all(), repo_root=self._repo_root
                    )
                )
            except Exception as exc:
                manifest_errors.append(f"phase tool contract validation failed: {exc}")
        if manifest_errors:
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.PHASE_MANIFEST_INVALID,
                "; ".join(manifest_errors),
                run_id=run_id,
            ))
            return

        if dry_run:
            yield self._emit(ResultEnvelope.success(
                data={
                    "phases": plan.ids() if hasattr(plan, "ids") else [n.id for n in plan.nodes],
                    "manifests_ok": True,
                },
                run_id=run_id,
                phase="dry_run",
                took_ms=0,
            ))
            return

        try:
            max_iterations = max(32, len(plan.nodes) * 8)
            iterations = 0
            while True:
                self._merge_persisted_plan_state(plan)
                if self._sync_execute_subtasks_from_latest_plan(plan, run_id=run_id):
                    save_plan(plan, self._drive_root)
                phase_node = plan.next_pending()
                if phase_node is None:
                    break
                iterations += 1
                if iterations > max_iterations:
                    yield self._emit(ResultEnvelope.failure(
                        ErrorCode.WATCHER_ABORT,
                        "phase runner exceeded loop-back iteration limit",
                        run_id=run_id,
                        phase=phase_node.id,
                    ))
                    return
                if self._stop_requested():
                    yield self._emit(ResultEnvelope.failure(
                        ErrorCode.WATCHER_ABORT,
                        "stop_requested by user before phase start",
                        run_id=run_id,
                        phase=phase_node.id,
                    ))
                    return
                result = yield from self._run_phase(
                    phase_node, plan, run_id=run_id, task_input=task_input
                )
                if result is None or result.outcome == "failed":
                    return
                if result and result.outcome == "loop_back" and result.loop_back_target:
                    self._merge_persisted_plan_state(plan)
                    target = plan.get_node(result.loop_back_target)
                    if target:
                        target.status = "pending"
                        target.started_at = None
                        target.ended_at = None
                    current = plan.get_node(result.phase_id)
                    if current:
                        current.status = "pending"
                        current.started_at = None
                        current.ended_at = None
                    save_plan(plan, self._drive_root)
        finally:
            if self._owns_launcher and self._launcher is not None:
                try:
                    self._launcher.stop()
                except Exception:
                    log.debug("Launcher stop failed", exc_info=True)

        try:
            from umbrella.memory.backends.base import DurableEvent
            from umbrella.memory.backends.factory import retain_hindsight_event_best_effort

            retain_hindsight_event_best_effort(
                repo_root=self._repo_root,
                workspace_id=self._workspace_id,
                event=DurableEvent(
                    event_id=f"run_summary:{run_id}",
                    kind="run_summary",
                    content=f"Umbrella run {run_id} completed for workspace {self._workspace_id}.",
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    trust_level="supervisor_verified",
                    tags=["kind:run_summary", "scope:workspace", "tier:durable"],
                    metadata={
                        "umbrella_id": f"run_summary:{run_id}",
                        "workspace_id": self._workspace_id,
                        "run_id": run_id,
                        "kind": "run_summary",
                        "trust_level": "supervisor_verified",
                    },
                ),
                op="retain_run_summary",
            )
        except Exception:
            if os.environ.get("UMBRELLA_HINDSIGHT_FAIL_CLOSED") == "1":
                raise

        yield self._emit(ResultEnvelope.success(
            data={"run_id": run_id, "status": "complete"},
            run_id=run_id,
            took_ms=0,
        ))

    def _run_phase(
        self,
        phase_node: PhaseNode,
        plan: PhasePlan,
        *,
        run_id: str,
        task_input: str,
    ) -> Iterator[ResultEnvelope]:
        try:
            manifest = self._registry.get(phase_node.manifest_id)
        except KeyError as exc:
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.UNKNOWN_PHASE, str(exc), run_id=run_id, phase=phase_node.id
            ))
            return None

        if manifest.id == "execute":
            materialization_failure = self._done_subtasks_materialization_failure(
                phase_node
            )
            if materialization_failure:
                phase_node.status = "failed"
                phase_node.ended_at = time.time()
                save_plan(plan, self._drive_root)
                yield self._emit(ResultEnvelope.failure(
                    ErrorCode.EVIDENCE_VALIDATION_FAILED,
                    materialization_failure,
                    run_id=run_id,
                    phase=phase_node.id,
                ))
                return None

        phase_node.status = "running"
        phase_node.started_at = time.time()
        self._write_phase_budget_file(phase_node.id, manifest.budgets)
        self._clear_pending_phase_signal()
        save_plan(plan, self._drive_root)

        yield self._emit(ResultEnvelope.success(
            data={"event": "phase_started", "phase": phase_node.id, "label": manifest.id},
            run_id=run_id,
            phase=phase_node.id,
            took_ms=0,
        ))

        base_task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=self._workspace_id,
            run_id=run_id,
            palace=self._palace,
            drive_root=self._drive_root,
            repo_root=self._repo_root,
        )
        if isinstance(phase_node.overlay, dict) and phase_node.overlay.get(
            "retry_reason"
        ):
            revision_contract = phase_node.overlay.get("revision_contract")
            retry_context = phase_node.overlay.get("retry_context")
            if isinstance(revision_contract, dict):
                revision_text = json.dumps(
                    revision_contract,
                    ensure_ascii=False,
                    indent=2,
                )
            elif isinstance(retry_context, dict):
                revision_text = json.dumps(
                    retry_context,
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                revision_text = str(phase_node.overlay.get("retry_reason") or "")
            base_task["input"] = (
                (base_task.get("input") or "")
                + "\n\n## Active retry/revision contract\n"
                + "This phase is being retried after an Umbrella control-plane gate. Treat the retry context below as required acceptance criteria for the new attempt. Do not call the completion tool until the latest artifact explicitly addresses the previous failure and no longer depends on the rejected older attempt.\n"
                + "For planning retries, `propose_phase_plan.plan` must be the full revised compact object with executable leaves. Do not send a diff, notes-only patch, markdown, or serialized/truncated JSON string under `plan.plan`; shorten prose instead of wrapping or truncating the plan.\n"
                + "```json\n"
                + revision_text
                + "\n```\n"
                + "Do not finish this phase until the required completion calls are accepted with concrete verification evidence.\n"
            )
        base_task["input"] = (base_task.get("input") or "") + f"\n\n## User task\n{task_input}\n"

        if (
            self._candidates_per_phase > 1
            and not self._phase_allows_workspace_writes(manifest)
        ):
            outcome = self._run_phase_with_harness(
                base_task, phase_node, manifest, run_id=run_id
            )
        else:
            outcome = self._run_phase_single(base_task, phase_node, run_id=run_id)

        self._merge_persisted_plan_state(plan)
        phase_node = plan.get_node(phase_node.id) or phase_node

        pending_signal = self._watcher.read_pending_signal()
        if outcome.get("status") == "watcher" or pending_signal is not None:
            if pending_signal is not None:
                result, envelope = self._apply_pending_watcher_signal(
                    signal=pending_signal,
                    phase_node=phase_node,
                    plan=plan,
                    run_id=run_id,
                    outcome=outcome,
                )
                if envelope is not None:
                    yield envelope
                if result is not None:
                    return result
                return None

        if outcome.get("status") == "error":
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.WORKER_PANIC,
                str(outcome.get("error") or "worker failure"),
                run_id=run_id,
                phase=phase_node.id,
            ))
            return None

        model_failure = self._outcome_model_response_failure(outcome)
        if model_failure:
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.WORKER_PANIC,
                model_failure,
                run_id=run_id,
                phase=phase_node.id,
            ))
            return PhaseResult(
                phase_id=phase_node.id,
                outcome="failed",
                error=model_failure,
            )

        completion_failure = self._phase_completion_failure(
            phase_node=phase_node,
            plan=plan,
            manifest=manifest,
            outcome=outcome,
        )
        if not completion_failure and manifest.id == "execute":
            incomplete = self._incomplete_subtasks(phase_node)
            completed = self._latest_completed_subtask_from_phase(
                phase_node=phase_node,
                outcome=outcome,
            )
            if incomplete and completed is None:
                first = incomplete[0]
                completion_failure = (
                    "execute phase still has incomplete subtask card(s): "
                    + ", ".join(card.id for card in incomplete[:8])
                    + ". Continue with exactly the next pending subtask "
                    f"`{first.id}` ({first.title}) and call "
                    "`mark_subtask_complete` with a fresh "
                    "CompletionContract after its proof evidence is present."
                )
        if not completion_failure and manifest.id == "execute":
            if self._phase_effective_write_count(
                task_id=str(outcome.get("task_id") or "")
            ) <= 0:
                completion_failure = (
                    "execute phase completed without any effective workspace write tool calls"
                )
        if completion_failure:
            if completion_failure.startswith(
                ("micro review requested revisions", "contract decision loop_back")
            ):
                loop_back_target = self._phase_loop_back_target(
                    phase_node=phase_node,
                    outcome=outcome,
                )
                if not loop_back_target:
                    loop_back_target = "plan" if manifest.id == "plan_review" else phase_node.id
                if loop_back_target and plan.get_node(loop_back_target) is not None:
                    if manifest.id == "subtask_review" and loop_back_target == "execute":
                        self._set_reviewed_subtask_verdict(
                            plan=plan,
                            review_node=phase_node,
                            verdict="revise",
                        )
                    result, envelope = self._finish_phase_loop_back(
                        phase_node=phase_node,
                        plan=plan,
                        run_id=run_id,
                        outcome=outcome,
                        loop_back_target=loop_back_target,
                        retry_reason=completion_failure,
                    )
                    yield envelope
                    return result
            if (
                (
                    manifest.id in {"execute", "plan"}
                    and completion_failure.startswith(
                        "phase exit criteria missing required call(s):"
                    )
                )
                or (
                    manifest.id == "execute"
                    and completion_failure.startswith(
                        "execute phase still has incomplete subtask"
                    )
                )
                or (
                    manifest.id == "research"
                    and completion_failure.startswith(
                        "phase exit criteria missing palace writes:"
                    )
                )
                or (
                    manifest.id == "research"
                    and completion_failure.startswith("latest research summary")
                )
                or (
                    manifest.id == "plan"
                    and completion_failure.startswith("latest phase plan")
                )
            ):
                result, envelope = self._finish_phase_loop_back(
                    phase_node=phase_node,
                    plan=plan,
                    run_id=run_id,
                    outcome=outcome,
                    loop_back_target=phase_node.id,
                    retry_reason=completion_failure,
                )
                yield envelope
                return result
            if (
                manifest.id == "plan_review"
                and completion_failure.startswith("latest phase plan")
            ):
                result, envelope = self._finish_phase_loop_back(
                    phase_node=phase_node,
                    plan=plan,
                    run_id=run_id,
                    outcome=outcome,
                    loop_back_target="plan",
                    retry_reason=completion_failure,
                )
                yield envelope
                return result
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.VERIFY_FAILED,
                completion_failure,
                run_id=run_id,
                phase=phase_node.id,
            ))
            return PhaseResult(
                phase_id=phase_node.id,
                outcome="failed",
                error=completion_failure,
            )

        result = PhaseResult(phase_id=phase_node.id, outcome="done")
        if outcome.get("outcome") == "loop_back":
            result = PhaseResult(
                phase_id=phase_node.id,
                outcome="loop_back",
                loop_back_target=outcome.get("loop_back_target"),
            )
        else:
            loop_back_target = self._phase_loop_back_target(
                phase_node=phase_node,
                outcome=outcome,
            )
            if loop_back_target:
                result = PhaseResult(
                    phase_id=phase_node.id,
                    outcome="loop_back",
                    loop_back_target=loop_back_target,
                )

        pending_signal = self._watcher.read_pending_signal()
        if pending_signal is not None:
            result, envelope = self._apply_pending_watcher_signal(
                signal=pending_signal,
                phase_node=phase_node,
                plan=plan,
                run_id=run_id,
                outcome=outcome,
            )
            if envelope is not None:
                yield envelope
            if result is not None:
                return result
            return None

        if self._stop_requested():
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.WATCHER_ABORT,
                "stop_requested by user during phase",
                run_id=run_id,
                phase=phase_node.id,
            ))
            return None

        task_id = str(outcome.get("task_id") or "")
        if result.outcome == "done":
            if manifest.id == "verify":
                from umbrella.memory.proactive.phase_hooks import (
                    mirror_verify_durable_if_needed,
                )

                mirror_verify_durable_if_needed(
                    repo_root=self._repo_root,
                    drive_root=self._drive_root,
                    workspace_id=self._workspace_id,
                    task_id=task_id,
                    phase_started_at=phase_node.started_at,
                    tools_log_path=self._drive_root / "logs" / "tools.jsonl",
                )
            elif manifest.id == "reflexion":
                from umbrella.memory.proactive.phase_hooks import (
                    process_reflexion_bkb_patch,
                )

                promotion_result = process_reflexion_bkb_patch(
                    repo_root=self._repo_root,
                    drive_root=self._drive_root,
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                )
                if promotion_result and promotion_result.get("accepted") is False:
                    result, envelope = self._finish_phase_loop_back(
                        phase_node=phase_node,
                        plan=plan,
                        run_id=run_id,
                        outcome=outcome,
                        loop_back_target=phase_node.id,
                        retry_reason=(
                            "BKB proposal rejected: "
                            f"{promotion_result.get('reason', '')}"
                        ),
                    )
                    yield envelope
                    return result

            if manifest.id == "execute":
                completed = self._latest_completed_subtask_from_phase(
                    phase_node=phase_node,
                    outcome=outcome,
                )
                review_manifest_id = str(
                    getattr(manifest, "mini_review_after", "") or ""
                ).strip()
                if completed is not None and review_manifest_id:
                    scheduled = self._schedule_subtask_review(
                        plan=plan,
                        execute_node=phase_node,
                        completed_subtask=completed,
                        review_manifest_id=review_manifest_id,
                        run_id=run_id,
                    )
                    result = PhaseResult(
                        phase_id=phase_node.id,
                        outcome="done",
                        artifacts={"scheduled_phase": scheduled},
                    )
            elif manifest.id == "subtask_review":
                self._set_reviewed_subtask_verdict(
                    plan=plan,
                    review_node=phase_node,
                    verdict="ok",
                )
                self._resume_execute_after_subtask_review(
                    plan=plan,
                    review_node=phase_node,
                )

        phase_node.status = "done"
        phase_node.ended_at = time.time()
        phase_node.overlay = None
        save_plan(plan, self._drive_root)

        yield self._emit(ResultEnvelope.success(
            data={
                "event": "phase_done",
                "phase": phase_node.id,
                "outcome": result.outcome,
                "events": outcome.get("event_count", 0),
            },
            run_id=run_id,
            phase=phase_node.id,
            took_ms=int(
                (phase_node.ended_at - (phase_node.started_at or phase_node.ended_at)) * 1000
            ),
        ))
        return result

    def _run_phase_single(
        self, task: dict[str, Any], phase_node: PhaseNode, *, run_id: str
    ) -> dict[str, Any]:
        launcher = self._ensure_launcher()
        try:
            handle = launcher.submit_task(task, timeout=self._phase_timeout_seconds) \
                if hasattr(launcher, "submit_task") else None
            if handle is None:
                return {"status": "error", "error": "launcher.submit_task returned None"}
            phase_started_at = float(phase_node.started_at or time.time())
            worker_pid = (
                getattr(handle, "worker_pid", None)
                or getattr(handle, "process_pid", None)
                or getattr(handle, "pid", None)
            )
            poll_sec = max(1, int(self._watcher._poll_sec))
            while True:
                outcome = handle.wait(timeout=float(poll_sec))
                if outcome is not None:
                    outcome["event_count"] = len(outcome.get("events") or [])
                    return outcome
                self._watcher.tick(
                    phase=phase_node.id,
                    phase_started_at=phase_started_at,
                    worker_pid=worker_pid,
                )
                pending = self._watcher.read_pending_signal()
                if pending is not None:
                    return {
                        "status": "watcher",
                        "task_id": task.get("id"),
                        "watcher_signal": pending.kind,
                        "watcher_signal_id": pending.signal_id,
                    }
        except Exception as exc:
            log.error("Phase %s launcher invocation failed", phase_node.id, exc_info=True)
            return {"status": "error", "error": str(exc)}

    def _run_phase_with_harness(
        self,
        base_task: dict[str, Any],
        phase_node: PhaseNode,
        manifest: Any,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Run N candidates in parallel for this phase, pick the winner.

        Each candidate gets an isolated task_id and mutated prompt; after all complete
        the Watcher (or a heuristic) selects the best result and that one is promoted
        as the phase outcome.
        """
        import concurrent.futures

        launcher = self._ensure_launcher()
        candidates: list[dict[str, Any]] = []
        for k in range(self._candidates_per_phase):
            task_k = dict(base_task)
            task_k["id"] = f"{base_task['id']}:c{k}"
            task_k["input"] = (
                base_task["input"]
                + f"\n\n## Candidate {k+1} of {self._candidates_per_phase}\n"
                "Explore one specific approach; differ from sibling candidates."
            )
            task_k["candidate_isolation"] = True
            candidates.append(task_k)

        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._candidates_per_phase, 4)
        ) as executor:
            futures = {
                executor.submit(self._submit_candidate, launcher, c, phase_node): c
                for c in candidates
            }
            for fut in concurrent.futures.as_completed(futures):
                cand = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {"status": "error", "error": str(exc), "task_id": cand["id"]}
                res["_candidate_id"] = cand["id"]
                results.append(res)

        winner = self._pick_candidate_winner(results, phase_id=phase_node.id, run_id=run_id)
        winner["event_count"] = len(winner.get("events") or [])
        winner["harness_candidates"] = [
            {"id": r.get("_candidate_id"), "status": r.get("status")} for r in results
        ]
        return winner

    def _submit_candidate(
        self,
        launcher: Any,
        task: dict[str, Any],
        phase_node: PhaseNode,
    ) -> dict[str, Any]:
        handle = launcher.submit_task(task, timeout=self._phase_timeout_seconds)
        worker_pid = (
            getattr(handle, "worker_pid", None)
            or getattr(handle, "process_pid", None)
            or getattr(handle, "pid", None)
        )
        phase_started_at = float(phase_node.started_at or time.time())
        poll_sec = max(1, int(self._watcher._poll_sec))
        while True:
            outcome = handle.wait(timeout=float(poll_sec))
            if outcome is not None:
                return outcome
            self._watcher.tick(
                phase=phase_node.id,
                phase_started_at=phase_started_at,
                worker_pid=worker_pid,
            )
            pending = self._watcher.read_pending_signal()
            if pending is not None:
                return {
                    "status": "watcher",
                    "task_id": task.get("id"),
                    "watcher_signal": pending.kind,
                    "watcher_signal_id": pending.signal_id,
                }

    def _pick_candidate_winner(
        self, results: list[dict[str, Any]], *, phase_id: str, run_id: str
    ) -> dict[str, Any]:
        """Pick best candidate. Heuristic: prefer complete > recovered > error;
        within same tier, more events wins."""
        if not results:
            return {"status": "error", "error": "no candidates ran"}

        def score(r: dict[str, Any]) -> tuple[int, int]:
            status = r.get("status", "")
            tier = {"complete": 3, "recovered": 2, "ok": 2}.get(status, 0)
            return (tier, len(r.get("events") or []))

        return max(results, key=score)


def run_phases(
    task_input: str,
    *,
    repo_root: pathlib.Path,
    workspace_id: str,
    phases: list[str] | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    launcher: Any = None,
    candidates_per_phase: int = 1,
    on_envelope: Callable[[ResultEnvelope], None] | None = None,
) -> Iterator[ResultEnvelope]:
    runner = PhaseRunner(
        repo_root=repo_root,
        workspace_id=workspace_id,
        launcher=launcher,
        candidates_per_phase=candidates_per_phase,
        on_envelope=on_envelope,
    )
    yield from runner.run(task_input, phases=phases, run_id=run_id, dry_run=dry_run)

