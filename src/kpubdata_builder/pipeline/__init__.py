"""파이프라인 오케스트레이션 패키지 (#48).

Bronze → Silver → Gold → (Export) → Manifest 흐름을 묶는 orchestrator와 실행
컨텍스트를 노출한다. Export 단계 연결은 stage-aware exporter(#28/v0.2)에서 추가한다.

주요 구성:
    - BuildContext: 단일 실행 컨텍스트
    - run_build: 파이프라인 진입점
    - BuildResult / SourceBuildOutcome: 실행 결과 모델
    - preview_build: 파일 미기록 미리보기 진입점
    - PreviewResult / SourcePreview: 미리보기 결과 모델
"""

from __future__ import annotations

from .context import BuildContext
from .orchestrator import BuildResult, SourceBuildOutcome, run_build
from .preview import PreviewResult, SourcePreview, preview_build

__all__ = [
    "BuildContext",
    "BuildResult",
    "PreviewResult",
    "SourceBuildOutcome",
    "SourcePreview",
    "preview_build",
    "run_build",
]
