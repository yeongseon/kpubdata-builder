"""Silver stage models: tabularized dataset with schema, stats, and preview."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ...spec import JsonValue


@dataclass(frozen=True)
class ColumnInfo:
    """Schema information for a single column."""

    name: str
    dtype: str
    has_nulls: bool
    unique_count: int


@dataclass(frozen=True)
class SchemaSummary:
    """Column-level schema description of a Silver table."""

    columns: tuple[ColumnInfo, ...] = ()


@dataclass(frozen=True)
class TableStatistics:
    """Aggregate statistics for a Silver table."""

    row_count: int
    null_counts: dict[str, int] = field(default_factory=dict)
    duplicate_rate: float = 0.0


@dataclass(frozen=True)
class PreviewSlice:
    """A small sample of rows for previewing a Silver table."""

    rows: tuple[dict[str, JsonValue], ...] = ()
    total_rows: int = 0


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a Silver table against expectations."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when validation produced no errors."""
        return not self.errors


@dataclass(frozen=True)
class SilverDataset:
    """Tabularized, validated dataset produced by the Silver stage."""

    table: pl.DataFrame
    schema_summary: SchemaSummary
    statistics: TableStatistics
    preview: PreviewSlice
    validation_result: ValidationResult
    source_bronze: str
