"""SQLite 기반 빌드 인덱스 (#309, ADR 0003).

완료된 빌드의 메타데이터를 인덱싱하여 목록 조회 성능을 개선한다.
manifest.json이 정본이며, 이 인덱스는 파생물이다.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    _BaseConn = sqlite3.Connection
else:
    _BaseConn = object

# 스키마 버전: 인덱스 구조 변경 시 증가
SCHEMA_VERSION = 1

# 인덱스 파일 이름
_INDEX_FILENAME = "_builds.sqlite"


@dataclass(frozen=True)
class BuildEntry:
    """빌드 인덱스 엔트리."""

    run_id: str
    status: Literal["ok", "failed"]
    started_at: str | None
    finished_at: str | None
    spec_digest: str | None
    error: str | None


class BuildIndex:
    """SQLite 기반 빌드 인덱스.

    ADR 0003에 따라:
    - manifest.json이 정본이며, 이 인덱스는 파생물이다
    - 인덱스 쓰기 실패가 빌드 실패의 원인이 되어서는 안 됨
    - WAL 모드 + busy_timeout으로 동시성 안전 장치
    """

    def __init__(self, output_root: Path) -> None:
        """인덱스를 초기화한다.

        Args:
            output_root: 빌드 출력 루트 디렉터리 (인덱스는 output_root/_builds.sqlite)
        """
        self._output_root = output_root
        self._index_path = output_root / _INDEX_FILENAME
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        """스레드 로컬 연결을 반환한다 (lazy initialization)."""
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return cast(sqlite3.Connection, self._local.conn)

    def _connect(self) -> sqlite3.Connection:
        """새 SQLite 연결을 생성하고 설정한다."""
        conn = sqlite3.connect(
            str(self._index_path),
            timeout=30.0,  # busy_timeout: 동시성 경합 시 대기 시간
        )
        conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging: 동시 읽기 허용
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        """데이터베이스 스키마를 초기화한다."""
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            # 스키마 버전 확인
            cur = self._conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            current_version = row[0] if row else None

            if current_version != SCHEMA_VERSION:
                # builds 테이블 생성 (기존 테이블은 DROP 후 재생성)
                self._conn.execute("DROP TABLE IF EXISTS builds")
                self._conn.execute(
                    """
                    CREATE TABLE builds (
                        run_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
                        started_at TEXT,
                        finished_at TEXT,
                        spec_digest TEXT,
                        error TEXT
                    )
                    """
                )
                # finished_at 인덱스 (최신 빌드 우선 조회)
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_builds_finished_at ON builds(finished_at DESC)"
                )
                # 스키마 버전 기록
                self._conn.execute(
                    f"INSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION})"
                )
                self._conn.execute(
                    "DELETE FROM schema_version WHERE version != ?", (SCHEMA_VERSION,)
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """트랜잭션 컨텍스트 매니저."""
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def insert_or_replace(
        self,
        run_id: str,
        status: Literal["ok", "failed"],
        started_at: str | None,
        finished_at: str | None,
        spec_digest: str | None = None,
        error: str | None = None,
    ) -> None:
        """빌드 엔트리를 삽입 또는 대체한다.

        Args:
            run_id: 빌드 실행 식별자
            status: 빌드 상태 (ok/failed)
            started_at: 빌드 시작 시각 (ISO 8601)
            finished_at: 빌드 완료 시각 (ISO 8601)
            spec_digest: spec 해시 (선택)
            error: 오류 메시지 (실패 시)
        """
        try:
            with self._transaction():
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO builds
                    (run_id, status, started_at, finished_at, spec_digest, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, status, started_at, finished_at, spec_digest, error),
                )
        except Exception:
            # ADR 0003: 인덱스 쓰기 실패가 빌드 실패의 원인이 되어서는 안 됨
            pass

    def list_builds(self, limit: int = 50) -> list[BuildEntry]:
        """빌드 목록을 최신 완료 시각 기준 내림차순으로 반환한다.

        Args:
            limit: 반환할 최대 빌드 수

        Returns:
            BuildEntry 목록 (finished_at이 최신인 순)
        """
        cur = self._conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, spec_digest, error
            FROM builds
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(Literal["ok", "failed"], row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
            )
            for row in cur
        ]

    def get(self, run_id: str) -> BuildEntry | None:
        """특정 빌드를 조회한다.

        Args:
            run_id: 빌드 실행 식별자

        Returns:
            BuildEntry 또는 None (미발견 시)
        """
        cur = self._conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, spec_digest, error
            FROM builds
            WHERE run_id = ?
            """,
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return BuildEntry(
            run_id=row[0],
            status=cast(Literal["ok", "failed"], row[1]),
            started_at=row[2],
            finished_at=row[3],
            spec_digest=row[4],
            error=row[5],
        )

    def delete(self, run_id: str) -> None:
        """빌드 엔트리를 삭제한다.

        Args:
            run_id: 빌드 실행 식별자
        """
        try:
            with self._transaction():
                self._conn.execute("DELETE FROM builds WHERE run_id = ?", (run_id,))
        except Exception:
            # 인덱스 실패는 무시
            pass

    def close(self) -> None:
        """연결을 닫는다."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")


def rebuild_index(output_root: Path) -> int:
    """파일시스템 스캔으로 인덱스를 재구축한다.

    output_root 아래의 모든 manifest.json을 스캔하여 인덱스를 다시 빌드한다.
    기존 인덱스는 삭제되고 새로 생성된다.

    Args:
        output_root: 빌드 출력 루트 디렉터리

    Returns:
        재구축된 빌드 수
    """
    import json

    index_path = output_root / _INDEX_FILENAME
    # 기존 인덱스 삭제
    if index_path.exists():
        index_path.unlink()

    index = BuildIndex(output_root)
    count = 0

    if not output_root.exists():
        return 0

    for run_dir in output_root.iterdir():
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        status = "failed" if manifest.get("errors") else "ok"
        started_at = manifest.get("started_at")
        finished_at = manifest.get("finished_at")

        index.insert_or_replace(
            run_id=run_dir.name,
            status=status,  # type: ignore[arg-type]
            started_at=started_at,
            finished_at=finished_at,
        )
        count += 1

    return count


__all__ = ["BuildIndex", "BuildEntry", "rebuild_index", "SCHEMA_VERSION"]
