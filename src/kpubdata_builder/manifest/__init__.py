"""빌드 매니페스트 패키지 (Medallion 재구성).

매니페스트 모델(models.py)과 기록기(writer.py)의 공개 표면을 re-export 한다.

주요 구성:
    - BuildManifest: 실행 요약 데이터 클래스
    - FieldSummary / SchemaSummary / build_schema_summary: 스키마 요약 (#11)
    - SourceProvenance / build_source_provenance / compute_data_checksum: 상세 출처 (#12)
    - manifest_writer / write_manifest: 디스크 기록 함수
"""

from __future__ import annotations

from .environment import BuildEnvironment, capture_build_environment
from .models import MANIFEST_SCHEMA_VERSION, BuildManifest
from .provenance import (
    SourceProvenance,
    build_source_provenance,
    compute_data_checksum,
    compute_inputs_fingerprint,
)
from .schema_summary import FieldSummary, SchemaSummary, build_schema_summary
from .writer import manifest_writer, write_manifest

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "BuildEnvironment",
    "BuildManifest",
    "FieldSummary",
    "SchemaSummary",
    "SourceProvenance",
    "build_schema_summary",
    "build_source_provenance",
    "capture_build_environment",
    "compute_data_checksum",
    "compute_inputs_fingerprint",
    "manifest_writer",
    "write_manifest",
]
