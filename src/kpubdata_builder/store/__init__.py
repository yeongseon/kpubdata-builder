"""영속 Build 저장소 (#309, ADR 0003).

SQLite 기반 빌드 인덱스를 제공하여 파일시스템 스캔 비용을 줄이고
확장성/일관성/동시성을 개선한다.
"""

from __future__ import annotations

from .build_index import BuildIndex, SCHEMA_VERSION, rebuild_index

__all__ = ["BuildIndex", "SCHEMA_VERSION", "rebuild_index"]
