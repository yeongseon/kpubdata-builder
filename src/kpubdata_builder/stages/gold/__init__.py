"""Gold 단계 패키지 (#47).

Silver 정제 테이블을 export-ready 패키지(GoldPackage)로 만드는 Gold 단계
구현을 노출한다.

주요 구성:
    - GoldPackage / ExportPlan: 산출물·내보내기 계획 모델
    - build_gold_package: SilverDataset → GoldPackage
    - persist_gold_package: Gold 산출물 영속화
    - DatasetCard / build_dataset_card / render_dataset_card: 데이터셋 카드 (#37)
"""

from __future__ import annotations

from .build import build_gold_package
from .card import CardField, DatasetCard, build_dataset_card, render_dataset_card
from .models import ExportPlan, GoldPackage
from .persist import GoldPersistResult, persist_gold_package
from .split import apply_splits, apply_splits_to_frame

__all__ = [
    "CardField",
    "DatasetCard",
    "ExportPlan",
    "GoldPackage",
    "GoldPersistResult",
    "apply_splits",
    "apply_splits_to_frame",
    "build_dataset_card",
    "build_gold_package",
    "persist_gold_package",
    "render_dataset_card",
]
