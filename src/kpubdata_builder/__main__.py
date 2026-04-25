"""Command line entry point for kpubdata-builder."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .errors import SpecLoadError, ValidationError
from .spec import load_spec
from .validator import validate_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KPubData Builder CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a BuildSpec YAML file")
    validate_parser.add_argument("spec_path", type=Path, help="Path to a BuildSpec YAML file")

    build_command = subparsers.add_parser("build", help="Run a dataset build")
    build_command.add_argument("spec_path", type=Path, help="Path to a BuildSpec YAML file")
    build_command.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where build outputs will be written",
    )

    return parser


def run_validate(spec_path: Path) -> int:
    try:
        spec = load_spec(spec_path)
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"BuildSpec load failed: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("BuildSpec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print("BuildSpec is valid.")
    print(f"Dataset ID: {spec.dataset_id}")
    print(f"Title: {spec.title}")
    return 0


def run_build(spec_path: Path, output_dir: Path) -> int:
    print("Build command is not implemented yet.")
    print(f"Spec path: {spec_path}")
    print(f"Output directory: {output_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return run_validate(args.spec_path)
    if args.command == "build":
        return run_build(args.spec_path, args.output_dir)

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
