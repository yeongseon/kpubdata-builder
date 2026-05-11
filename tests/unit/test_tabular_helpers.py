from __future__ import annotations

import polars as pl
import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular import (
    CastReport,
    CastResult,
    cast_columns,
    records_to_dataframe,
    validate_required_columns,
)


def test_records_to_dataframe_converts_raw_records_without_mutating_input() -> None:
    records: list[dict[str, JsonValue]] = [
        {"id": "1", "amount": "1000", "district": "강남구"},
        {"id": "2", "amount": "2500", "district": "서초구"},
    ]

    df = records_to_dataframe(records)

    assert df.shape == (2, 3)
    assert df.to_dicts() == records
    assert records[0]["amount"] == "1000"


def test_records_to_dataframe_accepts_empty_records() -> None:
    df = records_to_dataframe(())

    assert df.shape == (0, 0)


def test_validate_required_columns_returns_dataframe_when_columns_exist() -> None:
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    result = validate_required_columns(df, ("id", "amount"))

    assert result is df


def test_validate_required_columns_raises_for_missing_columns() -> None:
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Missing required columns: amount, district"):
        validate_required_columns(df, ("id", "amount", "district"))


def test_cast_columns_casts_named_dtypes_without_changing_original_dataframe() -> None:
    df = records_to_dataframe(
        (
            {"id": "1", "amount": "1000", "ratio": "1.5", "active": "true"},
            {"id": "2", "amount": "bad", "ratio": "", "active": "false"},
        )
    )

    casted = cast_columns(
        df,
        {
            "id": "str",
            "amount": "int",
            "ratio": "float",
            "active": "bool",
        },
    )

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["id"] == pl.Utf8
    assert casted.schema["amount"] == pl.Int64
    assert casted.schema["ratio"] == pl.Float64
    assert casted.schema["active"] == pl.Boolean
    assert casted.to_dicts() == [
        {"id": "1", "amount": 1000, "ratio": 1.5, "active": True},
        {"id": "2", "amount": None, "ratio": None, "active": False},
    ]
    assert df.schema["amount"] == pl.Utf8


def test_cast_columns_accepts_polars_dtypes() -> None:
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    casted = cast_columns(df, {"amount": pl.Int64})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["amount"] == pl.Int64
    assert casted.to_dicts() == [{"id": "1", "amount": 1000}]


def test_cast_columns_accepts_polars_dtype_class() -> None:
    """type[pl.DataType] (e.g. pl.Int64 without ()) should also work."""
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    casted = cast_columns(df, {"amount": pl.Int64})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["amount"] == pl.Int64


def test_cast_columns_accepts_parameterized_dtype_instance() -> None:
    """Parameterized dtype instances like pl.Datetime('ms') should work."""
    df = records_to_dataframe(({"ts": "2025-01-01 00:00:00"},))

    casted = cast_columns(df, {"ts": pl.Datetime("ms")})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["ts"] == pl.Datetime("ms")


def test_cast_columns_raises_for_missing_column() -> None:
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Cannot cast missing column"):
        cast_columns(df, {"amount": "int"})


def test_cast_columns_raises_for_unknown_dtype() -> None:
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Unsupported dtype"):
        cast_columns(df, {"id": "money"})


def test_cast_columns_audit_returns_cast_result() -> None:
    df = records_to_dataframe(
        (
            {"amount": "1000"},
            {"amount": "2000"},
        )
    )

    result = cast_columns(df, {"amount": "int"}, audit=True)

    assert isinstance(result, CastResult)
    assert result.df.schema["amount"] == pl.Int64
    assert result.has_data_loss is False
    assert result.reports == ()


def test_cast_columns_audit_detects_data_loss() -> None:
    df = records_to_dataframe(
        (
            {"amount": "1000"},
            {"amount": "bad"},
            {"amount": "3000"},
        )
    )

    result = cast_columns(df, {"amount": "int"}, audit=True)

    assert isinstance(result, CastResult)
    assert result.has_data_loss is True
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.column == "amount"
    assert report.nulls_before == 0
    assert report.nulls_after == 1
    assert report.nulls_introduced == 1


def test_cast_columns_audit_empty_dtypes() -> None:
    df = records_to_dataframe(({"id": "1"},))

    result = cast_columns(df, {}, audit=True)

    assert isinstance(result, CastResult)
    assert result.df is df
    assert result.reports == ()


def test_cast_report_nulls_introduced() -> None:
    report = CastReport(column="x", nulls_before=1, nulls_after=3)
    assert report.nulls_introduced == 2
