"""Silver stage preview: a small row sample for inspection."""

from __future__ import annotations

import datetime
from decimal import Decimal

import polars as pl

from ...spec import JsonValue
from .models import PreviewSlice

DEFAULT_PREVIEW_ROWS = 10


def _to_json_safe(value: object) -> JsonValue:
    """Convert a Polars-deserialized value to a JSON-serializable Python type."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _convert_row(row: dict[str, object]) -> dict[str, JsonValue]:
    return {k: _to_json_safe(v) for k, v in row.items()}


def build_preview(table: pl.DataFrame, *, limit: int = DEFAULT_PREVIEW_ROWS) -> PreviewSlice:
    """Return the first ``limit`` rows as a preview slice.

    The full row count is preserved in ``total_rows`` regardless of ``limit``.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    head_rows: list[dict[str, JsonValue]] = [
        _convert_row(row) for row in table.head(limit).to_dicts()
    ]
    return PreviewSlice(
        rows=tuple(head_rows),
        total_rows=table.height,
    )
