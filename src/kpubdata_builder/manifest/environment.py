"""빌드 환경 메타데이터 (#211).

매니페스트에 빌드를 생성한 실행 환경(Python/kpubdata/builder 버전)을 기록해
재현성과 디버깅을 돕는다. 패키지 메타데이터를 못 찾으면 "unknown"으로 폴백한다.

주요 구성:
    - BuildEnvironment: 실행 환경 스냅샷
    - capture_build_environment: 현재 환경에서 스냅샷 생성
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from importlib import metadata


@dataclass(frozen=True)
class BuildEnvironment:
    """빌드를 생성한 실행 환경 스냅샷.

    속성:
        python_version: 빌드를 실행한 Python 버전 (예: "3.12.3").
        kpubdata_version: 설치된 kpubdata 버전. 알 수 없으면 "unknown".
        builder_version: 설치된 kpubdata-builder 버전. 알 수 없으면 "unknown".
    """

    python_version: str
    kpubdata_version: str
    builder_version: str


def _package_version(name: str) -> str:
    """설치된 패키지 버전을 반환하고, 없으면 "unknown"을 돌려준다."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def capture_build_environment() -> BuildEnvironment:
    """현재 실행 환경의 BuildEnvironment 스냅샷을 만든다."""
    return BuildEnvironment(
        python_version=platform.python_version(),
        kpubdata_version=_package_version("kpubdata"),
        builder_version=_package_version("kpubdata-builder"),
    )


__all__ = ["BuildEnvironment", "capture_build_environment"]
