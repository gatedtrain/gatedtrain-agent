"""The `gatedtrain-agent` console script."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gatedtrain_agent.client import Client
from gatedtrain_agent.walker import walk


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gatedtrain-agent", description="Drivers for gatedtrain AI-native projects")
    sub = parser.add_subparsers(dest="command", required=True)
    do = sub.add_parser("do", help="walk a project's stages and stop where a person is needed")
    do.add_argument("root", help="project root")
    do.add_argument("--goal")
    do.add_argument("--data")
    do.add_argument("--pipeline")
    do.add_argument("--keeper", help="keeper socket path")
    do.add_argument("--gpu-hours")
    do.add_argument("--accept-inconclusive", action="store_true",
                    help="accept an inconclusive data size (the result is reported INCONCLUSIVE)")
    do.set_defaults(func=_do)
    sub.add_parser("mcp", help="serve the MCP tools over stdio").set_defaults(func=_mcp)
    sub.add_parser("skill-path", help="print the path of the packaged operator skill").set_defaults(func=_skill_path)
    return parser


def _do(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    given = [args.goal, args.data, args.pipeline]
    if any(given) and not all(given):
        parser.error("--goal, --data and --pipeline go together")
    root = Path(args.root).resolve()
    client = Client()
    if all(given):
        cmd = ["project", "create", str(root), "--goal", args.goal, "--data", args.data, "--pipeline", args.pipeline]
        if args.keeper:
            cmd += ["--keeper", args.keeper]
        if args.gpu_hours:
            cmd += ["--gpu-hours", args.gpu_hours]
        env = client.call(cmd)
        if not env.ok:
            print("create: refused")
            for line in env.problem_lines():
                print(f"  {line}")
            return 3
        print("create: done")
    ask = input if sys.stdin.isatty() else None
    return walk(client, root, accept_inconclusive=args.accept_inconclusive, ask=ask, out=sys.stdout)


def skill_path() -> str:
    return str(Path(__file__).resolve().parent / "skill" / "SKILL.md")


def _skill_path(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    print(skill_path())
    return 0


def _mcp(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from gatedtrain_agent.mcp_server import serve
    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args, parser)


if __name__ == "__main__":
    sys.exit(main())
