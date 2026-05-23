"""브론즈 단계 산출물을 실행 워크스페이스에 저장한다.

이 모듈은 BronzeArtifact를 실행 워크스페이스 아래의 결정적 경로에 저장하고,
raw_records JSONL과 metadata JSON을 함께 기록한다.

주요 구성:
    - BronzePersistResult: 저장 경로 결과 객체
    - persist_bronze_artifact: 브론즈 산출물 파일 기록 함수
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...spec import JsonValue
from .models import BronzeArtifact, ProvenanceEvent

_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


@dataclass(frozen=True)
class BronzePersistResult:
    """브론즈 산출물을 위해 기록된 파일시스템 경로.

    속성:
        bronze_dir: 산출물이 저장된 브론즈 디렉터리.
        records_path: raw_records JSONL 파일 경로.
        metadata_path: 메타데이터 JSON 파일 경로.
    """

    bronze_dir: Path
    records_path: Path
    metadata_path: Path


def _validate_path_segment(value: str, *, field_name: str) -> None:
    """워크스페이스를 벗어날 수 있는 경로 세그먼트를 거부한다.

    매개변수:
        value: 검증할 경로 세그먼트.
        field_name: 오류 메시지에 사용할 필드명.

    예외:
        ValueError: 비어 있거나 허용되지 않은 문자가 포함된 경우.
    """
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading/trailing whitespace")
    if not _SAFE_PATH_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} contains unsafe characters: {value!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


def _artifact_id(artifact: BronzeArtifact) -> str:
    """source_key와 fetch_params로부터 짧은 결정적 ID를 생성한다.

    동일한 소스와 동일한 fetch_params 조합은 항상 같은 artifact_id를 갖게 되어
    결과 경로를 예측 가능하게 유지한다.
    """
    key_material = json.dumps(
        {"source_key": artifact.source_key, "fetch_params": artifact.fetch_params},
        sort_keys=True,
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:12]


def persist_bronze_artifact(
    artifact: BronzeArtifact,
    *,
    output_root: Path,
    run_id: str,
) -> BronzePersistResult:
    """원시 레코드와 메타데이터를 build/{run_id}/bronze/{source_key}/{artifact_id} 아래에 기록한다.

    run_id 또는 source_key에 안전하지 않은 경로 문자가 포함되면 ValueError를 발생시킨다.
    """
    _validate_path_segment(run_id, field_name="run_id")

    # 파일시스템용으로 source_key를 정리한다(예: "datago.apt_trade" → "datago.apt_trade").
    source_key_segment = artifact.source_key.replace("/", "_")
    _validate_path_segment(source_key_segment, field_name="source_key")

    artifact_id = _artifact_id(artifact)

    bronze_dir = output_root / run_id / "bronze" / source_key_segment / artifact_id
    records_path = bronze_dir / "raw_records.jsonl"
    metadata_path = bronze_dir / "metadata.json"

    # 최종 포함 여부 검사: 해석된 경로는 반드시 output_root 아래에 있어야 한다.
    resolved_root = output_root.resolve()
    resolved_bronze = bronze_dir.resolve()
    if not str(resolved_bronze).startswith(str(resolved_root)):
        raise ValueError(
            f"Resolved bronze directory {resolved_bronze} escapes output_root {resolved_root}"
        )

    bronze_dir.mkdir(parents=True, exist_ok=True)

    records_content = "".join(
        f"{json.dumps(record, ensure_ascii=False, sort_keys=True)}\n"
        for record in artifact.raw_records
    )
    records_path.write_text(records_content, encoding="utf-8")

    metadata = _metadata_for_artifact(
        artifact,
        records_path=records_path,
        metadata_path=metadata_path,
        bronze_dir=bronze_dir,
    )
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return BronzePersistResult(
        bronze_dir=bronze_dir,
        records_path=records_path,
        metadata_path=metadata_path,
    )


def _metadata_for_artifact(
    artifact: BronzeArtifact,
    *,
    records_path: Path,
    metadata_path: Path,
    bronze_dir: Path,
) -> dict[str, JsonValue]:
    """BronzeArtifact에 대한 메타데이터 payload를 구성한다."""
    provenance = artifact.provenance
    return {
        "source_key": artifact.source_key,
        "fetch_params": artifact.fetch_params,
        "fetched_at": _format_datetime(artifact.fetched_at),
        "provenance": _provenance_to_dict(provenance) if provenance else None,
        "record_count": artifact.record_count,
        "artifact_paths": {
            "records": str(records_path.relative_to(bronze_dir)),
            "metadata": str(metadata_path.relative_to(bronze_dir)),
        },
    }


def _provenance_to_dict(provenance: ProvenanceEvent) -> dict[str, JsonValue]:
    """ProvenanceEvent를 JSON 직렬화 가능한 dict로 바꾼다."""
    return {
        "operation": provenance.operation,
        "source_key": provenance.source_key,
        "fetch_params": provenance.fetch_params,
        "fetched_at": _format_datetime(provenance.fetched_at),
    }


def _format_datetime(value: datetime) -> str:
    """datetime 값을 ISO 8601 문자열로 변환한다."""
    return value.isoformat()
