from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact
from kpubdata_builder.stages.silver import (
    build_silver_dataset,
    persist_silver_dataset,
)
from kpubdata_builder.stages.silver.models import TableStatistics
from kpubdata_builder.stages.silver.normalize import normalize_table
from kpubdata_builder.stages.silver.preview import build_preview
from kpubdata_builder.stages.silver.summarize import (
    summarize_schema,
    summarize_statistics,
)
from kpubdata_builder.stages.silver.validate import validate_table


def _bronze(records: tuple[dict[str, JsonValue], ...]) -> BronzeArtifact:
    return BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=records,
        fetched_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )


def test_summarize_schema_reports_dtype_nullable_and_unique() -> None:
    df = pl.DataFrame({"name": ["강남구", "서초구", "서초구"], "amount": [100, None, 200]})
    schema = summarize_schema(df)
    by_name = {c.name: c for c in schema.columns}
    assert by_name["name"].unique_count == 2
    assert by_name["name"].has_nulls is False
    assert by_name["amount"].has_nulls is True


def test_summarize_statistics_counts_rows_nulls_and_duplicates() -> None:
    df = pl.DataFrame({"id": ["1", "2", "2"], "amount": [10, 20, 20]})
    stats = summarize_statistics(df)
    assert stats.row_count == 3
    assert stats.null_counts == {"id": 0, "amount": 0}
    assert stats.duplicate_rate == pytest.approx(1 / 3)


def test_table_statistics_defends_against_external_mutation() -> None:
    source = {"id": 0}
    stats = TableStatistics(row_count=1, null_counts=source)
    source["id"] = 999  # mutating the original must not affect the frozen value
    assert stats.null_counts == {"id": 0}


def test_build_preview_limits_rows_but_keeps_total() -> None:
    df = pl.DataFrame({"a": [1, 2, 3, 4, 5]})
    preview = build_preview(df, limit=2)
    assert len(preview.rows) == 2
    assert preview.total_rows == 5


def test_validate_table_warns_on_empty_without_error() -> None:
    result = validate_table(pl.DataFrame({"a": []}))
    assert result.ok is True
    assert "table has no rows" in result.warnings


def test_normalize_table_passthrough_without_transforms() -> None:
    df = pl.DataFrame({"a": [1, 2]})
    assert normalize_table(df) is df


def test_normalize_table_rejects_unsupported_transforms() -> None:
    df = pl.DataFrame({"a": [1, 2]})
    with pytest.raises(ValueError, match="Unsupported transforms"):
        normalize_table(df, ["filter:x"])


def test_build_silver_dataset_orchestrates_all_parts() -> None:
    artifact = _bronze(
        (
            {"id": "1", "name": "강남구", "amount": 100},
            {"id": "2", "name": "서초구", "amount": 200},
        )
    )
    silver = build_silver_dataset(artifact)
    assert silver.source_bronze == "datago.apt_trade"
    assert silver.statistics.row_count == 2
    assert {c.name for c in silver.schema_summary.columns} == {"id", "name", "amount"}
    assert silver.preview.total_rows == 2
    assert silver.validation_result.ok is True


def test_persist_silver_dataset_writes_parquet_and_json(tmp_path: Path) -> None:
    artifact = _bronze(({"id": "1", "name": "강남구"}, {"id": "2", "name": "서초구"}))
    silver = build_silver_dataset(artifact)
    result = persist_silver_dataset(silver, output_root=tmp_path / "build", run_id="run-1")

    assert "run-1" in str(result.silver_dir)
    assert "silver" in str(result.silver_dir)
    assert result.table_path.exists()
    assert result.schema_path.exists()
    assert result.stats_path.exists()
    assert result.preview_path.exists()

    reloaded = pl.read_parquet(result.table_path)
    assert reloaded.height == 2
    assert reloaded["name"].to_list() == ["강남구", "서초구"]

    stats = json.loads(result.stats_path.read_text(encoding="utf-8"))
    assert stats["row_count"] == 2


def test_persist_silver_dataset_rejects_unsafe_run_id(tmp_path: Path) -> None:
    silver = build_silver_dataset(_bronze(({"id": "1"},)))
    with pytest.raises(ValueError, match="unsafe characters"):
        persist_silver_dataset(silver, output_root=tmp_path, run_id="../escape")
    with pytest.raises(ValueError, match="must not be empty"):
        persist_silver_dataset(silver, output_root=tmp_path, run_id="")


def test_persist_silver_dataset_rejects_source_bronze_with_slash(tmp_path: Path) -> None:
    artifact = BronzeArtifact(
        source_key="a/b",
        raw_records=({"id": "1"},),
        fetched_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    silver = build_silver_dataset(artifact)
    with pytest.raises(ValueError, match="unsafe characters"):
        persist_silver_dataset(silver, output_root=tmp_path, run_id="run-1")
