"""HuggingFaceExporter(#9)의 레이아웃·카드·메타데이터 생성을 검증한다."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, HuggingFaceExporter
from kpubdata_builder.spec import ExportTarget


def _artifact() -> ArtifactDataset:
    return ArtifactDataset(
        records=({"id": "1", "name": "강남구"}, {"id": "2", "name": "서초구"}),
        schema={"id": "str", "name": "str"},
        metadata={
            "title": "아파트 실거래가",
            "description": "서울 아파트 실거래가",
            "license": "cc-by-4.0",
        },
        provenance=("datago.apt_trade",),
    )


def test_creates_hf_directory_layout(tmp_path: Path) -> None:
    # data/ + README.md + dataset_infos.json의 HF 표준 레이아웃을 생성한다.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    assert result.output_path == tmp_path / "hf/apt_trade"
    assert (result.output_path / "data").is_dir()
    assert (result.output_path / "README.md").is_file()
    assert (result.output_path / "dataset_infos.json").is_file()
    assert result.format == "huggingface"
    assert result.file_size > 0


def test_default_format_is_parquet_and_round_trips(tmp_path: Path) -> None:
    # 기본 형식은 parquet이며 데이터가 round-trip 보존되는지 확인한다.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    data_file = result.output_path / "data" / "train-00000-of-00001.parquet"
    assert data_file.is_file()
    assert pl.read_parquet(data_file).to_dicts() == [
        {"id": "1", "name": "강남구"},
        {"id": "2", "name": "서초구"},
    ]


def test_jsonl_format_option(tmp_path: Path) -> None:
    # format=jsonl 옵션이면 jsonl shard를 한 줄당 한 레코드로 기록한다.
    target = ExportTarget(
        kind="huggingface", output_path="hf/apt_trade", options={"format": "jsonl"}
    )

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    data_file = result.output_path / "data" / "train-00000-of-00001.jsonl"
    lines = data_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"id": "1", "name": "강남구"}


def test_readme_has_yaml_front_matter_and_unicode(tmp_path: Path) -> None:
    # README.md가 YAML front matter로 시작하고 한글이 보존되는지 확인한다.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    text = (result.output_path / "README.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert text.count("---") >= 2  # front matter 시작/끝
    assert "license: cc-by-4.0" in text
    assert "# 아파트 실거래가" in text
    assert "- datago.apt_trade" in text


def test_dataset_infos_is_valid_json_with_features(tmp_path: Path) -> None:
    # dataset_infos.json이 유효 JSON이며 features/num_examples를 담는지 확인한다.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    infos = json.loads((result.output_path / "dataset_infos.json").read_text(encoding="utf-8"))
    assert infos["features"] == {"id": "str", "name": "str"}
    assert infos["num_examples"] == 2
    assert infos["provenance"] == ["datago.apt_trade"]


def test_rejects_unsupported_format(tmp_path: Path) -> None:
    # 지원하지 않는 format은 ExportError로 거부한다.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade", options={"format": "xml"})

    with pytest.raises(ExportError, match="format"):
        HuggingFaceExporter().export(_artifact(), target, tmp_path)


def test_registry_exposes_huggingface_exporter() -> None:
    # HF exporter가 kind "huggingface"로 레지스트리에 등록되어 있는지 확인한다.
    assert isinstance(EXPORTER_REGISTRY["huggingface"], HuggingFaceExporter)
