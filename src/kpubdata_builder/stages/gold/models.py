"""Gold 단계 산출물 모델 (#47).

Silver 정제 테이블을 export-ready 패키지로 표현한다. Gold는 내부 stage
모델이므로 table에 Polars 타입을 노출한다. export 대상은 spec.ExportTarget을
재사용한다. split 지원(SplitSet)은 v0.2(#38)에서 도입한다.

주요 구성:
    - ExportPlan: 어떤 형식으로 어디에 내보낼지에 대한 계획
    - GoldPackage: Gold 단계 산출물
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ...spec import ExportTarget


@dataclass(frozen=True)
class ExportPlan:
    """Gold 패키지의 내보내기 계획.

    속성:
        targets: 적용할 내보내기 대상 목록 (spec.ExportTarget 재사용).
    """

    targets: tuple[ExportTarget, ...] = ()


@dataclass(frozen=True)
class GoldPackage:
    """export 준비가 끝난 최종 데이터셋 패키지.

    속성:
        dataset_name: 데이터셋 이름 (출력 디렉터리 세그먼트로도 사용).
        table: export용 최종 Polars 테이블.
        export_plan: 내보내기 계획.
        source_silver: 원천 SilverDataset의 source 참조.
        metadata: 패키지에 실을 임의 메타데이터.
    """

    dataset_name: str
    table: pl.DataFrame
    export_plan: ExportPlan
    source_silver: str
    metadata: dict[str, str] = field(default_factory=dict)
    splits: dict[str, pl.DataFrame] | None = None
