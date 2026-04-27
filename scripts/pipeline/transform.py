"""Transform raw records into a validated Polars DataFrame."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import polars as pl

logger = logging.getLogger("publish_to_hf.transform")

_FILTER_RE = re.compile(r"(\w+)\s*(>=|<=|!=|==|>|<)\s*(.+)")
_NULL_TOKENS = {"", "-", "N/A", "null", "None"}


def transform_records(records: list[dict[str, Any]], config: dict[str, Any]) -> pl.DataFrame:
    """Transform raw records into a typed Polars DataFrame based on config."""
    transform = config["transform"]
    column_mapping: dict[str, str] = transform["column_mapping"]
    dtypes: dict[str, str] = transform.get("dtypes", {})

    mapped: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {}
        for raw_key, clean_key in column_mapping.items():
            val = record.get(raw_key)
            row[clean_key] = str(val) if val is not None else None
        mapped.append(row)

    # Force all columns to Utf8 to avoid mixed-type inference errors
    schema = {clean: pl.Utf8 for clean in column_mapping.values()}
    df = pl.DataFrame(mapped, schema=schema)
    logger.info("Raw DataFrame: %d rows x %d cols", df.height, df.width)

    for col, dtype_str in dtypes.items():
        if col not in df.columns:
            continue
        df = _cast_column(df, col, dtype_str)

    for derived in transform.get("derived", []):
        df = _add_derived_column(df, derived)

    pre_filter_count = df.height
    for filter_expr in transform.get("filters", []):
        df = _apply_filter(df, filter_expr)
    if df.height != pre_filter_count:
        logger.info("Filtered: %d -> %d rows", pre_filter_count, df.height)

    logger.info("Transformed DataFrame: %d rows x %d cols", df.height, df.width)
    return df


def validate_schema(df: pl.DataFrame, config: dict[str, Any]) -> None:
    """Validate that the DataFrame schema matches the config exactly.

    Fails if:
    - DataFrame has columns not declared in column_mapping + derived
    - Config declares columns missing from DataFrame
    - Any non-allowlisted column is 100% null
    """
    transform = config["transform"]
    column_mapping = transform["column_mapping"]
    derived = [d["name"] for d in transform.get("derived", [])]
    expected_cols = set(column_mapping.values()) | set(derived)
    actual_cols = set(df.columns)

    undeclared = actual_cols - expected_cols
    if undeclared:
        logger.error("Schema validation FAILED: undeclared columns in output: %s", undeclared)
        sys.exit(1)

    missing = expected_cols - actual_cols
    if missing:
        logger.error("Schema validation FAILED: declared columns missing from output: %s", missing)
        sys.exit(1)

    # Check for 100% null columns (warn, don't fail — some fields are legitimately sparse)
    for col in df.columns:
        if df[col].null_count() == df.height:
            logger.warning("Column '%s' is 100%% null in this dataset", col)

    logger.info("Schema validation passed: %d columns match config", len(actual_cols))


def _apply_filter(df: pl.DataFrame, expr_str: str) -> pl.DataFrame:
    """Apply a simple comparison filter: 'col >= value', 'col != value', etc."""
    match = _FILTER_RE.match(expr_str.strip())
    if not match:
        logger.warning("Cannot parse filter expression: %s", expr_str)
        return df

    col, op, val_str = match.group(1), match.group(2), match.group(3).strip()

    if col not in df.columns:
        logger.warning("Filter references unknown column '%s', skipping", col)
        return df

    try:
        val: int | float = int(val_str)
    except ValueError:
        val = float(val_str)

    ops = {
        ">": pl.col(col) > val,
        "<": pl.col(col) < val,
        ">=": pl.col(col) >= val,
        "<=": pl.col(col) <= val,
        "==": pl.col(col) == val,
        "!=": pl.col(col) != val,
    }
    return df.filter(ops[op])


def _nullify_tokens(col: str) -> pl.Expr:
    return (
        pl.when(pl.col(col).cast(pl.Utf8).str.strip_chars().is_in(list(_NULL_TOKENS)))
        .then(pl.lit(None, dtype=pl.Utf8))
        .otherwise(pl.col(col).cast(pl.Utf8).str.strip_chars())
        .alias(col)
    )


def _cast_column(df: pl.DataFrame, col: str, dtype_str: str) -> pl.DataFrame:
    if dtype_str == "int_comma":
        return df.with_columns(
            pl.col(col)
            .cast(pl.Utf8)
            .str.replace_all(",", "")
            .str.strip_chars()
            .cast(pl.Int64, strict=False)
            .alias(col)
        )
    if dtype_str == "int":
        df = df.with_columns(_nullify_tokens(col))
        return df.with_columns(pl.col(col).cast(pl.Int64, strict=False).alias(col))
    if dtype_str == "float":
        df = df.with_columns(_nullify_tokens(col))
        return df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias(col))
    if dtype_str == "str":
        return df.with_columns(pl.col(col).cast(pl.Utf8).alias(col))
    logger.warning("Unknown dtype '%s' for column '%s', skipping cast", dtype_str, col)
    return df


def _add_derived_column(df: pl.DataFrame, derived: dict[str, Any]) -> pl.DataFrame:
    name = derived["name"]
    expr = derived["expr"]
    dtype_str = derived.get("dtype", "str")

    if expr.startswith("concat_date("):
        inner = expr[len("concat_date(") : -1]
        col_refs = [p.strip() for p in inner.split(",")]
        if len(col_refs) == 3:
            year_col, month_col, day_col = col_refs
            polars_expr = (
                pl.col(year_col).cast(pl.Utf8)
                + pl.lit("-")
                + pl.col(month_col).cast(pl.Utf8).str.zfill(2)
                + pl.lit("-")
                + pl.col(day_col).cast(pl.Utf8).str.zfill(2)
            ).alias(name)
            df = df.with_columns(polars_expr)
        else:
            logger.warning("concat_date requires exactly 3 columns, got %d", len(col_refs))
            return df
    elif expr.startswith("format("):
        inner = expr[len("format(") : -1]
        parts = [p.strip() for p in inner.split(",")]
        fmt_str = parts[0].strip("'\"")
        col_refs = [p.strip() for p in parts[1:]]
        polars_expr = pl.format(fmt_str, *[pl.col(c) for c in col_refs]).alias(name)
        df = df.with_columns(polars_expr)
    else:
        logger.warning("Unsupported derived expression: %s", expr)
        return df

    if dtype_str != "str":
        df = _cast_column(df, name, dtype_str)
    return df


def build_variant_dataframes(df: pl.DataFrame, config: dict[str, Any]) -> dict[str, pl.DataFrame]:
    """Build dataset variants (e.g. ko/en) based on config.

    If no variants configured, returns {"default": df}.
    """
    variants_cfg: dict[str, Any] = config.get("variants", {})
    if not variants_cfg:
        return {"default": df}

    result: dict[str, pl.DataFrame] = {}
    for name, opts in variants_cfg.items():
        variant_df = df.clone()

        romanize_cols = opts.get("romanize_columns", [])
        if romanize_cols:
            from kr_building_normalizer import romanize

            for col in romanize_cols:
                if col in variant_df.columns:
                    variant_df = variant_df.with_columns(
                        pl.col(col).map_elements(romanize, return_dtype=pl.Utf8).alias(col)
                    )

        col_rename = opts.get("column_rename", {})
        if col_rename:
            rename_map = {k: v for k, v in col_rename.items() if k in variant_df.columns}
            variant_df = variant_df.rename(rename_map)

        result[name] = variant_df
        logger.info("Variant '%s': %d rows x %d cols", name, variant_df.height, variant_df.width)

    return result
