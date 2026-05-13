"""
Terminal-based human checkpoint system.

Shows prompts in the terminal, waits for 1 minute for human input,
and allows Ouroboros/Umbrella to decide if no response.

This replaces file-based human notifications with interactive terminal checks.
"""

import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class HumanResponse(StrEnum):
    """Possible responses from human."""

    APPROVE = "approve"
    REJECT = "reject"
    SKIP = "skip"
    TIMEOUT = "timeout"


@dataclass
class TerminalCheckRequest:
    """A request for human review in the terminal."""

    id: str
    task_id: str
    stage: str
    prompt: str
    context: dict[str, Any] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)
    timeout_seconds: float = 60.0
    created_at: float = field(default_factory=time.time)


@dataclass
class TerminalCheckResult:
    """Result from terminal human check."""

    request_id: str
    response: HumanResponse
    human_input: str | None = None
    response_time_seconds: float | None = None
    timed_out: bool = False
    auto_decision: str | None = None


def request_human_review_terminal(
    request: TerminalCheckRequest,
) -> TerminalCheckResult:
    """Request human review via terminal prompt.

    Shows a prompt in the terminal, waits for input (with timeout),
    and returns the result.

    Args:
        request: Check request with prompt and options

    Returns:
        Result of the human check
    """
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    log.info("=" * 60)
    log.info("👤 HUMAN REVIEW REQUESTED")
    log.info("=" * 60)
    log.info(f"Stage: {request.stage}")
    log.info(f"Task: {request.task_id}")
    log.info("")
    log.info(request.prompt)
    log.info("")

    if request.options:
        log.info("Options:")
        for i, option in enumerate(request.options, 1):
            log.info(f"  {i}. {option}")
        log.info("")

    timeout_min = request.timeout_seconds / 60
    log.info(f"⏱️  Waiting {timeout_min:.1f} minutes for response...")
    log.info("    Type your choice and press Enter, or wait for timeout")
    log.info("")

    if not interactive:
        log.info("stdin is not interactive; human review will time out immediately")
        return TerminalCheckResult(
            request_id=request.id,
            response=HumanResponse.TIMEOUT,
            human_input=None,
            response_time_seconds=0.0,
            timed_out=True,
        )

    # Save terminal settings
    old_settings = None
    try:
        import select
        import tty
        import termios

        old_settings = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())
    except Exception:
        old_settings = None

    start_time = time.time()
    human_input = None
    timed_out = False
    buffered_input: dict[str, str | bool] = {}

    try:
        if old_settings:
            import select

            while True:
                elapsed = time.time() - start_time
                if elapsed >= request.timeout_seconds:
                    timed_out = True
                    break

                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    human_input = sys.stdin.readline().strip()
                    break
        else:

            def _read_input() -> None:
                try:
                    buffered_input["value"] = input().strip()
                except EOFError:
                    buffered_input["eof"] = True

            reader = threading.Thread(target=_read_input, daemon=True)
            reader.start()
            reader.join(request.timeout_seconds)
            if reader.is_alive():
                timed_out = True
            else:
                human_input = str(buffered_input.get("value") or "").strip() or None

    except KeyboardInterrupt:
        log.info("\nInterrupted by human")
        human_input = "skip"
    except EOFError:
        timed_out = True
    finally:
        # Restore terminal settings
        if old_settings:
            try:
                import termios

                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    response_time = time.time() - start_time

    if timed_out:
        log.info("")
        log.info("⏱️  TIMEOUT - No human response received")
        log.info("🤖 Ouroboros/Umbrella will make the decision")
        result = TerminalCheckResult(
            request_id=request.id,
            response=HumanResponse.TIMEOUT,
            human_input=None,
            response_time_seconds=response_time,
            timed_out=True,
        )
    elif human_input:
        log.info("")
        log.info(f"✓ Human input received: {human_input}")

        # Parse input
        input_lower = human_input.lower().strip()
        if input_lower in ["1", "approve", "yes", "y", "go"]:
            response = HumanResponse.APPROVE
        elif input_lower in ["2", "reject", "no", "n"]:
            response = HumanResponse.REJECT
        else:
            response = HumanResponse.SKIP

        result = TerminalCheckResult(
            request_id=request.id,
            response=response,
            human_input=human_input,
            response_time_seconds=response_time,
            timed_out=False,
        )
    else:
        # Fallback
        result = TerminalCheckResult(
            request_id=request.id,
            response=HumanResponse.SKIP,
            human_input=None,
            response_time_seconds=response_time,
            timed_out=False,
        )

    log.info("=" * 60)
    log.info(f"Result: {result.response.value}")
    log.info("=" * 60)

    return result


def make_auto_decision(
    request: TerminalCheckRequest,
    context: dict[str, Any],
) -> str:
    """Make an automatic decision when human doesn't respond.

    This allows Ouroboros/Umbrella to proceed without human input.

    Args:
        request: Original check request
        context: Context for making the decision

    Returns:
        Decision description
    """
    stage = request.stage.lower()
    context_info = context.get("eval_context") or {}

    # Simple rules for auto-decision
    if "outline" in stage:
        # For outline checks, approve if eval score is decent
        score = context_info.get("overall_score", 0.5)
        if score >= 0.5:
            decision = "Auto-approved: Outline looks acceptable (score {:.2f})".format(
                score
            )
        else:
            decision = "Auto-rejected: Outline needs improvement (score {:.2f})".format(
                score
            )

    elif "draft" in stage or "final" in stage:
        # For final drafts, be more cautious
        score = context_info.get("overall_score", 0.5)
        if score >= 0.7:
            decision = "Auto-approved: Final draft quality good (score {:.2f})".format(
                score
            )
        else:
            decision = (
                f"Auto-rejected: Final draft needs revision (score {score:.2f})"
            )

    else:
        # Default: proceed cautiously
        decision = "Auto-approved: Proceeding with caution (no human input)"

    log.info(f"🤖 AUTO DECISION: {decision}")
    return decision


def request_checkpoint_terminal(
    checkpoint_id: str,
    task_id: str,
    stage: str,
    prompt: str,
    *,
    context: dict[str, Any] | None = None,
    timeout_seconds: float = 60.0,
) -> TerminalCheckResult:
    """Convenience function to request a checkpoint via terminal.

    Args:
        checkpoint_id: Unique ID for this checkpoint
        task_id: Associated task ID
        stage: Stage name (e.g., "outline_approved", "final_draft")
        prompt: Prompt to show human
        context: Optional context for auto-decision
        timeout_seconds: How long to wait (default 60s)

    Returns:
        Result of the check
    """
    request = TerminalCheckRequest(
        id=checkpoint_id,
        task_id=task_id,
        stage=stage,
        prompt=prompt,
        context=context or {},
        timeout_seconds=timeout_seconds,
    )

    result = request_human_review_terminal(request)

    # If timeout, make auto decision
    if result.timed_out:
        auto_decision = make_auto_decision(request, context or {})
        result.auto_decision = auto_decision

    return result


def prompt_for_approval(
    question: str,
    *,
    timeout_seconds: float = 60.0,
    task_id: str = "current",
) -> bool:
    """Simple yes/no prompt in terminal.

    Args:
        question: Question to ask
        timeout_seconds: How long to wait
        task_id: Associated task ID

    Returns:
        True if approved, False otherwise
    """
    import uuid

    result = request_checkpoint_terminal(
        checkpoint_id=str(uuid.uuid4()),
        task_id=task_id,
        stage="approval",
        prompt=f"{question}\n\nRespond:\n  1. Yes/Approve\n  2. No/Reject",
        timeout_seconds=timeout_seconds,
    )

    if result.timed_out:
        # Default to cautious "no" on timeout
        return False

    return result.response == HumanResponse.APPROVE


# =============================================================================
# Integration with existing checkpoint system
# =============================================================================


class TerminalCheckpointAdapter:
    """Adapter to integrate terminal checks with existing checkpoint system."""

    def __init__(self, state_dir: Path | None = None):
        self.state_dir = (
            state_dir or Path(".umbrella") / "control_plane" / "checkpoints"
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def request_review(
        self,
        task_id: str,
        stage: str,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        timeout_seconds: float = 60.0,
    ) -> TerminalCheckResult:
        """Request a human review via terminal.

        Args:
            task_id: Task ID
            stage: Stage being reviewed
            prompt: Prompt to show
            context: Optional context
            timeout_seconds: Timeout in seconds

        Returns:
            Result of the check
        """
        import uuid

        checkpoint_id = f"terminal_{uuid.uuid4().hex[:8]}"

        log.info(f"Requesting terminal checkpoint: {checkpoint_id}")

        result = request_checkpoint_terminal(
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            stage=stage,
            prompt=prompt,
            context=context,
            timeout_seconds=timeout_seconds,
        )

        # Save result
        self._save_result(checkpoint_id, result)

        return result

    def _save_result(self, checkpoint_id: str, result: TerminalCheckResult) -> None:
        """Save checkpoint result to file."""
        result_file = self.state_dir / f"{checkpoint_id}.json"
        data = {
            "checkpoint_id": checkpoint_id,
            "response": result.response.value,
            "human_input": result.human_input,
            "response_time_seconds": result.response_time_seconds,
            "timed_out": result.timed_out,
            "auto_decision": result.auto_decision,
            "timestamp": datetime.now().isoformat(),
        }
        result_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
