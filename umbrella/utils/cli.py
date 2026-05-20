import argparse
import os
from typing import Any

from umbrella.utils.result_envelope import ResultEnvelope, emit


def add_output_args(parser: argparse.ArgumentParser) -> None:
    """Add standard --output-format and --stream args to any CLI parser."""
    default_fmt = os.environ.get("UMBRELLA_OUTPUT_FORMAT", "json")
    parser.add_argument(
        "--output-format",
        choices=["json", "pretty"],
        default=default_fmt,
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream NDJSON events to stdout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show loaded manifests/envelope/recall without LLM calls",
    )


def print_result(
    envelope: ResultEnvelope,
    *,
    output_format: str = "json",
) -> int:
    """Print envelope and return exit code (0=ok, 1=error)."""
    emit(envelope)
    if output_format == "pretty" and not envelope.ok:
        for err in envelope.errors:
            print(f"ERROR [{err.code}]: {err.message}", flush=True)
    return 0 if envelope.ok else 1
