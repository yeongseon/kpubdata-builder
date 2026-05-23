"""Silver 요약 (#46).

정규화된 테이블에서 스키마 요약과 테이블 통계를 산출한다. 실제 계산은 tabular
엔진(#49)에 위임하고, Silver 단계의 의미 있는 진입점만 제공한다.

주요 함수:
    - build_schema: pl.DataFrame → SchemaInfo
    - build_statistics: pl.DataFrame → TableStatistics
"""

from __future__ import annotations

import polars as pl

from ...tabular import SchemaInfo, TableStatistics, compute_statistics, infer_schema


def build_schema(table: pl.DataFrame) -> SchemaInfo:
    """테이블 스키마 요약을 생성한다."""
    return infer_schema(table)


def build_statistics(table: pl.DataFrame) -> TableStatistics:
    """테이블 통계 요약을 생성한다."""
    return compute_statistics(table)
