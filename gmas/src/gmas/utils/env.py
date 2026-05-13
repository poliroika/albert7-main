"""Console and environment helpers for examples and scripts."""

import os
import sys
from pathlib import Path


def configure_console() -> None:
    """Prefer UTF-8 output so scripts work in Windows terminals."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # ty:ignore[call-non-callable]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")  # ty:ignore[call-non-callable]


def load_dotenv_file(path: str | Path) -> None:
    """
    Load KEY=VALUE pairs from *path* without overriding existing variables.

    A minimal .env loader that avoids pulling in ``python-dotenv``.
    Lines that are blank, start with ``#``, or contain no ``=`` are skipped.
    """
    p = Path(path)
    if not p.exists():
        return

    with p.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key or os.environ.get(key):
                continue

            os.environ[key] = value.strip().strip('"').strip("'")
