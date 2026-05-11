from __future__ import annotations

import polars as pl
import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular import (
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

    assert casted.schema["amount"] == pl.Int64
    assert casted.to_dicts() == [{"id": "1", "amount": 1000}]


def test_cast_columns_raises_for_missing_column() -> None:
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Cannot cast missing column: amount"):
        cast_columns(df, {"amount": "int"})


def test_cast_columns_raises_for_unknown_dtype() -> None:
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Unsupported dtype: money"):
        cast_columns(df, {"id": "money"})
