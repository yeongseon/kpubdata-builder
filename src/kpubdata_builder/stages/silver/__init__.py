"""Silver stage public API."""

from __future__ import annotations

from .build import build_silver_dataset
from .models import (
    ColumnInfo,
    PreviewSlice,
    SchemaSummary,
    SilverDataset,
    TableStatistics,
    ValidationResult,
)
from .normalize import normalize_table
from .persist import SilverPersistResult, persist_silver_dataset
from .preview import build_preview
from .summarize import summarize_schema, summarize_statistics
from .validate import validate_table

__all__ = [
    "ColumnInfo",
    "PreviewSlice",
    "SchemaSummary",
    "SilverDataset",
    "SilverPersistResult",
    "TableStatistics",
    "ValidationResult",
    "build_preview",
    "build_silver_dataset",
    "normalize_table",
    "persist_silver_dataset",
    "summarize_schema",
    "summarize_statistics",
    "validate_table",
]
