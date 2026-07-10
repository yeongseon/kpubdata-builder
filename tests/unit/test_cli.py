"""CLI 진입점의 파서, 종료 코드, 오류 메시지를 검증한다."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kpubdata_builder import __version__
from kpubdata_builder.cli import build_parser, main
from kpubdata_builder.publishers.base import PublishResult

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
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

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
    _ = spec_path.write_text(INVALID_SPEC_YAML_NO_SOURCES, encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err
    assert "sources" in captured.err


def test_validate_fails_for_malformed_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # YAML 문법 자체가 깨진 경우 로드 실패로 처리되는지 확인한다.
    spec_path = tmp_path / "bad.yaml"
    _ = spec_path.write_text("{{{{not: valid: yaml: [", encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err


# ---------------------------------------------------------------------------
# publish 명령 테스트
# ---------------------------------------------------------------------------


def test_publish_local_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --target local end-to-end: 파일이 destination 으로 복사되고 요약이 출력된다.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "a.parquet").write_bytes(b"parquet1")
    (artifacts_dir / "b.parquet").write_bytes(b"parquet2")

    dest_dir = tmp_path / "dest"

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "local",
            "--destination",
            str(dest_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (dest_dir / "a.parquet").exists()
    assert (dest_dir / "b.parquet").exists()
    assert "publish: dataset.sample -> local" in captured.out
    assert "artifacts: 2" in captured.out
    assert captured.err == ""


def test_publish_missing_artifacts_dir_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # artifacts-dir 가 존재하지 않으면 exit 1 과 오류 메시지를 반환해야 한다.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    missing_dir = tmp_path / "no-such-dir"

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(missing_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no artifacts found" in captured.err


def test_publish_empty_artifacts_dir_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # artifacts-dir 가 비어 있으면 exit 1 과 오류 메시지를 반환해야 한다.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "empty"
    artifacts_dir.mkdir()

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no artifacts found" in captured.err


def test_publish_unknown_target_rejected_by_argparse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --target zzz は argparse が拒否して exit code 2 を返す.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "f.txt").write_text("x", encoding="utf-8")

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "zzz",
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )

    assert exit_code == 2


def test_publish_huggingface_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # HuggingFace publisher 를 stub 으로 교체하여 publish 가 올바른 인자로
    # 호출되고 exit 0 을 반환하는지 검증한다.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifact_file = artifacts_dir / "data.parquet"
    artifact_file.write_bytes(b"data")

    fake_result = PublishResult(
        publisher="huggingface",
        reference="https://huggingface.co/datasets/org/dataset",
        artifact_count=1,
    )
    stub = MagicMock()
    # 실제 HuggingFacePublisher는 파일 단위 입력을 받으므로 stub도 동일하게 맞춘다.
    stub.expects_directory = False
    stub.publish.return_value = fake_result

    import kpubdata_builder.cli as cli_module

    monkeypatch.setitem(cli_module.PUBLISHER_REGISTRY, "huggingface", stub)

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "huggingface",
            "--destination",
            "org/dataset",
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    stub.publish.assert_called_once_with(
        (artifact_file,),
        destination="org/dataset",
    )
    assert "publish: dataset.sample -> huggingface" in captured.out
    assert "artifacts: 1" in captured.out


def test_publish_publish_error_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # publisher が PublishError を送出した場合は exit 1 と stderr エラーを返す.
    # LocalPublisher の basename 衝突パスを使ってトリガーする.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    sub_a = artifacts_dir / "a"
    sub_b = artifacts_dir / "b"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    # 서로 다른 하위 디렉터리에 같은 basename 파일 → LocalPublisher가 PublishError 발생
    (sub_a / "data.parquet").write_bytes(b"1")
    (sub_b / "data.parquet").write_bytes(b"2")

    dest_dir = tmp_path / "dest"

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "local",
            "--destination",
            str(dest_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "publish failed" in captured.err


def test_publish_kaggle_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --target kaggle end-to-end: CLI가 dataset-metadata.json이 든 디렉터리 자체를
    # KagglePublisher에 전달하고, fake API로 업로드가 호출되는지 검증한다 (#176, #181).
    import json
    import sys
    import types

    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "data.csv").write_text("id\n1\n", encoding="utf-8")
    (artifacts_dir / "dataset-metadata.json").write_text(
        json.dumps({"id": "kpub/sample", "title": "Sample", "resources": []}),
        encoding="utf-8",
    )

    calls: list[str] = []

    class _FakeApi:
        def authenticate(self) -> None:
            calls.append("authenticate")

        def dataset_list(self, *, mine: bool, search: str) -> list[str]:
            del mine, search
            return []

        def dataset_create_new(self, *args: object, **kwargs: object) -> None:
            del args
            calls.append(f"create_new:public={kwargs.get('public')}")

        def dataset_create_version(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("create_version")

    extended = types.ModuleType("kaggle.api.kaggle_api_extended")
    extended.KaggleApi = lambda: _FakeApi()  # type: ignore[attr-defined]
    api_pkg = types.ModuleType("kaggle.api")
    kaggle_pkg = types.ModuleType("kaggle")
    monkeypatch.setitem(sys.modules, "kaggle", kaggle_pkg)
    monkeypatch.setitem(sys.modules, "kaggle.api", api_pkg)
    monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", extended)

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "kaggle",
            "--destination",
            "kpub/sample",
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "authenticate" in calls
    # public 플래그를 주지 않았으므로 비공개로 생성되어야 한다.
    assert "create_new:public=False" in calls
    assert "publish: dataset.sample -> kaggle" in captured.out
    assert "artifacts: 1" in captured.out


def test_serve_invokes_http_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # serve 명령이 http.serve를 올바른 host/port로 호출해야 한다 (#249).
    import kpubdata_builder.service.http as http_module

    captured_kwargs: dict[str, object] = {}

    def fake_serve(service: object, *, host: str, port: int) -> None:
        captured_kwargs["host"] = host
        captured_kwargs["port"] = port

    monkeypatch.setattr(http_module, "serve", fake_serve)

    exit_code = main(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
            "--output-dir",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert captured_kwargs == {"host": "0.0.0.0", "port": 9123}
    assert "serving kpubdata-builder" in out
