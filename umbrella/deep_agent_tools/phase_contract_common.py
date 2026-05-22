"""Shared imports for Umbrella phase-contract tools."""

import importlib
import json
import os
import pathlib
import platform
import re
import time
import uuid
from typing import Any, Iterator

from ouroboros.tools import umbrella_tools
from ouroboros.tools.registry import ToolContext, ToolEntry

_LOCALHOST_E2E_RE = re.compile(
    r"(?:localhost|127\.0\.0\.1|web\s*ui|browser\s*(?:game|app)?|playwright|http://localhost)",
    re.IGNORECASE,
)
_LOCALHOST_PROOF_RE = re.compile(
    r"(?:localhost|127\.0\.0\.1|http://|https://127|browser|playwright|page\.goto)",
    re.IGNORECASE,
)

__all__ = [
    "Any",
    "Iterator",
    "ToolContext",
    "ToolEntry",
    "importlib",
    "json",
    "os",
    "pathlib",
    "platform",
    "re",
    "time",
    "umbrella_tools",
    "uuid",
    "_LOCALHOST_E2E_RE",
    "_LOCALHOST_PROOF_RE",
]
