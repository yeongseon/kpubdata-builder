"""Small Polars helpers for tabularizing raw records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from ..spec import JsonValue

DtypeSpec = str | type[pl.DataType]

_TRUE_TOKENS = {"1", "t", "true", "y", "yes"}
_FALSE_TOKENS = {"0", "f", "false", "n", "no"}

_NAMED_DTYPES: Mapping[str, type[pl.DataType]] = {
    "bool": pl.Boolean,
    "boolean": pl.Boolean,
    "date": pl.Date,
    "datetime": pl.Datetime,
    "float": pl.Float64,
    "float64": pl.Float64,
    "int": pl.Int64,
    "int64": pl.Int64,
    "str": pl.Utf8,
    "string": pl.Utf8,
    "utf8": pl.Utf8,
}


def records_to_dataframe(records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame:
    """Convert raw record mappings into a Polars DataFrame."""
    return pl.DataFrame(list(records))


def validate_required_columns(
    df: pl.DataFrame,
    required_columns: Sequence[str],
) -> pl.DataFrame:
    """Validate that a DataFrame contains all required columns."""
    missing_columns = [column for column in required_columns if column not in df.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise ValueError(f"Missing required columns: {missing}")
    return df


def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
) -> pl.DataFrame:
    """Cast DataFrame columns according to a column-to-dtype mapping."""
    expressions: list[pl.Expr] = []
    for column, dtype in dtypes.items():
        if column not in df.columns:
            raise ValueError(f"Cannot cast missing column: {column}")
        resolved_dtype = _resolve_dtype(dtype)
        if resolved_dtype == pl.Boolean:
            expressions.append(_cast_boolean(column))
        else:
            expressions.append(pl.col(column).cast(resolved_dtype, strict=False).alias(column))

    if not expressions:
        return df
    return df.with_columns(expressions)


def _resolve_dtype(dtype: DtypeSpec) -> type[pl.DataType]:
    if isinstance(dtype, str):
        normalized = dtype.strip().lower()
        try:
            return _NAMED_DTYPES[normalized]
        except KeyError as exc:
            raise ValueError(f"Unsupported dtype: {dtype}") from exc
    return dtype


def _cast_boolean(column: str) -> pl.Expr:
    normalized = pl.col(column).cast(pl.Utf8).str.strip_chars().str.to_lowercase()
    return (
        pl.when(normalized.is_in(_TRUE_TOKENS))
        .then(pl.lit(True))
        .when(normalized.is_in(_FALSE_TOKENS))
        .then(pl.lit(False))
        .otherwise(pl.lit(None, dtype=pl.Boolean))
        .alias(column)
    )
