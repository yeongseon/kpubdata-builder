"""빌드 매니페스트용 스키마 요약 모델 (#11).

이 모듈은 manifest에 실을 컬럼 수준 스키마 정보를 담는 불변 값 객체와,
원시 (name, type, nullable) 시퀀스로부터 요약을 만드는 빌더를 정의한다.
tabular 엔진에 의존하지 않도록 입력은 primitive 튜플로 받는다.

주요 구성:
    - FieldSummary: 단일 컬럼 요약 (이름/타입/nullable)
    - SchemaSummary: 컬럼 순서를 보존한 요약 묶음
    - build_schema_summary: (name, type, nullable) 시퀀스 → SchemaSummary
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSummary:
    """단일 컬럼의 스키마 요약.

    속성:
        name: 컬럼명.
        type: 컬럼 타입의 문자열 표현.
        nullable: 컬럼에 null 값이 존재할 수 있는지 여부.
    """

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class SchemaSummary:
    """데이터셋 스키마 요약.

    속성:
        fields: 컬럼 순서를 보존한 FieldSummary 묶음.
        total_fields: 컬럼 개수 (fields 길이와 동일).
    """

    fields: tuple[FieldSummary, ...] = ()
    total_fields: int = 0


def build_schema_summary(fields: Iterable[tuple[str, str, bool]]) -> SchemaSummary:
    """(name, type, nullable) 시퀀스로부터 SchemaSummary를 생성한다.

    매개변수:
        fields: 컬럼 순서를 보존한 (이름, 타입, nullable) 튜플 시퀀스.

    반환값:
        SchemaSummary: total_fields가 fields 길이와 일치하는 요약.
    """
    summaries = tuple(
        FieldSummary(name=name, type=type_name, nullable=nullable)
        for name, type_name, nullable in fields
    )
    return SchemaSummary(fields=summaries, total_fields=len(summaries))


__all__ = ["FieldSummary", "SchemaSummary", "build_schema_summary"]
