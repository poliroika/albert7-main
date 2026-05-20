from umbrella.permissions.envelope import PermissionEnvelope, EnvelopeResult

_WATCHER_ALLOWED_TOOLS = frozenset({
    "palace_search",
    "read_drive_log",
    "read_terminal_scrollback",
    "palace_walk",
    "palace_health",
    # control signals (write to signal file only, not repo)
    "request_abort_phase",
    "request_restart_phase",
    "request_mutate_phase_plan",
    "force_verify",
    "inject_lesson",
    "request_human_checkpoint",
    "request_watcher_review",
})

_WATCHER_WRITE_STORES = frozenset({"palace.run"})  # incident nodes only


class WatcherEnvelope:
    """Hardcoded read-only envelope for Watcher. Cannot be overridden by YAML."""

    def check(
        self,
        tool_name: str,
        *,
        paths: list[str] | None = None,
        cmd: str | None = None,
        scope_arg: str | None = None,
    ) -> EnvelopeResult:
        if tool_name in _WATCHER_ALLOWED_TOOLS:
            return EnvelopeResult(True)
        return EnvelopeResult(False, f"watcher envelope: tool '{tool_name}' not in allowed set")


_WATCHER_ENVELOPE = WatcherEnvelope()


def get_watcher_envelope() -> WatcherEnvelope:
    return _WATCHER_ENVELOPE
