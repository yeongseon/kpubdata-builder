"""Silver 단계 산출물을 실행 워크스페이스에 저장한다 (#46).

SilverDataset을 output_root/{run_id}/silver/{source_key}/ 아래에 저장한다. 테이블은
parquet으로, 스키마/통계/미리보기/검증 정보는 결정적 JSON으로 기록한다.

주요 구성:
    - SilverPersistResult: 저장 경로 결과 객체
    - persist_silver_dataset: Silver 산출물 파일 기록 함수
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from .._path_safety import ensure_within, validate_path_segment
from .models import SilverDataset


@dataclass(frozen=True)
class SilverPersistResult:
    """Silver 산출물을 위해 기록된 파일시스템 경로.

    속성:
        silver_dir: 산출물이 저장된 Silver 디렉터리.
        table_path: 정제 테이블 parquet 파일 경로.
        schema_path: 스키마 요약 JSON 경로.
        stats_path: 통계 요약 JSON 경로.
        preview_path: 미리보기 JSON 경로.
        validation_path: 검증 결과 JSON 경로.
    """

    silver_dir: Path
    table_path: Path
    schema_path: Path
    stats_path: Path
    preview_path: Path
    validation_path: Path


def _json_default(value: object) -> str:
    """JSON 기본 직렬화기. date/datetime은 ISO 문자열로 변환한다.

    preview 행은 Date/Datetime으로 캐스팅된 컬럼을 포함할 수 있어 plain
    json.dumps가 TypeError를 낼 수 있으므로, 결정적 ISO 문자열로 변환한다.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _write_json(path: Path, payload: object) -> None:
    """payload를 결정적 JSON으로 기록한다 (date/datetime은 ISO 문자열)."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )


def persist_silver_dataset(
    dataset: SilverDataset,
    *,
    output_root: Path,
    run_id: str,
) -> SilverPersistResult:
    """Silver 산출물을 output_root/{run_id}/silver/{source_key}/ 아래에 기록한다.

    run_id 또는 source_key에 안전하지 않은 경로 문자가 포함되면 ValueError를
    발생시킨다.

    매개변수:
        dataset: 저장할 Silver 산출물.
        output_root: 실행 워크스페이스 루트.
        run_id: 빌드 실행 식별자.

    반환값:
        SilverPersistResult: 기록된 파일 경로 모음.
    """
    validate_path_segment(run_id, field_name="run_id")

    source_key_segment = dataset.source_bronze.replace("/", "_")
    validate_path_segment(source_key_segment, field_name="source_key")

    silver_dir = output_root / run_id / "silver" / source_key_segment
    ensure_within(output_root, silver_dir, label="silver directory")

    silver_dir.mkdir(parents=True, exist_ok=True)

    table_path = silver_dir / "table.parquet"
    schema_path = silver_dir / "schema.json"
    stats_path = silver_dir / "stats.json"
    preview_path = silver_dir / "preview.json"
    validation_path = silver_dir / "validation.json"

    dataset.table.write_parquet(table_path)
    _write_json(schema_path, asdict(dataset.schema))
    _write_json(stats_path, asdict(dataset.statistics))
    _write_json(preview_path, asdict(dataset.preview))
    _write_json(validation_path, asdict(dataset.validation))

    return SilverPersistResult(
        silver_dir=silver_dir,
        table_path=table_path,
        schema_path=schema_path,
        stats_path=stats_path,
        preview_path=preview_path,
        validation_path=validation_path,
    )
