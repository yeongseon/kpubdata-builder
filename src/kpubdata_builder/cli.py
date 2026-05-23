"""Command-line entrypoint for kpubdata-builder."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .errors import SpecLoadError, ValidationError
from .preview import PreviewResult, preview_build
from .spec import BuildSpec, load_spec
from .validator import validate_spec

_RESERVED_COMMANDS: frozenset[str] = frozenset({"build"})


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
        help="Preview a build's schema and sample records without writing artifacts.",
    )
    preview_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")

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


def _print_preview(spec: BuildSpec, preview: PreviewResult) -> None:
    print(f"preview: {spec.dataset_id}")
    print(f"schema: {', '.join(preview.schema) if preview.schema else '(empty)'}")
    shown = len(preview.sample_records)
    print(f"records ({shown} of {preview.total_records} shown):")
    for record in preview.sample_records:
        print(f"  {json.dumps(record, ensure_ascii=False, sort_keys=True)}")
    for warning in preview.warnings:
        print(f"warning: {warning}")


def _run_preview(spec_path: str) -> int:
    try:
        spec = load_spec(Path(spec_path))
        preview = preview_build(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    _print_preview(spec, preview)
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
    if command == "preview":
        return _run_preview(args.spec)
    if command in _RESERVED_COMMANDS:
        return _run_reserved(command)
    # Unreachable via normal CLI (argparse rejects unknown subcommands),
    # but kept as a defensive fallback for programmatic callers.
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
