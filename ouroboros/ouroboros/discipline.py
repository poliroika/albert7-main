"""Loop discipline: track edits between verify runs and surface reminders.

Background
----------
A failure mode we observed in long Ouroboros runs (see JKX post-mortem):
the agent writes 100+ files in a row with zero verification, then at
``MAX_ROUNDS`` discovers that a basic integration is broken (e.g. dataset
emits Russian category names, pipeline returns English ones) and runs out
of budget before it can even acknowledge the bug.

This module provides a *soft* discipline gate: a counter of write-style
tool calls since the last verification run. When the counter crosses a
threshold, the loop injects a strong system reminder telling the model
"you've made N edits since last verify; recommended to run
``run_workspace_verify`` before adding more code". Tool schemas stay
intact — the model can still ignore the reminder, but it will see a fresh
nudge every ``REMIND_INTERVAL`` rounds until it does verify.

Design choices
--------------
* **Soft, not hard**. We do not strip tool schemas (that has its own
  failure mode — see how the ``MAX_ROUNDS`` exit path used to leave the
  model unable to call any tool). The user explicitly preferred the
  soft variant after weighing the tradeoff.
* **Per-workspace counters**. A single Ouroboros run can touch multiple
  workspaces; each gets its own counter so the JKX edit count doesn't
  reset when the agent reads a polymarket file.
* **Threshold configurable** via env var ``OUROBOROS_VERIFY_GATE_EDITS``
  (default 50). Set to ``0`` to disable.
* **Reminder rate-limited**. We re-inject the reminder at most every
  ``REMIND_INTERVAL`` rounds so the system message stream doesn't drown
  the conversation while the agent is in the middle of a coherent
  multi-step task.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Dict

from collections.abc import Iterable

log = logging.getLogger(__name__)

# Tool names that count as "writes" against the verify budget. These are
# the same tools whose failure-to-verify caused the JKX disaster: lots of
# updates, zero verification.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "update_workspace_seed",
        "apply_workspace_patch",
        "update_workspace_from_instance",
        "commit_workspace_changes",
        "repo_write_commit",
        "repo_commit_push",
        "sandbox_self_edit",
    }
)

# Tools that reset the verify counter (the agent has just verified, so
# the budget is fresh again).
VERIFY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "run_workspace_verify",
    }
)

# Re-inject the reminder at most every this many rounds. Keeps the system
# message stream from looking like a stuck loop.
REMIND_INTERVAL: int = 5

DEFAULT_THRESHOLD: int = 50


def _resolve_threshold() -> int:
    """Read ``OUROBOROS_VERIFY_GATE_EDITS``; ``0`` (or invalid) disables.

    Kept as a free function (rather than a class attribute) so tests can
    monkeypatch the env var without instantiating the gate.
    """
    raw = os.environ.get("OUROBOROS_VERIFY_GATE_EDITS")
    if raw is None or raw == "":
        return DEFAULT_THRESHOLD
    try:
        value = int(raw)
    except (ValueError, TypeError):
        log.warning(
            "Invalid OUROBOROS_VERIFY_GATE_EDITS=%r, defaulting to %d",
            raw,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD
    return max(0, value)


@dataclass
class VerifyGate:
    """Tracks edit-vs-verify ratio for each workspace touched by the loop.

    Lifecycle::

        gate = VerifyGate()
        ...
        for round in loop:
            for tool_call in this_round:
                gate.observe(tool_call.name, workspace_id=tool_call.ws)
            if gate.should_remind(round):
                messages.append(gate.build_reminder(round))
    """

    threshold: int = field(default_factory=_resolve_threshold)
    edits: dict[str, int] = field(default_factory=dict)
    last_remind_round: int = 0
    # Workspaces that already triggered a reminder at least once this
    # session, so we know which ones to mention by name in subsequent
    # ones (avoids repeating the same generic message).
    reminded_workspaces: set[str] = field(default_factory=set)

    def observe(self, tool_name: str, *, workspace_id: str = "") -> None:
        """Update internal counters from one tool invocation.

        ``tool_name`` is matched case-insensitively. ``workspace_id`` may
        be empty (e.g. for tools that don't carry a workspace) — those
        edits are bucketed under the ``""`` key and the gate will still
        fire if generic write activity piles up without a verify.
        """
        if not tool_name:
            return
        name = str(tool_name).strip()
        ws = (workspace_id or "").strip()
        if name in VERIFY_TOOL_NAMES:
            self.edits[ws] = 0
            self.reminded_workspaces.discard(ws)
            return
        if name in WRITE_TOOL_NAMES:
            self.edits[ws] = self.edits.get(ws, 0) + 1

    def hot_workspaces(self) -> list[tuple[str, int]]:
        """Return ``(workspace_id, edit_count)`` for every workspace whose
        counter is at or above the threshold, sorted by count descending.
        """
        if self.threshold <= 0:
            return []
        return sorted(
            ((ws, n) for ws, n in self.edits.items() if n >= self.threshold),
            key=lambda kv: kv[1],
            reverse=True,
        )

    def should_remind(self, round_idx: int) -> bool:
        """True iff at least one workspace is past the threshold and we
        haven't reminded too recently.
        """
        if self.threshold <= 0:
            return False
        if not self.hot_workspaces():
            return False
        if self.last_remind_round == 0:
            return True
        return (round_idx - self.last_remind_round) >= REMIND_INTERVAL

    def build_reminder(self, round_idx: int) -> dict[str, str]:
        """Construct the system message dict to append to ``messages``.

        Side effects: bumps ``last_remind_round`` and marks each hot
        workspace as reminded so the next call can phrase itself
        differently for first-time vs repeat warnings.
        """
        hot = self.hot_workspaces()
        self.last_remind_round = round_idx
        first_time: list[tuple[str, int]] = []
        repeat: list[tuple[str, int]] = []
        for ws, count in hot:
            if ws in self.reminded_workspaces:
                repeat.append((ws, count))
            else:
                first_time.append((ws, count))
                self.reminded_workspaces.add(ws)

        lines: list[str] = [
            f"[VERIFY_GATE] {sum(c for _, c in hot)} write-style edits since last "
            f"`run_workspace_verify` (threshold={self.threshold})."
        ]
        for ws, count in first_time:
            label = ws or "<no-workspace>"
            lines.append(
                f"  - {label}: {count} edits without verify. "
                f"Strongly recommended: call `run_workspace_verify(workspace_id='{label}')` "
                f"before writing more code. The integration bugs you cannot see "
                f"from a single file (schema mismatches, NameErrors in untouched "
                f"branches, broken end-to-end paths) only surface in verify."
            )
        for ws, count in repeat:
            label = ws or "<no-workspace>"
            lines.append(
                f"  - {label}: still {count} edits without verify. "
                f"This is the second nudge — please run "
                f"`run_workspace_verify(workspace_id='{label}')` now."
            )
        lines.append(
            "If verify steps are missing or wrong for this workspace, fix "
            "[verification].steps in workspace.toml first — that gate is what "
            "tells you whether your code actually works."
        )
        return {"role": "system", "content": "\n".join(lines)}


def collect_workspace_ids_from_args(args: Iterable[object]) -> list[str]:
    """Best-effort extraction of ``workspace_id`` from a tool-call args
    payload. Used by ``loop.py`` to feed the gate without committing to
    a single args schema.

    The loop sees a wide variety of arg shapes (positional dict, JSON
    string, kwargs). We only look at the obvious ``workspace_id`` key in
    a top-level dict — anything else is treated as "no workspace", which
    the gate aggregates under ``""``.
    """
    out: list[str] = []
    for item in args:
        if isinstance(item, dict):
            ws = item.get("workspace_id")
            if isinstance(ws, str) and ws.strip():
                out.append(ws.strip())
    return out
