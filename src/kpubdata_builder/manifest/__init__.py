"""빌드 매니페스트 패키지 (Medallion 재구성).

매니페스트 모델(models.py)과 기록기(writer.py)의 공개 표면을 re-export 한다.

주요 구성:
    - BuildManifest: 실행 요약 데이터 클래스
    - manifest_writer / write_manifest: 디스크 기록 함수
"""

from __future__ import annotations

from .models import BuildManifest
from .writer import manifest_writer, write_manifest

__all__ = [
    "BuildManifest",
    "manifest_writer",
    "write_manifest",
]
