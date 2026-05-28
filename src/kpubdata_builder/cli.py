"""Command-line entrypoint for kpubdata-builder."""

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

    publish_cmd = subparsers.add_parser(
        "publish",
        help="Publish build artifacts to a destination (local or HuggingFace).",
    )
    publish_cmd.add_argument("source", help="Path to the build output directory to publish.")
    publish_cmd.add_argument(
        "--target",
        choices=["local", "huggingface"],
        default="local",
        help="Publish target (default: local).",
    )
    publish_cmd.add_argument(
        "--destination",
        default="./published",
        help="Destination path (for local) or HF repo ID (for huggingface).",
    )

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


def _run_publish(source: str, target: str, destination: str) -> int:
    source_path = Path(source)
    if not source_path.exists():
        print(f"error: source path does not exist: {source}", file=sys.stderr)
        return 1

    artifact_paths = tuple(source_path.iterdir()) if source_path.is_dir() else (source_path,)
    if not artifact_paths:
        print("error: no artifacts found to publish", file=sys.stderr)
        return 1

    from .publishers.base import BasePublisher

    publisher: BasePublisher
    if target == "local":
        from .publishers.local import LocalPublisher

        publisher = LocalPublisher(destination=Path(destination))
    elif target == "huggingface":
        from .publishers.huggingface import HuggingFacePublisher

        publisher = HuggingFacePublisher(repo_id=destination)
    else:
        print(f"error: unsupported publish target: {target}", file=sys.stderr)
        return 1

    try:
        publisher.publish(artifact_paths)
    except RuntimeError as exc:
        print(f"error: publish failed: {exc}", file=sys.stderr)
        return 1

    print(f"published to {target}: {destination}")
    return 0


def dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "validate":
        return _run_validate(args.spec)
    if command == "publish":
        return _run_publish(args.source, args.target, args.destination)
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
