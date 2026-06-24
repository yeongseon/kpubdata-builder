"""CsvExporter의 출력 규칙을 테스트로 고정한다.

CSV는 콤마/따옴표/개행이 포함된 값을 올바르게 인용해야 하고, 컬럼 순서가
결정적이어야 한다. 헤더 구성·셀 포매팅·빈 데이터 정책·반환 메타데이터를
회귀 테스트로 못 박는다.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, CsvExporter
from kpubdata_builder.spec import ExportTarget


def _read_rows(path: Path) -> list[list[str]]:
    # 기록된 CSV를 csv.reader로 되읽어 인용/이스케이프를 검증한다.
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def test_writes_header_and_one_row_per_record(tmp_path: Path) -> None:
    # 레코드 2개면 헤더 1줄 + 데이터 2줄이 되어야 한다.
    artifact = ArtifactDataset(records=({"id": "1", "name": "a"}, {"id": "2", "name": "b"}))
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["id", "name"], ["1", "a"], ["2", "b"]]


def test_column_order_follows_schema_when_present(tmp_path: Path) -> None:
    # schema가 있으면 헤더 순서는 schema 키 순서를 따른다.
    artifact = ArtifactDataset(
        records=({"b": "2", "a": "1"},),
        schema={"a": "str", "b": "str"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["a", "b"], ["1", "2"]]


def test_column_order_is_first_seen_when_no_schema(tmp_path: Path) -> None:
    # schema가 없으면 레코드에서 처음 등장한 순서를 따르며, 누락 키는 빈 셀로 채운다.
    artifact = ArtifactDataset(records=({"id": "1"}, {"id": "2", "extra": "x"}))
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["id", "extra"], ["1", ""], ["2", "x"]]


def test_quotes_values_with_comma_quote_and_newline(tmp_path: Path) -> None:
    # 콤마/따옴표/개행이 포함된 값이 인용되어 round-trip 되는지 검증한다.
    artifact = ArtifactDataset(
        records=({"v": 'a,b "c" \n d'},),
        schema={"v": "str"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["v"], ['a,b "c" \n d']]


def test_formats_special_cell_values(tmp_path: Path) -> None:
    # None은 빈 셀, bool은 소문자, 중첩 list/dict는 결정적 JSON 문자열로 직렬화한다.
    artifact = ArtifactDataset(
        records=({"nullable": None, "flag": True, "nested": {"b": 2, "a": 1}, "items": [1, 2]},),
        schema={"nullable": "str", "flag": "bool", "nested": "json", "items": "json"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    assert rows[0] == ["nullable", "flag", "nested", "items"]
    assert rows[1] == ["", "true", '{"a": 1, "b": 2}', "[1, 2]"]


def test_preserves_unicode(tmp_path: Path) -> None:
    # 한글이 깨지지 않고 그대로 보존되는지 확인한다.
    artifact = ArtifactDataset(records=({"district": "강남구"},), schema={"district": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_text(encoding="utf-8")
    assert "강남구" in raw


def test_empty_records_without_schema_writes_empty_file(tmp_path: Path) -> None:
    # schema도 records도 없으면 빈 파일(크기 0)로 기록한다.
    artifact = ArtifactDataset(records=())
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == ""
    assert result.file_size == 0


def test_schema_without_records_writes_header_only(tmp_path: Path) -> None:
    # schema는 있고 records가 없으면 헤더만 기록한다.
    artifact = ArtifactDataset(records=(), schema={"id": "str", "name": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == "id,name\n"


def test_returns_metadata_pointing_to_created_file(tmp_path: Path) -> None:
    # 반환된 Path가 실제 생성된 파일을 가리키고 메타데이터가 정확한지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1"},), schema={"id": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.csv"
    assert result.output_path.is_file()
    assert result.file_size == result.output_path.stat().st_size
    assert result.format == "csv"


def test_registry_exposes_csv_exporter() -> None:
    # CSV exporter가 kind 문자열 "csv"로 레지스트리에 등록되어 있는지 확인한다.
    assert isinstance(EXPORTER_REGISTRY["csv"], CsvExporter)


def test_wraps_io_failure_in_export_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 파일 쓰기 실패가 ExportError로 래핑되는지 확인한다.
    import os

    artifact = ArtifactDataset(records=({"id": "1"},), schema={"id": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    def raise_on_replace(src: str, dst: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", raise_on_replace)

    with pytest.raises(ExportError):
        CsvExporter().export(artifact, target, tmp_path)
