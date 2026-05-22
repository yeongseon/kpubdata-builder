"""CLI 진입점의 파서, 종료 코드, 오류 메시지를 검증한다."""

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
    # parser가 기대한 프로그램 이름을 노출하는지 확인한다.
    parser = build_parser()
    assert parser.prog == "kpubdata-builder"


def test_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # --help 호출이 성공 종료 코드와 도움말 본문을 반환하는지 검증한다.
    exit_code = main(["--help"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "kpubdata-builder" in captured.out
    assert "validate" in captured.out


def test_version_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # --version 호출이 버전 문자열을 출력하는지 확인한다.
    exit_code = main(["--version"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert __version__ in captured.out


def test_no_subcommand_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    # 하위 명령이 없을 때 argparse 스타일 오류 코드 2를 반환하는지 검증한다.
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "kpubdata-builder" in captured.err


def test_unknown_command_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    # 알 수 없는 명령이 stderr와 함께 거부되는지 확인한다.
    exit_code = main(["does-not-exist"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err


def test_validate_succeeds_for_valid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 유효한 YAML 명세는 validate 명령에서 성공해야 한다.
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
    # 파일이 없으면 로드 실패 메시지와 종료 코드 1을 반환해야 한다.
    missing = tmp_path / "missing.yaml"

    exit_code = main(["validate", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err


def test_validate_fails_for_invalid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # YAML 문법은 맞아도 필수 필드 검증에 실패하면 오류가 출력되는지 확인한다.
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(INVALID_SPEC_YAML_NO_SOURCES, encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err
    assert "sources" in captured.err


def test_preview_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    # 예약 명령 preview가 미구현 오류로 차단되는지 검증한다.
    exit_code = main(["preview", "any.yaml"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "preview" in captured.err
    assert "not implemented" in captured.err


def test_preview_without_spec_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    # preview 인자 생략 시에도 예약 명령 오류 경로를 유지하는지 확인한다.
    exit_code = main(["preview"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "preview" in captured.err
    assert "not implemented" in captured.err


def test_build_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    # 예약 명령 build가 미구현 오류를 반환하는지 검증한다.
    exit_code = main(["build", "any.yaml"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "build" in captured.err
    assert "not implemented" in captured.err


def test_build_without_spec_is_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    # build 인자가 없어도 예약 명령 오류 메시지를 유지해야 한다.
    exit_code = main(["build"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "build" in captured.err
    assert "not implemented" in captured.err


def test_validate_fails_for_malformed_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # YAML 문법 자체가 깨진 경우 로드 실패로 처리되는지 확인한다.
    spec_path = tmp_path / "bad.yaml"
    spec_path.write_text("{{{{not: valid: yaml: [", encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err
