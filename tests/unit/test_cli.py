"""Tests for the kpubdata-builder command-line entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import __version__
from kpubdata_builder.cli import build_parser, main

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)

INVALID_SPEC_YAML_NO_SOURCES = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources: []
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


def test_build_parser_uses_program_name() -> None:
    parser = build_parser()
    assert parser.prog == "kpubdata-builder"


def test_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--help"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "kpubdata-builder" in captured.out
    assert "validate" in captured.out


def test_version_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--version"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert __version__ in captured.out


def test_no_subcommand_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "kpubdata-builder" in captured.err


def test_unknown_command_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["does-not-exist"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err


def test_validate_succeeds_for_valid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "dataset.sample" in captured.out
    assert captured.err == ""


def test_validate_fails_for_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.yaml"

    exit_code = main(["validate", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err


def test_validate_fails_for_invalid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(INVALID_SPEC_YAML_NO_SOURCES, encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "spec validation failed" in captured.err
    assert "at least one source is required" in captured.err


def test_preview_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["preview", "any.yaml"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "preview" in captured.err
    assert "not implemented" in captured.err


def test_preview_without_spec_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["preview"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "preview" in captured.err
    assert "not implemented" in captured.err


def test_build_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["build", "any.yaml"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "build" in captured.err
    assert "not implemented" in captured.err


def test_build_without_spec_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["build"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "build" in captured.err
    assert "not implemented" in captured.err


def test_validate_fails_for_malformed_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "bad.yaml"
    spec_path.write_text("{{{{not: valid: yaml: [", encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err
