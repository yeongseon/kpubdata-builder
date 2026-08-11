"""드리프트 감지 단위 테스트 (#445, DRIFT-1)."""

from __future__ import annotations

from kpubdata_builder.stages.silver.drift import detect_drift
from kpubdata_builder.tabular import SchemaInfo, TableStatistics
from kpubdata_builder.tabular.types import ColumnInfo

_SCHEMA_A = SchemaInfo(
    columns=(
        ColumnInfo(name="a", dtype="Int64", nullable=False, unique_count=3),
        ColumnInfo(name="b", dtype="Utf8", nullable=True, unique_count=2),
    )
)
_STATS_A = TableStatistics(row_count=100, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)


class TestDetectDriftSchema:
    def test_column_added(self) -> None:
        schema_b = SchemaInfo(
            columns=_SCHEMA_A.columns
            + (ColumnInfo(name="c", dtype="Float64", nullable=True, unique_count=1),)
        )
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "column_added" and f.column == "c" for f in findings)

    def test_column_removed(self) -> None:
        schema_b = SchemaInfo(columns=(_SCHEMA_A.columns[0],))
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "column_removed" and f.column == "b" for f in findings)

    def test_dtype_changed(self) -> None:
        schema_b = SchemaInfo(
            columns=(
                ColumnInfo(name="a", dtype="Float64", nullable=False, unique_count=3),
                ColumnInfo(name="b", dtype="Utf8", nullable=True, unique_count=2),
            )
        )
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "dtype_changed" and f.column == "a" for f in findings)

    def test_no_schema_drift(self) -> None:
        findings = detect_drift(_SCHEMA_A, _STATS_A, _SCHEMA_A, _STATS_A)
        assert findings == []


class TestDetectDriftStats:
    def test_row_count_jump(self) -> None:
        stats_b = TableStatistics(row_count=500, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)
        findings = detect_drift(_SCHEMA_A, stats_b, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "row_count_jump" for f in findings)

    def test_small_row_count_change_no_drift(self) -> None:
        stats_b = TableStatistics(row_count=110, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)
        findings = detect_drift(_SCHEMA_A, stats_b, _SCHEMA_A, _STATS_A)
        assert findings == []

    def test_previous_zero_rows_no_jump(self) -> None:
        stats_prev = TableStatistics(row_count=0, null_counts={}, duplicate_rate=0.0)
        stats_curr = TableStatistics(row_count=100, null_counts={"a": 0}, duplicate_rate=0.0)
        findings = detect_drift(_SCHEMA_A, stats_curr, _SCHEMA_A, stats_prev)
        assert not any(f.kind == "row_count_jump" for f in findings)
