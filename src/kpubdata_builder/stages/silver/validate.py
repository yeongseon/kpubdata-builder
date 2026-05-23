"""Silver stage validation: structural checks producing a ValidationResult."""

from __future__ import annotations

import polars as pl

from .models import ValidationResult


def validate_table(table: pl.DataFrame) -> ValidationResult:
    """Run conservative structural checks on a tabularized Silver table.

    Only reports informational warnings; it does not impose data-quality
    opinions, per the Silver stage principle that the Builder must not
    define what "clean" data means.

    Empty-table policy: a table with no rows and/or no columns is a VALID
    Silver output. Emptiness yields warnings, never errors, so an empty
    upstream source does not fail the build; deciding whether emptiness is
    acceptable is left to downstream stages.
    """
    warnings: list[str] = []
    if table.width == 0:
        warnings.append("table has no columns")
    if table.height == 0:
        warnings.append("table has no rows")
    return ValidationResult(errors=(), warnings=tuple(warnings))
