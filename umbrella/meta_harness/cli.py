"""Meta-Harness experience CLI.

Usage:
    python -m umbrella.meta_harness.cli list
    python -m umbrella.meta_harness.cli top --experiment latest --n 10
    python -m umbrella.meta_harness.cli show <candidate_id>
    python -m umbrella.meta_harness.cli trace <candidate_id> --type errors
    python -m umbrella.meta_harness.cli diff <candidate_a> <candidate_b>
    python -m umbrella.meta_harness.cli frontier --x total_tokens --y avg_score
    python -m umbrella.meta_harness.cli failures --workspace <workspace_id>
"""

import argparse
import json
import sys
from pathlib import Path

from umbrella.meta_harness.store import MetaHarnessStore, get_default_store


def _get_store(args: argparse.Namespace) -> MetaHarnessStore:
    repo_root = Path(getattr(args, "repo_root", ".")).resolve()
    return get_default_store(repo_root)


def _resolve_experiment_id(store: MetaHarnessStore, experiment_arg: str) -> str:
    if experiment_arg == "latest":
        exp = store.get_latest_experiment()
        if exp is None:
            print("No experiments found.")
            sys.exit(1)
        return exp.id
    return experiment_arg


def _fmt_score(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:.3f}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    store = _get_store(args)
    experiments = store.list_experiments()
    if not experiments:
        print("No experiments found.")
        return

    for exp in experiments:
        cand_count = len(exp.candidate_ids)
        print(
            f"  {exp.id}  status={exp.status}  workspace={exp.workspace_id}  "
            f"candidates={cand_count}  best_score={_fmt_score(exp.best_score)}  "
            f"iterations={exp.iterations_completed}"
        )


def cmd_top(args: argparse.Namespace) -> None:
    store = _get_store(args)
    exp_id = _resolve_experiment_id(store, args.experiment)
    pairs = store.top_candidates(exp_id, n=args.n, sort_by=args.sort)

    if not pairs:
        print(f"No candidates in experiment {exp_id}.")
        return

    print(f"Top {len(pairs)} candidates for {exp_id}:")
    for i, (cand, ev) in enumerate(pairs, 1):
        score = _fmt_score(ev.avg_score if ev else None)
        print(
            f"  {i}. {cand.candidate_id}  score={score}  "
            f"status={cand.run_status}  writes={cand.write_calls}  "
            f"cost=${cand.cost_usd:.4f}  ws={cand.workspace_id}"
        )


def cmd_show(args: argparse.Namespace) -> None:
    store = _get_store(args)
    candidate = store.find_candidate(args.candidate_id)
    if candidate is None:
        print(f"Candidate {args.candidate_id} not found.")
        sys.exit(1)

    data = candidate.model_dump(mode="json")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    ev = store.get_eval(args.candidate_id)
    if ev:
        print("\n--- Evaluation ---")
        print(json.dumps(ev.model_dump(mode="json"), indent=2, ensure_ascii=False))

    decision = store.get_promotion_decision(args.candidate_id)
    if decision:
        print("\n--- Promotion Decision ---")
        print(
            json.dumps(decision.model_dump(mode="json"), indent=2, ensure_ascii=False)
        )


def cmd_trace(args: argparse.Namespace) -> None:
    store = _get_store(args)
    cand_dir = store.find_candidate_dir(args.candidate_id)
    if cand_dir is None:
        print(f"Candidate {args.candidate_id} not found.")
        sys.exit(1)

    trace_type = args.type
    if trace_type == "errors":
        events = store.get_execution_events(args.candidate_id)
        errors = [
            e for e in events if e.get("type") in ("error", "exception", "failure")
        ]
        if not errors:
            # Fallback: show events with error-like content
            errors = [e for e in events if "error" in json.dumps(e).lower()]
        for e in errors[: args.limit]:
            print(json.dumps(e, ensure_ascii=False))
    elif trace_type == "all":
        events = store.get_execution_events(args.candidate_id)
        for e in events[: args.limit]:
            print(json.dumps(e, ensure_ascii=False))
    elif trace_type == "diff":
        diff_path = cand_dir / "diffs" / "worktree.diff"
        if diff_path.exists():
            print(diff_path.read_text(encoding="utf-8")[: args.max_chars])
        else:
            print("No diff available.")
    elif trace_type == "prompt":
        for f in sorted((cand_dir / "prompt_snapshot").glob("*")):
            print(f"--- {f.name} ---")
            print(f.read_text(encoding="utf-8")[: args.max_chars])
    else:
        print(f"Unknown trace type: {trace_type}")


def cmd_diff(args: argparse.Namespace) -> None:
    store = _get_store(args)
    a = store.find_candidate(args.candidate_a)
    b = store.find_candidate(args.candidate_b)

    if a is None or b is None:
        print("One or both candidates not found.")
        sys.exit(1)

    ev_a = store.get_eval(args.candidate_a)
    ev_b = store.get_eval(args.candidate_b)

    print(f"Comparing {args.candidate_a} vs {args.candidate_b}:")
    print(f"  Status:     {a.run_status:>12}  vs  {b.run_status}")
    print(f"  Writes:     {a.write_calls:>12}  vs  {b.write_calls}")
    print(f"  Tool calls: {a.tool_calls:>12}  vs  {b.tool_calls}")
    print(f"  Cost:       ${a.cost_usd:>11.4f}  vs  ${b.cost_usd:.4f}")
    print(f"  Changes:    {len(a.changed_files):>12}  vs  {len(b.changed_files)}")

    if ev_a and ev_b:
        print(
            f"  Avg score:  {_fmt_score(ev_a.avg_score):>12}  vs  {_fmt_score(ev_b.avg_score)}"
        )
        delta = ev_b.avg_score - ev_a.avg_score
        print(f"  Delta:      {delta:>+12.3f}")


def cmd_frontier(args: argparse.Namespace) -> None:
    store = _get_store(args)
    exp_id = _resolve_experiment_id(store, args.experiment)
    pairs = store.top_candidates(exp_id, n=50, sort_by="time")

    if not pairs:
        print("No candidates.")
        return

    print(f"{'candidate':>30}  {args.x:>15}  {args.y:>15}")
    print("-" * 65)
    for cand, ev in pairs:
        x_val = getattr(cand, args.x, None) or (
            getattr(ev, args.x, None) if ev else None
        )
        y_val = getattr(cand, args.y, None) or (
            getattr(ev, args.y, None) if ev else None
        )
        print(f"  {cand.candidate_id:>28}  {str(x_val):>15}  {str(y_val):>15}")


def cmd_failures(args: argparse.Namespace) -> None:
    store = _get_store(args)
    exp_id = _resolve_experiment_id(store, args.experiment)
    failures = store.get_failures(exp_id, workspace_id=args.workspace or "")

    if not failures:
        print("No failures found.")
        return

    print(f"Failures in {exp_id}:")
    for cand, ev in failures:
        score = _fmt_score(ev.avg_score if ev else None)
        print(
            f"  {cand.candidate_id}  status={cand.run_status}  "
            f"score={score}  error={cand.error[:80]}  ws={cand.workspace_id}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="umbrella.meta_harness.cli",
        description="Meta-Harness experience navigator",
    )
    parser.add_argument("--repo-root", default=".", help="Repository root")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all experiments")

    p_top = sub.add_parser("top", help="Show top candidates")
    p_top.add_argument("--experiment", default="latest")
    p_top.add_argument("--n", type=int, default=10)
    p_top.add_argument("--sort", choices=["score", "cost", "time"], default="score")

    p_show = sub.add_parser("show", help="Show candidate details")
    p_show.add_argument("candidate_id")

    p_trace = sub.add_parser("trace", help="Inspect candidate traces")
    p_trace.add_argument("candidate_id")
    p_trace.add_argument(
        "--type", default="errors", choices=["errors", "all", "diff", "prompt"]
    )
    p_trace.add_argument("--limit", type=int, default=50)
    p_trace.add_argument("--max-chars", type=int, default=20000)

    p_diff = sub.add_parser("diff", help="Compare two candidates")
    p_diff.add_argument("candidate_a")
    p_diff.add_argument("candidate_b")

    p_frontier = sub.add_parser("frontier", help="Show Pareto frontier")
    p_frontier.add_argument("--experiment", default="latest")
    p_frontier.add_argument("--x", default="total_tokens")
    p_frontier.add_argument("--y", default="avg_score")

    p_failures = sub.add_parser("failures", help="Show failed candidates")
    p_failures.add_argument("--experiment", default="latest")
    p_failures.add_argument("--workspace", default="")

    args = parser.parse_args()

    commands = {
        "list": cmd_list,
        "top": cmd_top,
        "show": cmd_show,
        "trace": cmd_trace,
        "diff": cmd_diff,
        "frontier": cmd_frontier,
        "failures": cmd_failures,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
