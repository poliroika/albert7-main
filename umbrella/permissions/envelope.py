import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class EnvelopeResult:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class PermissionEnvelope:
    """
    Evaluates whether a tool call is allowed for a given phase.

    Rules are evaluated top-to-bottom; first match wins.
    After phase rules, global deny rules are applied (they can override allow).
    """

    def __init__(
        self,
        phase_rules: list[dict[str, Any]],
        global_deny_rules: list[dict[str, Any]] | None = None,
        context_vars: dict[str, str] | None = None,
    ) -> None:
        self._phase_rules = phase_rules
        self._global_deny = global_deny_rules or []
        self._vars = context_vars or {}

    def _substitute(self, pattern: str) -> str:
        for k, v in self._vars.items():
            pattern = pattern.replace(f"${{{k}}}", v)
        return pattern

    def _match_paths(self, patterns: list[str], paths: list[str]) -> bool:
        for pattern in patterns:
            p = self._substitute(pattern)
            for path in paths:
                if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p.lstrip("*/")):
                    return True
        return False

    def _match_cmd(self, cmd_re: str | None, cmd: str | None) -> bool:
        if cmd_re is None or cmd is None:
            return cmd_re is None  # no cmd constraint = always matches
        return bool(re.search(cmd_re, cmd))

    def _rule_applies_to_tool(self, rule: dict[str, Any], tool_name: str) -> bool:
        if "allow_tools" in rule:
            return tool_name in rule["allow_tools"]
        if "allow_tool" in rule:
            return tool_name == rule["allow_tool"]
        if "deny_tools" in rule:
            return tool_name in rule["deny_tools"]
        if "deny_tool" in rule:
            return tool_name == rule["deny_tool"]
        if "deny_path" in rule:
            return True  # path rules apply to all tools
        return False

    def _rule_is_allow(self, rule: dict[str, Any]) -> bool:
        return "allow_tool" in rule or "allow_tools" in rule

    def _check_args_match(
        self,
        rule: dict[str, Any],
        paths: list[str],
        cmd: str | None,
        scope_arg: str | None,
    ) -> bool:
        """Return True if the rule's arg constraints all match."""
        args = rule.get("args", {})
        if "cmd_re" in args and not self._match_cmd(args["cmd_re"], cmd):
            return False
        if "scope" in args and scope_arg is not None and args["scope"] != scope_arg:
            return False
        if "working_directory" in args and paths:
            if not self._match_paths([args["working_directory"]], paths):
                return False
        return True

    def check(
        self,
        tool_name: str,
        *,
        paths: list[str] | None = None,
        cmd: str | None = None,
        scope_arg: str | None = None,
    ) -> EnvelopeResult:
        paths = paths or []

        # deny_path rules always take priority — scan phase rules first
        for rule in self._phase_rules:
            if "deny_path" in rule and paths:
                if self._match_paths(rule["deny_path"], paths):
                    return EnvelopeResult(False, f"path denied by rule: {rule['deny_path']}")

        # Phase rules: first matching non-path rule wins
        phase_result: EnvelopeResult | None = None
        for rule in self._phase_rules:
            if "deny_path" in rule:
                continue  # already evaluated above
            if not self._rule_applies_to_tool(rule, tool_name):
                continue
            if not self._check_args_match(rule, paths, cmd, scope_arg):
                continue
            if self._rule_is_allow(rule):
                phase_result = EnvelopeResult(True)
            else:
                return EnvelopeResult(False, f"denied by rule: {rule}")
            break

        # Global deny rules override a phase allow
        for rule in self._global_deny:
            if not self._rule_applies_to_tool(rule, tool_name):
                continue
            if "deny_path" in rule and paths:
                if self._match_paths(rule["deny_path"], paths):
                    return EnvelopeResult(False, f"path denied by global rule: {rule['deny_path']}")
            if not self._check_args_match(rule, paths, cmd, scope_arg):
                continue
            if not self._rule_is_allow(rule):
                return EnvelopeResult(False, f"denied by global rule: {rule}")

        if phase_result is not None:
            return phase_result

        return EnvelopeResult(False, f"no allow rule matched for '{tool_name}'")


DENIED = EnvelopeResult(False, "watcher read-only envelope")
ALLOWED = EnvelopeResult(True)
