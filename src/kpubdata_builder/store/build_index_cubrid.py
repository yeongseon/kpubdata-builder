"""CUBRID 기반 빌드 인덱스 (ADR 0016).

``SqliteBuildIndex`` 와 동일한 ``BuildIndex`` Protocol 을 SQLAlchemy Core 로 구현한다
(ORM 아님 — 기존 raw-SQL 스타일 유지). manifest.json 정본 원칙과 SCHEMA_VERSION,
"인덱스 쓰기 실패가 빌드를 실패시키지 않는다"(ADR 0003 규칙4)는 그대로 지킨다.

이 모듈은 ``make_build_index()`` 의 cubrid 분기에서만 import 된다 — ``sqlalchemy`` 를
import 하므로 기본(sqlite) 경로에 optional 의존성을 끌어들이지 않는다.

동시성: 프로세스 전역 단일 Engine(``backend.get_engine()``, 커넥션 풀 + pool_pre_ping)을
받아 연산마다 ``with engine.begin()`` 으로 짧은 커넥션을 빌린다. 스레드 간 raw
connection 을 재사용하지 않는다(#334 async job ThreadPoolExecutor 대비).

upsert 는 dialect 독립적으로 단일 트랜잭션 내 delete+insert 로 처리한다 — CUBRID
dialect 의 MERGE/ON DUPLICATE 지원 여부에 의존하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    insert,
    inspect,
    or_,
    select,
)

from .build_index import SCHEMA_VERSION, BuildEntry, BuildStatus

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Row

_SCHEMA_VERSION_TABLE = "build_schema_version"


class CubridBuildIndex:
    """CUBRID 기반 빌드 인덱스 (ADR 0016). ``BuildIndex`` Protocol 구현체."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._metadata = MetaData()
        # 파생 인덱스 스키마. 정본은 manifest.json — 스키마 버전이 바뀌면 DROP 후
        # 재생성한다(데이터는 rebuild_index 로 복원 가능).
        self._builds = Table(
            "builds",
            self._metadata,
            Column("run_id", String(255), primary_key=True),
            Column("status", String(16), nullable=False),
            Column("started_at", String(40)),
            Column("finished_at", String(40)),
            Column("spec_digest", String(128)),
            Column("error", Text),
            Column("created_by", String(255)),
            Column("dataset_id", String(255)),
            Column("owner_id", String(255)),
        )
        self._schema_version = Table(
            _SCHEMA_VERSION_TABLE,
            self._metadata,
            Column("version", Integer, primary_key=True),
        )
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """스키마 버전을 확인하고, 불일치 시 builds 테이블을 재생성한다.

        인덱스는 파생물이므로 스키마 변경 시 DROP + recreate 가 안전하다(정본
        manifest.json 에서 rebuild_index 로 재구축 가능).
        """
        with self._engine.begin() as conn:
            existing = set(inspect(conn).get_table_names())
            version: int | None = None
            if _SCHEMA_VERSION_TABLE in existing:
                row = conn.execute(select(self._schema_version.c.version)).first()
                version = int(row[0]) if row is not None else None
            if version == SCHEMA_VERSION:
                return
            # 버전 불일치(또는 최초 생성): 파생 테이블을 재생성한다.
            self._builds.drop(conn, checkfirst=True)
            self._schema_version.drop(conn, checkfirst=True)
            self._schema_version.create(conn, checkfirst=True)
            self._builds.create(conn, checkfirst=True)
            conn.execute(insert(self._schema_version).values(version=SCHEMA_VERSION))

    def _row_to_entry(self, row: Row[Any]) -> BuildEntry:
        m = row._mapping
        return BuildEntry(
            run_id=m["run_id"],
            status=cast(BuildStatus, m["status"]),
            started_at=m["started_at"],
            finished_at=m["finished_at"],
            spec_digest=m["spec_digest"],
            error=m["error"],
            created_by=m["created_by"],
            dataset_id=m["dataset_id"],
            owner_id=m["owner_id"],
        )

    def insert_or_replace(
        self,
        run_id: str,
        status: BuildStatus,
        started_at: str | None,
        finished_at: str | None,
        spec_digest: str | None = None,
        error: str | None = None,
        created_by: str | None = None,
        dataset_id: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        values = {
            "run_id": run_id,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "spec_digest": spec_digest,
            "error": error,
            "created_by": created_by,
            "dataset_id": dataset_id,
            "owner_id": owner_id,
        }
        try:
            # 단일 트랜잭션 내 delete+insert — dialect upsert 에 의존하지 않는다.
            with self._engine.begin() as conn:
                conn.execute(delete(self._builds).where(self._builds.c.run_id == run_id))
                conn.execute(insert(self._builds).values(**values))
        except Exception:
            # ADR 0003 규칙4: 인덱스 쓰기 실패가 빌드 실패의 원인이 되어서는 안 됨.
            pass

    def list_builds(self, limit: int | None = 50) -> list[BuildEntry]:
        stmt = select(self._builds).order_by(self._builds.c.finished_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def list_by_dataset(self, dataset_id: str, limit: int | None = None) -> list[BuildEntry]:
        stmt = (
            select(self._builds)
            .where(self._builds.c.dataset_id == dataset_id)
            .order_by(self._builds.c.finished_at.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def list_recent_owned(
        self, *, limit: int, principal_owner_id: str | None, principal_label: str
    ) -> list[BuildEntry]:
        # ownership 필터를 LIMIT 보다 먼저 WHERE 로 적용한다(#527) — service.auth.
        # principal_owns()와 동일 정책. NULL 비교는 자연히 fail-closed.
        b = self._builds.c
        if principal_owner_id is not None:
            cond = or_(
                and_(b.owner_id.is_not(None), b.owner_id == principal_owner_id),
                and_(b.owner_id.is_(None), b.created_by == principal_label),
            )
        else:
            cond = b.created_by == principal_label
        stmt = select(self._builds).where(cond).order_by(b.finished_at.desc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def list_between(self, start_iso: str, end_iso: str) -> list[BuildEntry]:
        # [start, end) 구간 + finished_at NULL(판정 불가 → 호출자에게 넘김, #516).
        b = self._builds.c
        stmt = (
            select(self._builds)
            .where(
                or_(
                    b.finished_at.is_(None),
                    and_(b.finished_at >= start_iso, b.finished_at < end_iso),
                )
            )
            .order_by(b.finished_at.asc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def latest_successful_finished_at(self) -> str | None:
        # 가장 최근 성공 빌드의 finished_at (#516). 성공 기록 없으면 None.
        b = self._builds.c
        stmt = (
            select(b.finished_at)
            .where(and_(b.status == "ok", b.finished_at.is_not(None)))
            .order_by(b.finished_at.desc())
            .limit(1)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return str(row[0]) if row is not None else None

    def get(self, run_id: str) -> BuildEntry | None:
        stmt = select(self._builds).where(self._builds.c.run_id == run_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return self._row_to_entry(row) if row is not None else None

    def delete(self, run_id: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(delete(self._builds).where(self._builds.c.run_id == run_id))
        except Exception:
            # 인덱스 실패는 무시 (ADR 0003 규칙4).
            pass

    def rebuild(self, entries: Iterable[BuildEntry]) -> int:
        """builds 테이블을 truncate 후 스캔 엔트리로 재삽입한다(단일 트랜잭션).

        재구축은 명시적 관리 작업이라 ``insert_or_replace`` 와 달리 예외를 삼키지
        않는다 — 실패하면 전체가 롤백되어 이전 인덱스가 보존된다.
        """
        count = 0
        with self._engine.begin() as conn:
            conn.execute(delete(self._builds))
            for entry in entries:
                conn.execute(
                    insert(self._builds).values(
                        run_id=entry.run_id,
                        status=entry.status,
                        started_at=entry.started_at,
                        finished_at=entry.finished_at,
                        spec_digest=entry.spec_digest,
                        error=entry.error,
                        created_by=entry.created_by,
                        dataset_id=entry.dataset_id,
                        owner_id=entry.owner_id,
                    )
                )
                count += 1
        return count

    def close(self) -> None:
        """no-op. 전역 Engine 은 공유 자원이라 여기서 dispose 하지 않는다.

        (프로세스 종료 시 ``backend.dispose_engine()`` 이 정리한다.)
        """


__all__ = ["CubridBuildIndex"]
