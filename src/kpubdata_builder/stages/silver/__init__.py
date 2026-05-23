"""Silver 단계 패키지 (#46).

Bronze raw records를 Polars 테이블로 변환하고 스키마 검증·통계 요약·미리보기를
생성하는 Silver 단계 구현을 노출한다.

주요 구성:
    - SilverDataset / ValidationResult: 산출물·검증 모델
    - build_silver_dataset: BronzeArtifact → SilverDataset
    - persist_silver_dataset: Silver 산출물 영속화
"""

from __future__ import annotations

from .build import build_silver_dataset
from .models import SilverDataset, ValidationResult
from .normalize import normalize_table
from .persist import SilverPersistResult, persist_silver_dataset
from .preview import build_preview
from .summarize import build_schema, build_statistics
from .validate import validate_table

__all__ = [
    "SilverDataset",
    "SilverPersistResult",
    "ValidationResult",
    "build_preview",
    "build_schema",
    "build_silver_dataset",
    "build_statistics",
    "normalize_table",
    "persist_silver_dataset",
    "validate_table",
]
