"""Silver 단계(#46): tabularize → validate → summarize → preview → persist 검증."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.silver import (
    SilverDataset,
    ValidationResult,
    build_silver_dataset,
    persist_silver_dataset,
)
from kpubdata_builder.tabular import (
    PreviewSlice,
    SchemaInfo,
    TableStatistics,
    compute_statistics,
    generate_preview,
    infer_schema,
)


def _bronze(
    records: tuple[Mapping[str, JsonValue], ...], *, source_key: str = "datago.apt_trade"
) -> BronzeArtifact:
    normalized_records = tuple(dict(record) for record in records)
    return BronzeArtifact(
        source_key=source_key,
        raw_records=normalized_records,
        fetched_at=utc_now(),
    )


class TestBuildSilverDataset:
    def test_produces_dataset_with_table_schema_stats_preview(self) -> None:
        bronze = _bronze(
            (
                {"id": "1", "amount": 1000, "district": "강남구"},
                {"id": "2", "amount": 2500, "district": "서초구"},
            )
        )

        dataset = build_silver_dataset(bronze)

        assert isinstance(dataset, SilverDataset)
        assert isinstance(dataset.table, pl.DataFrame)
        assert dataset.table.shape == (2, 3)
        assert isinstance(dataset.schema, SchemaInfo)
        assert [c.name for c in dataset.schema.columns] == ["id", "amount", "district"]
        assert isinstance(dataset.statistics, TableStatistics)
        assert dataset.statistics.row_count == 2
        assert isinstance(dataset.preview, PreviewSlice)
        assert dataset.source_bronze == "datago.apt_trade"

    def test_validation_passes_when_required_columns_present(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        dataset = build_silver_dataset(bronze, required_columns=("id", "amount"))

        assert isinstance(dataset.validation, ValidationResult)
        assert dataset.validation.ok is True
        assert dataset.validation.problems == ()

    def test_validation_fails_when_required_column_missing(self) -> None:
        bronze = _bronze(({"id": "1"},))

        dataset = build_silver_dataset(bronze, required_columns=("id", "amount"))

        assert dataset.validation.ok is False
        assert any("amount" in problem for problem in dataset.validation.problems)

    def test_preview_respects_limit(self) -> None:
        records = tuple({"n": i} for i in range(10))
        dataset = build_silver_dataset(_bronze(records), preview_limit=3)

        assert dataset.preview.total_rows == 10
        assert len(dataset.preview.rows) == 3

    def test_optional_casts_apply_declared_dtypes(self) -> None:
        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "2500"}))

        dataset = build_silver_dataset(bronze, casts={"amount": "int"})

        assert dataset.table.schema["amount"] == pl.Int64


class TestPersistSilverDataset:
    def test_writes_parquet_and_json_sidecars(self, tmp_path: Path) -> None:
        bronze = _bronze(
            (
                {"id": "1", "amount": 1000},
                {"id": "2", "amount": 2500},
            )
        )
        dataset = build_silver_dataset(bronze, required_columns=("id",))

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        assert result.table_path.exists()
        assert result.schema_path.exists()
        assert result.stats_path.exists()
        assert result.preview_path.exists()
        assert result.validation_path.exists()

        # parquet round-trip
        assert pl.read_parquet(result.table_path).to_dicts() == dataset.table.to_dicts()

        # json sidecars are well-formed and reflect the dataset
        stats = cast(
            dict[str, JsonValue], json.loads(result.stats_path.read_text(encoding="utf-8"))
        )
        assert stats["row_count"] == 2
        validation = cast(
            dict[str, JsonValue], json.loads(result.validation_path.read_text(encoding="utf-8"))
        )
        assert validation["ok"] is True

    def test_rejects_unsafe_run_id(self, tmp_path: Path) -> None:
        dataset = build_silver_dataset(_bronze(({"id": "1"},)))

        with pytest.raises(ValueError, match="run_id"):
            _ = persist_silver_dataset(dataset, output_root=tmp_path, run_id="../escape")

    def test_serializes_date_values_as_iso_strings(self, tmp_path: Path) -> None:
        # Date/Datetime으로 캐스팅된 컬럼이 preview에 들어가도 persist가 깨지지 않고
        # ISO 문자열로 직렬화되는지 검증한다 (#93 review).
        bronze = _bronze(({"d": "2025-01-01"}, {"d": "2025-01-02"}))
        dataset = build_silver_dataset(bronze, casts={"d": "date"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["d"] == "2025-01-01"

    def test_serializes_naive_datetime_values_as_iso_strings(self, tmp_path: Path) -> None:
        # naive datetime으로 캐스팅된 컬럼이 preview에서 offset 없는 ISO 문자열로
        # 직렬화되는지 검증한다 (#97 datetime regression).
        bronze = _bronze(({"ts": "2025-01-01T12:30:00"}, {"ts": "2025-01-02T08:00:00"}))
        dataset = build_silver_dataset(bronze, casts={"ts": "datetime"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["ts"] == "2025-01-01T12:30:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00"

    def test_serializes_timezone_aware_datetime_values_as_iso_strings(self, tmp_path: Path) -> None:
        # timezone-aware datetime 컬럼이 UTC offset을 보존한 ISO 문자열로 직렬화되는지
        # 검증한다. cast map은 naive Datetime만 만들므로 aware 테이블을 직접 구성한다
        # (#97 datetime regression).
        kst = timezone(timedelta(hours=9))
        table = pl.DataFrame(
            {
                "ts": [
                    datetime(2025, 1, 1, 12, 30, tzinfo=timezone.utc),
                    datetime(2025, 1, 2, 17, 0, tzinfo=kst),
                ]
            }
        )
        assert table.schema["ts"].time_zone is not None
        dataset = SilverDataset(
            table=table,
            schema=infer_schema(table),
            statistics=compute_statistics(table),
            preview=generate_preview(table),
            validation=ValidationResult(ok=True),
            source_bronze="datago.apt_trade",
        )

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        # 두 입력 모두 UTC로 정규화되어 동일 시각 + offset을 보존한다(KST 17:00 == UTC 08:00).
        assert rows[0]["ts"] == "2025-01-01T12:30:00+00:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00+00:00"
