"""Gold 단계 패키지 (#47).

Silver 정제 테이블을 export-ready 패키지(GoldPackage)로 만드는 Gold 단계
구현을 노출한다.

주요 구성:
    - GoldPackage / ExportPlan: 산출물·내보내기 계획 모델
    - build_gold_package: SilverDataset → GoldPackage
    - persist_gold_package: Gold 산출물 영속화
"""

from __future__ import annotations

from .build import build_gold_package
from .models import ExportPlan, GoldPackage
from .persist import GoldPersistResult, persist_gold_package

__all__ = [
    "ExportPlan",
    "GoldPackage",
    "GoldPersistResult",
    "build_gold_package",
    "persist_gold_package",
]
