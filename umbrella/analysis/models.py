"""Common static-analysis result types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StaticAnalysisIssue:
    code: str
    message: str
    path: str = ""
    line: int = 0
    snippet: str = ""
