"""SQLite 빌드 인덱스 모듈 (#328).

ADR 0003에 따라 완료된 빌드의 파생 인덱스를 제공한다.
manifest.json이 정본이며, 이 SQLite 인덱스는 목록/조회 성능 최적화를 위한 캐시이다.

주요 구성:
    BuildIndex: SQLite 연결 및 쿼리 래퍼
    initialize_schema: 스키마 생성/마이그레이션
"""

from __future__ import annotations

from .index import _SCHEMA_VERSION, BuildIndex, initialize_schema

__all__ = ["BuildIndex", "_SCHEMA_VERSION", "initialize_schema"]
