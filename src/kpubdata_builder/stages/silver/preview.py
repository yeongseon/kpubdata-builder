"""Silver stage preview: a small row sample for inspection."""

from __future__ import annotations

import polars as pl

from ...spec import JsonValue
from .models import PreviewSlice

DEFAULT_PREVIEW_ROWS = 10


def build_preview(table: pl.DataFrame, *, limit: int = DEFAULT_PREVIEW_ROWS) -> PreviewSlice:
    """Return the first ``limit`` rows as a preview slice.

    The full row count is preserved in ``total_rows`` regardless of ``limit``.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    head_rows: list[dict[str, JsonValue]] = table.head(limit).to_dicts()
    return PreviewSlice(
        rows=tuple(head_rows),
        total_rows=table.height,
    )
