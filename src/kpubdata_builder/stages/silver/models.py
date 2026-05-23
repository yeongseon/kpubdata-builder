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

    def __post_init__(self) -> None:
        # frozen=True only blocks attribute reassignment; the mapping itself
        # stays mutable. Store a defensive copy so a caller mutating the dict
        # they passed in cannot alter this otherwise-immutable value.
        object.__setattr__(self, "null_counts", dict(self.null_counts))


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
    """Tabularized, validated dataset produced by the Silver stage.

    ``frozen=True`` here is shallow: ``table`` is held by reference. A Polars
    ``DataFrame`` is treated as immutable by convention (its operations return
    new frames), so this is safe in practice but is not deep immutability.
    """

    table: pl.DataFrame
    schema_summary: SchemaSummary
    statistics: TableStatistics
    preview: PreviewSlice
    validation_result: ValidationResult
    source_bronze: str
