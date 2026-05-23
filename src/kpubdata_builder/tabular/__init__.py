"""Polars 기반 tabular 엔진 패키지.

스키마/통계/미리보기 산출(polars_engine), 타입 캐스팅 도우미
(polars_helpers), 공개 값 객체(types)를 한곳에 노출한다.

원칙:
    - Polars 단일 엔진 (dual-engine 금지)
    - 공개 루트 API에는 Polars 반환 타입을 직접 다시 노출하지 않는다
    - records ↔ DataFrame 변환은 내부/서브모듈(convert)에서만 사용한다
"""

from __future__ import annotations

from .polars_engine import (
    DEFAULT_PREVIEW_LIMIT,
    compute_statistics,
    generate_preview,
    infer_schema,
)
from .polars_helpers import (
    CastReport,
    CastResult,
    cast_columns,
    validate_required_columns,
)
from .types import ColumnInfo, PreviewSlice, SchemaInfo, TableStatistics

__all__ = [
    "DEFAULT_PREVIEW_LIMIT",
    "CastReport",
    "CastResult",
    "ColumnInfo",
    "PreviewSlice",
    "SchemaInfo",
    "TableStatistics",
    "cast_columns",
    "compute_statistics",
    "generate_preview",
    "infer_schema",
    "validate_required_columns",
]
