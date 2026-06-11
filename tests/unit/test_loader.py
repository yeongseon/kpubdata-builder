"""BuildSpec YAML 로드 경로의 성공과 실패 시나리오를 검증한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import SpecLoadError
from kpubdata_builder.spec import load_spec


def test_load_spec_reads_valid_yaml(tmp_path: Path) -> None:
    # 정상 YAML이 BuildSpec 객체로 파싱되는지 확인한다.
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
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
        + "\n",
        encoding="utf-8",
    )

    spec = load_spec(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.sources[0].provider == "datago"
    assert spec.exports[0].output_path == "out/data.jsonl"


def test_load_spec_raises_for_invalid_yaml(tmp_path: Path) -> None:
    # 잘못된 YAML 문법은 SpecLoadError로 감싸지는지 검증한다.
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("dataset_id: [unterminated\n", encoding="utf-8")

    with pytest.raises(SpecLoadError):
        load_spec(spec_path)


def test_load_spec_raises_for_missing_file(tmp_path: Path) -> None:
    # 존재하지 않는 파일 경로도 SpecLoadError로 처리되는지 확인한다.
    with pytest.raises(SpecLoadError):
        load_spec(tmp_path / "missing.yaml")


def test_load_spec_rejects_circular_yaml_without_crash(tmp_path: Path) -> None:
    # YAML anchor/alias로 만든 순환 구조는 RecursionError crash가 아니라
    # SpecLoadError로 처리되어야 한다 (#169).
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
    params: &p
      self: *p
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecLoadError, match="circular reference"):
        load_spec(spec_path)
