"""SQLite 빌드 인덱스 구현 (#328).

ADR 0003에 따른 완료된 빌드 파생 인덱스. manifest.json이 정본이며,
이 SQLite 인덱스는 목록/조회 성능 최적화를 위한 캐시 역할을 한다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

# 현재 스키마 버전 (마이그레이션 대비)
_SCHEMA_VERSION = 1

# 빌드 상태 타입
BuildStatus = Literal["completed", "failed", "cancelled"]


def initialize_schema(db_path: Path) -> None:
    """SQLite 데이터베이스 스키마를 초기화한다 (#328).

    매개변수:
        db_path: 데이터베이스 파일 경로.

    스키마:
        - builds 테이블 (run_id, status, started_at, finished_at, spec_digest,
          error, schema_version)
        - 인덱스 (finished_at, status)
        - WAL 모드 활성화
        - busy_timeout 설정 (5초)
    """
    # 데이터베이스 파일이 없으면 부모 디렉터리 생성
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with _get_connection(db_path) as conn:
        cursor = conn.cursor()

        # builds 테이블 생성
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS builds (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                spec_digest TEXT,
                error TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        # 인덱스 생성 (목록 조회 최적화)
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_builds_finished_at
            ON builds(finished_at DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_builds_status
            ON builds(status)
            """
        )

        # WAL 모드 활성화 (Write-Ahead Logging)
        # - 동시 읽기/쓰기 허용
        # - 크래시 발생 시 복구 가능
        cursor.execute("PRAGMA journal_mode=WAL")

        # busy_timeout 설정 (5초)
        # - 데이터베이스가 잠겨 있을 때 대기 시간
        cursor.execute("PRAGMA busy_timeout=5000")

        conn.commit()


@contextmanager
def _get_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """SQLite 연결 컨텍스트 매니저."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


class BuildIndex:
    """SQLite 빌드 인덱스 래퍼 (#328).

    빌드 생성/상태전이 시 인덱스를 갱신하며,
    list_builds 쿼리를 제공한다.
    """

    def __init__(self, db_path: Path) -> None:
        """빌드 인덱스를 초기화한다.

        매개변수:
            db_path: 데이터베이스 파일 경로.
        """
        self._db_path = db_path
        # 데이터베이스가 없으면 스키마 초기화
        if not db_path.exists():
            initialize_schema(db_path)

    def record_build(
        self,
        *,
        run_id: str,
        status: BuildStatus,
        started_at: str,
        finished_at: str,
        spec_digest: str | None = None,
        error: str | None = None,
    ) -> None:
        """빌드를 인덱스에 기록한다.

        매개변수:
            run_id: 실행 식별자.
            status: 빌드 상태 (completed/failed/cancelled).
            started_at: 시작 시간 (ISO 8601 문자열).
            finished_at: 종료 시간 (ISO 8601 문자열).
            spec_digest: 스펙 해시 (변경 감지용, 선택).
            error: 실패 시 오류 메시지 (선택).

        예외:
            sqlite3.Error: 데이터베이스 오류 발생 시.
        """
        with _get_connection(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO builds
                (run_id, status, started_at, finished_at, spec_digest, error, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, status, started_at, finished_at, spec_digest, error, _SCHEMA_VERSION),
            )
            conn.commit()

    def list_builds(
        self,
        *,
        limit: int = 50,
        status: BuildStatus | None = None,
    ) -> list[dict[str, object]]:
        """빌드 목록을 조회한다.

        매개변수:
            limit: 최대 반환 개수 (기본값 50).
            status: 필터링할 상태 (선택).

        반환값:
            빌드 정보 목록 (최신순).
        """
        with _get_connection(self._db_path) as conn:
            cursor = conn.cursor()

            if status:
                cursor.execute(
                    """
                    SELECT run_id, status, started_at, finished_at, spec_digest, error
                    FROM builds
                    WHERE status = ?
                    ORDER BY finished_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT run_id, status, started_at, finished_at, spec_digest, error
                    FROM builds
                    ORDER BY finished_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )

            rows = cursor.fetchall()
            return [
                {
                    "run_id": row[0],
                    "status": row[1],
                    "started_at": row[2],
                    "finished_at": row[3],
                    "spec_digest": row[4],
                    "error": row[5],
                }
                for row in rows
            ]

    def get_build(self, run_id: str) -> dict[str, object] | None:
        """특정 빌드를 조회한다.

        매개변수:
            run_id: 실행 식별자.

        반환값:
            빌드 정보 딕셔너리 또는 None (없는 경우).
        """
        with _get_connection(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT run_id, status, started_at, finished_at, spec_digest, error
                FROM builds
                WHERE run_id = ?
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "run_id": row[0],
                "status": row[1],
                "started_at": row[2],
                "finished_at": row[3],
                "spec_digest": row[4],
                "error": row[5],
            }


__all__ = ["BuildIndex", "initialize_schema", "_SCHEMA_VERSION"]
