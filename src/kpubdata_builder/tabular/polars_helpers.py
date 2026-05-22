"""원시 레코드를 표 형태로 만들기 위한 작은 Polars 도우미."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, overload

import polars as pl

from ..spec import JsonValue

DtypeSpec = str | pl.DataType | type[pl.DataType]

_TRUE_TOKENS = {"1", "t", "true", "y", "yes"}
_FALSE_TOKENS = {"0", "f", "false", "n", "no"}

_NAMED_DTYPES: Mapping[str, pl.DataType] = {
    "bool": pl.Boolean(),
    "boolean": pl.Boolean(),
    "date": pl.Date(),
    "datetime": pl.Datetime(),
    "float": pl.Float64(),
    "float64": pl.Float64(),
    "int": pl.Int64(),
    "int64": pl.Int64(),
    "str": pl.Utf8(),
    "string": pl.Utf8(),
    "utf8": pl.Utf8(),
}


@dataclass(frozen=True)
class CastReport:
    """strict=False 캐스팅으로 추가된 null 값의 컬럼별 개수."""

    column: str
    nulls_before: int
    nulls_after: int

    @property
    def nulls_introduced(self) -> int:
        return self.nulls_after - self.nulls_before


@dataclass(frozen=True)
class CastResult:
    """audit 결과와 컬럼별 null 보고서를 담는 값 객체."""

    df: pl.DataFrame
    reports: tuple[CastReport, ...] = field(default_factory=tuple)

    @property
    def has_nulls_introduced(self) -> bool:
        return any(r.nulls_introduced > 0 for r in self.reports)


def records_to_dataframe(records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame:
    """원시 레코드 매핑을 Polars DataFrame으로 변환한다."""
    return pl.DataFrame(list(records))


def validate_required_columns(
    df: pl.DataFrame,
    required_columns: Sequence[str],
) -> pl.DataFrame:
    """필수 컬럼이 모두 있는지 확인하고 누락 시 예외를 발생시킨다."""
    missing_columns = [column for column in required_columns if column not in df.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise ValueError(f"Missing required columns: {missing}")
    return df


@overload
def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
    *,
    audit: Literal[False] = ...,
) -> pl.DataFrame: ...


@overload
def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
    *,
    audit: Literal[True],
) -> CastResult: ...


def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
    *,
    audit: bool = False,
) -> pl.DataFrame | CastResult:
    """지정한 타입으로 컬럼을 캐스팅하고 필요하면 null 보고서를 함께 반환한다.

    audit=True이면 컬럼별 null 보고서를 담은 CastResult를 반환한다.
    audit=False(기본값)이면 데이터프레임을 직접 반환한다.
    """
    reports: list[CastReport] = []
    expressions: list[pl.Expr] = []

    for column, dtype in dtypes.items():
        if column not in df.columns:
            raise ValueError(
                f"Cannot cast missing column: {column!r}. Available columns: {df.columns}"
            )
        resolved_dtype = _resolve_dtype(dtype)
        if isinstance(resolved_dtype, pl.Boolean):
            expressions.append(_cast_boolean(column))
        else:
            expressions.append(pl.col(column).cast(resolved_dtype, strict=False).alias(column))

    if not expressions:
        if audit:
            return CastResult(df=df, reports=())
        return df

    if audit:
        nulls_before = {col: df[col].null_count() for col in dtypes if col in df.columns}

    result_df = df.with_columns(expressions)

    if audit:
        for column in dtypes:
            if column in df.columns:
                report = CastReport(
                    column=column,
                    nulls_before=nulls_before[column],
                    nulls_after=result_df[column].null_count(),
                )
                if report.nulls_introduced > 0:
                    reports.append(report)
        return CastResult(df=result_df, reports=tuple(reports))

    return result_df


def _resolve_dtype(dtype: DtypeSpec) -> pl.DataType:
    if isinstance(dtype, str):
        normalized = dtype.strip().lower()
        try:
            return _NAMED_DTYPES[normalized]
        except KeyError as exc:
            supported = ", ".join(sorted(_NAMED_DTYPES.keys()))
            raise ValueError(f"Unsupported dtype: {dtype!r}. Supported: {supported}") from exc
    if isinstance(dtype, pl.DataType):
        return dtype
    if isinstance(dtype, type) and issubclass(dtype, pl.DataType):
        return dtype()
    raise TypeError(f"Invalid dtype spec: {dtype!r}")


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
