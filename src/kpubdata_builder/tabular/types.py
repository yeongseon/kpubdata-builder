"""tabular 엔진의 공개 값 객체 (#49).

Silver 단계가 소비하는 스키마/통계/미리보기 표현을 정의한다. Polars 타입을
공개 표면에 노출하지 않기 위해, dtype은 문자열로만 표현한다.

주요 구성:
    - ColumnInfo: 컬럼 단위 스키마 정보
    - SchemaInfo: 테이블 전체 스키마 요약
    - TableStatistics: 행/널/중복 통계
    - PreviewSlice: 상위 N행 미리보기 슬라이스
"""

from __future__ import annotations

from dataclasses import dataclass

from ..spec import JsonValue


@dataclass(frozen=True)
class ColumnInfo:
    """단일 컬럼의 추론된 스키마 정보.

    속성:
        name: 컬럼명.
        dtype: Polars dtype의 문자열 표현 (예: "Int64", "String").
        nullable: 컬럼에 null 값이 하나라도 존재하는지 여부.
        unique_count: 고유 값 개수 (null 포함, Polars n_unique 기준).
    """

    name: str
    dtype: str
    nullable: bool
    unique_count: int


@dataclass(frozen=True)
class SchemaInfo:
    """테이블 전체 스키마 요약.

    속성:
        columns: 컬럼 순서를 보존한 ColumnInfo 튜플.
    """

    columns: tuple[ColumnInfo, ...] = ()


@dataclass(frozen=True)
class TableStatistics:
    """테이블 수준 통계 요약.

    속성:
        row_count: 전체 행 수.
        null_counts: 컬럼별 null 개수.
        duplicate_rate: 중복 행 비율 (0.0 ~ 1.0). 빈 테이블은 0.0.
    """

    row_count: int
    null_counts: dict[str, int]
    duplicate_rate: float


@dataclass(frozen=True)
class PreviewSlice:
    """상위 N행 미리보기 슬라이스.

    속성:
        rows: 미리보기 행 (plain dict). Polars 타입을 노출하지 않는다.
        total_rows: 원본 테이블의 전체 행 수.
    """

    rows: tuple[dict[str, JsonValue], ...]
    total_rows: int


__all__ = [
    "ColumnInfo",
    "PreviewSlice",
    "SchemaInfo",
    "TableStatistics",
]
