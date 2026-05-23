"""내보내기 도구가 사용하는 표준 조립 산출물 모델.

이 파일은 Bronze/Silver/Gold 등 내부 단계를 거친 뒤 exporter가 공통으로
소비할 수 있는 최소 데이터 구조를 정의한다.

주요 클래스:
    - ArtifactDataset: 레코드, 스키마, 메타데이터, provenance를 담는 값 객체
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .spec import JsonValue


@dataclass(frozen=True)
class ArtifactDataset:
    """구체적인 내보내기 전에 사용하는 조립된 데이터셋 표현.

    레코드 본문과 스키마, provenance, 통계 요약을 함께 보관하여
    다양한 exporter가 동일한 계약으로 동작하도록 만든다.

    속성:
        records: 내보낼 정규화 레코드 모음.
        schema: 컬럼명과 타입명을 담는 선택적 스키마 설명.
        metadata: 데이터셋 수준 메타데이터.
        provenance: 어떤 소스 조합에서 생성되었는지 나타내는 식별자 목록.
        statistics: 레코드 수 같은 간단한 집계 정보.

    예시:
        >>> artifact = ArtifactDataset(records=({"id": "1"},))
        >>> len(artifact.records)
        1
    """

    records: tuple[dict[str, JsonValue], ...] = ()
    schema: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    statistics: dict[str, int] = field(default_factory=dict)
