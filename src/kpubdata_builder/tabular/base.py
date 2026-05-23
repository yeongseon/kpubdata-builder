"""tabular 엔진의 내부 protocol (#49).

Silver 단계가 의존하는 엔진 표면을 구조적 타입으로 명시한다. 공개 API가
아니라 내부 계약 문서화/타입 체크용이며, 현재 구현체는 polars_engine 모듈의
함수 집합이다. 단일 엔진(Polars) 원칙에 따라 dual-engine 추상화는 두지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import polars as pl

from ..spec import JsonValue
from .types import PreviewSlice, SchemaInfo, TableStatistics


class TabularEngine(Protocol):
    """tabular 엔진이 제공해야 하는 최소 연산 집합 (internal)."""

    def records_to_dataframe(self, records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame: ...

    def dataframe_to_records(self, df: pl.DataFrame) -> list[dict[str, JsonValue]]: ...

    def infer_schema(self, df: pl.DataFrame) -> SchemaInfo: ...

    def compute_statistics(self, df: pl.DataFrame) -> TableStatistics: ...

    def generate_preview(self, df: pl.DataFrame, limit: int) -> PreviewSlice: ...


__all__ = ["TabularEngine"]
