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
        # ValidationProblem 객체로 바뀔었으므로 message 필드를 확인 (#261)
        assert any("amount" in problem.message for problem in dataset.validation.problems)

    def test_validation_passes_when_dtype_matches(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        dataset = build_silver_dataset(
            bronze, casts={"amount": "int"}, column_dtypes={"amount": "int"}
        )

        assert dataset.validation.ok is True
        assert dataset.validation.problems == ()

    def test_validation_fails_when_dtype_mismatches(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        # amount는 바로 읽으면 Int64; Float64를 요구하면 실패해야 한다
        dataset = build_silver_dataset(bronze, column_dtypes={"amount": "float"})

        assert dataset.validation.ok is False
        assert any("amount" in p.message for p in dataset.validation.problems)

    def test_validation_reports_missing_column_for_dtype_spec(self) -> None:
        bronze = _bronze(({"id": "1"},))

        # 'amount' 코럼이 없으면 dtype 검증 실패 메시지를 포함해야 한다
        dataset = build_silver_dataset(bronze, column_dtypes={"amount": "int"})

        assert dataset.validation.ok is False
        assert any("amount" in p.message for p in dataset.validation.problems)

    def test_preview_respects_limit(self) -> None:
        records = tuple({"n": i} for i in range(10))
        dataset = build_silver_dataset(_bronze(records), preview_limit=3)

        assert dataset.preview.total_rows == 10
        assert len(dataset.preview.rows) == 3

    def test_optional_casts_apply_declared_dtypes(self) -> None:
        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "2500"}))

        dataset = build_silver_dataset(bronze, casts={"amount": "int"})

        assert dataset.table.schema["amount"] == pl.Int64

    def test_cast_data_loss_raises_instead_of_silently_nulling(self) -> None:
        # 선언된 캐스팅이 값을 null로 떨어뜨리면 조용히 묻지 않고 TabularError로 실패 (#188).
        from kpubdata_builder.errors import TabularError

        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "oops"}))

        with pytest.raises(TabularError, match="data loss"):
            _ = build_silver_dataset(bronze, casts={"amount": "int"})

    def test_rejects_negative_preview_limit(self) -> None:
        # 음수 preview_limit은 df.head(-1)로 새지 않도록 일찍 거부한다 (#190).
        bronze = _bronze(({"id": "1"},))

        with pytest.raises(ValueError, match="preview_limit"):
            _ = build_silver_dataset(bronze, preview_limit=-1)


class TestRowPreservingInvariant:
    """Bronze→Silver가 행을 filter/dedup/reorder하지 않는다는 불변조건을 고정한다 (#497).

    pipeline.preview의 Source↔Silver diff는 ``bronze.raw_records[i]``와
    ``silver.table``의 i번째 행이 항상 같은 논리적 행이라는 이 불변조건에
    의존한다(diff_available 판정의 실제 근거). normalize_table()이
    records_to_dataframe() → cast_columns()만 호출하고 validate_table()은
    테이블을 아예 건드리지 않으므로 오늘은 이 불변조건이 성립하지만, 향후 누군가
    Silver에 dedup/filter/reorder를 추가하면 이 테스트가 깨져서 pipeline/preview.py
    의 alignment 가정을 재검토하라는 신호를 준다.
    """

    def test_row_count_is_preserved(self) -> None:
        records = tuple({"id": str(i), "amount": i * 100} for i in range(50))
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze, casts={"amount": "float"})

        assert dataset.table.height == len(records)
        assert dataset.statistics.row_count == len(records)

    def test_row_order_is_preserved_across_normalize_and_validate(self) -> None:
        # id를 원본 순서의 지문으로 써서, 캐스팅/검증을 거쳐도 순서가 바뀌지
        # 않는지 확인한다 — 값이 바뀌어도(#497 diff의 목적) 행의 *위치*는 원본과
        # 1:1로 대응해야 한다.
        records = tuple({"id": str(i), "amount": str(i * 100)} for i in range(20))
        bronze = _bronze(records)

        dataset = build_silver_dataset(
            bronze,
            casts={"amount": "int"},
            required_columns=("id", "amount"),
            column_dtypes={"amount": "int"},
        )

        assert dataset.table["id"].to_list() == [r["id"] for r in records]
        assert dataset.validation.ok is True

    def test_row_order_is_preserved_without_declared_casts(self) -> None:
        # casts가 전혀 없어도(가장 흔한 preview 경로) 순서 보존은 동일하게 성립한다.
        records = tuple({"id": str(i), "label": chr(ord("a") + i)} for i in range(10))
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze)

        assert dataset.table["id"].to_list() == [r["id"] for r in records]


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
        # timezone-aware datetime 컬럼은 UTC로 정규화된 뒤 +00:00 offset을 포함한 ISO
        # 문자열로 직렬화된다(서로 다른 입력 tz가 동일 UTC 시각으로 수렴). cast map은
        # naive Datetime만 만들므로 aware 테이블을 직접 구성한다 (#97 datetime regression).
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
        # 두 입력이 UTC로 정규화되어 +00:00 offset을 포함한다(KST 17:00 == UTC 08:00).
        assert rows[0]["ts"] == "2025-01-01T12:30:00+00:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00+00:00"

    def test_serializes_datetime_with_microseconds(self, tmp_path: Path) -> None:
        # microseconds를 가진 datetime이 잘리지 않고 ISO 소수 초까지 직렬화되는지 검증한다.
        bronze = _bronze(({"ts": "2025-01-01T12:30:00.123456"},))
        dataset = build_silver_dataset(bronze, casts={"ts": "datetime"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["ts"] == "2025-01-01T12:30:00.123456"
