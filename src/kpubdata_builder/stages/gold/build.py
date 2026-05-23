"""Gold 단계 오케스트레이션 (#47).

SilverDataset을 받아 export-ready GoldPackage로 변환한다. 현재는 Silver 테이블을
그대로 패키징하고 내보내기 계획을 부착한다. split 분할은 v0.2(#38)에서 도입한다.

주요 함수:
    - build_gold_package: SilverDataset → GoldPackage
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...spec import ExportTarget
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage


def build_gold_package(
    silver: SilverDataset,
    *,
    dataset_name: str,
    exports: Sequence[ExportTarget] = (),
    metadata: Mapping[str, str] | None = None,
) -> GoldPackage:
    """Silver 데이터셋을 export-ready Gold 패키지로 변환한다.

    매개변수:
        silver: 원천 Silver 데이터셋.
        dataset_name: 데이터셋 이름.
        exports: 내보내기 대상 목록.
        metadata: 패키지에 실을 임의 메타데이터.

    반환값:
        GoldPackage: 최종 테이블과 내보내기 계획.
    """
    return GoldPackage(
        dataset_name=dataset_name,
        table=silver.table,
        export_plan=ExportPlan(targets=tuple(exports)),
        source_silver=silver.source_bronze,
        metadata=dict(metadata or {}),
    )
