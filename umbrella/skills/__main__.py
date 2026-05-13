"""CLI for Umbrella procedural skill lifecycle."""

import argparse
import json
from pathlib import Path

from umbrella.skills.promotion import (
    format_cli_payload,
    list_skills,
    promote_skill,
    retire_skill,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m umbrella.skills")
    parser.add_argument("--repo-root", default=".", help="Repository root path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_cmd = sub.add_parser("list", help="List skills")
    list_cmd.add_argument("--status", default="", help="Filter by status")

    promote_cmd = sub.add_parser("promote", help="Promote candidate skill to active")
    promote_cmd.add_argument("slug", help="Skill slug")

    retire_cmd = sub.add_parser("retire", help="Retire a skill")
    retire_cmd.add_argument("slug", help="Skill slug")
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo_root = Path(args.repo_root).resolve()

    if args.cmd == "list":
        skills = list_skills(repo_root, status=args.status or None)
        print(
            json.dumps(
                [
                    {
                        "slug": s.slug,
                        "name": s.name,
                        "status": s.status,
                        "domains": s.domains,
                        "path": str(s.path),
                    }
                    for s in skills
                ],
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd == "promote":
        result = promote_skill(repo_root, args.slug)
        print(format_cli_payload(result))
        return 0 if result.status == "promoted" else 2

    if args.cmd == "retire":
        result = retire_skill(repo_root, args.slug)
        print(format_cli_payload(result))
        return 0 if result.status == "retired" else 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
