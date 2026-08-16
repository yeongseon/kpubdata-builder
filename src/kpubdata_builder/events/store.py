"""Append-only SQLite 기반 run event store (#496).

``store.build_index.BuildIndex``(#309, ADR 0003)와 같은 SQLite 동시성 패턴
(WAL 모드, busy_timeout, thread-local connection, 트랜잭션)을 재사용하지만
failure policy는 다르다:

- ``BuildIndex``는 ``manifest.json``의 **파생·재구축 가능한 인덱스**라서 쓰기
  실패를 삼켜도 된다(ADR 0003) — 잃어도 파일시스템 스캔으로 다시 만들 수 있다.
- 이 store는 run event timeline의 **유일한 정본**이다 — 다시 만들 수 있는
  원본이 없다. 그래서 ``append()``는 실패를 삼키지 않고 그대로 전파한다
  (#496 "lost event 없음"). 이 store를 감싸는 pipeline 쪽 호출부
  (``events.recorder.BuildEventRecorder``)는 그 실패가 *다른* 정본(manifest,
  소스 outcome)을 침범하지 않도록 흡수하되, ``BuildManifest.warnings``로
  드러낸다 — store 자체는 절대 자기 실패를 조용히 숨기지 않는다.

동시성: 여러 source가 ``ThreadPoolExecutor``(#247)로 병렬 실행되며 각자
event를 append한다. ``AUTOINCREMENT`` 기본키는 SQLite가 커밋 순서대로 부여하는
전역 monotonic sequence이므로, 이 값 하나로 서로 다른 스레드의 append 순서를
손실 없이, causal ordering을 지어내지 않고 그대로 보존할 수 있다(#496 병렬
ordering 정책).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ..spec.models import JsonValue
from .models import BuildEvent, EventName, EventStatus, StageName

SCHEMA_VERSION = 1

_EVENTS_FILENAME = "_build_events.sqlite"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    source_key TEXT,
    stage TEXT,
    message TEXT,
    metrics TEXT
)
"""

_SELECT_COLUMNS = "seq, run_id, timestamp, event, status, source_key, stage, message, metrics"


class BuildEventStore:
    """단일 output_root의 모든 run에 대한 append-only event 저장소.

    ``BuildIndex``와 동일하게 output_root 아래 별도 SQLite 파일
    (``_build_events.sqlite``)을 쓴다 — manifest.json/BuildIndex와 독립적인
    파일이라 이 store의 스키마 변경이 다른 정본에 영향을 주지 않는다.
    """

    def __init__(self, output_root: Path, *, db_path: Path | None = None) -> None:
        self._output_root = output_root
        self._db_path = db_path if db_path is not None else output_root / _EVENTS_FILENAME
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return cast(sqlite3.Connection, self._local.conn)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=30.0)  # busy_timeout: 동시성 경합 대기
        conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기 + 병렬 append 허용
        return conn

    def _init_db(self) -> None:
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            cur = self._conn.execute("SELECT version FROM schema_version")
            if cur.fetchone() is None:
                # v1은 첫 배포다 — 파괴적 마이그레이션(DROP)이 필요 없다. 이 store는
                # append-only 정본이므로(BuildIndex와 달리) 스키마가 바뀌어도 절대
                # 기존 event 행을 DROP하지 않는다 — 향후 버전은 ALTER/신규 컬럼
                # 추가로만 마이그레이션해야 한다.
                self._conn.execute(
                    f"INSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION})"
                )
            self._conn.execute(_CREATE_TABLE_SQL)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_build_events_run_seq ON build_events(run_id, seq)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def append(self, event: BuildEvent) -> BuildEvent:
        """event를 append하고, store가 부여한 ``seq``가 채워진 새 인스턴스를 반환한다.

        실패를 삼키지 않는다(#496) — 이 store는 event timeline의 유일한 정본이라
        ``BuildIndex``(ADR 0003, 파생 인덱스)와 달리 쓰기 실패를 조용히
        무시하면 event가 영구히 사라진다. 호출부가 실패 처리 정책을 정한다.

        예외:
            sqlite3.Error: 기록에 실패한 경우 그대로 전파된다.
        """
        timestamp = event.timestamp
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("event.timestamp must be timezone-aware")
        metrics_json = json.dumps(event.metrics) if event.metrics is not None else None
        with self._transaction():
            cur = self._conn.execute(
                """
                INSERT INTO build_events
                    (run_id, timestamp, event, status, source_key, stage, message, metrics)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    timestamp.astimezone(timezone.utc).isoformat(),
                    event.event,
                    event.status,
                    event.source_key,
                    event.stage,
                    event.message,
                    metrics_json,
                ),
            )
            seq = cur.lastrowid
        if seq is None:  # pragma: no cover - sqlite3 always assigns lastrowid on INSERT
            raise RuntimeError("build event insert did not return a row id")
        return replace(event, seq=seq)

    def list_for_run(self, run_id: str, *, limit: int, tail: bool) -> tuple[BuildEvent, ...]:
        """단일 run의 event를 chronological ascending 순서로 최대 ``limit``개 반환한다.

        ``tail=False``(기본)는 run 시작부터 ``limit``개, ``tail=True``는 가장 최근
        ``limit``개를 고르되 반환 자체는 항상 오름차순이다(#496 timeline
        rendering 정책) — 클라이언트가 매번 뒤집을 필요가 없다. bounded query다:
        limit이 항상 SQL ``LIMIT``으로 전달되어 테이블 전체를 읽지 않는다.
        """
        if tail:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM build_events "
                "WHERE run_id = ? ORDER BY seq DESC LIMIT ?",
                (run_id, limit),
            )
            rows = list(cur.fetchall())
            rows.reverse()
        else:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM build_events "
                "WHERE run_id = ? ORDER BY seq ASC LIMIT ?",
                (run_id, limit),
            )
            rows = list(cur.fetchall())
        return tuple(_row_to_event(row) for row in rows)

    def close(self) -> None:
        """연결을 닫는다 (WAL을 메인 DB 파일로 체크포인트한 뒤).

        ``BuildIndex.close()``와 동일한 패턴 — 테스트/재배치에서 파일을
        안전하게 옮길 수 있게 한다.
        """
        if hasattr(self._local, "conn"):
            self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._local.conn.close()
            delattr(self._local, "conn")


def _row_to_event(row: tuple[object, ...]) -> BuildEvent:
    seq, run_id, timestamp_text, event, status, source_key, stage, message, metrics_json = row
    metrics: dict[str, JsonValue] | None = None
    if metrics_json is not None:
        try:
            parsed = json.loads(cast(str, metrics_json))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metrics = cast(dict[str, JsonValue], parsed)
    return BuildEvent(
        seq=cast(int, seq),
        timestamp=datetime.fromisoformat(cast(str, timestamp_text)),
        run_id=cast(str, run_id),
        event=cast(EventName, event),
        status=cast(EventStatus, status),
        source_key=cast("str | None", source_key),
        stage=cast("StageName | None", stage),
        message=cast("str | None", message),
        metrics=metrics,
    )


__all__ = ["BuildEventStore", "SCHEMA_VERSION"]
