"""kpubdata-builder용 명령줄 진입점."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .errors import SpecLoadError, ValidationError
from .spec import load_spec
from .validator import validate_spec

_RESERVED_COMMANDS: frozenset[str] = frozenset({"preview", "build"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kpubdata-builder",
        description="KPubData Builder command-line interface.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    validate_cmd = subparsers.add_parser(
        "validate",
        help="Validate a BuildSpec YAML file.",
    )
    validate_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")

    preview_cmd = subparsers.add_parser(
        "preview",
        help="Preview a BuildSpec execution (reserved; not implemented yet).",
    )
    preview_cmd.add_argument("spec", nargs="?", help="Path to the BuildSpec YAML file.")

    build_cmd = subparsers.add_parser(
        "build",
        help="Execute a BuildSpec to produce artifacts (reserved; not implemented yet).",
    )
    build_cmd.add_argument("spec", nargs="?", help="Path to the BuildSpec YAML file.")

    return parser


def _run_validate(spec_path: str) -> int:
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"spec is valid: {spec.dataset_id}")
    return 0


def _run_reserved(command: str) -> int:
    print(
        f"error: '{command}' is not implemented yet "
        "(pipeline orchestrator pending — see issue #43)",
        file=sys.stderr,
    )
    return 1


def dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "validate":
        return _run_validate(args.spec)
    if command in _RESERVED_COMMANDS:
        return _run_reserved(command)
    # 일반적인 CLI 경로로는 도달할 수 없지만(argparse가 알 수 없는 하위 명령을 거부함),
    # 프로그래밍 방식 호출자를 위한 방어적 대체 경로로 유지한다.
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    return dispatch(args)


__all__ = ["build_parser", "dispatch", "main"]
