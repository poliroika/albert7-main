import json
import logging
import os
import pathlib
import time
import uuid
from typing import Any

from umbrella.phases.base import WatcherSignal
from umbrella.orchestrator.watcher_triggers import WatcherTriggers, TriggerEvent

log = logging.getLogger(__name__)


class WatcherPollLoop:
    """Idle-by-default watcher. Polls trigger heuristics and calls the LLM
    only when a trigger fires.

    LLM model selection: set ``OUROBOROS_WATCHER_MODEL`` in the environment
    to override the default model used for watcher decisions (e.g.
    ``OUROBOROS_WATCHER_MODEL=openai/gpt-4o-mini``). When unset, the
    ``LLMClient.default_model()`` is used.
    """

    def __init__(
        self,
        drive_root: pathlib.Path,
        *,
        llm_client: Any | None = None,
        poll_sec: int | None = None,
    ) -> None:
        self._drive = drive_root
        self._llm_client = llm_client
        self._poll_sec = poll_sec or int(os.environ.get("OUROBOROS_WATCHER_POLL_SEC", "5"))
        self._triggers = WatcherTriggers(drive_root)
        self._running = False
        self._processed: set[str] = set()
        self._signal_path = drive_root / "state" / "watcher_signal.json"
        self._processed_path = drive_root / "state" / "watcher_signals.processed.jsonl"

    def write_signal(self, signal: WatcherSignal) -> None:
        self._signal_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._signal_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({
                "signal_id": signal.signal_id,
                "created_at": signal.created_at,
                "kind": signal.kind,
                "reason": signal.reason,
                "trigger": signal.trigger,
                "payload": signal.payload,
            }),
            encoding="utf-8",
        )
        os.replace(tmp, self._signal_path)

    def read_pending_signal(self) -> WatcherSignal | None:
        if not self._signal_path.exists():
            return None
        try:
            data = json.loads(self._signal_path.read_text())
        except Exception:
            return None
        sid = data.get("signal_id", "")
        if sid in self._processed:
            return None
        return WatcherSignal(
            signal_id=sid,
            created_at=data.get("created_at", time.time()),
            kind=data["kind"],
            reason=data.get("reason", ""),
            trigger=data.get("trigger", ""),
            payload=data.get("payload"),
        )

    def mark_processed(self, signal_id: str) -> None:
        self._processed.add(signal_id)
        self._processed_path.parent.mkdir(parents=True, exist_ok=True)
        with self._processed_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"signal_id": signal_id, "processed_at": time.time()}) + "\n")

    def tick(self, *, phase: str, phase_started_at: float, worker_pid: int | None = None) -> WatcherSignal | None:
        trigger = self._triggers.check(phase=phase, phase_started_at=phase_started_at)
        if trigger is None:
            trigger = self._triggers.check_worker_alive(worker_pid)
        if trigger is None:
            return None
        return self._invoke_llm_for_trigger(trigger, phase=phase)

    def _invoke_llm_for_trigger(self, trigger: TriggerEvent, *, phase: str) -> WatcherSignal | None:
        auto_signal = self._deterministic_signal_for_trigger(trigger, phase=phase)
        if auto_signal is not None:
            self.write_signal(auto_signal)
            return auto_signal
        try:
            system_prompt = self._load_watcher_prompt()
            user_msg = json.dumps(
                {"trigger": trigger.kind, "context": trigger.context, "phase": phase},
                ensure_ascii=False,
            )
            llm = self._get_llm()
            model = os.environ.get("OUROBOROS_WATCHER_MODEL") or llm.default_model()
            response_msg, _usage = llm.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                model=model,
                reasoning_effort="low",
                max_tokens=512,
            )
            content = (response_msg.get("content") or "").strip()
            kind, reason = self._parse_watcher_response(content, trigger)
            if kind == "ok":
                return None
            signal = WatcherSignal(
                signal_id=str(uuid.uuid4()),
                created_at=time.time(),
                kind=kind,
                reason=reason,
                trigger=trigger.kind,
                payload=trigger.context,
            )
            self.write_signal(signal)
            return signal
        except Exception as exc:
            log.warning("Watcher LLM call failed: %s", exc)
            if trigger.kind in ("worker_panic",):
                signal = WatcherSignal(
                    signal_id=str(uuid.uuid4()),
                    created_at=time.time(),
                    kind="abort_phase",
                    reason=f"auto: {trigger.kind}",
                    trigger=trigger.kind,
                    payload=trigger.context,
                )
                self.write_signal(signal)
                return signal
            return None

    def _get_llm(self) -> Any:
        if self._llm_client is not None:
            return self._llm_client
        import sys
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "ouroboros"))
        from ouroboros.llm import LLMClient
        return LLMClient()

    def _load_watcher_prompt(self) -> str:
        prompt_path = pathlib.Path(__file__).parent.parent / "prompts" / "watcher.system.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return (
            "You are the Watcher agent monitoring a running phase. "
            "When a trigger fires, decide whether to intervene. "
            'Respond with JSON: {"signal": "ok"|"abort_phase"|"restart_phase"|"force_verify", "reason": "..."}. '
            "Only intervene if clearly needed. Prefer \"ok\" when in doubt."
        )

    def _parse_watcher_response(self, content: str, trigger: TriggerEvent) -> tuple[str, str]:
        try:
            data = json.loads(content)
            return data.get("signal", "ok"), data.get("reason", "")
        except Exception:
            for keyword in ("abort_phase", "restart_phase", "force_verify", "inject_lesson"):
                if keyword in content.lower():
                    return keyword, content[:200]
            return "ok", ""

    def _deterministic_signal_for_trigger(
        self, trigger: TriggerEvent, *, phase: str
    ) -> WatcherSignal | None:
        if trigger.kind == "repeat_semantic_failure":
            category = str((trigger.context or {}).get("category") or "").strip()
            reason = (
                "Repeated semantic tool failure"
                + (f" ({category})" if category else "")
                + (
                    f" during phase `{phase}`. Abort the phase so the runner can "
                    "repair the missing evidence/context instead of spending more "
                    "rounds on the same rejected contract."
                )
            )
            return WatcherSignal(
                signal_id=str(uuid.uuid4()),
                created_at=time.time(),
                kind="abort_phase",
                reason=reason,
                trigger=trigger.kind,
                payload=trigger.context,
            )
        if trigger.kind == "repeat_structural_layout":
            return WatcherSignal(
                signal_id=str(uuid.uuid4()),
                created_at=time.time(),
                kind="abort_phase",
                reason=(
                    "Repeated structural layout block during execute; aborting "
                    "phase so the next attempt can inspect the declared layout "
                    "policy and repair the file placement."
                ),
                trigger=trigger.kind,
                payload=trigger.context,
            )
        return None

    def stop(self) -> None:
        self._running = False
