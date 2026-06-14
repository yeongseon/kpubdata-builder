"""Polars tabular helper의 변환, 검증, 캐스팅 동작을 검증한다."""

from __future__ import annotations

import polars as pl
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular import (
    CastReport,
    CastResult,
    cast_columns,
    validate_required_columns,
)
from kpubdata_builder.tabular.convert import records_to_dataframe


def test_records_to_dataframe_rejects_heterogeneous_column() -> None:
    # 한 컬럼에 문자열+숫자가 섞이면 조용히 강제 변환하지 않고 명확히 실패한다 (#187).
    records: list[dict[str, JsonValue]] = [{"v": 1}, {"v": "two"}]

    with pytest.raises(TabularError, match="heterogeneous column types"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_numeric_mix_and_nulls() -> None:
    # int/float 혼합과 null은 호환으로 보고 통과시킨다(거짓 양성 방지) (#187).
    records: list[dict[str, JsonValue]] = [{"v": 1}, {"v": 2.5}, {"v": None}]

    df = records_to_dataframe(records)

    assert df.height == 3


def test_records_to_dataframe_rejects_large_int_mixed_with_float() -> None:
    # 2^53을 넘는 정수가 float와 같은 컬럼에 있으면 f64 업캐스트로 반올림되므로 거부 (#198).
    records: list[dict[str, JsonValue]] = [{"v": 9007199254740993}, {"v": 2.5}]

    with pytest.raises(TabularError, match="precision loss"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_large_int_only_column() -> None:
    # float가 섞이지 않은 순수 정수 컬럼은 i64로 정확히 보존되므로 통과한다 (#198).
    big = 9007199254740993
    records: list[dict[str, JsonValue]] = [{"v": big}, {"v": 1}]

    df = records_to_dataframe(records)

    assert df["v"].to_list() == [big, 1]


def test_records_to_dataframe_rejects_large_int_mixed_with_float_in_nested_list() -> None:
    # 중첩 list 안에서도 큰 정수+float 혼합은 f64 업캐스트로 반올림되므로 거부 (#198).
    records: list[dict[str, JsonValue]] = [{"v": [9007199254740993]}, {"v": [2.5]}]

    with pytest.raises(TabularError, match="precision loss"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_rejects_large_int_mixed_with_float_in_nested_struct() -> None:
    # 중첩 struct의 동일 필드에 큰 정수+float가 섞이면 거부 (#198).
    records: list[dict[str, JsonValue]] = [
        {"v": {"x": 9007199254740993}},
        {"v": {"x": 2.5}},
    ]

    with pytest.raises(TabularError, match="precision loss"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_large_int_and_float_in_separate_struct_fields() -> None:
    # 서로 다른 struct 필드는 별도 컬럼이므로 큰 정수와 float가 공존해도 안전하다 (#198).
    big = 9007199254740993
    records: list[dict[str, JsonValue]] = [{"v": {"i": big, "f": 2.5}}]

    df = records_to_dataframe(records)

    assert df.height == 1


def test_records_to_dataframe_detects_nested_list_heterogeneity() -> None:
    # list[int] vs list[str]는 같은 "list" 카테고리가 아니라 요소 타입까지 구분해 거부 (#199).
    records: list[dict[str, JsonValue]] = [{"v": [1]}, {"v": ["x"]}]

    with pytest.raises(TabularError, match="heterogeneous column types"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_detects_nested_struct_heterogeneity() -> None:
    # struct{x:int} vs struct{x:str}도 필드 타입까지 들여다보고 거부 (#199).
    records: list[dict[str, JsonValue]] = [{"v": {"x": 1}}, {"v": {"x": "s"}}]

    with pytest.raises(TabularError, match="heterogeneous column types"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_optional_nested_field() -> None:
    # 중첩 struct의 선택적 필드(한쪽 null/부재)는 거짓 양성으로 막지 않는다 (#199).
    records: list[dict[str, JsonValue]] = [
        {"v": {"x": 1, "y": "a"}},
        {"v": {"x": 2}},
        {"v": {"x": 3, "y": None}},
    ]

    df = records_to_dataframe(records)

    assert df.height == 3


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
        _ = validate_required_columns(df, ("id", "amount", "district"))


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
        _ = cast_columns(df, {"amount": "int"})


def test_cast_columns_raises_for_unknown_dtype() -> None:
    # 지원하지 않는 dtype 이름은 명시적으로 거부되는지 확인한다.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Unsupported dtype"):
        _ = cast_columns(df, {"id": "money"})


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
