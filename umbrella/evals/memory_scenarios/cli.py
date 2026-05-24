"""CLI for Memory Scenario Harness."""

import argparse
import json
import sys
from pathlib import Path

from umbrella.evals.memory_scenarios.fixtures import REPO_ROOT, SCENARIOS_DIR
from umbrella.evals.memory_scenarios.reports import print_terminal_summary
from umbrella.evals.memory_scenarios.runner import (
    MemoryScenarioRunner,
    run_all_scenarios,
    run_scenario_by_id,
)
from umbrella.evals.memory_scenarios.scenario_loader import list_scenario_paths, load_scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Memory Scenario Harness / Audit Lab")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available scenarios")

    run_p = sub.add_parser("run", help="Run scenario(s)")
    run_p.add_argument("--scenario", help="Scenario id or yaml stem")
    run_p.add_argument("--all", action="store_true")
    run_p.add_argument("--phase", help="Filter matrix scenarios by phase name")
    run_p.add_argument("--verbose", action="store_true")
    run_p.add_argument("--keep-tmp", action="store_true")
    run_p.add_argument("--fail-fast", action="store_true")
    run_p.add_argument("--update-golden", action="store_true")
    run_p.add_argument("--json", action="store_true")
    run_p.add_argument(
        "--report-dir",
        default=str(REPO_ROOT / ".mrt" / "memory_scenarios"),
    )

    inspect_p = sub.add_parser("inspect", help="Show last report path")
    inspect_p.add_argument("--scenario", required=True)
    inspect_p.add_argument("--open-report", action="store_true")

    args = parser.parse_args(argv)
    report_root = Path(args.report_dir) if hasattr(args, "report_dir") else REPO_ROOT / ".mrt" / "memory_scenarios"

    if args.command == "list":
        for path in list_scenario_paths():
            sc = load_scenario(path)
            print(f"{path.stem}\t{sc.id}\t{sc.mode}")
        return 0

    if args.command == "inspect":
        report = report_root / args.scenario / "report.md"
        if not report.is_file():
            print(f"no report: {report}", file=sys.stderr)
            return 1
        print(report)
        if args.open_report:
            print(report.read_text(encoding="utf-8"))
        return 0

    if args.command == "run":
        kwargs = {
            "report_root": report_root,
            "verbose": args.verbose,
            "keep_tmp": args.keep_tmp,
            "update_golden": args.update_golden,
        }
        if args.all:
            result = run_all_scenarios(fail_fast=args.fail_fast, **kwargs)
            if args.json:
                print(json.dumps(result.dashboard, indent=2))
            return 0 if result.ok else 1
        if not args.scenario:
            print("specify --scenario or --all", file=sys.stderr)
            return 2
        result = run_scenario_by_id(args.scenario, **kwargs)
        if args.json:
            print(json.dumps({"ok": result.ok, "failures": result.invariant_failures}, indent=2))
        else:
            print(result.summary_text)
        return 0 if result.ok else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
