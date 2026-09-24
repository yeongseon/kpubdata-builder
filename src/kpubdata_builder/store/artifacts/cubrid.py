"""CubridArtifactStore — manifest 문서를 CUBRID 정본으로 두는 저장소 (ADR 0016).

산출물 바이트와 run 워크스페이스는 ``LocalArtifactStore`` 에 위임한다(FS/블록 볼륨).
manifest 문서만 CUBRID ``manifests`` 행을 정본으로 삼고 FS 는 미러(캐시)로 유지한다 —
FS 미러가 항상 유지되므로 벌크 스캔(datasets/list_builds)은 계속 FS 에서 동작하고,
단일-run 조회(``get_manifest``)는 CUBRID 정본을 우선한다.

이 모듈은 make_artifact_store() 의 cubrid 분기에서만 import 된다 — sqlalchemy 를
import 하므로 기본(sqlite/local) 경로에 optional 의존성을 끌어들이지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Column, MetaData, String, Table, Text, delete, insert, select

from .local import LocalArtifactStore

if TYPE_CHECKING:
    from sqlalchemy import Engine


class CubridArtifactStore:
    """manifest 정본을 CUBRID 에 두는 ArtifactStore (바이트는 FS 위임)."""

    def __init__(self, output_root: Path, engine: Engine) -> None:
        self._local = LocalArtifactStore(output_root)
        self._engine = engine
        self._metadata = MetaData()
        self._table = Table(
            "manifests",
            self._metadata,
            Column("run_id", String(255), primary_key=True),
            # manifest JSON 문서 정본. CLOB(Text)로 저장한다(대용량 바이트가 아니라 문서).
            Column("manifest", Text, nullable=False),
            Column("updated_at", String(40), nullable=False),
        )
        self._table.create(self._engine, checkfirst=True)

    def run_dir(self, run_id: str) -> Path:
        # 바이트는 FS(블록 볼륨)에 둔다 — 쿼리 엔진이 실제 경로를 요구한다.
        return self._local.run_dir(run_id)

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        # CUBRID 정본 우선, 없으면 FS 미러 폴백.
        stmt = select(self._table.c.manifest).where(self._table.c.run_id == run_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is not None:
            try:
                data = json.loads(row[0])
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                return data
        return self._local.get_manifest(run_id)

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # 단일 트랜잭션 내 delete+insert — dialect upsert 에 의존하지 않는다.
        with self._engine.begin() as conn:
            conn.execute(delete(self._table).where(self._table.c.run_id == run_id))
            conn.execute(
                insert(self._table).values(run_id=run_id, manifest=payload, updated_at=updated_at)
            )
        # FS 미러 유지(벌크 스캔·바이트 colocate·백업). CUBRID 기록이 성공한 뒤에만 미러.
        self._local.put_manifest(run_id, manifest)

    def list_run_ids(self) -> list[str]:
        # CUBRID 정본 + FS 미러 합집합(승격 실패로 FS 에만 있는 run 도 포함).
        with self._engine.connect() as conn:
            rows = conn.execute(select(self._table.c.run_id)).all()
        ids = {str(r[0]) for r in rows}
        ids.update(self._local.list_run_ids())
        return sorted(ids)


__all__ = ["CubridArtifactStore"]
