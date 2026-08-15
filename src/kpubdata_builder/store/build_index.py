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
# 스키마 버전 2: status 어휘를 ok/failed/cancelled로 확장 (#334 비동기 job 모델 대비)
# 스키마 버전 4: dataset_id 컬럼 추가 (#488). 정본은 BuildSpec snapshot(#487)이며,
# 이 컬럼은 dataset→run 조회 성능을 위한 파생 검색 값일 뿐이다.
# 스키마 버전 5: owner_id 컬럼 추가 (#505). 정본은 manifest.json이며, 이 컬럼은
# canonical stable owner identity에 대한 파생 검색 값일 뿐이다. 이 인덱스는
# 파생물이라 스키마 버전이 바뀌면 테이블을 DROP 후 재생성한다 — 기존 인덱스
# 데이터는 사라지지만 manifest.json에서 rebuild_index()로 재구축할 수 있다.
SCHEMA_VERSION = 5

# 빌드 인덱스 status 어휘. ADR 0003 파생 캐시. manifest.json이 정본.
BuildStatus = Literal["ok", "failed", "cancelled"]

# 인덱스 파일 이름
_INDEX_FILENAME = "_builds.sqlite"


@dataclass(frozen=True)
class BuildEntry:
    """빌드 인덱스 엔트리."""

    run_id: str
    status: BuildStatus
    started_at: str | None
    finished_at: str | None
    spec_digest: str | None
    error: str | None
    created_by: str | None = None
    dataset_id: str | None = None
    owner_id: str | None = None


class BuildIndex:
    """SQLite 기반 빌드 인덱스.

    ADR 0003에 따라:
    - manifest.json이 정본이며, 이 인덱스는 파생물이다
    - 인덱스 쓰기 실패가 빌드 실패의 원인이 되어서는 안 됨
    - WAL 모드 + busy_timeout으로 동시성 안전 장치
    """

    def __init__(self, output_root: Path, *, index_path: Path | None = None) -> None:
        """인덱스를 초기화한다.

        Args:
            output_root: 빌드 출력 루트 디렉터리 (인덱스는 output_root/_builds.sqlite)
            index_path: 인덱스 파일 경로 오버라이드 (rebuild_index의 원자적 교체용 임시 파일 등)
        """
        self._output_root = output_root
        self._index_path = index_path if index_path is not None else output_root / _INDEX_FILENAME
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
                        status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'cancelled')),
                        started_at TEXT,
                        finished_at TEXT,
                        spec_digest TEXT,
                        error TEXT,
                        created_by TEXT,
                        dataset_id TEXT,
                        owner_id TEXT
                    )
                    """
                )
                # finished_at 인덱스 (최신 빌드 우선 조회)
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_builds_finished_at ON builds(finished_at DESC)"
                )
                # dataset_id 인덱스 (#488): dataset→run 조회. snapshot 없는 legacy run은
                # dataset_id가 NULL이므로 자연히 dataset grouping에서 제외된다.
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_builds_dataset_id ON builds(dataset_id)"
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
        status: BuildStatus,
        started_at: str | None,
        finished_at: str | None,
        spec_digest: str | None = None,
        error: str | None = None,
        created_by: str | None = None,
        dataset_id: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        """빌드 엔트리를 삽입 또는 대체한다.

        Args:
            run_id: 빌드 실행 식별자
            status: 빌드 상태 (ok/failed)
            started_at: 빌드 시작 시각 (ISO 8601)
            finished_at: 빌드 완료 시각 (ISO 8601)
            spec_digest: spec 해시 (선택)
            error: 오류 메시지 (실패 시)
            created_by: 빌드를 요청한 주체 라벨 (선택, #388)
            dataset_id: BuildSpec.dataset_id (선택, #488). snapshot이 없는 legacy
                run은 None — dataset_id를 추측해 채우지 않는다.
            owner_id: canonical stable owner identity (선택, #505). manifest.json에
                이 필드가 없는 legacy run은 None.
        """
        try:
            with self._transaction():
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO builds
                    (run_id, status, started_at, finished_at, spec_digest, error, created_by,
                     dataset_id, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        status,
                        started_at,
                        finished_at,
                        spec_digest,
                        error,
                        created_by,
                        dataset_id,
                        owner_id,
                    ),
                )
        except Exception:
            # ADR 0003: 인덱스 쓰기 실패가 빌드 실패의 원인이 되어서는 안 됨
            pass

    def list_builds(self, limit: int | None = 50) -> list[BuildEntry]:
        """빌드 목록을 최신 완료 시각 기준 내림차순으로 반환한다.

        Args:
            limit: 반환할 최대 빌드 수. None이면 모든 빌드를 반환한다.

        Returns:
            BuildEntry 목록 (finished_at이 최신인 순)
        """
        sql = """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            ORDER BY finished_at DESC
        """
        if limit is None:
            cur = self._conn.execute(sql)
        else:
            cur = self._conn.execute(f"{sql} LIMIT ?", (limit,))
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def list_by_dataset(self, dataset_id: str, limit: int | None = None) -> list[BuildEntry]:
        """특정 dataset_id에 속한 빌드 목록을 최신 완료 시각 기준 내림차순 반환한다 (#488).

        Args:
            dataset_id: BuildSpec.dataset_id 값 (정확히 일치하는 것만).
            limit: 반환할 최대 빌드 수. None이면 해당 dataset의 모든 빌드를 반환한다.

        Returns:
            BuildEntry 목록 (finished_at이 최신인 순). 이 dataset_id로 인덱싱된
            run이 없으면 빈 목록.
        """
        sql = """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            WHERE dataset_id = ?
            ORDER BY finished_at DESC
        """
        if limit is None:
            cur = self._conn.execute(sql, (dataset_id,))
        else:
            cur = self._conn.execute(f"{sql} LIMIT ?", (dataset_id, limit))
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def list_recent_owned(
        self, *, limit: int, principal_owner_id: str | None, principal_label: str
    ) -> list[BuildEntry]:
        """principal 소유 build만 최신 완료 순으로 최대 ``limit``개 반환한다 (#527).

        ownership 필터를 Python에서 전체 결과에 사후 적용하기 전에 LIMIT을
        걸면(예: 전역 최신 10건을 먼저 가져온 뒤 필터링), 다른 principal의
        최신 run들이 LIMIT을 다 채워 본인의 recent run이 잘릴 수 있다 — 이
        메서드는 필터를 SQL WHERE로 LIMIT보다 먼저 적용해 그 문제를 없앤다.

        정책은 ``service.auth.principal_owns()``(#505)와 정확히 동일해야
        한다:

        - 레코드와 principal 양쪽 모두 ``owner_id``가 있으면 그 값을 비교한다
          (``principal_owner_id``가 아닌 경우에만).
        - 그 외(레코드에 ``owner_id``가 없거나 principal에 ``owner_id``가
          없음)에는 ``created_by == principal_label``로 폴백한다.
        - 어느 쪽도 매치하지 않으면 제외한다(fail-closed) — SQL의 NULL 비교는
          자연히 조건을 만족시키지 않으므로 별도 처리가 필요 없다.

        Args:
            limit: 반환할 최대 빌드 수.
            principal_owner_id: 요청 principal의 canonical stable owner_id.
                ``None``이면(legacy/owner_id 미설정 principal) 레코드
                ``owner_id``와 무관하게 항상 ``created_by`` 비교로 폴백한다
                (``principal_owns()``와 동일).
            principal_label: 요청 principal의 legacy 비교용 label
                (``Principal.label``).

        Returns:
            BuildEntry 목록 (finished_at 내림차순, 최대 limit개).
        """
        if principal_owner_id is not None:
            sql = """
                SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                       dataset_id, owner_id
                FROM builds
                WHERE (owner_id IS NOT NULL AND owner_id = ?)
                   OR (owner_id IS NULL AND created_by = ?)
                ORDER BY finished_at DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (principal_owner_id, principal_label, limit)
        else:
            sql = """
                SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                       dataset_id, owner_id
                FROM builds
                WHERE created_by = ?
                ORDER BY finished_at DESC
                LIMIT ?
            """
            params = (principal_label, limit)
        cur = self._conn.execute(sql, params)
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def list_between(self, start_iso: str, end_iso: str) -> list[BuildEntry]:
        """``[start_iso, end_iso)`` 반열린 구간에 완료된 빌드를 오름차순으로 반환한다 (#516).

        ``finished_at``이 문자열 비교로 구간 안에 드는 행에 더해 ``finished_at``이
        NULL인 행도 함께 반환한다 — SQL에서는 어느 구간에 속하는지 판정할 수 없는
        값이므로 침묵하며 제외하지 않고 호출자에게 넘겨 malformed로 셀 수 있게
        한다(#516 partial 판정). ISO 8601 UTC(``Z`` suffix, zero-padded) 형식이면
        문자열 정렬이 시각 정렬과 일치하지만, 그 형식을 벗어나면서 문자열 정렬상
        구간 밖으로 벗어나는 극단적인 legacy 값은 이 쿼리 자체에서 걸러질 수 있다
        — 실제 배포에서 malformed 값은 대체로 같은 날짜 prefix를 공유하는 손상된
        ISO 문자열(NULL 포함)이라 이 한계는 좁다. ``idx_builds_finished_at``
        인덱스를 활용해 테이블 전체를 로드하지 않는다.

        Args:
            start_iso: 구간 시작(포함), ISO 8601 UTC 문자열.
            end_iso: 구간 끝(제외), ISO 8601 UTC 문자열.

        Returns:
            BuildEntry 목록 (finished_at 오름차순, NULL은 먼저 온다).
        """
        cur = self._conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            WHERE finished_at IS NULL OR (finished_at >= ? AND finished_at < ?)
            ORDER BY finished_at ASC
            """,
            (start_iso, end_iso),
        )
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def latest_successful_finished_at(self) -> str | None:
        """가장 최근 성공(``status='ok'``) 빌드의 ``finished_at``을 반환한다 (#516).

        Artifact Store의 ``last_write_at`` 근거로 쓰인다 — 실제 성공한 빌드가
        artifact를 기록했다는 확실한 증거만 반환하며, 성공 기록이 없으면
        ``None``이다("모른다"를 임의 값으로 채우지 않는다). ``idx_builds_finished_at``
        인덱스를 활용하는 bounded 쿼리다.

        Returns:
            ISO 8601 문자열 또는 성공 기록이 없으면 None.
        """
        cur = self._conn.execute(
            """
            SELECT finished_at FROM builds
            WHERE status = 'ok' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        return cast(str | None, row[0]) if row is not None else None

    def get(self, run_id: str) -> BuildEntry | None:
        """특정 빌드를 조회한다.

        Args:
            run_id: 빌드 실행 식별자

        Returns:
            BuildEntry 또는 None (미발견 시)
        """
        cur = self._conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
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
            status=cast(BuildStatus, row[1]),
            started_at=row[2],
            finished_at=row[3],
            spec_digest=row[4],
            error=row[5],
            created_by=row[6],
            dataset_id=row[7],
            owner_id=row[8],
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
        """연결을 닫는다.

        파일을 이름 변경(rename)만으로 안전하게 이관할 수 있도록,
        닫기 전에 WAL 내용을 메인 DB 파일로 체크포인트한다.
        """
        if hasattr(self._local, "conn"):
            self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._local.conn.close()
            delattr(self._local, "conn")


def rebuild_index(output_root: Path) -> int:
    """파일시스템 스캔으로 인덱스를 재구축한다.

    output_root 아래의 모든 manifest.json을 스캔하여 새 인덱스를 .tmp 파일에
    빌드한 뒤, 기존 인덱스를 .bak으로 백업하고 .tmp를 원자적으로 rename하여
    교체한다 (#366). 스캔 도중 실패해도 기존 인덱스는 그대로 남는다.

    Args:
        output_root: 빌드 출력 루트 디렉터리

    Returns:
        재구축된 빌드 수
    """
    import json

    import yaml

    from ..spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME, compute_spec_digest

    if not output_root.exists():
        return 0

    index_path = output_root / _INDEX_FILENAME
    tmp_path = output_root / f"{_INDEX_FILENAME}.tmp"
    backup_path = output_root / f"{_INDEX_FILENAME}.bak"

    # 이전 실행이 중단되어 남은 임시 파일 정리
    tmp_path.unlink(missing_ok=True)

    index = BuildIndex(output_root, index_path=tmp_path)
    try:
        count = 0
        for run_dir in output_root.iterdir():
            if not run_dir.is_dir():
                continue
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue

            status = "failed" if manifest.get("errors") else "ok"
            started_at = manifest.get("started_at")
            finished_at = manifest.get("finished_at")
            snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
            spec_digest: str | None = None
            dataset_id: str | None = None
            # is_file()은 symlink를 따라가므로, 워크스페이스 밖 파일을
            # 해시하는 것을 막기 위해 symlink는 명시적으로 거부한다.
            if snapshot_path.is_file() and not snapshot_path.is_symlink():
                try:
                    snapshot_bytes = snapshot_path.read_bytes()
                except OSError:
                    snapshot_bytes = None
                if snapshot_bytes is not None:
                    spec_digest = compute_spec_digest(snapshot_bytes)
                    # dataset_id는 파생 검색값일 뿐이다 (#488). snapshot YAML을
                    # 읽거나 파싱할 수 없으면 추측하지 않고 None으로 남긴다 —
                    # 인덱스 손상/누락이 정본(BuildSpec snapshot)을 바꾸지 않는다.
                    try:
                        snapshot_doc = yaml.safe_load(snapshot_bytes.decode("utf-8"))
                    except (UnicodeDecodeError, yaml.YAMLError):
                        snapshot_doc = None
                    if isinstance(snapshot_doc, dict):
                        raw_dataset_id = snapshot_doc.get("dataset_id")
                        if isinstance(raw_dataset_id, str) and raw_dataset_id:
                            dataset_id = raw_dataset_id

            index.insert_or_replace(
                run_id=run_dir.name,
                status=status,  # type: ignore[arg-type]
                started_at=started_at,
                finished_at=finished_at,
                spec_digest=spec_digest,
                created_by=manifest.get("created_by"),
                dataset_id=dataset_id,
                owner_id=manifest.get("owner_id"),
            )
            count += 1
    finally:
        index.close()

    # 원자적 교체: 기존 인덱스를 .bak으로 백업 후 .tmp를 원본 자리로 rename
    backup_path.unlink(missing_ok=True)
    if index_path.exists():
        index_path.rename(backup_path)

    try:
        tmp_path.rename(index_path)
    except OSError:
        # 교체 실패 시 백업에서 복원
        if backup_path.exists():
            backup_path.rename(index_path)
        raise
    else:
        backup_path.unlink(missing_ok=True)

    return count


__all__ = ["BuildIndex", "BuildEntry", "rebuild_index", "SCHEMA_VERSION"]
