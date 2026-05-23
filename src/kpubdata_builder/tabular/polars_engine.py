"""Polars 기반 tabular 엔진 구현 (#49).

DataFrame으로부터 스키마 정보, 테이블 통계, 미리보기 슬라이스를 산출한다.
records ↔ DataFrame 변환은 convert 모듈을 재노출하여 단일 진입점을 제공한다.

주요 함수:
    - infer_schema: DataFrame → SchemaInfo
    - compute_statistics: DataFrame → TableStatistics
    - generate_preview: DataFrame → PreviewSlice
"""

from __future__ import annotations

import polars as pl

from .convert import dataframe_to_records, records_to_dataframe
from .types import ColumnInfo, PreviewSlice, SchemaInfo, TableStatistics

DEFAULT_PREVIEW_LIMIT = 5


def infer_schema(df: pl.DataFrame) -> SchemaInfo:
    """DataFrame의 컬럼 스키마를 SchemaInfo로 추론한다.

    nullable은 컬럼에 null이 하나라도 존재하는지로 판정하고, unique_count는
    Polars n_unique(null을 하나의 고유값으로 포함) 기준이다.

    매개변수:
        df: 스키마를 추론할 DataFrame.

    반환값:
        SchemaInfo: 컬럼 순서를 보존한 스키마 요약.
    """
    columns = tuple(
        ColumnInfo(
            name=name,
            dtype=str(dtype),
            nullable=df.get_column(name).null_count() > 0,
            unique_count=df.get_column(name).n_unique(),
        )
        for name, dtype in df.schema.items()
    )
    return SchemaInfo(columns=columns)


def compute_statistics(df: pl.DataFrame) -> TableStatistics:
    """DataFrame의 행/널/중복 통계를 계산한다.

    duplicate_rate는 (전체 행 - 고유 행) / 전체 행으로 계산하며, 빈 테이블은
    0.0으로 둔다.

    매개변수:
        df: 통계를 계산할 DataFrame.

    반환값:
        TableStatistics: 행 수, 컬럼별 null 수, 중복 행 비율.
    """
    row_count = df.height
    null_counts = {name: df.get_column(name).null_count() for name in df.columns}
    duplicate_rate = 0.0 if row_count == 0 else 1.0 - (df.n_unique() / row_count)
    return TableStatistics(
        row_count=row_count,
        null_counts=null_counts,
        duplicate_rate=duplicate_rate,
    )


def generate_preview(df: pl.DataFrame, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewSlice:
    """DataFrame 상위 N행 미리보기 슬라이스를 생성한다.

    매개변수:
        df: 미리보기를 만들 DataFrame.
        limit: 포함할 최대 행 수 (기본값 DEFAULT_PREVIEW_LIMIT).

    반환값:
        PreviewSlice: 상위 행과 전체 행 수.
    """
    rows = tuple(dataframe_to_records(df.head(limit)))
    return PreviewSlice(rows=rows, total_rows=df.height)


__all__ = [
    "DEFAULT_PREVIEW_LIMIT",
    "compute_statistics",
    "dataframe_to_records",
    "generate_preview",
    "infer_schema",
    "records_to_dataframe",
]
