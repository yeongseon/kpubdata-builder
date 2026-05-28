"""스냅샷 기반 증분 빌드 지원 (#15).

이전 빌드의 스냅샷(데이터 체크섬 + fetch 파라미터)을 저장·로드하고, 현재
데이터/파라미터와 비교해 변경 여부를 판단한다. 변경이 없으면 재빌드를 건너뛰어
빌드 시간과 API 호출을 줄일 수 있다.

스냅샷 저장 구조::

    {root}/.kpubdata-builder/snapshots/{dataset_id}/snapshot.json

주요 구성:
    - BuildSnapshot: 마지막 빌드 스냅샷 모델
    - compute_records_checksum: 재현 가능한 SHA-256 체크섬
    - save_snapshot / load_snapshot: 디스크 저장·로드
    - has_data_changed: 이전 스냅샷 대비 변경 여부 판단
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .spec import JsonValue
from .stages._path_safety import validate_path_segment

_SNAPSHOT_DIRNAME = ".kpubdata-builder"


@dataclass(frozen=True)
class BuildSnapshot:
    """마지막 빌드의 데이터 버전 스냅샷.

    속성:
        dataset_id: 데이터셋 식별자.
        built_at: 빌드 시각 (UTC ISO 8601 문자열).
        data_checksum: 데이터의 재현 가능한 체크섬 ("sha256:...").
        record_count: 레코드 수.
        source_params: fetch 파라미터 스냅샷.
    """

    dataset_id: str
    built_at: str
    data_checksum: str
    record_count: int = 0
    source_params: dict[str, JsonValue] = field(default_factory=dict)


def compute_records_checksum(records: Sequence[Mapping[str, JsonValue]]) -> str:
    """레코드의 재현 가능한 SHA-256 체크섬을 계산한다.

    정렬 키 기반 JSON 직렬화로 키 순서 차이를 제거한다.

    매개변수:
        records: 체크섬을 계산할 레코드 시퀀스.

    반환값:
        str: "sha256:" 접두사가 붙은 16진 해시.
    """
    payload = json.dumps(
        [dict(record) for record in records],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _snapshot_path(root: Path, dataset_id: str) -> Path:
    """dataset_id에 대한 스냅샷 파일 경로를 계산한다(경로 안전성 검증 포함)."""
    validate_path_segment(dataset_id, field_name="dataset_id")
    return root / _SNAPSHOT_DIRNAME / "snapshots" / dataset_id / "snapshot.json"


def save_snapshot(snapshot: BuildSnapshot, *, root: Path) -> Path:
    """스냅샷을 root 아래 결정적 JSON으로 저장하고 경로를 반환한다.

    매개변수:
        snapshot: 저장할 스냅샷.
        root: 워크스페이스 루트.

    반환값:
        Path: 기록된 snapshot.json 경로.
    """
    path = _snapshot_path(root, snapshot.dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_id": snapshot.dataset_id,
        "built_at": snapshot.built_at,
        "data_checksum": snapshot.data_checksum,
        "record_count": snapshot.record_count,
        "source_params": snapshot.source_params,
    }
    _ = path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_snapshot(dataset_id: str, *, root: Path) -> BuildSnapshot | None:
    """저장된 스냅샷을 로드한다(없으면 None).

    매개변수:
        dataset_id: 데이터셋 식별자.
        root: 워크스페이스 루트.

    반환값:
        BuildSnapshot | None: 스냅샷 객체 또는 None.
    """
    path = _snapshot_path(root, dataset_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return BuildSnapshot(
        dataset_id=str(data["dataset_id"]),
        built_at=str(data["built_at"]),
        data_checksum=str(data["data_checksum"]),
        record_count=int(data.get("record_count", 0)),
        source_params=dict(data.get("source_params", {})),
    )


def has_data_changed(
    records: Sequence[Mapping[str, JsonValue]],
    source_params: Mapping[str, JsonValue],
    snapshot: BuildSnapshot | None,
) -> bool:
    """이전 스냅샷 대비 데이터 또는 파라미터가 바뀌었는지 판단한다.

    매개변수:
        records: 현재 데이터 레코드.
        source_params: 현재 fetch 파라미터.
        snapshot: 이전 스냅샷(없으면 첫 빌드).

    반환값:
        bool: 변경되었거나 첫 빌드면 True.
    """
    if snapshot is None:
        return True
    if dict(source_params) != snapshot.source_params:
        return True
    return compute_records_checksum(records) != snapshot.data_checksum


__all__ = [
    "BuildSnapshot",
    "compute_records_checksum",
    "has_data_changed",
    "load_snapshot",
    "save_snapshot",
]
