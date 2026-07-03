"""Silver 단계 산출물 모델 (#46).

Bronze raw records를 Polars 테이블로 정제한 결과와, 스키마/통계/미리보기/검증
정보를 함께 담는다. 스키마·통계·미리보기 값 객체는 tabular 엔진(#49)의 공개
타입을 재사용한다. Silver는 내부 stage 모델이므로 table에 Polars 타입을 노출한다.

주요 구성:
    - ValidationProblem: 개별 검증 위반 사항 (구조화된 객체, #261)
    - ValidationResult: 스키마 검증 결과
    - SilverDataset: Silver 단계 산출물
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl

from ...tabular import PreviewSlice, SchemaInfo, TableStatistics

if TYPE_CHECKING:
    from .validate import ValidationProblem


@dataclass(frozen=True)
class ValidationResult:
    """Silver 스키마 검증 결과.

    속성:
        ok: 모든 검증 규칙을 통과했는지 여부.
        problems: 위반한 규칙 메시지 목록.
    """

    ok: bool
    problems: tuple[ValidationProblem, ...] = ()


@dataclass(frozen=True)
class SilverDataset:
    """Silver 단계가 생성한 정제 테이블과 메타 정보.

    속성:
        table: 정제된 Polars 테이블.
        schema: 추론된 스키마 요약.
        statistics: 행/널/중복 통계.
        preview: 상위 N행 미리보기.
        validation: 스키마 검증 결과.
        source_bronze: 원천 BronzeArtifact의 source_key 참조.
    """

    table: pl.DataFrame
    schema: SchemaInfo
    statistics: TableStatistics
    preview: PreviewSlice
    validation: ValidationResult
    source_bronze: str
    metadata: dict[str, str] = field(default_factory=dict)
