"""ParquetExporter의 출력 규칙을 테스트로 고정한다.

Parquet은 바이너리 columnar 포맷이므로, polars로 다시 읽었을 때 레코드가
보존되는지·컬럼 타입이 유지되는지·빈 데이터 정책·반환 메타데이터를 회귀
테스트로 못 박는다.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, ParquetExporter
from kpubdata_builder.spec import ExportTarget


def test_records_round_trip_through_parquet(tmp_path: Path) -> None:
    # 레코드 2개를 기록한 뒤 read_parquet로 되읽어 원본과 일치하는지 검증한다.
    records = ({"id": "1", "amount": 1000}, {"id": "2", "amount": 2500})
    artifact = ArtifactDataset(records=records)
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.to_dicts() == [dict(record) for record in records]


def test_column_types_are_preserved(tmp_path: Path) -> None:
    # int 컬럼이 round-trip 후에도 정수 타입으로 유지되는지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1", "amount": 1000},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.schema["amount"] == pl.Int64
    assert frame.schema["id"] == pl.Utf8


def test_preserves_unicode(tmp_path: Path) -> None:
    # 한글 값이 round-trip 후에도 보존되는지 확인한다.
    artifact = ArtifactDataset(records=({"district": "강남구"},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    assert pl.read_parquet(result.output_path).to_dicts() == [{"district": "강남구"}]


def test_empty_records_with_schema_keeps_columns(tmp_path: Path) -> None:
    # 빈 데이터라도 schema가 있으면 0행이지만 컬럼 이름을 보존한다.
    artifact = ArtifactDataset(records=(), schema={"id": "str", "amount": "int"})
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.height == 0
    assert frame.columns == ["id", "amount"]


def test_empty_records_without_schema_writes_readable_empty_file(tmp_path: Path) -> None:
    # schema도 records도 없으면 0행·0열의 읽을 수 있는 Parquet 파일이 된다.
    artifact = ArtifactDataset(records=())
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.shape == (0, 0)


def test_returns_metadata_pointing_to_created_file(tmp_path: Path) -> None:
    # 반환된 Path가 실제 생성된 파일을 가리키고 메타데이터가 정확한지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1"},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.parquet"
    assert result.output_path.is_file()
    assert result.file_size == result.output_path.stat().st_size
    assert result.format == "parquet"


def test_registry_exposes_parquet_exporter() -> None:
    # Parquet exporter가 kind 문자열 "parquet"로 레지스트리에 등록되어 있는지 확인한다.
    assert isinstance(EXPORTER_REGISTRY["parquet"], ParquetExporter)


def test_wraps_write_failure_in_export_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Parquet 쓰기 실패가 ExportError로 래핑되는지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1"},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    def raise_os_error(self: pl.DataFrame, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise OSError("disk full")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", raise_os_error)

    with pytest.raises(ExportError):
        ParquetExporter().export(artifact, target, tmp_path)
