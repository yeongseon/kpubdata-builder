"""파이프라인 오케스트레이션 패키지 (#48).

Bronze → Silver → Gold → (Export) → Manifest 흐름을 묶는 orchestrator와 실행
컨텍스트를 노출한다. Export 단계 연결은 stage-aware exporter(#28/v0.2)에서 추가한다.

주요 구성:
    - BuildContext: 단일 실행 컨텍스트
    - run_build: 파이프라인 진입점
    - BuildResult / SourceBuildOutcome: 실행 결과 모델
    - preview_build: 파일 미기록 미리보기 진입점
    - PreviewResult / SourcePreview: 미리보기 결과 모델
    - PreviewDiffItem / PreviewTransformSummary / SampleMode: Source↔Silver diff·sampling
      결과 모델 (#497)
"""

from __future__ import annotations

from .context import BuildContext
from .orchestrator import BuildResult, SourceBuildOutcome, run_build
from .preview import (
    DEFAULT_PREVIEW_SEED,
    MAX_PREVIEW_DIFF_ITEMS,
    PreviewDiffItem,
    PreviewResult,
    PreviewTransformSummary,
    SampleMode,
    SourcePreview,
    preview_build,
)

__all__ = [
    "DEFAULT_PREVIEW_SEED",
    "MAX_PREVIEW_DIFF_ITEMS",
    "BuildContext",
    "BuildResult",
    "PreviewDiffItem",
    "PreviewResult",
    "PreviewTransformSummary",
    "SampleMode",
    "SourceBuildOutcome",
    "SourcePreview",
    "preview_build",
    "run_build",
]
