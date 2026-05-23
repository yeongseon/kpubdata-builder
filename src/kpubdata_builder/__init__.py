"""kpubdata-builder의 공개 패키지 표면.

이 모듈은 외부 사용자가 가장 먼저 가져오게 되는 핵심 타입과 예외를
한곳에 다시 노출한다.

주요 구성:
    - ArtifactDataset: 내보내기 전 표준 산출물 표현
    - BuildSpec / SourceRef / ExportTarget: 선언적 빌드 명세 모델
    - BuildManifest: 실행 결과를 기록하는 매니페스트 모델
    - validate_spec: 빌드 명세 검증 진입점
"""

from __future__ import annotations

from .artifact import ArtifactDataset
from .errors import (
    AssemblyError,
    BuildError,
    ExecutionError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)
from .manifest import BuildManifest, manifest_writer
from .spec import BuildSpec, ExportTarget, SourceRef
from .validator import validate_spec

__version__ = "0.1.0a0"  # 패키지 버전 문자열

__all__ = [
    "ArtifactDataset",
    "BuildError",
    "BuildManifest",
    "BuildSpec",
    "ExecutionError",
    "ExportError",
    "ManifestError",
    "ExportTarget",
    "SourceRef",
    "SpecLoadError",
    "ValidationError",
    "AssemblyError",
    "__version__",
    "manifest_writer",
    "validate_spec",
]
