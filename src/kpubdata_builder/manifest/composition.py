"""빌드 매니페스트용 composition(join) provenance 모델 (#506).

CompositionSpec/JoinSpec으로 두 source를 결합한 결과의 출처 추적 정보를 담는다.
duplicate key로 인한 explosion 위험은 사전 계산된 비율이 아니라 원본 행 수/distinct
키 수를 그대로 남긴다 — auditor가 스스로 재계산/검증할 수 있게 하기 위함이다
(임의 요약값으로 근거를 감추지 않는다는 이 저장소의 기존 provenance 원칙과 동일).

주요 구성:
    - CompositionProvenance: composition 결과의 출처 스냅샷
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompositionProvenance:
    """composition(join) 실행 결과의 상세 출처 정보.

    속성:
        name: 결합 Gold dataset 이름 (CompositionSpec.name).
        left: 왼쪽 source의 output key(alias).
        right: 오른쪽 source의 output key(alias).
        join_type: "inner" | "left".
        left_key: 왼쪽 join key 컬럼명.
        right_key: 오른쪽 join key 컬럼명.
        left_row_count: 왼쪽 Silver 테이블 행 수.
        left_distinct_key_count: 왼쪽 join key 컬럼의 distinct 값 수 (null 제외).
        right_row_count: 오른쪽 Silver 테이블 행 수.
        right_distinct_key_count: 오른쪽 join key 컬럼의 distinct 값 수 (null 제외).
        output_row_count: join 결과 행 수.
        duplicate_key_warning: 양쪽 join key 모두 중복 값을 가져(many-to-many) 행
            폭증 위험이 감지됐는지 여부. 감지 시 실제 처리(경고만 남길지, 빌드를
            실패시킬지)는 JoinSpec.on_duplicate_key가 결정한다 — 이 필드는 감지
            여부만 기록한다.
    """

    name: str
    left: str
    right: str
    join_type: str
    left_key: str
    right_key: str
    left_row_count: int
    left_distinct_key_count: int
    right_row_count: int
    right_distinct_key_count: int
    output_row_count: int
    duplicate_key_warning: bool


__all__ = ["CompositionProvenance"]
