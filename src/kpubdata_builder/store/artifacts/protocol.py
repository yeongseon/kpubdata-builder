"""ArtifactStore Protocol (ADR 0010/0016).

산출물 워크스페이스 접근과 manifest 문서 소유를 추상화한다. 승인된 설계(ADR 0016):

- **산출물 바이트**(parquet/CSV/HF 레이아웃 등)는 두 백엔드 모두 로컬 파일시스템
  (OCI 블록 볼륨)에 둔다 — ``query/engine.py`` 가 별도 프로세스에서 ``pl.scan_parquet``
  로 실제 경로를 lazy 스캔하므로 파일 경로가 필요하고, 대용량 파일을 RDBMS BLOB 에
  넣는 것은 안티패턴이며 단일 replica 라 공유 오브젝트 스토어 이점이 없다. 따라서
  ``run_dir()`` 는 두 구현체에서 동일하게 FS 경로를 돌려준다.
- **manifest 문서**만 백엔드가 다르다. ``CubridArtifactStore`` 는 CUBRID 행을 정본으로
  삼고 FS 는 미러(캐시)로 유지한다(ADR 0003 supersede). ``LocalArtifactStore`` 는 FS
  파일 자체가 정본이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    """산출물 워크스페이스 + manifest 문서 저장소 인터페이스."""

    def run_dir(self, run_id: str) -> Path:
        """run 산출물 워크스페이스 경로(FS). 바이트 읽기/쓰기·서빙·쿼리에 쓰인다."""
        ...

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        """manifest 문서를 반환한다(없거나 손상 시 None). CUBRID 백엔드는 정본 행 우선."""
        ...

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        """manifest 문서를 authoritative store 에 기록한다(CUBRID 행 + FS 미러)."""
        ...

    def list_run_ids(self) -> list[str]:
        """manifest 를 가진 run_id 목록."""
        ...


__all__ = ["ArtifactStore"]
