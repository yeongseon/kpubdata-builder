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

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

from .artifact import ArtifactDataset
from .errors import (
    BuildError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)
from .manifest import BuildManifest, manifest_writer
from .spec import BuildSpec, ExportTarget, SourceRef
from .spec.validator import validate_spec

# 버전의 정본은 pyproject.toml 의 `version` 하나뿐이다. 여기서 문자열을 다시
# 적어두면 두 값이 갈라진다 — 실제로 CHANGELOG 가 v0.4 를 서술하는 동안 이 상수와
# 배포 이미지 태그는 0.1.0 으로 남아 있었다(#592). 설치된 배포판 메타데이터에서
# 읽어 고칠 곳을 한 군데로 줄인다.
try:
    __version__ = _metadata_version("kpubdata-builder")
except PackageNotFoundError:  # pragma: no cover - 설치되지 않은 소스 트리에서만
    __version__ = "0.0.0+unknown"

__all__ = [
    "ArtifactDataset",
    "BuildError",
    "BuildManifest",
    "BuildSpec",
    "ExportError",
    "ManifestError",
    "ExportTarget",
    "SourceRef",
    "SpecLoadError",
    "ValidationError",
    "__version__",
    "manifest_writer",
    "validate_spec",
]
