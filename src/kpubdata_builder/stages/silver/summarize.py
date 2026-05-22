"""Silver stage summarization: schema description and table statistics."""

from __future__ import annotations

import polars as pl

from .models import ColumnInfo, SchemaSummary, TableStatistics


def summarize_schema(table: pl.DataFrame) -> SchemaSummary:
    """Describe each column's dtype, nullability, and unique value count."""
    columns: list[ColumnInfo] = []
    for name in table.columns:
        series = table[name]
        columns.append(
            ColumnInfo(
                name=name,
                dtype=str(series.dtype),
                nullable=series.null_count() > 0,
                unique_count=series.n_unique(),
            )
        )
    return SchemaSummary(columns=tuple(columns))


def summarize_statistics(table: pl.DataFrame) -> TableStatistics:
    """Compute row count, per-column null counts, and duplicate row rate."""
    row_count = table.height
    null_counts = {name: int(table[name].null_count()) for name in table.columns}
    duplicate_rate = _duplicate_rate(table, row_count)
    return TableStatistics(
        row_count=row_count,
        null_counts=null_counts,
        duplicate_rate=duplicate_rate,
    )


def _duplicate_rate(table: pl.DataFrame, row_count: int) -> float:
    """Fraction of rows that are duplicates of an earlier row (0.0 when empty)."""
    if row_count == 0:
        return 0.0
    unique_rows = table.unique().height
    return (row_count - unique_rows) / row_count
