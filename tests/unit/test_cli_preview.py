"""CLI `preview` 명령(#3): 스키마+샘플 출력, 아티팩트 파일 미생성 검증."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from kpubdata_builder import cli
from kpubdata_builder.spec import JsonValue

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


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _write_spec(tmp_path: Path) -> Path:
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")
    return spec_path


def test_preview_prints_schema_and_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_spec(tmp_path)
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    monkeypatch.setattr(cli, "_create_client", lambda: client)

    exit_code = cli.main(["preview", str(spec_path), "--limit", "1"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "datago.air_quality" in captured.out
    assert "id" in captured.out and "v" in captured.out
    # 미리보기는 어떤 아티팩트 파일도 만들지 않는다 (spec.yaml만 존재)
    assert list(tmp_path.iterdir()) == [spec_path]


def test_preview_requires_spec_argument(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["preview"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err


def test_preview_reports_spec_load_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main(["preview", str(tmp_path / "missing.yaml")])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err
