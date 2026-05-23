"""Polars tabular helper의 변환, 검증, 캐스팅 동작을 검증한다."""

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
    # 입력 레코드를 보존한 채 DataFrame으로 변환하는지 확인한다.
    records: list[dict[str, JsonValue]] = [
        {"id": "1", "amount": "1000", "district": "강남구"},
        {"id": "2", "amount": "2500", "district": "서초구"},
    ]

    df = records_to_dataframe(records)

    assert df.shape == (2, 3)
    assert df.to_dicts() == records
    assert records[0]["amount"] == "1000"


def test_records_to_dataframe_accepts_empty_records() -> None:
    # 빈 입력도 예외 없이 빈 DataFrame으로 처리되는지 검증한다.
    df = records_to_dataframe(())

    assert df.shape == (0, 0)


def test_validate_required_columns_returns_dataframe_when_columns_exist() -> None:
    # 필수 컬럼이 모두 존재하면 원본 DataFrame을 그대로 반환해야 한다.
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    result = validate_required_columns(df, ("id", "amount"))

    assert result is df


def test_validate_required_columns_raises_for_missing_columns() -> None:
    # 누락 컬럼 목록이 포함된 ValueError가 발생하는지 확인한다.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Missing required columns: amount, district"):
        validate_required_columns(df, ("id", "amount", "district"))


def test_cast_columns_casts_named_dtypes_without_changing_original_dataframe() -> None:
    # 문자열 dtype 별칭이 올바른 Polars 타입으로 캐스팅되는지 검증한다.
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
    # Polars dtype 클래스 자체도 입력으로 허용되는지 확인한다.
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    casted = cast_columns(df, {"amount": pl.Int64})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["amount"] == pl.Int64
    assert casted.to_dicts() == [{"id": "1", "amount": 1000}]


def test_cast_columns_accepts_polars_dtype_instance() -> None:
    """pl.Int64() (instantiated DataType) should work."""
    # DataType 인스턴스 입력도 정상 처리되는지 확인한다.
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    casted = cast_columns(df, {"amount": pl.Int64()})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["amount"] == pl.Int64


def test_cast_columns_accepts_parameterized_dtype_instance() -> None:
    """Parameterized dtype instances like pl.Datetime('ms') should work."""
    # 파라미터가 있는 DataType 인스턴스도 유지되는지 검증한다.
    df = records_to_dataframe(({"ts": "2025-01-01 00:00:00"},))

    casted = cast_columns(df, {"ts": pl.Datetime("ms")})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["ts"] == pl.Datetime("ms")


def test_cast_columns_raises_for_missing_column() -> None:
    # 존재하지 않는 컬럼 캐스팅 요청은 즉시 실패해야 한다.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Cannot cast missing column"):
        cast_columns(df, {"amount": "int"})


def test_cast_columns_raises_for_unknown_dtype() -> None:
    # 지원하지 않는 dtype 이름은 명시적으로 거부되는지 확인한다.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Unsupported dtype"):
        cast_columns(df, {"id": "money"})


def test_cast_columns_audit_returns_cast_result() -> None:
    # audit=True일 때 CastResult 래퍼를 반환하는지 검증한다.
    df = records_to_dataframe(
        (
            {"amount": "1000"},
            {"amount": "2000"},
        )
    )

    result = cast_columns(df, {"amount": "int"}, audit=True)

    assert isinstance(result, CastResult)
    assert result.df.schema["amount"] == pl.Int64
    assert result.has_nulls_introduced is False
    assert result.reports == ()


def test_cast_columns_audit_detects_data_loss() -> None:
    # 변환 실패로 null이 늘어난 컬럼이 보고서에 기록되는지 확인한다.
    df = records_to_dataframe(
        (
            {"amount": "1000"},
            {"amount": "bad"},
            {"amount": "3000"},
        )
    )

    result = cast_columns(df, {"amount": "int"}, audit=True)

    assert isinstance(result, CastResult)
    assert result.has_nulls_introduced is True
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.column == "amount"
    assert report.nulls_before == 0
    assert report.nulls_after == 1
    assert report.nulls_introduced == 1


def test_cast_columns_audit_empty_dtypes() -> None:
    # 캐스팅 대상이 없으면 원본 DataFrame과 빈 보고서를 유지해야 한다.
    df = records_to_dataframe(({"id": "1"},))

    result = cast_columns(df, {}, audit=True)

    assert isinstance(result, CastResult)
    assert result.df is df
    assert result.reports == ()


def test_cast_report_nulls_introduced() -> None:
    # null 증가량 계산 프로퍼티가 단순 차이를 반환하는지 확인한다.
    report = CastReport(column="x", nulls_before=1, nulls_after=3)
    assert report.nulls_introduced == 2
